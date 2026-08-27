"""
Tests that the origin gates are actually wired into the app, not merely implemented.

test_tokens.py covers whether a token verifies. This file covers the separate question
of whether an unverified request is stopped — which is a property of main.py's middleware
and dependency, and would break silently if either were unregistered or misordered.

Requests go through TestClient without its context manager on purpose: that skips the
lifespan handler, so no migrations run and no database is needed. Only routes that answer
before touching the session are exercised here.
"""

from fastapi.testclient import TestClient

import pytest

from app.config import settings
from app.main import app


TEAM = "triangleshows.cloudflareaccess.com"
SCHEDULER_SA = "triangle-shows-scrape@triangle-shows.iam.gserviceaccount.com"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def gates_off(monkeypatch):
    monkeypatch.setattr(settings, "CF_ACCESS_TEAM_DOMAIN", "")
    monkeypatch.setattr(settings, "CF_ACCESS_AUD", "")
    monkeypatch.setattr(settings, "SCRAPE_ALLOWED_SERVICE_ACCOUNTS", "")


@pytest.fixture
def gates_on(monkeypatch):
    monkeypatch.setattr(settings, "CF_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.setattr(settings, "CF_ACCESS_AUD", "aud123")
    monkeypatch.setattr(settings, "SCRAPE_ALLOWED_SERVICE_ACCOUNTS", SCHEDULER_SA)
    monkeypatch.setattr(settings, "SCRAPE_OIDC_AUDIENCE", "")


# --- Unconfigured: both gates inert ---

class TestGatesInertWhenUnconfigured:
    """Deploying this code must change nothing until the settings are populated.

    That is what lets it ship ahead of the Cloudflare and Cloud Scheduler configuration
    without breaking local dev, CI, or the running site.
    """

    # What matters is that the *middleware* passed the request through; what the handler
    # then decides is not this test's business. That distinction has now bitten twice:
    #
    #   - pinning an exact status meant pinning 404, which broke when #36 added the admin
    #     subsite and /admin began answering 303
    #   - relaxing it to "not 403" broke when /admin/login gained its own, legitimate 403
    #     for "no password configured and no Access identity"
    #
    # So assert on the gate's own rejection body instead, which is unambiguous.

    # Distinguished by shape, not by wording: the middleware rejects with JSON, every
    # /admin handler responds with HTML or a redirect. Matching on the phrase "Cloudflare
    # Access" was the first attempt and it false-positived, because the handler's own
    # "login is disabled" page mentions Cloudflare Access too.
    def _passed_the_gate(self, response):
        if response.status_code != 403:
            return True
        return "application/json" not in response.headers.get("content-type", "")

    def test_admin_is_not_gated_when_unconfigured(self, client, gates_off):
        assert self._passed_the_gate(client.get("/admin", follow_redirects=False))

    def test_admin_login_is_not_gated_when_unconfigured(self, client, gates_off):
        assert self._passed_the_gate(client.get("/admin/login", follow_redirects=False))


# --- Configured: /admin gate ---

class TestAdminGate:
    def test_no_token_is_rejected(self, client, gates_on):
        assert client.get("/admin").status_code == 403

    def test_the_login_page_is_gated_too(self, client, gates_on):
        """Reaching a password prompt on the origin is the exposure being closed, so the
        gate covers /admin/login rather than only the API beneath it."""
        assert client.get("/admin/login").status_code == 403

    def test_a_nested_admin_path_is_rejected(self, client, gates_on):
        assert client.get("/admin/api/events").status_code == 403

    def test_a_garbage_token_is_rejected(self, client, gates_on):
        response = client.get("/admin", headers={"Cf-Access-Jwt-Assertion": "not-a-token"})
        assert response.status_code == 403

    def test_the_rejection_does_not_explain_itself(self, client, gates_on):
        """Why a token failed is reconnaissance; the response says only that the surface
        is Cloudflare-only."""
        body = client.get("/admin", headers={"Cf-Access-Jwt-Assertion": "x.y.z"}).json()
        assert "Cloudflare Access" in body["detail"]
        for leak in ("signature", "expired", "audience", "issuer", "algorithm"):
            assert leak not in body["detail"].lower()


class TestPublicRoutesAreUnaffected:
    def test_a_normal_route_is_not_gated(self, client, gates_on):
        """The /admin gate must key on the path prefix only, and let everything else by.

        /openapi.json rather than /api/health: health queries the database for its counts,
        so it cannot answer in a test that deliberately skips lifespan and runs without
        Postgres. This route proves the same thing about the middleware.
        """
        assert client.get("/openapi.json").status_code == 200

    def test_a_path_merely_containing_admin_is_not_gated(self, client, gates_on):
        """Guarding on startswith('/admin') and not on a substring match."""
        assert client.get("/api/venues/not-admin-really").status_code != 403


# --- Configured: scrape gate ---

class TestScrapeGate:
    def test_no_token_is_rejected(self, client, gates_on):
        assert client.post("/api/scrape").status_code == 403

    def test_a_bearer_token_that_is_not_a_token_is_rejected(self, client, gates_on):
        response = client.post("/api/scrape", headers={"Authorization": "Bearer garbage"})
        assert response.status_code == 403

    def test_a_non_bearer_scheme_is_rejected(self, client, gates_on):
        response = client.post("/api/scrape", headers={"Authorization": "Basic dXNlcjpwdw=="})
        assert response.status_code == 403

    def test_the_rejection_does_not_explain_itself(self, client, gates_on):
        body = client.post("/api/scrape").json()
        assert body["detail"] == "Not authorized to trigger a scrape."
