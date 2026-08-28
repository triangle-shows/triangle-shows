/**
 * The night-density equalizer -- a pixel bar meter for the next two weeks.
 *
 * Role: Loaded before app.js, which calls Equalizer.update() with the filtered event list
 * on every calendar render. Each bar is one night; its height is how many shows are on
 * that night, so the strip answers "when is there anything on" at a glance -- something
 * the month grid cannot show, since a busy Saturday and a quiet one occupy the same cell.
 *
 * It reads as a flourish and behaves as one, but every bar is real data. It redraws
 * whenever a filter changes, which is why it lives at the top of the filter column: the
 * cause and its effect are visible together.
 *
 * Bars are built from discrete stacked cells rather than a scaled height, so the pixel
 * grid is real geometry and each cell can be lit, delayed, and flickered on its own.
 */

(function (global) {
  "use strict";

  const NIGHTS = 14;   // two weeks: long enough to show a rhythm, short enough to stay legible
  const CELLS  = 8;    // vertical resolution of one bar
  const MIN_SCALE = 6; // floor for the bar scale — see the comment in update()

  let _root     = null;
  let _readout  = null;
  let _bars     = [];  // { el, cells[], date, key, count }
  let _lastData = [];
  let _focused  = 0;   // roving tabindex position

  // --- Date helpers ---

  // Local-date key, deliberately not toISOString(): that converts to UTC first, which
  // shifts the date by one for anyone west of Greenwich after 7pm -- the exact hours this
  // site is used.
  function _key(d) {
    return (
      d.getFullYear() +
      "-" + String(d.getMonth() + 1).padStart(2, "0") +
      "-" + String(d.getDate()).padStart(2, "0")
    );
  }

  function _startOfToday() {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    return d;
  }

  const DAY_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];

  function _label(date, count) {
    const day = DAY_NAMES[date.getDay()];
    const num = date.getDate();
    if (count === 0) return day + " " + num + " · nothing on";
    return day + " " + num + " · " + count + (count === 1 ? " show" : " shows");
  }

  // --- Build ---

  function init() {
    _root    = document.getElementById("equalizer");
    _readout = document.getElementById("eq-readout");
    if (!_root) return;

    const today = _startOfToday();
    _root.innerHTML = "";
    _bars = [];

    for (let i = 0; i < NIGHTS; i++) {
      const date = new Date(today);
      date.setDate(today.getDate() + i);

      const bar = document.createElement("button");
      bar.type = "button";
      bar.className = "eq-bar";
      bar.tabIndex = i === 0 ? 0 : -1;
      // Staggered so a redraw sweeps left to right instead of snapping all at once.
      bar.style.setProperty("--bar-index", String(i));
      // Marks the start of each week so a fortnight reads as two weeks, not fourteen days.
      if (date.getDay() === 0) bar.classList.add("eq-week-start");

      const cells = [];
      for (let c = 0; c < CELLS; c++) {
        const cell = document.createElement("i");
        cell.className = "eq-cell";
        cell.style.setProperty("--cell-index", String(c));
        bar.appendChild(cell);
        cells.push(cell);
      }

      const entry = { el: bar, cells: cells, date: date, key: _key(date), count: 0 };
      _bars.push(entry);

      bar.addEventListener("mouseenter", function () { _showReadout(entry); });
      bar.addEventListener("focus",      function () { _showReadout(entry); });
      bar.addEventListener("click",      function () { _jumpTo(entry); });
      bar.addEventListener("keydown",    _onKeydown);

      _root.appendChild(bar);
    }

    _root.addEventListener("mouseleave", _clearReadout);
    update(_lastData);
  }

  // --- Interaction ---

  function _showReadout(entry) {
    if (_readout) _readout.textContent = _label(entry.date, entry.count);
    _bars.forEach(function (b) { b.el.classList.toggle("is-active", b === entry); });
  }

  function _clearReadout() {
    if (_readout) _readout.textContent = _summary();
    _bars.forEach(function (b) { b.el.classList.remove("is-active"); });
  }

  function _jumpTo(entry) {
    if (!global.calendar || typeof global.calendar.gotoDate !== "function") return;
    global.calendar.gotoDate(entry.date);
  }

  // Roving tabindex: the strip is one tab stop and arrows move within it, rather than
  // dropping fourteen stops between search and the city filters.
  function _onKeydown(e) {
    let next = null;
    if (e.key === "ArrowRight") next = Math.min(_focused + 1, _bars.length - 1);
    else if (e.key === "ArrowLeft") next = Math.max(_focused - 1, 0);
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = _bars.length - 1;
    else return;

    e.preventDefault();
    _bars[_focused].el.tabIndex = -1;
    _focused = next;
    _bars[_focused].el.tabIndex = 0;
    _bars[_focused].el.focus();
  }

  // --- Render ---

  function _summary() {
    const total = _bars.reduce(function (sum, b) { return sum + b.count; }, 0);
    if (total === 0) return "nothing in the next two weeks";
    return total + (total === 1 ? " show" : " shows") + " in the next two weeks";
  }

  /**
   * Recompute every bar from a FullCalendar event array.
   *
   * Safe to call on every render: it is a count over events already in memory, and the
   * cell classes it sets are idempotent.
   */
  function update(events) {
    _lastData = events || [];
    if (!_root || _bars.length === 0) return;

    const counts = Object.create(null);
    _lastData.forEach(function (ev) {
      // Accept both the raw feed shape (start: "YYYY-MM-DD") and a Date, so this works
      // whether it is handed cached JSON or live calendar objects.
      const raw = ev && ev.start;
      if (!raw) return;
      const key = typeof raw === "string" ? raw.slice(0, 10) : _key(raw);
      counts[key] = (counts[key] || 0) + 1;
    });

    let peak = 0;
    _bars.forEach(function (bar) {
      bar.count = counts[bar.key] || 0;
      if (bar.count > peak) peak = bar.count;
    });

    // Scale to the busiest night in view so the shape stays readable under a narrow
    // filter -- but floor that scale, or a filter leaving only single-show nights makes
    // every one of them a full-height bar, which reads as "packed" when it means "one
    // show". With the floor, one show is one cell whether or not anything else is on.
    const scale = Math.max(peak, MIN_SCALE);

    _bars.forEach(function (bar) {
      let lit = 0;
      if (bar.count > 0) {
        lit = Math.max(1, Math.round((bar.count / scale) * CELLS));
      }

      bar.cells.forEach(function (cell, index) {
        // Cell 0 is the bottom of the bar: DOM order is top-down, so invert.
        const height = CELLS - index;
        cell.classList.toggle("on", height <= lit);
        cell.classList.toggle("cap", height === lit && lit > 0);
      });

      bar.el.classList.toggle("is-empty", bar.count === 0);
      bar.el.setAttribute("aria-label", _label(bar.date, bar.count));
    });

    if (_root) _root.setAttribute("aria-label", _summary());
    _clearReadout();
  }

  function refresh() { update(_lastData); }

  // --- Exports ---
  global.Equalizer = { init: init, update: update, refresh: refresh };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})(window);
