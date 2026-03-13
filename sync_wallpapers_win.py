#!/usr/bin/env python3
"""
Sync a local folder of Bing wallpapers against metadata.json from gh-pages.

  - Downloads UHD versions of images missing from the folder
  - Replaces wrong-resolution versions with UHD (deletes the old file)
  - Sets mtime (and Windows creation date) to the image's Bing publish date

Usage:
  python sync_wallpapers_win.py C:\\Users\\You\\Pictures\\bingimages
  python sync_wallpapers_win.py C:\\Users\\You\\Pictures\\bingimages --dry-run
  python sync_wallpapers_win.py C:\\Users\\You\\Pictures\\bingimages --workers 16
"""

import ctypes
import ctypes.wintypes
import json
import os
import re
import ssl
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

METADATA_URL = "https://wmfrov.github.io/wallbing/metadata.json"
DEFAULT_WORKERS = 8
USER_AGENT = "wallbing-sync/1.0"

# Matches _UHD or _1920x1080 etc. at the end of a stem
RES_RE = re.compile(r"[_-](UHD|\d{3,5}x\d{3,5})$", re.IGNORECASE)


# ── Metadata ──────────────────────────────────────────────────────────────

def load_metadata():
    ssl_ctx = build_ssl_ctx()
    try:
        data = fetch_url(METADATA_URL, ssl_ctx, timeout=30)
        return json.loads(data)
    except Exception as e:
        print(f"ERROR: could not fetch metadata from {METADATA_URL}: {e}", file=sys.stderr)
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

def _datetime_to_filetime(dt):
    """Convert a datetime to a Windows FILETIME (100-ns intervals since 1601-01-01)."""
    EPOCH_DIFF = 116444736000000000  # 100-ns intervals between 1601-01-01 and 1970-01-01
    timestamp_ns = int(dt.timestamp() * 10_000_000) + EPOCH_DIFF
    filetime = ctypes.wintypes.FILETIME()
    filetime.dwLowDateTime = timestamp_ns & 0xFFFFFFFF
    filetime.dwHighDateTime = (timestamp_ns >> 32) & 0xFFFFFFFF
    return filetime


def set_file_date(path: str, date_str: str):
    """Set mtime and Windows creation date to noon UTC on date_str (YYYY-MM-DD)."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
            hour=12, tzinfo=timezone.utc
        )
        ts = dt.timestamp()
        os.utime(path, (ts, ts))
    except Exception as e:
        print(f"  Warning: could not set mtime on {path}: {e}")
        return

    # Windows creation date via kernel32.SetFileTime
    try:
        kernel32 = ctypes.windll.kernel32

        GENERIC_WRITE = 0x40000000
        OPEN_EXISTING = 3
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
        FILE_ATTRIBUTE_NORMAL = 0x80

        handle = kernel32.CreateFileW(
            path,
            GENERIC_WRITE,
            0,      # no sharing
            None,   # default security
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL | FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if handle == ctypes.wintypes.HANDLE(-1).value:
            return

        ft = _datetime_to_filetime(dt)
        # SetFileTime(handle, lpCreationTime, lpLastAccessTime, lpLastWriteTime)
        kernel32.SetFileTime(
            handle,
            ctypes.byref(ft),   # creation time
            ctypes.byref(ft),   # last access time
            ctypes.byref(ft),   # last write time
        )
        kernel32.CloseHandle(handle)
    except Exception:
        pass  # non-Windows or permission error; mtime via os.utime is enough


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
