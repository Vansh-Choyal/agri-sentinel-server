"""Refresh pinned browser assets; runtime never depends on an external CDN."""
import re
from pathlib import Path

import requests

root = Path(__file__).resolve().parent.parent / "agrisentinel" / "static"
session = requests.Session()
session.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"


def download(url, target):
    response = session.get(url, timeout=30)
    response.raise_for_status()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(response.content)
    print(f"Vendored {target.name} ({len(response.content):,} bytes)")


download("https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js", root / "vendor" / "chart.umd.min.js")
download("https://cdn.jsdelivr.net/npm/chart.js@4.5.1/LICENSE.md", root / "vendor" / "CHART-LICENSE.md")
for family, slug in (("IBM Plex Sans", "plex-sans"), ("IBM Plex Mono", "plex-mono")):
    response = session.get("https://fonts.googleapis.com/css2", params={"family": family + ":wght@400;500;600", "display": "swap"}, timeout=30)
    response.raise_for_status()
    # The final latin face has the Latin codepoints used by this English interface.
    latin_faces = response.text.split("/* latin */")[1:]
    urls = re.findall(r"url\((https://[^)]+)\)", latin_faces[0] if latin_faces else response.text)
    if not urls:
        raise RuntimeError(f"No font source returned for {family}")
    download(urls[0], root / "fonts" / f"{slug}.woff2")
    download(f"https://raw.githubusercontent.com/google/fonts/main/ofl/{family.lower().replace(' ', '')}/OFL.txt", root / "fonts" / f"{slug}-OFL.txt")
