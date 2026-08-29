// Attribute-injection regression tests for ../js/modal.js.
//
// Run with:  node --test frontend/tests/modal.test.js
//
// These are not wired into .github/workflows/ci.yml, which installs Python and runs
// pytest only. They need no dependencies beyond Node's own test runner, so they can be
// run by hand or added to CI as a step whenever that is worth doing.
//
// modal.js is a plain <script> (no module.exports) that touches `document` at load
// time, so it cannot be require()d directly. The loader below evaluates it into a vm
// context holding a minimal DOM stub instead — top-level function declarations become
// properties of the sandbox object, so the assertions run against modal.js's actual
// `_buildEventRow` / `openModal` template code rather than a reimplementation of it.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const MODAL_JS = path.join(__dirname, "..", "js", "modal.js");

// A stand-in for a DOM element, carrying just the surface modal.js touches.
// `classList.addedClasses` records every class added, so a test can assert the script
// actually acted on a specific element rather than merely that a stub had the method.
function stubElement() {
  const addedClasses = [];
  const removedClasses = [];
  return {
    innerHTML: "",
    classList: {
      addedClasses,
      removedClasses,
      add(cls) { addedClasses.push(cls); },
      remove(cls) { removedClasses.push(cls); },
    },
    addEventListener() {},
  };
}

// Evaluates modal.js into a fresh vm context alongside the three elements it looks up
// at load time, and returns both the populated sandbox and those elements.
function loadModal() {
  const elements = {
    "event-modal": stubElement(),
    "modal-overlay": stubElement(),
    "modal-content": stubElement(),
  };
  const sandbox = {
    document: {
      getElementById: (id) => elements[id] || stubElement(),
      querySelector: () => null,
      querySelectorAll: () => [],
      addEventListener() {},
    },
  };
  vm.createContext(sandbox);
  // filename surfaces the real path in stack traces from inside the sandbox.
  vm.runInContext(fs.readFileSync(MODAL_JS, "utf8"), sandbox, { filename: MODAL_JS });
  return { sandbox, elements };
}

function singleEvent(extendedProps) {
  return {
    event: {
      id: "42",
      extendedProps: {
        date: "2026-08-08",
        name: "Show Name",
        venue_slug: "cats-cradle",
        venue_name: "Cat's Cradle",
        venue_city: "Carrboro",
        ...extendedProps,
      },
    },
  };
}

function groupRowEvent(extendedProps) {
  return {
    id: "42",
    title: "Show Title",
    extendedProps: { venue_slug: "cats-cradle", ...extendedProps },
  };
}

// A `"` placed after a real https:// prefix still passes the safeUrl prefix check,
// which is exactly why that check alone was never sufficient — only escaping closes
// the gap.
const TICKET_URL_BREAKOUT = 'https://evil.example/show?ref=1" onmouseover="alert(document.cookie)';
// image_url has no prefix check at all in modal.js; a broken `src` fires `onerror` the
// moment the modal opens, no click required.
const IMAGE_URL_BREAKOUT = 'x" onerror="alert(document.cookie)';

function assertNoAttributeBreakout(html) {
  // The vulnerability signature: a *raw* `"` immediately followed by an
  // onerror=/onmouseover= attribute — what a successful break-out renders as (the
  // injected quote closing src/href early, followed by a sibling event-handler
  // attribute). A raw `"` only ever appears there if escaping failed; once _h() runs
  // that quote is `&quot;`, an entity rather than a `"` character, so this regex —
  // unlike a plain substring check — does not false-positive on the harmless case
  // where "onerror=" appears as escaped text inside a still-intact attribute value.
  assert.equal(/"\s+on(error|mouseover)\s*=/.test(html), false);
}

test("_buildEventRow escapes a double-quote in ticket_url so it cannot break out of the <a> attribute", () => {
  const { sandbox } = loadModal();
  const html = sandbox._buildEventRow(groupRowEvent({ ticket_url: TICKET_URL_BREAKOUT }));

  assertNoAttributeBreakout(html);
  assert.match(
    html,
    /href="https:\/\/evil\.example\/show\?ref=1&quot; onmouseover=&quot;alert\(document\.cookie\)"/
  );
});

test("openModal escapes a double-quote in ticket_url so it cannot break out of the <a> attribute", () => {
  const { sandbox, elements } = loadModal();
  sandbox.openModal(singleEvent({ ticket_url: TICKET_URL_BREAKOUT }));
  const html = elements["modal-content"].innerHTML;

  assertNoAttributeBreakout(html);
  assert.match(
    html,
    /href="https:\/\/evil\.example\/show\?ref=1&quot; onmouseover=&quot;alert\(document\.cookie\)"/
  );
});

test("openModal escapes a double-quote in image_url so the onerror payload cannot reach the <img> attribute", () => {
  const { sandbox, elements } = loadModal();
  sandbox.openModal(singleEvent({ image_url: IMAGE_URL_BREAKOUT }));
  const html = elements["modal-content"].innerHTML;

  assertNoAttributeBreakout(html);
  // The whole payload, quotes included, must land inside one escaped src="..."
  // attribute rather than spilling into a sibling onerror="..." attribute.
  assert.match(
    html,
    /<img src="x&quot; onerror=&quot;alert\(document\.cookie\)" alt="Show Name" class="modal-image">/
  );
});

test("openModal renders a well-formed ticket_url with query params as an identical working link", () => {
  const { sandbox, elements } = loadModal();
  sandbox.openModal(singleEvent({ ticket_url: "https://tickets.example.com/42?ref=abc&utm_source=site" }));
  const html = elements["modal-content"].innerHTML;

  // & is escaped to &amp; in the attribute (correct HTML), which the browser resolves
  // back to the identical URL — it is not a behavior change.
  assert.match(html, /href="https:\/\/tickets\.example\.com\/42\?ref=abc&amp;utm_source=site"/);
  // Confirms the modal actually opened: classList.add("active") ran against the real
  // #event-modal element, not just that the stub still has an `add` method.
  assert.deepEqual(elements["event-modal"].classList.addedClasses, ["active"]);
});

test("openModal renders a well-formed image_url with query params as an identical working <img src>", () => {
  const { sandbox, elements } = loadModal();
  sandbox.openModal(singleEvent({ image_url: "https://cdn.example.com/poster.jpg?w=800&h=600" }));
  const html = elements["modal-content"].innerHTML;

  assert.match(
    html,
    /<img src="https:\/\/cdn\.example\.com\/poster\.jpg\?w=800&amp;h=600" alt="Show Name" class="modal-image">/
  );
});

// --- _h() must not throw on a non-string value ---
//
// The fields _h() receives come from JSON the API returns, and nothing in the client
// type-checks them. `(s || "")` passed any *truthy* non-string straight to .replace(),
// which exists only on String.prototype — so a numeric image_url threw a TypeError
// inside openModal and the modal never opened at all. image_url is the exposed call
// site because its guard (`props.image_url ? …`) is a bare truthiness check; safeUrl is
// incidentally shielded because RegExp.test() coerces before _h() ever runs.

test("openModal renders a non-string image_url as escaped text instead of throwing", () => {
  const { sandbox, elements } = loadModal();

  assert.doesNotThrow(() => sandbox.openModal(singleEvent({ image_url: 12345 })));
  const html = elements["modal-content"].innerHTML;
  assert.match(html, /<img src="12345" alt="Show Name" class="modal-image">/);
  // The rest of the modal still rendered — a throw here aborted openModal partway and
  // left modal-content empty.
  assert.match(html, /<h2>Show Name<\/h2>/);
});

test("_h escapes a non-string that carries an attribute-breakout payload in its toString", () => {
  const { sandbox } = loadModal();
  // An object whose toString() carries the payload: coercion must happen *before*
  // escaping, never instead of it.
  const hostile = { toString: () => 'x" onerror="alert(1)' };

  assert.equal(sandbox._h(hostile), "x&quot; onerror=&quot;alert(1)");
});

test("_h maps null and undefined to the empty string", () => {
  const { sandbox } = loadModal();

  assert.equal(sandbox._h(null), "");
  assert.equal(sandbox._h(undefined), "");
});
