# Tools

Dev and ops scripts for triangle-shows. Run from the repo root.

## Deployment

### `wait_for_deploy.py`
Polls `/api/health` until the deployed git SHA matches your local HEAD. Run this after a `git push` to know when the deploy is live.

```
python tools/wait_for_deploy.py
python tools/wait_for_deploy.py --interval 10   # poll every 10s
python tools/wait_for_deploy.py --url http://localhost:8000
```

## Scraping

### `run_scrape.py`
Triggers the `/api/scrape` endpoint and prints a structured summary table (found / created / updated per venue). Appends results to `tools/scrape_results.log`.

```
python tools/run_scrape.py                          # scrape all venues
python tools/run_scrape.py --type rhp_events        # one scraper type
python tools/run_scrape.py --url http://localhost:8000
```

### `import_submissions.py`
Imports approved event submissions from a Google Sheet into the database. Requires a GCP service account key at `tools/service_account.json` (gitignored) and env vars for the sheet ID and DB URL. See the file header for full setup instructions.

```
python tools/import_submissions.py
python tools/import_submissions.py --dry-run
```

## Scraper Debugging

These were written to diagnose scraper issues for specific venues. They're one-off scripts rather than maintained tools, but useful as references when building a new scraper.

### `diagnose_scrapers.py`
Fetches event pages for a hardcoded list of venues and checks whether expected HTML patterns (JSON-LD, tribe CSS selectors, JSON endpoints) are present. Originally used to debug venues returning 0 events.

### `check_venue_urls.py`
Makes HTTP requests to a hardcoded list of venue event URLs and reports status codes and redirect chains. Used to verify which URLs are reachable.

### `check_roots.py`
Fetches root domains for a list of venues and sniffs for event-related links (looks for `/events`, `/shows`, etc.). Used when a venue's event URL was unknown.

### `inspect_html.py`
Fetches raw HTML from a URL and prints excerpts around a search pattern. Used to inspect the DOM structure of a venue's event page to figure out what selectors to use.

### `inspect_js_venues.py`
Similar to `inspect_html.py` but also extracts inline `<script>` blocks and looks for embedded JSON. Originally used for Motorco, Kings, and Carolina Theatre.
