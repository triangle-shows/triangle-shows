"""Diagnose why HTML scrapers are returning 0 events."""
import urllib.request
import urllib.error
import re
import json

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

venues = [
    ("lincoln-theatre",      "https://www.lincolntheatre.com/events/"),
    ("cats-cradle",          "https://catscradle.com/events/"),
    ("local-506",            "https://local506.com/events/"),
    ("the-pinhook",          "https://www.thepinhook.com/events/"),
    ("carolina-theatre",     "https://www.carolinatheatre.org/events"),
    ("motorco",              "https://motorcomusic.com/calendar/"),
    ("kings",                "https://www.kingsraleigh.com/"),
    ("the-cave",             "https://caverntavern.com/"),
]

JS_FRAMEWORKS = ["react", "vue", "angular", "__next", "gatsby", "nuxt", "svelte", "ember"]

for name, url in venues:
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"  {url}")
    print(f"{'='*60}")
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="ignore")

        # Page title
        title_m = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
        print(f"  Title: {title_m.group(1).strip()[:80] if title_m else '?'}")

        # JS framework detection
        html_lower = html.lower()
        detected = [fw for fw in JS_FRAMEWORKS if fw in html_lower]
        if detected:
            print(f"  JS frameworks detected: {detected}")

        # JSON-LD events
        jsonld_scripts = re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S | re.I)
        event_jsonld = []
        for s in jsonld_scripts:
            try:
                data = json.loads(s)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    t = item.get("@type", "")
                    if isinstance(t, list):
                        t = " ".join(t)
                    if "event" in t.lower():
                        event_jsonld.append(item.get("name", "?")[:60])
            except Exception:
                pass
        if event_jsonld:
            print(f"  JSON-LD events found: {len(event_jsonld)}")
            for e in event_jsonld[:3]:
                print(f"    - {e}")
        else:
            print(f"  JSON-LD events: none")

        # CSS classes that look event-related
        all_classes = re.findall(r'class="([^"]+)"', html)
        event_classes = set()
        for cls_str in all_classes:
            for cls in cls_str.split():
                if any(kw in cls.lower() for kw in ["event", "show", "concert", "gig", "calendar", "tribe", "rhp", "ep-"]):
                    event_classes.add(cls)
        if event_classes:
            print(f"  Event-related CSS classes: {sorted(event_classes)[:15]}")
        else:
            print(f"  Event-related CSS classes: none found")

        # Body content size (small = JS-rendered shell)
        body_m = re.search(r"<body[^>]*>(.*?)</body>", html, re.S | re.I)
        body_text = re.sub(r"<[^>]+>", " ", body_m.group(1)) if body_m else ""
        body_text = re.sub(r"\s+", " ", body_text).strip()
        print(f"  Body text length: {len(body_text)} chars")
        # Show a snippet of body text
        if body_text:
            print(f"  Body snippet: {body_text[:200]}")

    except urllib.error.HTTPError as e:
        print(f"  HTTP ERROR: {e.code}")
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
