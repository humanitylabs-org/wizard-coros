"""Coros: the COROS connector for a Wizard box (contract wizard.health-data/1).

Built on "Build on COROS MCP" (https://support.coros.com/hc/en-us/articles/53181619102996-Build-on-COROS-MCP), COROS's
self-service path for apps: no application or approval, OAuth 2.0 against the official COROS MCP server
(https://mcp.coros.com/mcp), single-user scope, no webhooks (the app polls). This app is an MCP client of that
official endpoint, the way it is documented in COROS's own GitHub repo (github.com/coroslab/COROS-MCP: Tool Guide and
the coros_mcp_login gateway helper):

- Sign-in: the app reads the gateway's OpenID configuration (which picks the owner's region: US, EU or CN), registers
  itself once with dynamic client registration (public client, PKCE, scopes `openid offline_access mcp.tools`), and
  opens a COROS login session (`POST /api/v1/cli/login-sessions`). The owner signs in to COROS on the login page in
  his own browser; the app polls the session's claim endpoint, gets a one-time login ticket, finishes the authorization
  request with it and exchanges the code (PKCE) for an access and refresh token. Nothing is pasted back.
- Data: MCP `tools/call` on querySleepOverview, querySleepHrv, queryRestingHeartRate, queryDailyHealthData,
  queryTrainingLoadAssessment, querySportRecords + getActivityDetail and queryRecoveryStatus. COROS answers in
  formatted text (examples in the Tool Guide), which the app reads line by line ("Label: value" under a date).

COROS publishes no 0-100 readiness history: queryRecoveryStatus is only the current recovery percentage. This source
therefore sends `readiness: null` (scales.readiness available: false) and keeps the current recovery snapshot under
extras.coros. Load is COROS Training Load (the sum of the day's activities), unchanged.

Stdlib only. Tokens live in DATA_DIR (0600 files, 0700 folder) and are never returned by any API or tool.
"""
import base64
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent.parent if '__file__' in globals() else Path(os.environ.get('APP_ROOT', '.')).resolve()
DATA = Path(os.environ.get('DATA_DIR', ROOT / '.data'))
APP_ID = 'wizard-coros'
NAME = 'Coros'
VERSION = '0.1.1'
PREFIX = '/' + APP_ID
PORT = os.environ.get('PORT', '8842')
GATEWAY = os.environ.get('COROS_GATEWAY', 'https://mcp.coros.com').rstrip('/')
REGIONS = {'auto': None, 'us': 'https://mcpus.coros.com', 'eu': 'https://mcpeu.coros.com', 'cn': 'https://mcpcn.coros.com'}
if os.environ.get('COROS_ISSUER'):          # tests and private setups pin one issuer
    REGIONS = {k: os.environ['COROS_ISSUER'].rstrip('/') for k in REGIONS}
SCOPES = 'openid offline_access mcp.tools'
CLIENT_NAME = 'Wizard Health (Coros connector)'
REDIRECT = 'http://127.0.0.1:43123/callback'   # never loaded: the login ticket finishes the authorization server-side
DOCS_URL = 'https://support.coros.com/hc/en-us/articles/53181619102996-Build-on-COROS-MCP'
SIGNUP_URL = os.environ.get('COROS_SIGNUP_URL', 'https://coros.com/app')
SYNC_MINUTES = float(os.environ.get('COROS_SYNC_MINUTES', '20'))
OVERLAP_DAYS = int(os.environ.get('COROS_OVERLAP_DAYS', '4'))
HISTORY_DAYS = int(os.environ.get('COROS_HISTORY_DAYS', '90'))
WEEK = 7                                   # HRV and other "recent days" tools cover at most 7 days per call
SLEEP_WINDOW = int(os.environ.get('COROS_SLEEP_WINDOW', '28'))
REQUEST_GAP = float(os.environ.get('COROS_REQUEST_GAP', '0.5'))
LOGIN_POLL = float(os.environ.get('COROS_LOGIN_POLL', '3'))
RETRIES = int(os.environ.get('COROS_RETRIES', '3'))
BACKOFF = float(os.environ.get('COROS_BACKOFF', '5'))
UA = f'{APP_ID}/{VERSION} (+Wizard OS)'
STATIC = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml'}
FORWARDED = ('tailscale-user-login', 'x-forwarded-for', 'forwarded', 'x-real-ip', 'cf-connecting-ip')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
LOCK = threading.RLock()
SYNC_LOCK = threading.Lock()
LOGIN_LOCK = threading.Lock()
WAKE = threading.Event()


class Problem(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class ProviderError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ---------------- private storage ----------------

def _private():
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
    return DATA


def _write_secret(name, text):
    p = _private() / name
    tmp = p.with_name('.' + name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def _read(name):
    try:
        return json.loads((DATA / name).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _shred(name):
    p = DATA / name
    try:
        size = p.stat().st_size
        with open(p, 'r+b') as f:
            f.write(b'\0' * size)
            f.flush()
            os.fsync(f.fileno())
        p.unlink()
    except FileNotFoundError:
        pass


def _client():
    return _read('client.json')


def _tokens():
    return _read('tokens.json')


def _save_tokens(t):
    with LOCK:
        if t:
            _write_secret('tokens.json', json.dumps(t))
        else:
            _shred('tokens.json')


def _state():
    return _read('state.json')


def _update_state(**kv):
    with LOCK:
        s = _state()
        for k, v in kv.items():
            if v is None:
                s.pop(k, None)
            else:
                s[k] = v
        _write_secret('state.json', json.dumps(s))
        return s


def secure_files():
    db().close()
    for p in DATA.iterdir():
        if p.is_file():
            os.chmod(p, 0o600)


def db():
    path = _private() / 'coros.sqlite3'
    fresh = not path.exists()
    c = sqlite3.connect(path, timeout=15)
    if fresh:
        os.chmod(path, 0o600)
    c.row_factory = sqlite3.Row
    c.executescript('''
      CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL, id TEXT NOT NULL, day TEXT, start TEXT, data TEXT NOT NULL,
        changed_seq INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (kind, id));
      CREATE INDEX IF NOT EXISTS records_day ON records(kind, day);
      CREATE INDEX IF NOT EXISTS records_changed ON records(changed_seq);
      CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, data TEXT NOT NULL);
    ''')
    return c


def _next_seq(c):
    n = int(json.loads((c.execute("SELECT data FROM meta WHERE key='seq'").fetchone() or ['0'])[0])) + 1
    c.execute("INSERT OR REPLACE INTO meta (key,data) VALUES ('seq',?)", (str(n),))
    return n


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(t):
    return t.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def _parse(s):
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _ago(iso):
    t = _parse(iso)
    return (_now() - t).total_seconds() if t else 1e12


def _tz():
    """The owner's time zone (sent by his browser when he connects); COROS reports local times without an offset."""
    name = _state().get('timezone') or os.environ.get('COROS_TIMEZONE') or 'UTC'
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return dt.timezone.utc


# ---------------- HTTP to COROS ----------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _http(method, url, form=None, body=None, headers=None, timeout=45):
    data, h = None, {'User-Agent': UA, 'Accept': 'application/json'}
    if form is not None:
        data = urlencode(form).encode()
        h['Content-Type'] = 'application/x-www-form-urlencoded'
    elif body is not None:
        data = json.dumps(body).encode()
        h['Content-Type'] = 'application/json'
    elif method == 'POST':
        data = b''
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProviderError(0, 'unreachable', 'COROS did not answer. Check the box is online; the app will retry.') from None


def _json(b):
    try:
        return json.loads(b or b'{}')
    except ValueError:
        return {}


def _sse_json(raw, headers):
    """A JSON-RPC reply as JSON or as a text/event-stream (the last `data:` event)."""
    kind = {k.lower(): v for k, v in headers.items()}.get('content-type', '')
    text = (raw or b'').decode('utf-8', 'replace')
    if 'text/event-stream' in kind or text.lstrip().startswith(('event:', 'data:')):
        events, cur = [], []
        for line in text.splitlines():
            if not line.strip():
                if cur:
                    events.append('\n'.join(cur)); cur = []
            elif line.startswith('data:'):
                cur.append(line[5:].lstrip())
        if cur:
            events.append('\n'.join(cur))
        for e in reversed(events):
            try:
                return json.loads(e)
            except ValueError:
                continue
        return {}
    return _json(raw)


def discover_issuer(region='auto'):
    """The issuer for the owner's region: fixed for us/eu/cn, else the one the gateway picks (as COROS's helper does)."""
    fixed = REGIONS.get(region or 'auto')
    if fixed:
        return fixed
    status, body, _ = _http('GET', GATEWAY + '/.well-known/openid-configuration', timeout=15)
    iss = _json(body).get('issuer') if status == 200 else None
    if isinstance(iss, str) and iss.startswith('https://') and (urlsplit(iss).hostname or '').endswith('.coros.com'):
        return iss.rstrip('/')
    if isinstance(iss, str) and iss.startswith('http://127.0.0.1'):   # tests
        return iss.rstrip('/')
    raise ProviderError(status, 'discovery_failed', 'COROS did not say which region to use. Pick your account region and try again.')


def _register(issuer):
    """Dynamic client registration (RFC 7591): a public client, once per box and region."""
    c = _client()
    if c.get('client_id') and c.get('issuer') == issuer:
        return c['client_id']
    status, body, _ = _http('POST', issuer + '/connect/register', body={
        'client_name': CLIENT_NAME, 'redirect_uris': [REDIRECT], 'grant_types': ['authorization_code', 'refresh_token'],
        'response_types': ['code'], 'scope': SCOPES, 'token_endpoint_auth_method': 'none'})
    cid = _json(body).get('client_id')
    if status not in (200, 201) or not isinstance(cid, str) or not cid:
        raise ProviderError(status, 'register_failed', f'COROS did not register this app ({status}). Try again in a minute.')
    _write_secret('client.json', json.dumps({'client_id': cid, 'issuer': issuer, 'registered_at': _iso(_now())}))
    return cid


def _pkce():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip('=')
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    return verifier, challenge


def _token_call(issuer, form):
    status, body, _ = _http('POST', issuer + '/oauth2/token', form=form)
    return status, _json(body)


def _keep_tokens(d, issuer, client_id):
    if not d.get('access_token') or not d.get('refresh_token'):
        raise ProviderError(502, 'token_failed', 'COROS did not return a complete sign-in. Connect again.')
    _save_tokens({'access_token': d['access_token'], 'refresh_token': d['refresh_token'], 'issuer': issuer, 'client_id': client_id,
                  'expires_at': time.time() + int(d.get('expires_in') or 3600), 'scope': d.get('scope') or SCOPES})


# ---------------- sign-in (COROS login session) ----------------

def login_start(body=None):
    body = body or {}
    region = str(body.get('region') or 'auto')
    if region not in REGIONS:
        raise Problem(400, 'Pick your COROS account region: Automatic, United States, Europe or China.')
    tz = str(body.get('timezone') or '')
    if tz:
        try:
            ZoneInfo(tz)
            _update_state(timezone=tz)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    issuer = discover_issuer(region)
    cid = _register(issuer)
    verifier, challenge = _pkce()
    st = secrets.token_urlsafe(24)
    status, raw, _ = _http('POST', issuer + '/api/v1/cli/login-sessions', body={'clientId': cid})
    d = _json(raw)
    if status == 401 or str(d.get('error')) == 'invalid_client':
        _shred('client.json')   # the registration is gone on COROS's side: register again next time
        raise ProviderError(status, 'register_lost', 'COROS no longer knew this app. Tap Connect with COROS again.')
    if status not in (200, 201) or not all(isinstance(d.get(k), str) and d.get(k) for k in ('sessionId', 'pollToken', 'loginUrl')):
        raise ProviderError(status, 'session_failed', f'COROS could not start a sign-in ({status}). Try again in a minute.')
    login_url = d['loginUrl']
    if urlsplit(login_url).scheme != 'https' and not login_url.startswith('http://127.0.0.1'):
        raise ProviderError(502, 'session_failed', 'COROS sent an unexpected sign-in address.')
    pending = {'issuer': issuer, 'client_id': cid, 'verifier': verifier, 'challenge': challenge, 'state': st, 'region': region,
               'session_id': d['sessionId'], 'poll_token': d['pollToken'], 'login_url': login_url,
               'expires_at': d.get('expiresAt') or _iso(_now() + dt.timedelta(minutes=5)),
               'interval': max(1.0, float(d.get('intervalSeconds') or LOGIN_POLL)), 'started_at': _iso(_now())}
    _update_state(pending=pending, login_error=None, region=region)
    threading.Thread(target=_safe_finish, daemon=True).start()
    return {'login_url': login_url, 'expires_at': pending['expires_at']}


def _authorize_url(p):
    q = {'response_type': 'code', 'client_id': p['client_id'], 'redirect_uri': REDIRECT, 'scope': SCOPES,
         'code_challenge': p['challenge'], 'code_challenge_method': 'S256', 'resource': p['issuer'] + '/mcp', 'state': p['state']}
    return p['issuer'] + '/oauth2/authorize?' + urlencode(q)


def login_poll_once():
    """One claim poll. Returns 'pending', 'connected' or raises Problem with a plain reason."""
    p = _state().get('pending')
    if not p:
        raise Problem(409, 'No COROS sign-in is waiting. Tap Connect with COROS.')
    if _ago(p['expires_at']) > 0:
        _update_state(pending=None, login_error='The COROS sign-in page expired. Tap Connect with COROS again.')
        raise Problem(410, 'The COROS sign-in page expired. Tap Connect with COROS again.')
    status, raw, _ = _http('POST', f"{p['issuer']}/api/v1/cli/login-sessions/{p['session_id']}/claim", headers={'X-Poll-Token': p['poll_token']})
    d = _json(raw)
    st = str(d.get('status') or '').lower()
    if status == 200 and st == 'pending':
        return 'pending'
    if status != 200 or st != 'authorized' or not d.get('loginTicket'):
        msg = {'expired': 'The COROS sign-in page expired. Tap Connect with COROS again.',
               'failed': 'COROS did not finish the sign-in. Tap Connect with COROS to try again.',
               'claimed': 'This COROS sign-in was already used. Tap Connect with COROS again.'}.get(st, f'COROS did not finish the sign-in ({status}). Tap Connect with COROS again.')
        _update_state(pending=None, login_error=msg)
        raise Problem(400, msg)
    # finish the authorization request with the one-time login ticket; COROS answers with a redirect carrying the code
    u = urlsplit(_authorize_url(p))
    url = urlunsplit(u._replace(query=u.query + '&' + urlencode({'login_ticket': d['loginTicket']})))
    status, _, headers = _http('GET', url)
    loc = {k.lower(): v for k, v in headers.items()}.get('location', '')
    q = {k: v[0] for k, v in parse_qs(urlsplit(loc).query).items()}
    if status not in (302, 303) or not q.get('code') or not secrets.compare_digest(q.get('state', ''), p['state']):
        msg = 'COROS did not allow access. Tap Connect with COROS to try again.' if q.get('error') else 'COROS did not finish the sign-in. Tap Connect with COROS again.'
        _update_state(pending=None, login_error=msg)
        raise Problem(400, msg)
    status, tok = _token_call(p['issuer'], {'grant_type': 'authorization_code', 'client_id': p['client_id'], 'code': q['code'],
                                            'redirect_uri': REDIRECT, 'code_verifier': p['verifier']})
    if status != 200:
        _update_state(pending=None, login_error='COROS refused the sign-in code. Tap Connect with COROS again.')
        raise Problem(400, 'COROS refused the sign-in code. Tap Connect with COROS again.')
    _keep_tokens(tok, p['issuer'], p['client_id'])
    _update_state(pending=None, auth='connected', login_error=None, last_error=None, connected_at=_iso(_now()), issuer=p['issuer'])
    start_sync_thread(full=not _state().get('backfilled'))
    return 'connected'


def _safe_finish():
    if not LOGIN_LOCK.acquire(blocking=False):
        return
    try:
        while True:
            p = _state().get('pending')
            if not p:
                return
            try:
                if login_poll_once() == 'connected':
                    return
            except (Problem, ProviderError):
                return
            time.sleep(float(p.get('interval') or LOGIN_POLL))
    finally:
        LOGIN_LOCK.release()


def login_cancel():
    _update_state(pending=None, login_error=None)
    return status_info()


def test_service(region=None):
    """Check against COROS's real token endpoint with a made-up refresh token. A registered client answers
    invalid_grant (COROS knows this app); an unknown one invalid_client (sign-in service up; Connect registers it)."""
    region = region or _state().get('region') or 'auto'
    try:
        issuer = discover_issuer(region if region in REGIONS else 'auto')
    except ProviderError as e:
        return {'result': 'unknown', 'message': e.message}
    c = _client()
    cid = c.get('client_id') if c.get('issuer') == issuer else None
    status, d = _token_call(issuer, {'grant_type': 'refresh_token', 'client_id': cid or 'wizard-check-' + secrets.token_hex(6),
                                     'refresh_token': 'wizard-check-' + secrets.token_hex(12)})
    err, host = str(d.get('error') or ''), urlsplit(issuer).hostname
    if cid and err == 'invalid_grant':
        out = {'result': 'valid', 'message': f'COROS ({host}) recognises this app. Tap Connect with COROS to sign in.'}
    elif err == 'invalid_client' and cid:
        _shred('client.json')
        out = {'result': 'valid', 'message': f'COROS ({host}) is answering. It will register this app again when you connect.'}
    elif err in ('invalid_client', 'invalid_grant', 'unauthorized_client'):
        out = {'result': 'valid', 'message': f'COROS sign-in ({host}) is answering. Tap Connect with COROS to sign in.'}
    elif status == 429:
        out = {'result': 'unknown', 'message': 'COROS is busy right now. Try again in a minute.'}
    else:
        out = {'result': 'unknown', 'message': f'COROS gave an unexpected answer ({status}). Try again shortly.'}
    _update_state(service_check=out['result'], service_checked_at=_iso(_now()))
    return out


def setup_info():
    st, c = _state(), _client()
    p = st.get('pending') or {}
    return {'docs_url': DOCS_URL, 'signup_url': SIGNUP_URL, 'regions': list(REGIONS), 'region': st.get('region') or 'auto',
            'registered': bool(c.get('client_id')), 'pending': bool(p), 'login_url': p.get('login_url'),
            'login_expires_at': p.get('expires_at'), 'login_error': st.get('login_error'),
            'service_check': st.get('service_check')}


# ---------------- MCP client ----------------

def _access():
    t = _tokens()
    if not t.get('access_token'):
        raise Problem(409, 'Not connected to COROS. Open the Coros app and tap Connect with COROS.')
    if (t.get('expires_at') or 0) - time.time() > 120:
        return t
    status, d = _token_call(t['issuer'], {'grant_type': 'refresh_token', 'client_id': t['client_id'], 'refresh_token': t['refresh_token']})
    if status == 200 and d.get('access_token'):
        d.setdefault('refresh_token', t['refresh_token'])
        _keep_tokens(d, t['issuer'], t['client_id'])
        return _tokens()
    if status in (400, 401):
        _needs_login('COROS ended this sign-in. Connect again in the Coros app.')
    raise ProviderError(status, 'refresh_failed', f'COROS could not renew the sign-in ({status}). The app will retry.')


def _needs_login(msg):
    _save_tokens(None)
    _update_state(auth='needs_login', last_error=msg)
    raise Problem(401, msg)


def _rpc(method, params, rid):
    t = _access()
    url = t['issuer'] + '/mcp'
    body = {'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params}
    status = 0
    for attempt in range(RETRIES + 1):
        status, raw, headers = _http('POST', url, body=body, headers={'Authorization': 'Bearer ' + t['access_token'],
                                                                     'Accept': 'application/json, text/event-stream'}, timeout=60)
        if status == 200:
            d = _sse_json(raw, headers)
            if d.get('error'):
                raise ProviderError(400, 'mcp_error', f"COROS refused {params.get('name', method)}: {str(d['error'].get('message', ''))[:120]}")
            return d.get('result') or {}
        if status == 401 and attempt == 0:
            _save_tokens(dict(_tokens(), expires_at=0))     # renew once, then retry
            t = _access()
            continue
        if status == 401:
            _needs_login('COROS ended this sign-in. Connect again in the Coros app.')
        if status == 429 or status >= 500:
            if attempt >= RETRIES:
                break
            time.sleep(BACKOFF * (2 ** attempt))
            continue
        raise ProviderError(status, 'mcp_error', f'COROS answered {status}.')
    if status == 429:
        _update_state(rate_limited_until=time.time() + 900)
        raise ProviderError(429, 'rate_limited', 'COROS asked the app to slow down. Syncing resumes in about 15 minutes.')
    raise ProviderError(status, 'mcp_error', f'COROS answered {status}. The app will retry.')


def mcp_init():
    return _rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                               'clientInfo': {'name': CLIENT_NAME, 'version': VERSION}}, 1)


def tool(name, args=None):
    """tools/call -> (text, structured). COROS answers in formatted text; JSON text or structuredContent is used as is."""
    r = _rpc('tools/call', {'name': name, 'arguments': args or {}}, 2)
    if r.get('isError'):
        msg = ' '.join(c.get('text', '') for c in r.get('content') or [] if isinstance(c, dict))[:160]
        raise ProviderError(400, 'tool_error', f'COROS could not answer {name}: {msg}')
    text = '\n'.join(c.get('text', '') for c in r.get('content') or [] if isinstance(c, dict) and c.get('type') == 'text')
    structured = r.get('structuredContent')
    if structured is None:
        try:
            structured = json.loads(text)
        except ValueError:
            structured = None
    time.sleep(REQUEST_GAP)
    return text, structured


# ---------------- reading COROS's text answers ----------------

_DAY_PAT = re.compile(r'(?<!\d)(\d{4})[-/.]?(\d{2})[-/.]?(\d{2})(?!\d)')


def _norm(k):
    return re.sub(r'[^a-z0-9%]+', ' ', str(k).lower()).strip()


def _date_of(s):
    m = _DAY_PAT.search(str(s))
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def blocks_by_day(text):
    """{date: {normalized label: raw value}} from COROS's formatted text. A line that is only a date (or starts a
    numbered item / heading with a date) opens a block; "Label: value" lines (also "A: x | B: y") fill it; a line
    "2026-10-15: 52 bpm" gives the block a '_value'."""
    out, cur = {}, None
    for raw in str(text or '').splitlines():
        line = raw.strip().strip('-*•').strip()
        if not line:
            continue
        d = _date_of(line)
        m = re.match(r'^(?:\d+\.\s*)?(\d{4}-\d{2}-\d{2}|\d{8})\s*[:：]\s*(.+)$', line)
        if m and _date_of(m.group(1)):
            cur = out.setdefault(_date_of(m.group(1)), {})
            rest = m.group(2)
            if ':' in rest:
                _pairs(rest, cur)
            else:
                cur['_value'] = rest
            continue
        head = re.sub(r'\s+', ' ', _DAY_PAT.sub('', line)).strip(' :—-()')
        if d and (':' not in line or line.index(':') > line.find(d[:4]) + 9 or re.match(r'^(?:\d+\.|[A-Za-z ()\d]+[—-])', line)) and len(head) < 60 and not re.search(r'\d{1,2}:\d{2}\s*-\s*', line):
            cur = out.setdefault(d, {})
            if head:
                cur.setdefault('_title', head)
            continue
        if cur is not None and ':' in line:
            _pairs(line, cur)
    return out


def _pairs(line, into):
    for part in re.split(r'\s+\|\s+', line):
        if ':' in part:
            k, v = part.split(':', 1)
            if k.strip():
                into[_norm(k)] = v.strip()


def num(v):
    m = re.search(r'-?\d+(?:\.\d+)?', str(v or '').replace(',', ''))
    return float(m.group(0)) if m else None


def hours(v):
    """'7h 57min', '7 h 57 min', '2:12:38', '45 min', '5 min' -> hours."""
    s = str(v or '').lower()
    m = re.search(r'(\d+):(\d{2})(?::(\d{2}))?', s)
    if m and not re.search(r'\d{4}-\d{2}', s):
        h, mi, se = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        return round(h + mi / 60 + se / 3600, 2)
    h = re.search(r'(\d+(?:\.\d+)?)\s*h', s)
    mi = re.search(r'(\d+(?:\.\d+)?)\s*m(?:in)?', s)
    if not h and not mi:
        return None
    return round((float(h.group(1)) if h else 0) + (float(mi.group(1)) / 60 if mi else 0), 2)


def _pick(b, *needles, avoid=()):
    for k, v in b.items():
        if all(n in k for n in needles) and not any(a in k for a in avoid):
            return v
    return None


def _local_iso(s):
    try:
        t = dt.datetime.strptime(s.strip(), '%Y-%m-%d %H:%M').replace(tzinfo=_tz())
    except ValueError:
        return None
    return _iso(t)


def parse_sleep(text):
    out = {}
    for day, b in blocks_by_day(text).items():
        asleep = hours(_pick(b, 'main sleep', 'asleep')) or hours(_pick(b, 'main sleep', avoid=('window',)))
        daily = hours(_pick(b, 'daily sleep'))
        win = _pick(b, 'sleep window') or ''
        m = re.findall(r'\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}', win)
        r = {'date': day, 'score': num(_pick(b, 'sleep score')), 'asleep_h': asleep, 'daily_h': daily,
             'deep_pct': num(_pick(b, 'deep')), 'light_pct': num(_pick(b, 'light')), 'rem_pct': num(_pick(b, 'rem')),
             'awake_h': hours(_pick(b, 'awake')), 'naps_h': hours(_pick(b, 'nap')),
             'start': _local_iso(m[0]) if len(m) >= 1 else None, 'end': _local_iso(m[1]) if len(m) >= 2 else None}
        if any(v is not None for k, v in r.items() if k != 'date'):
            out[day] = r
    return out


def parse_hrv(text):
    out = {}
    for day, b in blocks_by_day(text).items():
        v = num(b.get('_value')) if b.get('_value') else None
        v = v if v is not None else num(_pick(b, 'hrv', avoid=('range', 'baseline', 'normal')) or _pick(b, 'average') or _pick(b, 'avg'))
        rng = _pick(b, 'range') or _pick(b, 'normal')
        lo_hi = [float(x) for x in re.findall(r'\d+(?:\.\d+)?', str(rng or ''))[:2]]
        ev = _pick(b, 'evaluation') or _pick(b, 'assessment') or _pick(b, 'status')
        if v is not None:
            out[day] = {'date': day, 'hrv_ms': v, 'normal_range_ms': lo_hi if len(lo_hi) == 2 else None, 'evaluation': ev}
    return out


def parse_single(text, *needles):
    """Per-day single values ('2026-10-15: 52 bpm' or a block with a matching label)."""
    out = {}
    for day, b in blocks_by_day(text).items():
        v = num(b['_value']) if b.get('_value') else num(_pick(b, *needles))
        if v is not None:
            out[day] = v
    return out


def parse_daily(text):
    out = {}
    for day, b in blocks_by_day(text).items():
        r = {'date': day, 'steps': num(_pick(b, 'step')), 'calories_kcal': num(_pick(b, 'calor')),
             'avg_hr_bpm': num(_pick(b, 'average heart') or _pick(b, 'avg heart') or _pick(b, 'avg hr')),
             'resting_hr_bpm': num(_pick(b, 'resting')), 'stress_avg': num(_pick(b, 'stress')),
             'training_load': num(_pick(b, 'training load') or _pick(b, 'load', avoid=('ratio',)))}
        if any(v is not None for k, v in r.items() if k != 'date'):
            out[day] = r
    return out


def parse_load(text):
    out = {}
    for day, b in blocks_by_day(text).items():
        r = {'date': day, 'training_load': num(b['_value']) if b.get('_value') and not _pick(b, 'load') else num(_pick(b, 'training load') or _pick(b, 'daily load') or _pick(b, 'load', avoid=('ratio', 'short', 'long', 'acute', 'chronic', 'base', 'atl', 'ctl'))),
             'short_term_load': num(_pick(b, 'short') or _pick(b, 'acute') or _pick(b, 'atl')),
             'long_term_load': num(_pick(b, 'long') or _pick(b, 'chronic') or _pick(b, 'base') or _pick(b, 'ctl')),
             'load_ratio': num(_pick(b, 'ratio')), 'assessment': _pick(b, 'assessment') or _pick(b, 'status')}
        if any(v is not None for k, v in r.items() if k not in ('date', 'assessment')):
            out[day] = r
    return out


def parse_activities(text, structured=None):
    """Activity list: numbered items with a date, a sport code ('Sport(9807)' or 'SportType: 100') and a label id."""
    items = []
    if isinstance(structured, dict):
        structured = structured.get('records') or structured.get('activities') or structured.get('data')
    if isinstance(structured, list):
        for r in structured:
            if isinstance(r, dict) and (r.get('labelId') or r.get('LabelId')):
                items.append({'label_id': str(r.get('labelId') or r.get('LabelId')), 'sport_type': r.get('sportType') or r.get('SportType'),
                              'date': _date_of(r.get('date') or r.get('startTime') or ''), 'raw': {k: r[k] for k in list(r)[:30]}})
        return items
    cur = None
    for raw in str(text or '').splitlines():
        line = raw.strip()
        if re.match(r'^\d+\.\s', line):
            cur = {'date': _date_of(line), 'sport_type': None, 'label_id': None, 'title': re.sub(r'^\d+\.\s*', '', line), 'fields': {}}
            m = re.search(r'Sport\((\d+)\)', line)
            if m:
                cur['sport_type'] = int(m.group(1))
            items.append(cur)
            continue
        if cur is None:
            continue
        _pairs(line, cur['fields'])
    out = []
    for it in items:
        f = it.pop('fields', {})
        lid = _pick(f, 'labelid') or _pick(f, 'label id') or _pick(f, 'label')
        st = _pick(f, 'sporttype') or _pick(f, 'sport type')
        it['label_id'] = str(lid).strip() if lid else None
        if st and num(st) is not None:
            it['sport_type'] = int(num(st))
        it['duration_h'] = hours(_pick(f, 'duration'))
        dist = _pick(f, 'distance')
        it['distance_m'] = round(num(dist) * (1000 if 'km' in str(dist).lower() else 1)) if num(dist) is not None else None
        out.append(it)
    return out


def parse_activity_detail(text):
    b = {}
    for line in str(text or '').splitlines():
        if ':' in line:
            _pairs(line.strip(), b)
    tl = _pick(b, 'training load')
    return {'training_load': num(tl), 'avg_hr_bpm': num(_pick(b, 'average heart') or _pick(b, 'avg hr')),
            'max_hr_bpm': num(_pick(b, 'max heart') or _pick(b, 'maximum heart')), 'calories_kcal': num(_pick(b, 'calor')),
            'duration_h': hours(_pick(b, 'workout time') or _pick(b, 'duration')), 'start': _pick(b, 'start'),
            'aerobic_te': num(_pick(b, 'aerobic')), 'anaerobic_te': num(_pick(b, 'anaerobic')), 'training_focus': _pick(b, 'focus')}


def parse_recovery(text, structured=None):
    b = {}
    src = structured if isinstance(structured, dict) else None
    if src:
        b = {_norm(k): v for k, v in src.items()}
    for line in str(text or '').splitlines():
        if ':' in line:
            _pairs(line.strip(), b)
    pct = num(_pick(b, 'recovery', '%') or _pick(b, 'percent') or _pick(b, 'recovery', avoid=('level', 'time', 'hour', 'full')))
    return {'recovery_pct': pct if pct is not None and 0 <= pct <= 100 else None,
            'level': _pick(b, 'level'), 'hours_to_full': num(_pick(b, 'full') or _pick(b, 'hour'))}


# ---------------- sync ----------------

def _save(kind, rows):
    """Upsert {day: record}; changed_seq moves only when a record really changes."""
    n = 0
    with LOCK:
        c = db()
        with c:
            seq = _next_seq(c)
            for rid, r in rows.items():
                day = r.get('date')
                if not rid or not day or not DATE_RE.match(str(day)):
                    continue
                c.execute('''INSERT INTO records (kind,id,day,start,data,changed_seq) VALUES (?,?,?,?,?,?)
                             ON CONFLICT(kind,id) DO UPDATE SET day=excluded.day, start=excluded.start,
                               changed_seq=CASE WHEN records.data IS excluded.data THEN records.changed_seq ELSE excluded.changed_seq END,
                               data=excluded.data''',
                          (kind, str(rid), day, r.get('start'), json.dumps(r, sort_keys=True), seq))
                n += 1
        c.close()
    return n


def _raw(tool_name, text):
    """Keep the latest raw answer per tool (no tokens), so a later version can re-read it after a format change."""
    with LOCK:
        c = db()
        with c:
            c.execute('INSERT OR REPLACE INTO meta (key,data) VALUES (?,?)', ('raw:' + tool_name, json.dumps({'at': _iso(_now()), 'text': text[:20000]})))
        c.close()


def _stored(kind, rid):
    with LOCK:
        c = db()
        row = c.execute('SELECT data FROM records WHERE kind=? AND id=?', (kind, rid)).fetchone()
        c.close()
    return json.loads(row['data']) if row else None


def _ymd(d):
    return d.strftime('%Y%m%d')


def _windows(start, end, size):
    out, a = [], start
    while a <= end:
        b = min(end, a + dt.timedelta(days=size - 1))
        out.append((a, b))
        a = b + dt.timedelta(days=1)
    return out


def _progress(kind, n):
    _update_state(progress={'kind': kind, 'records': n})


def _call(name, args, parse, kind, counts):
    text, _ = tool(name, args)
    _raw(name, text)
    rows = parse(text)
    counts[kind] = counts.get(kind, 0) + _save(kind, rows)
    _progress(kind, counts[kind])
    return rows


def sync(full=False):
    if not SYNC_LOCK.acquire(blocking=False):
        return dict(status_info(), note='A sync is already running.')
    try:
        st = _state()
        if (st.get('rate_limited_until') or 0) > time.time():
            raise ProviderError(429, 'rate_limited', 'COROS asked the app to slow down. Syncing resumes shortly.')
        full = full or not st.get('backfilled')
        _update_state(syncing=_iso(_now()), sync_kind='backfill' if full else 'incremental')
        today = dt.datetime.now(_tz()).date()
        since = today - dt.timedelta(days=HISTORY_DAYS - 1) if full else \
            ((_parse(st.get('last_sync')) or _now()).astimezone(_tz()).date() - dt.timedelta(days=OVERLAP_DAYS))
        ndays = (today - since).days + 1
        counts = {}
        mcp_init()
        for a, b in _windows(since, today, SLEEP_WINDOW):
            _call('querySleepOverview', {'startDate': _ymd(a), 'endDate': _ymd(b)}, parse_sleep, 'sleep', counts)
        for a, b in _windows(since, today, WEEK):
            _call('querySleepHrv', {'startDate': _ymd(a), 'endDate': _ymd(b)}, parse_hrv, 'hrv', counts)
        _call('queryRestingHeartRate', {'days': ndays}, lambda x: {d: {'date': d, 'value': v} for d, v in parse_single(x, 'resting').items()}, 'rhr', counts)
        _call('queryDailyHealthData', {'days': ndays}, parse_daily, 'daily', counts)
        _call('queryTrainingLoadAssessment', {'days': ndays}, parse_load, 'load', counts)
        n_act = 0
        for a, b in _windows(since, today, 30):
            text, structured = tool('querySportRecords', {'startDate': _ymd(a), 'endDate': _ymd(b), 'limit': 100})
            _raw('querySportRecords', text)
            acts = {}
            for it in parse_activities(text, structured):
                if not it.get('date'):
                    continue
                rid = f"{it['label_id']}:{it.get('sport_type')}" if it.get('label_id') else \
                    f"{it['date']}:{it.get('sport_type')}:{it.get('duration_h')}:{it.get('distance_m')}"
                old = _stored('activity', rid)
                if old and old.get('detail'):
                    it['detail'] = old['detail']
                elif it.get('label_id') and it.get('sport_type') is not None:
                    try:
                        it['detail'] = parse_activity_detail(tool('getActivityDetail', {'labelId': it['label_id'], 'sportType': it['sport_type']})[0])
                    except ProviderError:
                        it['detail'] = None
                acts[rid] = it
            n_act += _save('activity', acts)
            _progress('activities', n_act)
        counts['activities'] = n_act
        try:
            text, structured = tool('queryRecoveryStatus', {})
            rec = parse_recovery(text, structured)
            if rec.get('recovery_pct') is not None:
                _update_state(recovery={**rec, 'at': _iso(_now()), 'date': today.isoformat()})
        except ProviderError:
            pass
        _update_state(last_sync=_iso(_now()), last_error=None, backfilled=True, auth='connected', syncing=None, progress=None,
                      sync_kind=None, last_counts=counts, rate_limited_until=None)
        return dict(status_info(), pulled=counts)
    except (Problem, ProviderError) as e:
        _update_state(last_error=e.message, syncing=None, progress=None, sync_kind=None)
        raise
    finally:
        SYNC_LOCK.release()


def _safe_sync(full=False):
    try:
        sync(full)
    except (Problem, ProviderError):
        pass
    except Exception:
        _update_state(last_error='Sync failed unexpectedly; it will retry.', syncing=None, progress=None)


def start_sync_thread(full=False):
    threading.Thread(target=_safe_sync, args=(full,), daemon=True).start()


def _scheduler():
    time.sleep(5)
    while True:
        st = _state()
        if _tokens().get('access_token'):
            due = not st.get('backfilled') or _ago(st.get('last_sync')) >= SYNC_MINUTES * 60
            if due and (st.get('rate_limited_until') or 0) <= time.time():
                _safe_sync(full=not st.get('backfilled'))
        elif st.get('pending') and not LOGIN_LOCK.locked():
            threading.Thread(target=_safe_finish, daemon=True).start()   # a restart interrupted a sign-in
        WAKE.wait(60)
        WAKE.clear()


# ---------------- the health-data contract ----------------

CONTRACT = 'wizard.health-data/1'
SOURCE = {'id': APP_ID, 'provider': 'coros', 'name': NAME, 'app_path': PREFIX + '/'}
# Load = COROS Training Load for the day (the sum of the day's activities, read from getActivityDetail; else the daily
# value COROS reports), unchanged and open-ended. scale_max 300 is only the top of the drawing scale; COROS publishes
# no daily targets, so none are sent. COROS has no readiness history: readiness is null on every day.
LOAD_SCALE_MAX = 300
LOAD = {'label': 'Training load', 'max': None, 'scale_max': LOAD_SCALE_MAX, 'unit': 'TL'}
SCALES = {'readiness': {'label': 'Recovery', 'min': 0, 'max': 100, 'available': False,
                        'note': 'COROS shares only the current recovery percentage, with no daily history; this source sends no readiness.'},
          'sleep_score': {'label': 'Sleep score', 'min': 0, 'max': 100},
          'load': dict(LOAD, min=0, formula='coros-training-load/1')}


def _r(v, n=1):
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    return round(v) if n == 0 else round(v, n)


def _dataset():
    st = _state()
    if not st.get('dataset'):
        st = _update_state(dataset=secrets.token_hex(12))
    return st['dataset']


def _n_sleep(s):
    asleep = s.get('asleep_h')
    part = lambda p: round(asleep * p / 100, 2) if asleep is not None and p is not None else None
    a, b = _parse(s.get('start')), _parse(s.get('end'))
    in_bed = round((b - a).total_seconds() / 3600, 2) if a and b and b > a else None
    naps_h = s.get('naps_h')
    if naps_h is None and s.get('daily_h') is not None and asleep is not None and s['daily_h'] > asleep:
        naps_h = round(s['daily_h'] - asleep, 2)
    score = s.get('score')
    return {'start': s.get('start'), 'end': s.get('end'), 'asleep_h': asleep, 'in_bed_h': in_bed, 'awake_h': s.get('awake_h'),
            'light_h': part(s.get('light_pct')), 'deep_h': part(s.get('deep_pct')), 'rem_h': part(s.get('rem_pct')),
            'score': int(score) if isinstance(score, (int, float)) and 0 <= score <= 100 else None,
            'efficiency_pct': round(100 * asleep / in_bed) if asleep and in_bed else None, 'respiratory_rate': None, 'need_h': None,
            'naps': [{'start': None, 'end': None, 'asleep_h': naps_h}] if naps_h else []}


def _n_workout(a):
    d = a.get('detail') or {}
    tl = d.get('training_load')
    dur = d.get('duration_h') if d.get('duration_h') is not None else a.get('duration_h')
    title = re.sub(r'Sport\(\d+\)', '', (a.get('title') or '').split('—')[0]).strip()[:40]
    return {'id': a.get('label_id') or f"{a.get('date')}-{a.get('sport_type')}-{a.get('duration_h')}", 'sport': title or 'Workout',
            'start': None, 'end': None, 'minutes': round(dur * 60) if dur is not None else None,
            'load': dict(LOAD, value=_r(tl)) if tl is not None else None,
            'avg_hr_bpm': _r(d.get('avg_hr_bpm'), 0), 'max_hr_bpm': _r(d.get('max_hr_bpm'), 0), 'calories_kcal': _r(d.get('calories_kcal'), 0),
            'energy_kj': None, 'distance_m': a.get('distance_m'), 'zones_min': None,
            'extras': {'coros': {'sport_type': a.get('sport_type'), 'aerobic_te': d.get('aerobic_te'), 'anaerobic_te': d.get('anaerobic_te'),
                                 'training_focus': d.get('training_focus'), 'training_load': tl}}}


def _n_day(day, rows):
    sl, hrv, daily, load = rows.get('sleep'), rows.get('hrv') or {}, rows.get('daily') or {}, rows.get('load') or {}
    acts = rows.get('activity') or []
    rhr = (rows.get('rhr') or {}).get('value')
    if rhr is None:
        rhr = daily.get('resting_hr_bpm')
    tls = [(a.get('detail') or {}).get('training_load') for a in acts]
    tls = [x for x in tls if isinstance(x, (int, float))]
    tl = sum(tls) if tls else (daily.get('training_load') if daily.get('training_load') is not None else load.get('training_load'))
    rec = _state().get('recovery') or {}
    extras = {'readiness_available': False,
              'recovery_now': {k: rec.get(k) for k in ('recovery_pct', 'level', 'hours_to_full', 'at')} if rec.get('date') == day else None,
              'hrv_normal_range_ms': hrv.get('normal_range_ms'), 'hrv_evaluation': hrv.get('evaluation'),
              'stress_avg': daily.get('stress_avg'), 'short_term_load': load.get('short_term_load'),
              'long_term_load': load.get('long_term_load'), 'load_ratio': load.get('load_ratio'),
              'load_assessment': load.get('assessment')}
    return {
        'date': day, 'updated_at': (sl or {}).get('end'), 'readiness': None,
        'hrv_ms': hrv.get('hrv_ms'), 'resting_hr_bpm': _r(rhr, 0), 'spo2_pct': None, 'skin_temp_c': None, 'skin_temp_delta_c': None,
        'respiratory_rate': None, 'sleep': _n_sleep(sl) if sl else None,
        'load': dict(LOAD, value=_r(tl)) if isinstance(tl, (int, float)) and tl >= 0 else None,
        'calories_kcal': _r(daily.get('calories_kcal'), 0), 'energy_kj': None, 'avg_hr_bpm': _r(daily.get('avg_hr_bpm'), 0),
        'max_hr_bpm': None, 'steps': _r(daily.get('steps'), 0),
        'workouts': [_n_workout(a) for a in acts], 'extras': {'coros': extras},
    }


def _build_days(dates):
    if not dates:
        return []
    with LOCK:
        c = db()
        try:
            marks = ','.join('?' * len(dates))
            by = {}
            for x in c.execute(f'SELECT kind, day, data FROM records WHERE day IN ({marks}) ORDER BY id', list(dates)):
                d = by.setdefault(x['day'], {})
                if x['kind'] == 'activity':
                    d.setdefault('activity', []).append(json.loads(x['data']))
                else:
                    d[x['kind']] = json.loads(x['data'])
        finally:
            c.close()
    return [_n_day(day, by.get(day, {})) for day in dates]


def _page_token(d):
    return base64.urlsafe_b64encode(json.dumps(d, separators=(',', ':')).encode()).decode().rstrip('=')


def _untoken(s):
    try:
        d = json.loads(base64.urlsafe_b64decode(s + '=' * (-len(s) % 4)))
        if isinstance(d, dict):
            return d
    except (ValueError, TypeError):
        pass
    raise Problem(400, 'page is not a page token from this source.')


def _cursor_seq(s):
    if s in (None, ''):
        return 0
    m = re.fullmatch(r'c1\.(\d{1,15})', str(s))
    if not m:
        raise Problem(400, 'changed_since is not a cursor from this source.')
    return int(m.group(1))


def _seq_now(c):
    row = c.execute("SELECT data FROM meta WHERE key='seq'").fetchone()
    return int(json.loads(row['data'])) if row else 0


def _check_day(v, name):
    if v is None or v == '':
        return None
    if not DATE_RE.match(str(v)):
        raise Problem(400, f'{name} must be YYYY-MM-DD.')
    return str(v)


def contract_days(args):
    frm, to = _check_day(args.get('from'), 'from'), _check_day(args.get('to'), 'to')
    if frm and to and frm > to:
        raise Problem(400, 'from must be on or before to.')
    try:
        limit = max(1, min(500, int(args.get('limit') or 100)))
    except (TypeError, ValueError):
        raise Problem(400, 'limit must be a number.') from None
    page = _untoken(str(args['page'])) if args.get('page') else {}
    changed = 'changed_since' in args
    since = _cursor_seq(args.get('changed_since')) if changed else 0
    after = page.get('after') or ''
    with LOCK:
        c = db()
        try:
            hw = int(page.get('hw') or _seq_now(c))
            q, p = ['SELECT DISTINCT day FROM records WHERE day IS NOT NULL AND day>?'], [after]
            if frm:
                q.append('AND day>=?'); p.append(frm)
            if to:
                q.append('AND day<=?'); p.append(to)
            if changed:
                q.append('AND day IN (SELECT day FROM records WHERE changed_seq>? AND changed_seq<=?)')
                p += [since, hw]
            q.append('ORDER BY day LIMIT ?'); p.append(limit + 1)
            dates = [r['day'] for r in c.execute(' '.join(q), p)]
        finally:
            c.close()
    more = len(dates) > limit
    dates = dates[:limit]
    return {'contract': CONTRACT, 'source': APP_ID, 'dataset': _dataset(), 'days': _build_days(dates),
            'next_page': _page_token({'after': dates[-1], 'hw': hw}) if more else None, 'cursor': f'c1.{hw}'}


def _counts():
    with LOCK:
        c = db()
        out = {r['kind']: r['n'] for r in c.execute('SELECT kind, COUNT(*) n FROM records GROUP BY kind')}
        span = c.execute('SELECT MIN(day) a, MAX(day) b, COUNT(DISTINCT day) n FROM records').fetchone()
        c.close()
    return out, span['a'], span['b'], span['n']


def status_info():
    t, st = _tokens(), _state()
    if not t.get('access_token'):
        state = 'needs_login' if st.get('auth') == 'needs_login' else 'not_configured'
    elif st.get('syncing') and _ago(st.get('syncing')) < 7200:
        state = 'syncing'
    elif (st.get('rate_limited_until') or 0) > time.time():
        state = 'rate_limited'
    else:
        state = 'connected'
    counts, first, last, ndays = _counts()
    nxt = None
    if state in ('connected', 'rate_limited') and st.get('last_sync'):
        nxt = _iso(_parse(st['last_sync']) + dt.timedelta(minutes=SYNC_MINUTES))
    p = st.get('pending') or {}
    rec = st.get('recovery') or {}
    return {
        'contract': CONTRACT, 'source': SOURCE, 'status': state, 'version': VERSION, 'account': None, 'dataset': _dataset(),
        'last_sync': st.get('last_sync'), 'next_sync': nxt, 'sync_every_minutes': SYNC_MINUTES,
        'history_from': first, 'latest_date': last,
        'counts': {'days': ndays, 'sleeps': counts.get('sleep', 0), 'workouts': counts.get('activity', 0)},
        'error': st.get('last_error'), 'scales': SCALES, 'provides': {'readiness': False},
        'recovery_now': {k: rec.get(k) for k in ('recovery_pct', 'level', 'hours_to_full', 'at')} if rec else None,
        'setup': {'pending': bool(p), 'login_url': p.get('login_url'), 'login_expires_at': p.get('expires_at'),
                  'login_error': st.get('login_error'), 'region': st.get('region') or 'auto',
                  'region_host': urlsplit(t.get('issuer') or p.get('issuer') or '').hostname,
                  'backfilled': bool(st.get('backfilled')),
                  'sync_kind': st.get('sync_kind') if state == 'syncing' else None,
                  'progress': st.get('progress') if state == 'syncing' else None},
    }


def disconnect(forget=False):
    t = _tokens()
    if t.get('refresh_token'):
        try:   # best effort: revoke the refresh token (RFC 7009), which ends this app's access
            _http('POST', t['issuer'] + '/oauth2/revoke', form={'token': t['refresh_token'], 'token_type_hint': 'refresh_token',
                                                              'client_id': t['client_id']}, timeout=15)
        except ProviderError:
            pass
    with LOCK:
        _save_tokens(None)
        for p in list(DATA.glob('coros.sqlite3*')):
            p.unlink(missing_ok=True)
        _shred('state.json')
        if forget:
            _shred('client.json')
    return status_info()


# ---------------- MCP (this app's own tools) ----------------

_NONE = {'type': 'object', 'properties': {}, 'additionalProperties': False}
TOOLS = [
    {'name': 'health_data_status', 'description': "Coros connector status (contract wizard.health-data/1): not_configured, needs_login, syncing, connected or rate_limited; last/next sync, history span, record counts and COROS's current recovery % snapshot. Never returns tokens. If not connected, send the owner the Coros app link [Coros](/wizard-coros/); its screen has one Connect with COROS button (sign in on COROS's own page). Never ask for COROS passwords in chat. For health questions use the Health app's tools instead.",
     'inputSchema': _NONE, 'annotations': {'readOnlyHint': True}},
    {'name': 'health_data_sync', 'description': 'Ask the Coros connector to pull new data from COROS now (it also syncs every 20 minutes). Returns at once; check health_data_status for progress. Reads COROS only.',
     'inputSchema': _NONE, 'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True}},
    {'name': 'health_data_days', 'description': 'Raw contract read: normalized COROS day records (no readiness: COROS has no daily score; sleep score and stages, overnight HRV, resting HR, COROS Training Load as the load, steps, calories, activities, extras.coros), oldest first, paged with next_page; pass changed_since=<cursor> to get only changed days. For owner questions prefer the Health app tools.',
     'inputSchema': {'type': 'object', 'properties': {
         'from': {'type': 'string', 'description': 'YYYY-MM-DD inclusive'}, 'to': {'type': 'string', 'description': 'YYYY-MM-DD inclusive'},
         'limit': {'type': 'integer', 'description': 'days per page, default 100, max 500'},
         'page': {'type': 'string', 'description': 'next_page from the previous reply'},
         'changed_since': {'type': 'string', 'description': 'cursor from an earlier reply; empty string = everything'}},
         'additionalProperties': False}, 'annotations': {'readOnlyHint': True}},
]


def request_sync(full=False):
    s = status_info()
    if s['status'] in ('not_configured', 'needs_login'):
        raise Problem(409, 'Coros is not connected. The owner connects in the Coros app (/wizard-coros/).')
    start_sync_thread(full=full)
    return dict(status_info(), note='Sync started.')


def call(name, a):
    if name == 'health_data_status':
        return status_info()
    if name == 'health_data_sync':
        return request_sync()
    if name == 'health_data_days':
        return contract_days(a)
    raise KeyError(name)


def rpc(msg):
    if not isinstance(msg, dict):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Expected a JSON-RPC object.'}}
    m, i, p = msg.get('method'), msg.get('id'), msg.get('params') or {}
    if 'id' not in msg:
        return None
    try:
        if m == 'initialize':
            r = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': APP_ID, 'version': VERSION}}
        elif m == 'tools/list':
            r = {'tools': TOOLS}
        elif m == 'tools/call':
            try:
                v = call(p.get('name'), p.get('arguments') or {})
            except KeyError:
                return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32602, 'message': 'Unknown tool.'}}
            r = {'content': [{'type': 'text', 'text': json.dumps(v, ensure_ascii=False)}], 'structuredContent': v, 'isError': False}
        elif m == 'ping':
            r = {}
        else:
            return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32601, 'message': 'Unknown method.'}}
    except (Problem, ProviderError) as e:
        r = {'content': [{'type': 'text', 'text': e.message}], 'isError': True}
    except (ValueError, TypeError, AttributeError):
        r = {'content': [{'type': 'text', 'text': 'Invalid arguments.'}], 'isError': True}
    return {'jsonrpc': '2.0', 'id': i, 'result': r}


# ---------------- HTTP ----------------

def _csv(name):
    return {x.strip().lower() for x in os.environ.get(name, '').split(',') if x.strip()}


_LOOPBACK = {f'127.0.0.1:{PORT}', f'localhost:{PORT}', f'{APP_ID}:{PORT}'}
TRUSTED_HOSTS = _csv('WIZARD_TRUSTED_HOSTS') | _LOOPBACK
TRUSTED_ORIGINS = _csv('WIZARD_TRUSTED_ORIGINS') | {'http://' + h for h in _LOOPBACK}
TRUSTED_NAMES = {h.split(':')[0] for h in TRUSTED_HOSTS}
CSP = ("default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
       "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")


def _host_ok(host):
    host = (host or '').lower()
    return host in TRUSTED_HOSTS or host.split(':')[0] in TRUSTED_NAMES


class Handler(BaseHTTPRequestHandler):
    def refused(self, write=False):
        host = (self.headers.get('Host') or '').lower()
        if not _host_ok(host):
            self.send(403, {'error': 'Unknown host.'}); return True
        origin = (self.headers.get('Origin') or '').lower()
        if origin and origin not in TRUSTED_ORIGINS and urlsplit(origin).netloc != host:
            self.send(403, {'error': 'Cross-origin request refused.'}); return True
        if write and not origin and (self.headers.get('Sec-Fetch-Site') or '') == 'cross-site':
            self.send(403, {'error': 'Cross-origin request refused.'}); return True
        return False

    def forwarded(self):
        return any(self.headers.get(h) for h in FORWARDED)

    def send(self, code, body=None, kind='application/json', extra=None):
        b = body if isinstance(body, bytes) else b'' if body is None else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(b)

    def route(self):
        p = urlsplit(self.path).path
        return '/' if p == PREFIX else p[len(PREFIX):] if p.startswith(PREFIX + '/') else p

    def body(self):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise Problem(415, 'Use application/json.')
        size = int(self.headers.get('Content-Length', '0') or 0)
        if not 0 <= size <= 65536:
            raise Problem(413, 'Request too large.')
        try:
            d = json.loads(self.rfile.read(size) or b'{}')
        except ValueError:
            raise Problem(400, 'Invalid JSON.') from None
        if not isinstance(d, dict):
            raise Problem(400, 'Expected a JSON object.')
        return d

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.refused():
            return
        path = self.route()
        query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query, keep_blank_values=True).items()}
        try:
            if path in ('/healthz', '/api/health'):
                s = status_info()
                return self.send(200, {'ok': True, 'app': APP_ID, 'version': VERSION, 'status': s['status'],
                                       'connected': s['status'] in ('connected', 'syncing', 'rate_limited'), 'last_sync': s['last_sync']})
            if path == '/api/status':
                return self.send(200, status_info())
            if path == '/api/setup':
                return self.send(200, setup_info())
            if path == '/api/days':
                if self.forwarded():
                    return self.send(403, {'error': 'Health data reads are box-only.'})
                return self.send(200, contract_days(query))
            if path == '/openapi.json':
                return self.send(200, (ROOT / 'openapi.json').read_bytes())
            name = 'index.html' if path == '/' else path[1:]
            kind = STATIC.get(Path(name).suffix)
            if kind and '/' not in name and not name.startswith('.') and (ROOT / 'web' / name).is_file():
                return self.send(200, (ROOT / 'web' / name).read_bytes(), kind)
            self.send(405 if path == '/mcp' else 404, {'error': 'Use POST /mcp.' if path == '/mcp' else 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except (ValueError, TypeError):
            self.send(400, {'error': 'Invalid query.'})

    def do_POST(self):
        if self.refused(write=True):
            return
        path = self.route()
        try:
            if path == '/mcp':
                if self.forwarded():
                    return self.send(403, {'error': 'MCP is box-only.'})
                out = rpc(self.body())
                return self.send(202 if out is None else 200, out)
            b = self.body()
            if path == '/api/login/start':
                return self.send(200, login_start(b))
            if path == '/api/login/cancel':
                return self.send(200, login_cancel())
            if path == '/api/check':
                return self.send(200, test_service(b.get('region')))
            if path == '/api/sync':
                return self.send(202, request_sync(full=bool(b.get('full'))))
            if path == '/api/disconnect':
                return self.send(200, disconnect(forget=bool(b.get('forget_registration'))))
            self.send(404, {'error': 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except ProviderError as e:
            self.send(429 if e.code == 'rate_limited' else 502, {'error': e.message})
        except (ValueError, TypeError):
            self.send(400, {'error': 'Invalid request.'})

    def log_message(self, *a):
        pass


def main():
    secure_files()
    st = _state()
    if st.get('syncing'):
        _update_state(syncing=None, progress=None, sync_kind=None)
    threading.Thread(target=_scheduler, daemon=True).start()
    ThreadingHTTPServer((os.environ.get('BIND', '0.0.0.0'), int(PORT)), Handler).serve_forever()

# ---- wizard.app-switches/1 ------------------------------------------------------------------------------------------
# Owner feature switches for the Tracker's Try it (wizard-os docs/APP_SWITCHES.md). Shared block: the same text ships in
# every Wizard app (template: wizard-app-manager templates/hello-wizard). Every new feature ships behind a switch, off by
# default: append it to SWITCHES with the issue it delivers, e.g.
#   {'id': 'compact-header', 'name': 'Compact top bar', 'description': '...', 'default': False,
#    'issues': ['humanitylabs-org/<repo>#20']}
# and gate the feature on switch_on('compact-header'). State lives on the box (DATA_DIR/switches.json), so the app's
# page, the Tracker (through the App Manager) and the agent see one state. GET/POST /api/switches, owner only: the
# box's Tailscale Serve owner login with X-Wizard-Switch: 1; never a bearer, agent grant or box-local host.
import json as _sw_json
import os as _sw_os
import re as _sw_re
import threading as _sw_threading
from pathlib import Path as _SwPath
from urllib.parse import urlsplit as _sw_urlsplit

SWITCHES = []
_SW_CONTRACT = 'wizard.app-switches/1'
_SW_ROUTE = _sw_re.compile(r'(?:/[a-z0-9][a-z0-9-]{0,62})?/api/switches/?')
_SW_LOCAL = {'localhost', '127.0.0.1', '::1', '[::1]', '0.0.0.0'}
_sw_lock = _sw_threading.Lock()


def _sw_file():
    return _SwPath(_sw_os.environ.get('WIZARD_SWITCHES_DIR') or _sw_os.environ.get('DATA_DIR') or '/data') / 'switches.json'


def _sw_saved():
    try:
        saved = _sw_json.loads(_sw_file().read_text())
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def switches_doc():
    saved = _sw_saved()
    return {'contract': _SW_CONTRACT, 'switches': [
        dict(s, on=saved[s['id']] if isinstance(saved.get(s['id']), bool) else bool(s.get('default'))) for s in SWITCHES]}


def switch_on(switch_id):
    return next((s['on'] for s in switches_doc()['switches'] if s['id'] == switch_id), False)


def _sw_set(switch_id, on):
    with _sw_lock:
        ids = {s['id'] for s in SWITCHES}
        saved = {k: v for k, v in _sw_saved().items() if k in ids and isinstance(v, bool)}
        saved[switch_id] = on
        f = _sw_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + '.tmp')
        tmp.write_text(_sw_json.dumps(saved))
        _sw_os.replace(tmp, f)
    return switches_doc()


def _sw_owner(h, write):
    hd = h.headers
    if hd.get('Authorization') or hd.get('X-Wizard-Switch') != '1':
        return False
    if (hd.get('Sec-Fetch-Site') or 'same-origin').lower() not in ('same-origin', 'none'):
        return False
    host = (hd.get('Host') or '').strip().lower()
    name = host.split(']')[0] + ']' if host.startswith('[') else host.rsplit(':', 1)[0]
    if not name or name in _SW_LOCAL or '.' not in name:      # box-local and container hosts never prove the owner
        return False
    origin = hd.get('Origin')
    trusted = {o.strip() for o in (_sw_os.environ.get('WIZARD_TRUSTED_ORIGINS') or '').split(',') if o.strip()}
    if origin and _sw_urlsplit(origin).netloc.lower() != host and origin not in trusted:
        return False
    if write and not origin:
        return False
    logins = [x.strip() for x in (hd.get_all('Tailscale-User-Login') or [])]
    owners = {x.strip().lower() for x in (_sw_os.environ.get('WIZARD_OWNER_LOGINS') or '').split(',') if x.strip()}
    return len(logins) == 1 and '@' in logins[0] and (not owners or logins[0].lower() in owners)


def _sw_send(h, code, doc):
    body = _sw_json.dumps(doc).encode()
    h.send_response(code)
    h.send_header('Content-Type', 'application/json')
    h.send_header('Cache-Control', 'no-store')
    h.send_header('Content-Length', str(len(body)))
    h.end_headers()
    if h.command != 'HEAD':
        h.wfile.write(body)


def _sw_handle(h):
    """Serve /api/switches (or /<app>/api/switches) and return True; False for every other request."""
    if not _SW_ROUTE.fullmatch(_sw_urlsplit(h.path).path):
        return False
    if h.command in ('GET', 'HEAD'):
        if not _sw_owner(h, False):
            _sw_send(h, 403, {'error': "Only this Wizard's owner can see these switches.", 'code': 'owner_only'})
        else:
            _sw_send(h, 200, switches_doc())
        return True
    if not _sw_owner(h, True):
        _sw_send(h, 403, {'error': "Only this Wizard's owner can change these switches.", 'code': 'owner_only'})
        return True
    try:
        if (h.headers.get('Content-Type') or '').split(';')[0].strip() != 'application/json':
            raise ValueError
        size = int(h.headers.get('Content-Length') or '0')
        if size <= 0 or size > 4096:
            raise ValueError
        body = _sw_json.loads(h.rfile.read(size))
        if not isinstance(body, dict) or not isinstance(body.get('id'), str) or not isinstance(body.get('on'), bool) \
                or set(body) - {'id', 'on'}:
            raise ValueError
    except (ValueError, TypeError):
        _sw_send(h, 400, {'error': 'Send JSON {"id": "...", "on": true|false}', 'code': 'invalid_input'})
        return True
    if body['id'] not in {s['id'] for s in SWITCHES}:
        _sw_send(h, 404, {'error': 'No such switch', 'code': 'unknown_switch'})
        return True
    try:
        _sw_send(h, 200, _sw_set(body['id'], body['on']))
    except OSError:
        _sw_send(h, 500, {'error': 'storage_failure'})
    return True


def wizard_switches_install(handler_cls):
    """Route the switch endpoint ahead of the app's own GET/HEAD/POST handlers."""
    for method in ('do_GET', 'do_HEAD', 'do_POST'):
        orig = getattr(handler_cls, method, None)
        if getattr(orig, '_wizard_switches', False):
            continue

        def wrapped(self, _orig=orig):
            if _sw_handle(self):
                return None
            if _orig is None:
                return self.send_error(501)
            return _orig(self)
        wrapped._wizard_switches = True
        setattr(handler_cls, method, wrapped)
    return handler_cls
# ---- end wizard.app-switches/1 ---------------------------------------------------------------------------------------

wizard_switches_install(Handler)


if __name__ == '__main__':
    main()
