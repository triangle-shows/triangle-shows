"""
Static HTML/JS for the show-proposal form, served by app.api.submissions.

Role: One self-contained page at /new-shows-form. Kept out of the public `frontend/`
static mount for the same reason the admin pages are: the route that serves it is the
one the origin gate guards, and a copy under the static mount would be reachable by
anyone. All data comes from /new-shows-form/api/* via fetch. Plain triple-quoted string
(NOT an f-string) so JS braces are literal; no backslash escapes, for the same reason.

Styled after the public site's default Amber palette rather than the admin's green:
the people using this are contributors to the calendar, not its moderators, and the
page should read as part of the site they already know.
"""

SUBMIT_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>propose a show · triangle-shows</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
  /* The public site's Amber palette (frontend/js/config.js). */
  :root { color-scheme: dark;
          --bg:#1a1008; --surface:#241609; --surface2:#2e1d0c; --border:#3d2a12;
          --text:#e8d5b0; --muted:#9a7a50; --dim:#5a4020;
          --accent:#c87941; --accent-hover:#e09050; --accent-bg:rgba(200,121,65,0.10);
          --ok:#8fc98a; --bad:#e08a6a; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font-family:'Space Mono',ui-monospace,monospace; font-size:0.85rem; line-height:1.45; }
  a { color:var(--accent); }
  a:hover { color:var(--accent-hover); }
  header { display:flex; align-items:baseline; flex-wrap:wrap; gap:0.4rem 1rem;
           padding:0.9rem 16px; border-bottom:1px solid var(--border); }
  header .home { color:var(--accent); font-weight:700; text-decoration:none;
                 letter-spacing:0.06em; text-transform:uppercase; font-size:0.9rem; }
  header .sp { flex:1; }
  header .who { color:var(--muted); font-size:0.72rem; overflow-wrap:anywhere; }
  main { max-width:44rem; margin:0 auto; padding:1.2rem 16px 3rem; }
  h1 { font-size:1rem; letter-spacing:0.08em; text-transform:uppercase; color:var(--accent); margin:0 0 0.4rem; }
  h2 { font-size:0.78rem; letter-spacing:0.08em; text-transform:uppercase; color:var(--accent);
       margin:2rem 0 0.6rem; }
  .hint { color:var(--muted); font-size:0.78rem; margin:0 0 1.1rem; }
  form { background:var(--surface); border:1px solid var(--border); border-radius:2px; padding:1rem; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(14rem, 1fr)); gap:0.7rem 0.9rem; }
  .f { display:flex; flex-direction:column; gap:0.25rem; min-width:0; }
  .f.wide { grid-column:1 / -1; }
  .f label { color:var(--muted); font-size:0.68rem; text-transform:uppercase; letter-spacing:0.06em; }
  .f label .req { color:var(--accent); }
  .f input, .f select, .f textarea {
    font-family:inherit; font-size:0.85rem; padding:0.45rem 0.55rem; border-radius:2px; width:100%;
    background:var(--bg); border:1px solid var(--border); color:var(--text); }
  .f input:focus, .f select:focus, .f textarea:focus { outline:none; border-color:var(--accent); }
  .f textarea { resize:vertical; min-height:4rem; }
  .f .note { color:var(--dim); font-size:0.68rem; }
  .f.check { flex-direction:row; align-items:flex-start; gap:0.5rem; }
  .f.check input { width:auto; margin-top:0.2rem; }
  .f.check label { text-transform:none; letter-spacing:0; font-size:0.78rem; color:var(--text); }
  /* Revealed by the venue picker. The id rule sets display, so .hidden needs an id rule
     of its own to win (the same trap the admin's #newVenue documents). */
  #newVenue { grid-column:1 / -1; border-left:2px solid var(--accent); padding-left:0.8rem;
              display:grid; grid-template-columns:repeat(auto-fit, minmax(13rem, 1fr)); gap:0.7rem 0.9rem; }
  #newVenue.hidden { display:none; }
  .actions { margin-top:1.1rem; display:flex; align-items:center; gap:0.8rem; flex-wrap:wrap; }
  button.go { font-family:inherit; font-size:0.78rem; text-transform:uppercase; letter-spacing:0.06em;
              padding:0.5rem 1rem; border:1px solid var(--accent); background:var(--accent);
              color:var(--bg); font-weight:700; cursor:pointer; border-radius:2px; }
  button.go:hover { background:var(--accent-hover); border-color:var(--accent-hover); }
  button.go:disabled { opacity:0.5; cursor:default; }
  #err { color:var(--bad); font-size:0.78rem; min-height:1em; margin:0.8rem 0 0; }
  #ok { color:var(--ok); font-size:0.78rem; min-height:1em; margin:0.8rem 0 0; }
  .hidden { display:none; }
  .srow { display:flex; align-items:baseline; gap:0.4rem 0.8rem; flex-wrap:wrap; padding:0.5rem 0;
          border-bottom:1px solid var(--border); }
  .srow:last-child { border-bottom:none; }
  .srow .sn { flex:1; min-width:10rem; overflow-wrap:anywhere; }
  .srow .sm { color:var(--muted); font-size:0.72rem; }
  .b { display:inline-block; padding:0.05rem 0.45rem; border-radius:2px; font-size:0.66rem;
       font-weight:700; text-transform:uppercase; letter-spacing:0.04em; }
  .b.pending { background:var(--accent-bg); color:var(--accent); }
  .b.approved { background:rgba(143,201,138,0.12); color:var(--ok); }
  .b.rejected { background:rgba(224,138,106,0.12); color:var(--bad); }
  .empty { color:var(--muted); font-size:0.78rem; }
</style>
</head><body>
  <header>
    <a class="home" href="/">triangle-shows</a>
    <span class="sp"></span>
    <span class="who" id="who"></span>
    <a href="/cdn-cgi/access/logout" style="font-size:0.72rem">sign out</a>
  </header>
  <main>
    <h1>Propose a show</h1>
    <p class="hint">Know about a show that isn't on the calendar? Send it in. Someone looks
    over every submission before it goes up, so it won't appear straight away. You can see
    what happened to yours at the bottom of this page.</p>

    <form id="form">
      <div class="grid">
        <div class="f wide">
          <label for="venue">venue <span class="req">*</span></label>
          <select id="venue" required></select>
        </div>

        <div id="newVenue" class="hidden">
          <div class="f">
            <label for="nvName">venue name <span class="req">*</span></label>
            <input id="nvName" type="text" maxlength="200">
          </div>
          <div class="f">
            <label for="nvCity">city <span class="req">*</span></label>
            <input id="nvCity" type="text" list="cityList" maxlength="50" placeholder="Durham">
            <datalist id="cityList"></datalist>
          </div>
          <div class="f wide">
            <label for="nvWebsite">venue website</label>
            <input id="nvWebsite" type="url" maxlength="500" placeholder="https://...">
          </div>
        </div>

        <div class="f wide">
          <label for="name">show name <span class="req">*</span></label>
          <input id="name" type="text" required maxlength="500" placeholder="what the listing should say">
        </div>
        <div class="f">
          <label for="artist">headliner</label>
          <input id="artist" type="text" maxlength="300">
        </div>
        <div class="f">
          <label for="support">support</label>
          <input id="support" type="text">
        </div>
        <div class="f">
          <label for="date">date <span class="req">*</span></label>
          <input id="date" type="date" required>
        </div>
        <div class="f">
          <label for="show">show time</label>
          <input id="show" type="time">
        </div>
        <div class="f">
          <label for="doors">doors</label>
          <input id="doors" type="time">
        </div>
        <div class="f">
          <label for="age">ages</label>
          <input id="age" type="text" maxlength="50" placeholder="e.g. 18+">
        </div>
        <div class="f">
          <label for="priceMin">price from ($)</label>
          <input id="priceMin" type="number" min="0" step="any" placeholder="0 if free">
        </div>
        <div class="f">
          <label for="priceMax">price to ($)</label>
          <input id="priceMax" type="number" min="0" step="any">
        </div>
        <div class="f wide">
          <label for="url">ticket or info link</label>
          <input id="url" type="url" maxlength="1000" placeholder="https://...">
        </div>
        <div class="f wide">
          <label for="desc">description</label>
          <textarea id="desc" maxlength="5000"></textarea>
        </div>
        <div class="f">
          <label for="genre">genre</label>
          <input id="genre" type="text" maxlength="100">
        </div>
        <div class="f check wide">
          <input id="live" type="checkbox" checked>
          <label for="live">live music. Untick for comedy, trivia, a DJ or club night, and the like.</label>
        </div>
        <div class="f wide">
          <label for="note">anything else we should know</label>
          <textarea id="note" maxlength="2000" placeholder="where you heard about it, a correction, ..."></textarea>
          <span class="note">only seen by whoever reviews it</span>
        </div>
      </div>
      <div class="actions">
        <button type="submit" class="go" id="submit">send it in</button>
      </div>
      <p id="err" role="alert"></p>
      <p id="ok" role="status"></p>
    </form>

    <h2>Your submissions</h2>
    <div id="mine"><p class="empty">loading…</p></div>
  </main>
<script>
  const $ = (s) => document.querySelector(s);
  const NEW_VENUE = '__new__';

  async function api(path, opts) {
    const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
    if (!r.ok) {
      let detail = null;
      try { detail = (await r.json()).detail; } catch (_) {}
      // A 422 from FastAPI carries a list of field errors rather than a sentence.
      if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join('; ');
      if (r.status === 403 && !detail) detail = 'You are signed out. Reload the page to sign in again.';
      throw new Error(detail || ('Something went wrong (' + r.status + ').'));
    }
    return r.json();
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
      ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));
  }

  function value(id) {
    const v = ($(id).value || '').trim();
    return v === '' ? null : v;
  }

  function number(id) {
    const v = value(id);
    return v === null ? null : Number(v);
  }

  function venueChanged() {
    const isNew = $('#venue').value === NEW_VENUE;
    $('#newVenue').classList.toggle('hidden', !isNew);
    // Toggled rather than fixed in the markup: a hidden required field blocks submit
    // with a browser message pointing at something nobody can see.
    ['#nvName', '#nvCity'].forEach((id) => { $(id).required = isNew; });
  }

  async function loadVenues() {
    const data = await api('/new-shows-form/api/venues');
    // Grouped by city, the way the public site's sidebar groups them.
    const byCity = {};
    data.venues.forEach((v) => { (byCity[v.city] = byCity[v.city] || []).push(v); });
    let html = '<option value="" disabled selected>choose…</option>';
    Object.keys(byCity).sort().forEach((city) => {
      html += '<optgroup label="' + esc(city) + '">' +
        byCity[city].map((v) => '<option value="' + v.id + '">' + esc(v.name) + '</option>').join('') +
        '</optgroup>';
    });
    html += '<option value="' + NEW_VENUE + '">＋ it isn&#39;t listed…</option>';
    $('#venue').innerHTML = html;
    $('#cityList').innerHTML = data.cities.map((c) => '<option value="' + esc(c) + '"></option>').join('');
    venueChanged();
  }

  async function loadMine() {
    const data = await api('/new-shows-form/api/submissions');
    $('#who').textContent = 'signed in as ' + data.you;
    if (!data.submissions.length) {
      $('#mine').innerHTML = '<p class="empty">Nothing yet. What you send in will show up here.</p>';
      return;
    }
    $('#mine').innerHTML = data.submissions.map((s) =>
      '<div class="srow">' +
        '<span class="b ' + esc(s.status) + '">' + esc(s.status) + '</span>' +
        '<span class="sn">' + esc(s.name) + '</span>' +
        '<span class="sm">' + esc(s.date) + '</span>' +
        '<span class="sm">' + esc(s.venue || '') + '</span>' +
      '</div>').join('');
  }

  async function submit(e) {
    e.preventDefault();
    $('#err').textContent = '';
    $('#ok').textContent = '';
    const btn = $('#submit');
    btn.disabled = true;
    try {
      const isNew = $('#venue').value === NEW_VENUE;
      const created = await api('/new-shows-form/api/submissions', {
        method: 'POST',
        body: JSON.stringify({
          venue_id: isNew ? null : Number($('#venue').value),
          new_venue_name: isNew ? value('#nvName') : null,
          new_venue_city: isNew ? value('#nvCity') : null,
          new_venue_website: isNew ? value('#nvWebsite') : null,
          name: value('#name'),
          date: value('#date'),
          artist: value('#artist'),
          support_artists: value('#support'),
          show_time: value('#show'),
          doors_time: value('#doors'),
          age_restriction: value('#age'),
          price_min: number('#priceMin'),
          price_max: number('#priceMax'),
          ticket_url: value('#url'),
          description: value('#desc'),
          genre: value('#genre'),
          is_live_music: $('#live').checked,
          note: value('#note'),
        }),
      });
      // Venue and date are kept: several acts on one bill, or a run of nights at one
      // room, is the common case for sending more than one.
      ['#name', '#artist', '#support', '#show', '#doors', '#age', '#priceMin', '#priceMax',
       '#url', '#desc', '#genre', '#note'].forEach((id) => { $(id).value = ''; });
      $('#live').checked = true;
      $('#ok').textContent = 'Thanks! "' + created.name + '" is waiting for review. ' +
                             'The venue and date are still filled in if you have another.';
      await loadMine();
      $('#name').focus();
    } catch (err) {
      $('#err').textContent = err.message;
    } finally {
      btn.disabled = false;
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    $('#venue').addEventListener('change', venueChanged);
    $('#form').addEventListener('submit', submit);
    loadVenues().catch((err) => { $('#err').textContent = err.message; });
    loadMine().catch((err) => { $('#mine').innerHTML = '<p class="empty">' + esc(err.message) + '</p>'; });
  });
</script>
</body></html>
"""
