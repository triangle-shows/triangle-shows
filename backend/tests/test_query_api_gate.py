"""The queryable read endpoints are closed while a registration process is designed (#62).

`GET /api/events` and `GET /api/events/{id}` let a caller interrogate the dataset — search
it, filter by genre or status, page through it, address a row by id. `GET
/api/events/fullcalendar` draws the calendar the site displays. Only the third is used by
`frontend/`, so closing the first two costs the site nothing.

**What these tests are not claiming.** Closing them does not stop bulk collection.
/api/events/fullcalendar is unauthenticated, accepts no date bound, and returns every event
with every field — because that is what the site itself fetches. The gate removes a
convenient documented interface; it does not close the door, and the class below that pins
the open endpoints exists partly to keep that fact visible.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


# raise_server_exceptions=False so an endpoint that reaches the database in this
# no-database harness surfaces as a 500 response rather than an exception — that
# distinction is what several assertions below rest on.
@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


class TestTheQueryableEndpointsAreClosed:
    @pytest.mark.parametrize("path", ["/api/events", "/api/events/1"])
    def test_refused(self, client, path):
        assert client.get(path).status_code == 403

    @pytest.mark.parametrize("path", ["/api/events", "/api/events/1"])
    def test_refused_before_touching_the_database(self, client, path):
        """403 rather than 500 in a harness with no database is the evidence: the gate
        short-circuits, so a closed endpoint costs no connection. If the dependency ordering
        ever changed so the session resolved first, this would turn into a 500 and a closed
        endpoint would still be doing work for every caller."""
        assert client.get(path).status_code != 500

    @pytest.mark.parametrize("path", ["/api/events", "/api/events/1"])
    def test_the_refusal_says_where_to_ask(self, client, path):
        """403 with an address, not 404. The data is public and the site renders it, so this
        is a policy boundary rather than a security one — pretending the endpoint does not
        exist would mislead an integrator acting in good faith while doing nothing to a
        scraper, who does not need it."""
        detail = client.get(path).json()["detail"]
        assert "registration" in detail
        assert "github.com/triangle-shows" in detail

    def test_the_refusal_names_what_is_still_open(self, client):
        """Someone who wanted the calendar should not conclude the whole API is gone."""
        detail = client.get("/api/events").json()["detail"]
        assert "/api/events/fullcalendar" in detail
        assert "/feeds/events.ics" in detail


class TestTheSitesOwnEndpointsAreUntouched:
    """The regression that would take the site down. These three are what `frontend/`
    actually calls — verified by grep over frontend/js and index.html — and none may be
    gated by anything added for #62."""

    @pytest.mark.parametrize("path", [
        "/api/events/fullcalendar",
        "/api/venues",
        "/feeds/events.ics",
    ])
    def test_not_gated(self, client, path):
        # 500 here is the no-database harness, not a refusal; what matters is that the
        # request reached the handler rather than being turned away at the gate.
        assert client.get(path).status_code != 403


class TestItIsReversible:
    """The point of a flag rather than deleted routes. #62 reopens these behind a key, and
    that should be a configuration change plus an auth check — not rebuilding handlers,
    schemas and tests that were removed."""

    def test_the_flag_reopens_them(self, client, monkeypatch):
        monkeypatch.setattr(settings, "PUBLIC_QUERY_API_ENABLED", True)
        for path in ("/api/events", "/api/events/1"):
            assert client.get(path).status_code != 403, f"{path} stayed closed"

    def test_closed_by_default(self):
        """The setting ships closed, so a deployment that forgets to set it is paused rather
        than open."""
        from app.config import Settings

        assert Settings().PUBLIC_QUERY_API_ENABLED is False

    def test_the_routes_still_exist(self):
        """Gated, not deleted — so the OpenAPI schema still documents them and the handlers,
        response models and their tests survive intact for #62 to reopen."""
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/api/events" in paths
        assert "/api/events/{event_id}" in paths
