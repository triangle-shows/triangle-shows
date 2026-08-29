"""
Unit tests for app.scrapers.tickpick_organizer — the schema.org image handling.

`_parse_event` never read the JSON-LD `image` property, so every Chapel of Bones
event was stored with image_url = None regardless of what TickPick published.

schema.org does not fix a single shape for `image`: it may be a bare URL string,
a list of those, an ImageObject dict, or a list of ImageObject dicts. A fetch of
the live organizer page confirmed the JSON-LD is reachable and this parse path is
the one exercised (one Organization block, 50 nested Events), but none of those
50 events populates `image` at all today — only the top-level Organization.logo
does. So the absent-image case below is the one confirmed against live data; the
present-image cases follow the schema.org spec and the same normalization mec.py
and ticketmaster.py already apply.

Run from the backend/ directory:  pytest tests/test_tickpick_organizer.py
`_parse_event` is fed JSON-LD dicts directly, so there is no DB and no network.
"""

from datetime import date, timedelta

from app.scrapers.tickpick_organizer import TickPickOrganizerScraper

FUTURE = (date.today() + timedelta(days=30)).isoformat()

# The real key set of a single Event nested under Organization.event on the live
# page (@type, location, name, startDate, url), with `image` layered on per the
# shape under test.
BASE_EVENT = {
    "@type": "Event",
    "location": {
        "@type": "Place",
        "name": "Chapel of Bones",
        "address": "600 Glenwood Ave, Raleigh, NC 27603",
    },
    "name": "Cat Power",
    "startDate": f"{FUTURE}T20:00:00Z",
    "url": "https://www.tickpick.com/organizer/event/cat-power-12345678",
}

POSTER = "https://static-o.tickpick.com/poster.jpg"
POSTER_2 = "https://static-o.tickpick.com/poster-2.jpg"


def _scraper() -> TickPickOrganizerScraper:
    return TickPickOrganizerScraper("chapel-of-bones", {"organizer_id": "chapel-of-bones"})


def _image_url(image=...):
    data = dict(BASE_EVENT)
    if image is not ...:
        data["image"] = image
    parsed = _scraper()._parse_event(data)
    assert parsed is not None, "the event must survive whatever `image` holds"
    return parsed.image_url


def test_a_bare_url_string_is_taken_as_is():
    assert _image_url(POSTER) == POSTER


def test_a_list_of_urls_yields_the_first():
    assert _image_url([POSTER, POSTER_2]) == POSTER


def test_an_image_object_yields_its_url():
    assert _image_url({"@type": "ImageObject", "url": POSTER}) == POSTER


def test_a_list_of_image_objects_yields_the_first_url():
    image = [
        {"@type": "ImageObject", "url": POSTER},
        {"@type": "ImageObject", "url": POSTER_2},
    ]
    assert _image_url(image) == POSTER


def test_an_absent_image_is_none():
    """The live shape today — every event on the page omits `image` entirely."""
    assert _image_url() is None


def test_an_empty_list_is_none():
    assert _image_url([]) is None


def test_a_blank_string_is_none_not_a_broken_img_src():
    for blank in ("", "   ", "\n\t"):
        assert _image_url(blank) is None


def test_an_unrecognised_shape_is_none_rather_than_an_exception():
    for junk in (42, [[POSTER]], {"@type": "ImageObject"}, {"url": 7}):
        assert _image_url(junk) is None


def test_the_fields_the_scraper_already_derived_are_unchanged():
    parsed = _scraper()._parse_event({**BASE_EVENT, "image": POSTER})
    assert parsed.name == "Cat Power"
    assert parsed.artist == "Cat Power"
    assert parsed.date == date.fromisoformat(FUTURE)
    assert parsed.show_time is not None
    assert (parsed.show_time.hour, parsed.show_time.minute) == (20, 0)
    assert parsed.ticket_url == BASE_EVENT["url"]
    assert parsed.source_url == BASE_EVENT["url"]
