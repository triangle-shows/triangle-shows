# Running Triangle Shows locally

## Requirements

- [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- A free [Ticketmaster Developer API key](https://developer.ticketmaster.com/) (required for several venues)

## Setup

```bash
git clone https://github.com/ty-fi/triangle-shows
cd triangle-shows

# Copy the example env file and fill in your Ticketmaster API key
cp backend/.env.example backend/.env
```

Open `backend/.env` and replace `your_key_here` with your Ticketmaster API key. The other defaults work as-is for local development:

```env
DATABASE_URL=postgresql+asyncpg://postgres:postgres@db:5432/triangle_shows
TICKETMASTER_API_KEY=your_key_here   # <-- fill this in
ENABLE_SCHEDULER=false
APP_ENV=development
LOG_LEVEL=INFO
```

## Start the app

```bash
docker-compose up
```

This starts two containers: a PostgreSQL database and the FastAPI backend. On first startup it will:

1. Run database migrations (Alembic)
2. Seed the venue list
3. Kick off an initial scrape in the background (takes a minute or two)

The app is available at **http://localhost:8000**.

## Trigger a manual scrape

```bash
# Scrape all venues
curl -X POST http://localhost:8000/api/scrape

# Scrape a single venue type (useful for debugging)
curl -X POST "http://localhost:8000/api/scrape?scraper_type=rhp_events"
```

Scrape results are logged to the database (`ScrapeLog` table) and printed to the Docker console.
