"""
Verification of signed tokens, so the Cloud Run origin can tell who a request came from.

Role: Imported by main.py to guard two routes that must not be open on the origin —
`/admin/*` (Cloudflare Access) and `POST /api/scrape` (Google OIDC, as sent by Cloud
Scheduler). Pure verification: no database, no application state.

Why this exists. The Cloud Run service sets no `--ingress` restriction, so its `.run.app`
hostnames answer the public internet directly, skipping Cloudflare and anything enforced
there. A Cloudflare Access policy on `triangle-shows.net/admin` therefore protects only
the Cloudflare path; the origin remains reachable. Redacting the hostname (see
docs/ARCHITECTURE.md) reduces disclosure but is not a control — the hostname is derived
from the service name and project number rather than from anything secret.

The fix is for the origin to require proof that a request passed through a trusted issuer.
Both issuers here sign a short-lived token with a private key and publish the matching
public keys at a fixed URL, so verification is local arithmetic against keys already
fetched: no callback to the issuer on the request path, and a forged token is not
possible without the issuer's private key.

Four checks, and the audience check is the one worth being explicit about: without it a
token the issuer legitimately minted for a *different* application would verify here.
Signature, expiry, audience, issuer — all four or the token is rejected.

Requires: `pyjwt[crypto]`. Configuration lives in app.config (CF_ACCESS_*, SCRAPE_*);
each gate is inert until configured, which is what keeps local dev and CI working.
"""

# --- Imports ---
import logging
from typing import Any, Optional, Sequence

import jwt
from jwt import PyJWKClient

logger = logging.getLogger(__name__)


# --- Issuer endpoints ---

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"

# Google has emitted both spellings over the years and both remain valid on ID tokens.
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")

# Only asymmetric signing is accepted. Allowing an HMAC algorithm here would let a caller
# who knows the (public) verification key sign their own tokens with it.
ALLOWED_ALGORITHMS = ("RS256",)

# Tolerance for clock drift between the issuer and this container, in seconds.
CLOCK_LEEWAY = 60


class TokenError(Exception):
    """A token was absent, malformed, or failed one of the four checks."""


# --- Key fetching ---

# One client per JWKS URL, module-scoped. Each client holds the keys it has fetched and
# refetches only on seeing an unknown key id, so constructing one per request would turn
# every request into an outbound HTTPS call and defeat the point of caching.
_jwks_clients: dict[str, PyJWKClient] = {}


def _jwks_client(url: str) -> PyJWKClient:
    client = _jwks_clients.get(url)
    if client is None:
        client = PyJWKClient(url, cache_jwk_set=True, lifespan=3600)
        _jwks_clients[url] = client
    return client


# --- Core verification ---

def verify_token(
    token: Optional[str],
    *,
    jwks_url: str,
    issuers: Sequence[str],
    audience: Optional[str],
) -> dict[str, Any]:
    """Verify a signed token and return its claims, or raise TokenError.

    `audience` may be None to skip only the audience check — see the callers for when
    that is a defensible trade and when it is not. Every other check always runs.

    Raises TokenError for every failure mode, so a caller can turn any of them into one
    response without distinguishing between them. Deliberate: telling an unauthenticated
    caller *why* their token was rejected is free reconnaissance.
    """
    if not token:
        raise TokenError("no token presented")

    try:
        signing_key = _jwks_client(jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=list(ALLOWED_ALGORITHMS),
            audience=audience,
            leeway=CLOCK_LEEWAY,
            options={
                "require": ["exp"],
                "verify_exp": True,
                "verify_aud": audience is not None,
            },
        )
    except jwt.PyJWTError as exc:
        raise TokenError(f"{type(exc).__name__}: {exc}") from exc
    except Exception as exc:
        # Key fetching failures (network, malformed JWKS) land here. Treated the same as a
        # bad token: fail closed. An issuer we cannot reach is not an issuer we can trust.
        raise TokenError(f"key retrieval failed: {type(exc).__name__}: {exc}") from exc

    # Issuer is checked here rather than passed to jwt.decode() because Google accepts two
    # spellings and decode() takes a single value.
    issuer = claims.get("iss")
    if issuer not in issuers:
        raise TokenError(f"unexpected issuer: {issuer!r}")

    return claims


# --- Cloudflare Access (guards /admin/*) ---

def cloudflare_access_configured() -> bool:
    """True when both settings needed to enforce the Access gate are present."""
    from app.config import settings

    return bool(settings.CF_ACCESS_TEAM_DOMAIN and settings.CF_ACCESS_AUD)


def verify_cloudflare_access(token: Optional[str]) -> dict[str, Any]:
    """Verify a Cloudflare Access token and return its claims.

    Cloudflare mints this after the visitor signs in against the configured identity
    provider, and attaches it to every request it forwards as `Cf-Access-Jwt-Assertion`.
    A request arriving at the origin directly carries no such header.

    The audience is the Access application's AUD tag and is required: a Cloudflare team
    can host several applications, and without this check a token issued for any of them
    would open the admin surface.
    """
    from app.config import settings

    team_domain = settings.CF_ACCESS_TEAM_DOMAIN.strip().rstrip("/")
    # Accept the team domain with or without a scheme, since the dashboard shows it bare.
    if "://" in team_domain:
        team_domain = team_domain.split("://", 1)[1]

    return verify_token(
        token,
        jwks_url=f"https://{team_domain}/cdn-cgi/access/certs",
        issuers=(f"https://{team_domain}",),
        audience=settings.CF_ACCESS_AUD.strip(),
    )


def access_identity(claims: dict[str, Any]) -> str:
    """The signed-in person's email, for logging and attribution.

    Access always sets `email` for an interactive login; `sub` is the fallback for a
    service token, which has no human behind it.
    """
    return claims.get("email") or claims.get("sub") or "unknown"


# --- Google OIDC (guards POST /api/scrape) ---

def scrape_allowed_service_accounts() -> tuple[str, ...]:
    """Service-account emails permitted to trigger a scrape.

    Comma-separated in the environment so a second caller is a configuration change
    rather than a code change.
    """
    from app.config import settings

    raw = settings.SCRAPE_ALLOWED_SERVICE_ACCOUNTS or ""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def scrape_token_configured() -> bool:
    """True when at least one service account is allowlisted, which enables the gate."""
    return bool(scrape_allowed_service_accounts())


def verify_scrape_token(token: Optional[str]) -> dict[str, Any]:
    """Verify a Google-issued OIDC token from Cloud Scheduler and return its claims.

    Cloud Scheduler is already configured to attach this token, so nothing changes on the
    scheduler side — the endpoint simply starts reading what has been arriving all along.
    Cloud Run cannot enforce it for us: the service must accept unauthenticated requests
    for the public site to work, so the check has to happen in the application.

    The allowlisted service-account email is the substantive control here. Only a caller
    holding that identity can obtain a Google-signed token bearing it, and an attacker
    cannot mint one.

    SCRAPE_OIDC_AUDIENCE is honored when set and skipped when not. Skipping it is a real
    if narrow weakening: it would permit a token this same service account obtained for a
    *different* audience to be replayed here. With one scheduler job and one service
    account there is no such second audience, but set it when the value is known.
    """
    from app.config import settings

    audience = (settings.SCRAPE_OIDC_AUDIENCE or "").strip() or None
    claims = verify_token(
        token,
        jwks_url=GOOGLE_JWKS_URL,
        issuers=GOOGLE_ISSUERS,
        audience=audience,
    )

    email = claims.get("email")
    allowed = scrape_allowed_service_accounts()
    if email not in allowed:
        raise TokenError(f"service account not allowlisted: {email!r}")

    return claims


def bearer_token(header_value: Optional[str]) -> Optional[str]:
    """Pull the token out of an `Authorization: Bearer <token>` header."""
    if not header_value:
        return None
    scheme, _, value = header_value.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


# --- Startup reporting ---

def log_enforcement_state() -> None:
    """Say plainly, once per boot, which gates are live.

    A security control that silently does nothing when misconfigured is worse than one
    that is absent, because it reads as protection. These lines are how you tell which
    you have.
    """
    if cloudflare_access_configured():
        logger.info("[tokens] /admin: Cloudflare Access enforced at the origin")
    else:
        logger.warning(
            "[tokens] /admin: Cloudflare Access NOT enforced (CF_ACCESS_TEAM_DOMAIN "
            "and CF_ACCESS_AUD are not both set). The origin hostname reaches /admin "
            "without passing Cloudflare."
        )

    if scrape_token_configured():
        audience_note = "with audience check" if _scrape_audience_set() else "without audience check"
        logger.info(
            f"[tokens] POST /api/scrape: Google OIDC enforced for "
            f"{len(scrape_allowed_service_accounts())} service account(s), {audience_note}"
        )
    else:
        logger.warning(
            "[tokens] POST /api/scrape: NOT enforced "
            "(SCRAPE_ALLOWED_SERVICE_ACCOUNTS is empty). Anyone who knows the origin "
            "hostname can trigger a full scrape."
        )


def _scrape_audience_set() -> bool:
    from app.config import settings

    return bool((settings.SCRAPE_OIDC_AUDIENCE or "").strip())
