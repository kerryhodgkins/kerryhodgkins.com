#!/usr/bin/env python3
# Tests for subscribe.py against a fake Resend API. Run: python3 _subscribe/test_subscribe.py
import json, os, re, socket, subprocess, sys, tempfile, threading, time, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
calls = []


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        calls.append((self.path, self.headers.get('Authorization'), body))
        fail = self.path.startswith('/audiences/') and body.get('email', '').startswith('fail')
        self.send_response(500 if fail else 200); self.send_header('Content-Type', 'application/json'); self.end_headers()
        self.wfile.write(b'{"id":"x"}')


def free():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return p


api = ThreadingHTTPServer(('127.0.0.1', free()), Fake); threading.Thread(target=api.serve_forever, daemon=True).start()
port = free(); state = tempfile.mkdtemp()
env = dict(os.environ, RESEND_API=f'http://127.0.0.1:{api.server_port}', RESEND_API_KEY='re_test', RESEND_AUDIENCE='aud123',
           KH_PORT=str(port), KH_STATE=state, KH_PUBLIC='https://subscribe.example', NO_PROXY='*', no_proxy='*')
proc = subprocess.Popen([sys.executable, os.path.join(HERE, 'subscribe.py')], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
base = f'http://127.0.0.1:{port}'
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for _ in range(50):
    try: opener.open(base + '/healthz', timeout=1); break
    except OSError: time.sleep(0.1)

res = []
def ok(name, cond, extra=''):
    res.append(bool(cond)); print('PASS' if cond else 'FAIL', name, '' if cond else str(extra)[:300])

def get(path):
    try:
        r = opener.open(base + path, timeout=5); return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e: return e.code, e.read().decode(), dict(e.headers)

def post(path, data, ip='10.0.0.1'):
    req = urllib.request.Request(base + path, data=urllib.parse.urlencode(data).encode(), headers={'X-Real-IP': ip})
    try:
        r = opener.open(req, timeout=10); return r.status, r.read().decode()
    except urllib.error.HTTPError as e: return e.code, e.read().decode()

def fresh_form():
    s, b, _ = get('/')
    return dict(ts=re.search(r'name="ts" value="(\d+)"', b).group(1), sig=re.search(r'name="sig" value="(\w+)"', b).group(1))

try:
    s, b, hd = get('/')
    ok('the sign-up page loads, with no scripts allowed', s == 200 and 'Get new projects by email' in b and "default-src 'none'" in hd.get('Content-Security-Policy', ''))
    ok('it says how often and what for', 'About once a month' in b and 'never shared' in b)
    f = fresh_form()
    post('/', dict(f, email='fast@example.com'))
    ok('a form sent faster than a person can type sends nothing', not calls, calls)
    time.sleep(2.1)
    post('/', dict(f, email='bot@example.com', website='http://spam'))
    ok('the spam trap sends nothing (and says the same thing)', not calls, calls)
    s, b = post('/', dict(f, email='not an email'))
    ok('a bad address is explained', s == 400 and 'look like an email address' in b)
    s, b = post('/', dict(f, email='Reader@Example.com'))
    ok('a good address gets the check-your-email page', s == 200 and 'Check your email' in b)
    ok('one confirm email goes out, from Kerry, with the key sent only to Resend', len(calls) == 1 and calls[0][0] == '/emails' and calls[0][1] == 'Bearer re_test'
       and calls[0][2]['to'] == ['reader@example.com'] and 'kerryhodgkins.com' in calls[0][2]['from'], calls)
    link = re.search(r'https://subscribe\.example/confirm\?t=([A-Za-z0-9_-]{43})', calls[0][2]['text'])
    ok('...with a one-time confirm link', link, calls[0][2]['text'])
    ok('nobody is added before confirming', not any(c[0].startswith('/audiences') for c in calls))
    pend = open(os.path.join(state, 'pending.json')).read()
    ok('the pending file stores a hash of the link, not the link', link.group(1) not in pend)
    s, b, _ = get('/confirm?t=' + link.group(1))
    ok('opening the link alone adds nothing (a mail scanner can\'t confirm)', s == 200 and 'Confirm' in b and not any(c[0].startswith('/audiences') for c in calls))
    s, b = post('/confirm', {'t': link.group(1)})
    ok('confirming adds the address to the audience', s == 200 and 're in</h1>' in b and calls[-1][0] == '/audiences/aud123/contacts'
       and calls[-1][2] == {'email': 'reader@example.com', 'unsubscribed': False}, calls[-1])
    s, b = post('/confirm', {'t': link.group(1)})
    ok('a used link is refused', s == 400 and 'expired' in b)
    ok('confirmed addresses are not kept here', 'reader@example.com' not in open(os.path.join(state, 'pending.json')).read())
    n = len(calls)
    for i in range(4): post('/', dict(f, email=f'r{i}@example.com'), ip='10.0.0.9')
    s, b = post('/', dict(f, email='r9@example.com'), ip='10.0.0.9')
    s, b = post('/', dict(f, email='r10@example.com'), ip='10.0.0.9')
    ok('one visitor can only ask 5 times an hour', s == 429 and len(calls) - n == 5, (s, len(calls) - n))
    time.sleep(0.1); post('/', dict(f, email='fail@example.com'), ip='10.0.0.20')
    l2 = re.search(r'confirm\?t=([A-Za-z0-9_-]{43})', calls[-1][2]['text']).group(1)
    s, b = post('/confirm', {'t': l2})
    ok('if Resend fails, the person is told to try again', s == 502 and 'try again' in b)
    req = urllib.request.Request(base + '/', data=b'email=a%40b.co', headers={'Origin': 'https://evil.example'})
    try: opener.open(req, timeout=5); code = 200
    except urllib.error.HTTPError as e: code = e.code
    ok('a post from another site is refused', code == 403)
    out = proc.stdout.read1(65536).decode() if hasattr(proc.stdout, 'read1') else ''
    ok('nothing logs an email address', '@' not in out, out)
finally:
    proc.terminate(); api.shutdown()
print(f'{sum(res)}/{len(res)} passed')
sys.exit(0 if all(res) else 1)
