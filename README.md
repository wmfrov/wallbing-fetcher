# wallbing-fetcher

Downloads the latest Bing images of the day to a local folder for use as macOS desktop wallpapers. Fetches the last 8 images and saves any that are missing to `~/Pictures/bingimages` (or a custom directory). Maintains an `images.txt` manifest in the repo.

## Setup

```bash
pip install -r requirements.txt
python3 fetch_weekly.py
```

Optional: set `WALLPAPER_DIR` to save images somewhere other than `~/Pictures/bingimages`.

## Weekly updates (launchd)

To run automatically every Monday at 9:00 AM:

1. Copy the plist and fix the path in `ProgramArguments` to match where you cloned this repo (e.g. `$HOME/projects/wallbing-fetcher`).
2. Install: `cp com.wallbing.weekly.plist ~/Library/LaunchAgents/` then `launchctl load ~/Library/LaunchAgents/com.wallbing.weekly.plist`.

Logs: `/tmp/wallbing.log` and `/tmp/wallbing.err`.

## Browsable gallery

For a searchable, filterable gallery of Bing images of the day hosted on GitHub Pages, see [wallbing](https://github.com/wmfrov/wallbing).

## License

[MIT License](LICENSE)
