// Tests for the client-side .ics generation in ../js/favorites.js.
//
// favorites.js is a plain <script> (no module.exports), but it touches no browser
// global at load time — every DOM/localStorage access sits inside a function. So it
// loads cleanly into a vm context whose sandbox supplies only the globals
// downloadFavorites actually reaches for (localStorage, Blob, URL, document,
// setTimeout), and the generated calendar body can be captured from the Blob stub.
//
// What is under test is iCalendar *property injection*. The .ics is assembled by
// joining property lines with CRLF, so any CR or LF that survives into a value
// terminates that property early and everything after it is parsed as a fresh
// iCalendar property of the attacker's choosing.
//
// The values here come from localStorage: ticket_url is stored as the scraper found it
// (no validation on the model or the schema), app.js writes it verbatim into the
// favorites blob (app.js:477), and getFavorites() JSON.parses it back with no
// revalidation — so the value under test is whatever a scraped venue page carried.
//
// Run with: node --test frontend/tests/favorites.test.js
// node:test is a Node built-in, so this file needs no package.json and no deps.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const FAVORITES_SRC = fs.readFileSync(path.join(__dirname, "../js/favorites.js"), "utf8");

// Loads favorites.js into a fresh vm context seeded with `favorites` as the stored
// blob. Returns the sandbox plus a `captured` handle that receives the Blob parts
// downloadFavorites builds, so tests can assert on the exact calendar body.
function loadFavorites(favorites = {}) {
  const captured = {};
  const sandbox = {
    localStorage: {
      getItem: (key) =>
        key === "triangle-shows-favorites" ? JSON.stringify(favorites) : null,
      setItem() {},
      removeItem() {},
    },
    Blob: class {
      constructor(parts, opts) {
        captured.parts = parts;
        captured.opts = opts;
      }
    },
    URL: { createObjectURL: () => "blob:stub", revokeObjectURL() {} },
    document: {
      createElement: () => ({ click() {} }),
      body: { appendChild() {}, removeChild() {} },
      getElementById: () => null,
      querySelectorAll: () => [],
    },
    setTimeout() {},
  };
  vm.createContext(sandbox);
  vm.runInContext(FAVORITES_SRC, sandbox);
  return { sandbox, captured };
}

// Runs downloadFavorites over one favorite and returns the generated calendar as an
// array of property lines, split the way an iCalendar parser splits it.
function generateLines(favorite) {
  const { sandbox, captured } = loadFavorites({ [favorite.id]: favorite });
  sandbox.downloadFavorites();
  return captured.parts[0].split("\r\n");
}

const BASE_FAVORITE = {
  id: "42",
  title: "Wednesday",
  date: "2026-08-08",
  show_time: "20:00:00",
  venue_name: "Cat's Cradle",
  venue_city: "Carrboro",
};

test("downloadFavorites does not let a CRLF in ticket_url inject an iCalendar property", () => {
  const lines = generateLines({
    ...BASE_FAVORITE,
    // A newline in the value ends the URL property; everything after it would be
    // parsed as sibling properties — here an ATTENDEE and a replacement SUMMARY.
    ticket_url:
      "https://tickets.example.com/42\r\nATTENDEE:mailto:evil@example.com\r\nSUMMARY:Injected",
  });

  const injected = lines.filter(
    (line) => line.startsWith("ATTENDEE") || line === "SUMMARY:Injected"
  );
  assert.deepEqual(injected, []);
  // The whole payload stays inside the one URL property it was written into.
  const urlLines = lines.filter((line) => line.startsWith("URL:"));
  assert.equal(urlLines.length, 1);
  assert.equal(
    urlLines[0],
    "URL:https://tickets.example.com/42ATTENDEE:mailto:evil@example.comSUMMARY:Injected"
  );
});

test("downloadFavorites emits a well-formed ticket_url in URL: byte-for-byte", () => {
  // RFC 5545 types URL as URI (3.3.13), not TEXT (3.3.11), so it must NOT pick up
  // _esc()'s backslash escaping of `,` `;` `\` — that would corrupt any real ticket
  // link containing them. This asserts the injection fix did not reach for _esc.
  const url = "https://tickets.example.com/42?ref=a,b;c&utm_source=site";
  const lines = generateLines({ ...BASE_FAVORITE, ticket_url: url });

  assert.ok(lines.includes(`URL:${url}`));
});

test("downloadFavorites does not let a CRLF in a favorite's title inject an iCalendar property", () => {
  const lines = generateLines({
    ...BASE_FAVORITE,
    title: "Show\r\nATTENDEE:mailto:evil@example.com",
  });

  assert.deepEqual(lines.filter((line) => line.startsWith("ATTENDEE")), []);
  assert.ok(lines.includes("SUMMARY:Show\\nATTENDEE:mailto:evil@example.com"));
});

test("_esc collapses CRLF, bare CR, and bare LF to the escaped \\n sequence", () => {
  const { sandbox } = loadFavorites();

  // A bare CR survived the old /\n/g replace untouched, leaving a raw control
  // character in a TEXT value (RFC 5545 forbids those outright) that a lenient
  // parser can still read as a line break.
  assert.equal(sandbox._esc("a\r\nb"), "a\\nb");
  assert.equal(sandbox._esc("a\rb"), "a\\nb");
  assert.equal(sandbox._esc("a\nb"), "a\\nb");
  // The backslash pass still runs first, so a literal backslash doubles and the
  // newline escape that follows is not itself re-escaped.
  assert.equal(sandbox._esc("a\\b\nc"), "a\\\\b\\nc");
  // TEXT escaping of , and ; is unchanged.
  assert.equal(sandbox._esc("a,b;c"), "a\\,b\\;c");
});

test("_esc coerces a non-string instead of throwing", () => {
  const { sandbox } = loadFavorites();

  // Favorites come back out of localStorage with no type check, and `.replace` only
  // exists on String.prototype — so a stored number reached the old _esc and threw.
  assert.equal(sandbox._esc(12345), "12345");
  assert.equal(sandbox._esc(null), "");
});
