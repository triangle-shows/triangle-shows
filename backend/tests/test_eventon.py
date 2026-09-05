import asyncio
from datetime import date, time

import pytest
from bs4 import BeautifulSoup

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


def test_non_event_jsonld_is_ignored():
    scraper = EventONScraper("bowstring-brewyard-raleigh", {})
    soup = BeautifulSoup(
        '<script type="application/ld+json">{"@type":"WebPage"}</script>', "lxml"
    )

    assert scraper._extract_jsonld_events(soup) == []
