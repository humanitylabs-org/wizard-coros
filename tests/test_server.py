"""Tests for the Coros connector against a fake COROS MCP server built from COROS's official sources: "Build on COROS
MCP" (support.coros.com), the Tool Guide and the official login helper in github.com/coroslab/COROS-MCP
(skill/coros_mcp_login_gateway/scripts/coros_mcp_login.py): OpenID discovery of the regional issuer, dynamic client
registration (public client), the CLI login session (create -> claim pending/authorized -> login ticket), the
authorization request finished with the ticket (302 to the redirect with code + state), PKCE code exchange, refresh
tokens, the stateless MCP endpoint (JSON or SSE replies) and tool answers in COROS's formatted text.

Text formats: querySleepOverview, querySportRecords and getActivityDetail use the Tool Guide's response examples
verbatim. The guide gives no examples for querySleepHrv, queryRestingHeartRate, queryDailyHealthData,
queryTrainingLoadAssessment or queryRecoveryStatus; their fakes follow the same "date heading + Label: value" style
and are ASSUMED, so the readers are tested against several plausible variants.
Run: python3 -m unittest discover -s tests"""
import base64
import datetime as dt
import hashlib
import importlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path(__file__).resolve().parent.parent
TZ = 'Asia/Bangkok'
TODAY = dt.datetime.now(dt.timezone(dt.timedelta(hours=7))).date()

# ---- the Tool Guide's response examples, verbatim ----
DOC_SLEEP = """Sleep Overview

2026-10-15
Sleep Score: 91
Daily Sleep: 7h 57min (incl. naps)
Main Sleep (asleep): 7h 57min
Deep Sleep Ratio: 27%
Light Sleep Ratio: 41%
REM Ratio: 31%
Awake Time: 5 min
Main Sleep Window: 2026-10-15 00:28 - 2026-10-15 08:30
Naps Total: 0 min
"""
DOC_RECORDS = """Sport Records — 2026-09-19 to 2026-10-18 (1 records)

1. Sport(9807) — 2026-10-03
   Duration: 2:12:38 | Distance: 12.03 km
   Average Pace: 11:01 /km
"""
DOC_DETAIL = """Sport(9807) Activity Details

Workout Time: 2:12:38
Distance: 12.03 km
Average Pace: 11:01 /km
Average Heart Rate: 76 bpm
Calories: 372 kcal
"""


def day(i):
    return (TODAY - dt.timedelta(days=i)).isoformat()


def ymd(s):
    return s.replace('-', '')


class Fake:
    def __init__(self):
        self.reset()

    def reset(self):
        self.clients, self.sessions, self.codes = {}, {}, {}
        self.access, self.refresh = set(), set()
        self.calls, self.tool_calls = [], []
        self.claims_until_authorized = 2
        self.sse = False
        self.revoked = []
        self.force_401 = 0
        self.sleep_score = {}

    def sleep_text(self, a, b):
        out = ['Sleep Overview', '']
        d = dt.date.fromisoformat(a)
        while d <= dt.date.fromisoformat(b):
            i = (TODAY - d).days
            if 0 <= i < 20:
                s, prev = d.isoformat(), (d - dt.timedelta(days=1)).isoformat()
                out += [s, f"Sleep Score: {self.sleep_score.get(s, 70 + i)}", 'Daily Sleep: 8h 20min (incl. naps)', 'Main Sleep (asleep): 7h 30min',
                        'Deep Sleep Ratio: 20%', 'Light Sleep Ratio: 60%', 'REM Ratio: 20%', 'Awake Time: 15 min',
                        f'Main Sleep Window: {prev} 23:30 - {s} 07:15', 'Naps Total: 50 min', '']
            d += dt.timedelta(days=1)
        return '\n'.join(out)


FAKE = Fake()


class FakeCoros(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body=None, headers=None, raw=None, kind='application/json'):
        b = raw if raw is not None else (b'' if body is None else json.dumps(body).encode())
        self.send_response(code)
        self.send_header('Content-Type', kind)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    @property
    def base(self):
        return f'http://127.0.0.1:{self.server.server_address[1]}'

    def do_GET(self):
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        FAKE.calls.append(('GET', u.path))
        if u.path == '/.well-known/openid-configuration':
            return self.reply(200, {'issuer': self.base, 'token_endpoint': self.base + '/oauth2/token', 'registration_endpoint': self.base + '/connect/register'})
        if u.path == '/oauth2/authorize':
            sess = next((s for s in FAKE.sessions.values() if s.get('ticket') and s['ticket'] == q.get('login_ticket')), None)
            if not sess or q.get('client_id') not in FAKE.clients or q.get('code_challenge_method') != 'S256' or q.get('resource') != self.base + '/mcp':
                return self.reply(400, {'error': 'invalid_request'})
            if 'mcp.tools' not in q.get('scope', '') or 'offline_access' not in q.get('scope', ''):
                return self.reply(400, {'error': 'invalid_scope'})
            sess['ticket'] = None                                   # single use
            code = 'code-' + os.urandom(6).hex()
            FAKE.codes[code] = {'client_id': q['client_id'], 'challenge': q['code_challenge'], 'redirect_uri': q['redirect_uri']}
            return self.reply(302, headers={'Location': q['redirect_uri'] + '?' + urlencode({'code': code, 'state': q['state']})})
        self.reply(404)

    def do_POST(self):
        u = urlsplit(self.path)
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n).decode()
        FAKE.calls.append(('POST', u.path))
        if u.path == '/connect/register':
            d = json.loads(raw)
            if d.get('token_endpoint_auth_method') != 'none' or not d.get('redirect_uris'):
                return self.reply(400, {'error': 'invalid_client_metadata'})
            cid = f'{os.urandom(4).hex()}-cid'
            FAKE.clients[cid] = d
            return self.reply(201, dict(d, client_id=cid, client_id_issued_at=int(time.time())))
        if u.path == '/api/v1/cli/login-sessions':
            d = json.loads(raw or '{}')
            if d.get('clientId') not in FAKE.clients:
                return self.reply(401, {'error': 'invalid_client'})
            sid = 'sess' + os.urandom(6).hex()
            FAKE.sessions[sid] = {'poll': 'pt' + os.urandom(6).hex(), 'claims': 0, 'ticket': None}
            return self.reply(200, {'sessionId': sid, 'pollToken': FAKE.sessions[sid]['poll'], 'loginUrl': f'{self.base}/cli/login/{sid}',
                                    'expiresAt': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat().replace('+00:00', 'Z'),
                                    'intervalSeconds': 0})
        if u.path.startswith('/api/v1/cli/login-sessions/') and u.path.endswith('/claim'):
            sid = u.path.split('/')[-2]
            s = FAKE.sessions.get(sid)
            if not s:
                return self.reply(200, {'status': 'expired'})
            if self.headers.get('X-Poll-Token') != s['poll']:
                return self.reply(403, {'error': 'forbidden'})
            s['claims'] += 1
            if s['claims'] <= FAKE.claims_until_authorized:
                return self.reply(200, {'status': 'pending'})
            if s.get('done'):
                return self.reply(200, {'status': 'claimed', 'errorCode': 'already_claimed'})
            s['done'] = True
            s['ticket'] = 'lt-' + os.urandom(6).hex()
            return self.reply(200, {'status': 'authorized', 'loginTicket': s['ticket']})
        if u.path == '/oauth2/token':
            f = {k: v[0] for k, v in parse_qs(raw).items()}
            if f.get('client_id') not in FAKE.clients:
                return self.reply(401, {'error': 'invalid_client'})       # what the real endpoint answers
            if f.get('grant_type') == 'authorization_code':
                c = FAKE.codes.pop(f.get('code'), None)
                ch = base64.urlsafe_b64encode(hashlib.sha256((f.get('code_verifier') or '').encode()).digest()).decode().rstrip('=')
                if not c or c['challenge'] != ch or c['redirect_uri'] != f.get('redirect_uri'):
                    return self.reply(400, {'error': 'invalid_grant'})
            elif f.get('grant_type') == 'refresh_token':
                if f.get('refresh_token') not in FAKE.refresh:
                    return self.reply(400, {'error': 'invalid_grant'})
                FAKE.refresh.discard(f['refresh_token'])            # rotating refresh tokens
            else:
                return self.reply(400, {'error': 'unsupported_grant_type'})
            a, r = 'at-' + os.urandom(8).hex(), 'rt-' + os.urandom(8).hex()
            FAKE.access.add(a); FAKE.refresh.add(r)
            return self.reply(200, {'access_token': a, 'refresh_token': r, 'token_type': 'Bearer', 'expires_in': 3600, 'scope': 'openid offline_access mcp.tools'})
        if u.path == '/oauth2/revoke':
            FAKE.revoked.append(parse_qs(raw).get('token', [''])[0])
            return self.reply(200, {})
        if u.path == '/mcp':
            tok = (self.headers.get('Authorization') or '')[7:]
            if tok not in FAKE.access:
                return self.reply(401, headers={'WWW-Authenticate': f'Bearer resource_metadata="{self.base}/.well-known/oauth-protected-resource/mcp"'})
            if FAKE.force_401:
                FAKE.force_401 -= 1
                FAKE.access.discard(tok)
                return self.reply(401)
            msg = json.loads(raw)
            if msg['method'] == 'initialize':
                return self.rpc(msg['id'], {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'coros-mcp'}})
            if msg['method'] != 'tools/call':
                return self.rpc(msg['id'], None, {'code': -32601, 'message': 'Method not found'})
            name, a = msg['params']['name'], msg['params'].get('arguments') or {}
            FAKE.tool_calls.append((name, a))
            text = self.tool_text(name, a)
            if text is None:
                return self.rpc(msg['id'], None, {'code': -32602, 'message': 'Unknown tool'})
            return self.rpc(msg['id'], {'content': [{'type': 'text', 'text': text}], 'isError': False})
        self.reply(404)

    def rpc(self, rid, result, error=None):
        body = {'jsonrpc': '2.0', 'id': rid, **({'error': error} if error else {'result': result})}
        if FAKE.sse:
            return self.reply(200, raw=f'event: message\ndata: {json.dumps(body)}\n\n'.encode(), kind='text/event-stream')
        return self.reply(200, body)

    def tool_text(self, name, a):
        def dates():
            s, e = a.get('startDate'), a.get('endDate')
            s, e = f'{s[:4]}-{s[4:6]}-{s[6:]}', f'{e[:4]}-{e[4:6]}-{e[6:]}'
            return s, e
        if name == 'querySleepOverview':
            return FAKE.sleep_text(*dates())
        if name == 'querySleepHrv':        # ASSUMED format
            s, e = dates()
            if (dt.date.fromisoformat(e) - dt.date.fromisoformat(s)).days > 6:
                return None
            out = ['Sleep HRV', '']
            d = dt.date.fromisoformat(s)
            while d <= dt.date.fromisoformat(e):
                i = (TODAY - d).days
                if 0 <= i < 20:
                    out += [d.isoformat(), f'Average HRV: {50 + i % 6} ms', 'Normal Range: 45-62 ms', 'Evaluation: Normal', '']
                d += dt.timedelta(days=1)
            return '\n'.join(out)
        if name == 'queryRestingHeartRate':     # ASSUMED format: one line per day
            return 'Resting Heart Rate\n' + '\n'.join(f'{day(i)}: {48 + i % 4} bpm' for i in range(min(a.get('days', 7), 20)))
        if name == 'queryDailyHealthData':      # ASSUMED format
            return '\n'.join(f'{day(i)}\nSteps: {8000 + i} | Calories: 2,{300 + i} kcal\nAverage Heart Rate: 64 bpm | Average Stress: 31\n'
                             for i in range(min(a.get('days', 7), 20)))
        if name == 'queryTrainingLoadAssessment':   # ASSUMED format
            return 'Training Load\n\n' + '\n'.join(f'{day(i)}\nShort-term Load: {60 + i}\nLong-term Load: 55\nLoad Ratio: 1.1\n' for i in range(min(a.get('days', 7), 20)))
        if name == 'querySportRecords':
            s, e = dates()
            rows = [i for i in range(0, 20, 4) if s <= day(i) <= e]
            body = '\n'.join(f'{k + 1}. Sport(100) — {day(i)}\n   LabelId: L{i:03d} | SportType: 100\n   Duration: 0:45:00 | Distance: 8.20 km\n'
                             for k, i in enumerate(rows))
            return f'Sport Records — {s} to {e} ({len(rows)} records)\n\n' + body
        if name == 'getActivityDetail':
            if not a.get('labelId') or a.get('sportType') is None:
                return None
            return DOC_DETAIL + f"Training Load: {40 + int(a['labelId'][1:])}\nAerobic Training Effect: 3.2\n"
        if name == 'queryRecoveryStatus':       # ASSUMED format
            return 'Recovery Status\n\nRecovery: 82%\nRecovery Level: Good\nTime to Full Recovery: 6 hours\n'
        return None


def http(port, method, path, body=None, headers=None):
    h = {'Host': f'127.0.0.1:{port}'}
    if body is not None:
        h['Content-Type'] = 'application/json'
    h.update(headers or {})
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class CorosTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = ThreadingHTTPServer(('127.0.0.1', 0), FakeCoros)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        fp = cls.fake.server_address[1]
        cls.tmp = tempfile.mkdtemp()
        os.environ.update({'DATA_DIR': cls.tmp + '/private', 'COROS_GATEWAY': f'http://127.0.0.1:{fp}', 'COROS_REQUEST_GAP': '0',
                           'COROS_LOGIN_POLL': '0.05', 'COROS_BACKOFF': '0.01', 'COROS_HISTORY_DAYS': '30', 'PORT': '18996',
                           'WIZARD_TRUSTED_HOSTS': 'box.example.ts.net'})
        sys.path.insert(0, str(ROOT / 'app'))
        import server
        cls.S = importlib.reload(server)
        cls.S.REGIONS = {k: (f'http://127.0.0.1:{fp}' if k != 'auto' else None) for k in cls.S.REGIONS}
        cls.app = ThreadingHTTPServer(('127.0.0.1', 0), cls.S.Handler)
        threading.Thread(target=cls.app.serve_forever, daemon=True).start()
        cls.port = cls.app.server_address[1]
        cls.S.TRUSTED_HOSTS.add(f'127.0.0.1:{cls.port}')

    @classmethod
    def tearDownClass(cls):
        cls.fake.shutdown()
        cls.app.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def files_text(self):
        return ''.join(p.read_bytes().decode('latin-1') for p in Path(self.tmp).rglob('*') if p.is_file())

    def wait(self, cond, timeout=30):
        t = time.time()
        while time.time() - t < timeout:
            if cond():
                return
            time.sleep(0.05)
        self.fail('timed out')

    def wait_sync(self):
        self.wait(lambda: not self.S.SYNC_LOCK.locked() and self.S._state().get('last_sync') and not self.S._state().get('syncing'))

    def test_flow(self):
        S = self.S
        self.assertEqual(S.status_info()['status'], 'not_configured')
        self.assertEqual(S.status_info()['scales']['readiness']['available'], False)
        # Check before connecting: real-style token endpoint answers invalid_client for an unknown client
        code, body = http(self.port, 'POST', '/api/check', {})
        self.assertEqual(json.loads(body)['result'], 'valid', body)
        self.assertIn('answering', json.loads(body)['message'])
        self.assertFalse(FAKE.clients, 'Check must not register anything')
        with self.assertRaises(S.Problem):
            S.request_sync()

        # ---- Connect: discovery, registration, login session; the app finishes by itself after the owner signs in
        code, body = http(self.port, 'POST', '/api/login/start', {'region': 'auto', 'timezone': TZ})
        self.assertEqual(code, 200, body)
        r = json.loads(body)
        self.assertIn('/cli/login/', r['login_url'])
        self.assertEqual(len(FAKE.clients), 1)
        reg = next(iter(FAKE.clients.values()))
        self.assertEqual((reg['token_endpoint_auth_method'], reg['scope'], reg['grant_types']),
                         ('none', 'openid offline_access mcp.tools', ['authorization_code', 'refresh_token']))
        self.assertNotIn('poll', body.decode().lower().replace('pollt', ''))   # the poll token never leaves the server
        st = S.status_info()
        self.assertEqual((st['status'], st['setup']['pending']), ('not_configured', True))
        self.wait(lambda: S.status_info()['status'] in ('syncing', 'connected') and S._state().get('last_sync'))
        self.wait_sync()
        st = S.status_info()
        self.assertEqual(st['status'], 'connected', st)
        self.assertEqual(st['provides'], {'readiness': False})
        self.assertEqual(st['source'], {'id': 'wizard-coros', 'provider': 'coros', 'name': 'Coros', 'app_path': '/wizard-coros/'})
        self.assertEqual(st['scales']['load']['scale_max'], 300)
        self.assertEqual(st['recovery_now']['recovery_pct'], 82)
        self.assertEqual(oct(os.stat(S.DATA / 'tokens.json').st_mode & 0o777), '0o600')
        self.assertEqual(oct(os.stat(S.DATA).st_mode & 0o777), '0o700')
        names = [n for n, _ in FAKE.tool_calls]
        for t in ('querySleepOverview', 'querySleepHrv', 'queryRestingHeartRate', 'queryDailyHealthData', 'queryTrainingLoadAssessment',
                  'querySportRecords', 'getActivityDetail', 'queryRecoveryStatus'):
            self.assertIn(t, names)
        hrv = [a for n, a in FAKE.tool_calls if n == 'querySleepHrv']
        self.assertTrue(all((dt.date.fromisoformat(f"{x['endDate'][:4]}-{x['endDate'][4:6]}-{x['endDate'][6:]}") -
                             dt.date.fromisoformat(f"{x['startDate'][:4]}-{x['startDate'][4:6]}-{x['startDate'][6:]}")).days <= 6 for x in hrv))
        self.assertEqual(st['counts'], {'days': 20, 'sleeps': 20, 'workouts': 5}, st)

        # ---- the contract
        d0 = day(0)
        d = S.contract_days({'from': d0, 'to': d0})['days'][0]
        check_day(self, d)
        self.assertIsNone(d['readiness'])
        self.assertEqual((d['hrv_ms'], d['resting_hr_bpm'], d['steps'], d['calories_kcal'], d['avg_hr_bpm']), (50, 48, 8000, 2300, 64))
        s = d['sleep']
        self.assertEqual((s['score'], s['asleep_h'], s['deep_h'], s['light_h'], s['rem_h'], s['awake_h'], s['in_bed_h']), (70, 7.5, 1.5, 4.5, 1.5, 0.25, 7.75))
        self.assertEqual(s['end'], d0 + 'T00:15:00.000Z')              # 07:15 in Bangkok
        self.assertEqual(s['naps'][0]['asleep_h'], 0.83)
        self.assertEqual(d['load'], {'label': 'Training load', 'max': None, 'scale_max': 300, 'unit': 'TL', 'value': 40})
        w = d['workouts'][0]
        self.assertEqual((w['id'], w['minutes'], w['avg_hr_bpm'], w['calories_kcal'], w['distance_m']), ('L000', 133, 76, 372, 8200))
        self.assertEqual(d['extras']['coros']['recovery_now']['recovery_pct'], 82)
        self.assertEqual(d['extras']['coros']['short_term_load'], 60)
        d1 = S.contract_days({'from': day(1), 'to': day(1)})['days'][0]
        self.assertIsNone(d1['load'])              # no activity, no daily training load: never invented from ATL/CTL
        self.assertIsNone(d1['extras']['coros']['recovery_now'])   # the recovery snapshot belongs to today only
        with self.assertRaises(S.Problem):
            S.contract_days({'changed_since': 'g1.4'})

        # ---- changed_since: an unchanged re-sync emits nothing; a re-scored night does
        cursor = S.contract_days({})['cursor']
        details_before = len([1 for n, _ in FAKE.tool_calls if n == 'getActivityDetail'])
        S.sync()
        self.assertEqual(S.contract_days({'changed_since': cursor})['days'], [])
        self.assertEqual(len([1 for n, _ in FAKE.tool_calls if n == 'getActivityDetail']), details_before, 'details are read once per activity')
        FAKE.sleep_score[day(2)] = 44
        FAKE.sse = True                         # the endpoint may answer as an event stream
        S.sync()
        FAKE.sse = False
        ch = S.contract_days({'changed_since': cursor})
        self.assertEqual([x['date'] for x in ch['days']], [day(2)])
        self.assertEqual(ch['days'][0]['sleep']['score'], 44)

        # ---- access token expiry and a 401 renew through the (rotating) refresh token
        S._save_tokens(dict(S._tokens(), expires_at=0))
        S.sync()
        FAKE.force_401 = 1
        S.sync()
        self.assertEqual(S.status_info()['status'], 'connected')
        code, body = http(self.port, 'POST', '/api/check', {})
        self.assertIn('recognises', json.loads(body)['message'])

        # ---- HTTP + MCP guards
        code, body = http(self.port, 'POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        self.assertEqual([t['name'] for t in json.loads(body)['result']['tools']], ['health_data_status', 'health_data_sync', 'health_data_days'])
        secrets_now = [S._tokens()['access_token'], S._tokens()['refresh_token']]
        for name, args in [('health_data_status', {}), ('health_data_days', {'from': d0}), ('health_data_days', {'changed_since': ''})]:
            code, body = http(self.port, 'POST', '/mcp', {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}})
            self.assertFalse(json.loads(body)['result']['isError'])
            for sec in secrets_now:
                self.assertNotIn(sec, body.decode())
        self.assertEqual(http(self.port, 'POST', '/mcp', {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'}, {'X-Forwarded-For': '1.2.3.4'})[0], 403)
        self.assertEqual(http(self.port, 'GET', '/api/status', headers={'Host': 'evil.example:18996'})[0], 403)
        self.assertEqual(http(self.port, 'POST', '/api/login/start', {}, {'Host': 'box.example.ts.net:8444', 'Origin': 'https://evil.example'})[0], 403)
        for path in ('/api/status', '/api/setup', '/api/days', '/wizard-coros/api/status', '/healthz', '/', '/icon.svg'):
            code, body = http(self.port, 'GET', path)
            self.assertEqual(code, 200, path)
            for sec in secrets_now:
                self.assertNotIn(sec, body.decode('latin-1'), path)
        for hdr in ('X-Forwarded-For', 'Tailscale-User-Login', 'Forwarded', 'X-Real-IP', 'CF-Connecting-IP'):
            self.assertEqual(http(self.port, 'GET', '/api/days', headers={hdr: 'x'})[0], 403, hdr)

        # ---- a dead refresh token: needs_login, data kept
        while S.SYNC_LOCK.locked():
            time.sleep(0.05)
        FAKE.refresh.clear()
        S._save_tokens(dict(S._tokens(), expires_at=0))
        with self.assertRaises(S.Problem):
            S.sync()
        st = S.status_info()
        self.assertEqual(st['status'], 'needs_login')
        self.assertEqual(st['counts']['days'], 20)
        ds = st['dataset']

        # ---- reconnect reuses the registration; an expired sign-in page is reported plainly
        n_clients = len(FAKE.clients)
        FAKE.claims_until_authorized = 10 ** 6
        S.login_start({'region': 'us'})
        self.assertEqual(len(FAKE.clients), n_clients)
        p = S._state()['pending']
        S._update_state(pending=dict(p, expires_at='2000-01-01T00:00:00Z'))
        with self.assertRaises(S.Problem) as e:
            S.login_poll_once()
        self.assertIn('expired', e.exception.message)
        self.wait(lambda: not S.LOGIN_LOCK.locked())
        FAKE.claims_until_authorized = 0
        S.login_start({'region': 'us'})
        self.wait(lambda: S._tokens().get('access_token'))
        self.wait_sync()

        # ---- disconnect revokes and wipes; forget drops the registration too
        rt = S._tokens()['refresh_token']
        code, body = http(self.port, 'POST', '/api/disconnect', {'forget_registration': True})
        self.assertEqual(json.loads(body)['status'], 'not_configured')
        self.assertIn(rt, FAKE.revoked)
        blob = self.files_text()
        for sec in [rt] + list(FAKE.access):
            self.assertNotIn(sec, blob)
        self.assertFalse((S.DATA / 'client.json').exists())
        st = S.status_info()
        self.assertEqual(st['counts'], {'days': 0, 'sleeps': 0, 'workouts': 0})
        self.assertNotEqual(st['dataset'], ds)


class ParseTest(unittest.TestCase):
    """The Tool Guide's verbatim examples, plus tolerant reading of plausible variants for undocumented formats."""
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / 'app'))
        import server
        cls.S = server

    def test_doc_sleep(self):
        S = self.S
        r = S.parse_sleep(DOC_SLEEP)['2026-10-15']
        self.assertEqual((r['score'], r['asleep_h'], r['daily_h'], r['deep_pct'], r['light_pct'], r['rem_pct'], r['awake_h'], r['naps_h']),
                         (91, 7.95, 7.95, 27, 41, 31, 0.08, 0.0))
        n = S._n_sleep(r)
        self.assertEqual((n['deep_h'], n['light_h'], n['rem_h'], n['score'], n['naps']), (2.15, 3.26, 2.46, 91, []))

    def test_doc_records_and_detail(self):
        S = self.S
        a = S.parse_activities(DOC_RECORDS)
        self.assertEqual(len(a), 1)
        self.assertEqual((a[0]['date'], a[0]['sport_type'], a[0]['label_id'], a[0]['duration_h'], a[0]['distance_m']), ('2026-10-03', 9807, None, 2.21, 12030))
        d = S.parse_activity_detail(DOC_DETAIL)
        self.assertEqual((d['avg_hr_bpm'], d['calories_kcal'], d['duration_h'], d['training_load']), (76, 372, 2.21, None))
        w = S._n_workout(dict(a[0], detail=d))
        self.assertEqual((w['sport'], w['minutes'], w['load'], w['distance_m']), ('Workout', 133, None, 12030))

    def test_variants(self):
        S = self.S
        self.assertEqual(S.parse_single('Resting Heart Rate\n2026-10-15: 52 bpm\n20261014: 50 bpm', 'resting'), {'2026-10-15': 52, '2026-10-14': 50})
        self.assertEqual(S.parse_single('2026-10-15\nResting Heart Rate: 51 bpm\n', 'resting'), {'2026-10-15': 51})
        h = S.parse_hrv('2026-10-15\nDaily Average HRV: 61 ms\nNormal Range: 48–70 ms\nEvaluation: Balanced')
        self.assertEqual(h['2026-10-15']['hrv_ms'], 61)
        self.assertEqual(h['2026-10-15']['normal_range_ms'], [48, 70])
        self.assertEqual(S.parse_hrv('2026-10-15: 58 ms')['2026-10-15']['hrv_ms'], 58)
        self.assertEqual(S.parse_load('2026-10-15\nTraining Load: 120\nLoad Ratio: 1.3')['2026-10-15']['training_load'], 120)
        self.assertIsNone(S.parse_load('2026-10-15\nShort-term Load: 80\nLong-term Load: 60')['2026-10-15']['training_load'])
        self.assertEqual(S.parse_recovery('Recovery: 64%\nLevel: Fair')['recovery_pct'], 64)
        self.assertIsNone(S.parse_recovery('Nothing here')['recovery_pct'])
        self.assertEqual(S.hours('2:12:38'), 2.21)
        self.assertEqual(S.hours('45 min'), 0.75)
        self.assertEqual(S.hours('7 h'), 7.0)
        self.assertEqual(S.parse_sleep('Sleep Overview\n\nNo sleep data'), {})
        self.assertEqual(S.blocks_by_day(''), {})

    def test_sse(self):
        S = self.S
        raw = b'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"ok":1}}\n\n'
        self.assertEqual(S._sse_json(raw, {'Content-Type': 'text/event-stream'})['result'], {'ok': 1})
        self.assertEqual(S._sse_json(b'{"a":1}', {'Content-Type': 'application/json'}), {'a': 1})


DAY_KEYS = {'date', 'updated_at', 'readiness', 'hrv_ms', 'resting_hr_bpm', 'spo2_pct', 'skin_temp_c', 'skin_temp_delta_c', 'respiratory_rate',
            'sleep', 'load', 'calories_kcal', 'energy_kj', 'avg_hr_bpm', 'max_hr_bpm', 'steps', 'workouts', 'extras'}
SLEEP_KEYS = {'start', 'end', 'asleep_h', 'in_bed_h', 'awake_h', 'light_h', 'deep_h', 'rem_h', 'score', 'efficiency_pct', 'respiratory_rate', 'need_h', 'naps'}
WORKOUT_KEYS = {'id', 'sport', 'start', 'end', 'minutes', 'load', 'avg_hr_bpm', 'max_hr_bpm', 'calories_kcal', 'energy_kj', 'distance_m', 'zones_min', 'extras'}


def check_day(t, d):
    t.assertTrue(DAY_KEYS <= set(d), DAY_KEYS - set(d))
    t.assertIsNone(d['readiness'])
    if d['sleep']:
        t.assertTrue(SLEEP_KEYS <= set(d['sleep']), SLEEP_KEYS - set(d['sleep']))
    if d['load']:
        t.assertTrue({'value', 'max', 'label', 'scale_max'} <= set(d['load']))
    for w in d['workouts']:
        t.assertTrue(WORKOUT_KEYS <= set(w), WORKOUT_KEYS - set(w))
    t.assertEqual(set(d['extras']), {'coros'})


class IconTest(unittest.TestCase):
    """Own icon: full-colour store square and a one-colour silhouette; never COROS's logo or wordmark."""
    def test_icons(self):
        self.assertIn('\nicon: icon.png\n', (ROOT / 'wizard.md').read_text())
        png = (ROOT / 'icon.png').read_bytes()
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual((int.from_bytes(png[16:20], 'big'), int.from_bytes(png[20:24], 'big')), (1024, 1024))
        mask = (ROOT / 'web' / 'icon.svg').read_text()
        self.assertIn('viewBox="0 0 512 512"', mask)
        self.assertNotRegex(mask, r'<text|<image|href=|<script')
        self.assertNotIn('coros', (mask + (ROOT / 'tools' / 'icon-source.svg').read_text()).lower())


if __name__ == '__main__':
    unittest.main()
