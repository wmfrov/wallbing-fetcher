#!/usr/bin/env python3
"""
Sync a local folder of Bing wallpapers against metadata.json from gh-pages.

  - Downloads UHD versions of images missing from the folder
  - Replaces wrong-resolution versions with UHD (deletes the old file)
  - Sets mtime (and macOS creation date) to the image's Bing publish date

Usage:
  python3 sync_wallpapers.py /path/to/folder
  python3 sync_wallpapers.py /path/to/folder --dry-run
  python3 sync_wallpapers.py /path/to/folder --workers 16
"""

import json
import os
import re
import ssl
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_WORKERS = 8
USER_AGENT = "wallbing-sync/1.0"

# Matches _UHD or _1920x1080 etc. at the end of a stem
RES_RE = re.compile(r"[_-](UHD|\d{3,5}x\d{3,5})$", re.IGNORECASE)


# ── Metadata ──────────────────────────────────────────────────────────────

def load_metadata():
    subprocess.run(
        ["git", "fetch", "origin", "gh-pages"],
        capture_output=True, cwd=SCRIPT_DIR,
    )
    for ref in ("origin/gh-pages", "gh-pages"):
        result = subprocess.run(
            ["git", "show", f"{ref}:metadata.json"],
            capture_output=True, text=True, cwd=SCRIPT_DIR,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    print("ERROR: could not read metadata.json from gh-pages", file=sys.stderr)
    sys.exit(1)


# ── URL / filename helpers ─────────────────────────────────────────────────

def slug_filename(slug):
    """The canonical local filename for a slug, e.g. PenguinLove_EN-US123_UHD.jpg"""
    return slug + ".jpg"


def base_stem(stem):
    """Strip resolution suffix: 'PenguinLove_EN-US123_UHD' → 'PenguinLove_EN-US123'"""
    return RES_RE.sub("", stem)


def download_url(bing_url):
    """
    Primary:  the bing_url stored in metadata (query-param style)
    Fallback: path-based CDN URL, which some proxies handle better
    """
    parsed = urlparse(bing_url)
    id_val = parse_qs(parsed.query).get("id", [""])[0]  # OHR.Slug_UHD.jpg
    name = id_val.removeprefix("OHR.")                  # Slug_UHD.jpg
    fallback = f"https://www.bing.com/az/hprichbg/rb/{name}" if name else None
    return bing_url, fallback


# ── File date spoofing ─────────────────────────────────────────────────────

def set_file_date(path: str, date_str: str):
    """Set mtime to noon UTC on date_str (YYYY-MM-DD).
    Also sets macOS HFS/APFS creation date via SetFile if available."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
            hour=12, tzinfo=timezone.utc
        )
        ts = dt.timestamp()
        os.utime(path, (ts, ts))
    except Exception as e:
        print(f"  Warning: could not set mtime on {path}: {e}")
        return

    # macOS creation date (requires Xcode command line tools)
    try:
        mac_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%m/%d/%Y 12:00:00")
        subprocess.run(
            ["SetFile", "-d", mac_date, "-m", mac_date, path],
            check=True, capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass  # SetFile not available; mtime is enough


# ── Download ───────────────────────────────────────────────────────────────

def build_ssl_ctx():
    ctx = ssl.create_default_context()
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    try:
        urllib.request.urlopen(
            urllib.request.Request("https://www.bing.com", method="HEAD"),
            timeout=5, context=ctx,
        )
        return ctx
    except Exception:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx


def fetch_url(url, ssl_ctx, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as r:
        return r.read()


def download_image(primary_url, fallback_url, dest_path, ssl_ctx):
    """Download to dest_path. Returns True on success."""
    for url in filter(None, [primary_url, fallback_url]):
        try:
            data = fetch_url(url, ssl_ctx)
            if len(data) < 10_000:
                continue  # probably an error page, try fallback
            with open(dest_path, "wb") as f:
                f.write(data)
            return True
        except Exception:
            continue
    return False


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv
    workers = DEFAULT_WORKERS
    for i, arg in enumerate(sys.argv):
        if arg == "--workers" and i + 1 < len(sys.argv):
            workers = int(sys.argv[i + 1])

    positional = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not positional:
        print(__doc__)
        sys.exit(1)

    folder = Path(positional[0]).expanduser().resolve()
    if not folder.is_dir():
        print(f"ERROR: not a directory: {folder}", file=sys.stderr)
        sys.exit(1)

    print("Loading metadata from gh-pages...")
    metadata = load_metadata()
    print(f"  {len(metadata)} entries")

    # Index existing files: base_stem (lowercase) → Path
    # Separate UHD from other-resolution so we can prioritise
    uhd_files: dict[str, Path] = {}
    other_files: dict[str, Path] = {}
    for p in folder.iterdir():
        if p.suffix.lower() not in (".jpg", ".jpeg"):
            continue
        b = base_stem(p.stem).lower()
        if RES_RE.sub("", p.stem) != p.stem or "_uhd" in p.stem.lower():
            # File has a resolution suffix
            if "_uhd" in p.stem.lower():
                uhd_files[b] = p
            else:
                other_files[b] = p
        else:
            # No resolution suffix at all — treat as unknown, skip replacing
            other_files[b] = p

    print(f"  {len(uhd_files)} UHD files, {len(other_files)} other-resolution files in folder")

    ssl_ctx = build_ssl_ctx()

    # Classify each metadata entry
    to_download: list[tuple] = []  # (slug, entry, dest, replace_path | None)
    already_ok = 0
    already_ok_slugs = []

    for slug, entry in metadata.items():
        bing_url = entry.get("bing_url", "")
        if not bing_url:
            continue
        b = base_stem(slug).lower()
        dest = folder / slug_filename(slug)

        if b in uhd_files:
            already_ok += 1
            already_ok_slugs.append((uhd_files[b], entry.get("date", "")))
        else:
            replace = other_files.get(b)  # non-UHD file to remove after download
            to_download.append((slug, entry, dest, replace))

    print(f"  {already_ok} already UHD  |  {len(to_download)} to download")

    if dry_run:
        print("\nDry run — files that would be downloaded:")
        for slug, entry, dest, replace in to_download[:30]:
            tag = f"replace {replace.name}" if replace else "new"
            print(f"  [{tag}]  {dest.name}  ({entry.get('date', '')})")
        if len(to_download) > 30:
            print(f"  … and {len(to_download) - 30} more")
        print(f"\n{already_ok} files already present as UHD (dates would NOT be changed).")
        return

    # Fix dates on existing UHD files if they look wrong
    # (only touch mtime if it differs from the expected date by more than a day)
    fixed_dates = 0
    for path, date_str in already_ok_slugs:
        if not date_str:
            continue
        expected_ts = datetime.strptime(date_str, "%Y-%m-%d").replace(
            hour=12, tzinfo=timezone.utc
        ).timestamp()
        current_mtime = path.stat().st_mtime
        if abs(current_mtime - expected_ts) > 86_400:  # off by more than a day
            set_file_date(str(path), date_str)
            fixed_dates += 1
    if fixed_dates:
        print(f"  Fixed dates on {fixed_dates} existing UHD files")

    # Download
    completed = 0
    failed = 0
    failed_slugs = []

    def _do(item):
        slug, entry, dest, replace = item
        primary, fallback = download_url(entry["bing_url"])
        ok = download_image(primary, fallback, str(dest), ssl_ctx)
        if ok:
            date_str = entry.get("date", "")
            if date_str:
                set_file_date(str(dest), date_str)
            if replace and replace.exists():
                replace.unlink()
        return ok, slug

    print(f"\nDownloading {len(to_download)} images (workers={workers})…")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_do, item): item for item in to_download}
        for fut in as_completed(futures):
            ok, slug = fut.result()
            if ok:
                completed += 1
            else:
                failed += 1
                failed_slugs.append(slug)
            done = completed + failed
            if done % 50 == 0 or done == len(to_download):
                print(f"  {done}/{len(to_download)}  ({completed} ok, {failed} failed)")

    print(f"\nDone: {completed} downloaded, {failed} failed")
    if failed_slugs:
        print("Failed slugs:")
        for s in failed_slugs:
            print(f"  {s}")


if __name__ == "__main__":
    main()
