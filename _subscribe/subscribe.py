#!/usr/bin/env python3
# Email sign-up for kerryhodgkins.com: subscribe.kerryhodgkins.com.
# Someone enters an email, gets a one-time confirm link, and only after they click it is the address added to a Resend
# segment. Kerry sends the monthly update from Resend (Broadcasts, to the segment), which adds the unsubscribe link and handles opt-outs.
#
# Privacy: unconfirmed addresses are kept (as a hash of the token, with the email) for 48 hours, then dropped. Confirmed
# addresses live only in Resend (as contacts in the segment). No cookies, no analytics, no scripts on the pages. Logs never hold emails.
#
# Runs on 127.0.0.1 behind nginx. Settings come from the environment (systemd EnvironmentFile=/etc/kh-subscribe.env):
#   RESEND_API_KEY   a Resend key that can send email and manage contacts (never commit it, never paste it in chat)
#   RESEND_SEGMENT   the segment id ("kerryhodgkins.com") new subscribers go into (Resend replaced audiences with segments)
#   KH_FROM          sender, e.g. "Kerry Hodgkins <hello@kerryhodgkins.com>" (its domain must be verified in Resend)
#   KH_PUBLIC        the public address of this service, default https://subscribe.kerryhodgkins.com
#   KH_PORT          default 8097;  KH_STATE  default /var/lib/kh-subscribe;  RESEND_API  default https://api.resend.com
import hashlib, hmac, html, json, os, re, secrets, threading, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

API = os.environ.get('RESEND_API', 'https://api.resend.com').rstrip('/')
KEY = os.environ.get('RESEND_API_KEY', '')
SEGMENT = os.environ.get('RESEND_SEGMENT') or os.environ.get('RESEND_AUDIENCE', '')
FROM = os.environ.get('KH_FROM', 'Kerry Hodgkins <hello@kerryhodgkins.com>')
PUBLIC = os.environ.get('KH_PUBLIC', 'https://subscribe.kerryhodgkins.com').rstrip('/')
PORT = int(os.environ.get('KH_PORT', '8097'))
STATE = os.environ.get('KH_STATE', '/var/lib/kh-subscribe')
SITE = 'https://kerryhodgkins.com/'
PENDING_SECS = 48 * 3600
MAIL_PER_HOUR = 60            # confirm emails across everyone, so a flood can't burn the sending quota
EMAIL = re.compile(r'^[^@\s"<>]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}$')
TOKEN = re.compile(r'^[A-Za-z0-9_-]{43}$')
LOCK = threading.Lock()
HITS = {}


def now(): return time.time()
def h(s): return html.escape(str(s), quote=True)
def sha(s): return hashlib.sha256(s.encode()).hexdigest()


def limited(key, n, secs):
    with LOCK:
        t = now(); xs = [x for x in HITS.get(key, []) if t - x < secs]
        if len(xs) >= n: HITS[key] = xs; return True
        xs.append(t); HITS[key] = xs; return False


def load(name, default):
    try:
        with open(os.path.join(STATE, name)) as f: return json.load(f)
    except (OSError, ValueError): return default


def save(name, data):
    p = os.path.join(STATE, name); t = p + '.tmp'
    with open(t, 'w') as f: json.dump(data, f)
    os.replace(t, p)


def secret():
    p = os.path.join(STATE, 'secret')
    with LOCK:
        try: return open(p).read().strip()
        except OSError:
            s = secrets.token_hex(32)
            with open(p, 'w') as f: f.write(s)
            return s


def pending_put(email):
    tok = secrets.token_urlsafe(32)
    with LOCK:
        st = {k: v for k, v in load('pending.json', {}).items() if v['exp'] > now()}
        st = {k: v for k, v in st.items() if v['email'] != email}   # one live link per address
        st[sha(tok)] = {'email': email, 'exp': now() + PENDING_SECS}
        save('pending.json', st)
    return tok


def pending_take(tok):
    if not TOKEN.match(tok or ''): return None
    with LOCK:
        st = {k: v for k, v in load('pending.json', {}).items() if v['exp'] > now()}
        v = st.pop(sha(tok), None); save('pending.json', st)
    return v and v['email']


def mail_budget():
    with LOCK:
        b = load('budget.json', {}); hour = time.strftime('%Y%m%d%H')
        n = b.get(hour, 0)
        if n >= MAIL_PER_HOUR: return False
        save('budget.json', {hour: n + 1}); return True


def resend(method, path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(), method=method,
                                 headers={'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json',
                                          'User-Agent': 'kh-subscribe/1'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r: return r.status, json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read() or b'{}')
        except ValueError: return e.code, {}
    except (urllib.error.URLError, OSError, ValueError): return 0, {}


def send_confirm(email, tok):
    link = f'{PUBLIC}/confirm?t={tok}'
    text = ("Someone (hopefully you) asked to get updates from kerryhodgkins.com by email.\n\n"
            f"To confirm, open:\n\n  {link}\n\n"
            "That's roughly one email a month: what I shipped, what I’m building, and anything worth sharing. Your address is never shared or sold.\n"
            "The link works once and expires in 48 hours. If this wasn't you, ignore this email and nothing happens.\n\n"
            "Kerry Hodgkins\nhttps://kerryhodgkins.com")
    body = text.replace('&', '&amp;').replace('<', '&lt;').replace('\n', '<br>').replace(link, f'<a href="{h(link)}">{h(link)}</a>')
    code, _ = resend('POST', '/emails', {'from': FROM, 'to': [email], 'subject': 'Confirm your email for kerryhodgkins.com',
                                         'text': text, 'html': f'<div style="font-family:system-ui,sans-serif;font-size:15px;line-height:1.5">{body}</div>'})
    return 200 <= code < 300


def add_contact(email):
    """Contacts are global in Resend; the segment is the list Kerry broadcasts to. Someone already a contact (say,
    signing up again after unsubscribing) counts as done: Resend's own unsubscribe state stays theirs to change."""
    code, body = resend('POST', '/contacts', {'email': email, 'unsubscribed': False, 'segments': [{'id': SEGMENT}]})
    return 200 <= code < 300 or (code in (409, 422) and 'exist' in json.dumps(body).lower())


def page(title, body, status=''):
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{h(title)} | Kerry Hodgkins</title><meta name="robots" content="noindex">
<style>
:root{{--bg:#F5F7F9;--card:#fff;--ink:#0B1E33;--text:#16202B;--muted:#4B5C6B;--rule:#E2E7EC;--accent:#2F6694;--ok:#1F7A4D;--bad:#B3261E}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0E1722;--card:#13202E;--ink:#E9EEF3;--text:#D5DDE5;--muted:#9AAAB8;--rule:#22313F;--accent:#7FB0DC;--ok:#5CC08C;--bad:#F28B82}}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:400 1rem/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:34rem;margin:8vh auto;padding:0 1.25rem}} .card{{background:var(--card);border:1px solid var(--rule);border-radius:8px;padding:2rem}}
a{{color:var(--accent)}} h1{{color:var(--ink);font-size:1.6rem;line-height:1.2;margin:0 0 .75rem}} p{{margin:.5rem 0}} .muted{{color:var(--muted);font-size:.9rem}}
label{{display:block;font-weight:600;margin:1.25rem 0 .35rem;color:var(--ink)}}
input[type=email]{{width:100%;font:inherit;padding:.65rem .75rem;border:1px solid var(--rule);border-radius:4px;background:var(--bg);color:var(--text)}}
input:focus-visible,button:focus-visible,a:focus-visible{{outline:3px solid var(--accent);outline-offset:2px}}
button{{margin-top:1rem;font:600 1rem system-ui,sans-serif;padding:.7rem 1.15rem;border:0;border-radius:4px;background:var(--ink);color:var(--bg);cursor:pointer}}
.status{{border-left:3px solid var(--ok);padding:.25rem .75rem;margin:1rem 0}} .status.bad{{border-color:var(--bad)}}
.hp{{position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden}}
</style></head><body><main><div class="card">
<h1>{h(title)}</h1>{status}{body}
</div><p class="muted" style="text-align:center;margin-top:1rem"><a href="{SITE}">kerryhodgkins.com</a></p></main></body></html>"""


def form(err='', value=''):
    ts = str(int(now())); sig = hmac.new(secret().encode(), ts.encode(), 'sha256').hexdigest()[:32]
    st = f'<p class="status bad" role="alert">{h(err)}</p>' if err else ''
    return page('Get new projects by email', f"""
<p>Roughly once a month: what I shipped, what I’m building, and anything worth sharing. Never shared or sold.</p>
<form method="post" action="/">
<label for="e">Your email</label>
<input id="e" type="email" name="email" required autocomplete="email" maxlength="254" value="{h(value)}">
<div class="hp" aria-hidden="true"><label>Leave this empty <input type="text" name="website" tabindex="-1" autocomplete="off"></label></div>
<input type="hidden" name="ts" value="{ts}"><input type="hidden" name="sig" value="{sig}">
<button>Email me a confirm link</button>
</form>
<p class="muted">You'll get one email with a link. Nothing is sent after that unless you click it. Every update has an unsubscribe link. No tracking pixels.</p>
<p class="muted">Rather use a feed reader? <a href="{SITE}feed.xml">RSS</a>.</p>""", st)


class H(BaseHTTPRequestHandler):
    server_version = 'kh-subscribe'; sys_version = ''

    def log_message(self, *a): pass   # nginx keeps the access log; nothing here ever logs an address

    def ip(self): return self.headers.get('X-Real-IP') or self.client_address[0]

    def send(self, code, body):
        b = body.encode()
        self.send_response(code)
        for k, v in (('Content-Type', 'text/html; charset=utf-8'), ('Content-Length', str(len(b))), ('Cache-Control', 'no-store'),
                     ('Referrer-Policy', 'no-referrer'), ('X-Content-Type-Options', 'nosniff'), ('X-Frame-Options', 'DENY'),
                     ('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")):
            self.send_header(k, v)
        self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        u = urlsplit(self.path); q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        if u.path == '/': return self.send(200, form())
        if u.path == '/confirm':   # a button, so mail scanners that open links can't confirm on someone's behalf
            return self.send(200, page('One more click', f'<p>Press the button to confirm your email.</p><form method="post" action="/confirm"><input type="hidden" name="t" value="{h(q.get("t", ""))}"><button>Confirm</button></form>'))
        if u.path == '/healthz': return self.send(200, 'ok')
        self.send(404, page('Not found', f'<p>Nothing here. <a href="/">Sign up</a> or go back to <a href="{SITE}">kerryhodgkins.com</a>.</p>'))

    def form_data(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n > 4096: return None
        return {k: v[-1] for k, v in parse_qs(self.rfile.read(n).decode('utf-8', 'replace'), keep_blank_values=True).items()}

    def do_POST(self):
        p = urlsplit(self.path).path; f = self.form_data()
        if f is None: return self.send(413, page('Too big', '<p>That was too much to send.</p>'))
        o = self.headers.get('Origin')
        if o and o != 'null' and urlsplit(o).netloc != self.headers.get('Host', ''): return self.send(403, page('Not allowed', '<p>That came from another site.</p>'))
        if p == '/':
            done = page('Check your email', '<p>If that address is valid, a confirm link is on its way. Click it within 48 hours to finish signing up.</p><p class="muted">Nothing arrived? Check spam, or try again in a few minutes.</p>')
            ts = f.get('ts', ''); ok_sig = ts.isdigit() and hmac.compare_digest(f.get('sig', ''), hmac.new(secret().encode(), ts.encode(), 'sha256').hexdigest()[:32])
            if f.get('website') or not ok_sig or not (2 <= now() - int(ts) <= 6 * 3600):
                return self.send(200, done)   # a bot: same answer, nothing sent
            e = f.get('email', '').strip().lower()
            if not EMAIL.match(e): return self.send(400, form("That doesn't look like an email address.", f.get('email', '')))
            if limited('ip:' + self.ip(), 5, 3600) or limited('em:' + sha(e), 3, 3600):
                return self.send(429, form('Too many tries. Wait an hour and try again.'))
            if mail_budget(): send_confirm(e, pending_put(e))
            return self.send(200, done)
        if p == '/confirm':
            if limited('cf:' + self.ip(), 20, 3600): return self.send(429, page('Slow down', '<p>Too many tries. Wait a while and try again.</p>'))
            e = pending_take(f.get('t', ''))
            if not e: return self.send(400, page('Link expired', '<p>That link has expired or was already used. <a href="/">Sign up again</a>.</p>'))
            if not add_contact(e): return self.send(502, page("Couldn't finish", '<p>Something went wrong on our side. Please <a href="/">try again</a> in a few minutes.</p>'))
            return self.send(200, page("You're in", f'<p>Thanks. You\'ll get about one email a month: what I shipped and what\'s next.</p><p class="muted">Every email has an unsubscribe link. Back to <a href="{SITE}">kerryhodgkins.com</a>.</p>'))
        self.send(404, page('Not found', '<p>Nothing here.</p>'))


if __name__ == '__main__':
    if not (KEY and SEGMENT): raise SystemExit('kh-subscribe: set RESEND_API_KEY and RESEND_SEGMENT (see /etc/kh-subscribe.env)')
    os.makedirs(STATE, exist_ok=True); os.umask(0o077)
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), H); srv.daemon_threads = True
    print(f'kh-subscribe on 127.0.0.1:{PORT}', flush=True)
    srv.serve_forever()
