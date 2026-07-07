"""
Static HTML/JS for the admin subsite, served by app.api.admin.

Role: Two self-contained pages — a login screen and the moderation dashboard —
returned as strings by the admin routes. Kept out of the public `frontend/` static
mount so the dashboard is only reachable through the auth-guarded route. All data
is loaded from the guarded /admin/api/* endpoints via fetch; these strings contain
no secrets. Plain triple-quoted strings (NOT f-strings) so JS `${}`/`{}` are literal.
"""

# --- Login page ---

LOGIN_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>admin · triangle-shows</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; min-height:100vh; display:grid; place-items:center;
         background:#0e0e10; color:#e8e6e3; font-family:'Space Mono',ui-monospace,monospace; }
  .box { width:min(90vw,340px); border:1px solid #2a2a2e; padding:1.6rem; border-radius:2px; }
  h1 { font-size:0.95rem; letter-spacing:0.08em; text-transform:uppercase; color:#c87941; margin:0 0 1.2rem; }
  input, button { width:100%; font-family:inherit; font-size:0.85rem; padding:0.55rem 0.6rem;
                  box-sizing:border-box; border-radius:2px; }
  input { background:#17171a; border:1px solid #2a2a2e; color:#e8e6e3; margin-bottom:0.7rem; }
  input:focus { outline:none; border-color:#c87941; }
  button { background:#c87941; border:1px solid #c87941; color:#0e0e10; cursor:pointer; font-weight:700; }
  button:hover { filter:brightness(1.1); }
  .err { color:#e0625f; font-size:0.75rem; min-height:1em; margin:0.7rem 0 0; }
</style>
</head><body>
  <div class="box">
    <h1>triangle-shows admin</h1>
    <form id="f">
      <input type="password" id="pw" placeholder="password" autocomplete="current-password" autofocus>
      <button type="submit">log in</button>
    </form>
    <p id="err" class="err"></p>
  </div>
<script>
  const f = document.getElementById('f'), err = document.getElementById('err');
  f.addEventListener('submit', async (e) => {
    e.preventDefault();
    err.textContent = '';
    try {
      const r = await fetch('/admin/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: document.getElementById('pw').value }),
      });
      if (r.ok) { location.href = '/admin'; }
      else { err.textContent = 'invalid password'; }
    } catch (_) { err.textContent = 'something went wrong'; }
  });
</script>
</body></html>
"""


# --- Dashboard page ---

ADMIN_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>admin · triangle-shows</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing:border-box; }
  body { margin:0; background:#0e0e10; color:#e8e6e3;
         font-family:'Space Mono',ui-monospace,monospace; font-size:0.82rem; }
  header { display:flex; align-items:center; gap:1rem; padding:0.8rem 1.1rem;
           border-bottom:1px solid #2a2a2e; position:sticky; top:0; background:#0e0e10; z-index:2; }
  header h1 { font-size:0.9rem; letter-spacing:0.08em; text-transform:uppercase; color:#c87941; margin:0; }
  header .sp { flex:1; }
  a { color:#c87941; }
  main { padding:1.1rem; max-width:1100px; margin:0 auto; }
  .controls { display:flex; flex-wrap:wrap; gap:0.5rem; align-items:center; margin-bottom:1rem; }
  .fbtn, button.link { font-family:inherit; font-size:0.72rem; text-transform:uppercase; letter-spacing:0.06em;
          padding:0.3rem 0.6rem; border:1px solid #3a3a3e; background:transparent; color:#a8a6a3;
          cursor:pointer; border-radius:2px; }
  .fbtn.on { background:#c87941; border-color:#c87941; color:#0e0e10; font-weight:700; }
  #search { flex:1; min-width:160px; font-family:inherit; font-size:0.8rem; padding:0.35rem 0.6rem;
            background:#17171a; border:1px solid #2a2a2e; color:#e8e6e3; border-radius:2px; }
  #count { color:#8a8a8e; font-size:0.72rem; margin-left:auto; }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:0.5rem 0.5rem; border-bottom:1px solid #1e1e22; vertical-align:top; }
  th { color:#8a8a8e; font-size:0.66rem; text-transform:uppercase; letter-spacing:0.08em; }
  td .sub { color:#8a8a8e; font-size:0.72rem; margin-top:0.15rem; }
  .b { display:inline-block; padding:0.05rem 0.4rem; border-radius:2px; font-size:0.68rem; font-weight:700; text-transform:uppercase; }
  .b.live { background:#1f3d2a; color:#7fd69a; }
  .b.nonlive { background:#3d2020; color:#e08a88; }
  .b.manual { background:#3a2f12; color:#e0b45f; }
  .reason { color:#8a8a8e; font-size:0.7rem; }
  td.actions { white-space:nowrap; }
  td.actions button { font-family:inherit; font-size:0.68rem; margin:0.1rem 0.15rem 0.1rem 0; padding:0.22rem 0.45rem;
          border:1px solid #3a3a3e; background:transparent; color:#c8c6c3; cursor:pointer; border-radius:2px; }
  td.actions button:hover { border-color:#c87941; color:#c87941; }
  .series { color:#8a8a8e; }
  .empty { color:#8a8a8e; text-align:center; padding:1.5rem; }
  #rules { white-space:pre-wrap; background:#17171a; border:1px solid #2a2a2e; padding:0.8rem;
           border-radius:2px; font-size:0.72rem; margin-top:1rem; color:#b8b6b3; }
  .hidden { display:none; }
</style>
</head><body>
  <header>
    <h1>triangle-shows admin</h1>
    <button class="link" onclick="showRules()">detection rules</button>
    <span class="sp"></span>
    <a href="/admin/logout">log out</a>
  </header>
  <main>
    <div class="controls">
      <button class="fbtn on" data-f="non_live" onclick="setFilter('non_live')">non-live</button>
      <button class="fbtn" data-f="live" onclick="setFilter('live')">live</button>
      <button class="fbtn" data-f="all" onclick="setFilter('all')">all</button>
      <input id="search" placeholder="search name / artist...">
      <span id="count"></span>
    </div>
    <pre id="rules" class="hidden"></pre>
    <table>
      <thead><tr><th>date</th><th>event</th><th>status</th><th>actions</th></tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </main>
<script>
  let RULES = null;
  const $ = (s) => document.querySelector(s);
  const state = { filter: 'non_live', search: '' };

  async function api(path, opts) {
    const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
    if (r.status === 401) { location.href = '/admin/login'; throw new Error('unauth'); }
    if (!r.ok) throw new Error('http ' + r.status);
    return r.status === 204 ? null : r.json();
  }

  function esc(s) {
    return (s || '').replace(/[&<>"]/g, (c) => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;' }[c]));
  }

  function badge(ev) {
    if (ev.is_manual_override)
      return '<span class="b manual">manual: ' + (ev.is_live_music ? 'live' : 'non-live') + '</span>';
    return '<span class="b ' + (ev.is_live_music ? 'live' : 'nonlive') + '">' +
           (ev.is_live_music ? 'live' : 'non-live') + '</span> ' +
           '<span class="reason">' + esc(ev.classification_reason || '') + '</span>';
  }

  function actions(ev) {
    let a = '';
    if (ev.is_manual_override) {
      a += '<button onclick="clearOv(' + ev.id + ')">clear override</button>';
    } else {
      a += '<button onclick="ov(' + ev.id + ',true)">mark live</button>';
      a += '<button onclick="ov(' + ev.id + ',false)">mark non-live</button>';
    }
    if (ev.series_override_id) {
      a += ' <span class="series">series: ' + (ev.series_override_is_live ? 'live' : 'non-live') +
           ' <button onclick="delSeries(' + ev.series_override_id + ')">remove</button></span>';
    } else {
      a += ' <button onclick="series(' + ev.id + ',true)">series live</button>';
      a += '<button onclick="series(' + ev.id + ',false)">series non-live</button>';
    }
    return a;
  }

  function render(evs, count) {
    $('#count').textContent = count + ' event(s)';
    $('#tbody').innerHTML = evs.length ? evs.map((ev) =>
      '<tr><td>' + ev.date + '</td>' +
      '<td>' + esc(ev.artist || ev.name) + '<div class="sub">' + esc(ev.venue_name || '') + '</div></td>' +
      '<td>' + badge(ev) + '</td>' +
      '<td class="actions">' + actions(ev) + '</td></tr>'
    ).join('') : '<tr><td colspan="4" class="empty">no events</td></tr>';
  }

  async function load() {
    const q = new URLSearchParams({ filter: state.filter, search: state.search });
    const data = await api('/admin/api/events?' + q.toString());
    render(data.events, data.count);
  }

  async function ov(id, isLive) {
    await api('/admin/api/events/' + id + '/override', { method: 'POST', body: JSON.stringify({ is_live_music: isLive }) });
    load();
  }
  async function clearOv(id) {
    await api('/admin/api/events/' + id + '/clear-override', { method: 'POST' });
    load();
  }
  async function series(eventId, isLive) {
    await api('/admin/api/series', { method: 'POST', body: JSON.stringify({ event_id: eventId, is_live_music: isLive }) });
    load();
  }
  async function delSeries(id) {
    await api('/admin/api/series/' + id, { method: 'DELETE' });
    load();
  }
  function setFilter(f) {
    state.filter = f;
    document.querySelectorAll('.fbtn').forEach((b) => b.classList.toggle('on', b.dataset.f === f));
    load();
  }
  async function showRules() {
    if (!RULES) RULES = await api('/admin/api/rules');
    $('#rules').textContent = JSON.stringify(RULES, null, 2);
    $('#rules').classList.toggle('hidden');
  }

  let t;
  document.addEventListener('DOMContentLoaded', () => {
    $('#search').addEventListener('input', (e) => {
      clearTimeout(t);
      t = setTimeout(() => { state.search = e.target.value.trim(); load(); }, 300);
    });
    load();
  });
</script>
</body></html>
"""
