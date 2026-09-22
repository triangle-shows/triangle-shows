// Shareable lineup poster: renders the visitor's upcoming favourites to a PNG.
//
// The poster is drawn from the site's own materials rather than a separate design --
// the wordmark is read out of the header and set in the same face the site uses for it,
// and every colour is pulled from the live CSS custom properties, so a poster made in
// wisteria light mode looks like the site that made it.
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

  const MASTHEAD_TOP = 52;
  const WORDMARK_MAX = 96;     // cap; narrower wordmarks are fitted to the width instead
  const HEADING_GAP = 34;      // masthead block to heading
  // The ASCII banner is given more width than the text column, running out to the
  // hairline frame instead of stopping at PAD. The frame is stroked at PAD/2 with a
  // 2px pen, so its inner edge is x=33; this leaves about a 7px gap from that before
  // the art's own cell rounding centres it. Every extra pixel of width is a wider
  // character cell, and at ten pixels a character that is the whole problem with
  // drawing this art at poster size.
  const ASCII_INSET = 40;
  // Where the list stops. The space below it is margin, and deliberately generous: it is
  // what keeps the bottom of a full poster from running into the edge of the paper.
  const LIST_BOTTOM = H - 110;
  const SIGNOFF_BASELINE = H - 64;   // in the margin the list leaves below itself

  // How many shows a poster will ever list. Past fifteen the rows are too tight to read
  // at a glance, which is the only thing a poster is for, so a longer list is cut to the
  // fifteen soonest and the footer says so rather than being crushed to fit.
  const MAX_EVENTS = 15;

  // The list length the layout is drawn for. No poster gets taller rows or larger type
  // than one with this many shows: a single favourite is set at the row height of a
  // five-show poster instead of taking a fifth of the page, and the space it does not
  // use becomes margin under the list.
  const REF_ROWS = 5;

  // Row metrics. ROW_MIN is only a geometric guard now -- MAX_EVENTS is what limits the
  // list -- but it has to stay below available/MAX_EVENTS (~60px against the current
  // banner), or the region rather than the cap would decide how many shows are shown.
  const ROW_MIN = 54;
  const ROW_MAX = 150;
  const DATE_COL = 168;        // "FRI 10.16" plus breathing room
  const RULE_W = 4;            // venue-coloured gutter rule, as on the calendar tiles
  const RULE_GAP = 22;

  // Ripple matrix.
  // A two-tone background: every cell of a very fine grid is either ink or paper with
  // nothing in between, and the picture comes from which cells are set. That is what a
  // dot-matrix printer or a 1-bit screen does, and it is a different idea from the
  // halftone above, where dots grow and shrink to carry a gradient.
  //
  // The picture is a ripple -- concentric waves spreading from a point just below the
  // bottom edge, as if something had been dropped there. A cell is inked where the wave
  // clears a threshold, so crests come out as fine arcs of pins and troughs as bare
  // paper. Coverage is the only thing that varies, which is what lets the bands read as
  // tone from across a room and as individual pins up close.
  const RIPPLE_PITCH = 4;     // cell size; the whole point is that this is small
  const RIPPLE_PIN = 1.4;     // the inked square inside a cell, in px
  const RIPPLE_WAVE = 52;     // crest-to-crest distance
  const RIPPLE_START = 0.46;  // fraction down the page where the matrix begins
  const RIPPLE_DECAY = 1400;  // how far the wave carries before it flattens out
  const RIPPLE_ORIGIN_Y = 40; // the source sits this far below the bottom edge, so the
                              // arcs crossing the page are wide sweeps rather than
                              // circles pinned to the margin

  // Photo ripple.
  // The same two-tone matrix, but the picture is a photograph of real water rather than
  // a sine wave: img/ripple-matrix.jpg, a CC0 frame of concentric rings prepared so the
  // centre falls at the bottom-left corner. See img/README.md for its provenance and the
  // recipe that produced it.
  //
  // Real water gives what the sine wave cannot: rings that are not quite circular, not
  // quite evenly spaced, and interrupted the way a surface actually is.
  //
  // Tone becomes coverage by ordered dithering against a Bayer matrix. A plain threshold
  // was tried first and gave hard contours -- only the brightest crests survived, as a
  // handful of thin scratches. The dither is what makes a photograph read as a
  // photograph in two tones.
  const PHOTO_SRC = "/img/ripple-matrix.jpg";
  const PHOTO_PITCH = 4;      // cell size, as RIPPLE_PITCH
  const PHOTO_PIN = 2;        // inked square inside a cell, in px
  const PHOTO_START = 0.46;   // fraction down the page where the matrix begins
  // Contrast gain around mid-grey, applied before the dither. The rings in the
  // photograph are broad and gentle, so neighbouring cells differ by very little tone;
  // dithered straight, they came out as an even field of noise with the ripple barely
  // legible. Stretching the tone first is what turns the rings back into arcs.
  const PHOTO_GAIN = 2.2;

  // 8x8 Bayer matrix, the standard recursive one, as thresholds in 0..63. Ordered rather
  // than error-diffused on purpose: a fixed matrix gives the even mechanical texture of a
  // printed screen, where Floyd-Steinberg would scatter the dots and lose it.
  const BAYER_8 = [
    [0, 32, 8, 40, 2, 34, 10, 42],
    [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44, 4, 36, 14, 46, 6, 38],
    [60, 28, 52, 20, 62, 30, 54, 22],
    [3, 35, 11, 43, 1, 33, 9, 41],
    [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47, 7, 39, 13, 45, 5, 37],
    [63, 31, 55, 23, 61, 29, 53, 21],
  ];

  /**
   * Type sizes for a given row height.
   *
   * Fixed sizes made a two-show poster look like a fifteen-show one that had failed to
   * load: the same small type stranded in the middle of an empty page. Growing the type
   * with the row turns that space into margin instead of a gap.
   *
   * The growth stops at ROW_MAX, which is the row height of a REF_ROWS poster, so the
   * top of this scale is a reference layout rather than whatever a one-show page could
   * have stretched to.
   */
  function rowTypeScale(rowHeight) {
    const t = Math.min(1, Math.max(0, (rowHeight - ROW_MIN) / (ROW_MAX - ROW_MIN)));
    return {
      title: Math.round(28 + 16 * t),
      meta: Math.round(18 + 6 * t),
      date: Math.round(21 + 7 * t),
    };
  }

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

  /**
   * Decide how tall each row is and where the block of them starts.
   *
   * Returns { rowHeight, visible, blockTop }. `count` has already been capped at
   * MAX_EVENTS by the caller, so the maxRows guard here only bites if the region ever
   * shrinks -- a taller banner, say -- and it drops rows rather than letting them run
   * over the footer.
   *
   * Both the row height and the anchor are worked out against at least REF_ROWS rows,
   * so every poster from one show to five is laid out identically and simply has more
   * empty paper below the list. Dividing the page by the real count instead left a
   * one-show poster as a lone line floating dead centre, which reads as a full poster
   * that failed to load rather than as a deliberately sparse one.
   */
  function planLayout(count, regionTop, regionBottom) {
    const available = regionBottom - regionTop;
    const maxRows = Math.max(1, Math.floor(available / ROW_MIN));
    const visible = Math.min(count, maxRows);

    const rows = Math.max(visible, REF_ROWS);
    // The ROW_MIN floor matters only in the same shrunken-region case as maxRows: with
    // fewer than REF_ROWS rows' worth of page left, dividing by REF_ROWS would put the
    // two lines of type on top of each other. Clamping up is safe because `visible` is
    // already no more than the region holds at ROW_MIN.
    const rowHeight = Math.min(ROW_MAX, Math.max(ROW_MIN, available / rows));
    const blockTop = regionTop + Math.max(0, (available - rows * rowHeight) / 2);

    return { rowHeight, visible, blockTop };
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

  /**
   * The list a poster is drawn from: the soonest MAX_EVENTS upcoming favourites.
   *
   * Separate from upcomingFavorites so the favourites bar can go on counting every
   * upcoming show while the poster lists only as many as fit at a readable size. Note
   * that a longer list is now cut silently -- the poster carries no count.
   */
  function posterEvents(favs, today) {
    return upcomingFavorites(favs, today).slice(0, MAX_EVENTS);
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

  /**
   * The name of the site, in both the forms a masthead might want it.
   *
   * Read from the page rather than written out here, which is what lets the Durham
   * variant put its own name on its own posters without this file knowing that variant
   * exists: SITE_CONFIG has already swapped both elements by the time this runs.
   *
   * `wordmark` is .site-title, the logo the site shows on narrow screens. `banner` is
   * the lines of .ascii-title, the art the desktop header carries, or [] when the header
   * is absent -- in which case the ascii masthead has nothing to draw and defers.
   */
  function readSite() {
    const titleEl = document.querySelector(".site-title");
    const artEl = document.querySelector(".ascii-title");
    // textContent picks up the words and drops the empty cursor <span> with them, and
    // for the art it does so without disturbing the spacing the drawing depends on.
    const wordmark = (titleEl ? titleEl.textContent.trim() : "") || "triangle-shows";
    const lines = artEl ? artEl.textContent.replace(/\s+$/, "").split("\n") : [];
    return { wordmark, banner: lines.some((l) => l.trim()) ? lines : [] };
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

  // The ripple photograph, loaded once. Kept as a module-level promise so a second
  // poster in the same visit costs nothing, and so the drawing code below can stay
  // synchronous -- it only ever sees an image that is already decoded, or null.
  let _photoPromise = null;
  let _photo = null;

  /**
   * Fetch and decode the background photograph.
   *
   * Resolves to null rather than rejecting when the asset is missing or fails to decode.
   * A poster is worth more than its background: the caller falls back to the procedural
   * ripple, which needs nothing from the network.
   */
  function ensureAssets() {
    if (_photoPromise) return _photoPromise;
    _photoPromise = new Promise((resolve) => {
      const img = new Image();
      img.onload = () => { _photo = img; resolve(img); };
      img.onerror = () => { _photo = null; resolve(null); };
      // Same origin, so the canvas stays untainted and toBlob() still works. A
      // cross-origin photo would need CORS headers or the export would throw.
      img.src = PHOTO_SRC;
    });
    return _photoPromise;
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
      // Orbitron ships only a weight axis, so the italic the site asks for is synthetic
      // in the browser and synthetic again on the canvas. Loading it under the same
      // description the wordmark is drawn with is what stops canvas quietly falling back
      // to a default sans, which would not look like the site at all.
      'italic 900 20px "Orbitron"',
    ];
    try {
      await Promise.all(faces.map((f) => document.fonts.load(f)));
      await document.fonts.ready;
    } catch {
      /* Substituted monospace is a worse poster, not a broken one. */
    }
  }

  // --- Rendering ---

  /**
   * The ripple matrix, drawn in the same place and to the same purpose as the halftone.
   *
   * Two-tone leaves no opacity or dot size to modulate, so the fade toward the top of
   * the page is done the way a 1-bit image does it: by moving the threshold. Near the
   * bottom it sits at zero and half of each wave is inked, giving bold arcs. Further up
   * it climbs until only the crests survive as thin broken lines, then nothing. Fading
   * with alpha instead would make it a grey wash and give up the one thing two-tone is
   * good at.
   *
   * Every pin goes into a single path and is filled once. At this pitch there are tens
   * of thousands of them, and a fill per pin is the difference between a poster that
   * appears at once and one the visitor waits for.
   */
  function drawRippleMatrix(ctx, theme) {
    const startY = H * RIPPLE_START;
    const span = H - startY;
    const cx = W / 2;
    const cy = H + RIPPLE_ORIGIN_Y;
    const k = (Math.PI * 2) / RIPPLE_WAVE;

    ctx.save();
    ctx.fillStyle = theme.accent;
    ctx.beginPath();
    for (let y = startY; y <= H; y += RIPPLE_PITCH) {
      // 0 where the matrix begins, 1 at the bottom edge, squared so the sparse end
      // covers more of the page than the dense end. That is what keeps the fade from
      // announcing itself as a line across the poster.
      const t = (y - startY) / span;
      const threshold = 1 - t * t;
      if (threshold >= 1) continue;   // nothing in this row could clear it
      const dy = y - cy;
      for (let x = 0; x <= W; x += RIPPLE_PITCH) {
        const dx = x - cx;
        const d = Math.sqrt(dx * dx + dy * dy);
        // Amplitude falls off with distance, so the wave is strongest near its source
        // and the arcs high on the page break up before the threshold alone would have
        // cut them.
        const amp = 1 / (1 + d / RIPPLE_DECAY);
        if (Math.sin(d * k) * amp <= threshold) continue;
        ctx.rect(x, y, RIPPLE_PIN, RIPPLE_PIN);
      }
    }
    ctx.fill();
    ctx.restore();
  }

  /**
   * The photograph, dithered into the same two-tone matrix.
   *
   * The image is drawn once into an offscreen canvas at exactly the sample grid's size,
   * which hands the downsampling to the browser, and then read back in a single
   * getImageData call. Sampling the full-size image per cell instead would mean a
   * getImageData per pin and a poster that takes seconds.
   *
   * Falls back to the procedural ripple when the photo is not available, so a missing or
   * blocked asset costs the poster its texture and nothing else.
   */
  function drawPhotoRipple(ctx, theme) {
    if (!_photo) return drawRippleMatrix(ctx, theme);

    const startY = H * PHOTO_START;
    const span = H - startY;
    const cols = Math.ceil(W / PHOTO_PITCH) + 1;
    const rows = Math.ceil(span / PHOTO_PITCH) + 1;

    const grid = document.createElement("canvas");
    grid.width = cols;
    grid.height = rows;
    const gctx = grid.getContext("2d");
    gctx.drawImage(_photo, 0, 0, cols, rows);
    const data = gctx.getImageData(0, 0, cols, rows).data;

    ctx.save();
    ctx.fillStyle = theme.accent;
    ctx.beginPath();
    for (let j = 0; j < rows; j++) {
      // Coverage falls off toward the top of the band, squared so the sparse end runs
      // long. With two tones there is no opacity to fade, so the fade has to happen in
      // how many cells are set -- which is the same trick the sine ripple uses.
      const t = j / (rows - 1);
      const fade = t * t;
      const bayerRow = BAYER_8[j % 8];
      for (let i = 0; i < cols; i++) {
        const p = (j * cols + i) * 4;
        // The asset is greyscale, so the red channel is the luminance.
        const v = (data[p] / 255 - 0.5) * PHOTO_GAIN + 0.5;
        const level = Math.min(1, Math.max(0, v)) * fade;
        if (level <= (bayerRow[i % 8] + 0.5) / 64) continue;
        ctx.rect(i * PHOTO_PITCH, startY + j * PHOTO_PITCH, PHOTO_PIN, PHOTO_PIN);
      }
    }
    ctx.fill();
    ctx.restore();
  }

  /**
   * Draw text with a background-coloured outline laid down first.
   *
   * The rows sit over the background, and ink crowding the edge of a
   * glyph costs more legibility than its size suggests -- worst on the muted venue
   * line, which is the closest in tone to the dots themselves. Knocking the ink out
   * from behind the type is what a screen printer would do, and high on the page, where
   * there is nothing to knock out, it draws nothing.
   *
   * Only the outline is stroked: the fill goes on top at full weight, so the glyph
   * keeps its own shape rather than being fattened by the stroke.
   */
  function inkText(ctx, theme, text, x, y) {
    ctx.save();
    ctx.strokeStyle = theme.bg;
    ctx.lineWidth = 4;           // 2px of clear paper each side of the glyph
    ctx.lineJoin = "round";      // without this, sharp joins spike out past the glyph
    ctx.miterLimit = 2;
    ctx.strokeText(text, x, y);
    ctx.restore();
    ctx.fillText(text, x, y);
  }

  /**
   * The masthead: the header's ASCII banner, snapped to a whole-pixel character grid.
   *
   * The art is monospace, so it has a character cell -- and at the size the page forces,
   * 90 characters across the width, that cell is about 11px. Drawn as plain strings most
   * rows and every fourth character landed between pixels and every stroke was
   * anti-aliased across two, which was a real share of what read as noise at this size.
   * Drawing the string in one call leaves the placement to the text engine; drawing each
   * character at an integer x does not.
   *
   * The cost is one fillText per character -- about 500 for the whole banner, which is
   * nothing next to the background's thousands of dots. Monospace has no kerning to
   * lose, so nothing about the art changes except that it lands on the grid.
   */
  function drawAsciiMasthead(ctx, theme, site) {
    const lines = site.banner;
    const widest = lines.reduce((n, l) => Math.max(n, l.length), 0);
    if (!widest) return drawWordmarkMasthead(ctx, theme, site);

    // Size first, from the width the page allows -- the same number the unsnapped
    // version uses, so no weight is given up. Then the cell is that size's advance
    // rounded to whole pixels: 10 rather than 10.4, which pulls the art in by about 5%
    // and is what buys every character an integer x. Glyphs end up a shade wider than
    // their cell, so runs of underscores close up into continuous rules instead of
    // showing the hairline seams that make the art look noisy.
    ctx.font = '700 100px "Space Mono", monospace';
    const advanceAt100 = ctx.measureText("M").width || 60;
    const band = W - ASCII_INSET * 2;
    const size = Math.floor((band / widest) * (100 / advanceAt100));
    const cell = Math.max(1, Math.round((size * advanceAt100) / 100));
    // Centred on what the integer cell actually comes to, which is a little narrower
    // than the band -- 90 cells of 11px is 990 of 1000 -- so the leftover splits evenly
    // instead of all landing on the right.
    const x0 = Math.round((W - widest * cell) / 2);
    const lineHeight = Math.round(size * 1.35);   // integer, so every row is on the grid

    ctx.font = `700 ${size}px "Space Mono", monospace`;
    ctx.fillStyle = theme.accent;
    ctx.textBaseline = "top";
    lines.forEach((line, row) => {
      const y = MASTHEAD_TOP + row * lineHeight;
      for (let i = 0; i < line.length; i++) {
        const ch = line[i];
        if (ch === " ") continue;
        ctx.fillText(ch, x0 + i * cell, y);
      }
    });

    return MASTHEAD_TOP + lines.length * lineHeight;
  }

  /**
   * The wordmark masthead: the site's name, in the site's logo face.
   *
   * Sized by measuring rather than by a fixed number, so it sets to the same width
   * whatever it says -- "durm-shows" is four characters shorter than "triangle-shows"
   * and would otherwise leave a ragged gap. WORDMARK_MAX caps it, because a short name
   * fitted to the full width would tower over the list it belongs to.
   *
   * Returns the bottom of the block, which is where the heading measures from.
   */
  function drawWordmarkMasthead(ctx, theme, site) {
    const text = site.wordmark;
    const face = 'italic 900 %SIZE%px "Orbitron", "Space Mono", monospace';

    // Measure at a known size and scale the result; Orbitron's advance widths are not
    // something to hard-code, and this still fits if the face was substituted.
    ctx.font = face.replace("%SIZE%", "100");
    const widthAt100 = ctx.measureText(text).width || 100;
    const size = Math.min(WORDMARK_MAX, Math.floor((W - PAD * 2) * (100 / widthAt100)));

    ctx.font = face.replace("%SIZE%", String(size));
    // The site sets .site-title with letter-spacing; canvas takes the same declaration
    // where it is supported and ignores it where it is not, which costs nothing.
    ctx.letterSpacing = "0.03em";
    ctx.fillStyle = theme.accent;
    ctx.textBaseline = "top";
    ctx.fillText(text, PAD, MASTHEAD_TOP);
    ctx.letterSpacing = "0px";

    // line-height: 1, as .site-title has, plus the slack a 900-weight italic needs below
    // the baseline so the heading rule does not crowd the descenders.
    return MASTHEAD_TOP + size * 1.06;
  }

  function drawHeading(ctx, y, theme) {
    ctx.font = '700 34px "Space Mono", monospace';
    ctx.fillStyle = theme.text;
    ctx.textBaseline = "top";
    ctx.fillText("i\u2019m going to:", PAD, y);

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
    inkText(ctx, theme, `${day} ${date}`, PAD, mid + size.date * 0.36);

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
    inkText(ctx, theme, truncateToWidth(measureTitle, ev.title, textW), textX, mid - 4);

    const meta = metaLine(ev);
    if (meta) {
      ctx.font = `400 ${size.meta}px "IBM Plex Mono", monospace`;
      const measureMeta = (s) => ctx.measureText(s).width;
      ctx.fillStyle = theme.muted;
      inkText(ctx, theme, truncateToWidth(measureMeta, meta, textW), textX,
              mid + size.meta + 6);
    }
  }

  /**
   * The sign-off, bottom left, on the baseline the old footer used.
   *
   * Knocked out like the rows are: it sits low on the page, which is the densest part of
   * every background.
   */
  function drawSignoff(ctx, theme) {
    ctx.textBaseline = "alphabetic";
    ctx.textAlign = "left";
    ctx.font = '700 22px "Space Mono", monospace';
    // Same ink as the heading, so the two lines in the poster's own voice match,
    // and the accent stays reserved for the dates, the rule and the venue marks.
    ctx.fillStyle = theme.text;
    inkText(ctx, theme, "see you out there...", PAD, SIGNOFF_BASELINE);
  }

  /** Draw the whole poster onto a fresh canvas and return it. `site` is readSite()'s
   *  output: the wordmark and the banner lines. */
  function renderPoster(events, theme, site) {
    const canvas = document.createElement("canvas");
    canvas.width = W;
    canvas.height = H;
    const ctx = canvas.getContext("2d");

    ctx.fillStyle = theme.bg;
    ctx.fillRect(0, 0, W, H);

    // Before the frame, so the hairline stays crisp over a background that bleeds off
    // the edge of the page rather than being interrupted by it.
    drawPhotoRipple(ctx, theme);

    // A hairline frame, echoing the calendar's ruled surfaces.
    ctx.strokeStyle = theme.border;
    ctx.lineWidth = 2;
    ctx.strokeRect(PAD / 2, PAD / 2, W - PAD, H - PAD);

    const mastheadBottom =
      drawAsciiMasthead(ctx, theme, site || { wordmark: "", banner: [] });

    const regionTop = drawHeading(ctx, mastheadBottom + HEADING_GAP, theme) + 28;
    const regionBottom = LIST_BOTTOM;

    const plan = planLayout(events.length, regionTop, regionBottom);
    for (let i = 0; i < plan.visible; i++) {
      drawRow(ctx, events[i], plan.blockTop + i * plan.rowHeight, plan.rowHeight, theme);
    }

    drawSignoff(ctx, theme);
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
    const events = posterEvents(
      typeof getFavorites === "function" ? getFavorites() : {}, today
    );
    if (!events.length) return;

    const btn = document.getElementById("btn-lineup-graphic");
    const label = btn ? btn.textContent : null;
    if (btn) { btn.disabled = true; btn.textContent = "▣ drawing..."; }

    try {
      // Both in flight at once; neither needs the other, and the photo is the larger
      // of the two waits.
      await Promise.all([ensureFonts(), ensureAssets()]);
      const canvas = renderPoster(events, readTheme(), readSite());
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
  //
  // drawPhotoRipple and drawAsciiMasthead are here for the tests too, and for one
  // specific reason: each defers to a second function when what it wants is missing --
  // the photograph, or the header's art -- and neither of those paths runs in ordinary
  // use. Exported, they can be called with a recording canvas and a missing input, which
  // is the only way the fallbacks get exercised at all. Without that they are code that
  // nothing reaches until the day it matters.
  global.Lineup = {
    upcomingFavorites,
    posterEvents,
    formatPosterDate,
    planLayout,
    truncateToWidth,
    rowTypeScale,
    metaLine,
    posterFilename,
    todayKey,
    renderPoster,
    ensureAssets,
    drawPhotoRipple,
    drawAsciiMasthead,
    MAX_EVENTS,
    REF_ROWS,
    ROW_MIN,
    ROW_MAX,
  };
  global.makeLineupPoster = makeLineupPoster;
  global.closeLineup = closeLineup;
})(typeof window !== "undefined" ? window : globalThis);
