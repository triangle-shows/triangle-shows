"""
Tests for the versioned asset URLs on index.html.

What this protects. index.html references /css and /js at fixed URLs, and Cloudflare caches
those at the edge for hours while never caching index.html itself. After a deploy that
serves new HTML against old assets, which is worse than serving a wholly stale page: on the
2026-08-28 release the new markup loaded a new equalizer.js against a styles.css with no
rules for it, and the sidebar rendered as unstyled buttons. Appending ?v=<commit sha> makes
every deploy a new URL at the edge.

The mechanism has two halves and a test that only checks one of them passes for the wrong
reason, so both are pinned:

  1. The response contains no unsubstituted placeholder. On its own this also passes if
     index.html simply never had a placeholder in it.
  2. The file on disk still contains the placeholder, and the response carries a real
     version token in its place.

The CDN assertion is here because the substitution is a string replace over the whole
document: a broader pattern that caught the FullCalendar or Google Fonts URLs would append
a query string to a third-party request, which is both wrong and hard to notice.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.main import (
    ASSET_VERSION_PLACEHOLDER,
    _asset_version,
    _frontend_dir,
    _index_html,
    app,
)


needs_frontend = pytest.mark.skipif(
    _frontend_dir is None, reason="no frontend directory in this checkout"
)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def index_body(client):
    response = client.get("/")
    assert response.status_code == 200, "index.html is not being served at /"
    return response.text


@needs_frontend
class TestPlaceholderIsSubstituted:
    def test_no_placeholder_survives_into_the_response(self, index_body):
        assert ASSET_VERSION_PLACEHOLDER not in index_body

    def test_the_source_file_really_does_contain_the_placeholder(self):
        """Guards against the first assertion passing because nothing needed substituting.

        If someone removes the placeholders from index.html, the whole mechanism silently
        stops working while every other test here still passes.
        """
        raw = (_frontend_dir / "index.html").read_text(encoding="utf-8")
        assert ASSET_VERSION_PLACEHOLDER in raw

    def test_local_assets_carry_the_version_token(self, index_body):
        version = _asset_version()
        assert f"/css/styles.css?v={version}" in index_body
        for script in ("app.js", "config.js", "design.js", "equalizer.js"):
            assert f"/js/{script}?v={version}" in index_body

    def test_every_local_asset_is_versioned_not_just_some(self, index_body):
        """A partially versioned page is the exact failure being fixed: the mixed state,
        where some files come from the new deploy and some from cache."""
        unversioned = re.findall(r'(?:href|src)="(/(?:css|js)/[^"?]+\.(?:css|js))"', index_body)
        assert unversioned == [], f"local assets missing ?v=: {unversioned}"


@needs_frontend
class TestThirdPartyUrlsAreUntouched:
    def test_cdn_and_font_urls_have_no_version_query(self, index_body):
        for host in ("cdn.jsdelivr.net", "fonts.googleapis.com", "fonts.gstatic.com"):
            for url in re.findall(rf'"(https://{re.escape(host)}[^"]*)"', index_body):
                assert "?v=" not in url, f"third-party URL was versioned: {url}"


@needs_frontend
class TestTheDocumentItselfIsNotCached:
    def test_cache_control_forbids_storing_the_html(self, client):
        """The load-bearing header. This document is what names the versioned asset URLs,
        so a cached copy keeps pointing at the previous deploy's files and the versioning
        buys nothing."""
        cache_control = client.get("/").headers.get("cache-control", "")
        assert "no-store" in cache_control

    def test_index_html_path_behaves_like_the_root(self, client):
        """Both routes exist, so a link to /index.html cannot bypass the substitution and
        get the raw file from the StaticFiles mount."""
        response = client.get("/index.html")
        assert response.status_code == 200
        assert ASSET_VERSION_PLACEHOLDER not in response.text
        assert "no-store" in response.headers.get("cache-control", "")


class TestVersionToken:
    def test_falls_back_to_dev_when_git_commit_is_unset(self, monkeypatch):
        """Local dev and CI have no GIT_COMMIT, and must still serve a usable page."""
        monkeypatch.delenv("GIT_COMMIT", raising=False)
        assert _asset_version() == "dev"

    def test_uses_the_commit_sha_when_present(self, monkeypatch):
        monkeypatch.setenv("GIT_COMMIT", "0123456789abcdef0123456789abcdef01234567")
        assert _asset_version() == "0123456789ab"

    def test_token_is_url_safe(self, monkeypatch):
        """It goes straight into a query string, so anything needing escaping would produce
        a subtly wrong URL rather than an error."""
        monkeypatch.setenv("GIT_COMMIT", "06bdf7f132dc5fec7d541ce40e353d69a2784966")
        assert re.fullmatch(r"[A-Za-z0-9._-]+", _asset_version())

    def test_changing_the_sha_changes_the_asset_urls(self, monkeypatch):
        """The property the whole fix depends on: a new deploy must produce new URLs.

        _index_html is lru_cached, since neither the file nor the SHA changes within a
        container's life, so the cache is cleared here to exercise a fresh render.
        """
        if _frontend_dir is None:
            pytest.skip("no frontend directory in this checkout")

        monkeypatch.setenv("GIT_COMMIT", "a" * 40)
        _index_html.cache_clear()
        first = _index_html()

        monkeypatch.setenv("GIT_COMMIT", "b" * 40)
        _index_html.cache_clear()
        second = _index_html()

        assert first != second
        assert "?v=" + "a" * 12 in first
        assert "?v=" + "b" * 12 in second

        _index_html.cache_clear()
