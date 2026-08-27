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
  /* Deliberately green, not the public site's amber (--accent in frontend/css/styles.css),
     so the admin surface is visually distinct from the live site at a glance. */
  :root { color-scheme: dark; --accent:#4fae7a; }
  body { margin:0; min-height:100vh; display:grid; place-items:center;
         background:#0e0e10; color:#e8e6e3; font-family:'Space Mono',ui-monospace,monospace; }
  .box { width:min(90vw,340px); border:1px solid #2a2a2e; padding:1.6rem; border-radius:2px; }
  h1 { font-size:0.95rem; letter-spacing:0.08em; text-transform:uppercase; color:var(--accent); margin:0 0 1.2rem; }
  input, button { width:100%; font-family:inherit; font-size:0.85rem; padding:0.55rem 0.6rem;
                  box-sizing:border-box; border-radius:2px; }
  input { background:#17171a; border:1px solid #2a2a2e; color:#e8e6e3; margin-bottom:0.7rem; }
  input:focus { outline:none; border-color:var(--accent); }
  button { background:var(--accent); border:1px solid var(--accent); color:#0e0e10; cursor:pointer; font-weight:700; }
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
  /* Deliberately green, not the public site's amber (--accent in frontend/css/styles.css),
     so the admin surface is visually distinct from the live site at a glance. */
  :root { color-scheme: dark; --accent:#4fae7a; }
  * { box-sizing:border-box; }
  body { margin:0; background:#0e0e10; color:#e8e6e3;
         font-family:'Space Mono',ui-monospace,monospace; font-size:0.82rem; }
  header { display:flex; align-items:center; gap:1rem; padding:0.8rem 1.1rem;
           border-bottom:1px solid #2a2a2e; position:sticky; top:0; background:#0e0e10; z-index:2; }
  header h1 { font-size:0.9rem; letter-spacing:0.08em; text-transform:uppercase; color:var(--accent); margin:0; }
  header .sp { flex:1; }
  a { color:var(--accent); }
  main { padding:1.1rem; max-width:1100px; margin:0 auto; }
  .controls { display:flex; flex-wrap:wrap; gap:0.5rem; align-items:center; margin-bottom:1rem; }
  .fbtn, button.link { font-family:inherit; font-size:0.72rem; text-transform:uppercase; letter-spacing:0.06em;
          padding:0.3rem 0.6rem; border:1px solid #3a3a3e; background:transparent; color:#a8a6a3;
          cursor:pointer; border-radius:2px; }
  .fbtn.on, button.link.on { background:var(--accent); border-color:var(--accent); color:#0e0e10; font-weight:700; }
  button.link.on::after { content:' ×'; }
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
  .b.mixed { background:#2a2a3d; color:#9a9ad6; }
  .reason { color:#8a8a8e; font-size:0.7rem; }
  tr.grow { cursor:pointer; }
  tr.grow:hover > td { background:#17171a; }
  tr.grow td:first-child { color:#c8c6c3; }
  .caret { display:inline-block; width:1em; color:var(--accent); }
  .gcount { color:#8a8a8e; font-size:0.72rem; }
  .divider { width:1px; align-self:stretch; background:#2a2a2e; margin:0 0.2rem; }
  .warn { color:#e0b45f; }
  tr.erow { cursor:pointer; }
  tr.erow:hover > td { background:#17171a; }
  tr.eopen > td { background:#17171a; }
  tr.detail > td { background:#141417; border-bottom:1px solid #1e1e22; padding-top:0.4rem; }
  tr.detail .dk { color:#8a8a8e; text-transform:uppercase; font-size:0.64rem; letter-spacing:0.06em; margin-right:0.4rem; }
  tr.detail .desc { color:#c8c6c3; margin:0.35rem 0; max-width:60ch; line-height:1.45; }
  tr.detail div { margin:0.12rem 0; }
  td.actions button.ok { border-color:#2f5c40; color:#7fd69a; }
  #help { background:#17171a; border:1px solid #2a2a2e; border-radius:2px;
          padding:0.4rem 1rem 1rem; margin-top:1rem; max-width:76ch; }
  #help h2 { font-size:0.72rem; text-transform:uppercase; letter-spacing:0.08em;
             color:var(--accent); margin:1.1rem 0 0.3rem; }
  #help p { color:#b8b6b3; line-height:1.55; margin:0.4rem 0; }
  #help b { color:#e8e6e3; }
  tr.child > td { border-bottom:1px solid #1a1a1e; }
  tr.child td:first-child { padding-left:1.6rem; color:#8a8a8e; }
  tr.child > td:first-child { border-left:2px solid #2a2a2e; }
  td.actions { white-space:nowrap; }
  td.actions button { font-family:inherit; font-size:0.68rem; margin:0.1rem 0.15rem 0.1rem 0; padding:0.22rem 0.45rem;
          border:1px solid #3a3a3e; background:transparent; color:#c8c6c3; cursor:pointer; border-radius:2px; }
  td.actions button:hover { border-color:var(--accent); color:var(--accent); }
  .series { color:#8a8a8e; }
  .empty { color:#8a8a8e; text-align:center; padding:1.5rem; }
  #rules { white-space:pre-wrap; background:#17171a; border:1px solid #2a2a2e; padding:0.8rem;
           border-radius:2px; font-size:0.72rem; margin-top:1rem; color:#b8b6b3; }
  .hidden { display:none; }
</style>
</head><body>
  <header>
    <h1>triangle-shows admin</h1>
    <button class="link" id="rulesBtn" aria-expanded="false" onclick="showRules()">detection rules</button>
    <button class="link" id="helpBtn" aria-expanded="false" onclick="showHelp()">how this works</button>
    <span class="sp"></span>
    <a href="/admin/logout">log out</a>
  </header>
  <main>
    <div class="controls">
      <button class="fbtn on" data-f="non_live" onclick="setFilter('non_live')">non-live</button>
      <button class="fbtn" data-f="live" onclick="setFilter('live')">live</button>
      <button class="fbtn" data-f="all" onclick="setFilter('all')">all</button>
      <span class="divider"></span>
      <button class="fbtn tog" id="futureBtn" aria-pressed="false" onclick="toggleFuture()">future dates only</button>
      <button class="fbtn tog" id="approvedBtn" aria-pressed="false" onclick="toggleApproved()">show approved</button>
      <input id="search" placeholder="search name / artist...">
      <span id="count"></span>
    </div>
    <pre id="rules" class="hidden"></pre>
    <div id="help" class="hidden">
      <h2>What this page is for</h2>
      <p>Every event is flagged <b>live music</b> or <b>non-live</b>. The public calendar
      uses that flag to let visitors hide karaoke, trivia, DJ nights and the like. This page
      is where wrong guesses get corrected.</p>
      <p>The list is a <b>review queue</b>: it opens on non-live events, because those are
      the ones the detector had to make a judgement call about. Click any row to see its
      description, genre and a link to the venue page — often the only way to settle an
      ambiguous one.</p>

      <h2>The two kinds of correction</h2>
      <p><b>mark live / mark non-live</b> fixes one event. It sets a manual override, which
      survives re-scrapes and reclassification — the detector will never overwrite it.</p>
      <p><b>series live / series non-live</b> creates a standing rule for a repeating event
      at one venue, matched on its name. It applies to every date in the series <i>and to
      future instances that haven't been scraped yet</i>. The number on the button is how
      many events it currently affects. Use this for anything recurring: one rule beats
      twenty corrections.</p>

      <h2>Approving</h2>
      <p><b>approve</b> means "I checked this and the flag is right". It doesn't change the
      flag — it just removes the event from the queue so you can tell reviewed work from
      work you haven't looked at yet. On a collapsed series row it approves every date at
      once.</p>
      <p>An approval applies to <i>that verdict</i>, not the event forever. If something
      later changes the flag — a new series rule, or the detector spotting a recurrence it
      had missed — the approval is dropped and the event comes back to the queue. Marking an
      event live or non-live by hand also approves it, since you've just made the call.</p>

      <h2>Reading the list</h2>
      <p>Repeating events collapse into one row showing a date range and a count; click to
      expand. A collapsed row sits at the series' <i>first</i> date, so the dates in the
      left column aren't a straight timeline — a row near the top may run for months.</p>
      <p><b>mixed</b> means some dates in a series are flagged differently from others,
      usually because one was overridden by hand.</p>
      <p>The list covers the last 30 days as well as everything upcoming, because the
      calendar can be scrolled back. <b>future dates only</b> narrows it; <b>show approved</b>
      brings reviewed events back into view. Neither changes what a series action affects —
      that always reaches every matching event, shown or not.</p>

      <h2>Where the guesses come from</h2>
      <p><b>detection rules</b> lists the automatic criteria: keyword matches, and how many
      repeats at one venue count as a recurring series. Anything auto-flagged shows its
      reason next to the badge.</p>
    </div>
    <table>
      <thead><tr><th>date</th><th>event</th><th>status</th><th>actions</th></tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </main>
<script>
  let RULES = null;
  const $ = (s) => document.querySelector(s);
  const state = { filter: 'non_live', search: '', futureOnly: false, showApproved: false };

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

  function approveAction(ev, isSeries, n) {
    const suffix = (isSeries && n > 1) ? ' (' + n + ')' : '';
    if (ev.is_approved && !isSeries) {
      return '<button class="ok" onclick="unapprove(' + ev.id + ',false)">approved ✓</button>';
    }
    const fn = isSeries ? 'approve(' + ev.id + ',true)' : 'approve(' + ev.id + ',false)';
    return '<button onclick="' + fn + '">approve' + suffix + '</button>';
  }

  function actions(ev) {
    let a = approveAction(ev, false, 1) + ' ';
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

  // Grouping. Events sharing a venue and series_key collapse into one expandable
  // row. series_key comes from the server's normalize_series_name, the same key a
  // series override matches on — so what you see grouped is exactly what a series
  // action will affect. GROUPS is rebuilt on every load; `expanded` is keyed by
  // series so open rows survive the re-render that follows an action.
  let GROUPS = [];
  const expanded = new Set();

  function groupKeyOf(ev) {
    // A blank series_key would otherwise collapse unrelated events into one row.
    if (!ev.series_key) return 'ev||' + ev.id;
    return 'series||' + (ev.venue_slug || '') + '||' + ev.series_key;
  }

  function buildGroups(evs) {
    const map = new Map();
    evs.forEach((ev) => {
      const k = groupKeyOf(ev);
      if (!map.has(k)) map.set(k, []);
      map.get(k).push(ev);
    });
    return Array.from(map.entries());
  }

  function groupBadge(evs) {
    const live = evs.filter((e) => e.is_live_music).length;
    if (live === evs.length) return '<span class="b live">live</span>';
    if (live === 0) return '<span class="b nonlive">non-live</span>';
    return '<span class="b mixed">mixed</span> <span class="reason">' +
           live + ' live / ' + (evs.length - live) + ' non-live</span>';
  }

  function seriesActions(ev) {
    if (ev.series_override_id) {
      return '<span class="series">series: ' + (ev.series_override_is_live ? 'live' : 'non-live') +
             ' <button onclick="delSeries(' + ev.series_override_id + ')">remove</button></span>';
    }
    // Put the blast radius on the button. series_size counts every matching event in
    // the reclassify range, not just the rows on screen, so a collapsed or filtered
    // view can't make the action look smaller than it is. Omitted at 1, where there
    // is no series to speak of.
    const n = ev.series_size > 1 ? ' (' + ev.series_size + ')' : '';
    const t = ev.series_size > 1
      ? ' title="applies to ' + ev.series_size + ' events at this venue, including ones not shown"'
      : '';
    return '<button' + t + ' onclick="series(' + ev.id + ',true)">series live' + n + '</button>' +
           '<button' + t + ' onclick="series(' + ev.id + ',false)">series non-live' + n + '</button>';
  }

  // Event ids whose detail panel is open. Like `expanded`, this is keyed by id so
  // open panels survive the re-render that follows every action.
  const detailOpen = new Set();

  function toggleDetail(id) {
    if (detailOpen.has(id)) detailOpen.delete(id); else detailOpen.add(id);
    renderGroups();
  }

  function detailRow(ev) {
    let bits = '';
    if (ev.genre) bits += '<div><span class="dk">genre</span> ' + esc(ev.genre) + '</div>';
    if (ev.age_restriction) bits += '<div><span class="dk">ages</span> ' + esc(ev.age_restriction) + '</div>';
    if (ev.artist && ev.artist !== ev.name) bits += '<div><span class="dk">listed as</span> ' + esc(ev.name) + '</div>';
    if (ev.description) bits += '<div class="desc">' + esc(ev.description) + '</div>';
    if (ev.ticket_url) {
      bits += '<div><a href="' + esc(ev.ticket_url) + '" target="_blank" rel="noopener noreferrer">open venue page</a></div>';
    }
    // Most events carry no description at all, so say so rather than opening an
    // empty panel that looks broken.
    if (!bits) bits = '<div class="dk">no description, genre, or link on this event</div>';
    return '<tr class="detail"><td></td><td colspan="3">' + bits + '</td></tr>';
  }

  function eventRow(ev, cls) {
    const open = detailOpen.has(ev.id);
    return '<tr class="' + cls + ' erow' + (open ? ' eopen' : '') + '" onclick="toggleDetail(' + ev.id + ')">' +
      '<td>' + ev.date + '</td>' +
      '<td>' + esc(ev.artist || ev.name) + '<div class="sub">' + esc(ev.venue_name || '') + '</div></td>' +
      '<td>' + badge(ev) + '</td>' +
      '<td class="actions" onclick="event.stopPropagation()">' + actions(ev) + '</td></tr>' +
      (open ? detailRow(ev) : '');
  }

  function groupRow(i, evs) {
    const first = evs[0], last = evs[evs.length - 1];
    const open = expanded.has(GROUPS[i][0]);
    const dates = first.date === last.date ? first.date : first.date + ' - ' + last.date;
    return '<tr class="grow" onclick="toggleGroup(' + i + ')">' +
      '<td><span class="caret">' + (open ? 'v' : '>') + '</span>' + dates + '</td>' +
      '<td>' + esc(first.name) + ' <span class="gcount">(' + evs.length + ' dates)</span>' +
        '<div class="sub">' + esc(first.venue_name || '') + '</div></td>' +
      '<td>' + groupBadge(evs) + '</td>' +
      '<td class="actions" onclick="event.stopPropagation()">' +
        approveAction(first, true, first.series_size) + ' ' + seriesActions(first) +
      '</td></tr>';
  }

  function renderGroups() {
    if (!GROUPS.length) {
      $('#tbody').innerHTML = '<tr><td colspan="4" class="empty">no events</td></tr>';
      return;
    }
    let html = '';
    GROUPS.forEach((entry, i) => {
      const evs = entry[1];
      // A one-off event has nothing to collapse, so render it as a plain row.
      if (evs.length === 1) { html += eventRow(evs[0], ''); return; }
      html += groupRow(i, evs);
      if (expanded.has(entry[0])) {
        evs.forEach((ev) => { html += eventRow(ev, 'child'); });
      }
    });
    $('#tbody').innerHTML = html;
  }

  function toggleGroup(i) {
    const k = GROUPS[i][0];
    if (expanded.has(k)) expanded.delete(k); else expanded.add(k);
    renderGroups();
  }

  function render(evs, count, total, approvedHidden) {
    GROUPS = buildGroups(evs);
    const seriesCount = GROUPS.filter((g) => g[1].length > 1).length;
    const hidden = approvedHidden ? ' · ' + approvedHidden + ' approved hidden' : '';
    // Say when the view is partial. A series action reaches every matching event,
    // including ones cut off by the row cap or hidden by future-only, so a silent
    // count would understate what a series button affects.
    const truncated = total > count;
    $('#count').innerHTML =
      (truncated ? '<span class="warn">' + count + ' of ' + total + ' shown</span>'
                 : count + ' event(s)') +
      (seriesCount ? ', ' + seriesCount + ' series' : '') +
      (state.futureOnly ? ' · past dates hidden' : '') + hidden;
    renderGroups();
  }

  async function load() {
    const q = new URLSearchParams({
      filter: state.filter,
      search: state.search,
      future_only: state.futureOnly ? 'true' : 'false',
      show_approved: state.showApproved ? 'true' : 'false',
    });
    const data = await api('/admin/api/events?' + q.toString());
    render(data.events, data.count, data.total, data.approved_hidden);
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
    // Only the data-f buttons are a radio group; the future-only toggle is
    // independent and must not be cleared when the classification filter changes.
    document.querySelectorAll('.fbtn[data-f]').forEach((b) => b.classList.toggle('on', b.dataset.f === f));
    load();
  }

  function toggleFuture() {
    state.futureOnly = !state.futureOnly;
    $('#futureBtn').classList.toggle('on', state.futureOnly);
    $('#futureBtn').setAttribute('aria-pressed', state.futureOnly ? 'true' : 'false');
    load();
  }

  function toggleApproved() {
    state.showApproved = !state.showApproved;
    $('#approvedBtn').classList.toggle('on', state.showApproved);
    $('#approvedBtn').setAttribute('aria-pressed', state.showApproved ? 'true' : 'false');
    load();
  }

  async function approve(id, isSeries) {
    await api('/admin/api/events/' + id + '/approve?series=' + (isSeries ? 'true' : 'false'), { method: 'POST' });
    load();
  }
  async function unapprove(id, isSeries) {
    await api('/admin/api/events/' + id + '/unapprove?series=' + (isSeries ? 'true' : 'false'), { method: 'POST' });
    load();
  }
  // Panel toggles. Reflect open/closed on the button itself, so each reads as a
  // toggle rather than a one-way action and it's obvious how to dismiss it. The two
  // panels are mutually exclusive — opening one closes the other.
  function setPanel(panelId, btnId, shown) {
    $(panelId).classList.toggle('hidden', !shown);
    $(btnId).classList.toggle('on', shown);
    $(btnId).setAttribute('aria-expanded', shown ? 'true' : 'false');
  }

  async function showRules() {
    if (!RULES) RULES = await api('/admin/api/rules');
    $('#rules').textContent = JSON.stringify(RULES, null, 2);
    const shown = $('#rules').classList.contains('hidden');
    setPanel('#rules', '#rulesBtn', shown);
    if (shown) setPanel('#help', '#helpBtn', false);
  }

  function showHelp() {
    const shown = $('#help').classList.contains('hidden');
    setPanel('#help', '#helpBtn', shown);
    if (shown) setPanel('#rules', '#rulesBtn', false);
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
