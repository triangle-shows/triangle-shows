"""
Tests for app.tokens — the signed-token gates on /admin/* and POST /api/scrape.

These verify the checks that actually stop a forged request, so each one is written to
fail if the corresponding check is removed:

  * signature   — a token signed by a different key must not verify
  * expiry      — an expired token must not verify
  * audience    — a token minted for another application must not verify
  * issuer      — a token from an unexpected issuer must not verify
  * algorithm   — an HMAC-signed token must not verify, even signed with the public key

The last one is the subtle one. RS256 verification uses a *public* key, which anyone can
fetch. If HS256 were also accepted, a caller could sign their own token using that public
key as the shared secret and it would verify. Restricting algorithms to RS256 is what
prevents it, and the test below is the only thing that would catch a regression there.

A real RSA keypair is generated once per module and the JWKS lookup is redirected at it,
so nothing here touches the network. Key generation costs well under a second.
"""

import base64
import hashlib
import hmac
import json
import time
from typing import Optional

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import tokens


# --- Keys and signing helpers ---

@pytest.fixture(scope="module")
def keypair():
    """One RSA keypair for the whole module: generation is the slow part."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


@pytest.fixture(scope="module")
def other_keypair():
    """A second, unrelated keypair — stands in for an attacker's own key."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


def _pem_private(key) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@pytest.fixture(autouse=True)
def redirect_jwks(monkeypatch, keypair):
    """Point every JWKS lookup at the module's public key, with no network access.

    Mirrors what PyJWKClient returns: an object exposing the verification key as `.key`.
    """
    _, public = keypair

    class _StubKey:
        key = public

    class _StubClient:
        def get_signing_key_from_jwt(self, token):
            return _StubKey()

    monkeypatch.setattr(tokens, "_jwks_client", lambda url: _StubClient())


def make_token(
    private_key,
    *,
    issuer: str,
    audience: Optional[str],
    email: Optional[str] = None,
    expires_in: int = 600,
) -> str:
    now = int(time.time())
    claims = {"iss": issuer, "iat": now, "exp": now + expires_in}
    if audience is not None:
        claims["aud"] = audience
    if email is not None:
        claims["email"] = email
    return jwt.encode(claims, _pem_private(private_key), algorithm="RS256")


def _public_pem(public_key) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def make_hmac_token(secret: bytes, claims: dict) -> str:
    """Build an HS256 token by hand, signed with `secret`.

    Assembled manually rather than through jwt.encode(), which refuses a PEM public key
    as an HMAC secret. That guard protects the signing side; this test is about the
    verifying side, and an attacker writing 10 lines of base64 and hmac is not constrained
    by our library's opinions.
    """
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    signature = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(signature)}"


# --- Cloudflare Access gate (/admin/*) ---

TEAM = "triangleshows.cloudflareaccess.com"
CF_ISSUER = f"https://{TEAM}"
CF_AUD = "abc123def456"


@pytest.fixture
def cf_configured(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "CF_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.setattr(settings, "CF_ACCESS_AUD", CF_AUD)
    return settings


class TestCloudflareAccessGate:
    def test_valid_token_is_accepted_and_carries_the_email(self, keypair, cf_configured):
        private, _ = keypair
        token = make_token(private, issuer=CF_ISSUER, audience=CF_AUD, email="a@example.org")

        claims = tokens.verify_cloudflare_access(token)

        assert tokens.access_identity(claims) == "a@example.org"

    def test_missing_token_is_rejected(self, cf_configured):
        with pytest.raises(tokens.TokenError):
            tokens.verify_cloudflare_access(None)

    def test_garbage_token_is_rejected(self, cf_configured):
        with pytest.raises(tokens.TokenError):
            tokens.verify_cloudflare_access("not-a-token")

    def test_token_signed_by_another_key_is_rejected(self, other_keypair, cf_configured):
        """The core forgery case: right claims, wrong signer."""
        private, _ = other_keypair
        token = make_token(private, issuer=CF_ISSUER, audience=CF_AUD, email="a@example.org")

        with pytest.raises(tokens.TokenError):
            tokens.verify_cloudflare_access(token)

    def test_expired_token_is_rejected(self, keypair, cf_configured):
        private, _ = keypair
        token = make_token(
            private, issuer=CF_ISSUER, audience=CF_AUD, email="a@example.org", expires_in=-3600
        )

        with pytest.raises(tokens.TokenError):
            tokens.verify_cloudflare_access(token)

    def test_token_for_another_access_application_is_rejected(self, keypair, cf_configured):
        """A Cloudflare team can host several apps; each token names the one it is for."""
        private, _ = keypair
        token = make_token(
            private, issuer=CF_ISSUER, audience="some-other-app", email="a@example.org"
        )

        with pytest.raises(tokens.TokenError):
            tokens.verify_cloudflare_access(token)

    def test_token_with_no_audience_at_all_is_rejected(self, keypair, cf_configured):
        private, _ = keypair
        token = make_token(private, issuer=CF_ISSUER, audience=None, email="a@example.org")

        with pytest.raises(tokens.TokenError):
            tokens.verify_cloudflare_access(token)

    def test_token_from_an_unexpected_issuer_is_rejected(self, keypair, cf_configured):
        private, _ = keypair
        token = make_token(
            private, issuer="https://attacker.cloudflareaccess.com", audience=CF_AUD
        )

        with pytest.raises(tokens.TokenError):
            tokens.verify_cloudflare_access(token)

    def test_hmac_signed_token_is_rejected(self, keypair, cf_configured):
        """Algorithm confusion: a token signed with the *public* key as an HMAC secret.

        This is the attack the ALLOWED_ALGORITHMS allowlist exists to prevent — the
        verification key is published at the issuer's JWKS URL, so if HS256 were accepted
        an attacker could sign their own tokens with it.

        Honest note on what this test proves. It asserts the property (such a token does
        not verify), not the mechanism. Widening ALLOWED_ALGORITHMS to include HS256 does
        *not* make it fail, because PyJWT's own prepare_key refuses a PEM or key-object
        operand as an HMAC secret, on decode as well as encode. Confirmed by mutation.
        So the allowlist here is defense in depth behind a library guarantee rather than
        the sole control, and no test in this file can pin it while that guarantee holds.
        Keep the allowlist regardless: it is explicit, and it is what would still hold if
        the key handling or the library ever changed underneath.
        """
        now = int(time.time())
        _, public = keypair
        forged = make_hmac_token(
            _public_pem(public),
            {"iss": CF_ISSUER, "aud": CF_AUD, "email": "x@example.org", "iat": now, "exp": now + 600},
        )

        with pytest.raises(tokens.TokenError):
            tokens.verify_cloudflare_access(forged)

    def test_team_domain_with_a_scheme_is_tolerated(self, keypair, monkeypatch):
        """The Cloudflare dashboard shows the domain bare; pasting a URL must still work."""
        from app.config import settings

        monkeypatch.setattr(settings, "CF_ACCESS_TEAM_DOMAIN", f"https://{TEAM}/")
        monkeypatch.setattr(settings, "CF_ACCESS_AUD", CF_AUD)
        private, _ = keypair
        token = make_token(private, issuer=CF_ISSUER, audience=CF_AUD, email="a@example.org")

        assert tokens.verify_cloudflare_access(token)["email"] == "a@example.org"


class TestCloudflareAccessConfiguration:
    def test_not_configured_when_both_unset(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "CF_ACCESS_TEAM_DOMAIN", "")
        monkeypatch.setattr(settings, "CF_ACCESS_AUD", "")
        assert tokens.cloudflare_access_configured() is False

    def test_not_configured_when_only_the_domain_is_set(self, monkeypatch):
        """Half-configured must read as off, not as on — otherwise the audience check,
        which is the one that matters, would be silently skipped."""
        from app.config import settings

        monkeypatch.setattr(settings, "CF_ACCESS_TEAM_DOMAIN", TEAM)
        monkeypatch.setattr(settings, "CF_ACCESS_AUD", "")
        assert tokens.cloudflare_access_configured() is False

    def test_configured_when_both_are_set(self, cf_configured):
        assert tokens.cloudflare_access_configured() is True


# --- Google OIDC gate (POST /api/scrape) ---

SCHEDULER_SA = "triangle-shows-scrape@triangle-shows.iam.gserviceaccount.com"
SCRAPE_AUD = "https://triangle-shows.net/api/scrape"


@pytest.fixture
def scrape_configured(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "SCRAPE_ALLOWED_SERVICE_ACCOUNTS", SCHEDULER_SA)
    monkeypatch.setattr(settings, "SCRAPE_OIDC_AUDIENCE", SCRAPE_AUD)
    return settings


class TestScrapeGate:
    def test_valid_token_from_the_allowlisted_account_is_accepted(self, keypair, scrape_configured):
        private, _ = keypair
        token = make_token(
            private, issuer="https://accounts.google.com", audience=SCRAPE_AUD, email=SCHEDULER_SA
        )

        assert tokens.verify_scrape_token(token)["email"] == SCHEDULER_SA

    def test_the_bare_issuer_spelling_is_accepted(self, keypair, scrape_configured):
        """Google has emitted both 'accounts.google.com' and the https:// form."""
        private, _ = keypair
        token = make_token(
            private, issuer="accounts.google.com", audience=SCRAPE_AUD, email=SCHEDULER_SA
        )

        assert tokens.verify_scrape_token(token)["email"] == SCHEDULER_SA

    def test_a_different_service_account_is_rejected(self, keypair, scrape_configured):
        """A validly signed Google token is not enough — it must name an allowlisted
        account, or any Google customer could trigger a scrape."""
        private, _ = keypair
        token = make_token(
            private,
            issuer="https://accounts.google.com",
            audience=SCRAPE_AUD,
            email="someone-else@other-project.iam.gserviceaccount.com",
        )

        with pytest.raises(tokens.TokenError):
            tokens.verify_scrape_token(token)

    def test_a_token_with_no_email_claim_is_rejected(self, keypair, scrape_configured):
        private, _ = keypair
        token = make_token(private, issuer="https://accounts.google.com", audience=SCRAPE_AUD)

        with pytest.raises(tokens.TokenError):
            tokens.verify_scrape_token(token)

    def test_wrong_audience_is_rejected_when_the_audience_is_configured(self, keypair, scrape_configured):
        private, _ = keypair
        token = make_token(
            private,
            issuer="https://accounts.google.com",
            audience="https://example.org/something-else",
            email=SCHEDULER_SA,
        )

        with pytest.raises(tokens.TokenError):
            tokens.verify_scrape_token(token)

    def test_audience_is_not_checked_when_left_unconfigured(self, keypair, monkeypatch):
        """Documented trade in verify_scrape_token: the service-account check still
        applies, so this stays useful, but set the audience when the value is known."""
        from app.config import settings

        monkeypatch.setattr(settings, "SCRAPE_ALLOWED_SERVICE_ACCOUNTS", SCHEDULER_SA)
        monkeypatch.setattr(settings, "SCRAPE_OIDC_AUDIENCE", "")
        private, _ = keypair
        token = make_token(
            private,
            issuer="https://accounts.google.com",
            audience="whatever-audience",
            email=SCHEDULER_SA,
        )

        assert tokens.verify_scrape_token(token)["email"] == SCHEDULER_SA

    def test_expired_token_is_rejected(self, keypair, scrape_configured):
        private, _ = keypair
        token = make_token(
            private,
            issuer="https://accounts.google.com",
            audience=SCRAPE_AUD,
            email=SCHEDULER_SA,
            expires_in=-60,
        )

        with pytest.raises(tokens.TokenError):
            tokens.verify_scrape_token(token)

    def test_token_signed_by_another_key_is_rejected(self, other_keypair, scrape_configured):
        private, _ = other_keypair
        token = make_token(
            private, issuer="https://accounts.google.com", audience=SCRAPE_AUD, email=SCHEDULER_SA
        )

        with pytest.raises(tokens.TokenError):
            tokens.verify_scrape_token(token)


class TestScrapeConfiguration:
    def test_not_configured_when_the_allowlist_is_empty(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "SCRAPE_ALLOWED_SERVICE_ACCOUNTS", "")
        assert tokens.scrape_token_configured() is False
        assert tokens.scrape_allowed_service_accounts() == ()

    def test_multiple_accounts_are_parsed_and_trimmed(self, monkeypatch):
        """Granting a second caller is a config change, not a code change."""
        from app.config import settings

        monkeypatch.setattr(
            settings, "SCRAPE_ALLOWED_SERVICE_ACCOUNTS", f" {SCHEDULER_SA} , other@x.iam.gserviceaccount.com "
        )

        assert tokens.scrape_allowed_service_accounts() == (
            SCHEDULER_SA,
            "other@x.iam.gserviceaccount.com",
        )
        assert tokens.scrape_token_configured() is True

    def test_blank_entries_are_ignored(self, monkeypatch):
        """A trailing comma must not allowlist an empty email, which would then match a
        token carrying no email claim."""
        from app.config import settings

        monkeypatch.setattr(settings, "SCRAPE_ALLOWED_SERVICE_ACCOUNTS", f"{SCHEDULER_SA},,")
        assert tokens.scrape_allowed_service_accounts() == (SCHEDULER_SA,)


# --- Authorization header parsing ---

class TestBearerToken:
    def test_reads_a_bearer_token(self):
        assert tokens.bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"

    def test_scheme_is_case_insensitive(self):
        assert tokens.bearer_token("bearer abc.def.ghi") == "abc.def.ghi"

    def test_missing_header_yields_none(self):
        assert tokens.bearer_token(None) is None

    def test_empty_header_yields_none(self):
        assert tokens.bearer_token("") is None

    def test_another_scheme_yields_none(self):
        assert tokens.bearer_token("Basic dXNlcjpwYXNz") is None

    def test_bearer_with_no_value_yields_none(self):
        assert tokens.bearer_token("Bearer ") is None
