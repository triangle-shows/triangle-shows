/**
 * Venue-color normalization and the design-lab toggles.
 *
 * Role: Loaded before app.js. Exposes normalizeVenueColor(), which app.js applies to
 * every event as it arrives, and wires the sidebar's design-lab controls to attributes
 * on <html> that styles.css keys off.
 *
 * Why normalization exists. Venue colors are authored by hand in backend/app/seed.py and
 * nothing checks them for contrast. The Carolina Theatre's #F5D765 renders white-on-yellow
 * at 1.42:1 in dark mode, and venue-colored-text-on-warm-paper at roughly 1.2:1 in light
 * mode -- unreadable in both. Rather than hand-fixing that one value and waiting for the
 * next light venue color to reopen the same hole, every color is clamped to a luminance
 * band on the way in. Hue and saturation survive, so venues stay distinguishable.
 *
 * The band is derived, not guessed: TARGET_CONTRAST is set by the dimmest text that sits
 * on a tile (the venue line, --tile-ink-dim), not by the title. Meet the floor for the
 * quieter of the two and the louder one follows.
 */

(function (global) {
  "use strict";

  // --- Contrast math (WCAG 2.1 relative luminance) ---

  function _srgbToLinear(channel) {
    return channel <= 0.04045
      ? channel / 12.92
      : Math.pow((channel + 0.055) / 1.055, 2.4);
  }

  function relativeLuminance(rgb) {
    const r = _srgbToLinear(rgb[0] / 255);
    const g = _srgbToLinear(rgb[1] / 255);
    const b = _srgbToLinear(rgb[2] / 255);
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  }

  function contrastWithWhite(rgb) {
    return 1.05 / (relativeLuminance(rgb) + 0.05);
  }

  // --- Color space conversion ---

  function hexToRgb(hex) {
    const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || "").trim());
    if (!m) return null;
    const n = parseInt(m[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  function rgbToHex(rgb) {
    return (
      "#" +
      rgb
        .map(function (c) {
          return Math.max(0, Math.min(255, Math.round(c)))
            .toString(16)
            .padStart(2, "0");
        })
        .join("")
    );
  }

  function rgbToHsl(rgb) {
    const r = rgb[0] / 255, g = rgb[1] / 255, b = rgb[2] / 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b);
    const l = (max + min) / 2;
    if (max === min) return [0, 0, l];

    const d = max - min;
    const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    let h;
    if (max === r)      h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
    else if (max === g) h = ((b - r) / d + 2) / 6;
    else                h = ((r - g) / d + 4) / 6;
    return [h, s, l];
  }

  function hslToRgb(hsl) {
    const h = hsl[0], s = hsl[1], l = hsl[2];
    if (s === 0) return [l * 255, l * 255, l * 255];

    const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    const p = 2 * l - q;

    function hue(t) {
      if (t < 0) t += 1;
      if (t > 1) t -= 1;
      if (t < 1 / 6) return p + (q - p) * 6 * t;
      if (t < 1 / 2) return q;
      if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
      return p;
    }

    return [hue(h + 1 / 3) * 255, hue(h) * 255, hue(h - 1 / 3) * 255];
  }

  // --- Normalization ---

  // The venue line uses --tile-ink-dim (#e2dace, luminance ~0.70). Holding that to the
  // WCAG 4.5:1 minimum for small text caps tile luminance at ~0.117, which is a white
  // contrast of ~6.3. Rounded up for margin.
  const TARGET_CONTRAST = 6.5;

  // Below this, a hue reads as grey and two venues become hard to tell apart. Darkening a
  // pale color drops its saturation, so it is restored afterwards.
  const MIN_SATURATION = 0.30;

  const _cache = Object.create(null);

  /**
   * Darken a venue color until white text on it clears TARGET_CONTRAST.
   *
   * Returns the input unchanged when it already passes, which is 21 of the 22 seeded
   * venues -- this is a floor, not a restyle. Unparseable input is returned as-is so a
   * missing color never throws inside a render path.
   */
  function normalizeVenueColor(hex) {
    if (!hex) return hex;
    if (_cache[hex] !== undefined) return _cache[hex];

    const rgb = hexToRgb(hex);
    if (!rgb) {
      _cache[hex] = hex;
      return hex;
    }

    if (contrastWithWhite(rgb) >= TARGET_CONTRAST) {
      _cache[hex] = hex;
      return hex;
    }

    const hsl = rgbToHsl(rgb);
    const hue = hsl[0];
    const saturation = Math.max(hsl[1], MIN_SATURATION);

    // Walk lightness down in small steps rather than solving directly: perceived
    // luminance varies enormously by hue at a fixed HSL lightness (yellow is far brighter
    // than blue), so stepping until the measurement passes is both simpler and correct
    // for every hue.
    let lightness = hsl[2];
    let out = rgb;
    while (lightness > 0.04) {
      lightness -= 0.01;
      out = hslToRgb([hue, saturation, lightness]);
      if (contrastWithWhite(out) >= TARGET_CONTRAST) break;
    }

    const result = rgbToHex(out);
    _cache[hex] = result;
    return result;
  }

  // --- Design lab ---
  //
  // Exploration controls for this branch: they let the two tile treatments and the two CRT
  // textures be compared on real data instead of in the abstract. Each writes one attribute
  // on <html> and persists it. Remove this block, its markup, and the [data-tiles="gutter"]
  // / [data-glow] / [data-scanlines] rules once the direction is settled.

  // Glow defaults on -- it was kept after the comparison pass. Scanlines were dropped
  // outright rather than defaulted off, so no stale attribute is left on <html>.
  const LAB_KEYS = {
    tiles: { attr: "data-tiles", storage: "ts-lab-tiles", fallback: "fill" },
    glow:  { attr: "data-glow",  storage: "ts-lab-glow",  fallback: "on"   },
  };

  function _read(key) {
    const spec = LAB_KEYS[key];
    try {
      return localStorage.getItem(spec.storage) || spec.fallback;
    } catch (e) {
      return spec.fallback;
    }
  }

  function setLabOption(key, value) {
    const spec = LAB_KEYS[key];
    if (!spec) return;

    document.documentElement.setAttribute(spec.attr, value);
    try {
      localStorage.setItem(spec.storage, value);
    } catch (e) {
      /* Private browsing: the choice still applies, it just will not persist. */
    }

    document.querySelectorAll('.lab-btn[data-lab-key="' + key + '"]').forEach(function (btn) {
      const active = btn.getAttribute("data-lab-value") === value;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", active ? "true" : "false");
    });

    // The tile treatments differ in text color, so the equalizer's own tinting follows.
    if (key === "tiles" && global.Equalizer && global.Equalizer.refresh) {
      global.Equalizer.refresh();
    }
  }

  function initDesignLab() {
    Object.keys(LAB_KEYS).forEach(function (key) {
      setLabOption(key, _read(key));
    });

    document.querySelectorAll(".lab-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setLabOption(btn.getAttribute("data-lab-key"), btn.getAttribute("data-lab-value"));
      });
    });
  }

  // Applied before first paint so tiles never flash the wrong treatment.
  Object.keys(LAB_KEYS).forEach(function (key) {
    document.documentElement.setAttribute(LAB_KEYS[key].attr, _read(key));
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initDesignLab);
  } else {
    initDesignLab();
  }

  // --- Exports ---
  global.normalizeVenueColor = normalizeVenueColor;
  global.setLabOption = setLabOption;
})(window);
