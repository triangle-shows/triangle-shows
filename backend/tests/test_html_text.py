"""
Tests for clean_html_text, the shared HTML-to-readable-text helper (#13).

Two real bugs motivated this: Shadowbox (MEC) stored a description that was HTML
markup put through an extra HTML-entity pass, so the literal text '&lt;p&gt;...' was
what ended up on the page. Boom Club (Squarespace) had its real excerpt content
silently discarded because the extraction only knew how to read Squarespace's
Post-Body wrapper, which excerpt content is never wrapped in.

Fixtures below are trimmed from real feeds where noted.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.scrapers.html_text import clean_html_text  # noqa: E402


# --- The core regressions ---

class TestDoubleEncodedHtml:
    """Shadowbox / MEC: the JSON-LD description field itself has been HTML-escaped."""

    RAW = (
        "&lt;p&gt;&lt;strong&gt;Presenting the final volume of Summer Nights hip hop "
        "showcase!&lt;/strong&gt;&lt;/p&gt; &lt;p&gt;Co-presented by "
        "&lt;strong&gt;&lt;a href=&quot;https://www.superempty.com&quot; "
        "target=&quot;_blank&quot;&gt;Super Empty&lt;/a&gt;&lt;/strong&gt;.&lt;/p&gt;"
    )

    def test_no_escape_sequences_survive(self):
        out = clean_html_text(self.RAW)
        assert "&lt;" not in out and "&gt;" not in out and "&quot;" not in out

    def test_no_tags_survive(self):
        out = clean_html_text(self.RAW)
        assert "<p>" not in out and "<strong>" not in out and "<a " not in out

    def test_the_readable_text_survives(self):
        out = clean_html_text(self.RAW)
        assert "Presenting the final volume of Summer Nights hip hop showcase!" in out
        assert "Co-presented by Super Empty." in out

    def test_paragraphs_are_separated(self):
        out = clean_html_text(self.RAW)
        assert "\n\n" in out


class TestExcerptContentIsNotDiscarded:
    """Boom Club: excerpt is real content with no Post-Body wrapper around it."""

    # Trimmed from the live boom-club.org feed.
    EXCERPT = (
        '<p style="white-space:pre-wrap;" data-rte-preserve-empty="true">'
        "Modular virtuoso (and great friend of BOOM) Matthew Cha presents "
        "<em>Technojazz,</em> a live, semi-improvised suite.</p>"
    )

    def test_content_with_no_wrapping_div_is_still_read(self):
        """The regression: this used to return None with no wrapper to find."""
        out = clean_html_text(self.EXCERPT)
        assert out is not None
        assert "Modular virtuoso" in out
        assert "Technojazz" in out


# --- Title-repeat handling ---

class TestDropFirstParagraph:
    def test_matching_first_paragraph_is_dropped(self):
        html = "<p>The Regulars</p><p>Doors at 8pm.</p>"
        out = clean_html_text(html, drop_first_paragraph_if="The Regulars")
        assert "The Regulars" not in out
        assert out == "Doors at 8pm."

    def test_match_is_case_insensitive(self):
        html = "<p>THE REGULARS</p><p>Doors at 8pm.</p>"
        out = clean_html_text(html, drop_first_paragraph_if="the regulars")
        assert out == "Doors at 8pm."

    def test_non_matching_first_paragraph_is_kept(self):
        """Real content that isn't a title repeat must never be discarded."""
        html = "<p>Doors at 8pm.</p><p>$12 advance</p>"
        out = clean_html_text(html, drop_first_paragraph_if="The Regulars")
        assert "Doors at 8pm." in out

    def test_unset_never_drops_anything(self):
        """Excerpt content is never checked against the title — see squarespace.py."""
        html = "<p>The Regulars</p><p>Doors at 8pm.</p>"
        out = clean_html_text(html)
        assert "The Regulars" in out

    def test_a_solo_matching_paragraph_leaves_nothing(self):
        out = clean_html_text("<p>The Regulars</p>", drop_first_paragraph_if="The Regulars")
        assert out is None


# --- Structural handling ---

class TestStructure:
    def test_br_becomes_a_newline(self):
        out = clean_html_text("<p>Line one<br>Line two</p>")
        assert out == "Line one\nLine two"

    def test_no_p_tags_falls_back_to_the_whole_fragment(self):
        """Not every source wraps content in <p> — plain text must not vanish."""
        assert clean_html_text("Plain text, no markup at all.") == "Plain text, no markup at all."

    def test_inline_tags_do_not_fragment_a_paragraph(self):
        """<strong>/<a> must not each become their own paragraph."""
        out = clean_html_text("<p>Come see <strong>the band</strong> tonight.</p>")
        assert out == "Come see the band tonight."

    def test_nbsp_is_treated_as_whitespace(self):
        out = clean_html_text("<p>Doors at 8pm.</p>")
        assert out == "Doors at 8pm."

    def test_empty_paragraphs_are_dropped(self):
        out = clean_html_text("<p></p><p>Real content.</p><p>   </p>")
        assert out == "Real content."


# --- Truncation ---

class TestLimit:
    def test_truncates_the_cleaned_text_not_the_markup(self):
        """Truncating raw HTML first could cut mid-tag; this must cut the clean text."""
        html = "<p>" + ("x" * 600) + "</p>"
        out = clean_html_text(html, limit=500)
        assert len(out) == 500
        assert "<" not in out

    def test_short_text_is_not_padded_or_altered(self):
        assert clean_html_text("<p>Short.</p>", limit=500) == "Short."


# --- Empty / missing input ---

class TestEmptyInput:
    def test_none_returns_none(self):
        assert clean_html_text(None) is None

    def test_empty_string_returns_none(self):
        assert clean_html_text("") is None

    def test_whitespace_only_html_returns_none(self):
        assert clean_html_text("<p>   </p>") is None

    def test_layout_scaffolding_with_no_text_returns_none(self):
        """Boom Club's genuinely empty events (#35) — layout markup, zero text."""
        html = (
            '<div class="sqs-layout sqs-grid-12 columns-12 empty" '
            'data-layout-label="Post Body"><div class="row sqs-row">'
            '<div class="col sqs-col-12 span-12"></div></div></div>'
        )
        assert clean_html_text(html) is None
