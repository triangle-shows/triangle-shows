# triangle-shows.net

A semi-interactive shows calendar for venues across the triangle.

**[triangle-shows.net](https://triangle-shows.net)**

---

## What it does

In the background, a script scrapes show listings from every venues every 6 hours. The site itself lets you search and view events and details (and in some cases click through to buy tickets!) and customize your view by selecting or hiding shows and venues. You can also favorite events and download them to wherever you keep your calendar!

### Venues covered

| Venue | City |
|---|---|
| Boom Club | Durham |
| DPAC | Durham |
| Motorco Music Hall | Durham |
| Rubies on Five Poitns | Durham |
| Shadowbox Studio | Durham |
| Stancyks | Durham |
| The Pinhook | Durham |
| Cat's Cradle and Back Room| Chapel Hill-Carrboro |
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

## Roadmap

### What I'm working on now
- Adding more venues (Sharp 9 Gallery, the Fruit, others???)
- Tinkering with the UI
- Hopefully making load times faster

### What's next
- Handling custom / one-off events somehow
- Form submission for new events
- Email inbox
- Code hygiene

### Later / eventually
- Stickers?
- Custom playlists?
- Possible UI re-design

### IDK how but thinking about it
- Bringing in events from insta like Fuzzy Needle

---

## Help me!

I'm looking for co-developers here -- this thing could be really cool and TBH I am not an experienced developer! Reach out to me if you want to help

---

## Technical Stuff

### What does what

- **Backend:** Python / FastAPI, SQLAlchemy (async), PostgreSQL
- **Scrapers:** Ticketmaster Discovery API + custom scrapers for RHP Tickets, Tribe Events, Squarespace, EventPrime, VenuePilot, and venue-specific parsers
- **Scheduler:** APScheduler (6-hour scrape interval in production)
- **Frontend:** Vanilla JS, [FullCalendar v6](https://fullcalendar.io/)
- **Deployment:** Google Cloud Run + Neon PostgreSQL + Cloud Scheduler

### Running locally, if you want to

Requires Docker.

```bash
git clone https://github.com/ty-fi/triangle-shows
cd triangle-shows
cp backend/.env.example backend/.env   # add your TICKETMASTER_API_KEY
docker-compose up
```

The app is available at `http://localhost:8000`. On startup it runs migrations, seeds venues, and triggers an initial scrape.

To trigger a manual scrape:

```bash
curl -X POST http://localhost:8000/api/scrape
```

### Project structure

```
backend/
  app/
    api/          # FastAPI route handlers
    scrapers/     # One scraper per venue/platform
    models.py     # SQLAlchemy ORM (Venue, Event, ScrapeLog)
    scheduler.py  # APScheduler job config
    seed.py       # Venue seed data
frontend/
  index.html
  css/styles.css
  js/
    app.js        # FullCalendar init
    filters.js    # Client-side filter logic
    config.js     # Palettes, API base URL
    modal.js      # Event detail modal
    favorites.js  # Heart/hide/export
```

### Developer tools

Scripts in `tools/` for local development and debugging. Note these are really only tested on my machine. Some may also be outdated.

| Script | Purpose |
|---|---|
| `import_submissions.py` | Import approved show submissions from Google Sheet into DB |
| `check_roots.py` | Verify scraper URL roots resolve correctly |
| `check_venue_urls.py` | Spot-check venue event page URLs |
| `diagnose_scrapers.py` | Run scrapers individually and report output/errors |
| `inspect_html.py` | Print raw HTML from a venue page for scraper debugging |
| `inspect_js_venues.py` | Inspect JS-heavy venue pages for API/widget patterns |
| `wait_for_deploy.py` | If you're deploying with Google Cloud Build this will let you know when the new build is live |
| `run_scrape.py` | Runs the scrape API and provides a summary of what happened. Prints results to `tools/scrape_results.log`. |

---

## Licensing / Contact me:

This project is licensed under the GNU General Public License Version 3.

Contact me at [@tyfi](https://bsky.app/profile/tyfi.bsky.social).
