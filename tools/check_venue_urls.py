"""Check current status of failing venue URLs."""
import urllib.request
import urllib.error

venues = [
    ("motorco",         "https://www.motorcomusic.com/events/"),
    ("kings",           "https://www.kingsbarcade.com/events/"),
    ("the-cave",        "https://www.caverntavern.com/events/"),
    ("moon-room",       "https://www.moonroomraleigh.com/events?format=json"),
    ("moon-room-alt",   "https://www.moonroomraleigh.com/events"),
    ("neptunes-parlour","https://neptunesparlour.com/events?format=json"),
    ("neptunes-no-www", "https://neptunesparlour.com/events"),
]

headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

for name, url in venues:
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            final_url = resp.url
            status = resp.status
            print(f"[{name}] {status} OK  =>  {final_url}")
    except urllib.error.HTTPError as e:
        print(f"[{name}] HTTP {e.code}  =>  {url}")
    except urllib.error.URLError as e:
        print(f"[{name}] ERROR: {e.reason}  =>  {url}")
    except Exception as e:
        print(f"[{name}] ERROR: {e}  =>  {url}")
