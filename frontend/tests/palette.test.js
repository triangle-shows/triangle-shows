// Tests for the seasonal palette rules in ../js/config.js.
//
// The Halloween palette is October-only: in season it is the default for visitors who
// haven't chosen a palette, and that default must not be saved -- otherwise everyone who
// visited in October would still be orange in March. These pin the month boundary and
// the no-persist behaviour.
//
// config.js is a plain <script>; at load time it reads window.location.hostname and
// registers DOMContentLoaded and resize listeners, so the sandbox stubs exactly those.
//
// Run with: node --test frontend/tests/palette.test.js

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const CONFIG_SRC = fs.readFileSync(path.join(__dirname, "../js/config.js"), "utf8");

function loadConfig() {
  const store = {};
  const sandbox = {
    window: {
      location: { hostname: "triangle-shows.net", origin: "https://triangle-shows.net" },
      addEventListener() {},
    },
    document: {
      documentElement: { dataset: {} },
      addEventListener() {},
      querySelectorAll: () => [],
    },
    localStorage: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: (k) => { delete store[k]; },
    },
  };
  sandbox.location = sandbox.window.location;
  vm.createContext(sandbox);
  // const/function declarations in a script don't become sandbox properties, so hand the
  // ones under test back explicitly.
  vm.runInContext(
    CONFIG_SRC + "\n;globalThis.__t = { PALETTES, isPaletteInSeason, seasonalDefaultPalette, applyPalette };",
    sandbox
  );
  return { ...sandbox.__t, store, html: sandbox.document.documentElement };
}

const day = (y, m, d) => new Date(y, m - 1, d, 12);

test("halloween is a full palette entry", () => {
  const { PALETTES } = loadConfig();
  assert.ok(PALETTES.halloween);
  assert.equal(Object.keys(PALETTES.halloween.vars).length, Object.keys(PALETTES.amber.vars).length);
});

test("halloween is in season for all of October and none of the months either side", () => {
  const { isPaletteInSeason } = loadConfig();
  assert.equal(isPaletteInSeason("halloween", day(2026, 10, 1)), true);
  assert.equal(isPaletteInSeason("halloween", day(2026, 10, 31)), true);
  assert.equal(isPaletteInSeason("halloween", day(2026, 9, 30)), false);
  assert.equal(isPaletteInSeason("halloween", day(2026, 11, 1)), false);
});

test("non-seasonal palettes are always in season", () => {
  const { isPaletteInSeason } = loadConfig();
  for (const key of ["amber", "phosphor", "midnight", "wisteria", "durham"]) {
    assert.equal(isPaletteInSeason(key, day(2026, 3, 15)), true, key);
    assert.equal(isPaletteInSeason(key, day(2026, 10, 15)), true, key);
  }
});

test("the seasonal default is halloween in October and nothing otherwise", () => {
  const { seasonalDefaultPalette } = loadConfig();
  assert.equal(seasonalDefaultPalette(day(2026, 10, 15)), "halloween");
  assert.equal(seasonalDefaultPalette(day(2026, 11, 1)), null);
});

test("a default applied without persist is shown but not remembered", () => {
  const { applyPalette, store, html } = loadConfig();
  applyPalette("halloween", { persist: false });
  assert.equal(html.dataset.palette, "halloween");
  assert.equal(store["triangle-shows-palette"], undefined);
});

test("a palette the visitor picks is still remembered", () => {
  const { applyPalette, store } = loadConfig();
  applyPalette("halloween");
  assert.equal(store["triangle-shows-palette"], "halloween");
});
