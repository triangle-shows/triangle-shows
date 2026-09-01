# triangle-shows.net

A concert calendar for venues across the Raleigh-Durham-Chapel Hill area.

**[triangle-shows.net](https://triangle-shows.net)**

---

## What it does

Triangle Shows aggregates live music listings from 21+ venues across the Triangle and displays them on an interactive calendar. Every 6 hours, a background scraper pulls fresh event data from each venue. Visitors can search events, filter by city or venue, favorite shows, hide clutter, and export to their personal calendar via `.ics` download or live subscription feed.

### Venues covered

| Venue | City |
|---|---|
| Boom Club | Durham |
| DPAC | Durham |
| Motorco Music Hall | Durham |
| Rubies on Five Points | Durham |
| Shadowbox Studio | Durham |
| Stancyks | Durham |
| The Pinhook | Durham |
| Cat's Cradle and Back Room | Chapel Hill-Carrboro |
| Local 506 | Chapel Hill-Carrboro |
| The Cave | Chapel Hill-Carrboro |
| Haw River Ballroom | Saxapahaw |
| Chapel of Bones | Raleigh |
| Kings | Raleigh |
| Koka Booth Amphitheatre | Raleigh |
| Lincoln Theatre | Raleigh |
| Neptune's Parlour | Raleigh |
| Pour House | Raleigh |
| Red Hat Amphitheater | Raleigh |
| Slim's | Raleigh |
| The Ritz | Raleigh |

---

## Features

- **Full-month calendar** — powered by FullCalendar v6 with month and list views
- **Search & filters** — filter by city, venue, or size; text search by artist/event name
- **Event details** — modal with doors/show time, price, description, and ticket link
- **Favorites** — heart events and export them as a `.ics` file
- **Hide shows** — hide events cluttering your view; restore them any time
- **Calendar subscription** — add `https://triangle-shows.net/feeds/events.ics` to Apple Calendar, Google Calendar, or Outlook for live updates
- **Color palettes** — 5 themes (Amber, Phosphor, Midnight, Wisteria, Durham) with light/dark modes
- **Durham site** — [durm-shows.net](https://durm-shows.net) shows only Durham venues with the Durham Bulls palette. `durm.triangle-shows.net` still works and serves the same thing

---

## Running locally

See [SELF-HOSTING.md](docs/SELF-HOSTING.md) for setup instructions.

---

## Running the tests

Two suites, and CI runs both on every pull request.

**Backend** — pytest, from `backend/`:

```bash
python -m pytest tests -q
```

A handful of migration tests need a reachable PostgreSQL server and skip cleanly without
one, so a local run with no database still passes. CI stands up a Postgres service
container, so they do run there.

**Frontend** — Node's built-in test runner, from the repo root:

```bash
node --test frontend/tests/
```

No `package.json`, no `npm install`, no dependencies: these use only `node:test`,
`node:assert`, `node:fs`, `node:path` and `node:vm`. Requires Node 18 or newer. Keeping
them dependency-free is deliberate — a browser-less runner that needs no toolchain is
what makes it reasonable to test plain `<script>` files at all, by evaluating them in a
`vm` context with only the globals they actually touch.

---

## Project structure

```
backend/
  app/
    api/            # FastAPI route handlers (events, venues, health, iCal feed)
    scrapers/       # One scraper per venue/platform
    models.py       # SQLAlchemy ORM — Venue, Event, ScrapeLog
    schemas.py      # Pydantic response models
    scheduler.py    # APScheduler job config
    seed.py         # Venue seed data (names, URLs, colors, capacities)
    config.py       # Settings loaded from .env
  alembic/          # Database migrations
frontend/
  index.html
  css/styles.css
  js/
    app.js          # FullCalendar init, loading screen, hidden-show chips
    filters.js      # Search, city, venue, and size filter logic
    config.js       # Color palettes, API base URL, site config
    modal.js        # Event detail modal
    favorites.js    # Heart, hide, restore, and export logic
  tests/            # Node built-in test runner, no dependencies
tools/              # Dev utilities (see below)
  mockups/          # Static HTML design explorations
docs/               # Architecture and self-hosting guides
```

### Developer tools

Testing scripts are in `tools/` for debugging scrapers and development. Fair warning — these are primarily tested on my machine and some may be outdated.

| Script | What it does |
|---|---|
| `run_scrape.py` | Calls the scrape API and prints a summary; logs to `tools/scrape_results.log` |
| `diagnose_scrapers.py` | Runs scrapers individually and reports output and errors |
| `inspect_html.py` | Fetches and prints raw HTML from a venue page for scraper debugging |
| `inspect_js_venues.py` | Inspects JS-heavy venue pages to find embedded API or widget patterns |
| `check_venue_urls.py` | Spot-checks that venue event page URLs resolve |
| `check_roots.py` | Verifies scraper URL roots are reachable |
| `wait_for_deploy.py` | Polls `/api/health` until a new Cloud Build deploy goes live |
| `import_submissions.py` | Imports approved event submissions from a Google Sheet into the DB |

---

## Scraper map

Each venue is handled by one scraper type. Last updated 2026-06-29 — may drift as venues are added.

| Scraper | Venues |
|---|---|
| `ticketmaster` | Koka Booth Amphitheatre, Red Hat Amphitheater, DPAC, The Ritz |
| `rhp_events` | Lincoln Theatre, Cat's Cradle, Cat's Cradle Back Room, Local 506, The Pinhook |
| `motorco` | Motorco Music Hall |
| `eventprime` | Kings |
| `tribe_events` | The Cave |
| `venuepilot` | Haw River Ballroom, Rubies on Five Points, Stanczyks |
| `squarespace` | Neptune's Parlour, Boom Club |
| `mec` | Shadowbox Studio, Slim's |
| `tickpick_organizer` | Chapel of Bones |
| `webflow_cms` | Pour House |

The authoritative source is [`backend/app/seed.py`](backend/app/seed.py) — each venue dict has a `scraper_type` field.

---

## Credentials in request URLs

The Ticketmaster Discovery API authenticates with a query parameter (`?apikey=`) rather than a header, so every request URL the Ticketmaster scraper builds *is* a live credential. Four sinks would otherwise carry it out of the process, and closing one leaves the others open:

| Sink | Closed by |
|---|---|
| `httpx` logs every request at INFO with the full query string | `app.main.configure_logging` pins the `httpx` logger to WARNING |
| `httpx.HTTPStatusError` embeds the URL in `str(e)`, which is logged on a failed scrape | `RedactingFormatter`, installed on every log handler in the process (so it covers exception tracebacks too) |
| An exception escaping a *route* is re-raised by Starlette's `ServerErrorMiddleware` and logged by the ASGI server on its own error logger — which has `propagate=False` and its own handler, so it never passes a root handler | the same sweep: `configure_logging` wraps *every* handler, not root's, via `redaction.redact_handler`, which preserves each handler's existing format instead of replacing it |
| That same string is persisted to `scrape_logs.error_message` and returned in the per-venue result dict from `POST /api/scrape` | `manager.scrape_venue` redacts once, before all three uses |
| `POST /api/scrape`'s catch-all also returns `detail=str(e)`, but on failures *outside* `scrape_venue` — session construction, a scraper import — that never pass through its redaction | `main.trigger_scrape` redacts independently, in its own `except` block |

`backend/app/redaction.py::redact_credentials` is the shared helper. It is a **denylist** of parameter names and therefore never complete — a new scraper authenticating with an unlisted parameter needs an entry there and a case in the parametrized test in `backend/tests/test_redaction.py`, not a nearby entry that happens to look similar. Only the value is removed, so a redacted URL still says which venue was being fetched and with what paging.

---

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, FastAPI, SQLAlchemy (async), Alembic |
| Database | PostgreSQL (asyncpg driver) |
| Scraping | httpx, BeautifulSoup4, Ticketmaster Discovery API |
| Frontend | Vanilla JS, FullCalendar v6 |
| Scheduling | APScheduler (local) / Google Cloud Scheduler (production) |
| Deployment | Google Cloud Run + Neon PostgreSQL + Cloudflare |

See [ARCHITECTURE.md](docs/ARCHITECTURE.md) for how a request reaches the app and how a commit becomes a deploy.

---

## Roadmap

Upcoming features and ideas are tracked in [GitHub Issues](https://github.com/triangle-shows/triangle-shows/issues). Some things I'm currently thinking about:

- Adding more venues (Sharp 9 Gallery, the Fruit, others)
- Handling custom/one-off events and form submission
- Performance improvements
- Possibly pulling in events from Instagram accounts like Fuzzy Needle

---

## Help me out!

I'm looking for co-developers — this thing could be really cool and TBH I'm not a professional developer. Reach out to me if you want to help build it!

See [CONTRIBUTING.md](.github/CONTRIBUTING.md) for the branch/PR workflow if you want to contribute.

---

## License / contact me

Functional Source License 1.1 with an Apache 2.0 future license ([FSL-1.1-ALv2](LICENSE)).
Source-available: use, modify and redistribute it for any purpose except competing use, and
each version becomes Apache 2.0 two years after its release.

The license covers the code. It does not cover the event data the API serves — that is
aggregated from venues' own public listings and from the Ticketmaster Discovery API, and
remains the property of the respective venues and rights holders.

Contact me at [@tyfi](https://bsky.app/profile/tyfi.bsky.social) on Bluesky, or you can email [mail@triangle-shows.net](mailto:mail@triangle-shows.net)
