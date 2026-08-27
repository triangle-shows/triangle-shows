"""
Turns an HTML description fragment into human-readable plain text.

Role: Shared by scrapers whose source hands back a marked-up description - MEC's
JSON-LD `description` field and Squarespace's `excerpt`/`body` fields. Kept out of
scrapers/base.py, which is pure-stdlib by design; this needs BeautifulSoup, but only
the two scrapers that actually have HTML to clean need to import it.
Requires: beautifulsoup4, lxml (both already required by every scraper that produces
HTML in the first place).
"""

import html
import re
from typing import Optional

from bs4 import BeautifulSoup

# \xa0 is U+00A0 NO-BREAK SPACE, what &nbsp; decodes to. Not a plain space, and not
# matched by \s in every regex flavor, so it needs to be named explicitly.
_WHITESPACE_RUN = re.compile(r"[ \t\xa0]+")


def clean_html_text(
    raw: Optional[str],
    *,
    drop_first_paragraph_if: Optional[str] = None,
    limit: Optional[int] = None,
) -> Optional[str]:
    """Decode entities, strip tags, and return readable multi-paragraph text.

    Every source this has been checked against (MEC's JSON-LD description; Squarespace's
    excerpt and Post Body content) uses `<p>` as its only block-level element -- `<strong>`,
    `<a>`, and `<em>` appear only inline. So paragraphs are read from `<p>` tags
    specifically, joined with a blank line, rather than every block-level tag: selecting
    `<div>` as well would double-count text, since BeautifulSoup's find_all matches an
    outer container and the paragraphs nested inside it as separate results. `<br>` is
    converted to a newline before extraction, since it carries no text of its own.
    Falls back to reading the whole fragment as one block when there are no `<p>` tags
    at all, so plain, already-markup-free text is not silently dropped.

    raw can itself be HTML-escaped HTML -- MEC's JSON-LD field stores markup as a string
    that has ALSO been through an HTML-entity pass, e.g. the literal text
    '&lt;p&gt;&lt;strong&gt;...' rather than an actual '<p>' tag. `html.unescape` runs
    first so BeautifulSoup sees real tags either way; it is a no-op on already-plain HTML.

    drop_first_paragraph_if: if the first paragraph case-insensitively equals this string
    once both are stripped, drop it. Squarespace's Post Body editor always repeats the
    event's own title as the first paragraph of `body` -- never of `excerpt`, which is a
    genuinely separate summary field -- so pass the event title from `body`-sourced content
    and leave it unset for `excerpt`. Real description text is never exactly the event's
    own title, so this cannot discard a paragraph that happens to be genuine content.

    limit: truncate the CLEANED text to this many characters. Truncating before cleaning
    can cut mid-tag or mid-entity and leave a stray fragment on the visible card.

    Returns None for empty or unparseable input -- same convention as pulling an optional
    field straight off a dict -- so callers can pass it directly into the description slot.
    """
    if not raw:
        return None

    text = html.unescape(raw)
    soup = BeautifulSoup(text, "lxml")
    for br in soup.find_all("br"):
        br.replace_with("\n")

    paragraphs = [_WHITESPACE_RUN.sub(" ", p.get_text()).strip() for p in soup.find_all("p")]
    paragraphs = [p for p in paragraphs if p]

    if not paragraphs:
        whole = _WHITESPACE_RUN.sub(" ", soup.get_text()).strip()
        paragraphs = [whole] if whole else []

    if (
        drop_first_paragraph_if
        and paragraphs
        and paragraphs[0].casefold() == drop_first_paragraph_if.strip().casefold()
    ):
        paragraphs = paragraphs[1:]

    cleaned = "\n\n".join(paragraphs).strip()
    if not cleaned:
        return None
    return cleaned[:limit] if limit else cleaned
