"""
Tests for the CORS configuration in main.py — review item #11.

The setting that matters is allow_credentials. `allow_origins=["*"]` alongside
allow_credentials=True let a hostile page read authenticated responses; with credentials
off, the same "*" is harmless.

Written against measured behavior rather than assumption, because the mechanism is not the
obvious one. Starlette substitutes the caller's Origin for the "*" whenever the request
carries a Cookie header, and it does that regardless of allow_credentials — so the origin
echo is not the vulnerability and removing it is not the fix. What a browser actually
requires before exposing a credentialed cross-origin response is the
Access-Control-Allow-Credentials header, and that is what is now absent.

These assertions therefore pin the header that carries the security property, and
deliberately do not pin the echo, which is Starlette's business and may change.
"""

from fastapi.testclient import TestClient

import pytest

from app.main import app


HOSTILE = "https://evil.example"


@pytest.fixture
def client():
    return TestClient(app)


class TestCredentialsAreNeverAllowed:
    """The load-bearing assertion. If this fails, a cross-origin page can read
    authenticated responses and review item #11 has regressed."""

    def test_no_credentials_header_on_a_plain_cross_origin_request(self, client):
        response = client.get("/openapi.json", headers={"Origin": HOSTILE})
        assert response.headers.get("access-control-allow-credentials") is None

    def test_no_credentials_header_when_the_request_carries_a_cookie(self, client):
        """The case that mattered: a hostile page causing the victim's browser to send its
        session cookie. The response must not be readable by that page."""
        response = client.get(
            "/openapi.json",
            headers={"Origin": HOSTILE, "Cookie": "ts_admin=stolen-or-ambient"},
        )
        assert response.headers.get("access-control-allow-credentials") is None

    def test_no_credentials_header_on_a_preflight(self, client):
        response = client.options(
            "/api/events",
            headers={
                "Origin": HOSTILE,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert response.headers.get("access-control-allow-credentials") is None


class TestOnlyTheSitesOwnOriginsMayReadFromABrowser:
    """Updated on purpose, as the class this replaced asked to be.

    It used to assert that *any* page could read the API, which was true and deliberate
    while allow_origins was "*". That has been narrowed alongside #62's gate, to stop a
    third party running a competing calendar whose JavaScript calls this API from their own
    users' browsers.

    The distinction that makes this worth having: CORS is a browser protection, so this
    closes the parasitic-frontend case and does nothing whatsoever to a server-side scraper.
    These tests should not be read as evidence the data is protected.
    """

    def test_a_hostile_origin_cannot_read(self, client):
        """The change itself. A page on another domain gets no usable allow-origin header,
        so the browser discards the response before that page can read it.

        Both branches matter, and the first version of this test only had one. Asserting
        merely that the header is not the hostile origin passes against `allow_origins=["*"]`
        — the wildcard is not equal to the origin while permitting it completely — so a
        revert to the old policy would have gone unnoticed here.
        """
        allowed = client.get(
            "/openapi.json", headers={"Origin": HOSTILE}
        ).headers.get("access-control-allow-origin")
        assert allowed != HOSTILE, "the hostile origin was echoed back"
        assert allowed != "*", "the wildcard is back, which permits every origin"

    @pytest.mark.parametrize("origin", [
        "https://triangle-shows.net",
        "https://www.triangle-shows.net",
        "https://durm.triangle-shows.net",
        "https://durm-shows.net",
        "https://www.durm-shows.net",
    ])
    def test_every_real_site_origin_is_allowed(self, client, origin):
        """The regression that would break a site. All five are served by this app — the two
        domains, their www forms, and the older durm.* subdomain."""
        response = client.get("/openapi.json", headers={"Origin": origin})
        assert response.headers.get("access-control-allow-origin") == origin

    def test_a_request_with_no_origin_is_untouched(self, client):
        """Every calendar client polling /feeds/events.ics sends no Origin, so it is outside
        CORS entirely. Narrowing the allowlist must not reach them."""
        response = client.get("/openapi.json")
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") is None

    def test_credentials_are_still_refused(self, client):
        """allow_credentials=False is what made the old "*" merely permissive rather than
        dangerous. Narrowing origins is not a reason to relax it, and a future change that
        does should fail here."""
        response = client.get(
            "/openapi.json", headers={"Origin": "https://triangle-shows.net"}
        )
        assert response.headers.get("access-control-allow-credentials") is None


class TestLocalOriginsAreDevelopmentOnly:
    def test_localhost_is_not_trusted_in_production(self):
        """A deployment must not end up trusting a loopback origin. Asserted against the
        list rather than a live request, since APP_ENV is fixed once the module is imported.
        """
        import importlib

        from app.config import settings

        original = settings.APP_ENV
        try:
            settings.APP_ENV = "production"
            import app.main

            reloaded = importlib.reload(app.main)
            assert not any("localhost" in o for o in reloaded.ALLOWED_ORIGINS)
            assert not any("127.0.0.1" in o for o in reloaded.ALLOWED_ORIGINS)
        finally:
            settings.APP_ENV = original
            importlib.reload(app.main)

    def test_localhost_is_available_outside_production(self):
        from app.main import ALLOWED_ORIGINS

        assert any("localhost" in o for o in ALLOWED_ORIGINS)
