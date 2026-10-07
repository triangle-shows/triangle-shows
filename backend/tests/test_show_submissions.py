"""Tests for proposed shows: the /new-shows-form gate, the rules, and approval.

Three properties carry the feature, and each class below holds one of them:

  TestTheTwoAccessApplicationsStaySeparate. The form is a separate Cloudflare Access
  application from /admin, with a broader policy. If a token minted for the form opened
  /admin, every submitter would be an admin. The gate is therefore asserted in both
  directions with real signed tokens, not by reading the source.

  TestTheFormFailsClosedInProduction. /admin has a password to fall back on when Access is
  not configured; the form has nothing. An unconfigured form in production must refuse
  everyone rather than accept anonymous submissions on the public origin.

  TestApproval. Approving is the only way a submission reaches the calendar, and it must
  produce exactly what an admin's own hand-add produces — both manual flags, a manual
  venue that scrape_all will skip — because it goes through the same builders.

No database. Handlers are called directly with a stand-in session, as in
test_admin_manual_edit.py, and gate tests go through TestClient without its context
manager, which skips lifespan, as in test_origin_gates.py.
"""

import asyncio
import importlib.util
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import jwt
import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import tokens
from app.api import admin, submissions
from app.api.admin import SubmissionBody, clean_submission, resolve_submission_venue
from app.config import settings
from app.main import app
from app.models import MANUAL_SCRAPER_TYPE, Event, ShowSubmission, SubmissionStatus, Venue


TEAM = "ty-fi.cloudflareaccess.com"
ADMIN_AUD = "admin-aud-tag"
SUBMIT_AUD = "submit-aud-tag"
SOON = date.today() + timedelta(days=14)


def run(coro):
    return asyncio.run(coro)


# --- Signed tokens, with the JWKS lookup pointed at a local key ---------------


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwks(monkeypatch, private_key):
    class _Key:
        key = private_key.public_key()

    class _Client:
        def get_signing_key_from_jwt(self, token):
            return _Key()

    monkeypatch.setattr(tokens, "_jwks_client", lambda url: _Client())


def token_for(private_key, audience: str, email: str = "someone@example.org") -> str:
    now = int(time.time())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return jwt.encode(
        {"iss": f"https://{TEAM}", "aud": audience, "email": email,
         "iat": now, "exp": now + 600},
        pem, algorithm="RS256",
    )


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def both_gates_on(monkeypatch, jwks):
    monkeypatch.setattr(settings, "CF_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.setattr(settings, "CF_ACCESS_AUD", ADMIN_AUD)
    monkeypatch.setattr(settings, "CF_ACCESS_SUBMIT_AUD", SUBMIT_AUD)


@pytest.fixture
def submit_gate_off(monkeypatch):
    monkeypatch.setattr(settings, "CF_ACCESS_SUBMIT_AUD", "")


# --- The gate ------------------------------------------------------------------


class TestTheTwoAccessApplicationsStaySeparate:
    def test_no_token_is_rejected_on_the_page(self, client, both_gates_on):
        assert client.get("/new-shows-form").status_code == 403

    def test_no_token_is_rejected_on_the_api(self, client, both_gates_on):
        assert client.get("/new-shows-form/api/venues").status_code == 403
        assert client.post("/new-shows-form/api/submissions", json={}).status_code == 403

    def test_a_form_token_opens_the_form(self, client, both_gates_on, private_key):
        r = client.get("/new-shows-form",
                       headers={"Cf-Access-Jwt-Assertion": token_for(private_key, SUBMIT_AUD)})
        assert r.status_code == 200
        assert "Propose a show" in r.text

    def test_a_form_token_does_not_open_the_admin(self, client, both_gates_on, private_key):
        """The direction that matters: submitters are a broader group than admins."""
        r = client.get("/admin",
                       headers={"Cf-Access-Jwt-Assertion": token_for(private_key, SUBMIT_AUD)})
        assert r.status_code == 403

    def test_an_admin_token_does_not_open_the_form(self, client, both_gates_on, private_key):
        """Each application gets its own token from Cloudflare; accepting the admin one
        here would mean the form's policy was not the thing deciding who can submit."""
        r = client.get("/new-shows-form",
                       headers={"Cf-Access-Jwt-Assertion": token_for(private_key, ADMIN_AUD)})
        assert r.status_code == 403

    def test_the_form_never_sets_the_admin_identity(self):
        """Admin handlers treat request.state.access_email as proof of an admin. The form's
        middleware must store its identity under another name."""
        import inspect
        from app import main

        source = inspect.getsource(main.enforce_submit_access)
        assert "submit_email" in source
        assert "access_email" not in source.split('"""')[-1], (
            "the form's gate must not set the attribute the admin trusts"
        )

    def test_the_prefix_match_is_not_a_bare_startswith(self):
        assert submissions.is_submit_path("/new-shows-form")
        assert submissions.is_submit_path("/new-shows-form/api/venues")
        assert not submissions.is_submit_path("/new-shows-formerly")
        assert not submissions.is_submit_path("/")


class TestTheFormFailsClosedInProduction:
    def test_unconfigured_in_production_refuses(self, client, submit_gate_off, monkeypatch):
        monkeypatch.setattr(settings, "APP_ENV", "production")
        assert client.get("/new-shows-form").status_code == 403

    def test_unconfigured_in_development_is_open(self, client, submit_gate_off, monkeypatch):
        monkeypatch.setattr(settings, "APP_ENV", "development")
        assert client.get("/new-shows-form").status_code == 200

    def test_a_configured_gate_without_an_identity_still_refuses(self, monkeypatch):
        """Belt and braces: require_submitter does not trust that the middleware ran."""
        monkeypatch.setattr(settings, "CF_ACCESS_TEAM_DOMAIN", TEAM)
        monkeypatch.setattr(settings, "CF_ACCESS_SUBMIT_AUD", SUBMIT_AUD)

        class _Req:
            class state:
                pass

        with pytest.raises(HTTPException) as exc:
            run(submissions.require_submitter(_Req()))
        assert exc.value.status_code == 403


# --- Stand-in session ------------------------------------------------------------


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        return self.value

    def scalars(self):
        return self

    def first(self):
        return self.value[0] if isinstance(self.value, list) and self.value else self.value

    def all(self):
        return self.value if isinstance(self.value, list) else []

    def unique(self):
        return self


class _Session:
    """get() from a dict, execute() from a queue, and ids assigned on flush."""

    def __init__(self, rows=None, results=None):
        self.rows = rows or {}
        self.results = list(results or [])
        self.added = []
        self.committed = False
        self._next_id = 900

    async def get(self, model, pk, **kw):
        return self.rows.get((model.__name__, pk))

    async def execute(self, statement, *a, **kw):
        return _Result(self.results.pop(0) if self.results else None)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1

    async def commit(self):
        self.committed = True

    async def refresh(self, obj, attrs=None):
        return None


def _venue(**kw):
    v = Venue()
    v.id = kw.get("id", 1)
    v.name = kw.get("name", "Cat's Cradle")
    v.slug = kw.get("slug", "cats-cradle")
    v.city = kw.get("city", "Carrboro")
    v.scraper_type = kw.get("scraper_type", "rhp_events")
    v.color = "#123456"
    return v


def _submission(**kw):
    s = ShowSubmission()
    s.id = kw.get("id", 5)
    s.status = kw.get("status", SubmissionStatus.pending.value)
    s.submitted_by = kw.get("submitted_by", "fan@example.org")
    s.venue_id = kw.get("venue_id")
    s.new_venue_name = kw.get("new_venue_name")
    s.new_venue_city = kw.get("new_venue_city")
    s.new_venue_website = kw.get("new_venue_website")
    s.name = kw.get("name", "Sub Rosa")
    s.date = kw.get("date", SOON)
    for attr in ("artist", "support_artists", "doors_time", "show_time", "ticket_url",
                 "price_min", "price_max", "description", "genre", "age_restriction", "note"):
        setattr(s, attr, kw.get(attr))
    s.is_live_music = kw.get("is_live_music", True)
    s.event_id = None
    return s


def _body(**kw):
    base = {"name": "Sub Rosa", "date": SOON.isoformat(), "venue_id": 1}
    base.update(kw)
    return SubmissionBody(**base)


# --- The rules -------------------------------------------------------------------


class TestCleanSubmission:
    def test_a_good_submission_becomes_columns(self):
        values = clean_submission(
            _body(show_time="20:00", ticket_url=" https://example.org/t ", artist="  "),
            earliest=date.today(),
        )
        assert values["date"] == SOON
        assert values["show_time"].hour == 20
        assert values["ticket_url"] == "https://example.org/t"
        assert values["artist"] is None, "whitespace-only is absent, not a blank string"
        assert set(values) <= set(ShowSubmission.__table__.columns.keys())

    def test_a_submitter_cannot_propose_a_past_show(self):
        with pytest.raises(HTTPException) as exc:
            clean_submission(_body(date=(date.today() - timedelta(days=1)).isoformat()),
                             earliest=date.today())
        assert exc.value.status_code == 400
        assert "passed" in exc.value.detail

    def test_an_admin_edit_may_keep_a_past_date(self):
        past = date.today() - timedelta(days=3)
        values = clean_submission(_body(date=past.isoformat()),
                                  earliest=admin.MANUAL_EVENT_MIN_DATE)
        assert values["date"] == past

    def test_a_mistyped_year_is_refused(self):
        with pytest.raises(HTTPException):
            clean_submission(_body(date=f"{date.today().year + 30}-01-01"), earliest=date.today())

    def test_a_link_without_a_scheme_is_refused_not_dropped(self):
        """ScrapedEvent would silently drop it at approval; a person should hear about it."""
        with pytest.raises(HTTPException) as exc:
            clean_submission(_body(ticket_url="www.example.org"), earliest=date.today())
        assert "https://" in exc.value.detail

    def test_a_javascript_link_is_refused(self):
        with pytest.raises(HTTPException):
            clean_submission(_body(new_venue_website="javascript:alert(1)", venue_id=None,
                                   new_venue_name="X", new_venue_city="Durham"),
                             earliest=date.today())

    def test_prices_are_checked(self):
        with pytest.raises(HTTPException):
            clean_submission(_body(price_min=20, price_max=10), earliest=date.today())
        with pytest.raises(HTTPException):
            clean_submission(_body(price_min=-1), earliest=date.today())

    def test_an_overlong_field_is_named(self):
        with pytest.raises(HTTPException) as exc:
            clean_submission(_body(genre="x" * 101), earliest=date.today())
        assert "genre" in exc.value.detail

    def test_a_name_is_required(self):
        with pytest.raises(HTTPException):
            clean_submission(_body(name="   "), earliest=date.today())


class TestResolveVenue:
    def test_an_existing_venue_clears_the_new_venue_fields(self):
        values = {"venue_id": 1, "new_venue_name": "Stray", "new_venue_city": "X",
                  "new_venue_website": None}
        venue = run(resolve_submission_venue(_Session({("Venue", 1): _venue()}), values))
        assert venue.id == 1
        assert values["new_venue_name"] is None

    def test_a_missing_venue_is_404(self):
        with pytest.raises(HTTPException) as exc:
            run(resolve_submission_venue(_Session(), {"venue_id": 99}))
        assert exc.value.status_code == 404

    def test_a_new_venue_needs_a_city(self):
        with pytest.raises(HTTPException) as exc:
            run(resolve_submission_venue(
                _Session(), {"venue_id": None, "new_venue_name": "The Fruit", "new_venue_city": None}))
        assert "city" in exc.value.detail

    def test_proposing_a_venue_that_is_already_listed_is_refused(self):
        """Approving it as typed would create a second copy of a real room."""
        session = _Session(results=[[_venue(name="The Fruit", slug="the-fruit")]])
        with pytest.raises(HTTPException) as exc:
            run(resolve_submission_venue(
                session, {"venue_id": None, "new_venue_name": "the fruit", "new_venue_city": "Durham"}))
        assert exc.value.status_code == 409
        assert "already listed" in exc.value.detail


# --- Submitting --------------------------------------------------------------------


class TestCreateSubmission:
    def test_a_submission_is_recorded_against_its_submitter(self):
        # pending count, hash probe, queued-duplicate probe
        session = _Session({("Venue", 1): _venue()}, results=[0, None, None])
        out = run(submissions.create_submission(_body(), who="fan@example.org", session=session))
        assert out["ok"]
        (row,) = session.added
        assert isinstance(row, ShowSubmission)
        assert row.submitted_by == "fan@example.org"
        assert row.venue_id == 1
        assert session.committed

    def test_nothing_touches_the_events_table(self):
        session = _Session({("Venue", 1): _venue()}, results=[0, None, None])
        run(submissions.create_submission(_body(), who="fan@example.org", session=session))
        assert not any(isinstance(o, (Event, Venue)) for o in session.added)

    def test_a_show_already_on_the_calendar_is_refused(self):
        existing = Event()
        existing.name, existing.date = "Sub Rosa", SOON
        session = _Session({("Venue", 1): _venue()}, results=[0, existing])
        with pytest.raises(HTTPException) as exc:
            run(submissions.create_submission(_body(), who="fan@example.org", session=session))
        assert exc.value.status_code == 409
        assert "already on the calendar" in exc.value.detail
        assert not session.committed

    def test_a_show_already_waiting_is_refused(self):
        session = _Session({("Venue", 1): _venue()}, results=[0, None, 42])
        with pytest.raises(HTTPException) as exc:
            run(submissions.create_submission(_body(), who="fan@example.org", session=session))
        assert exc.value.status_code == 409
        assert "waiting for review" in exc.value.detail

    def test_one_persons_queue_is_capped(self):
        session = _Session({("Venue", 1): _venue()},
                           results=[admin.MAX_PENDING_PER_SUBMITTER])
        with pytest.raises(HTTPException) as exc:
            run(submissions.create_submission(_body(), who="fan@example.org", session=session))
        assert exc.value.status_code == 429
        assert not session.added


# --- Reviewing ---------------------------------------------------------------------


class TestApproval:
    def test_approving_a_new_venue_creates_both_like_a_hand_add(self):
        sub = _submission(new_venue_name="The Fruit", new_venue_city="Durham",
                          is_live_music=False)
        # venue slug clash, existing colours, event hash clash
        session = _Session({("ShowSubmission", 5): sub}, results=[None, [], None])

        out = run(admin.approve_submission(5, who="admin@example.org", session=session))

        venue = next(o for o in session.added if isinstance(o, Venue))
        event = next(o for o in session.added if isinstance(o, Event))
        assert venue.scraper_type == MANUAL_SCRAPER_TYPE, "scrape_all must skip it"
        assert venue.slug == "the-fruit"
        assert event.venue_id == venue.id
        assert event.is_manually_created is True, "reconcile must not delete it"
        assert event.is_manual_override is True, "reclassify must not overwrite the verdict"
        assert event.is_live_music is False, "the submitted verdict is kept"
        assert event.approved_at is not None

        assert sub.status == SubmissionStatus.approved.value
        assert sub.reviewed_by == "admin@example.org"
        assert sub.event_id == event.id
        assert sub.venue_id == venue.id, "the record points at the venue it became"
        assert sub.new_venue_name == "The Fruit", "and keeps what was proposed"
        assert session.committed
        assert out["event_id"] == event.id

    def test_approving_at_an_existing_venue_creates_no_venue(self):
        sub = _submission(venue_id=1)
        session = _Session({("ShowSubmission", 5): sub, ("Venue", 1): _venue()},
                           results=[None])
        run(admin.approve_submission(5, who="admin@example.org", session=session))
        assert not any(isinstance(o, Venue) for o in session.added)
        assert any(isinstance(o, Event) for o in session.added)

    def test_a_refused_event_commits_nothing(self):
        """The venue was flushed, not committed, so the clash rolls it back with the event."""
        clash = Event()
        clash.id, clash.name, clash.date = 7, "Sub Rosa", SOON
        sub = _submission(new_venue_name="The Fruit", new_venue_city="Durham")
        session = _Session({("ShowSubmission", 5): sub}, results=[None, [], clash])
        with pytest.raises(HTTPException) as exc:
            run(admin.approve_submission(5, who="admin@example.org", session=session))
        assert exc.value.status_code == 409
        assert not session.committed
        assert sub.status == SubmissionStatus.pending.value

    def test_a_reviewed_submission_cannot_be_approved_again(self):
        sub = _submission(status=SubmissionStatus.approved.value, venue_id=1)
        session = _Session({("ShowSubmission", 5): sub})
        with pytest.raises(HTTPException) as exc:
            run(admin.approve_submission(5, who="admin@example.org", session=session))
        assert exc.value.status_code == 409

    def test_a_vanished_venue_asks_for_another(self):
        sub = _submission(venue_id=3)
        session = _Session({("ShowSubmission", 5): sub})
        with pytest.raises(HTTPException) as exc:
            run(admin.approve_submission(5, who="admin@example.org", session=session))
        assert exc.value.status_code == 400
        assert not session.committed

    def test_rejecting_keeps_the_row(self):
        sub = _submission(venue_id=1)
        session = _Session({("ShowSubmission", 5): sub})
        run(admin.reject_submission(5, who="admin@example.org", session=session))
        assert sub.status == SubmissionStatus.rejected.value
        assert sub.reviewed_by == "admin@example.org"
        assert session.committed


class TestEditingBeforeApproval:
    def test_remapping_to_an_existing_venue_drops_the_proposal(self):
        """The usual fix: someone proposed a venue that is already listed."""
        sub = _submission(new_venue_name="Cats Cradle", new_venue_city="Carrboro")
        session = _Session({("ShowSubmission", 5): sub, ("Venue", 1): _venue()})
        run(admin.edit_submission(5, _body(venue_id=1, new_venue_name="Cats Cradle"),
                                  session=session))
        assert sub.venue_id == 1
        assert sub.new_venue_name is None
        assert session.committed

    def test_an_approved_submission_cannot_be_edited(self):
        sub = _submission(status=SubmissionStatus.approved.value)
        with pytest.raises(HTTPException) as exc:
            run(admin.edit_submission(5, _body(), session=_Session({("ShowSubmission", 5): sub})))
        assert exc.value.status_code == 409


# --- Schema ---------------------------------------------------------------------------


def test_the_migration_creates_every_model_column():
    """Migration 0007 and the model must agree, or the first insert after deploy fails."""
    path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "0007_add_show_submissions.py"
    spec = importlib.util.spec_from_file_location("m0007", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    created = {}

    class _Op:
        def create_table(self, name, *cols, **kw):
            created[name] = {c.name for c in cols if isinstance(c, sa.Column)}

        def create_index(self, *a, **kw):
            pass

    module.op = _Op()
    module.upgrade()
    assert created["show_submissions"] == set(ShowSubmission.__table__.columns.keys())
