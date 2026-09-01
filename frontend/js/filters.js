// Filter logic — all filtering is done client-side.
// The API is fetched once per calendar month (FullCalendar's default); no server-side
// filter params are sent. Venue, city, and search filters are applied locally.

let venues = [];

// Persisted toggle: hide non-live-music events (karaoke, trivia, theme nights,
// recurring series) unless the visitor opts in. Default off = filtered.
const INCLUDE_NON_MUSIC_KEY = "triangle-shows-include-non-music";

let activeFilters = {
  search: "",
  forYou: false,
  includeNonMusic: localStorage.getItem(INCLUDE_NON_MUSIC_KEY) === "1",
  // Deliberately NOT persisted, unlike includeNonMusic. That one is a standing
  // preference ("I want to see trivia nights"); this is a transient "just show me
  // mine" view. A view filter that survives a reload is how someone concludes the
  // calendar is broken — they come back to a near-empty month and have no memory of
  // switching anything on.
  favoritesOnly: false,
};

// Cache of all events from the API (plain JS objects). Populated once on initial
// load by the function-based event source in app.js. Used by _getFilteredEvents()
// so we never call setProp on EventImpl objects during filter passes.
let _allEventsCache = [];

// ── Collapsed city groups ─────────────────────────────────────────────────────
//
// Which city groups are folded shut in the sidebar. Purely presentational: collapsing
// a city must never change which events are shown, or the calendar would silently
// disagree with the filters the visitor can see. Persisted because every other
// sidebar toggle is.
const COLLAPSED_CITIES_KEY = "triangle-shows-collapsed-cities";

// The persisted list, or null when the visitor has never folded anything.
function _storedCollapsedCities() {
  const raw = localStorage.getItem(COLLAPSED_CITIES_KEY);
  if (raw === null) return null;
  try { return new Set(JSON.parse(raw)); }
  catch { return new Set(); }
}

// Which cities are folded shut. Everything starts folded: the point of grouping is to
// shorten the sidebar, and four headers plus twenty-two rows is *longer* than the flat
// list this replaced. Once the visitor folds or unfolds anything, their explicit choice
// is stored and used instead.
function getCollapsedCities() {
  const stored = _storedCollapsedCities();
  if (stored) return stored;
  return new Set(venues.map((v) => v.city));
}

function saveCollapsedCities(set) {
  try { localStorage.setItem(COLLAPSED_CITIES_KEY, JSON.stringify([...set])); }
  catch { /* private browsing: the fold still works, it just won't persist */ }
}

// City names go into element ids, so reduce them to something id-safe.
function _citySlug(city) {
  return String(city).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function toggleCityCollapse(city) {
  const collapsed = getCollapsedCities();
  if (collapsed.has(city)) collapsed.delete(city);
  else                     collapsed.add(city);
  saveCollapsedCities(collapsed);

  const group = document.querySelector(`.city-group[data-city="${city}"]`);
  if (!group) return;

  const isCollapsed = collapsed.has(city);
  group.classList.toggle("collapsed", isCollapsed);

  const btn = group.querySelector(".city-collapse");
  if (btn) {
    const n = group.querySelectorAll(".venue-checkbox").length;
    btn.setAttribute("aria-expanded", isCollapsed ? "false" : "true");
    btn.setAttribute(
      "aria-label",
      `${isCollapsed ? "Expand" : "Collapse"} ${city}, ${n} venue${n === 1 ? "" : "s"}`
    );
  }
  // Deliberately no applyAllFilters() — see the note on COLLAPSED_CITIES_KEY.
}

// The count on a city header is "venues you can see here", so it has to be refreshed
// when one is hidden rather than only on a full re-render.
function _updateCityCounts() {
  document.querySelectorAll(".city-group").forEach((group) => {
    const count = group.querySelectorAll(".venue-checkbox").length;
    const el = group.querySelector(".city-count");
    if (el) el.textContent = String(count);
  });
}


// ── Hidden venues ─────────────────────────────────────────────────────────────
const HIDDEN_VENUES_KEY = "triangle-shows-hidden-venues";

function getHiddenVenues() {
  try { return new Set(JSON.parse(localStorage.getItem(HIDDEN_VENUES_KEY) || "[]")); }
  catch { return new Set(); }
}

function hideVenue(slug) {
  const hidden = getHiddenVenues();
  hidden.add(slug);
  localStorage.setItem(HIDDEN_VENUES_KEY, JSON.stringify([...hidden]));
  const label = document.querySelector(`.venue-checkbox input[data-venue="${slug}"]`)?.closest(".venue-checkbox");
  const group = label?.closest(".city-group");
  if (label) label.remove();
  // Drop a city whose last visible venue just went. An empty group still showing a
  // live filter chip reads as a rendering bug.
  if (group && !group.querySelector(".venue-checkbox")) group.remove();
  _updateCityCounts();
  _updateVenueRestoreBtn();
  applyAllFilters();
  updateCityChipStates();
}

function restoreHiddenVenues() {
  localStorage.removeItem(HIDDEN_VENUES_KEY);
  renderVenueFilters();
  applyAllFilters();
  updateCityChipStates();
}

function _updateVenueRestoreBtn() {
  const existing = document.getElementById("venue-restore-btn");
  if (existing) existing.remove();
  const hidden = getHiddenVenues();
  if (hidden.size === 0) return;
  const container = document.getElementById("venue-filters");
  const btn = document.createElement("button");
  btn.id = "venue-restore-btn";
  btn.className = "venue-restore-btn";
  btn.textContent = `↺ restore hidden (${hidden.size})`;
  btn.addEventListener("click", restoreHiddenVenues);
  container.appendChild(btn);
}

async function loadVenues(attempt = 0) {
  try {
    const resp = await fetch(`${API_BASE}/api/venues`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const allVenues = await resp.json();
    venues = SITE_CONFIG.city
      ? allVenues.filter((v) => v.city === SITE_CONFIG.city)
      : allVenues;
    renderFilters();
  } catch (err) {
    console.error("Failed to load venues:", err);
    if (attempt < 2) {
      setTimeout(() => loadVenues(attempt + 1), 2000);
    }
  }
}

function renderFilters() {
  // Cities and venues render together now — the city rows are the group headers.
  renderVenueFilters();
  setupSearch();

  updateCityChipStates();
}

// Render every city as a collapsible group with its venues nested underneath.
//
// The city header keeps the `.city-chip` class and `data-city`, and each venue keeps
// `data-venue-city`, so toggleCity() and updateCityChipStates() work against this
// markup unchanged — they query by those attributes, not by position.
function renderVenueFilters() {
  const container = document.getElementById("venue-filters");
  const hidden = getHiddenVenues();
  const collapsed = getCollapsedCities();

  // Preserve which venues are currently checked so restoring a hidden venue
  // doesn't reset other venues' selected/unselected state.
  const currentChecked = new Set();
  document.querySelectorAll(".venue-checkbox input[type=checkbox]").forEach((cb) => {
    if (cb.checked) currentChecked.add(cb.dataset.venue);
  });
  const hasExistingState = currentChecked.size > 0;

  const shown = venues.filter((v) => !hidden.has(v.slug));
  // A city with every venue hidden gets no group at all, rather than an empty one.
  const cities = [...new Set(shown.map((v) => v.city))].sort();

  container.innerHTML = cities
    .map((city) => {
      const colors   = CITY_COLORS[city] || {};
      const border   = colors.border   || "var(--dim)";
      const activeBg = colors.activeBg || "var(--accent-bg)";
      const cityVenues  = shown.filter((v) => v.city === city);
      const isCollapsed = collapsed.has(city);
      const venuesId    = `city-venues-${_citySlug(city)}`;

      const rows = cityVenues
        .map((v) => {
          // Newly-visible venues (just restored) default to checked.
          // Existing venues preserve their prior state.
          const isChecked = !hasExistingState || currentChecked.has(v.slug) || !document.querySelector(`[data-venue="${v.slug}"]`);
          return `
        <label class="venue-checkbox" data-venue-city="${city}">
          <input type="checkbox" data-venue="${v.slug}" ${isChecked ? "checked" : ""} onchange="toggleVenue('${v.slug}')">
          <span class="venue-dot" style="background-color: ${v.color}"></span>
          <span class="venue-label">${v.name}</span>
          <button class="venue-hide-btn" data-slug="${v.slug}" tabindex="-1" aria-label="Hide ${v.name}">✕</button>
        </label>`;
        })
        .join("");

      // Caret first so it reads as the disclosure control for the row, the way a
      // tree view does. The count is a plain span rather than part of either button —
      // it's a readout, and putting it inside the caret's hit area implied otherwise.
      return `
      <div class="city-group${isCollapsed ? " collapsed" : ""}" data-city="${city}">
        <div class="city-group-head">
          <button class="city-collapse" data-city="${city}"
                  aria-expanded="${isCollapsed ? "false" : "true"}"
                  aria-controls="${venuesId}"
                  aria-label="${isCollapsed ? "Expand" : "Collapse"} ${city}, ${cityVenues.length} venue${cityVenues.length === 1 ? "" : "s"}"
                  onclick="toggleCityCollapse('${city}')"><span class="city-caret" aria-hidden="true">▾</span></button>
          <button class="chip city-chip" data-city="${city}"
                  style="--chip-border: ${border}; --chip-active-bg: ${activeBg}"
                  onclick="toggleCity('${city}')">${city}</button>
          <span class="city-count" aria-hidden="true">${cityVenues.length}</span>
        </div>
        <div class="city-venues" id="${venuesId}">${rows}</div>
      </div>`;
    })
    .join("");

  container.querySelectorAll(".venue-hide-btn").forEach((btn) => {
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      hideVenue(this.dataset.slug);
    });
  });
  _updateVenueRestoreBtn();
}

function setupSearch() {
  const input = document.getElementById("search-input");
  let timeout;
  let viewSwitchedBySearch = false;

  input.addEventListener("input", () => {
    const q = input.value.trim();

    // Immediately switch to list view on first keystroke
    if (q && calendar && calendar.view.type !== "listUpcoming") {
      calendar.changeView("listUpcoming");
      viewSwitchedBySearch = true;
    }

    // Switch back to month grid when search is cleared (desktop only)
    if (!q && viewSwitchedBySearch && window.innerWidth >= 768) {
      calendar.changeView("dayGridMonth");
      viewSwitchedBySearch = false;
    }

    // Debounce the filter application
    clearTimeout(timeout);
    timeout = setTimeout(() => {
      activeFilters.search = q;
      applyAllFilters();
    }, 300);
  });
}

// ── Core filter logic ─────────────────────────────────────────────────────

// Toggle the "★ for you" Spotify filter on/off.
function toggleForYou() {
  activeFilters.forYou = !activeFilters.forYou;
  const chip = document.getElementById("chip-for-you");
  if (chip) chip.classList.toggle("active", activeFilters.forYou);
  applyAllFilters();
}

// Toggle whether non-live-music events (karaoke/trivia/theme nights/etc.) are shown.
// Choice is persisted so it sticks across visits. Default (chip inactive) hides them.
// Show only hearted shows. The button lives in the bottom-left favourites bar, so
// updateBottomBar() is refreshed here to keep its label and pressed state in sync.
function toggleFavoritesOnly() {
  activeFilters.favoritesOnly = !activeFilters.favoritesOnly;
  if (typeof updateBottomBar === "function") updateBottomBar();
  applyAllFilters();
}

function toggleNonMusic() {
  activeFilters.includeNonMusic = !activeFilters.includeNonMusic;
  localStorage.setItem(INCLUDE_NON_MUSIC_KEY, activeFilters.includeNonMusic ? "1" : "0");
  const chip = document.getElementById("chip-non-music");
  if (chip) chip.classList.toggle("active", activeFilters.includeNonMusic);
  applyAllFilters();
}

// Generate a webcal:// subscription URL from the currently-checked venues
// and redirect to it, triggering the native Add Subscription dialog.
function subscribeToVenues() {
  const allCbs  = [...document.querySelectorAll(".venue-checkbox input[type=checkbox]")];
  const checked = allCbs.filter(cb => cb.checked);
  const params = new URLSearchParams();

  // "Everything is ticked, so no filter is needed" holds only on the main site, where the
  // sidebar lists every venue. On a city-scoped site it lists that city's venues alone, so
  // omitting the filter subscribes the visitor to the whole Triangle — the opposite of the
  // page they are looking at. Measured on the Durham site: 8 of 8 ticked produced a bare
  // /feeds/events.ics and a calendar full of Raleigh and Chapel Hill shows.
  const cityScoped = typeof SITE_CONFIG === "object" && !!SITE_CONFIG.city;
  if (checked.length > 0 && (cityScoped || checked.length < allCbs.length)) {
    params.set("venue", checked.map(cb => cb.dataset.venue).join(","));
  }
  // Match the feed to what the visitor is viewing: if they've opted into
  // non-live-music events, include them in the subscription too.
  if (activeFilters.includeNonMusic) params.set("include_non_music", "1");
  const qs = params.toString();
  window.location.href = `webcal://${window.location.host}/feeds/events.ics${qs ? "?" + qs : ""}`;
}

// Returns true if an event should be visible given the current filter state.
// venueMap is a pre-built {slug: boolean} lookup (passed in to avoid per-event DOM queries).
function _checkEventVisible(ev, venueMap) {
  const props = ev.extendedProps;

  // On subdomain sites (e.g. durm.triangle-shows.net), only show events from
  // the locked city. Non-locked-city venues have no sidebar checkbox, so they
  // wouldn't be in venueMap and would otherwise pass through unchecked.
  if (SITE_CONFIG.city && props.venue_city !== SITE_CONFIG.city) return false;

  const hearted = typeof isFavorited === "function" && isFavorited(ev.id);

  // Favourites-only view. Composes with the venue and search filters below rather than
  // overriding them, because those are explicit choices — unticking a venue should keep
  // working even if you hearted something there.
  if (activeFilters.favoritesOnly && !hearted) return false;

  // Non-live-music events (karaoke, trivia, theme nights, recurring series) are
  // hidden unless the visitor opts in via the toggle. Only an explicit `false`
  // hides — missing/true always shows, so unclassified data is never dropped.
  //
  // Inside the favourites-only view a hearted show is exempt: the flag is the
  // classifier's guess, and someone who deliberately hearted a karaoke night has
  // overruled it. Without this the button could read "only my shows (5)" beside a
  // calendar showing two — the kind of quiet disagreement that reads as a bug.
  //
  // Scoped to that view on purpose. Outside it the toggle keeps meaning "hide this
  // category", so the normal calendar behaves exactly as before.
  const exemptAsFavorite = activeFilters.favoritesOnly && hearted;
  if (!activeFilters.includeNonMusic && props.is_live_music === false && !exemptAsFavorite) {
    return false;
  }

  // "For you" mode: show only Spotify-matched events, ignoring venue filters.
  if (activeFilters.forYou) {
    return typeof eventMatchesSpotify === "function" &&
      eventMatchesSpotify(ev.title, props.artist);
  }

  // Venue checkbox — if unchecked, hide
  if (venueMap && props.venue_slug in venueMap) {
    if (!venueMap[props.venue_slug]) return false;
  } else if (!venueMap) {
    // Fallback: direct DOM query (used when called outside applyAllFilters)
    const cb = document.querySelector(`[data-venue="${props.venue_slug}"]`);
    if (cb && !cb.checked) return false;
  }

  // Search filter
  if (activeFilters.search) {
    const q = activeFilters.search.toLowerCase();
    const haystack = [ev.title, props.name, props.artist, props.venue_name]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    if (!haystack.includes(q)) return false;
  }

  return true;
}

// Venues where multiple same-day events should collapse to one chip.
const GROUPED_VENUE_SLUGS = new Set(["dpac"]);

// Returns the subset of _allEventsCache that should be visible given current filter
// state. Used by the function-based event source — returns plain JS objects so
// FullCalendar renders them without ever calling setProp on existing EventImpls.
function _getFilteredEvents() {
  const venueMap = {};
  document.querySelectorAll(".venue-checkbox input[type=checkbox]").forEach((cb) => {
    venueMap[cb.dataset.venue] = cb.checked;
  });
  getHiddenVenues().forEach((slug) => { venueMap[slug] = false; });
  const hiddenObj = typeof getHidden === "function" ? getHidden() : {};

  // Apply venue / search / individually-hidden filters.
  const visible = _allEventsCache.filter((ev) => {
    if (hiddenObj[ev.id]) return false;
    return _checkEventVisible(ev, venueMap);
  });

  // DPAC grouping: keep one event per day, prefer on_sale over others.
  const dpacGroups = {};
  visible.forEach((ev) => {
    const slug = ev.extendedProps?.venue_slug;
    if (!GROUPED_VENUE_SLUGS.has(slug)) return;
    const key = slug + "|" + ev.extendedProps?.date;
    if (!dpacGroups[key]) {
      dpacGroups[key] = { primary: ev, all: [ev] };
    } else {
      dpacGroups[key].all.push(ev);
      if (ev.extendedProps?.status === "on_sale" &&
          dpacGroups[key].primary.extendedProps?.status !== "on_sale") {
        dpacGroups[key].primary = ev;
      }
    }
  });
  const suppressed = new Set();
  Object.values(dpacGroups).forEach(({ primary, all }) => {
    if (all.length > 1) all.forEach((ev) => { if (ev !== primary) suppressed.add(ev.id); });
  });

  return visible.filter((ev) => !suppressed.has(ev.id));
}

// rAF gate: absorbs rapid-fire calls so only one filter pass runs per animation frame.
let _filterRafId = null;
function applyAllFilters() {
  if (_filterRafId !== null) return;
  _filterRafId = requestAnimationFrame(() => {
    _filterRafId = null;
    _applyAllFiltersNow();
  });
}

// Triggers a re-render by asking FullCalendar to refetch events. The function-based
// event source calls _getFilteredEvents() and returns only the visible subset as
// plain JS objects — no setProp mutations needed.
function _applyAllFiltersNow() {
  if (!calendar) return;
  if (document.activeElement && document.activeElement !== document.body) {
    document.activeElement.blur();
  }

  // Pin the calendar height before refetch so the page doesn't briefly shrink
  // (which causes the browser to auto-adjust scrollY with no JS call stack).
  const calEl = document.getElementById("calendar");
  if (calEl) calEl.style.minHeight = calEl.offsetHeight + "px";

  calendar.refetchEvents();

  requestAnimationFrame(() => {
    if (calEl) calEl.style.minHeight = "";
  });
}

// ── Filter toggles ────────────────────────────────────────────────────────

// City chips solo/restore — mirrors the venue checkbox behaviour but at city level.
//   • All venues on  → click city  → solo that city (hide all other cities).
//   • Only that city → click city  → restore all venues.
//   • Mixed state    → click city  → enable all venues in that city.
function toggleCity(city) {
  const allCheckboxes = [
    ...document.querySelectorAll(".venue-checkbox input[type=checkbox]"),
  ];
  if (!allCheckboxes.length) return; // venues not yet loaded

  const cityCheckboxes = [
    ...document.querySelectorAll(
      `.venue-checkbox[data-venue-city="${city}"] input[type=checkbox]`
    ),
  ];
  if (!cityCheckboxes.length) return; // unknown city

  const totalChecked  = allCheckboxes.filter((cb) => cb.checked).length;
  const total         = allCheckboxes.length;
  const cityChecked   = cityCheckboxes.filter((cb) => cb.checked).length;
  const cityTotal     = cityCheckboxes.length;

  if (totalChecked === total) {
    // Everything is on → solo this city
    allCheckboxes.forEach((cb) => {
      cb.checked = cityCheckboxes.includes(cb);
    });
  } else if (cityChecked === cityTotal && totalChecked === cityTotal) {
    // Only this city is showing → restore all
    allCheckboxes.forEach((cb) => { cb.checked = true; });
  } else if (cityChecked === cityTotal) {
    // All this city's venues are already checked → uncheck them
    cityCheckboxes.forEach((cb) => { cb.checked = false; });
  } else {
    // Some city venues are unchecked → enable all in this city
    cityCheckboxes.forEach((cb) => { cb.checked = true; });
  }

  applyAllFilters();
  updateCityChipStates();
}

// City chip active state: active = at least one venue in that city is enabled.
function updateCityChipStates() {
  document.querySelectorAll(".city-chip").forEach((btn) => {
    const city = btn.dataset.city;
    const checkboxes = [
      ...document.querySelectorAll(
        `.venue-checkbox[data-venue-city="${city}"] input[type=checkbox]`
      ),
    ];
    const anyEnabled = checkboxes.some((cb) => cb.checked);
    btn.classList.toggle("active", anyEnabled);
  });
}

// Venue checkbox toggle with solo/restore behavior:
// • If all venues were enabled and user unchecks one → solo that venue (show only it).
// • If only one venue was enabled and user unchecks it → restore all venues.
// • Otherwise → normal toggle.
function toggleVenue(slug) {
  const allCheckboxes = [
    ...document.querySelectorAll(".venue-checkbox input[type=checkbox]"),
  ];
  const cb = document.querySelector(`[data-venue="${slug}"]`);
  const checkedCount = allCheckboxes.filter((c) => c.checked).length;
  const total = allCheckboxes.length;

  if (!cb.checked && checkedCount === total - 1) {
    // All were enabled; user unchecked one → solo it
    allCheckboxes.forEach((c) => {
      c.checked = c === cb;
    });
  } else if (!cb.checked && checkedCount === 0) {
    // Only this venue was enabled; user unchecked it → restore all
    allCheckboxes.forEach((c) => {
      c.checked = true;
    });
  }
  // else: normal toggle, checkbox state already updated by browser

  applyAllFilters();
  updateCityChipStates();
}

// Toggle filter sidebar on mobile
function toggleSidebar() {
  const sidebar = document.getElementById("sidebar");
  const backdrop = document.getElementById("sidebar-backdrop");
  sidebar.classList.toggle("open");
  if (backdrop) backdrop.classList.toggle("active");
}

// Reflect the persisted non-live-music toggle state on the (static) chip.
// Scripts load with `defer`, so the DOM is ready when this runs.
(function initNonMusicChip() {
  const chip = document.getElementById("chip-non-music");
  if (chip) chip.classList.toggle("active", activeFilters.includeNonMusic);
})();

// Initialize
loadVenues();
