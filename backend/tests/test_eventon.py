import asyncio
from datetime import date, time

import pytest
from bs4 import BeautifulSoup

import app.scrapers.eventon as eventon_module
from app.scrapers.eventon import EventONScraper


EVENT = {
    "@type": "Event",
    "@id": "event_2251_0",
    "name": "JET - Down the Moonlit Mile Revue",
    "startDate": "2026-9-17T19:00+0:00",
    "url": "https://bowstring.example/events/jet/",
    "image": "https://bowstring.example/uploads/jet.png",
    "description": (
        '<a href="https://tickets.example/jet">Get your tickets here!</a>'
        " Doors at 6pm."
    ),
    "eventStatus": "https://schema.org/EventScheduled",
}


def test_parses_eventon_datetime_and_urls():
    scraper = EventONScraper("bowstring-brewyard-raleigh", {})

    event = scraper._parse_jsonld_event(EVENT)

    assert event is not None
    assert event.date == date(2026, 9, 17)
    assert event.show_time == time(19, 0)
    assert event.ticket_url == "https://tickets.example/jet"
    assert event.source_url == "https://bowstring.example/events/jet/"
    assert event.image_url == "https://bowstring.example/uploads/jet.png"
    assert event.external_id == "event_2251_0"
    assert event.source == "eventon"


def test_missing_url_fails_before_request():
    scraper = EventONScraper("bowstring-brewyard-raleigh", {})

    with pytest.raises(ValueError, match="No URL configured"):
        asyncio.run(scraper.scrape())


def test_parses_non_padded_date_without_time():
    scraper = EventONScraper("bowstring-brewyard-raleigh", {})
    data = {**EVENT, "startDate": "2026-9-5"}

    event = scraper._parse_jsonld_event(data)

    assert event is not None
    assert event.date == date(2026, 9, 5)
    assert event.show_time is None


def test_adds_ticket_url_to_offer_metadata_without_losing_price():
    scraper = EventONScraper("bowstring-brewyard-raleigh", {})
    data = {**EVENT, "offers": {"price": "12"}}

    event = scraper._parse_jsonld_event(data)

    assert event is not None
    assert event.ticket_url == "https://tickets.example/jet"
    assert event.price_min == 12


def test_uses_ticket_link_instead_of_first_description_link():
    scraper = EventONScraper("bowstring-brewyard-raleigh", {})
    data = {
        **EVENT,
        "description": (
            '<a href="https://bowstring.example/about">Venue details</a>'
            '<a href="https://checkout.example/tickets/jet">Buy tickets</a>'
        ),
    }

    event = scraper._parse_jsonld_event(data)

    assert event is not None
    assert event.ticket_url == "https://checkout.example/tickets/jet"


def test_non_event_jsonld_is_ignored():
    scraper = EventONScraper("bowstring-brewyard-raleigh", {})
    soup = BeautifulSoup(
        '<script type="application/ld+json">{"@type":"WebPage"}</script>', "lxml"
    )

    assert scraper._extract_jsonld_events(soup) == []


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class FakeAsyncClient:
    response_text = ""

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, url):
        return FakeResponse(self.response_text)


def test_missing_eventon_calendar_is_a_failed_scrape(monkeypatch):
    FakeAsyncClient.response_text = "<html><body>No calendar</body></html>"
    monkeypatch.setattr(eventon_module.httpx, "AsyncClient", FakeAsyncClient)
    scraper = EventONScraper(
        "bowstring-brewyard-raleigh", {"url": "https://bowstring.example/events/"}
    )

    with pytest.raises(RuntimeError, match="EventON calendar not found"):
        asyncio.run(scraper.scrape())


def test_empty_eventon_calendar_is_a_failed_scrape(monkeypatch):
    FakeAsyncClient.response_text = '<div class="ajde_evcal_calendar"></div>'
    monkeypatch.setattr(eventon_module.httpx, "AsyncClient", FakeAsyncClient)
    scraper = EventONScraper(
        "bowstring-brewyard-raleigh", {"url": "https://bowstring.example/events/"}
    )

    with pytest.raises(RuntimeError, match="contained no parseable events"):
        asyncio.run(scraper.scrape())
