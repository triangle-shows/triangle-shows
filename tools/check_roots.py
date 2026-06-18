"""Check root domains and sniff for event page URLs."""
import urllib.request
import urllib.error
import re

headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

roots = [
    ("motorco",   "https://www.motorcomusic.com/"),
    ("kings",     "https://www.kingsbarcade.com/"),
    ("the-cave",  "https://www.caverntavern.com/"),
    ("moon-room", "https://www.moonroomraleigh.com/"),
    ("neptunes",  "https://www.neptunesparlour.com/"),
]

for name, url in roots:
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            final_url = r.url
            status = r.status
            html = r.read().decode("utf-8", errors="ignore")
            links = set(re.findall(r'href="([^"]*)"', html, re.I))
            event_links = sorted(l for l in links if "event" in l.lower() or "show" in l.lower() or "calendar" in l.lower())
            print(f"[{name}] {status} OK => {final_url}")
            for l in event_links[:8]:
                print(f"    {l}")
    except urllib.error.HTTPError as e:
        print(f"[{name}] HTTP {e.code} => {url}")
    except Exception as e:
        print(f"[{name}] ERROR: {e}")
