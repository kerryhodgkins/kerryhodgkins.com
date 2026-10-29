#!/usr/bin/env python3
"""kh-watch: is everything running? Green or red, with alerts.

  dashboard            check everything now and print green/red (anyone can run it)
  kh-watch run         check, save state, send alerts on changes, write the status page (the timer runs this every minute)
  kh-watch test-alert  send a test alert to every configured channel
  kh-watch init NAME   print a starter config for this server (revenant or haunted), keeping only units that exist here

Config:  /etc/kh-watch/watch.conf   (checks; readable by everyone, holds no secrets)
Secrets: /etc/kh-watch/secrets.env  (Resend key, ntfy topic, page token; root only, read by the timer service)
State:   /var/lib/kh-watch/state.json, page in /var/lib/kh-watch/www/<token>/
Python standard library only.
"""
import re, concurrent.futures, configparser, datetime, fnmatch, html, json, os, shutil, socket, ssl, subprocess, sys, time, urllib.request

CONF = os.environ.get('KW_CONF', '/etc/kh-watch/watch.conf')
STATE_DIR = os.environ.get('KW_STATE', '/var/lib/kh-watch')
NTFY = os.environ.get('KW_NTFY', 'https://ntfy.sh')
RESEND = os.environ.get('KW_RESEND', 'https://api.resend.com')
FAILS_TO_ALERT = 2          # two failed runs in a row (about two minutes) before anyone is woken up
UA = 'kh-watch/1'


# ---------- checks: each returns (ok, detail) ----------

def c_unit(name):
    r = subprocess.run(['systemctl', 'is-active', name], capture_output=True, text=True)
    state = r.stdout.strip() or r.stderr.strip() or 'unknown'
    if name.endswith('.timer'):
        return state == 'active', state
    return state == 'active', state


def c_tcp(target):
    host, port = target.rsplit(':', 1)
    t = time.monotonic()
    try:
        with socket.create_connection((host, int(port)), timeout=6):
            return True, f'{int((time.monotonic() - t) * 1000)} ms'
    except OSError as e:
        return False, short(e)


def fetch(url, timeout=10):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Cache-Control': 'no-cache'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(200_000).decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, ''


def c_http(url, contains=''):
    t = time.monotonic()
    try:
        code, body = fetch(url)
    except Exception as e:
        return False, short(e)
    ms = int((time.monotonic() - t) * 1000)
    if code >= 400:
        return False, f'HTTP {code}'
    if contains and contains not in body:
        return False, f'HTTP {code}, missing "{contains}"'
    return True, f'HTTP {code}, {ms} ms'


def c_cert(host, days=14):
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=8) as s, ctx.wrap_socket(s, server_hostname=host) as t:
            exp = ssl.cert_time_to_seconds(t.getpeercert()['notAfter'])
    except Exception as e:
        return False, short(e)
    left = int((exp - time.time()) // 86400)
    return left >= int(days), f'{left} days left'


def c_disk(path='/', min_free=10):
    u = shutil.disk_usage(path)
    free = 100 * u.free / u.total
    return free >= float(min_free), f'{free:.0f}% free ({u.free // 2**30} GB)'


def c_memory(min_free=10):
    info = {}
    with open('/proc/meminfo') as f:
        for line in f:
            k, v = line.split(':', 1); info[k] = int(v.split()[0])
    free = 100 * info.get('MemAvailable', 0) / info['MemTotal']
    return free >= float(min_free), f'{free:.0f}% available'


def c_heartbeat(url, max_age=300):
    """Another server's watcher writes status.json every minute; if it stops, that server (or its watcher) is down."""
    try:
        code, body = fetch(url)
        d = json.loads(body)
    except Exception as e:
        return False, short(e)
    age = int(time.time() - d.get('time', 0))
    if age > int(max_age):
        return False, f'last report {age // 60} min ago'
    bad = [c['name'] for c in d.get('checks', []) if not c.get('ok')]
    return True, (f'reporting; {len(bad)} red there: ' + ', '.join(bad[:3])) if bad else 'reporting, all green there'


def c_journal(unit, pattern, max_age=360):
    """Something should show up in a service's log regularly, e.g. Synchronet's DOVE-Net (QNET) calls."""
    def last(since):
        r = subprocess.run(['journalctl', '-u', unit, '--since', since, '--grep', pattern, '-n', '1', '-o', 'short-unix',
                            '--no-pager', '-q'], capture_output=True, text=True, timeout=20)
        line = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ''
        try:
            return float(line.split()[0]) if line else None
        except ValueError:
            return None
    t = last(f'-{int(max_age)} min')
    if t:
        return True, f'last seen {ago(t)}'
    t = last('-14 days')
    return False, (f'last seen {ago(t)} (limit {int(max_age) // 60} h)' if t else 'not seen in 14 days (or no access to the log)')


TYPES = {'unit': lambda c: c_unit(c['unit']),
         'journal': lambda c: c_journal(c['unit'], c['pattern'], c.get('max_age', 360)),
         'tcp': lambda c: c_tcp(c['target']),
         'http': lambda c: c_http(c['url'], c.get('contains', '')),
         'cert': lambda c: c_cert(c['host'], c.get('days', 14)),
         'disk': lambda c: c_disk(c.get('path', '/'), c.get('min_free', 10)),
         'memory': lambda c: c_memory(c.get('min_free', 10)),
         'heartbeat': lambda c: c_heartbeat(c['url'], c.get('max_age', 300))}


def short(e):
    s = str(getattr(e, 'reason', '') or e) or type(e).__name__
    s = re.sub(r'^\[Errno -?\d+\] ', '', s)
    return s[:80]


def load_conf():
    cp = configparser.ConfigParser(interpolation=None, strict=False)   # a section listed twice: the later one wins
    try:
        found = cp.read(CONF)
    except configparser.Error as e:
        sys.exit(f'Problem in {CONF}: {str(e).splitlines()[0]}\nFix that line with: sudo nano {CONF}')
    if not found:
        sys.exit(f'No config at {CONF}. Run: kh-watch init <revenant|haunted> > {CONF}')
    name = cp.get('watch', 'name', fallback=socket.gethostname())
    checks = []
    for sec in cp.sections():
        if sec.startswith('check '):
            c = dict(cp[sec]); c['name'] = sec[6:].strip(); c['group'] = c.get('group', 'Other')
            if c.get('type') not in TYPES:
                c['type'] = 'bad'
            checks.append(c)
    return name, checks


def load_public():
    """[public] maps what visitors see to the checks behind it:  Revenant BBS = Mystic telnet (23), revenantbbs.com"""
    out, on = [], False
    try:
        lines = open(CONF).read().splitlines()
    except OSError:
        return out
    for line in lines:
        t = line.strip()
        if t.startswith('['):
            on = t.lower() == '[public]'; continue
        if on and '=' in t and not t.startswith(('#', ';')):
            k, v = t.split('=', 1)
            names = [n.strip() for n in v.split(',') if n.strip()]
            if k.strip() and names:
                out.append((k.strip(), names))
    return out


def run_checks(checks):
    def one(c):
        if c['type'] == 'bad':
            return {**c, 'ok': False, 'detail': 'unknown check type'}
        try:
            ok, detail = TYPES[c['type']](c)
        except Exception as e:
            ok, detail = False, short(e)
        return {'name': c['name'], 'group': c['group'], 'ok': ok, 'detail': detail}
    with concurrent.futures.ThreadPoolExecutor(16) as pool:
        return list(pool.map(one, checks))


# ---------- dashboard ----------

def dashboard():
    name, checks = load_conf()
    tty = sys.stdout.isatty()
    G, R, D, B, X = (('\033[32m', '\033[31m', '\033[2m', '\033[1m', '\033[0m') if tty else ('',) * 5)
    t = time.time()
    res = run_checks(checks)
    state = read_json(os.path.join(STATE_DIR, 'state.json'))
    width = max([len(r['name']) for r in res] + [10])
    print(f'{B}{name}{X}  {D}{datetime.datetime.now():%a %b %d %H:%M}{X}')
    group = None
    for r in res:
        if r['group'] != group:
            group = r['group']; print(f'\n{D}{group}{X}')
        mark = f'{G}● UP  {X}' if r['ok'] else f'{R}● DOWN{X}'
        since = ''
        s = state.get('checks', {}).get(r['name'])
        if not r['ok'] and s and s.get('down_since'):
            since = f'  {D}(down {dur(s["down_since"])}){X}'
        print(f'  {mark}  {r["name"]:<{width}}  {D}{r["detail"]}{X}{since}')
    bad = [r for r in res if not r['ok']]
    print()
    if bad:
        print(f'{R}{B}{len(bad)} DOWN{X} of {len(res)}', end='')
    else:
        print(f'{G}{B}All {len(res)} green{X}', end='')
    last = state.get('time')
    tail = f'  {D}checked in {time.time() - t:.1f}s' + (f'; watcher last ran {ago(last)}' if last else '; watcher has not run yet') + X
    print(tail)
    if last and time.time() - last > 300:
        print(f'{R}The background watcher has not run for {ago(last)}: alerts are off. Check: systemctl status kh-watch.timer{X}')
    return 1 if bad else 0


def dur(ts):
    return ago(ts).replace(' ago', '')


def ago(ts):
    s = int(time.time() - ts)
    if s < 90: return f'{s}s ago'
    if s < 5400: return f'{s // 60} min ago'
    if s < 172800: return f'{s // 3600} h ago'
    return f'{s // 86400} days ago'


# ---------- run: state, alerts, page ----------

def read_json(p):
    try:
        with open(p) as f: return json.load(f)
    except (OSError, ValueError):
        return {}


def write_atomic(p, text):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + '.tmp'
    with open(tmp, 'w') as f: f.write(text)
    os.chmod(tmp, 0o644); os.replace(tmp, p)


def run():
    name, checks = load_conf()
    now = time.time()
    res = run_checks(checks)
    path = os.path.join(STATE_DIR, 'state.json')
    old = read_json(path).get('checks', {})
    remind = float(os.environ.get('KW_REMIND_HOURS', '6')) * 3600
    new, down, up, still = {}, [], [], []
    for r in res:
        o = old.get(r['name'], {})
        s = {'ok': r['ok'], 'detail': r['detail'], 'group': r['group'],
             'fails': 0 if r['ok'] else o.get('fails', 0) + 1,
             'alerted': o.get('alerted', False), 'down_since': None if r['ok'] else (o.get('down_since') or now),
             'reminded': o.get('reminded')}
        if not r['ok'] and s['fails'] >= FAILS_TO_ALERT and not s['alerted']:
            s['alerted'] = True; s['reminded'] = now; down.append(r)
        elif not r['ok'] and s['alerted'] and remind and now - (s['reminded'] or now) >= remind:
            s['reminded'] = now; still.append(r)
        elif r['ok'] and o.get('alerted'):
            s['alerted'] = False; s['reminded'] = None; up.append({**r, 'was': o.get('down_since')})
        new[r['name']] = s
    write_atomic(path, json.dumps({'name': name, 'time': now, 'checks': new}, indent=1))
    if down or up or still:
        alert(name, down, up, still)
    token = os.environ.get('KW_PAGE_TOKEN', '')
    if token:
        page(name, res, new, now, token)
        pub = load_public()
        if pub:
            public_page(pub, new, now)
    return 0


def alert(name, down, up, still):
    lines = [f'DOWN: {r["name"]} ({r["detail"]})' for r in down]
    lines += [f'Still down: {r["name"]} ({r["detail"]})' for r in still]
    lines += [f'Back up: {r["name"]}' + (f' after {dur(r["was"])}' if r.get('was') else '') for r in up]
    bad = down or still
    title = (f'{name}: {len(down) + len(still)} down' if bad else f'{name}: all clear')
    if bad and up:
        title += f', {len(up)} back up'
    send(title, '\n'.join(lines), urgent=bool(down))


def send(title, body, urgent=False):
    sent = []
    topic = os.environ.get('KW_NTFY_TOPIC', '')
    if topic:
        try:
            req = urllib.request.Request(f'{NTFY}/{topic}', data=body.encode(), method='POST', headers={
                'Title': title.encode('ascii', 'replace').decode(), 'Priority': 'high' if urgent else 'default',
                'Tags': 'red_circle' if urgent else 'green_circle', 'User-Agent': UA})
            urllib.request.urlopen(req, timeout=10).read(); sent.append('phone')
        except Exception as e:
            print(f'ntfy failed: {short(e)}', file=sys.stderr)
    key, to = os.environ.get('KW_RESEND_KEY', ''), os.environ.get('KW_EMAIL_TO', '')
    if key and to:
        link = os.environ.get('KW_PAGE_URL', '')
        msg = {'from': os.environ.get('KW_EMAIL_FROM', 'kh-watch <alerts@kerryhodgkins.com>'), 'to': [to],
               'subject': title, 'text': body + ('\n\nStatus page: ' + link if link else '') + '\n\nOn the server: dashboard'}
        try:
            req = urllib.request.Request(f'{RESEND}/emails', data=json.dumps(msg).encode(), method='POST', headers={
                'Authorization': f'Bearer {key}', 'Content-Type': 'application/json', 'User-Agent': UA})
            urllib.request.urlopen(req, timeout=15).read(); sent.append('email')
        except Exception as e:
            print(f'email failed: {short(e)}', file=sys.stderr)
    return sent


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><meta http-equiv="refresh" content="60"><title>{title}</title>
<style>
:root{{--bg:#0B1E33;--card:#122A44;--text:#E8EEF5;--muted:#9FB3C8;--up:#3FB950;--down:#F85149}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:24px 16px 48px}}h1{{font-size:1.3rem;margin:0 0 4px}}
.sum{{font-size:1.6rem;font-weight:700;margin:16px 0}}.sum.ok{{color:var(--up)}}.sum.bad{{color:var(--down)}}
h2{{font-size:.8rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:24px 0 6px}}
ul{{list-style:none;margin:0;padding:0;background:var(--card);border-radius:8px}}li{{display:flex;flex-wrap:wrap;gap:2px 10px;align-items:baseline;padding:9px 14px;border-top:1px solid rgba(255,255,255,.06)}}
li:first-child{{border-top:0}}.dot{{width:10px;height:10px;border-radius:50%;flex:none;align-self:center}}.up .dot{{background:var(--up)}}.down .dot{{background:var(--down)}}
.n{{flex:1 1 11rem;min-width:0}}.d{{flex:0 1 auto;margin-left:auto;color:var(--muted);font-size:.85rem;text-align:right}}.down .n{{font-weight:600}}
footer{{color:var(--muted);font-size:.8rem;margin-top:28px}}
</style></head><body><main><h1>{name}</h1><div style="color:var(--muted)">Updated {when} · refreshes every minute</div>
<div class="sum {cls}">{summary}</div>{groups}<footer>kh-watch · checks run every minute; alerts after two failures in a row</footer></main></body></html>
"""


def page(name, res, state, now, token):
    bad = [r for r in res if not r['ok']]
    groups, cur, items = [], None, []
    for r in res + [None]:
        if r is None or r['group'] != cur:
            if items: groups.append(f'<h2>{html.escape(cur)}</h2><ul>{"".join(items)}</ul>')
            if r is None: break
            cur, items = r['group'], []
        since = state[r['name']].get('down_since')
        det = r['detail'] + (f' · down {dur(since)}' if since and not r['ok'] else '')
        items.append(f'<li class="{"up" if r["ok"] else "down"}"><span class="dot"></span><span class="n">{html.escape(r["name"])}</span>'
                     f'<span class="d">{html.escape(det)}</span></li>')
    summary = f'{len(bad)} down' if bad else 'All green'
    when = datetime.datetime.fromtimestamp(now).strftime('%a %b %d, %H:%M:%S')
    out = os.path.join(STATE_DIR, 'www', token)
    write_atomic(os.path.join(out, 'index.html'), PAGE.format(title=f'{summary} · {name}', name=html.escape(name), when=when,
                 cls='bad' if bad else 'ok', summary=summary, groups=''.join(groups)))
    write_atomic(os.path.join(out, 'status.json'), json.dumps({'name': name, 'time': now, 'checks': [
        {'name': r['name'], 'ok': r['ok']} for r in res]}))


PUBLIC_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60"><title>{title}</title><meta name="description" content="Live status of the boards and services built by Kerry Hodgkins.">
<style>
:root{{--bg:#F4F6F9;--card:#fff;--text:#0B1E33;--muted:#5B6B7E;--line:#E3E8EF;--up:#1A7F37;--down:#CF222E}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0B1E33;--card:#122A44;--text:#E8EEF5;--muted:#9FB3C8;--line:rgba(255,255,255,.08);--up:#3FB950;--down:#F85149}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:640px;margin:0 auto;padding:32px 16px 48px}}a{{color:inherit}}.top{{font-size:.9rem;color:var(--muted)}}
h1{{font-size:1.5rem;margin:6px 0 2px}}.sum{{display:flex;align-items:center;gap:10px;font-size:1.25rem;font-weight:700;margin:22px 0;padding:16px 18px;border-radius:10px;background:var(--card);border:1px solid var(--line)}}
.sum.ok{{color:var(--up)}}.sum.bad{{color:var(--down)}}
ul{{list-style:none;margin:0;padding:0;background:var(--card);border:1px solid var(--line);border-radius:10px}}
li{{display:flex;flex-wrap:wrap;gap:2px 12px;align-items:center;padding:12px 18px;border-top:1px solid var(--line)}}li:first-child{{border-top:0}}
.dot{{width:11px;height:11px;border-radius:50%;flex:none}}.up .dot{{background:var(--up)}}.down .dot{{background:var(--down)}}
.n{{flex:1 1 10rem;font-weight:600}}.s{{margin-left:auto;font-size:.9rem;font-weight:600}}.up .s{{color:var(--up)}}.down .s{{color:var(--down)}}
footer{{color:var(--muted);font-size:.85rem;margin-top:24px}}
</style></head><body><main><div class="top"><a href="https://kerryhodgkins.com/">kerryhodgkins.com</a></div><h1>Service status</h1>
<div class="top">Checked every minute · updated {when}</div>
<div class="sum {cls}"><span class="dot" style="background:currentColor"></span>{summary}</div><ul>{items}</ul>
<footer>A service turns red after two failed checks in a row, about two minutes. No cookies, no trackers.</footer></main></body></html>
"""


def public_state(pub, state):
    """A service is down when any of its checks has failed twice in a row (the same rule as the alerts)."""
    out = []
    for label, names in pub:
        st = [state[n] for n in names if n in state]
        if not st:
            continue
        bad = [s for s in st if not s['ok'] and s['fails'] >= FAILS_TO_ALERT]
        since = min((s['down_since'] for s in bad if s.get('down_since')), default=None)
        out.append({'name': label, 'ok': not bad, 'since': since})
    return out


def badge(services):
    bad = sum(1 for s in services if not s['ok'])
    text = 'All services up' if not bad else (f'{bad} service down' if bad == 1 else f'{bad} services down')
    color = '#3FB950' if not bad else '#F85149'
    w = 34 + int(len(text) * 7.4)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="28" viewBox="0 0 {w} 28" role="img" aria-label="{text}">'
            f'<title>{text}</title><rect x=".5" y=".5" width="{w - 1}" height="27" rx="6" fill="#122A44" stroke="#2A4563"/>'
            f'<circle cx="15" cy="14" r="5" fill="{color}"/><text x="27" y="18.5" fill="#E8EEF5" '
            f'font-family="Inter,system-ui,-apple-system,Segoe UI,sans-serif" font-size="13" font-weight="600">{text}</text></svg>')


def public_page(pub, state, now):
    services = public_state(pub, state)
    bad = [s for s in services if not s['ok']]
    items = ''.join(f'<li class="{"up" if s["ok"] else "down"}"><span class="dot"></span><span class="n">{html.escape(s["name"])}</span>'
                    f'<span class="s">{"Up" if s["ok"] else "Down" + (" for " + dur(s["since"]) if s["since"] else "")}</span></li>' for s in services)
    summary = 'All services are up' if not bad else (f'{len(bad)} service is down' if len(bad) == 1 else f'{len(bad)} services are down')
    when = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).astimezone(PUBLIC_TZ).strftime('%b %d, %I:%M %p %Z').replace(' 0', ' ')
    out = os.path.join(STATE_DIR, 'www', 'public')
    write_atomic(os.path.join(out, 'index.html'), PUBLIC_PAGE.format(title=('All services up' if not bad else summary) + ' · Kerry Hodgkins',
                 when=when, cls='bad' if bad else 'ok', summary=summary, items=items))
    write_atomic(os.path.join(out, 'status.json'), json.dumps({'time': int(now), 'services': [
        {'name': s['name'], 'up': s['ok']} for s in services]}))
    write_atomic(os.path.join(out, 'badge.svg'), badge(services))


try:
    from zoneinfo import ZoneInfo
    PUBLIC_TZ = ZoneInfo(os.environ.get('KW_PUBLIC_TZ', 'America/Chicago'))
except Exception:
    PUBLIC_TZ = datetime.timezone.utc


PUBLIC_DEFAULT = [
    ('Revenant BBS', ['Mystic telnet (23)', 'Mystic SSH (2222)', 'revenantbbs.com']),
    ('Revenant BBS web dialer', ['Mystic web dialer port (2323)', 'Web dialer bridge: revenant']),
    ('Haunted Node', ['Haunted Node telnet (23)', 'Haunted Node SSH (2222)']),
    ('Haunted Node web dialer', ['Web dialer bridge: hauntednode']),
    ('NodeDial BBS list', ['nodedial.com', 'Board list data']),
    ('Revenant Poker', ['Poker hub', 'Poker hub port (7377)']),
    ('Sleepless Node', ['sleeplessnode.com', 'Sleepless Node telnet (666)']),
    ('Newsletter sign-up', ['Newsletter sign-up']),
]


def init_public():
    """Print a [public] section using only checks that exist in this server's config."""
    _, checks = load_conf()
    have = {c['name'] for c in checks}
    lines = ['', '# What visitors see at status.kerryhodgkins.com. Left: the public name. Right: the checks behind it (all must be up).', '[public]']
    for label, names in PUBLIC_DEFAULT:
        keep = [n for n in names if n in have]
        if keep:
            lines.append(f'{label} = {", ".join(keep)}')
    print('\n'.join(lines))


# ---------- init: starter configs ----------

REVENANT_IP, HAUNTED_IP = '137.220.60.147', '66.42.114.213'

TEMPLATES = {
 'revenant': {'name': 'Revenant', 'checks': [
  ('Mystic BBS', 'Revenant BBS', 'unit', 'unit=mis'),
  ('Mystic telnet (23)', 'Revenant BBS', 'tcp', 'target=127.0.0.1:23'),
  ('Mystic SSH (2222)', 'Revenant BBS', 'tcp', 'target=127.0.0.1:2222'),
  ('Mystic web dialer port (2323)', 'Revenant BBS', 'tcp', 'target=127.0.0.1:2323'),
  ('revenantbbs.com', 'Revenant BBS', 'http', 'url=https://revenantbbs.com/'),
  ('nginx', 'Web', 'unit', 'unit=nginx'),
  ('Certificate renewal timer', 'Web', 'unit', 'unit=certbot.timer'),
  ('nodedial.com', 'NodeDial', 'http', 'url=https://nodedial.com/'),
  ('Board list data', 'NodeDial', 'http', 'url=https://nodedial.com/directory.json\ncontains="boards"'),
  ('List monitor timer', 'NodeDial', 'unit', 'unit=nodedial-monitor.timer'),
  ('Sysop dashboard + add form', 'NodeDial', 'unit', 'unit=nodedial-sysop'),
  ('Web dialer bridges', 'NodeDial', 'unit', 'unit=nodedial-bridge@*'),
  ('Poker hub', 'Revenant Poker', 'unit', 'unit=poker-hub'),
  ('Poker hub port (7377)', 'Revenant Poker', 'tcp', 'target=127.0.0.1:7377'),
  ('Newsletter sign-up', 'kerryhodgkins.com', 'http', 'url=https://subscribe.kerryhodgkins.com/healthz'),
  ('kerryhodgkins.com', 'kerryhodgkins.com', 'http', 'url=https://kerryhodgkins.com/'),
  ('Sleepless Node telnet gateway', 'Sleepless Node', 'unit', 'unit=sn-telnet'),
  ('Sleepless Node telnet (666)', 'Sleepless Node', 'tcp', 'target=127.0.0.1:666'),
  ('sleeplessnode.com', 'Sleepless Node', 'http', 'url=https://sleeplessnode.com/'),
  ('Haunted Node telnet (23)', 'Haunted Node (from here)', 'tcp', f'target={HAUNTED_IP}:23'),
  ('Haunted Node SSH (2222)', 'Haunted Node (from here)', 'tcp', f'target={HAUNTED_IP}:2222'),
  ('Cert: revenantbbs.com', 'Certificates', 'cert', 'host=revenantbbs.com'),
  ('Cert: nodedial.com', 'Certificates', 'cert', 'host=nodedial.com'),
  ('Cert: subscribe.kerryhodgkins.com', 'Certificates', 'cert', 'host=subscribe.kerryhodgkins.com'),
  ('Cert: status.kerryhodgkins.com', 'Certificates', 'cert', 'host=status.kerryhodgkins.com'),
  ('Cert: kerryhodgkins.com', 'Certificates', 'cert', 'host=kerryhodgkins.com'),
  ('Disk', 'Server', 'disk', 'path=/\nmin_free=15'),
  ('Memory', 'Server', 'memory', 'min_free=10'),
  ('Firewall (ufw)', 'Server', 'unit', 'unit=ufw'),
 ]},
 'haunted': {'name': 'Haunted Node', 'checks': [
  ('Synchronet BBS', 'Haunted Node', 'unit', 'unit=sbbs'),
  ('Synchronet telnet (23)', 'Haunted Node', 'tcp', 'target=127.0.0.1:23'),
  ('Synchronet SSH (2222)', 'Haunted Node', 'tcp', 'target=127.0.0.1:2222'),
  ('DOVE-Net call to VERT', 'Haunted Node', 'journal', 'unit=sbbs\npattern=QNET.*VERT\nmax_age=360'),
  ('Disk', 'Server', 'disk', 'path=/\nmin_free=15'),
  ('Memory', 'Server', 'memory', 'min_free=10'),
  ('Firewall (ufw)', 'Server', 'unit', 'unit=ufw'),
  ('Revenant watcher', 'Revenant (from here)', 'heartbeat', 'url=REVENANT_STATUS_JSON\nmax_age=300'),
  ('Revenant telnet (23)', 'Revenant (from here)', 'tcp', f'target={REVENANT_IP}:23'),
  ('revenantbbs.com', 'Revenant (from here)', 'http', 'url=https://revenantbbs.com/'),
  ('nodedial.com', 'Revenant (from here)', 'http', 'url=https://nodedial.com/'),
 ]},
}


def units_here():
    r = subprocess.run(['systemctl', 'list-unit-files', '--no-legend', '--no-pager'], capture_output=True, text=True)
    files = {l.split()[0] for l in r.stdout.splitlines() if l.strip()}
    r = subprocess.run(['systemctl', 'list-units', '--all', '--no-legend', '--no-pager', '--plain'], capture_output=True, text=True)
    loaded = {l.split()[0] for l in r.stdout.splitlines() if l.strip()}
    return files | loaded


def init(kind, status_json=''):
    t = TEMPLATES.get(kind)
    if not t:
        sys.exit('kh-watch init revenant|haunted')
    have = units_here()
    unit_of = lambda u: u if '.' in u.split('@')[-1] else u + '.service'
    out = [f'# kh-watch checks for {t["name"]}. Edit freely; each [check ...] is one line on the dashboard.',
           '# Types: unit (systemd), tcp (host:port answers), http (url loads; optional contains=), cert (days left),',
           '#        disk (min_free %), memory (min_free %), heartbeat (another watcher\'s status.json is fresh),',
           '#        journal (a log line matching pattern= appeared in the last max_age minutes)', '',
           '[watch]', f'name = {t["name"]}', '']
    skipped = []
    for name, group, typ, args in t['checks']:
        kv = dict(a.split('=', 1) for a in args.split('\n'))
        if typ == 'unit':
            want = unit_of(kv['unit'])
            if '*' in want:
                pat = want.replace('@*.service', '@*.service') if want.endswith('.service') else want
                hits = sorted(u for u in have if fnmatch.fnmatch(u, pat) and not u.endswith('@.service'))
                if not hits:
                    skipped.append(name); continue
                for u in hits:
                    out += [f'[check {name.replace("bridges", "bridge")}: {u.split("@", 1)[1].rsplit(".", 1)[0]}]', f'group = {group}', 'type = unit', f'unit = {u}', '']
                continue
            if want not in have:
                skipped.append(name); continue
            kv['unit'] = want
        if typ == 'journal' and unit_of(kv['unit']) not in have:
            skipped.append(name); continue
        if typ == 'heartbeat':
            if not status_json:
                skipped.append(name + ' (no Revenant status link given)'); continue
            kv['url'] = status_json
        out += [f'[check {name}]', f'group = {group}', f'type = {typ}'] + [f'{k} = {v}' for k, v in kv.items()] + ['']
    if skipped:
        out += ['# Left out because they are not on this server (add them back if that changes):'] + [f'#   {s}' for s in skipped]
    print('\n'.join(out))


def load_secrets():
    """Lets 'sudo kh-watch test-alert' find the alert settings; the timer gets them from systemd instead."""
    p = os.environ.get('KW_SECRETS', '/etc/kh-watch/secrets.env')
    try:
        with open(p) as f:
            for line in f:
                if '=' in line and not line.lstrip().startswith('#'):
                    k, v = line.rstrip('\n').split('=', 1); os.environ.setdefault(k.strip(), v)
    except OSError:
        pass


def main(argv):
    load_secrets()
    prog = os.path.basename(argv[0])
    cmd = argv[1] if len(argv) > 1 else ('show' if prog.lower() == 'dashboard' else 'show')
    if cmd in ('show', 'dashboard'):
        return dashboard()
    if cmd == 'run':
        return run()
    if cmd == 'test-alert':
        name, _ = load_conf()
        sent = send(f'{name}: test alert', 'This is a test from kh-watch. If you can read this, alerts work.')
        print('Sent to: ' + (', '.join(sent) or 'nothing (no channels configured or all failed)'))
        return 0 if sent else 1
    if cmd == 'init-public':
        return init_public()
    if cmd == 'init':
        return init(argv[2] if len(argv) > 2 else '', argv[3] if len(argv) > 3 else '')
    print(__doc__); return 2


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv) or 0)
    except BrokenPipeError:   # dashboard | head
        os._exit(0)
