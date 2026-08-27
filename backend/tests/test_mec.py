"""
Tests for the MEC scraper's description handling (#13).

Regression: MEC's JSON-LD `description` field stores markup that has itself been
through an HTML-entity pass — the literal text '&lt;p&gt;&lt;strong&gt;...' rather
than an actual '<p>' tag. Reading it with a plain `data.get("description")` put that
literal escape text on the page, e.g. "&lt;p&gt;&lt;strong&gt;Presenting the final
volume...". Confirmed against real Shadowbox Studio data.

The fixture below is trimmed from a real Shadowbox event.
"""

from app.scrapers.mec import MECScraper


# Trimmed from a real Shadowbox Studio JSON-LD Event block.
DOUBLE_ENCODED_DESCRIPTION = (
    "&lt;p&gt;&lt;strong&gt;Presenting the final volume of Summer Nights hip hop "
    "showcase!&lt;/strong&gt;&lt;/p&gt; &lt;p&gt;Co-presented by "
    "&lt;strong&gt;&lt;a href=&quot;https://www.superempty.com&quot; "
    "target=&quot;_blank&quot;&gt;Super Empty&lt;/a&gt;&lt;/strong&gt;.&lt;/p&gt;"
)


def _scraper() -> MECScraper:
    return MECScraper("shadowbox-studio", {"url": "https://example.org/events/"})


def _jsonld(**overrides) -> dict:
    data = {
        "@type": "Event",
        "name": "Summer Nights, Vol. 3",
        "startDate": "2026-09-10T20:00:00",
        "description": DOUBLE_ENCODED_DESCRIPTION,
    }
    data.update(overrides)
    return data


class TestDescriptionIsReadable:
    def test_no_escape_sequences_reach_the_event(self):
        """The regression: this used to be the literal text '&lt;p&gt;...'."""
        parsed = _scraper()._parse_jsonld_event(_jsonld())

        assert parsed is not None
        assert "&lt;" not in parsed.description
        assert "&gt;" not in parsed.description

    def test_no_tags_reach_the_event(self):
        parsed = _scraper()._parse_jsonld_event(_jsonld())
        assert "<p>" not in parsed.description
        assert "<strong>" not in parsed.description

    def test_the_readable_text_is_present(self):
        parsed = _scraper()._parse_jsonld_event(_jsonld())
        assert "Presenting the final volume of Summer Nights hip hop showcase!" in (
            parsed.description
        )
        assert "Co-presented by Super Empty." in parsed.description

    def test_missing_description_is_none_not_empty_string(self):
        data = _jsonld()
        del data["description"]
        parsed = _scraper()._parse_jsonld_event(data)
        assert parsed.description is None

    def test_a_long_description_is_truncated_after_cleaning(self):
        """Truncating the raw markup first could cut mid-tag; must cut clean text."""
        html = "&lt;p&gt;" + ("x" * 600) + "&lt;/p&gt;"
        parsed = _scraper()._parse_jsonld_event(_jsonld(description=html))
        assert len(parsed.description) == 500
        assert "&lt;" not in parsed.description

    def test_required_fields_are_unaffected(self):
        """Description cleaning must not disturb the rest of the parse."""
        parsed = _scraper()._parse_jsonld_event(_jsonld())
        assert parsed.name == "Summer Nights, Vol. 3"
        assert parsed.venue_slug == "shadowbox-studio"
        assert parsed.source == "mec"
