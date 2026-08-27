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


class TestThePublicApiStaysBrowserConsumable:
    """The other half of the decision: dropping credentials rather than narrowing origins
    keeps the public read API usable from any page. If someone later swaps to an origin
    allowlist, these are the tests that should be updated on purpose rather than deleted."""

    def test_a_cross_origin_read_is_permitted(self, client):
        response = client.get("/openapi.json", headers={"Origin": HOSTILE})
        assert response.headers.get("access-control-allow-origin") is not None
