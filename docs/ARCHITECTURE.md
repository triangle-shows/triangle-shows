# Architecture

How a request reaches the app, and how a commit becomes a deploy. Values here were verified
against the live infrastructure on 2026-08-26.

For the branch and release workflow see [CONTRIBUTING.md](../.github/CONTRIBUTING.md).
For running the app on your own machine see [SELF-HOSTING.md](SELF-HOSTING.md).

## The pieces

| Component | Role | Configured in |
|-----------|------|---------------|
| **Cloudflare** | DNS, SSL, and a Worker that proxies to Cloud Run | Cloudflare dashboard (free plan) |
| **Cloud Run** | Runs the container; scales to zero when idle | `cloudbuild.yaml` deploy step |
| **Cloud Build** | Builds the image and deploys it on push to `prod` | `cloudbuild.yaml` + a console trigger |
| **Artifact Registry** | Stores built Docker images | GCP, repo `triangle-shows` |
| **Secret Manager** | Holds the DB URL and Ticketmaster API key | GCP, injected as env vars at deploy |
| **Cloud Scheduler** | Calls `/api/scrape` every 6 hours | GCP, job `triangle-shows-scrape` |
| **Neon** | Managed PostgreSQL, the only stateful component | Neon dashboard; URL stored in Secret Manager |

GCP project is `triangle-shows`, everything in `us-east1`.

## Request path

```
Browser
  │
  ├── triangle-shows.org ──► Cloudflare ──► 301 redirect ──► triangle-shows.net
  │                          (never reaches an origin server)
  │
  └── triangle-shows.net ──► Cloudflare DNS (proxied)
                               │
                               ├─ SSL terminated here (Full mode, free managed cert)
                               │
                               ▼
                             Worker: triangle-shows-proxy
                               │  rewrites the Host header
                               ▼
                             Cloud Run: triangle-shows (us-east1)
                               │
                               ▼
                             uvicorn ──► FastAPI
                               │
                               ├─ serves the static frontend
                               └─ /api/* ──► SQLAlchemy ──► Neon PostgreSQL
```

**Why the Worker exists.** Cloud Run routes requests by their `Host` header and only recognizes
its own `.run.app` hostname — a request arriving with `Host: triangle-shows.net` gets a 404.
Cloudflare's Transform Rules cannot rewrite `host` (it's a protected header), so a small Worker
does it instead:

```javascript
export default {
  async fetch(request) {
    const url = new URL(request.url);
    url.hostname = 'triangle-shows-bs2va6mvba-ue.a.run.app';
    return fetch(new Request(url.toString(), request));
  }
}
```

It is routed to `triangle-shows.net/*`. If this Worker is removed or misconfigured, the whole
site returns 404 while Cloud Run itself stays perfectly healthy — worth knowing before debugging
the app.

**The `.org` domain** has a deliberately fake `A` record pointing at `192.0.2.1`, an address
reserved for documentation that never carries traffic. It exists only so Cloudflare has something
to attach a proxied record to; a Redirect Rule issues the 301 at the edge, so no origin is ever
contacted.

### DNS

| Domain | Type | Name | Target | Proxied |
|---|---|---|---|---|
| triangle-shows.net | CNAME | `@` | `triangle-shows-bs2va6mvba-ue.a.run.app` | Yes |
| triangle-shows.net | CNAME | `www` | `triangle-shows-bs2va6mvba-ue.a.run.app` | Yes |
| triangle-shows.org | A | `@` | `192.0.2.1` (dummy) | Yes |

SSL mode is **Full**, with Always Use HTTPS on. Full works because the `.run.app` origin carries
a valid Google-managed certificate.

### Two Cloud Run URLs

The service answers on both of these, and both are in active use:

```
https://triangle-shows-bs2va6mvba-ue.a.run.app        ← Cloudflare Worker and CNAMEs
https://triangle-shows-178508749672.us-east1.run.app  ← Cloud Scheduler
```

Cloud Run issues the second, project-number form for newer services and keeps the older hashed
form working. They are the same service. This matters only when changing the hostname: the Worker
hardcodes the first, so updating one without the other breaks the site.

## Deploy path

```
PR merged into prod
  │
  ▼
Cloud Build trigger  auto-deploy-git-pushes-to-prod  (branch regex ^prod$)
  │
  ├─ 1. docker build       (Dockerfile, GIT_COMMIT baked in as $COMMIT_SHA)
  ├─ 2. docker push        (Artifact Registry: us-east1-docker.pkg.dev/triangle-shows/triangle-shows/api)
  └─ 3. gcloud run deploy  (new Cloud Run revision, secrets attached)
        │
        ▼
      Container starts, uvicorn boots FastAPI
        │
        ▼
      Startup lifespan runs `alembic upgrade head` against Neon
        │
        ▼
      Revision serves traffic; /api/health reports the new SHA
```

Only pushes to `prod` build anything. Pushes to `main` and feature branches do nothing, which is
what keeps un-shipped work away from the production database.

The image is tagged twice — `:$COMMIT_SHA` and `:latest`. The commit SHA is also baked into the
image as the `GIT_COMMIT` build argument, which is how `/api/health` can report exactly which
commit is live. That's what [`tools/wait_for_deploy.py`](../tools/wait_for_deploy.py) polls for.

### Runtime configuration

| Setting | Value | Why |
|---|---|---|
| min instances | 0 | Scales to zero when idle; costs ~$0 at rest, ~2–3s cold start |
| max instances | 2 | Caps runaway cost |
| memory / CPU | 512Mi / 1 | |
| request timeout | 300s | Scrape requests can be slow |
| `APP_ENV` | `production` | |
| `ENABLE_SCHEDULER` | `false` | Scraping is driven externally by Cloud Scheduler, not in-process |
| `LOG_LEVEL` | `INFO` | |
| `DATABASE_URL` | secret `triangle-shows-db-url` | |
| `TICKETMASTER_API_KEY` | secret `triangle-shows-tm-api-key` | |

The admin subsite (`/admin`) is **disabled**. It requires `ADMIN_PASSWORD` and `SESSION_SECRET`,
neither of which exists in Secret Manager, and the corresponding lines in `cloudbuild.yaml` are
commented out. It fails closed, so the rest of the site is unaffected. Create both secrets
*before* uncommenting, or the deploy fails on a missing secret.

## Scheduled scraping

Cloud Scheduler job `triangle-shows-scrape` posts to `/api/scrape` on `0 */6 * * *` (UTC), using
the project-number Cloud Run URL and OIDC authentication. This is why `ENABLE_SCHEDULER` is
`false` in the container — the schedule lives outside the app, so a scaled-to-zero service still
gets scraped, and two running instances don't scrape twice.

## Data

Neon is managed PostgreSQL, running in AWS `us-east-1` and reached through its pooler endpoint.
The app connects with SQLAlchemy's async driver, so the URL is in `postgresql+asyncpg://` form.

**There is one database and no staging copy.** Feature branches and `main` never touch it,
because only `prod` deploys. Migrations execute against real data at deploy time and nowhere
earlier — see the Database & migrations section of
[CONTRIBUTING.md](../.github/CONTRIBUTING.md) for what that implies about writing them.

## Monitoring

`/api/health` is the endpoint to watch. It queries the database for its counts, so a 200
response proves the app is up *and* Neon is reachable — more than a check against `/` would
tell you.

It also reports **503 with `status: "stale"`** when the most recent successful scrape is older
than 13 hours (two scheduler cycles plus slack). That turns silently-stopped scraping into
something a plain uptime monitor can catch, rather than a failure that looks healthy until
someone notices the listings are old. The response body is identical either way, so tools that
only want the deployed SHA can still read it from a 503.

Freshness is enforced only when `APP_ENV=production`. CI and local runs disable scraping on
purpose, so an empty `ScrapeLog` is expected there and must not fail the smoke test.

### Uptime checking

External monitoring runs on **[UptimeRobot](https://uptimerobot.com)** (free plan), checking
`https://triangle-shows.net/api/health`.

**The monitor must target the public hostname, not the `.run.app` URL.** The Cloudflare Worker
is the component whose failure takes the public site down while Cloud Run stays perfectly
healthy — a check against the origin would report all-clear through exactly that outage. Going
through the public hostname exercises DNS, Cloudflare, the Worker, Cloud Run, and Neon in one
request.

Two settings worth keeping as they are: a generous request timeout (~30s), because
`min-instances=0` means the first request after an idle period pays a 2–3 second cold start;
and alerting only after two consecutive failures, so a single blip doesn't page anyone.

## If something breaks

| Symptom | Likely cause |
|---|---|
| Whole site 404s, but the `.run.app` URL works | Cloudflare Worker missing or pointing at the wrong hostname |
| Site unreachable, `.run.app` fine | Cloudflare DNS or proxy status |
| Deploy never goes live | Check Cloud Build history — the trigger only fires on `prod` |
| `/api/health` reports an old SHA | The new revision failed to start; check Cloud Run logs for a migration error |
| Scrapes stopped | Cloud Scheduler job disabled, or its OIDC service account lost permission |

Recovery notes: the Cloud Run service itself needs no manual recreation — `gcloud run deploy`
creates it if absent, so a push to `prod` rebuilds it from nothing. The pieces that are *not*
reproducible from this repo are the Cloudflare configuration above, the Secret Manager values,
and Neon itself.

## Cost

Roughly $3–4/month, nearly all of it Neon and Artifact Registry storage. Cloud Run is ~$0 at
`min-instances=0`, and the Cloudflare free plan covers DNS, SSL, and the Worker.

This was ~$20/month until 2026-05-22, when a GCP HTTPS Load Balancer was replaced by Cloudflare.
Forwarding rules alone billed $0.025/hr each regardless of traffic. Historical detail on that
migration, including the exact resources deleted, lives in the internal repo's
`guides/hosting.md`.
