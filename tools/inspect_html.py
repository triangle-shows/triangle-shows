"""Fetch raw HTML snippets to understand event structure."""
import urllib.request
import re

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", errors="ignore")

def show_around(html, pattern, chars=800, label=""):
    m = re.search(pattern, html, re.I)
    if m:
        start = max(0, m.start() - 100)
        print(f"\n  [{label or pattern}] found at pos {m.start()}:")
        print(html[start:start+chars])
    else:
        print(f"\n  [{label or pattern}] NOT FOUND")

print("\n" + "="*60)
print("MOTORCO — looking for tc-responsive-event")
print("="*60)
html = fetch("https://motorcomusic.com/calendar/")
show_around(html, r'tc-responsive-event', chars=600, label="tc-responsive-event")

print("\n" + "="*60)
print("CATS CRADLE — looking for rhp-event structure")
print("="*60)
html = fetch("https://catscradle.com/events/")
show_around(html, r'rhp-event__date', chars=800, label="rhp-event BEM")

print("\n" + "="*60)
print("KINGS — looking for any event/calendar structure")
print("="*60)
html = fetch("https://www.kingsraleigh.com/")
# Show the middle section of the page where events likely are
mid = len(html) // 3
print(html[mid:mid+1500])
