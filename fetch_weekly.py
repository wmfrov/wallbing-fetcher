#!/usr/bin/env python3
"""
Fetch the last 8 Bing images of the day and save any that are missing locally.
Maintains images.txt manifest with one Bing CDN URL per line.
Designed to run weekly via launchd. Exits 0 on success, 1 on failure.
"""
import os
import re
import shutil
import sys

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WALLPAPER_DIR = os.environ.get("WALLPAPER_DIR", os.path.expanduser("~/Pictures/bingimages"))
MANIFEST_PATH = os.path.join(SCRIPT_DIR, "images.txt")
TIMEOUT = 30
BING_BASE = "https://www.bing.com"
API_URL = f"{BING_BASE}/HPImageArchive.aspx?format=js&idx=0&n=8&mkt=en-US"
RESOLUTION_RE = re.compile(r"_\d+x\d+\.jpg", re.IGNORECASE)


def load_manifest():
    """Load existing CDN URLs from images.txt into a set."""
    if os.path.isfile(MANIFEST_PATH):
        with open(MANIFEST_PATH, "r") as f:
            return set(line.strip() for line in f if line.strip())
    return set()


def seed_manifest(known_urls):
    """Create images.txt from existing local filenames if it doesn't exist yet."""
    if os.path.isfile(MANIFEST_PATH):
        return known_urls
    if not os.path.isdir(WALLPAPER_DIR):
        return known_urls
    seeded = set()
    for name in os.listdir(WALLPAPER_DIR):
        if not name.lower().endswith(".jpg"):
            continue
        base = name.rsplit(".", 1)[0]
        slug = re.sub(r"_UHD$", "", base)
        url = cdn_url_from_slug(slug)
        seeded.add(url)
    if seeded:
        with open(MANIFEST_PATH, "w") as f:
            for url in sorted(seeded):
                f.write(url + "\n")
        print(f"Seeded images.txt with {len(seeded)} existing images")
    return seeded


def append_to_manifest(url):
    with open(MANIFEST_PATH, "a") as f:
        f.write(url + "\n")


def fetch_image_list():
    r = requests.get(API_URL, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    images = data.get("images", [])
    if not images:
        raise RuntimeError("No images returned by Bing API")
    return images


def slug_from_entry(entry):
    urlbase = entry.get("urlbase", "")
    if "OHR." in urlbase:
        return urlbase.split("OHR.", 1)[-1].strip()
    url = entry.get("url", "")
    base = RESOLUTION_RE.sub("", url)
    if "OHR." in base:
        return base.split("OHR.", 1)[-1].split("&")[0]
    return None


def cdn_url_from_slug(slug):
    return f"{BING_BASE}/th?id=OHR.{slug}_UHD.jpg"


def download_image(entry, slug):
    url = entry.get("url", "")
    uhd_url = BING_BASE + RESOLUTION_RE.sub("_UHD.jpg", url)
    filename = slug + "_UHD.jpg"
    dest = os.path.join(WALLPAPER_DIR, filename)

    r = requests.get(uhd_url, stream=True, timeout=TIMEOUT)
    r.raise_for_status()
    with open(dest, "wb") as f:
        shutil.copyfileobj(r.raw, f)
    return filename


def main():
    os.makedirs(WALLPAPER_DIR, exist_ok=True)

    known_urls = load_manifest()
    known_urls = seed_manifest(known_urls)
    existing_files = set(os.listdir(WALLPAPER_DIR))

    try:
        images = fetch_image_list()
    except Exception as e:
        print(f"Error fetching API: {e}", file=sys.stderr)
        return 1

    downloaded = 0
    skipped = 0
    errors = 0

    for entry in images:
        slug = slug_from_entry(entry)
        if not slug:
            print(f"  Skip: could not parse slug from {entry.get('urlbase', '?')}", file=sys.stderr)
            errors += 1
            continue

        filename = slug + "_UHD.jpg"
        url = cdn_url_from_slug(slug)

        if filename in existing_files or url in known_urls:
            skipped += 1
            continue

        try:
            download_image(entry, slug)
            append_to_manifest(url)
            known_urls.add(url)
            downloaded += 1
            print(f"  Downloaded: {filename}")
        except Exception as e:
            print(f"  Error downloading {filename}: {e}", file=sys.stderr)
            errors += 1

    print(f"Done. Downloaded: {downloaded}, skipped: {skipped}, errors: {errors}")
    return 1 if errors and not downloaded else 0


if __name__ == "__main__":
    sys.exit(main())
