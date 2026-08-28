/**
 * Venue-color conditioning for the calendar.
 *
 * Role: Loaded before app.js, which applies both functions here to every event as it
 * arrives. Pure color math -- no DOM beyond setting the glow attribute at the end.
 *
 * Venue colors are authored by hand in backend/app/seed.py and nothing checks them for
 * contrast. Two separate problems follow, and each gets its own pass:
 *
 * normalizeVenueColor() caps how light a color may be. Its original job was white text on
 * a filled tile; tiles are now gutter-ruled, so what it actually guards today is the rule
 * against the light-mode surface -- a pale venue color would vanish on warm paper, which
 * is exactly what The Carolina Theatre's old #F5D765 did.
 *
 * venueRuleColors() solves the opposite problem. Every seeded color is dark, because they
 * were drawn to sit behind white text, and a dark rule on a dark surface is invisible: all
 * 22 measured between 1.14:1 and 2.51:1 against --surface2, under the 3:1 a graphic needs.
 * It emits a brightened variant for dark mode and leaves the original for light.
 *
 * Both walk the value in small steps rather than solving directly, because luminance per
 * unit of HSL lightness varies enormously by hue -- yellow is far brighter than blue at
 * the same nominal lightness.
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

  // Originally derived from white text on a filled tile. The tiles are gutter-ruled now,
  // so the number is kept for a different reason: a color this dark is guaranteed to clear
  // 3:1 as a rule against every light-mode --surface2, measured at 4.85:1 or better across
  // all five palettes. Loosening it would let a pale venue color disappear on warm paper.
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

  // --- Gutter rule colors ---
  //
  // The venue palette was authored for the old filled tile: every color is a dark
  // background meant to sit behind white text. As a 3px rule on a dark surface those same
  // colors are nearly invisible -- measured across all 22, they land between 1.14:1 and
  // 2.51:1 against --surface2, where a graphic needs 3:1. All 22 failed.
  //
  // They are fine on the light surface (4.85:1 and up), so only dark mode needs a
  // brightened variant. Both are emitted per event and CSS picks by mode.

  // 4:1 against the lightest dark --surface2 across the five palettes (durham, #0e2340).
  // Targeting the lightest one means the result clears every palette, not just amber.
  const RULE_TARGET_LUMINANCE = 0.2166;

  // Darkening a color to normalize it also drains saturation, and a rule that has gone
  // grey stops identifying its venue. Restored before brightening.
  const RULE_MIN_SATURATION = 0.45;

  const _ruleCache = Object.create(null);

  /**
   * Rule colors for one venue: { dark, light }.
   *
   * `light` is the color unchanged -- it is already dark enough to read on warm paper.
   * `dark` is the same hue lifted until it clears the target against a dark surface.
   */
  function venueRuleColors(hex) {
    if (!hex) return { dark: hex, light: hex };
    if (_ruleCache[hex]) return _ruleCache[hex];

    const rgb = hexToRgb(hex);
    if (!rgb) {
      _ruleCache[hex] = { dark: hex, light: hex };
      return _ruleCache[hex];
    }

    const hsl = rgbToHsl(rgb);
    const hue = hsl[0];
    const saturation = Math.max(hsl[1], RULE_MIN_SATURATION);

    // Stepped rather than solved directly, for the same reason as normalizeVenueColor:
    // luminance per unit of HSL lightness varies a lot by hue.
    let out = rgb;
    let lightness = hsl[2];
    while (lightness < 1) {
      out = hslToRgb([hue, saturation, lightness]);
      if (relativeLuminance(out) >= RULE_TARGET_LUMINANCE) break;
      lightness += 0.01;
    }

    _ruleCache[hex] = { dark: rgbToHex(out), light: hex };
    return _ruleCache[hex];
  }

  // The gutter tile treatment is now unconditional in the stylesheet, so it needs no
  // attribute. The glow still keys off one, which keeps it a single line to turn off.
  document.documentElement.setAttribute("data-glow", "on");

  // --- Exports ---
  global.normalizeVenueColor = normalizeVenueColor;
  global.venueRuleColors = venueRuleColors;
})(window);
