import asyncio
from datetime import date, time

import pytest
from bs4 import BeautifulSoup

import app.scrapers.arkon_bar_manager as arkon_module
from app.scrapers.arkon_bar_manager import ArkonBarManagerScraper


HTML = """
<div class="abm-calendar">
  <article class="abm-event" data-date="2026-09-10">
    <a class="abm-event-flyer" href="/music-and-events/hopscotch/">
      <img src="/uploads/hopscotch.jpg" alt="Hopscotch">
    </a>
    <div class="abm-event-title">
      <a href="/music-and-events/hopscotch/">Bands &amp;amp; Friends</a>
    </div>
    <p class="abm-event-blurb">Doors at six. Free show.</p>
    <span class="abm-meta-row abm-meta-time"><svg></svg><span>6:30 PM - 11:00 PM</span></span>
  </article>
  <article class="abm-event" data-date="not-a-date">
    <div class="abm-event-title"><a href="/bad/">Bad date</a></div>
  </article>
</div>
"""


def test_parses_arkon_calendar_event_fields():
    scraper = ArkonBarManagerScraper("slims", {"url": "https://slims.example/events/"})

    events = scraper._parse_events(BeautifulSoup(HTML, "lxml"), "https://slims.example/events/")

    assert len(events) == 1
    event = events[0]
    assert event.name == "Bands & Friends"
    assert event.date == date(2026, 9, 10)
    assert event.show_time == time(18, 30)
    assert event.ticket_url == "https://slims.example/music-and-events/hopscotch/"
    assert event.image_url == "https://slims.example/uploads/hopscotch.jpg"
    assert event.description == "Doors at six. Free show."
    assert event.source == "arkon_bar_manager"


def test_requires_a_configured_url():
    scraper = ArkonBarManagerScraper("slims", {})

    with pytest.raises(ValueError, match="No URL configured"):
        asyncio.run(scraper.scrape())


class FakeResponse:
    def __init__(self, *, text="", payload=None):
        self.text = text
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeAsyncClient:
    instance = None

    def __init__(self, **kwargs):
        self.post_calls = []
        FakeAsyncClient.instance = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, url):
        return FakeResponse(
            text=HTML.replace(
                '<div class="abm-calendar">',
                '<div class="abm-calendar" data-ajax="/ajax" '
                'data-cursor="first-cursor" data-more="10" '
                'data-last-month="2026-09" data-category="music">',
            )
        )

    async def post(self, url, data):
        self.post_calls.append((url, data))
        return FakeResponse(
            payload={
                "success": True,
                "data": {
                    "html": HTML.replace("2026-09-10", "2026-09-11"),
                    "cursor": "second-cursor",
                    "last_month": "2026-09",
                    "has_more": False,
                },
            }
        )


def test_follows_arkon_cursor_pagination(monkeypatch):
    monkeypatch.setattr(arkon_module.httpx, "AsyncClient", FakeAsyncClient)
    scraper = ArkonBarManagerScraper("slims", {"url": "https://slims.example/events/"})

    events = asyncio.run(scraper.scrape())

    assert [event.date for event in events] == [date(2026, 9, 10), date(2026, 9, 11)]
    assert FakeAsyncClient.instance.post_calls == [
        (
            "https://slims.example/ajax",
            {
                "action": "abm_load_events",
                "cursor": "first-cursor",
                "count": "10",
                "last_month": "2026-09",
                "category": "music",
            },
        )
    ]


def test_missing_calendar_is_a_failed_scrape(monkeypatch):
    async def get_without_calendar(self, url):
        return FakeResponse(text="<html><body>No calendar</body></html>")

    monkeypatch.setattr(FakeAsyncClient, "get", get_without_calendar)
    monkeypatch.setattr(arkon_module.httpx, "AsyncClient", FakeAsyncClient)
    scraper = ArkonBarManagerScraper("slims", {"url": "https://slims.example/events/"})

    with pytest.raises(RuntimeError, match="ABM calendar not found"):
        asyncio.run(scraper.scrape())
