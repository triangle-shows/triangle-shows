"""
Tests for treating a Cloudflare Access identity as admin authentication.

Why this exists. A single shared ADMIN_PASSWORD has no per-person identity: you cannot
tell who approved an event, and removing one collaborator's access means rotating the
password for everyone holding it. Cloudflare Access already identifies people
individually; this wires that to the app so the shared secret can stop existing.

Safe only because the origin gate rejects tokenless requests — verified in production,
where `GET https://<origin>/admin` returns 403. If /admin were reachable without a token,
the Cf-Access-Jwt-Assertion header could be set by any client and trusting it would be an
open door. The middleware in app.main never sets request.state.access_email from an
unverified header, and _access_identity() additionally refuses to read it unless the gate
is configured.

The most important class here is TestEmptyPasswordIsNeverAccepted. Widening
_admin_enabled() to include "Access is configured" would, if the password check were
gated on it, let compare_digest("", "") succeed and hand a session to anyone posting an
empty password. _password_login_enabled() exists separately for exactly that reason.

Handlers are called directly rather than over HTTP: pytest-asyncio is deliberately absent
from requirements-dev.txt, and with the gate configured the middleware answers /admin/*
with 403 before any handler runs, so an HTTP request cannot reach the code under test.
"""

import asyncio

from fastapi import HTTPException
from fastapi.testclient import TestClient

import pytest

from app.api import admin
from app.config import settings
from app.main import app


TEAM = "ty-fi.cloudflareaccess.com"
AUD = "2672446ca36104364662d122c152746305f880c1d9fb818c0b1ec6cb55c8f0c4"
PERSON = "collaborator@example.org"


def run(coro):
    """Drive one coroutine to completion."""
    return asyncio.run(coro)


class _FakeState:
    def __init__(self, email=None):
        if email is not None:
            self.access_email = email


class _FakeRequest:
    """Stands in for a Request the middleware has already annotated."""

    def __init__(self, email=None, cookies=None):
        self.state = _FakeState(email)
        self.cookies = cookies or {}


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def access_on(monkeypatch):
    monkeypatch.setattr(settings, "CF_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.setattr(settings, "CF_ACCESS_AUD", AUD)


@pytest.fixture
def access_off(monkeypatch):
    monkeypatch.setattr(settings, "CF_ACCESS_TEAM_DOMAIN", "")
    monkeypatch.setattr(settings, "CF_ACCESS_AUD", "")


@pytest.fixture
def no_password(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "")


@pytest.fixture
def with_password(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "a-strong-password")
    monkeypatch.setattr(settings, "SESSION_SECRET", "test-signing-key")


# --- the security property that must not regress ---

class TestEmptyPasswordIsNeverAccepted:
    def test_admin_is_enabled_by_access_alone(self, access_on, no_password):
        """The widening this change introduces: no password, but admin still works."""
        assert admin._admin_enabled() is True

    def test_password_login_stays_disabled_without_a_password(self, access_on, no_password):
        """_password_login_enabled() must not follow _admin_enabled() upward."""
        assert admin._password_login_enabled() is False

    def test_an_empty_password_is_rejected(self, access_on, no_password):
        """compare_digest("", "") is True, so gating the password check on
        _admin_enabled() would hand out a session for an empty post."""
        assert run(admin.login_submit(admin.LoginBody(password=""))).status_code == 401

    def test_any_password_is_rejected_when_none_is_configured(self, access_on, no_password):
        assert run(admin.login_submit(admin.LoginBody(password="guess"))).status_code == 401

    def test_no_cookie_is_issued_on_a_rejected_login(self, access_on, no_password):
        """Belt and braces: even were the status wrong, no session may be handed out."""
        response = run(admin.login_submit(admin.LoginBody(password="")))
        assert admin.COOKIE_NAME not in response.headers.get("set-cookie", "")

    def test_the_correct_password_still_works_when_one_is_set(self, access_off, with_password):
        response = run(admin.login_submit(admin.LoginBody(password="a-strong-password")))
        assert response.status_code == 200


# --- identity resolution ---

class TestAccessIdentity:
    def test_the_verified_email_is_used(self, access_on):
        assert admin._access_identity(_FakeRequest(PERSON)) == PERSON

    def test_the_header_is_ignored_when_the_gate_is_not_configured(self, access_off):
        """Without the gate there is no verified identity, so a value on request.state must
        not be honored — the fail-safe direction if middleware order ever changes."""
        assert admin._access_identity(_FakeRequest(PERSON)) is None

    def test_a_missing_identity_is_none(self, access_on):
        assert admin._access_identity(_FakeRequest()) is None


class TestRequireAdmin:
    def test_an_access_identity_authenticates_without_a_cookie(self, access_on, no_password):
        assert run(admin.require_admin(_FakeRequest(PERSON))) == PERSON

    def test_no_identity_and_no_cookie_is_a_401(self, access_on, no_password):
        with pytest.raises(HTTPException) as exc:
            run(admin.require_admin(_FakeRequest()))
        assert exc.value.status_code == 401

    def test_a_password_session_still_works(self, access_off, with_password):
        """The cookie path is unchanged — local dev and docker-compose rely on it."""
        token = admin._serializer.dumps("admin")
        result = run(admin.require_admin(_FakeRequest(cookies={admin.COOKIE_NAME: token})))
        assert result == "admin"

    def test_a_password_session_reports_no_person(self, access_off, with_password):
        """A shared password identifies nobody, and the return value should say so rather
        than inventing an email."""
        token = admin._serializer.dumps("admin")
        result = run(admin.require_admin(_FakeRequest(cookies={admin.COOKIE_NAME: token})))
        assert "@" not in result

    def test_a_forged_cookie_is_rejected(self, access_off, with_password):
        with pytest.raises(HTTPException):
            run(admin.require_admin(_FakeRequest(cookies={admin.COOKIE_NAME: "nonsense"})))


# --- page behavior ---

class TestPages:
    def test_an_identified_visitor_is_sent_from_login_to_the_dashboard(self, access_on, no_password):
        """Showing a password box to someone Access already identified would only reject
        them."""
        response = run(admin.login_page(_FakeRequest(PERSON)))
        assert response.status_code == 303
        assert response.headers["location"] == "/admin"

    def test_the_dashboard_renders_for_an_identified_visitor(self, access_on, no_password):
        assert run(admin.admin_page(_FakeRequest(PERSON))).status_code == 200

    def test_the_dashboard_redirects_an_unidentified_visitor(self, access_off, with_password):
        response = run(admin.admin_page(_FakeRequest()))
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/login"

    def test_login_says_so_when_nothing_can_authenticate(self, client, access_off, no_password):
        """No password and no Access: a form that cannot succeed is worse than a message."""
        response = client.get("/admin/login", follow_redirects=False)
        assert response.status_code == 403
        assert "disabled" in response.text.lower()

    def test_the_form_still_renders_when_a_password_is_configured(
        self, client, access_off, with_password
    ):
        response = client.get("/admin/login", follow_redirects=False)
        assert response.status_code == 200
        assert "password" in response.text.lower()


class TestLogout:
    def test_it_ends_the_access_session_when_the_gate_is_on(self, access_on, no_password):
        """Deleting the app cookie does nothing to an Access session — the old target
        bounced straight back to the dashboard, so the button appeared to do nothing."""
        response = run(admin.logout())
        assert response.status_code == 303
        assert response.headers["location"] == f"https://{TEAM}/cdn-cgi/access/logout"

    def test_it_returns_to_the_login_page_when_the_gate_is_off(self, access_off, with_password):
        assert run(admin.logout()).headers["location"] == "/admin/login"

    def test_a_team_domain_with_a_scheme_is_tolerated(self, monkeypatch, no_password):
        monkeypatch.setattr(settings, "CF_ACCESS_TEAM_DOMAIN", f"https://{TEAM}/")
        monkeypatch.setattr(settings, "CF_ACCESS_AUD", AUD)
        response = run(admin.logout())
        assert response.headers["location"] == f"https://{TEAM}/cdn-cgi/access/logout"
