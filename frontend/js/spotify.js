// Spotify Taste Matching
// Client-side only — Implicit Grant OAuth, localStorage caching, no backend.
//
// SETUP (one-time):
//   1. Go to https://developer.spotify.com/dashboard and create an app.
//   2. Under "Redirect URIs", add:
//        https://triangle-shows.net
//        http://localhost:8000
//   3. Paste your Client ID into config.js as SPOTIFY_CLIENT_ID.

const SPOTIFY_SCOPES   = "user-top-read user-follow-read user-library-read";
const SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize";
const SPOTIFY_API      = "https://api.spotify.com/v1";

const LS_TOKEN   = "sp-token";
const LS_EXPIRY  = "sp-expiry";
const LS_ARTISTS = "sp-artists";

// ── Normalization + matching ───────────────────────────────────────────────

function _norm(s) {
  return (s || "").toLowerCase().replace(/[^\w\s]/g, " ").replace(/\s+/g, " ").trim();
}

// Returns true if any cached Spotify artist matches this event.
// Checks substring in both directions — catches "Waxahatchee & S. Carey" ↔ "Waxahatchee",
// "An Evening with Waxahatchee" ↔ "Waxahatchee", etc.
function eventMatchesSpotify(eventTitle, eventArtist) {
  const artists = getSpotifyArtists();
  if (!artists.length) return false;
  const hay = _norm((eventTitle || "") + " " + (eventArtist || ""));
  const normTitle = _norm(eventTitle || "");
  return artists.some(a => hay.includes(a) || (a.length > 3 && a.includes(normTitle)));
}

// ── Auth state ─────────────────────────────────────────────────────────────

function isSpotifyConnected() {
  const token  = localStorage.getItem(LS_TOKEN);
  const expiry = localStorage.getItem(LS_EXPIRY);
  return !!(token && expiry && Date.now() < parseInt(expiry, 10));
}

function getSpotifyArtists() {
  if (!isSpotifyConnected()) return [];
  try {
    return JSON.parse(localStorage.getItem(LS_ARTISTS) || "[]");
  } catch {
    return [];
  }
}

function connectSpotify() {
  if (typeof SPOTIFY_CLIENT_ID === "undefined" || !SPOTIFY_CLIENT_ID) {
    alert("Spotify Client ID not configured. See spotify.js for setup instructions.");
    return;
  }
  const params = new URLSearchParams({
    client_id:     SPOTIFY_CLIENT_ID,
    response_type: "token",
    redirect_uri:  window.location.origin,
    scope:         SPOTIFY_SCOPES,
    show_dialog:   "false",
  });
  window.location.href = `${SPOTIFY_AUTH_URL}?${params}`;
}

function _clearSpotify() {
  localStorage.removeItem(LS_TOKEN);
  localStorage.removeItem(LS_EXPIRY);
  localStorage.removeItem(LS_ARTISTS);
}

// ── OAuth callback ─────────────────────────────────────────────────────────

async function handleSpotifyCallback() {
  const hash = window.location.hash;
  if (!hash.includes("access_token")) return;

  const params    = new URLSearchParams(hash.slice(1));
  const token     = params.get("access_token");
  const expiresIn = parseInt(params.get("expires_in") || "3600", 10);
  if (!token) return;

  // Remove the token hash from the URL without reloading
  history.replaceState(null, "", window.location.pathname + window.location.search);

  localStorage.setItem(LS_TOKEN,  token);
  localStorage.setItem(LS_EXPIRY, String(Date.now() + expiresIn * 1000));

  _updateSpotifyUI("loading");
  await fetchAllArtists(token);
  _updateSpotifyUI("connected");
  _onSpotifyReady();
}

// ── Spotify API helpers ────────────────────────────────────────────────────

async function _spotifyGet(token, url) {
  const resp = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
  if (resp.status === 401) { _clearSpotify(); return null; }
  if (!resp.ok) return null;
  return resp.json();
}

async function _fetchTopArtists(token) {
  const ranges = ["short_term", "medium_term", "long_term"];
  const results = await Promise.all(
    ranges.map(r => _spotifyGet(token, `${SPOTIFY_API}/me/top/artists?limit=50&time_range=${r}`))
  );
  const names = [];
  for (const r of results) {
    if (r && r.items) r.items.forEach(a => names.push(a.name));
  }
  return names;
}

async function _fetchFollowedArtists(token) {
  const names = [];
  let url = `${SPOTIFY_API}/me/following?type=artist&limit=50`;
  while (url) {
    const data = await _spotifyGet(token, url);
    if (!data || !data.artists) break;
    data.artists.items.forEach(a => names.push(a.name));
    url = data.artists.next; // null when no more pages
  }
  return names;
}

async function _fetchLikedTrackArtists(token) {
  const names = [];
  for (let i = 0; i < 5; i++) {
    const data = await _spotifyGet(token, `${SPOTIFY_API}/me/tracks?limit=50&offset=${i * 50}`);
    if (!data || !data.items || data.items.length === 0) break;
    data.items.forEach(item => {
      if (item.track && item.track.artists) {
        item.track.artists.forEach(a => names.push(a.name));
      }
    });
  }
  return names;
}

// Fetches all artist sources in parallel, merges + deduplicates, caches.
async function fetchAllArtists(token) {
  const [top, followed, liked] = await Promise.all([
    _fetchTopArtists(token),
    _fetchFollowedArtists(token),
    _fetchLikedTrackArtists(token),
  ]);

  const seen = new Set();
  const normalized = [];
  for (const name of [...top, ...followed, ...liked]) {
    const n = _norm(name);
    if (n && n.length > 1 && !seen.has(n)) {
      seen.add(n);
      normalized.push(n);
    }
  }

  localStorage.setItem(LS_ARTISTS, JSON.stringify(normalized));
  return normalized;
}

// ── UI helpers ─────────────────────────────────────────────────────────────

function _updateSpotifyUI(state) {
  const link = document.getElementById("spotify-connect-link");
  if (!link) return;
  if (state === "loading") {
    link.textContent = "♫ fetching artists…";
    link.style.pointerEvents = "none";
    link.style.opacity = "0.5";
  } else if (state === "connected") {
    const count = getSpotifyArtists().length;
    link.textContent = `♫ ${count} artists · refresh`;
    link.style.pointerEvents = "";
    link.style.opacity = "";
    link.onclick = function (e) { e.preventDefault(); _refreshSpotify(); };
  }
}

async function _refreshSpotify() {
  const token = localStorage.getItem(LS_TOKEN);
  if (!token || !isSpotifyConnected()) { connectSpotify(); return; }
  _updateSpotifyUI("loading");
  await fetchAllArtists(token);
  _updateSpotifyUI("connected");
  _onSpotifyReady();
}

// Called once artists are cached — surfaces the ★ for you chip and re-filters.
function _onSpotifyReady() {
  _renderForYouChip();
  if (typeof applyAllFilters === "function") applyAllFilters();
}

function _renderForYouChip() {
  // #quick-filters is the row for chips that aren't a city. It used to be
  // #city-filters, which no longer exists now that cities are group headers rather
  // than a flat chip row; the fallback keeps this working against an older index.html.
  const chipGroup = document.getElementById("quick-filters")
                 || document.getElementById("city-filters");
  if (!chipGroup || document.getElementById("chip-for-you")) return;
  const chip = document.createElement("button");
  chip.id        = "chip-for-you";
  chip.className = "chip for-you-chip";
  chip.textContent = "★ for you";
  chip.onclick = function () {
    if (typeof toggleForYou === "function") toggleForYou();
  };
  chipGroup.appendChild(chip);
}

// ── Init ───────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", function () {
  handleSpotifyCallback();
  if (isSpotifyConnected()) {
    _updateSpotifyUI("connected");
    _onSpotifyReady();
  }
});
