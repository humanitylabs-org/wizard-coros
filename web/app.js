'use strict';
// Connector screen: status, Sync now, Disconnect, and the guided setup (sign in on COROS's own page).
// Reads only this app's documented relative API. Health data itself is shown by the Health app.
const NAME = document.title;
const APP = document.documentElement.dataset.app;
const el = id => document.getElementById(id);
let S = null, SETUP = null, pollTimer = null;

function say(text, kind = '') {
  const s = el('status');
  s.classList.toggle('wz-sr', !kind);
  s.classList.toggle('note', kind === 'note');
  s.textContent = text;
}
async function api(path, data) {
  const init = data === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) };
  const r = await fetch(path, init);
  let body = {};
  try { body = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(body.error || `The ${NAME} app could not do that. Try again.`);
  return body;
}
function h(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }
const dayLabel = (iso, opts = { day: 'numeric', month: 'short', year: 'numeric' }) => iso ? new Date(iso + 'T12:00:00').toLocaleDateString(undefined, opts) : '';
function ago(iso) {
  if (!iso) return 'never';
  const m = Math.round((Date.now() - new Date(iso)) / 60000);
  if (m < 1) return 'just now'; if (m < 60) return m + ' min ago';
  const hrs = Math.round(m / 60); return hrs < 36 ? hrs + ' h ago' : Math.round(hrs / 24) + ' days ago';
}
const n = (v, one, many) => `${v || 0} ${(v || 0) === 1 ? one : many}`;
const setHref = (id, v) => { const e = el(id); if (e && v) e.href = v; };

el('open-health').addEventListener('click', e => {
  try {
    const top = window.top;
    if (top !== window && top.wizardUX && top.location.origin === location.origin) {
      top.dispatchEvent(new CustomEvent('wizard:open', { detail: { app: 'wizard-health', path: '/wizard-health/' } }));
      e.preventDefault();
    }
  } catch (_) { /* not same-origin: follow the link */ }
});
document.addEventListener('click', e => { const nx = e.target.closest('[data-next]'); if (nx) showStep(+nx.dataset.next); });

function showStep(k) {
  for (const li of document.querySelectorAll('.hl-step')) {
    const on = +li.dataset.step === k;
    li.classList.toggle('open', on);
    li.querySelector('.hl-step-h').setAttribute('aria-expanded', String(on));
  }
  const li = document.querySelector(`.hl-step[data-step="${k}"]`);
  if (li) requestAnimationFrame(() => li.scrollIntoView({ block: 'nearest', behavior: 'smooth' }));
}
for (const li of document.querySelectorAll('.hl-step')) li.querySelector('.hl-step-h').onclick = () => showStep(+li.dataset.step);

function renderSetup() {
  setHref('signup-link', SETUP.signup_url);
  const su = (S && S.setup) || {};
  if (SETUP.region && !el('f-region').dataset.touched) el('f-region').value = SETUP.region;
  const a = el('login-link');
  a.hidden = !su.pending || !su.login_url;
  if (su.login_url) a.href = su.login_url;
  el('connect-note').textContent = su.pending ? 'Waiting for you to sign in on COROS\u2026 This screen updates by itself.' : (su.login_error || '');
  for (const li of document.querySelectorAll('.hl-step')) li.classList.toggle('done', +li.dataset.step === 1 && (su.pending || !!SETUP.registered));
}
el('f-region').addEventListener('change', () => { el('f-region').dataset.touched = '1'; });

el('connect').onclick = async () => {
  const b = el('connect'); b.disabled = true;
  let w = null;
  try { w = window.open('', '_blank'); } catch (e) {}   // opened inside the tap so phones don't block it
  try {
    let tz = '';
    try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (e) {}
    const r = await api('api/login/start', { region: el('f-region').value, timezone: tz });
    if (w) { try { w.opener = null; w.location.href = r.login_url; } catch (e) { w = null; } }
    const a = el('login-link'); a.href = r.login_url; a.hidden = false;
    el('connect-note').textContent = w ? 'COROS opened in a new tab. Sign in there; this screen updates by itself.' : 'Tap \u201cOpen COROS sign-in\u201d to continue.';
    await refresh(); poll(3000, 120);
  } catch (err) { if (w) w.close(); el('connect-note').textContent = err.message; }
  finally { b.disabled = false; }
};
el('svc-test').onclick = async () => {
  const b = el('svc-test'); b.disabled = true;
  const p = el('test-result'); p.textContent = `Checking with COROS\u2026`; p.dataset.kind = '';
  try { const r = await api('api/check', { region: el('f-region').value }); p.textContent = r.message; p.dataset.kind = r.result; }
  catch (err) { p.textContent = err.message; p.dataset.kind = 'invalid'; }
  finally { b.disabled = false; }
};

el('open-disconnect').onclick = () => { el('sd-forget').checked = false; WzSheet.open(el('sheet-disconnect')); };
el('sd-go').onclick = async () => {
  const b = el('sd-go'); b.disabled = true;
  try {
    await api('api/disconnect', { forget_registration: el('sd-forget').checked });
    WzSheet.close(el('sheet-disconnect'));
    say(`${NAME} disconnected.`, 'note');
    el('setup').dataset.opened = '';
    await refresh();
  } catch (err) { say(err.message, 'error'); }
  finally { b.disabled = false; }
};
el('sync-now').onclick = async () => {
  const b = el('sync-now'); b.disabled = true;
  try { S = await api('api/sync', {}); render(); poll(3000, 400); } catch (err) { say(err.message, 'error'); }
  finally { b.disabled = false; }
};

const PILL = { not_configured: ['Not connected', 'off'], needs_login: ['Sign in again', 'warn'],
  syncing: ['Syncing', 'ok'], connected: ['Connected', 'ok'], rate_limited: ['Paused', 'warn'] };
const live = () => S && ['connected', 'syncing', 'rate_limited'].includes(S.status);
function connLine() {
  const st = S.status, su = S.setup || {};
  if (st === 'not_configured') return su.pending ? 'Waiting for you to sign in on COROS\u2026' : 'Connect below so Health can show your data.';
  if (st === 'needs_login') return S.error || 'COROS ended the sign-in. Connect again in step 2.';
  if (st === 'syncing') {
    const p = su.progress;
    return (su.sync_kind === 'backfill' ? 'Downloading your COROS history' : 'Syncing') + (p ? ` (${p.kind}: ${p.records})` : '') + '\u2026';
  }
  if (st === 'rate_limited') return 'COROS asked for a pause; syncing resumes on its own.';
  return 'Synced ' + ago(S.last_sync);
}
function render() {
  const [label, cls] = PILL[S.status] || ['Unknown', 'off'];
  const pill = el('conn-pill'); pill.textContent = label; pill.className = 'wz-pill ' + cls;
  el('conn-line').textContent = connLine();
  const c = S.counts || {}, su = S.setup || {};
  el('account').hidden = !live();
  if (live()) {
    el('acct-sub').textContent = S.history_from ? `History from ${dayLabel(S.history_from)} \u00b7 ${n(c.days, 'day', 'days')}, ${n(c.sleeps, 'sleep', 'sleeps')}, ${n(c.workouts, 'workout', 'workouts')}` : 'Downloading history\u2026';
    el('sync-title').textContent = S.status === 'syncing' ? 'Syncing now' : 'Synced ' + ago(S.last_sync);
    el('sync-sub').textContent = `Checks COROS every ${S.sync_every_minutes} minutes` + (S.next_sync && S.status !== 'syncing' ? ' \u00b7 next ' + new Date(S.next_sync).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : '');
    el('sync-now').disabled = S.status === 'syncing';
  }
  el('sync-error').hidden = !(live() && S.error);
  el('sync-error').textContent = S.error || '';
  el('more').hidden = !live() && S.status !== 'needs_login';
  const dl = el('details'); dl.replaceChildren();
  const rec = S.recovery_now;
  const rows = [['Status', label], ['Last sync', S.last_sync ? new Date(S.last_sync).toLocaleString() : 'never'],
    ['History from', S.history_from ? dayLabel(S.history_from) : '\u2013'], ['Latest day', S.latest_date ? dayLabel(S.latest_date) : '\u2013'],
    ['Saved here', `${n(c.days, 'day', 'days')}, ${n(c.sleeps, 'sleep', 'sleeps')}, ${n(c.workouts, 'workout', 'workouts')}`],
    ['COROS server', su.region_host || '\u2013'],
    ['Recovery now', rec && rec.recovery_pct != null ? `${rec.recovery_pct}% (COROS, ${ago(rec.at)}; no daily history, so not sent as a score)` : '\u2013'],
    ['Shared as', 'health data for Health (read-only, box-only)'], ['App version', S.version]];
  if (S.error) rows.push(['Last problem', S.error]);
  for (const [k, v] of rows) dl.append(h('dt', '', k), h('dd', '', v));
  const setup = !live();
  el('setup').hidden = !setup;
  if (setup && SETUP) {
    renderSetup();
    if (!el('setup').dataset.opened) { el('setup').dataset.opened = '1'; showStep(su.pending || S.status === 'needs_login' ? 2 : 1); }
  }
}

async function refresh() {
  try {
    S = await api('api/status');
    if (!live()) SETUP = await api('api/setup');
    render();
  } catch (err) { say(err.message, 'error'); }
}
function poll(ms, times) {
  clearTimeout(pollTimer);
  let k = 0;
  const tick = async () => {
    await refresh();
    k++;
    const fast = S && k < times && (S.status === 'syncing' || (S.setup && S.setup.pending));
    pollTimer = setTimeout(tick, fast ? 3000 : 60000);
  };
  pollTimer = setTimeout(tick, ms);
}

if (!window.WizardApp || WizardApp.version !== 1) {
  say(`Wizard system functions are unavailable. Open ${NAME} through Wizard.`, 'error');
} else {
  WizardApp.ready({ name: APP, version: document.documentElement.dataset.version || '' });
  WizardApp.back.register({ canGoBack: () => false, back: () => false });
}
refresh().then(() => poll(S && (S.status === 'syncing' || (S.setup && S.setup.pending)) ? 3000 : 60000, 1200));
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
