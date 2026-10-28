# Grab

A private, local YouTube downloader. No ads, no pop-ups, no redirects: paste a link and the file lands in your folder.

**Start it:** double-click `Grab.bat`. Your browser opens at http://127.0.0.1:8765. Close the console window to quit.

## What it does
- **Single videos**: MP4 / WEBM (Best, 4K…360p) or audio as MP3 (320–128 kbps), M4A, WAV.
- **Whole playlists**: lists every video with checkboxes. Deleted, private and members-only videos are marked and skipped automatically. Anything that fails while downloading is marked failed with the reason and can be retried, and the rest keep going.
- Playlists go into their own subfolder, numbered in playlist order (you can turn numbering off).
- A watch link that also has `&list=` gives you a one-click switch between "just this video" and "the whole playlist".
- "Already downloaded" detection: when you re-paste a playlist, videos you already have in that format are unchecked, so you only fetch the new ones.
- MP4 prefers H.264 + AAC so files play everywhere (Windows Photos, phones, TVs). Cover art, title and artist are embedded.
- Three downloads run at once, with live speed, ETA, cancel, retry and "show in folder".
- Press Ctrl+V anywhere on the page and the link is fetched instantly.

## Files
- `app.py`: the local server (Python + yt-dlp). It only listens on 127.0.0.1, so it can't be reached from other devices.
- `static/index.html`: the UI.
- `data/`: your settings and download history.

The launcher updates yt-dlp on every start. If downloads ever start failing, restarting Grab is usually the fix.
