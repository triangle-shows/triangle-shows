"""
Unit tests for app.scrapers.rhp_events — reading support acts from a listing card.

Cat's Cradle's listing page (https://catscradle.com/events/) puts the support line in
`h4.eventSubHeader`, not in any of the `.eventSupport`-style elements the scraper
looked for, so every Cat's Cradle and Cat's Cradle Back Room show was stored with no
support acts. The subheader is read only as a fallback: a card that does carry one
of the dedicated support elements keeps using it.

The card markup below is trimmed from the live listing page, keeping the elements
and classes the parser reads. `_parse_event` is called directly, so no network is
involved.

Run from the backend/ directory:  pytest tests/test_rhp_events.py
"""

import pytest
from bs4 import BeautifulSoup

from app.scrapers.rhp_events import RHPEventsScraper


def _card(title: str, sub_header: str = "", extra: str = "") -> str:
    """One `.eventWrapper` listing card, shaped like Cat's Cradle's live markup."""
    return f"""
    <div class="col-12 eventWrapper rhpSingleEvent py-4 px-0 rhp-event__single-event--list">
      <div class="rhp-event-thumb">
        <a class="url" href="https://catscradle.com/event/example/" title="{title}" rel="bookmark">
          <div id="eventDate" class="mb-0 eventMonth singleEventDate text-uppercase">Thu, Dec 03</div>
        </a>
      </div>
      <div class="col-12 px-0 eventTitleDiv">
        <h2 class="rhp-event__title--list">{title}</h2>
      </div>
      <div class="col-12 px-0">
        <h4 id="evSubHead" class="eventSubHeader rhp-event__subheader--list">
          {sub_header}
        </h4>
      </div>
      {extra}
      <div class="d-flex align-items-center eventVenue">
        <div class="rhpVenueContent rhp-event__venue-text--list">
          <a class="venueLink" href="https://catscradle.com/venue/cats-cradle/">Cat&#8217;s Cradle</a>
        </div>
      </div>
    </div>
    """


# Harvey Street, Dec 3 2026: the support line lives only in the subheader.
HARVEY_STREET = _card("Harvey Street", "Tobacco Road, Wim Tapley and The Cannons  ")

# Holler Choir, Dec 4 2026: a headline-only show, so the subheader is present but empty.
HOLLER_CHOIR = _card("Holler Choir")

# Hand-made: a card carrying both a dedicated support element and a subheader. The
# dedicated element sits later in the document than the subheader, which is why the
# subheader cannot simply join the same select_one() selector list.
BOTH = _card(
    "Example Headliner",
    "A tagline, not the support acts",
    extra='<div class="eventSupport">Real Support Act</div>',
)


def _scraper() -> RHPEventsScraper:
    return RHPEventsScraper("cats-cradle", {"url": "https://catscradle.com/events/"})


def _parse(html: str):
    wrapper = BeautifulSoup(html, "lxml").select_one(".eventWrapper")
    return _scraper()._parse_event(wrapper, venue_filter=None)


@pytest.mark.parametrize(
    "html, expected",
    [
        (HARVEY_STREET, "Tobacco Road, Wim Tapley and The Cannons"),
        (HOLLER_CHOIR, None),
        (BOTH, "Real Support Act"),
    ],
    ids=["subheader-only", "empty-subheader", "dedicated-element-wins"],
)
def test_support_artists(html, expected):
    event = _parse(html)
    assert event is not None
    assert event.support_artists == expected


def test_subheader_does_not_change_the_headliner():
    event = _parse(HARVEY_STREET)
    assert event.name == "Harvey Street"
    assert event.artist == "Harvey Street"
