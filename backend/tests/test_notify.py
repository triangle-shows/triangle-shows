"""Tests for ntfy notifications on new show submissions (app.notify).

What must hold, in order of how much it would hurt to lose:

  * A notification failure never fails a submission. The row is committed first and
    send() swallows every error, so ntfy being down costs a ping, not a show.
  * The topic URL never reaches the logs. On a public ntfy server the topic name is the
    whole of the access control, and httpx puts the request URL in its exception text.
  * Nothing is sent before the commit, so a ping never announces a rolled-back row.

Requests go to an httpx.MockTransport, so nothing touches the network.
"""

import asyncio
import json
import logging
from datetime import date, timedelta

import httpx
import pytest

from app import notify
from app.api import submissions
from app.config import settings
from tests.test_show_submissions import _Session, _body, _venue

TOPIC_URL = "https://ntfy.example/ts-submissions-s3cretTopicName"
SOON = date.today() + timedelta(days=14)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def ntfy_on(monkeypatch):
    monkeypatch.setattr(settings, "NTFY_URL", TOPIC_URL)
    monkeypatch.setattr(settings, "NTFY_TOKEN", "")
    monkeypatch.setattr(settings, "PUBLIC_SITE_URL", "https://triangle-shows.net")


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _message(**kw):
    base = dict(name="Sub Rosa", on=date(2026, 11, 14), venue="The Fruit",
                new_venue=True, city="Durham", who="fan@example.org")
    base.update(kw)
    return notify.submission_message(**base)


# --- The message ------------------------------------------------------------------


class TestMessage:
    def test_it_names_the_show_the_venue_and_the_submitter(self, ntfy_on):
        m = _message()
        assert m["title"] == "New show proposed: Sub Rosa"
        assert "Sat Nov 14" in m["message"]
        assert "The Fruit (new venue, Durham)" in m["message"]
        assert "from fan@example.org" in m["message"]

    def test_a_listed_venue_is_not_called_new(self, ntfy_on):
        m = _message(venue="Motorco Music Hall", new_venue=False, city=None)
        assert "Motorco Music Hall" in m["message"]
        assert "new venue" not in m["message"]

    def test_tapping_it_opens_the_admin_on_the_public_host(self, ntfy_on):
        """Not the request's host: the Worker rewrites that to the .run.app origin, which
        the Access gate refuses."""
        assert _message()["click"] == "https://triangle-shows.net/admin"


# --- Sending -----------------------------------------------------------------------


class TestSend:
    def test_unconfigured_sends_nothing(self, monkeypatch):
        monkeypatch.setattr(settings, "NTFY_URL", "")
        calls = []
        assert run(notify.send(_message(), client=_client(lambda r: calls.append(r)))) is False
        assert calls == []

    def test_publishes_json_to_the_server_root_with_the_topic(self, ntfy_on):
        """JSON, because a title travelling as an HTTP header cannot hold a curly quote."""
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"id": "x"})

        msg = _message(name="Sub Rosa’s “last” show 🎸")
        assert run(notify.send(msg, client=_client(handler))) is True
        assert seen["url"] == "https://ntfy.example"
        assert seen["body"]["topic"] == "ts-submissions-s3cretTopicName"
        assert seen["body"]["title"] == msg["title"]
        assert seen["auth"] is None, "no token configured, so no Authorization header"

    def test_a_token_is_sent_as_a_bearer_header(self, ntfy_on, monkeypatch):
        monkeypatch.setattr(settings, "NTFY_TOKEN", "tk_abc123")
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200)

        run(notify.send(_message(), client=_client(handler)))
        assert seen["auth"] == "Bearer tk_abc123"

    def test_a_self_hosted_server_under_a_path_prefix(self):
        assert notify._split_topic_url("https://example.org/ntfy/my-topic/") == (
            "https://example.org/ntfy", "my-topic",
        )

    def test_a_refusal_returns_false_without_raising(self, ntfy_on, caplog):
        with caplog.at_level(logging.WARNING, logger="app.notify"):
            ok = run(notify.send(_message(), client=_client(lambda r: httpx.Response(403))))
        assert ok is False
        assert "HTTP 403" in caplog.text

    def test_a_network_failure_returns_false_and_keeps_the_topic_out_of_the_log(
        self, ntfy_on, caplog
    ):
        # The topic travels in the JSON body, not the URL, so httpx's own message would not
        # contain it here. The rule being held is the stronger one — exception text is
        # never logged — so the error is made to carry the topic to prove that.
        def handler(request):
            raise httpx.ConnectError(f"cannot reach {TOPIC_URL}", request=request)

        with caplog.at_level(logging.DEBUG):
            ok = run(notify.send(_message(), client=_client(handler)))
        assert ok is False
        assert "ConnectError" in caplog.text
        assert "s3cretTopicName" not in caplog.text, "the topic name is the secret"


# --- Wired into the form --------------------------------------------------------------


class TestSubmissionNotifies:
    def test_a_saved_submission_sends_one_notification_after_the_commit(self, monkeypatch):
        session = _Session({("Venue", 1): _venue()}, results=[None, 0, None, []])
        sent = []

        async def fake_send(payload, **kw):
            sent.append((payload, session.committed))
            return True

        monkeypatch.setattr(notify, "send", fake_send)
        run(submissions.create_submission(_body(), who="fan@example.org", session=session))

        assert len(sent) == 1
        payload, committed_first = sent[0]
        assert committed_first, "a notification must never announce an uncommitted row"
        assert "from fan@example.org" in payload["message"]
        assert "Cat's Cradle" in payload["message"]

    def test_a_refused_submission_sends_nothing(self, monkeypatch):
        sent = []

        async def fake_send(payload, **kw):
            sent.append(payload)

        monkeypatch.setattr(notify, "send", fake_send)
        session = _Session({("Venue", 1): _venue()}, results=[None, 99])  # over the cap
        with pytest.raises(Exception):
            run(submissions.create_submission(_body(), who="fan@example.org", session=session))
        assert sent == []

    def test_ntfy_being_down_does_not_fail_the_submission(self, ntfy_on, monkeypatch):
        """The real send(), against a server that refuses to connect."""
        def handler(request):
            raise httpx.ConnectError("down", request=request)

        real_send = notify.send
        monkeypatch.setattr(notify, "send",
                            lambda payload, **kw: real_send(payload, client=_client(handler)))
        session = _Session({("Venue", 1): _venue()}, results=[None, 0, None, []])

        out = run(submissions.create_submission(_body(), who="fan@example.org", session=session))
        assert out["ok"]
        assert session.committed
