// Tests for the lineup-poster logic in ../js/lineup.js.
//
// Run with:  node --test frontend/tests/lineup.test.js
// node:test is a Node built-in, so this file needs no package.json and no deps.
//
// lineup.js is a plain <script> that touches no browser global at load time — every
// DOM, canvas and localStorage access sits inside a function — so it evaluates cleanly
// into a bare vm context and exports its pure half on `Lineup`. The drawing code is not
// exercised here; what is under test is everything that decides *what* gets drawn.
//
// Two of these are regression tests rather than specification:
//
// Dates. A favourite is stored as "YYYY-MM-DD" and `new Date("2026-10-16")` parses as
// UTC midnight, which is 8pm the previous day in this site's own timezone — so the
// naive implementation puts a Friday show on Thursday's poster and can drop today's
// show from it entirely. equalizer.js carries the same warning for the same reason.
//
// The fifteen-show cap. A longer list is cut to the soonest fifteen, and the poster now
// says nothing about what it left out, so the cap has to cut from the right end. It also
// has to be the thing that limits the list: if ROW_MIN ever rises past available/15, the
// region starts dropping shows the cap meant to keep, which is why the geometry is
// asserted alongside it.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// Arrays and objects returned by the script are constructed with the vm context's
// own intrinsics, so deepStrictEqual fails their prototype check even when the
// contents match. Every assertion below therefore compares primitives.
const ids = (events) => events.map((e) => e.id).join(",");

const LINEUP_SRC = fs.readFileSync(path.join(__dirname, "../js/lineup.js"), "utf8");

function loadLineup() {
  const sandbox = {};
  vm.createContext(sandbox);
  vm.runInContext(LINEUP_SRC, sandbox);
  return sandbox.Lineup;
}

const L = loadLineup();

function fav(id, date, extra = {}) {
  return { id, date, title: `Show ${id}`, venue_name: "The Pinhook", ...extra };
}

// A character-count stand-in for ctx.measureText, so truncation can be asserted
// exactly instead of against whatever a headless canvas would report.
const measure10 = (s) => s.length * 10;

// ── upcomingFavorites ───────────────────────────────────────────────────────

test("keeps only today and later, soonest first", () => {
  const favs = {
    a: fav("a", "2026-10-20"),
    b: fav("b", "2026-09-30"),
    c: fav("c", "2026-09-21"),   // today
    d: fav("d", "2026-09-20"),   // yesterday
  };
  assert.equal(ids(L.upcomingFavorites(favs, "2026-09-21")), "c,b,a");
});

test("today's show is on the poster", () => {
  // The boundary that the UTC-parsing bug gets wrong: a show tonight is upcoming.
  const out = L.upcomingFavorites({ a: fav("a", "2026-09-21") }, "2026-09-21");
  assert.equal(out.length, 1);
});

test("compares dates as strings, so no Date is constructed from a date-only value", () => {
  // Late December into January — the case where a UTC shift crosses a year boundary.
  const favs = { a: fav("a", "2027-01-02"), b: fav("b", "2026-12-31") };
  assert.equal(ids(L.upcomingFavorites(favs, "2026-12-31")), "b,a");
});

test("drops entries localStorage should not have produced", () => {
  // getFavorites() JSON.parses without revalidating, so this has to survive junk.
  const favs = {
    ok: fav("ok", "2026-10-01"),
    nodate: { id: "nodate", title: "No date" },
    bad: { id: "bad", date: "not-a-date", title: "Bad" },
    numeric: { id: "numeric", date: 20261001, title: "Number" },
    nulled: null,
  };
  assert.equal(ids(L.upcomingFavorites(favs, "2026-09-21")), "ok");
});

test("same-night shows get a stable order", () => {
  const favs = {
    z: fav("z", "2026-10-16", { title: "Zulu" }),
    a: fav("a", "2026-10-16", { title: "Alpha" }),
  };
  const out = L.upcomingFavorites(favs, "2026-09-21");
  assert.equal(out.map((e) => e.title).join(","), "Alpha,Zulu");
});

test("an empty or missing store yields nothing rather than throwing", () => {
  assert.equal(L.upcomingFavorites({}, "2026-09-21").length, 0);
  assert.equal(L.upcomingFavorites(null, "2026-09-21").length, 0);
});

// ── posterEvents ────────────────────────────────────────────────────────────

test("a long list is cut to the soonest MAX_EVENTS", () => {
  const favs = {};
  // 20 shows, one a day, added newest first so a cap that trusted insertion order
  // rather than the sort would keep the wrong end of the list.
  for (let i = 20; i >= 1; i--) favs["e" + i] = fav("e" + i, `2026-10-${String(i).padStart(2, "0")}`);

  const out = L.posterEvents(favs, "2026-09-21");
  assert.equal(out.length, L.MAX_EVENTS);
  assert.equal(out[0].id, "e1", "starts with the soonest");
  assert.equal(out[L.MAX_EVENTS - 1].id, "e" + L.MAX_EVENTS, "and stops at the cap");
});

test("a list at or under the cap is left whole", () => {
  const favs = {};
  for (let i = 1; i <= L.MAX_EVENTS; i++) favs["e" + i] = fav("e" + i, `2026-10-${String(i).padStart(2, "0")}`);

  assert.equal(L.posterEvents(favs, "2026-09-21").length, L.MAX_EVENTS);
});

test("one favourite is enough for a poster", () => {
  // There is no minimum: a single upcoming show gets a poster like any other list.
  assert.equal(L.posterEvents({ a: fav("a", "2026-10-01") }, "2026-09-21").length, 1);
});

test("past favourites are left out of the poster list", () => {
  const favs = { old: fav("old", "2026-01-01"), next: fav("next", "2026-10-01") };
  const out = L.posterEvents(favs, "2026-09-21");
  assert.equal(out.length, 1);
  assert.equal(out[0].id, "next");
});

// ── formatPosterDate ────────────────────────────────────────────────────────

test("reads the weekday in local time", () => {
  // 16 October 2026 is a Friday. Parsed as UTC it is Thursday evening here.
  const out = L.formatPosterDate("2026-10-16");
  assert.equal(out.day, "FRI");
  assert.equal(out.date, "10.16");
});

test("pads the day but not the month", () => {
  assert.equal(L.formatPosterDate("2027-01-02").date, "1.02");
});

// ── planLayout ──────────────────────────────────────────────────────────────

const TOP = 328;
const BOTTOM = 1240;          // the real region, ~912px tall
const AVAILABLE = BOTTOM - TOP;

test("a list shorter than the reference is laid out as if it were that long", () => {
  // The visitor-facing promise: a one-favourite poster is the five-favourite poster
  // with four rows missing, not one row stretched over the whole page.
  const one = L.planLayout(1, TOP, BOTTOM);
  const five = L.planLayout(L.REF_ROWS, TOP, BOTTOM);

  assert.equal(one.visible, 1);
  assert.equal(one.rowHeight, five.rowHeight, "same row height");
  assert.equal(one.blockTop, five.blockTop, "and the first row in the same place");
  assert.equal(one.rowHeight, L.ROW_MAX, "which is the top of the scale");
});

test("type is the same size on every poster up to the reference length", () => {
  const sizeAt = (n) => JSON.stringify(L.rowTypeScale(L.planLayout(n, TOP, BOTTOM).rowHeight));
  const five = sizeAt(L.REF_ROWS);
  for (const n of [1, 2, 3, 4]) {
    assert.equal(sizeAt(n), five, `a ${n}-show poster is set differently from a five-show one`);
  }
});

test("rows tighten as the list grows past the reference, down to the floor", () => {
  const few = L.planLayout(6, TOP, BOTTOM);
  const many = L.planLayout(L.MAX_EVENTS, TOP, BOTTOM);
  assert.ok(many.rowHeight < few.rowHeight);
  assert.ok(many.rowHeight >= L.ROW_MIN);
});

test("a full fifteen fit, so the cap is what limits the list and not the page", () => {
  // If this fails the poster is dropping shows the cap meant to keep: ROW_MIN has grown
  // past available/MAX_EVENTS, or the region above the footer has shrunk.
  const plan = L.planLayout(L.MAX_EVENTS, TOP, BOTTOM);
  assert.equal(plan.visible, L.MAX_EVENTS);
  assert.ok(L.MAX_EVENTS * L.ROW_MIN <= AVAILABLE,
    `${L.MAX_EVENTS} rows at ROW_MIN=${L.ROW_MIN} need ${L.MAX_EVENTS * L.ROW_MIN}px of ${AVAILABLE}`);
});

test("rows never overflow the region they were given", () => {
  // Past MAX_EVENTS is the caller's mistake, not a crash: the guard clamps instead.
  for (const n of [1, 2, 5, 10, 15, 16, 40, 200]) {
    const plan = L.planLayout(n, TOP, BOTTOM);
    const bottom = plan.blockTop + plan.visible * plan.rowHeight;
    assert.ok(bottom <= BOTTOM + 0.001, `n=${n} ran past the footer (${bottom} > ${BOTTOM})`);
    assert.ok(plan.blockTop >= TOP - 0.001, `n=${n} started above the region`);
  }
});

test("a region too short for the list drops rows rather than crushing them", () => {
  const plan = L.planLayout(15, TOP, TOP + 200);
  assert.ok(plan.visible < 15);
  assert.ok(plan.rowHeight >= L.ROW_MIN);
});

// ── truncateToWidth ─────────────────────────────────────────────────────────

test("text that fits is left exactly as it is", () => {
  assert.equal(L.truncateToWidth(measure10, "Short", 100), "Short");
});

test("text that does not fit is cut and given an ellipsis", () => {
  const out = L.truncateToWidth(measure10, "A very long artist name indeed", 100);
  assert.ok(out.endsWith("…"));
  assert.ok(measure10(out) <= 100, `"${out}" is still wider than the column`);
});

test("the ellipsis does not hang off a space", () => {
  const out = L.truncateToWidth(measure10, "Alpha Beta Gamma", 70);
  assert.ok(!out.includes(" …"), `got "${out}"`);
});

test("empty input draws nothing", () => {
  assert.equal(L.truncateToWidth(measure10, "", 100), "");
  assert.equal(L.truncateToWidth(measure10, null, 100), "");
  assert.equal(L.truncateToWidth(measure10, undefined, 100), "");
});

test("a column too narrow for even one character still returns something drawable", () => {
  assert.equal(L.truncateToWidth(measure10, "Wide", 5), "…");
});

// ── metaLine ────────────────────────────────────────────────────────────────

test("venue, city and time are joined", () => {
  const line = L.metaLine({ venue_name: "The Pinhook", venue_city: "Durham", show_time: "8:00 PM" });
  assert.equal(line, "The Pinhook  ·  Durham  ·  8:00 pm");
});

test("absent fields leave no dangling separator", () => {
  assert.equal(L.metaLine({ venue_name: "Kings" }), "Kings");
  assert.equal(L.metaLine({ venue_name: "Kings", show_time: "9:00 PM" }), "Kings  ·  9:00 pm");
  assert.equal(L.metaLine({}), "");
});

// ── posterFilename ──────────────────────────────────────────────────────────

test("the filename carries the date it was made", () => {
  assert.equal(L.posterFilename("2026-09-21"), "my-triangle-shows-2026-09-21.png");
});

// ── todayKey ────────────────────────────────────────────────────────────────

test("todayKey is the local date, not the UTC one", () => {
  // 1 Jan 2027 at 20:00 local is already 2 Jan in UTC for this site's timezone, so a
  // toISOString()-based key would report tomorrow and hide tonight's show.
  const localEvening = new Date(2027, 0, 1, 20, 0, 0);
  assert.equal(L.todayKey(localEvening), "2027-01-01");
});
