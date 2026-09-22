// Shareable lineup poster: renders the visitor's upcoming favourites to a PNG.
//
// The poster is drawn from the site's own materials rather than a separate design --
// the ASCII banner is read out of the header, and every colour is pulled from the live
// CSS custom properties, so a poster made in wisteria light mode looks like the site
// that made it. Fonts are the same two faces the page already loads.
//
// Output is 1080x1350, the tallest aspect ratio Instagram shows uncropped in feed, which
// is also close enough to a 4:5 print poster to read as one.
//
// Nothing here touches the DOM at load time, so the pure helpers can be evaluated in a
// bare vm context by frontend/tests/lineup.test.js.

(function (global) {
  "use strict";

  // --- Poster geometry ---
  // Fixed rather than derived: these are the numbers the layout was drawn against, and
  // a poster that reflows by viewport would stop being a predictable artefact.
  const W = 1080;
  const H = 1350;
  const PAD = 64;

  const BANNER_TOP = 76;
  const HEADING_GAP = 46;      // banner baseline block to heading
  const FOOTER_BASELINE = H - 64;

  // Row metrics. The block between the heading rule and the footer is divided by the
  // number of shows, then clamped: ROW_MIN keeps the two text lines from touching, and
  // ROW_MAX stops four favourites from becoming four enormous bands of empty paper.
  const ROW_MIN = 60;
  const ROW_MAX = 150;
  const DATE_COL = 168;        // "FRI 10.16" plus breathing room
  const RULE_W = 4;            // venue-coloured gutter rule, as on the calendar tiles
  const RULE_GAP = 22;

  /**
   * Type sizes for a given row height.
   *
   * Fixed sizes made a two-show poster look like a fifteen-show one that had failed to
   * load: the same small type stranded in the middle of an empty page. Growing the type
   * with the row turns that space into margin instead of a gap.
   */
  function rowTypeScale(rowHeight) {
    const t = Math.min(1, Math.max(0, (rowHeight - ROW_MIN) / (ROW_MAX - ROW_MIN)));
    return {
      title: Math.round(28 + 16 * t),
      meta: Math.round(18 + 6 * t),
      date: Math.round(21 + 7 * t),
    };
  }

  const MONTHS_SHORT = ["jan", "feb", "mar", "apr", "may", "jun",
                        "jul", "aug", "sep", "oct", "nov", "dec"];
  const DAY_NAMES = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"];

  // --- Pure helpers (unit-tested) ---

  /** Local-date key, deliberately not toISOString() -- see the same note in equalizer.js:
   *  converting to UTC first shifts the date by one for anyone west of Greenwich in the
   *  evening, which is exactly when this site is used. */
  function todayKey(now) {
    const d = now || new Date();
    return (
      d.getFullYear() +
      "-" + String(d.getMonth() + 1).padStart(2, "0") +
      "-" + String(d.getDate()).padStart(2, "0")
    );
  }

  /**
   * The favourites worth putting on a poster: today onward, soonest first.
   *
   * Compared as ISO strings rather than Date objects. Both sides are YYYY-MM-DD, where
   * lexical order is chronological order, and it avoids constructing a Date from a
   * bare date string -- which parses as UTC midnight and lands on the previous day for
   * every visitor in this site's own timezone.
   *
   * A stored favourite whose date is missing or malformed is dropped rather than
   * defaulted: localStorage is never revalidated on read, so this has to tolerate
   * whatever is in there without putting a row with no date on someone's poster.
   */
  function upcomingFavorites(favs, today) {
    return Object.values(favs || {})
      .filter((ev) => ev && typeof ev.date === "string" && /^\d{4}-\d{2}-\d{2}$/.test(ev.date))
      .filter((ev) => ev.date >= today)
      .sort((a, b) => {
        if (a.date !== b.date) return a.date < b.date ? -1 : 1;
        // Same night: keep it stable and readable by falling back to the title, since
        // show_time is a display string ("7:00 PM") and does not sort chronologically.
        return String(a.title || "").localeCompare(String(b.title || ""));
      });
  }

  /** "2026-10-16" -> { day: "FRI", date: "10.16" }. Split so the two can be drawn at
   *  different weights without re-parsing. */
  function formatPosterDate(dateStr) {
    const [y, m, d] = String(dateStr).split("-").map(Number);
    // Constructed as local midnight (not `new Date("2026-10-16")`, which is UTC) so the
    // weekday matches the one shown on the calendar.
    const dt = new Date(y, m - 1, d);
    return {
      day: DAY_NAMES[dt.getDay()],
      date: `${m}.${String(d).padStart(2, "0")}`,
    };
  }

  /** A human month range for the heading, e.g. "oct — dec" or just "oct". */
  function monthRange(events) {
    if (!events.length) return "";
    const first = Number(events[0].date.slice(5, 7)) - 1;
    const last = Number(events[events.length - 1].date.slice(5, 7)) - 1;
    return first === last ? MONTHS_SHORT[first] : `${MONTHS_SHORT[first]} — ${MONTHS_SHORT[last]}`;
  }

  /**
   * Decide how many shows fit and how tall each row is.
   *
   * Returns { rowHeight, visible, overflow, blockTop }. When the shows do not fill the
   * available height the block is centred in it, so a three-show poster reads as
   * composed rather than as a full one that failed to load.
   */
  function planLayout(count, regionTop, regionBottom) {
    const available = regionBottom - regionTop;
    const maxRows = Math.max(1, Math.floor(available / ROW_MIN));

    // One row is given up to the "+N more" line whenever the list is truncated.
    const overflow = Math.max(0, count - maxRows);
    const visible = overflow > 0 ? maxRows - 1 : count;
    const shown = visible + (overflow > 0 ? 1 : 0);

    const rowHeight = Math.min(ROW_MAX, Math.max(ROW_MIN, available / Math.max(shown, 1)));
    const used = shown * rowHeight;
    const blockTop = regionTop + Math.max(0, (available - used) / 2);

    return { rowHeight, visible, overflow: overflow > 0 ? count - visible : 0, blockTop };
  }

  /** Shorten `text` until it fits `maxWidth`, appending a single-character ellipsis.
   *  Returns "" for empty input so callers can skip drawing entirely. */
  function truncateToWidth(measure, text, maxWidth) {
    const str = String(text == null ? "" : text);
    if (!str) return "";
    if (measure(str) <= maxWidth) return str;
    let lo = 0;
    let hi = str.length;
    while (lo < hi) {
      const mid = Math.ceil((lo + hi) / 2);
      if (measure(str.slice(0, mid) + "…") <= maxWidth) lo = mid;
      else hi = mid - 1;
    }
    // trimEnd so the ellipsis does not hang off a space.
    return lo > 0 ? str.slice(0, lo).trimEnd() + "…" : "…";
  }

  /** The meta line under a title: venue, city and start time, skipping what is absent. */
  function metaLine(ev) {
    return [
      ev.venue_name,
      ev.venue_city,
      ev.show_time ? String(ev.show_time).toLowerCase() : null,
    ].filter(Boolean).join("  ·  ");
  }

  function posterFilename(today) {
    return `my-triangle-shows-${today}.png`;
  }

  // --- Theme + asset access (DOM, but only when called) ---

  /** Read the live palette. Falls back to the amber defaults so a poster still renders
   *  if a custom property is missing or the browser refuses getComputedStyle. */
  function readTheme() {
    const fallback = {
      bg: "#1a1008", surface: "#241609", border: "#3d2a12",
      text: "#e8d5b0", muted: "#9a7a50", accent: "#c87941",
    };
    try {
      const cs = getComputedStyle(document.documentElement);
      const pick = (name, dflt) => (cs.getPropertyValue(name) || "").trim() || dflt;
      return {
        bg: pick("--bg", fallback.bg),
        surface: pick("--surface", fallback.surface),
        border: pick("--border", fallback.border),
        text: pick("--text", fallback.text),
        muted: pick("--muted", fallback.muted),
        accent: pick("--accent", fallback.accent),
      };
    } catch {
      return fallback;
    }
  }

  /** The header's ASCII banner, read from the page so there is only ever one copy of it.
   *  Returns [] when the header is absent, and the caller draws a wordmark instead. */
  function readBanner() {
    const el = document.querySelector(".ascii-title");
    if (!el) return [];
    // textContent drops the empty cursor <span> without disturbing the art's spacing.
    const lines = el.textContent.replace(/\s+$/, "").split("\n");
    return lines.length && lines.some((l) => l.trim()) ? lines : [];
  }

  /**
   * The colour the calendar draws this event in.
   *
   * Favourites saved before this feature existed carry no venue_color, so it is
   * recovered from the loaded calendar first and the venue list second. Both are
   * best-effort: a favourite for a date outside the fetched window is in neither, which
   * is why there is an accent fallback rather than an error.
   */
  function venueColorFor(ev, accent) {
    if (ev.venue_color) return ev.venue_color;
    try {
      if (typeof _allEventsCache !== "undefined" && Array.isArray(_allEventsCache)) {
        const hit = _allEventsCache.find((e) => String(e.id) === String(ev.id));
        if (hit && hit.extendedProps && hit.extendedProps.venue_color) {
          return hit.extendedProps.venue_color;
        }
      }
      if (typeof venues !== "undefined" && Array.isArray(venues)) {
        const hit = venues.find((v) => v.name === ev.venue_name);
        if (hit && hit.color) return hit.color;
      }
    } catch {
      /* Either global may be undeclared depending on script order; accent is fine. */
    }
    return accent;
  }

  /** Canvas will silently substitute a fallback face for a webfont that has not loaded,
   *  so the specific weights used below are requested before the first draw. */
  async function ensureFonts() {
    if (!document.fonts || typeof document.fonts.load !== "function") return;
    const faces = [
      '700 20px "Space Mono"',
      '400 20px "Space Mono"',
      '600 20px "IBM Plex Mono"',
      '400 20px "IBM Plex Mono"',
    ];
    try {
      await Promise.all(faces.map((f) => document.fonts.load(f)));
      await document.fonts.ready;
    } catch {
      /* Substituted monospace is a worse poster, not a broken one. */
    }
  }

  // --- Rendering ---

  function drawBanner(ctx, lines, theme) {
    const widest = lines.reduce((n, l) => Math.max(n, l.length), 0);
    if (!widest) return BANNER_TOP;

    // Measure at a known size and scale, rather than assuming Space Mono's advance
    // ratio -- if the face was substituted, this still fits the width.
    ctx.font = '400 100px "Space Mono", monospace';
    const charAt100 = ctx.measureText("M").width || 60;
    const size = Math.floor(((W - PAD * 2) / widest) * (100 / charAt100));
    // Looser than .ascii-title's 1.22 in styles.css. The art is drawn in slashes and
    // underscores, and at poster scale those strokes close up the gap between rows and
    // read as one grey band; the extra leading is what keeps the wordmark legible.
    const lineHeight = size * 1.35;

    ctx.font = `400 ${size}px "Space Mono", monospace`;
    ctx.fillStyle = theme.accent;
    ctx.textBaseline = "top";
    lines.forEach((line, i) => ctx.fillText(line, PAD, BANNER_TOP + i * lineHeight));

    return BANNER_TOP + lines.length * lineHeight;
  }

  function drawWordmark(ctx, theme) {
    ctx.font = '700 54px "Space Mono", monospace';
    ctx.fillStyle = theme.accent;
    ctx.textBaseline = "top";
    ctx.fillText("triangle-shows", PAD, BANNER_TOP);
    return BANNER_TOP + 66;
  }

  function drawHeading(ctx, y, events, theme) {
    ctx.font = '700 34px "Space Mono", monospace';
    ctx.fillStyle = theme.text;
    ctx.textBaseline = "top";
    ctx.fillText("my upcoming shows", PAD, y);

    const range = monthRange(events);
    if (range) {
      ctx.font = '400 24px "Space Mono", monospace';
      ctx.fillStyle = theme.muted;
      ctx.textAlign = "right";
      ctx.fillText(range, W - PAD, y + 8);
      ctx.textAlign = "left";
    }

    const ruleY = y + 52;
    ctx.fillStyle = theme.accent;
    ctx.fillRect(PAD, ruleY, W - PAD * 2, 2);
    return ruleY + 2;
  }

  function drawRow(ctx, ev, top, rowHeight, theme) {
    const mid = top + rowHeight / 2;
    const size = rowTypeScale(rowHeight);
    const { day, date } = formatPosterDate(ev.date);

    // Date column
    ctx.textBaseline = "alphabetic";
    ctx.font = `700 ${size.date}px "Space Mono", monospace`;
    ctx.fillStyle = theme.accent;
    ctx.fillText(`${day} ${date}`, PAD, mid + size.date * 0.36);

    // Venue rule, in the venue's own colour -- the same identification the calendar
    // tiles use, so a poster and the site agree about which show is whose.
    const ruleX = PAD + DATE_COL;
    const ruleH = Math.min(rowHeight - 16, size.title + size.meta + 8);
    ctx.fillStyle = venueColorFor(ev, theme.accent);
    ctx.fillRect(ruleX, mid - ruleH / 2, RULE_W, ruleH);

    const textX = ruleX + RULE_W + RULE_GAP;
    const textW = W - PAD - textX;

    ctx.font = `600 ${size.title}px "IBM Plex Mono", monospace`;
    const measureTitle = (s) => ctx.measureText(s).width;
    ctx.fillStyle = theme.text;
    ctx.fillText(truncateToWidth(measureTitle, ev.title, textW), textX, mid - 4);

    const meta = metaLine(ev);
    if (meta) {
      ctx.font = `400 ${size.meta}px "IBM Plex Mono", monospace`;
      const measureMeta = (s) => ctx.measureText(s).width;
      ctx.fillStyle = theme.muted;
      ctx.fillText(truncateToWidth(measureMeta, meta, textW), textX, mid + size.meta + 6);
    }
  }

  function drawOverflow(ctx, count, top, rowHeight, theme) {
    const mid = top + rowHeight / 2;
    ctx.font = `400 ${rowTypeScale(rowHeight).meta + 3}px "IBM Plex Mono", monospace`;
    ctx.fillStyle = theme.muted;
    ctx.textBaseline = "alphabetic";
    ctx.fillText(`+ ${count} more`, PAD + DATE_COL + RULE_W + RULE_GAP, mid + 7);
  }

  function drawFooter(ctx, total, theme) {
    ctx.textBaseline = "alphabetic";
    ctx.font = '700 22px "Space Mono", monospace';
    ctx.fillStyle = theme.accent;
    ctx.fillText("triangle-shows.net", PAD, FOOTER_BASELINE);

    ctx.font = '400 20px "Space Mono", monospace';
    ctx.fillStyle = theme.muted;
    ctx.textAlign = "right";
    ctx.fillText(total === 1 ? "1 show" : `${total} shows`, W - PAD, FOOTER_BASELINE);
    ctx.textAlign = "left";
  }

  /** Draw the whole poster onto a fresh canvas and return it. */
  function renderPoster(events, theme, banner) {
    const canvas = document.createElement("canvas");
    canvas.width = W;
    canvas.height = H;
    const ctx = canvas.getContext("2d");

    ctx.fillStyle = theme.bg;
    ctx.fillRect(0, 0, W, H);

    // A hairline frame, echoing the calendar's ruled surfaces.
    ctx.strokeStyle = theme.border;
    ctx.lineWidth = 2;
    ctx.strokeRect(PAD / 2, PAD / 2, W - PAD, H - PAD);

    const bannerBottom = banner.length
      ? drawBanner(ctx, banner, theme)
      : drawWordmark(ctx, theme);

    const regionTop = drawHeading(ctx, bannerBottom + HEADING_GAP, events, theme) + 28;
    const regionBottom = FOOTER_BASELINE - 46;

    const plan = planLayout(events.length, regionTop, regionBottom);
    for (let i = 0; i < plan.visible; i++) {
      drawRow(ctx, events[i], plan.blockTop + i * plan.rowHeight, plan.rowHeight, theme);
    }
    if (plan.overflow > 0) {
      drawOverflow(ctx, plan.overflow, plan.blockTop + plan.visible * plan.rowHeight,
                   plan.rowHeight, theme);
    }

    drawFooter(ctx, events.length, theme);
    return canvas;
  }

  // --- Preview overlay + export ---

  function canvasToBlob(canvas) {
    return new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
  }

  function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }

  function closeLineup() {
    const el = document.getElementById("lineup-overlay");
    if (el) el.remove();
    document.removeEventListener("keydown", _onKeydown);
  }

  function _onKeydown(e) {
    if (e.key === "Escape") closeLineup();
  }

  function openPreview(canvas, blob, filename) {
    closeLineup();

    const overlay = document.createElement("div");
    overlay.id = "lineup-overlay";
    overlay.className = "lineup-overlay";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", "Lineup poster preview");

    const panel = document.createElement("div");
    panel.className = "lineup-panel";

    canvas.className = "lineup-canvas";
    panel.appendChild(canvas);

    const actions = document.createElement("div");
    actions.className = "lineup-actions";

    const save = document.createElement("button");
    save.className = "lineup-btn";
    save.textContent = "↓ save png";
    save.onclick = () => downloadBlob(blob, filename);
    actions.appendChild(save);

    // Only offered where it will work. canShare({files}) is the reliable test -- plenty
    // of desktop browsers expose navigator.share but reject file payloads, which would
    // otherwise present a button that always throws.
    const file = _asFile(blob, filename);
    if (file && navigator.canShare && navigator.canShare({ files: [file] })) {
      const share = document.createElement("button");
      share.className = "lineup-btn";
      share.textContent = "↗ share";
      share.onclick = async () => {
        try {
          await navigator.share({ files: [file], title: "My upcoming shows" });
        } catch (err) {
          // AbortError is the visitor dismissing the sheet, which is not a failure.
          if (err && err.name !== "AbortError") console.warn("Share failed:", err);
        }
      };
      actions.appendChild(share);
    }

    const close = document.createElement("button");
    close.className = "lineup-btn lineup-btn-quiet";
    close.textContent = "× close";
    close.onclick = closeLineup;
    actions.appendChild(close);

    panel.appendChild(actions);
    overlay.appendChild(panel);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) closeLineup(); });
    document.body.appendChild(overlay);
    document.addEventListener("keydown", _onKeydown);
    save.focus();
  }

  function _asFile(blob, filename) {
    try {
      return new File([blob], filename, { type: "image/png" });
    } catch {
      return null;   // File constructor is absent on some older mobile browsers.
    }
  }

  /** Entry point for the favourites-bar button. */
  async function makeLineupPoster() {
    const today = todayKey();
    const events = upcomingFavorites(
      typeof getFavorites === "function" ? getFavorites() : {}, today
    );
    if (!events.length) return;

    const btn = document.getElementById("btn-lineup-graphic");
    const label = btn ? btn.textContent : null;
    if (btn) { btn.disabled = true; btn.textContent = "▣ drawing..."; }

    try {
      await ensureFonts();
      const canvas = renderPoster(events, readTheme(), readBanner());
      const blob = await canvasToBlob(canvas);
      if (!blob) return;
      openPreview(canvas, blob, posterFilename(today));
    } catch (err) {
      console.error("Could not build the lineup poster:", err);
    } finally {
      if (btn) { btn.disabled = false; if (label !== null) btn.textContent = label; }
    }
  }

  // --- Exports ---
  // The pure half is exported for the tests; the rest is what index.html calls.
  global.Lineup = {
    upcomingFavorites,
    formatPosterDate,
    monthRange,
    planLayout,
    truncateToWidth,
    rowTypeScale,
    metaLine,
    posterFilename,
    todayKey,
    renderPoster,
    ROW_MIN,
    ROW_MAX,
  };
  global.makeLineupPoster = makeLineupPoster;
  global.closeLineup = closeLineup;
})(typeof window !== "undefined" ? window : globalThis);
