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
// Overflow arithmetic. The poster gives up one row to a "+N more" line when the list is
// too long, so the number in that line has to count the show whose row it took. Off by
// one here means a poster that silently loses a show.

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

// ── monthRange ──────────────────────────────────────────────────────────────

test("one month reads as one month", () => {
  assert.equal(L.monthRange([fav("a", "2026-10-02"), fav("b", "2026-10-30")]), "oct");
});

test("several months read as a range", () => {
  assert.equal(L.monthRange([fav("a", "2026-10-02"), fav("b", "2026-12-11")]), "oct — dec");
});

test("no shows, no range", () => {
  assert.equal(L.monthRange([]), "");
});

// ── planLayout ──────────────────────────────────────────────────────────────

const TOP = 328;
const BOTTOM = 1240;          // the real region, ~912px tall
const AVAILABLE = BOTTOM - TOP;

test("a short list is centred rather than stranded at the top", () => {
  const plan = L.planLayout(3, TOP, BOTTOM);
  assert.equal(plan.visible, 3);
  assert.equal(plan.overflow, 0);
  assert.equal(plan.rowHeight, L.ROW_MAX, "rows open up but stop at the max");
  const used = 3 * plan.rowHeight;
  assert.equal(plan.blockTop, TOP + (AVAILABLE - used) / 2);
});

test("rows tighten as the list grows, down to the floor", () => {
  const few = L.planLayout(6, TOP, BOTTOM);
  const many = L.planLayout(14, TOP, BOTTOM);
  assert.ok(many.rowHeight < few.rowHeight);
  assert.ok(many.rowHeight >= L.ROW_MIN);
});

test("the last list that fits whole is not truncated", () => {
  const maxRows = Math.floor(AVAILABLE / L.ROW_MIN);
  const plan = L.planLayout(maxRows, TOP, BOTTOM);
  assert.equal(plan.visible, maxRows);
  assert.equal(plan.overflow, 0);
});

test("an overlong list gives up one row and counts the show it displaced", () => {
  const maxRows = Math.floor(AVAILABLE / L.ROW_MIN);
  const count = maxRows + 5;
  const plan = L.planLayout(count, TOP, BOTTOM);

  assert.equal(plan.visible, maxRows - 1, "one row goes to the '+N more' line");
  // The whole point: drawn rows plus the overflow count must equal the real total, or
  // the poster quietly loses the show whose row became the overflow line.
  assert.equal(plan.visible + plan.overflow, count);
});

test("rows never overflow the region they were given", () => {
  for (const n of [1, 2, 5, 10, 15, 16, 40, 200]) {
    const plan = L.planLayout(n, TOP, BOTTOM);
    const drawn = plan.visible + (plan.overflow > 0 ? 1 : 0);
    const bottom = plan.blockTop + drawn * plan.rowHeight;
    assert.ok(bottom <= BOTTOM + 0.001, `n=${n} ran past the footer (${bottom} > ${BOTTOM})`);
    assert.ok(plan.blockTop >= TOP - 0.001, `n=${n} started above the region`);
  }
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
