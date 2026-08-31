// Favorites & hidden-show persistence.
// All state lives in localStorage; zero backend dependency.

const FAVORITES_KEY = "triangle-shows-favorites";
const HIDDEN_KEY    = "triangle-shows-hidden";

// ── Favorites ──────────────────────────────────────────────────────────────

function getFavorites() {
  try { return JSON.parse(localStorage.getItem(FAVORITES_KEY) || "{}"); }
  catch { return {}; }
}
// Returns false when the store refused the write, so callers can avoid showing a heart
// that was never saved.
//
// getFavorites() already tolerates unreadable storage; this side did not, so a throw
// propagated straight out of toggleFavorite() and skipped the UI update, leaving the
// heart, the bar and the equalizer disagreeing with each other.
//
// Capacity is not the likely trigger. Measured against all 2771 events currently in the
// database, a stored favourite averages 247 bytes, so a 5 MB origin quota holds roughly
// 21,000 of them and hearting literally every event would use about 13%. What does throw
// regardless of size is a private-browsing window or a browser set to block site data.
function saveFavorites(favs) {
  try {
    localStorage.setItem(FAVORITES_KEY, JSON.stringify(favs));
    return true;
  } catch (err) {
    console.warn("Could not save favorites — storage refused the write:", err);
    return false;
  }
}
function isFavorited(eventId) { return !!getFavorites()[eventId]; }

function toggleFavorite(eventId, eventData) {
  const favs = getFavorites();
  if (favs[eventId]) { delete favs[eventId]; }
  else               { favs[eventId] = eventData; }
  // Bail before touching the UI if the write failed, so the heart never claims a
  // favourite that was not stored.
  if (!saveFavorites(favs)) return;
  _refreshHeartUI(eventId, !!favs[eventId]);
  updateBottomBar();

  const favOnly = typeof activeFilters === "object" && !!activeFilters.favoritesOnly;
  if (favOnly && typeof applyAllFilters === "function") {
    // In the favourites-only view this heart changes which events belong on screen, so
    // the calendar has to be re-filtered — otherwise an un-hearted show just sits there.
    // The re-filter feeds the equalizer with the new visible set on its way through
    // app.js, so refreshing the equalizer here first would only render a set that is
    // about to be replaced.
    applyAllFilters();
  } else if (window.Equalizer && typeof window.Equalizer.refresh === "function") {
    // Outside that view the visible set is unchanged and only the hearted portion of
    // each bar needs repainting, which is far cheaper than a full re-filter.
    window.Equalizer.refresh();
  }
}

function _refreshHeartUI(eventId, hearted) {
  document.querySelectorAll(`.ev-heart[data-event-id="${eventId}"]`).forEach((btn) => {
    btn.classList.toggle("hearted", hearted);
    btn.textContent = hearted ? "♥" : "♡";
    btn.setAttribute("aria-label", hearted ? "Remove from favorites" : "Add to favorites");
    // Sync accent outline on the parent fc-event element
    const fcEl = btn.closest(".fc-event");
    if (fcEl) fcEl.classList.toggle("ev-hearted", hearted);
  });
}

// ── Hidden events ──────────────────────────────────────────────────────────

function getHidden() {
  try { return JSON.parse(localStorage.getItem(HIDDEN_KEY) || "{}"); }
  catch { return {}; }
}
function isHidden(eventId) { return !!getHidden()[eventId]; }

function hideEvent(eventId) {
  const hidden = getHidden();
  hidden[eventId] = true;
  localStorage.setItem(HIDDEN_KEY, JSON.stringify(hidden));
  updateBottomBar();
}

function restoreHidden() {
  localStorage.removeItem(HIDDEN_KEY);
  updateBottomBar();
  // Refetch so eventDidMount re-runs with the hidden set cleared
  if (typeof calendar !== "undefined" && calendar) {
    calendar.refetchEvents();
  }
}

function unhideForDate(date) {
  const hidden = getHidden();
  let changed = false;
  // Use _allEventsCache because hidden events are excluded from FC's event store.
  const source = (typeof _allEventsCache !== "undefined" && _allEventsCache.length)
    ? _allEventsCache
    : (typeof calendar !== "undefined" && calendar ? calendar.getEvents() : []);
  source.forEach((ev) => {
    const evDate = ev.extendedProps?.date;
    if (evDate === date && hidden[ev.id]) {
      delete hidden[ev.id];
      changed = true;
    }
  });
  if (changed) {
    localStorage.setItem(HIDDEN_KEY, JSON.stringify(hidden));
    updateBottomBar();
    if (typeof _updateHiddenChip === "function") _updateHiddenChip(date);
    if (typeof applyAllFilters === "function") requestAnimationFrame(applyAllFilters);
  }
}

// ── Bottom bar (favorites download + restore hidden) ────────────────────────

function updateBottomBar() {
  const bar = document.getElementById("favorites-bar");
  if (!bar) return;

  const favCount = Object.keys(getFavorites()).length;
  const hidCount = Object.keys(getHidden()).length;
  const favOnly  = typeof activeFilters === "object" && !!activeFilters.favoritesOnly;

  // `|| favOnly` is load-bearing. This bar holds the only control for the
  // favourites-only view, so it has to stay up while that view is on — including after
  // the last favourite is un-hearted. Without it, removing your final show hides the
  // bar and strands you on a filtered, empty calendar with nothing to switch off.
  bar.classList.toggle("visible", favCount > 0 || hidCount > 0 || favOnly);

  const dlBtn = bar.querySelector(".btn-download-shows");
  if (dlBtn) {
    dlBtn.style.display = favCount > 0 ? "" : "none";
    dlBtn.textContent   = `↓ download my shows (${favCount})`;
  }

  const onlyBtn = document.getElementById("btn-favorites-only");
  if (onlyBtn) {
    // Same reasoning as the bar itself: stays visible while the view is on even at
    // zero favourites, so it can always be turned back off.
    onlyBtn.style.display = favCount > 0 || favOnly ? "" : "none";
    // The count explains an empty calendar at a glance — "(0)" is why nothing is here.
    onlyBtn.textContent = `${favOnly ? "♥" : "♡"} only my shows (${favCount})`;
    onlyBtn.classList.toggle("active", favOnly);
    onlyBtn.setAttribute("aria-pressed", favOnly ? "true" : "false");
  }

  const restoreBtn = document.getElementById("btn-restore-hidden");
  if (restoreBtn) {
    restoreBtn.style.display = hidCount > 0 ? "" : "none";
    restoreBtn.textContent   = `↺ restore hidden (${hidCount})`;
  }
}

// Alias kept for the app.js initialisation call
const updateFavoritesButton = updateBottomBar;

// ── iCal generation ────────────────────────────────────────────────────────

function downloadFavorites() {
  const events = Object.values(getFavorites());
  if (!events.length) return;

  const lines = [
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//Triangle Shows//triangle-shows.net//EN",
    "CALSCALE:GREGORIAN",
    "METHOD:PUBLISH",
    "X-WR-CALNAME:My Triangle Shows",
    "X-WR-TIMEZONE:America/New_York",
  ];

  for (const ev of events) {
    const hasTime = !!(ev.show_time && ev.show_time !== "00:00:00");
    const dtstart = _icsStart(ev.date, hasTime ? ev.show_time : null);
    const dtend   = _icsEnd(ev.date,   hasTime ? ev.show_time : null);
    const dtProp  = hasTime ? "DTSTART" : "DTSTART;VALUE=DATE";
    const dteProp = hasTime ? "DTEND"   : "DTEND;VALUE=DATE";

    const location = [ev.venue_name, ev.venue_city].filter(Boolean).join(", ");
    const desc     = [location, ev.ticket_url].filter(Boolean).join("\n");

    lines.push(
      "BEGIN:VEVENT",
      `${dtProp}:${dtstart}`,
      `${dteProp}:${dtend}`,
      `SUMMARY:${_esc(ev.title)}`,
      ...(location    ? [`LOCATION:${_esc(location)}`]    : []),
      ...(desc        ? [`DESCRIPTION:${_esc(desc)}`]     : []),
      ...(ev.ticket_url ? [`URL:${ev.ticket_url}`]        : []),
      `UID:${ev.id}@triangle-shows.org`,
      "END:VEVENT"
    );
  }

  lines.push("END:VCALENDAR");

  const blob = new Blob([lines.join("\r\n")], { type: "text/calendar;charset=utf-8" });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement("a");
  a.href     = url;
  a.download = "my-triangle-shows.ics";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

function _icsStart(dateStr, timeStr) {
  if (timeStr) {
    const [y, mo, d] = dateStr.split("-");
    const [h, min]   = timeStr.split(":");
    return `${y}${mo}${d}T${h.padStart(2,"0")}${(min||"00").padStart(2,"0")}00`;
  }
  return dateStr.replace(/-/g, "");
}

function _icsEnd(dateStr, timeStr) {
  if (timeStr) {
    const [y, mo, d] = dateStr.split("-").map(Number);
    const [h, min]   = timeStr.split(":").map(Number);
    const dt = new Date(y, mo - 1, d, h + 2, min || 0);
    return (
      String(dt.getFullYear()) +
      String(dt.getMonth() + 1).padStart(2, "0") +
      String(dt.getDate()).padStart(2, "0") + "T" +
      String(dt.getHours()).padStart(2, "0") +
      String(dt.getMinutes()).padStart(2, "0") + "00"
    );
  }
  const [y, mo, d] = dateStr.split("-").map(Number);
  const dt = new Date(y, mo - 1, d + 1);
  return (
    String(dt.getFullYear()) +
    String(dt.getMonth() + 1).padStart(2, "0") +
    String(dt.getDate()).padStart(2, "0")
  );
}

function _esc(str) {
  if (!str) return "";
  return str
    .replace(/\\/g, "\\\\")
    .replace(/;/g, "\\;")
    .replace(/,/g, "\\,")
    .replace(/\n/g, "\\n");
}
