from datetime import date, time

from bs4 import BeautifulSoup

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

    import asyncio
    import pytest

    with pytest.raises(ValueError, match="No URL configured"):
        asyncio.run(scraper.scrape())
