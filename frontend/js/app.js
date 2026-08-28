// Rotating ko-fi pitch
const KOFI_PITCHES = [
  "help me pay for this domain",
  "keep the scrapers alive",
  "offset my caffeine dependency",
  "buy me a beer at Motorco",
  "cheaper than a ticket stub",
  "fuel for the scraper",
  "better than a StubHub fee",
  "helping you find your next show",
  "the server bill is real",
  "support local show-going",
];

document.addEventListener("DOMContentLoaded", function () {
  // Restore saved palette
  const savedPalette = localStorage.getItem("triangle-shows-palette");
  if (savedPalette && PALETTES[savedPalette]) {
    applyPalette(savedPalette);
  }

  // Restore saved mode, or detect OS preference
  const savedMode = localStorage.getItem("triangle-shows-mode");
  if (savedMode) {
    applyMode(savedMode);
  } else if (window.matchMedia("(prefers-color-scheme: light)").matches) {
    applyMode("light");
  } else {
    applyMode("dark");
  }

  // Rotating ko-fi pitch
  const pitchEl = document.querySelector(".kofi-pitch");
  if (pitchEl) {
    pitchEl.textContent = KOFI_PITCHES[Math.floor(Math.random() * KOFI_PITCHES.length)];
  }
});

// FullCalendar initialization
let calendar;

// Feed the night-density equalizer whatever the calendar is about to draw. Guarded rather
// than assumed present so the calendar still renders if equalizer.js fails to load.
function _updateEqualizer(events) {
  if (window.Equalizer && typeof window.Equalizer.update === "function") {
    window.Equalizer.update(events);
  }
}

// ── Scroll the month grid to today ───────────────────────────────────────────
//
// The month view renders at its full height inside .calendar-container, which is the
// element that scrolls. Late in a month that puts today's row below the fold, so the page
// opens on a stretch of days that have already happened.
//
// Only dayGrid needs this. The list view is built as a 180-day window starting today, so
// its first row already is today.
const _TODAY_HEADROOM = 90; // px of earlier weeks left visible above today, for context
let _scrolledToTodayOnce = false;

function _scrollToToday(smooth) {
  const container = document.querySelector(".calendar-container");
  const cell = document.querySelector(".fc-day-today");
  if (!container || !cell) return;

  const cellRect = cell.getBoundingClientRect();
  const boxRect  = container.getBoundingClientRect();

  // Leave it alone when today's row is already fully in view. Scrolling a reader back to
  // where they already are is worse than not scrolling at all.
  if (cellRect.top >= boxRect.top && cellRect.bottom <= boxRect.bottom) return;

  const target = container.scrollTop + (cellRect.top - boxRect.top) - _TODAY_HEADROOM;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  container.scrollTo({
    top: Math.max(0, target),
    behavior: smooth && !reduced ? "smooth" : "auto",
  });
}

// Row heights depend on how many events land in each cell, so this has to run after the
// tiles are in the DOM.
//
// Two frames when the tab is visible: one for FullCalendar's own paint, one for the layout
// that follows it. But requestAnimationFrame does not fire at all while a tab is in the
// background, so a timer backs it up -- without it, a page opened in a background tab is
// still sitting at the top of the month whenever someone gets round to looking at it.
// Whichever fires first wins; the other finds the work already done.
function _scrollToTodayWhenSettled(smooth) {
  let done = false;
  const run = function () {
    if (done) return;
    done = true;
    _scrollToToday(smooth);
  };

  requestAnimationFrame(function () { requestAnimationFrame(run); });
  setTimeout(run, 120);
}

// ── Sticky header offset ─────────────────────────────────────────────────────
//
// The toolbar and the weekday header are both sticky at top: 0, so the toolbar's opaque
// background covered SUN/MON/TUE entirely. styles.css offsets the header by
// --fc-toolbar-h; this keeps that value honest. Measured rather than hardcoded because the
// toolbar wraps to two rows at narrow widths.
let _toolbarObserver = null;

function _trackToolbarHeight() {
  const root = document.querySelector(".fc");
  const toolbar = document.querySelector(".fc-header-toolbar");
  if (!root || !toolbar) return;

  const apply = function () {
    root.style.setProperty("--fc-toolbar-h", toolbar.offsetHeight + "px");
  };
  apply();

  // Height changes on wrap, on font load, and on a view switch -- all of which a resize
  // handler alone would miss.
  if (window.ResizeObserver && !_toolbarObserver) {
    _toolbarObserver = new ResizeObserver(apply);
    _toolbarObserver.observe(toolbar);
  }
}

const _loadingScreenStart = Date.now();
let _initialLoadComplete = false;  // guards the function-source fetch against re-fetching from API
let _loadingScreenDismissed = false;

// ── Loading screen progress bar ──────────────────────────────────────────────
const _BAR_LEN = 20;
let _barRaf = null;

function _renderBar(pct) {
  const track = document.getElementById("ls-bar-track");
  const label = document.getElementById("ls-bar-pct");
  if (!track || !label) return;
  const filled = Math.round(pct / 100 * _BAR_LEN);
  track.innerHTML =
    '<span class="ls-bar-filled">' + "█".repeat(filled) + "</span>" +
    "░".repeat(_BAR_LEN - filled);
  label.textContent = "\u00a0".repeat(3 - String(Math.round(pct)).length) + Math.round(pct) + "%";
}

function _startProgressBar() {
  _renderBar(0);
  const start = performance.now();
  const FILL_MS = 900;
  function tick(now) {
    const t = Math.min((now - start) / FILL_MS, 1);
    _renderBar((1 - Math.pow(1 - t, 3)) * 85);
    if (t < 1) _barRaf = requestAnimationFrame(tick);
  }
  _barRaf = requestAnimationFrame(tick);
}

function _finishProgressBar(cb) {
  if (_barRaf) { cancelAnimationFrame(_barRaf); _barRaf = null; }
  _renderBar(100);
  setTimeout(cb, 200);
}
// ─────────────────────────────────────────────────────────────────────────────

// ── Per-day hidden-shows chips ────────────────────────────────────────────────
function _setHiddenChip(date, count) {
  // Month view: bordered chip in the day cell bottom
  const existing = document.querySelector(`.day-hidden-chip[data-date="${date}"]`);
  if (existing) existing.remove();
  const bottom = document.querySelector(`.fc-daygrid-day[data-date="${date}"] .fc-daygrid-day-bottom`);
  if (count > 0 && bottom) {
    const chip = document.createElement("a");
    chip.className = "day-hidden-chip";
    chip.dataset.date = date;
    chip.textContent = `↺ ${count} hidden`;
    chip.addEventListener("click", (e) => { e.stopPropagation(); unhideForDate(date); });
    bottom.appendChild(chip);
  }

  // List view: restore row after the last event row for this day
  const existingRow = document.querySelector(`.fc-list-hidden-row[data-date="${date}"]`);
  if (existingRow) existingRow.remove();
  if (count > 0) {
    const dayRow = document.querySelector(`tr.fc-list-day[data-date="${date}"]`);
    if (dayRow) {
      let insertAfter = dayRow;
      let sib = dayRow.nextElementSibling;
      while (sib && !sib.classList.contains("fc-list-day")) {
        if (sib.classList.contains("fc-list-event")) insertAfter = sib;
        sib = sib.nextElementSibling;
      }
      const label = count === 1 ? "↺ 1 hidden show" : `↺ ${count} hidden shows`;
      const tr = document.createElement("tr");
      tr.className = "fc-list-hidden-row";
      tr.dataset.date = date;
      tr.innerHTML = `<td colspan="3" class="fc-list-hidden-cell"><button class="list-hidden-btn">${label}</button></td>`;
      tr.querySelector(".list-hidden-btn").addEventListener("click", () => unhideForDate(date));
      insertAfter.after(tr);
    }
  }
}

function _updateHiddenChip(date) {
  const hiddenObj = typeof getHidden === "function" ? getHidden() : {};
  const count = _allEventsCache.filter(
    (ev) => ev.extendedProps?.date === date && hiddenObj[ev.id]
  ).length;
  _setHiddenChip(date, count);
}

function _updateAllHiddenChips() {
  const hiddenObj = getHidden();
  document.querySelectorAll(".day-hidden-chip, .fc-list-hidden-row").forEach((el) => el.remove());
  if (Object.keys(hiddenObj).length === 0) return;
  const byDate = {};
  calendar.getEvents().forEach((ev) => {
    if (hiddenObj[ev.id]) {
      const d = ev.extendedProps.date;
      byDate[d] = (byDate[d] || 0) + 1;
    }
  });
  Object.entries(byDate).forEach(([date, count]) => _setHiddenChip(date, count));
}

function _updateAllHiddenChipsFromSnapshot(allEvents) {
  const hiddenObj = getHidden();
  document.querySelectorAll(".day-hidden-chip, .fc-list-hidden-row").forEach((el) => el.remove());
  if (Object.keys(hiddenObj).length === 0) return;
  const byDate = {};
  allEvents.forEach((ev) => {
    if (hiddenObj[ev.id]) {
      const d = ev.extendedProps.date;
      byDate[d] = (byDate[d] || 0) + 1;
    }
  });
  Object.entries(byDate).forEach(([date, count]) => _setHiddenChip(date, count));
}
// ─────────────────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", function () {
  _startProgressBar();
  const calendarEl = document.getElementById("calendar");

  calendar = new FullCalendar.Calendar(calendarEl, {
    initialView: window.innerWidth < 768 ? "listUpcoming" : "dayGridMonth",
    views: {
      listUpcoming: {
        type: "list",
        duration: { days: 180 },
        buttonText: "list",
      },
    },
    headerToolbar: {
      left: "prev,next today",
      center: "title",
      right: "dayGridMonth,listUpcoming",
    },
    height: "auto",
    // Pin SUN/MON/TUE while the month grid scrolls. Previously nobody scrolled far enough
    // for it to matter; now that the view opens at today, the header would otherwise be
    // above the fold from the first paint and you would have to scroll up to learn which
    // column is which.
    stickyHeaderDates: true,
    fixedWeekCount: false,
    displayEventTime: false,
    eventSources: [
      function (info, successCallback, failureCallback) {
        if (!_initialLoadComplete) {
          fetch(`${API_BASE}/api/events/fullcalendar`)
            .then(function (r) {
              if (!r.ok) throw new Error("HTTP " + r.status);
              return r.json();
            })
            .then(function (data) {
              // Clamp every venue color to a readable luminance band before anything
              // renders. Venue colors are hand-authored in seed.py with no contrast check;
              // normalizing here fixes both modes at once (dark mode paints them as tile
              // backgrounds, light mode as tile text) and keeps any future light color
              // from reopening the same hole. See js/design.js.
              if (typeof normalizeVenueColor === "function") {
                data.forEach(function (ev) {
                  const safe = normalizeVenueColor(ev.backgroundColor);
                  ev.backgroundColor = safe;
                  ev.borderColor     = safe;
                  if (ev.extendedProps) {
                    ev.extendedProps.venue_color = safe;
                    // The gutter rule needs a brighter variant on a dark surface: these
                    // colors were authored as backgrounds behind white text, so all 22
                    // measure under 3:1 as a 3px rule on --surface2. Both variants are
                    // carried and CSS picks by mode.
                    if (typeof venueRuleColors === "function") {
                      const rule = venueRuleColors(safe);
                      ev.extendedProps.venue_rule_dark  = rule.dark;
                      ev.extendedProps.venue_rule_light = rule.light;
                    }
                  }
                });
              }
              _allEventsCache = data;
              _initialLoadComplete = true;
              const visible = _getFilteredEvents();
              _updateEqualizer(visible);
              successCallback(visible);
            })
            .catch(function (err) {
              console.error("Failed to fetch events:", err);
              failureCallback(err);
            });
        } else {
          const visible = _getFilteredEvents();
          _updateEqualizer(visible);
          successCallback(visible);
        }
      },
    ],
    eventClassNames: function (arg) {
      const classes = [];
      const today = new Date(); today.setHours(0, 0, 0, 0);
      if (arg.event.start && arg.event.start < today) classes.push("ev-past");
      if (arg.event.extendedProps.status === "sold_out") classes.push("ev-sold-out");
      if (typeof isFavorited === "function" && isFavorited(arg.event.id)) classes.push("ev-hearted");
      return classes;
    },
    eventClick: function (info) {
      info.jsEvent.preventDefault();
      openModal(info);
    },
    eventContent: function (arg) {
      const props   = arg.event.extendedProps;
      const soldOut = props.status === "sold_out" ? " [sold out]" : "";
      const hearted = typeof isFavorited === "function" && isFavorited(arg.event.id);
      const matched = typeof eventMatchesSpotify === "function" &&
                      typeof isSpotifyConnected  === "function" &&
                      isSpotifyConnected() &&
                      eventMatchesSpotify(arg.event.title, props.artist);
      const titleText = (matched ? "♫ " : "") + arg.event.title + soldOut;

      const html = `<div class="ev" style="--venue-color: ${props.venue_color || ''}">
        <button class="ev-heart${hearted ? " hearted" : ""}"
                data-event-id="${arg.event.id}"
                aria-label="${hearted ? "Remove from favorites" : "Add to favorites"}"
                tabindex="-1">${hearted ? "♥" : "♡"}</button>
        <button class="ev-hide"
                data-event-id="${arg.event.id}"
                aria-label="Hide this show"
                tabindex="-1">✕</button>
        <span class="ev-title">${titleText}</span>
        ${props.venue_name ? `<span class="ev-venue">${props.venue_name}</span>` : ""}
      </div>`;

      return { html };
    },
    windowResize: function (view) {
      const target = window.innerWidth < 768 ? "listUpcoming" : "dayGridMonth";
      if (calendar.view.type !== target) {
        calendar.changeView(target);
      }
    },
    datesSet: function (info) {
      // Fires on every view or date change -- paging months, hitting "today", and the
      // first render. Scroll only when the range on screen actually contains today, so
      // paging to an unrelated month leaves the scroll position alone.
      //
      // Deliberately not fired by a filter change: those call refetchEvents(), which does
      // not change the date range, so someone reading a later week is not yanked back.
      // Runs for every view: the toolbar exists in list view too, and re-observing after
      // a view switch is what keeps the measured height current.
      _trackToolbarHeight();

      if (info.view.type.indexOf("dayGrid") !== 0) return;
      const now = new Date();
      if (now >= info.start && now < info.end) {
        // Instant on first paint -- the page should simply open at today, with nothing to
        // animate from. Smooth afterwards, when someone has hit "today" or paged back to
        // this month and the movement tells them where they were taken.
        _scrollToTodayWhenSettled(_scrolledToTodayOnce);
        _scrolledToTodayOnce = true;
      }
    },
    loading: function (isLoading) {
      if (!isLoading) {
        if (!_loadingScreenDismissed) {
          _loadingScreenDismissed = true;

          // Re-run the scroll once the tiles exist. datesSet fires before the fetch
          // resolves, so the position it computed was against an empty grid and every row
          // below today has since grown. Instant, not smooth: a correction to where the
          // page already sits, not a movement anyone should watch.
          //
          // Inside this branch on purpose. `loading` also fires for the refetch behind
          // every filter change, and scrolling there would drag someone reading a later
          // week back to today each time they toggled a venue.
          _scrollToTodayWhenSettled(false);

          const elapsed = Date.now() - _loadingScreenStart;
          const delay = Math.max(0, 1000 - elapsed);
          setTimeout(function () {
            _finishProgressBar(function () {
              const screen = document.getElementById("loading-screen");
              if (screen) {
                screen.classList.add("fade-out");
                screen.addEventListener("transitionend", () => {
                  screen.remove();
                }, { once: true });
              } else {
                _filtersEnabled = true;
              }
            });
          }, delay);
        }
        // Update hidden-show chips after every load (initial + refetch).
        if (_allEventsCache.length > 0) {
          requestAnimationFrame(function () {
            _updateAllHiddenChipsFromSnapshot(_allEventsCache);
          });
        }
      }
    },
    eventDidMount: function (info) {
      // Publish the gutter rule colors on the event element rather than on .ev inside it.
      // The list view's colored dot is drawn by FullCalendar as a sibling of the title
      // cell, so a property set on .ev would not reach it.
      const p = info.event.extendedProps;
      if (p && p.venue_rule_dark) {
        info.el.style.setProperty("--venue-rule-dark", p.venue_rule_dark);
        info.el.style.setProperty("--venue-rule-light", p.venue_rule_light);
      }

      // Restore heart state for events loaded after page init.
      if (typeof isFavorited === "function" && isFavorited(info.event.id)) {
        const btn = info.el.querySelector(".ev-heart");
        if (btn) { btn.classList.add("hearted"); btn.textContent = "♥"; }
        info.el.classList.add("ev-hearted");
      }
      // List view: walk up to the <tr> (info.el is the title <td> or <a>, not
      // the row), then hide the sibling time cell and narrow the graphic cell.
      const tr = info.el.closest && info.el.closest("tr.fc-list-event");
      if (tr) {
        const timeTd = tr.querySelector(".fc-list-event-time");
        if (timeTd) { timeTd.style.display = "none"; timeTd.style.width = "0"; timeTd.style.padding = "0"; }
        const graphicTd = tr.querySelector(".fc-list-event-graphic");
        if (graphicTd) { graphicTd.style.width = "22px"; graphicTd.style.maxWidth = "22px"; }
      }
    },
  });

  calendar.render();
  // `let calendar` lives in the script scope, not on window, so the equalizer's
  // click-to-jump cannot reach it without this.
  window.calendar = calendar;

  // ── Heart / favorite click handler ──────────────────────────────────────
  // Use capture phase so we intercept before FullCalendar's eventClick fires.
  calendarEl.addEventListener(
    "click",
    function (e) {
      const heartBtn = e.target.closest(".ev-heart");
      const hideBtn  = e.target.closest(".ev-hide");
      if (!heartBtn && !hideBtn) return;
      e.stopPropagation(); // prevent modal from opening
      e.preventDefault();  // prevent <a href="#"> in FC list view from scrolling to top

      const eventId = (heartBtn || hideBtn).dataset.eventId;
      const fcEvent = calendar.getEventById(eventId);
      if (!fcEvent) return;

      if (heartBtn) {
        const p = fcEvent.extendedProps;
        toggleFavorite(eventId, {
          id:         eventId,
          title:      fcEvent.title,
          date:       p.date       || "",
          show_time:  p.show_time  || null,
          venue_name: p.venue_name || "",
          venue_city: p.venue_city || "",
          ticket_url: p.ticket_url || "",
        });
      } else {
        // For grouped venues (e.g. DPAC), hide all same-day events so a
        // different sibling doesn't surface on the next filter pass.
        const slug = fcEvent.extendedProps.venue_slug;
        const date = fcEvent.extendedProps.date;
        if (typeof GROUPED_VENUE_SLUGS !== "undefined" && GROUPED_VENUE_SLUGS.has(slug)) {
          _allEventsCache.forEach((ev) => {
            if (ev.extendedProps?.venue_slug === slug && ev.extendedProps?.date === date) {
              hideEvent(ev.id);
            }
          });
        } else {
          hideEvent(eventId);
        }
        applyAllFilters();
        _updateHiddenChip(date);
      }
    },
    true // capture
  );

  // Init favorites download button visibility
  if (typeof updateFavoritesButton === "function") updateFavoritesButton();
});
