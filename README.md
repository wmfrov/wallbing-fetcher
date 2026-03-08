# wallbing-fetcher

Downloads the latest Bing images of the day to a local folder for use as macOS desktop wallpapers. Fetches the last 8 images and saves any that are missing to `~/Pictures/bingimages` (or a custom directory). Maintains an `images.txt` manifest in the repo.

## Setup

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python fetch_weekly.py
```

Optional: set `WALLPAPER_DIR` to save images somewhere other than `~/Pictures/bingimages`.

## Weekly updates (launchd)

To run automatically every Monday at 9:00 AM:

1. Create the venv (see Setup above) so the job has `requests` available.
2. Copy the plist and set `WorkingDirectory` and the path inside `ProgramArguments` to your repo path (e.g. `/Users/you/projects/wallbing-fetcher` and `.../wallbing-fetcher/venv/bin/python`).
3. Install: `cp com.wallbing.weekly.plist ~/Library/LaunchAgents/` then `launchctl load ~/Library/LaunchAgents/com.wallbing.weekly.plist`.

Logs: `/tmp/wallbing.log` and `/tmp/wallbing.err`.

## Browsable gallery

For a searchable, filterable gallery of Bing images of the day hosted on GitHub Pages, see [wallbing](https://github.com/wmfrov/wallbing).

## License

[MIT License](LICENSE)
