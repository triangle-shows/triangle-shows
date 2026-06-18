// API configuration
const API_BASE = window.location.origin;

// Spotify — paste your Client ID from https://developer.spotify.com/dashboard
// Register redirect URIs: https://triangle-shows.org and http://localhost:8000
const SPOTIFY_CLIENT_ID = "";

// Color palettes — each entry maps CSS custom property names to values.
// applyPalette() sets these on :root and persists the choice to localStorage.
const PALETTES = {
  amber: {
    label: "Amber",
    accent: "#c87941",
    vars: {
      "--bg":           "#1a1008",
      "--surface":      "#241609",
      "--surface2":     "#2e1d0c",
      "--border":       "#3d2a12",
      "--text":         "#e8d5b0",
      "--muted":        "#9a7a50",
      "--dim":          "#5a4020",
      "--accent":       "#c87941",
      "--accent-hover": "#e09050",
      "--accent-bg":    "rgba(200,121,65,0.10)",
      "--today-bg":     "rgba(200,121,65,0.05)",
    },
  },
  phosphor: {
    label: "Phosphor",
    accent: "#44c754",
    vars: {
      "--bg":           "#060d07",
      "--surface":      "#0c1a0d",
      "--surface2":     "#122014",
      "--border":       "#1e3820",
      "--text":         "#a8d8ac",
      "--muted":        "#4e8858",
      "--dim":          "#28502e",
      "--accent":       "#44c754",
      "--accent-hover": "#68e078",
      "--accent-bg":    "rgba(68,199,84,0.10)",
      "--today-bg":     "rgba(68,199,84,0.05)",
    },
  },
  midnight: {
    label: "Midnight",
    accent: "#4888e8",
    vars: {
      "--bg":           "#050810",
      "--surface":      "#0a0f20",
      "--surface2":     "#101828",
      "--border":       "#1a2640",
      "--text":         "#c0cce0",
      "--muted":        "#4868a0",
      "--dim":          "#1a2e50",
      "--accent":       "#4888e8",
      "--accent-hover": "#68a8ff",
      "--accent-bg":    "rgba(72,136,232,0.12)",
      "--today-bg":     "rgba(72,136,232,0.05)",
    },
  },
  wisteria: {
    label: "Wisteria",
    accent: "#c060d0",
    vars: {
      "--bg":           "#0e0810",
      "--surface":      "#180e1c",
      "--surface2":     "#201428",
      "--border":       "#361e40",
      "--text":         "#e0c8e8",
      "--muted":        "#906898",
      "--dim":          "#4a2858",
      "--accent":       "#c060d0",
      "--accent-hover": "#d880e8",
      "--accent-bg":    "rgba(192,96,208,0.12)",
      "--today-bg":     "rgba(192,96,208,0.05)",
    },
  },
  // Durham Bulls: #003E7A navy + #B15E27 burnt orange
  durham: {
    label: "Durham",
    accent: "#bf6a28",
    vars: {
      "--bg":           "#030d22",
      "--surface":      "#081a30",
      "--surface2":     "#0e2340",
      "--border":       "#1c3a6a",
      "--text":         "#d8e4f4",
      "--muted":        "#4878b8",
      "--dim":          "#1c3868",
      "--accent":       "#bf6a28",
      "--accent-hover": "#f0a050",
      "--accent-bg":    "rgba(191,106,40,0.14)",
      "--today-bg":     "rgba(191,106,40,0.06)",
    },
  },
};

function applyPalette(key) {
  if (!PALETTES[key]) return;
  // Drive palette entirely through CSS: html[data-palette="..."] selectors in styles.css
  document.documentElement.dataset.palette = key;
  localStorage.setItem("triangle-shows-palette", key);
  document.querySelectorAll(".palette-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.palette === key);
  });
}

// Light / dark mode toggle
function applyMode(mode) {
  document.documentElement.dataset.mode = mode;
  localStorage.setItem("triangle-shows-mode", mode);
  const btn = document.getElementById("mode-toggle");
  if (btn) btn.textContent = mode === "light" ? "☾ dark" : "☀ light";
}

function toggleMode() {
  const current = document.documentElement.dataset.mode || "dark";
  applyMode(current === "dark" ? "light" : "dark");
}

// Dynamically shrink the ASCII title font so it never overlaps the header-right controls.
// Runs on load and on every resize; only active when the ASCII art is visible (desktop).
function fitAsciiTitle() {
  const header   = document.querySelector(".app-header");
  const title    = document.querySelector(".ascii-title");
  const rightBar = document.querySelector(".header-right");
  if (!header || !title) return;
  if (getComputedStyle(title).display === "none") return; // hidden on mobile

  // Leave room for the header-right buttons; fall back to 140px if not found.
  const headerW  = header.getBoundingClientRect().width;
  const rightW   = rightBar ? rightBar.getBoundingClientRect().width + 32 : 140;
  const available = headerW - rightW;

  // Step down from 17 → 9px until the title fits
  for (let size = 17; size >= 9; size -= 0.5) {
    title.style.fontSize = size + "px";
    if (title.scrollWidth <= available) return;
  }
}

document.addEventListener("DOMContentLoaded", fitAsciiTitle);
window.addEventListener("resize", fitAsciiTitle);

// Per-subdomain site configuration. Detected once at load time from hostname.
const SITE_CONFIG = (function () {
  const host = window.location.hostname;
  if (host.startsWith("durm.")) {
    return {
      city:      "Durham",
      title:     "durm-shows",
      subtitle:  `live music <s>across the triangle</s> in durham on one calendar. see the rest of the triangle <a href="https://triangle-shows.net" style="color: var(--accent-hover)">here</a>.`,
      palette:   "durham",
      ascii: `      __                               __                                    __ \n  ___/ /_  ___________ ___       _____/ /_  ____ _      _______  ____  ___  / /_\n / _  / / / / ___/ __ '__ \\_____/ ___/ __ \\/ __ \\ | /| / / ___/ / __ \\/ _ \\/ __/\n/ // / /_/ / /  / / / / / /_____\\__ / / / / /_/ / |/ |/ /\\__ / / / / /  __/ /_  \n\\___/\\____/_/  /_/ /_/ /_/    /____/_/ /_/\\____/|__/|__/____(_)_/ /_/\\___/\\__/ `,
    };
  }
  return { city: null };
})();

// Apply subdomain-specific title, subtitle, and default palette.
// Runs after DOM is ready; palette is only defaulted if the user has no saved preference.
function applySiteConfig() {
  if (!SITE_CONFIG.city) return;

  document.title = SITE_CONFIG.title + ".net";

  const asciiTitle = document.querySelector(".ascii-title");
  const siteTitle  = document.querySelector(".site-title");
  const subtitle   = document.querySelector(".site-subtitle");

  // Swap ASCII art text while preserving the cursor span
  if (asciiTitle && SITE_CONFIG.ascii) {
    const cursor = asciiTitle.querySelector(".cursor");
    asciiTitle.textContent = SITE_CONFIG.ascii;
    if (cursor) asciiTitle.appendChild(cursor);
  }
  if (siteTitle) siteTitle.textContent = SITE_CONFIG.title;
  if (subtitle)  subtitle.innerHTML = SITE_CONFIG.subtitle;

  // Hide palette swatches — durham palette is fixed; only dark/light toggle remains
  const paletteGroup = document.querySelector(".palette-group");
  if (paletteGroup) paletteGroup.style.display = "none";

  // Always apply the site palette — picker is hidden on subdomain sites so
  // there's no UI to change it, and the saved preference may be from the main domain.
  applyPalette(SITE_CONFIG.palette);
}

document.addEventListener("DOMContentLoaded", applySiteConfig);

// City color mappings — jewel-tone palette, matches venue color families
// border = chip border/text color; activeBg = subtle tint for active state
const CITY_COLORS = {
  Raleigh:                { border: "#a83850", activeBg: "rgba(168,56,80,0.16)" },    // ruby
  Durham:                 { border: "#2a6098", activeBg: "rgba(42,96,152,0.16)" },    // sapphire
  "Chapel Hill-Carrboro": { border: "#2a7a50", activeBg: "rgba(42,122,80,0.16)" },   // emerald
  Saxapahaw:              { border: "#8a30a8", activeBg: "rgba(138,48,168,0.16)" },   // orchid
};

