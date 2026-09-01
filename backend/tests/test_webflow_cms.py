"""
Unit tests for app.scrapers.webflow_cms — the show-flyer extraction.

Pour House renders every show twice on one page, in two separate Webflow CMS
lists: a "Calendar" list (`.show-collection-item`, carrying name/date/slug and no
image at all) and a "Grid" list, whose entries wrap an `<a href="/shows/<slug>">`
around the flyer `<img>`. A fetch of the live calendar showed 46 shows in each
list, 0 `<img>` elements anywhere inside the calendar list, and all 46 slugs
present on both sides — so the flyer is only reachable by joining the two lists
on slug.

Run from the backend/ directory:  pytest tests/test_webflow_cms.py
The parsing is fed HTML fragments directly, so there is no DB and no network —
the scraper's own HTTP call is not exercised here.
"""

from bs4 import BeautifulSoup

from app.scrapers.webflow_cms import WebflowCMSScraper


# Trimmed from the live markup at https://www.pourhouseraleigh.com/calendar:
# the real CSS classes and nesting the scraper's selectors target. The second
# show has no counterpart in the grid fragment, exercising the absent-flyer path.
CALENDAR_LIST = """
<div class="w-dyn-list">
  <div role="list" class="show-collection-list w-dyn-items">
    <div role="listitem" class="show-collection-item w-dyn-item">
      <div class="show-name">(18+) Nilufer Yanya</div>
      <div class="show-start-date">August 9, 2026</div>
      <div class="show-slug">18-nilufer-yanya-09-aug</div>
    </div>
    <div role="listitem" class="show-collection-item w-dyn-item">
      <div class="show-name">Chuquimamani-Condori</div>
      <div class="show-start-date">August 14, 2026</div>
      <div class="show-slug">chuquimamani-condori-14-aug</div>
    </div>
  </div>
</div>
"""

NILUFER_FLYER = "https://cdn.prod.website-files.com/68f7b7271d0ad608b3ca1008/aaa_nilufer-yanya.jpeg"

GRID_LIST = f"""
<div class="uui-padding-vertical-large-3 w-dyn-list">
  <div role="list" class="uui-layout88_list w-dyn-items">
    <div role="listitem" class="uui-layout88_item-2 w-dyn-item">
      <a href="/shows/18-nilufer-yanya-09-aug" class="link-block-2 w-inline-block">
        <div class="show-image-wrapper">
          <img loading="lazy" alt="" class="image-48" src="{NILUFER_FLYER}"/>
        </div>
      </a>
    </div>
  </div>
</div>
"""

PAGE = f"<html><body>{CALENDAR_LIST}{GRID_LIST}</body></html>"


def _scraper() -> WebflowCMSScraper:
    return WebflowCMSScraper(
        "pour-house",
        {
            "url": "https://www.pourhouseraleigh.com/calendar",
            "base_url": "https://www.pourhouseraleigh.com",
        },
    )


def _soup(html: str = PAGE) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def test_flyer_is_picked_up_from_the_grid_list():
    events = _scraper()._parse_soup(_soup())
    nilufer = next(e for e in events if "Nilufer" in e.name)
    assert nilufer.image_url == NILUFER_FLYER


def test_show_missing_from_the_grid_list_gets_no_flyer():
    events = _scraper()._parse_soup(_soup())
    chuqui = next(e for e in events if e.name == "Chuquimamani-Condori")
    assert chuqui.image_url is None


def test_a_page_with_no_grid_list_at_all_still_yields_every_event():
    """Markup drift must degrade image_url to None, never raise or drop an event."""
    events = _scraper()._parse_soup(_soup(CALENDAR_LIST))
    assert len(events) == 2
    assert all(e.image_url is None for e in events)


def test_the_fields_the_scraper_already_derived_are_unchanged():
    events = _scraper()._parse_soup(_soup())
    nilufer = next(e for e in events if "Nilufer" in e.name)
    assert nilufer.name == "Nilufer Yanya"
    assert nilufer.age_restriction == "18+"
    assert nilufer.ticket_url == "https://www.pourhouseraleigh.com/shows/18-nilufer-yanya-09-aug"


# --- The join is by slug, never by list position ---
# The two lists are independent Webflow collections with no guaranteed shared
# order and no guaranteed 1:1 cardinality. Both shows below appear in the grid
# list, but in the REVERSE order of the calendar list and with distinct flyers,
# so a positional (zip) pairing crosses the two images and only a real slug join
# keeps them straight.

CALENDAR_TWO = """
<div class="w-dyn-list">
  <div role="list" class="show-collection-list w-dyn-items">
    <div role="listitem" class="show-collection-item w-dyn-item">
      <div class="show-name">Juana Molina</div>
      <div class="show-start-date">September 3, 2026</div>
      <div class="show-slug">juana-molina-03-sep</div>
    </div>
    <div role="listitem" class="show-collection-item w-dyn-item">
      <div class="show-name">Jessica Pratt</div>
      <div class="show-start-date">September 5, 2026</div>
      <div class="show-slug">jessica-pratt-05-sep</div>
    </div>
  </div>
</div>
"""

JUANA_FLYER = "https://cdn.prod.website-files.com/68f7b7271d0ad608b3ca1008/bbb_juana-molina.jpeg"
JESSICA_FLYER = "https://cdn.prod.website-files.com/68f7b7271d0ad608b3ca1008/ccc_jessica-pratt.jpeg"

GRID_TWO_REVERSED = f"""
<div class="uui-padding-vertical-large-3 w-dyn-list">
  <div role="list" class="uui-layout88_list w-dyn-items">
    <div role="listitem" class="uui-layout88_item-2 w-dyn-item">
      <a href="/shows/jessica-pratt-05-sep" class="link-block-2 w-inline-block">
        <img loading="lazy" alt="" class="image-48" src="{JESSICA_FLYER}"/>
      </a>
    </div>
    <div role="listitem" class="uui-layout88_item-2 w-dyn-item">
      <a href="/shows/juana-molina-03-sep" class="link-block-2 w-inline-block">
        <img loading="lazy" alt="" class="image-48" src="{JUANA_FLYER}"/>
      </a>
    </div>
  </div>
</div>
"""


def test_flyers_are_joined_by_slug_not_by_position():
    events = _scraper()._parse_soup(
        _soup(f"<html><body>{CALENDAR_TWO}{GRID_TWO_REVERSED}</body></html>")
    )
    by_name = {e.name: e for e in events}
    assert by_name["Juana Molina"].image_url == JUANA_FLYER
    assert by_name["Jessica Pratt"].image_url == JESSICA_FLYER


def _grid_with(anchor_href: str, img_attrs: str) -> str:
    return f"""
    <div class="uui-padding-vertical-large-3 w-dyn-list">
      <div role="list" class="uui-layout88_list w-dyn-items">
        <div role="listitem" class="uui-layout88_item-2 w-dyn-item">
          <a href="{anchor_href}" class="link-block-2 w-inline-block">
            <img loading="lazy" alt="" class="image-48" {img_attrs}/>
          </a>
        </div>
      </div>
    </div>
    """


def _chuqui_flyer(grid: str) -> str:
    events = _scraper()._parse_soup(_soup(f"<html><body>{CALENDAR_LIST}{grid}</body></html>"))
    return next(e for e in events if e.name == "Chuquimamani-Condori").image_url


def test_a_lazy_loaded_flyer_is_read_from_data_src():
    """Some Webflow renders put the real URL in data-src with no plain src."""
    lazy = "https://cdn.prod.website-files.com/68f7b7271d0ad608b3ca1008/ddd_lazy.jpeg"
    grid = _grid_with("/shows/chuquimamani-condori-14-aug", f'data-src="{lazy}"')
    assert _chuqui_flyer(grid) == lazy


def test_a_trailing_slash_on_the_grid_href_still_joins():
    """The calendar slug carries no slash; /shows/<slug>/ must still match it."""
    flyer = "https://cdn.prod.website-files.com/68f7b7271d0ad608b3ca1008/eee_slash.jpeg"
    grid = _grid_with("/shows/chuquimamani-condori-14-aug/", f'src="{flyer}"')
    assert _chuqui_flyer(grid) == flyer


def test_a_duplicated_slug_keeps_the_first_flyer_in_document_order():
    """The grid can render one show twice; the choice must be deterministic."""
    first = "https://cdn.prod.website-files.com/68f7b7271d0ad608b3ca1008/fff_first.jpeg"
    second = "https://cdn.prod.website-files.com/68f7b7271d0ad608b3ca1008/999_second.jpeg"
    grid = (
        _grid_with("/shows/chuquimamani-condori-14-aug", f'src="{first}"')
        + _grid_with("/shows/chuquimamani-condori-14-aug", f'src="{second}"')
    )
    image_by_slug = WebflowCMSScraper._build_image_map(_soup(grid), "/shows/", "img")
    assert image_by_slug["chuquimamani-condori-14-aug"] == first
