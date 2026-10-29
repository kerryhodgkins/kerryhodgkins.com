#!/usr/bin/env python3
# Tests for watch.py with fake services and a fake ntfy/Resend. Run: python3 _watch/test_watch.py
import json, os, socket, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
W = os.path.join(HERE, 'watch.py')
sent = []


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        body = b'<html>NodeDial "boards"</html>' if self.path == '/ok' else b'nope'
        code = 200 if self.path in ('/ok', '/plain') else 500
        if self.path.startswith('/www/'):
            try:
                body = open(os.path.join(state, self.path[1:]), 'rb').read(); code = 200
            except OSError:
                body, code = b'', 404
        self.send_response(code); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0)); data = self.rfile.read(n).decode()
        sent.append({'path': self.path, 'title': self.headers.get('Title'), 'prio': self.headers.get('Priority'),
                     'auth': self.headers.get('Authorization'), 'body': data})
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"id":"x"}')


srv = ThreadingHTTPServer(('127.0.0.1', 0), Fake); threading.Thread(target=srv.serve_forever, daemon=True).start()
H = f'http://127.0.0.1:{srv.server_port}'
lst = socket.socket(); lst.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); lst.bind(('127.0.0.1', 0)); lst.listen()
UP = lst.getsockname()[1]
tmp = socket.socket(); tmp.bind(('127.0.0.1', 0)); FLAKY = tmp.getsockname()[1]; tmp.close()
state = tempfile.mkdtemp(); conf = os.path.join(state, 'watch.conf')
secrets = os.path.join(state, 'secrets.env')


def write_conf(extra=''):
    open(conf, 'w').write(f"""[watch]
name = Test Box <b>

[check Board telnet]
group = Boards
type = tcp
target = 127.0.0.1:{UP}

[check Poker port]
group = Boards
type = tcp
target = 127.0.0.1:{FLAKY}

[check Site]
group = Web
type = http
url = {H}/ok
contains = "boards"

[check Disk]
group = Server
type = disk
path = /
min_free = 0

[check Memory]
group = Server
type = memory
min_free = 0
{extra}""")


write_conf()
open(secrets, 'w').write(f'KW_NTFY_TOPIC=kh-testtopic\nKW_RESEND_KEY=re_secret\nKW_EMAIL_TO=me@example.com\nKW_PAGE_TOKEN=tok123\nKW_PAGE_URL=https://status.example/tok123/\n')
ENV = dict(os.environ, KW_CONF=conf, KW_STATE=state, KW_NTFY=H, KW_RESEND=H, KW_SECRETS=secrets, NO_PROXY='*', no_proxy='*',
           HTTP_PROXY='', HTTPS_PROXY='', http_proxy='', https_proxy='')
res = []


def ok(name, cond, extra=''):
    res.append(bool(cond)); print('PASS' if cond else 'FAIL', name, '' if cond else str(extra)[:400])


def kw(*args, env=None):
    return subprocess.run([sys.executable, W, *args], env=env or ENV, capture_output=True, text=True, timeout=60)


r = kw('show')
ok('dashboard lists every check', all(n in r.stdout for n in ('Board telnet', 'Poker port', 'Site', 'Disk', 'Memory')), r.stdout + r.stderr)
ok('a listening port is UP, a closed one is DOWN', 'UP    Board telnet' in r.stdout.replace('● ', '') and 'DOWN  Poker port' in r.stdout.replace('● ', ''), r.stdout)
ok('the summary counts what is down, and the exit code says so', '1 DOWN of 5' in r.stdout and r.returncode == 1, (r.stdout, r.returncode))
ok('it warns when the background watcher has never run', 'watcher has not run yet' in r.stdout, r.stdout)
ok('the dashboard is plain text when not on a terminal (no color codes in logs)', '\033[' not in r.stdout)

r = kw('run')
ok('first failure: no alert yet (one blip is not an outage)', r.returncode == 0 and not sent, (r.stderr, sent))
kw('run')
ok('second failure in a row: one alert to the phone and one email', len(sent) == 2 and {s['path'] for s in sent} == {'/kh-testtopic', '/emails'}, sent)
ph = next(s for s in sent if s['path'] == '/kh-testtopic'); em = next(s for s in sent if s['path'] == '/emails')
ok('the alert says which server, what, and why, at high priority', ph['title'] == 'Test Box <b>: 1 down' and 'DOWN: Poker port' in ph['body'] and ph['prio'] == 'high', ph)
ok('the email goes to Kerry, with the status page link, and the key only in the header', json.loads(em['body'])['to'] == ['me@example.com']
   and 'https://status.example/tok123/' in json.loads(em['body'])['text'] and em['auth'] == 'Bearer re_secret' and 're_secret' not in em['body'], em)
n = len(sent); kw('run')
ok('still down: no repeat alert every minute', len(sent) == n, sent[n:])

page = open(os.path.join(state, 'www', 'tok123', 'index.html')).read()
ok('the status page shows red, escapes names, and refreshes itself', '1 down' in page and 'Test Box &lt;b&gt;' in page and 'http-equiv="refresh"' in page
   and 'class="down"' in page and '<script' not in page, page[:300])
ok('the status page tells search engines to stay away', 'noindex' in page)
sj = json.load(open(os.path.join(state, 'www', 'tok123', 'status.json')))
ok('status.json carries names and green/red only (no details)', set(sj['checks'][0]) == {'name', 'ok'} and sj['name'] == 'Test Box <b>', sj)

r = kw('show')
ok('the dashboard shows how long something has been down', '(down ' in r.stdout and 'watcher last ran' in r.stdout, r.stdout)

fix = socket.socket(); fix.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); fix.bind(('127.0.0.1', FLAKY)); fix.listen()
n = len(sent); kw('run')
back = [s for s in sent[n:] if s['path'] == '/kh-testtopic']
ok('when it comes back: an all-clear, at normal priority', back and back[0]['title'] == 'Test Box <b>: all clear' and 'Back up: Poker port' in back[0]['body']
   and back[0]['prio'] == 'default', sent[n:])
r = kw('show')
ok('all green after recovery', 'All 5 green' in r.stdout and r.returncode == 0, r.stdout)

# reminders for long outages
fix.close()
env = dict(ENV, KW_REMIND_HOURS='0.0003')   # about one second
kw('run', env=env); kw('run', env=env); n = len(sent); time.sleep(1.3); kw('run', env=env)
ok('a long outage gets a reminder (every 6 hours by default)', any('Still down: Poker port' in s['body'] for s in sent[n:] if s['path'] == '/kh-testtopic'), sent[n:])

# heartbeat: one watcher checks the other is still reporting
write_conf(f"""
[check Peer watcher]
group = Peer
type = heartbeat
url = {H}/www/tok123/status.json
max_age = 300
""")
r = kw('show')
ok('a fresh heartbeat is green and mentions what is red over there', 'Peer watcher' in r.stdout and 'reporting; 1 red there: Poker port' in r.stdout, r.stdout)
p = os.path.join(state, 'www', 'tok123', 'status.json'); d = json.load(open(p)); d['time'] -= 900; json.dump(d, open(p, 'w'))
r = kw('show')
ok('a stale heartbeat (watcher stopped) is red', 'last report 15 min ago' in r.stdout, r.stdout)

# bad config and test-alert
write_conf('\n[check Oops]\ngroup = X\ntype = nonsense\n')
r = kw('show')
ok('a typo in the config shows as red instead of crashing', 'unknown check type' in r.stdout, r.stdout + r.stderr)
n = len(sent); r = kw('test-alert')
ok('test-alert reaches both channels (it reads the secrets file itself)', 'phone, email' in r.stdout and len(sent) - n == 2, r.stdout + r.stderr)
r = kw('test-alert', env=dict(ENV, KW_SECRETS='/nonexistent'))
ok('with no channels configured, test-alert says so and fails', r.returncode == 1 and 'nothing' in r.stdout, r.stdout)

# init: starter config parses and leaves out what isn't installed
import configparser
for kind in ('revenant', 'haunted'):
    r = kw('init', kind, 'https://status.example/tok/status.json')
    cp = configparser.ConfigParser(interpolation=None); cp.read_string(r.stdout)
    checks = [s for s in cp.sections() if s.startswith('check ')]
    ok(f'init {kind}: a valid config with network checks kept and missing units left out', r.returncode == 0 and len(checks) >= 4
       and any(cp[s]['type'] == 'tcp' for s in checks)
       and not any(cp[s].get('unit') in ('mis.service', 'sbbs.service', 'sn-telnet.service') for s in checks)   # not installed in this sandbox
       and (('#   Mystic BBS' in r.stdout) if kind == 'revenant' else ('#   Synchronet BBS' in r.stdout)), r.stdout[-600:] + r.stderr)
ok('haunted init points its heartbeat at Revenant\'s status.json', 'url = https://status.example/tok/status.json' in kw('init', 'haunted', 'https://status.example/tok/status.json').stdout)
ok('nothing printed anywhere contains the Resend key', all('re_secret' not in (kw(c).stdout + kw(c).stderr) for c in ('show',)))

# journal check: a fake journalctl that "saw" a QNET call N minutes ago
fake = tempfile.mkdtemp()
open(os.path.join(fake, 'journalctl'), 'w').write('''#!/bin/sh
# args: -u UNIT --since SINCE --grep PAT ...; LAST_MIN = minutes since the last matching line
since="$4"; mins=${since#-}; mins=${mins% min}
now=$(date +%s); t=$((now - LAST_MIN*60))
case "$since" in -14\ days) echo "$t.0 host synchronet[1]: evnt QNET Imported 3 from VERT";; *) [ "$LAST_MIN" -le "$mins" ] && echo "$t.0 host synchronet[1]: evnt QNET Imported 3 from VERT";; esac
exit 0
''')
os.chmod(os.path.join(fake, 'journalctl'), 0o755)
write_conf("""
[check DOVE-Net call to VERT]
group = Haunted Node
type = journal
unit = sbbs
pattern = QNET.*VERT
max_age = 360
""")
r = kw('show', env=dict(ENV, PATH=fake + ':' + os.environ['PATH'], LAST_MIN='45'))
ok('DOVE-Net called 45 min ago: green, with when', 'UP    DOVE-Net call to VERT' in r.stdout.replace('● ', '') and 'last seen 45 min ago' in r.stdout, r.stdout + r.stderr)
r = kw('show', env=dict(ENV, PATH=fake + ':' + os.environ['PATH'], LAST_MIN='480'))
ok('no call for 8 hours (limit 6): red, saying when it last called', 'DOWN  DOVE-Net call to VERT' in r.stdout.replace('● ', '') and 'last seen 8 h ago (limit 6 h)' in r.stdout, r.stdout + r.stderr)

write_conf("\n[check Disk]\ngroup = Server\ntype = disk\nmin_free = 0\n")
r = kw('show')
ok('a check listed twice does not crash the dashboard', r.returncode in (0, 1) and 'Traceback' not in r.stderr and r.stdout.count('Disk') == 1, r.stdout + r.stderr)
open(conf, 'a').write('\nthis line is broken\n')
r = kw('show')
ok('a broken line gives a one-line message, not a traceback', 'Problem in' in r.stderr and 'Traceback' not in r.stderr, r.stderr)

# public status page: service names only, green/red, plus a badge
st2 = tempfile.mkdtemp()
write_conf("""
[public]
Revenant BBS = Board telnet, Site
Revenant Poker = Poker port
Ghost Service = Not A Check
""")
envp = dict(ENV, KW_STATE=st2, KW_SECRETS='/nonexistent', KW_PAGE_TOKEN='tok9')
fixed = socket.socket(); fixed.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
kw('run', env=envp)
pub = os.path.join(st2, 'www', 'public')
pg = open(os.path.join(pub, 'index.html')).read()
ok('public page: one failed check does not turn it red yet', 'All services are up' in pg and 'Revenant Poker' in pg, pg[-800:])
kw('run', env=envp)
pg = open(os.path.join(pub, 'index.html')).read(); bj = json.load(open(os.path.join(pub, 'status.json'))); svg = open(os.path.join(pub, 'badge.svg')).read()
ok('after two failures the service is red for visitors', '1 service is down' in pg and 'class="down"><span class="dot"></span><span class="n">Revenant Poker' in pg, pg[-900:])
ok('visitors see service names only: no ports, check names or server details', 'Poker port' not in pg and '127.0.0.1' not in pg and 'Board telnet' not in pg
   and 'Test Box' not in pg and 'Disk' not in pg, pg)
ok('a service with no matching checks is left off', 'Ghost Service' not in pg and len(bj['services']) == 2, bj)
ok('status.json: names and up/down only', bj['services'] == [{'name': 'Revenant BBS', 'up': True}, {'name': 'Revenant Poker', 'up': False}], bj)
ok('the badge says what is down, in an SVG with a text fallback', svg.startswith('<svg') and '1 service down' in svg and '<title>1 service down</title>' in svg and '#F85149' in svg, svg)
ok('the public page links home and has no scripts', 'https://kerryhodgkins.com/' in pg and '<script' not in pg)
ok('the private page is still written next to it', os.path.exists(os.path.join(st2, 'www', 'tok9', 'index.html')))
r = kw('init-public', env=envp)
ok('init-public keeps only checks this server has', '[public]' in r.stdout and 'Revenant Poker = Poker port' not in r.stdout and 'NodeDial' not in r.stdout, r.stdout)

srv.shutdown()
print(f'{sum(res)}/{len(res)} passed')
sys.exit(0 if all(res) else 1)
