# Architecture

How a request reaches the app, and how a commit becomes a deploy. Values here were verified
against the live infrastructure on 2026-08-27.

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
  ├── durm-shows.net ──────┐  (and durm.triangle-shows.net)
  │                        │   same app, same origin; the Durham variant is chosen
  │                        │   client-side from the hostname, not by routing
  │                        ▼
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
    // The hashed .run.app hostname — see "Origin hostnames" below for why it is not
    // written out here, and how to look up the live value.
    url.hostname = 'triangle-shows-<hash>-ue.a.run.app';
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
| triangle-shows.net | CNAME | `@` | the hashed `.run.app` hostname | Yes |
| triangle-shows.net | CNAME | `www` | the hashed `.run.app` hostname | Yes |
| triangle-shows.net | CNAME | `durm` | the hashed `.run.app` hostname | Yes |
| triangle-shows.org | A | `@` | `192.0.2.1` (dummy) | Yes |
| durm-shows.net | CNAME | `@` | the hashed `.run.app` hostname | Yes |
| durm-shows.net | CNAME | `www` | the hashed `.run.app` hostname | Yes |

Every hostname that should reach the app needs **two** things, not one: a proxied DNS
record *and* a Worker route. The record alone gets the request to Cloudflare, where it
arrives with `Host: durm-shows.net` and Cloud Run answers 404 — the Worker is what rewrites
the Host header. See "Sites and hostnames" below.

SSL mode is **Full**, with Always Use HTTPS on. Full works because the `.run.app` origin carries
a valid Google-managed certificate.

### Sites and hostnames

One deployment serves both sites. There is no second service, no second build, and nothing
host-aware on the backend — the Worker rewrites every incoming Host to the `.run.app` name,
so FastAPI cannot tell the hostnames apart and does not need to.

The Durham variant is selected **client-side**, by `detectSiteConfig()` in
`frontend/js/config.js`, from `window.location.hostname`:

| Hostname | Site |
|---|---|
| `durm-shows.net`, `www.durm-shows.net` | Durham |
| `durm.*` (i.e. `durm.triangle-shows.net`) | Durham |
| `durm-shows.localhost` | Durham — local preview only |
| anything else | main Triangle site |

That variant filters venues to `SITE_CONFIG.city`, swaps the ASCII title and subtitle, and
pins the `durham` palette while hiding the palette picker.

**Adding another city site** therefore needs three things and no infrastructure work beyond
the first two:

1. A proxied DNS record for the hostname, pointing at the hashed `.run.app` name
2. A Worker route covering `<hostname>/*`, so the Host header gets rewritten
3. A branch in `detectSiteConfig()` returning that city's config

**Consequence worth remembering:** because selection is client-side and the origin is
host-blind, a hostname with DNS but no Worker route returns 404 for the whole site, and a
hostname with both but no `detectSiteConfig()` branch silently serves the *main* Triangle
site rather than erroring.

### Origin hostnames

The service answers on two `.run.app` hostnames, both in active use:

| Form | Used by |
|---|---|
| hashed — `triangle-shows-<hash>-ue.a.run.app` | Cloudflare Worker and the `.net` CNAMEs |
| project-number — `triangle-shows-<project-number>.us-east1.run.app` | Cloud Scheduler |

Cloud Run issues the project-number form for newer services and keeps the older hashed form
working. They are the same service. This matters only when changing the hostname: the Worker
hardcodes the hashed form, so updating one without the other breaks the site.

**Both are deliberately left out of this repo, which is public.** No `--ingress` flag is set on
the Cloud Run service, so it defaults to `ingress=all` and these hostnames reach the app directly,
skipping Cloudflare and anything enforced there — including a Cloudflare Access policy on
`/admin`. The `.net` CNAMEs are proxied, so the origin is not discoverable through public DNS
either; this document was the only place it was written down.

Treat that as reduced disclosure, not as protection: the origin stays directly reachable until
ingress is restricted or the app requires a header only the Worker sets. Anything that must not be
public needs its own authentication regardless of who knows the hostname.

Look up the live values with:

```bash
gcloud run services describe triangle-shows \
  --project=triangle-shows --region=us-east1 --format='value(status.url)'
```

Both hostnames are also recorded in `_SESSION-CONTEXT.md`, which is gitignored.

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
| `ENABLE_STARTUP_SCRAPE` | `false` | See below — a detached task cannot work under this service's CPU allocation |
| `LOG_LEVEL` | `INFO` | |
| `DATABASE_URL` | secret `triangle-shows-db-url` | |
| `TICKETMASTER_API_KEY` | secret `triangle-shows-tm-api-key` | |
| `SCRAPE_ALLOWED_SERVICE_ACCOUNTS` | the scheduler's service account | Gates `POST /api/scrape` — see Origin gates |
| `SCRAPE_OIDC_AUDIENCE` | `https://triangle-shows.net/api/scrape` | The audience configured on the scheduler job |
| `CF_ACCESS_TEAM_DOMAIN` | the Cloudflare Zero Trust team domain | Gates `/admin/*` |
| `CF_ACCESS_AUD` | the Access application's AUD tag | An identifier, not a credential — see Origin gates |

**No admin secrets exist, and none are needed.** `ADMIN_PASSWORD` and `SESSION_SECRET` are
absent from Secret Manager and their `cloudbuild.yaml` lines are commented out deliberately:
`/admin` authenticates from the Cloudflare Access identity instead, so there is no shared
password to distribute or rotate, and with no password login there is no cookie to sign.
Access to the admin surface is granted by adding someone to the `triangle-shows` GitHub
organization and revoked by removing them.

**Why the on-boot scrape is off.** It ran as a detached asyncio task, and this service sets no
CPU-throttling override, so Cloud Run allocates CPU only while a request is in flight — the
task was starved as soon as startup finished. On the 2026-08-27 release it deleted rows in
dribs over five minutes as health checks briefly granted CPU, never reached the reclassify
pass at the end, and emitted no stdout at all, so the live-music filter shipped classifying
nothing with no logs to say so. Cloud Scheduler already calls `POST /api/scrape` every six
hours inside a real request, which is where a scrape belongs.

### Origin gates

The service accepts unauthenticated requests — it has to, for the public site to work — so its
`.run.app` hostnames reach the app directly, skipping Cloudflare and anything enforced there.
A Cloudflare Access policy on `triangle-shows.net/admin` therefore protects only the Cloudflare
path; on its own it leaves the origin open.

`backend/app/tokens.py` closes that by making the application itself require proof that a
request passed through a trusted issuer. Both issuers sign a short-lived token and publish the
matching public keys, so verification is local arithmetic against cached keys:

| Route | Issuer | Header |
|---|---|---|
| `/admin/*` | Cloudflare Access | `Cf-Access-Jwt-Assertion` |
| `POST /api/scrape` | Google (Cloud Scheduler's OIDC token) | `Authorization: Bearer …` |

Four checks on each: signature, expiry, audience, issuer. Both gates are inert until their env
vars are set, and `log_enforcement_state()` states on every boot which are live — a control that
silently does nothing when misconfigured is worse than an absent one, because it reads as
protection.

Verified in production: `GET https://<origin>/admin` and `POST https://<origin>/api/scrape` both
return 403, while a signed-in request through Cloudflare reaches the app normally.

Restricting Cloud Run ingress is *not* an alternative here. `internal-and-cloud-load-balancing`
admits only Google's load balancer, and Cloudflare is not one — it reaches the origin over the
public internet like any other client. Making that setting work would mean reintroducing the
GCP HTTPS Load Balancer deleted during the Cloudflare migration, which is also why IAP is out.

## Scheduled scraping

Cloud Scheduler job `triangle-shows-scrape` posts to `/api/scrape` on `0 */6 * * *` (UTC), using
the project-number Cloud Run URL and an OIDC token from a dedicated service account,
`triangle-shows-scrape@…`, rather than the project's default compute identity. This is why
`ENABLE_SCHEDULER` is `false` in the container — the schedule lives outside the app, so a
scaled-to-zero service still gets scraped, and two running instances don't scrape twice.

**The application verifies that token** (see Origin gates). Cloud Run cannot do it for us,
because the service must accept unauthenticated requests for the public site to work, so the
token arrives and is simply ignored unless the app checks it. The allowlisted service-account
email is the substantive control; granting a second caller is a matter of adding its email to
`SCRAPE_ALLOWED_SERVICE_ACCOUNTS`, which is configuration rather than code.

Since this endpoint drives the whole dataset, note that a scrape also runs the reclassify pass
and the reconcile that deletes listings a source has stopped carrying. Its response reports both
the per-venue counts and the reclassify result, so a manual trigger says what it did without
needing the logs.

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

External monitoring runs on **[UptimeRobot](https://uptimerobot.com)** (free plan):

| | |
|---|---|
| Target | `https://triangle-shows.net/api/health` |
| Interval | every 15 minutes |
| Alerts | email |

**The monitor must target the public hostname, not the `.run.app` URL.** The Cloudflare Worker
is the component whose failure takes the public site down while Cloud Run stays perfectly
healthy — a check against the origin would report all-clear through exactly that outage. Going
through the public hostname exercises DNS, Cloudflare, the Worker, Cloud Run, and Neon in one
request.

Keep the request timeout generous (~30s). With `min-instances=0` the first request after an
idle period pays a 2–3 second cold start, and at a 15-minute interval most checks arrive after
Cloud Run has already scaled down — so cold starts are the normal case here, not the exception.

**Detection latency is roughly 15–30 minutes.** One missed check takes up to 15 minutes to
occur, and if the monitor is set to alert only after two consecutive failures, that doubles.
That's a deliberate tradeoff against false alarms rather than an oversight; shortening the
interval is the lever if an outage needs noticing sooner.

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
