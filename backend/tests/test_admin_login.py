"""
Tests for the admin login check — review item #12.

secrets.compare_digest raises TypeError when handed a str containing any non-ASCII
character, rather than returning False. So a password with an accented character turned a
wrong-password attempt into a 500. Two problems with that: the endpoint stops failing
cleanly, and because the response differs from the normal 401 it tells an unauthenticated
caller something about the configured password.

No database is touched — the login route only compares a string and sets a cookie.
"""

from fastapi.testclient import TestClient

import pytest

from app.config import settings
from app.main import app


ASCII_PASSWORD = "s3cret-ascii-only"
ACCENTED_PASSWORD = "contraseña-café"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def gates_off(monkeypatch):
    """The origin gate would 403 /admin before the login handler ever ran."""
    monkeypatch.setattr(settings, "CF_ACCESS_TEAM_DOMAIN", "")
    monkeypatch.setattr(settings, "CF_ACCESS_AUD", "")


def _configure(monkeypatch, password):
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", password)
    monkeypatch.setattr(settings, "SESSION_SECRET", "test-signing-key")


class TestNonAsciiPasswords:
    def test_a_wrong_guess_against_an_accented_password_is_a_clean_401(
        self, client, monkeypatch
    ):
        """The regression: this raised TypeError inside compare_digest and returned 500."""
        _configure(monkeypatch, ACCENTED_PASSWORD)

        response = client.post("/admin/login", json={"password": "wrong"})

        assert response.status_code == 401
        assert response.json()["ok"] is False

    def test_an_accented_guess_against_an_ascii_password_is_a_clean_401(
        self, client, monkeypatch
    ):
        """The other direction — the non-ASCII operand can arrive from the client."""
        _configure(monkeypatch, ASCII_PASSWORD)

        response = client.post("/admin/login", json={"password": ACCENTED_PASSWORD})

        assert response.status_code == 401

    def test_the_correct_accented_password_still_authenticates(self, client, monkeypatch):
        """Encoding both sides must not break the passwords it was meant to support."""
        _configure(monkeypatch, ACCENTED_PASSWORD)

        response = client.post("/admin/login", json={"password": ACCENTED_PASSWORD})

        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_wrong_and_right_are_indistinguishable_apart_from_the_verdict(
        self, client, monkeypatch
    ):
        """The reason a 500 mattered: a differing failure mode is an oracle. Both a wrong
        ASCII guess and a wrong non-ASCII guess must produce the same 401 shape."""
        _configure(monkeypatch, ACCENTED_PASSWORD)

        ascii_guess = client.post("/admin/login", json={"password": "wrong"})
        unicode_guess = client.post("/admin/login", json={"password": "wröng"})

        assert ascii_guess.status_code == unicode_guess.status_code == 401
        assert ascii_guess.json() == unicode_guess.json()


class TestOrdinaryCases:
    def test_the_correct_ascii_password_authenticates(self, client, monkeypatch):
        _configure(monkeypatch, ASCII_PASSWORD)

        response = client.post("/admin/login", json={"password": ASCII_PASSWORD})

        assert response.status_code == 200

    def test_an_empty_guess_is_rejected(self, client, monkeypatch):
        _configure(monkeypatch, ASCII_PASSWORD)

        assert client.post("/admin/login", json={"password": ""}).status_code == 401

    def test_login_is_disabled_when_no_password_is_configured(self, client, monkeypatch):
        """Fails closed. Notably an empty guess must not match an empty configured
        password — _admin_enabled() is what prevents compare_digest("", "") succeeding."""
        _configure(monkeypatch, "")

        assert client.post("/admin/login", json={"password": ""}).status_code == 401
        assert client.post("/admin/login", json={"password": "anything"}).status_code == 401
