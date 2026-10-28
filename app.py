"""Grab — a small, local YouTube downloader (yt-dlp + ffmpeg) with a web UI.

Run:  .venv\\Scripts\\python app.py      → opens http://127.0.0.1:8765
"""

import glob
import re
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp
from yt_dlp.utils import sanitize_filename

HOST, PORT = "127.0.0.1", 8765
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
SETTINGS_FILE = DATA / "settings.json"
HISTORY_FILE = DATA / "history.json"

UNAVAILABLE_TITLES = {"[deleted video]", "[private video]", "[unavailable video]"}

FORMATS = {
    # key: (kind, label)
    "mp4": ("video", "MP4"),
    "webm": ("video", "WEBM"),
    "mp3": ("audio", "MP3"),
    "m4a": ("audio", "M4A"),
    "wav": ("audio", "WAV"),
}


def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return str(Path(exe).parent)
    local = os.environ.get("LOCALAPPDATA", "")
    hits = glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "*", "bin", "ffmpeg.exe"))
    return str(Path(hits[0]).parent) if hits else None


FFMPEG_DIR = find_ffmpeg()


# ── persistence ──────────────────────────────────────────────────────────────

_lock = threading.RLock()


def load_json(path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def save_json(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), "utf-8")
    tmp.replace(path)


settings = {
    "folder": str(Path.home() / "Downloads" / "Grab"),
    "format": "mp4",
    "quality": "1080",
    "audio_quality": "320",
    "embed_metadata": True,
    "concurrency": 3,
    **load_json(SETTINGS_FILE, {}),
}
history = load_json(HISTORY_FILE, [])  # [{id, title, format, path, at}]


def downloaded_keys():
    with _lock:
        return {(h["id"], h["format"]) for h in history if h.get("path") and os.path.exists(h["path"])}


# ── yt-dlp helpers ───────────────────────────────────────────────────────────

class Cancelled(Exception):
    pass


class QuietLogger:
    def __init__(self, job=None):
        self.job = job

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        if self.job is not None:
            self.job["error"] = clean_error(msg)


def clean_error(msg):
    msg = str(msg).replace("ERROR: ", "")
    if "] " in msg and msg.startswith("["):
        msg = msg.split("] ", 1)[1]
    if ":" in msg and len(msg.split(":", 1)[0]) == 11:  # "<video id>: reason"
        msg = msg.split(":", 1)[1].strip()
    for marker in ("Private video", "Video unavailable", "This video is not available", "Sign in to confirm your age",
                   "members-only", "This video has been removed", "copyright"):
        if marker.lower() in msg.lower():
            return msg.split(". ")[0].strip().rstrip(".")
    return msg[:200]


def base_opts(job=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": QuietLogger(job),
        "js_runtimes": {"node": {}, "deno": {}},
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
    }
    if FFMPEG_DIR:
        opts["ffmpeg_location"] = FFMPEG_DIR
    return opts


def thumb_of(info):
    thumbs = info.get("thumbnails") or []
    if info.get("thumbnail"):
        return info["thumbnail"]
    if thumbs:
        return thumbs[-1].get("url")
    vid = info.get("id")
    return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else None


def entry_unavailable(e):
    title = (e.get("title") or "").strip().lower()
    if title in UNAVAILABLE_TITLES:
        return "Deleted" if "deleted" in title else ("Private" if "private" in title else "Unavailable")
    if e.get("availability") in ("private", "needs_auth", "subscriber_only", "premium_only"):
        return {"private": "Private", "needs_auth": "Sign-in required",
                "subscriber_only": "Members only", "premium_only": "Premium only"}[e["availability"]]
    return None


def analyze(url, mode):
    """mode: 'auto' | 'video' | 'playlist'. Returns a preview dict."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    has_list = "list" in qs
    has_video = "v" in qs or "youtu.be" in parsed.netloc or "/shorts/" in parsed.path

    target = url
    if mode == "video" and has_video and has_list:
        target = f"https://www.youtube.com/watch?v={qs['v'][0]}" if "v" in qs else url.split("?")[0]
    elif mode == "playlist" and has_list:
        target = f"https://www.youtube.com/playlist?list={qs['list'][0]}"
    elif mode == "auto" and has_list and not has_video:
        target = f"https://www.youtube.com/playlist?list={qs['list'][0]}"

    opts = {**base_opts(), "extract_flat": "in_playlist", "skip_download": True, "noplaylist": mode != "playlist",
            "ignoreerrors": True}
    if mode == "auto" and has_video:
        opts["noplaylist"] = True

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)
    if not info:
        raise ValueError("Couldn't read that link. Check that it's a valid, public YouTube URL.")

    done = downloaded_keys()
    if info.get("_type") == "playlist" or info.get("entries") is not None:
        entries = []
        for i, e in enumerate(info.get("entries") or [], 1):
            if not e:
                entries.append({"index": i, "id": None, "title": "Unavailable video", "unavailable": "Unavailable"})
                continue
            vid = e.get("id")
            entries.append({
                "index": i,
                "id": vid,
                "url": e.get("url") or f"https://www.youtube.com/watch?v={vid}",
                "title": e.get("title") or vid,
                "channel": e.get("channel") or e.get("uploader"),
                "duration": e.get("duration"),
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else None,
                "unavailable": entry_unavailable(e),
                "downloaded": sorted(f for (i_, f) in done if i_ == vid),
            })
        first = next((e for e in entries if not e.get("unavailable") and e.get("thumbnail")), None)
        return {
            "type": "playlist",
            "id": info.get("id"),
            "title": info.get("title") or "Playlist",
            "channel": info.get("channel") or info.get("uploader"),
            "thumbnail": first["thumbnail"] if first else None,
            "entries": entries,
            "has_video": has_video,
        }

    return {
        "type": "video",
        "id": info.get("id"),
        "url": info.get("webpage_url") or target,
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "duration": info.get("duration"),
        "thumbnail": thumb_of(info),
        "max_height": max((f.get("height") or 0 for f in info.get("formats") or []), default=0),
        "downloaded": sorted(f for (i_, f) in done if i_ == info.get("id")),
        "has_list": has_list,
    }


def format_opts(fmt, quality, audio_quality, embed):
    pps = []
    o = {}
    if fmt in ("mp4", "webm"):
        res = [] if quality == "best" else [f"res:{quality}"]
        if fmt == "mp4":
            o["format"] = "bv*+ba/b"
            o["format_sort"] = res + ["vcodec:h264", "ext:mp4:m4a"]
            o["merge_output_format"] = "mp4"
        else:
            o["format"] = "bv*[ext=webm]+ba[ext=webm]/bv*+ba/b"
            o["format_sort"] = res
            o["merge_output_format"] = "webm"
    else:
        o["format"] = "ba/b"
        pp = {"key": "FFmpegExtractAudio", "preferredcodec": fmt}
        if fmt == "mp3":
            pp["preferredquality"] = audio_quality
        pps.append(pp)
    if embed:
        pps.append({"key": "FFmpegMetadata", "add_metadata": True})
        if fmt in ("mp4", "mp3", "m4a"):
            o["writethumbnail"] = True
            pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
            pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
    o["postprocessors"] = pps
    return o


# ── jobs ─────────────────────────────────────────────────────────────────────

jobs = {}      # id -> job dict
groups = {}    # id -> group dict (playlist batch)
order = []     # newest first: ("job"|"group", id)
pool = ThreadPoolExecutor(max_workers=int(settings.get("concurrency", 3)))


def new_job(url, vid, title, thumbnail, fmt, folder, prefix="", group=None):
    job = {
        "id": uuid.uuid4().hex[:10], "video_id": vid, "url": url, "title": title, "thumbnail": thumbnail,
        "format": fmt, "folder": folder, "prefix": prefix, "group": group,
        "status": "queued", "progress": 0.0, "speed": None, "eta": None, "size": None,
        "error": None, "path": None, "cancel": False, "created": time.time(), "_opts": None,
    }
    jobs[job["id"]] = job
    return job


def run_job(job, opts_snapshot):
    job["_opts"] = opts_snapshot
    if job["cancel"]:
        job["status"] = "cancelled"
        return
    job["status"] = "downloading"
    final = {}

    def progress(d):
        if job["cancel"]:
            raise Cancelled()
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            # Video+audio are two downloads; weight video stream heavier.
            part = d.get("info_dict", {}).get("format_id", "")
            pct = (done / total) if total else 0
            job["_parts"] = job.get("_parts", {})
            job["_parts"][part] = (done, total or 0)
            all_done = sum(a for a, _ in job["_parts"].values())
            all_total = sum(b for _, b in job["_parts"].values())
            job["progress"] = min(0.99, all_done / all_total) if all_total else pct
            job["speed"] = d.get("speed")
            job["eta"] = d.get("eta")
            job["size"] = all_total or None
        elif d["status"] == "finished":
            job["status"] = "processing"

    def post(filepath):
        final["path"] = filepath

    def pp_hook(d):
        if d.get("status") == "started" and d.get("postprocessor") in ("ExtractAudio", "Merger", "EmbedThumbnail", "FFmpegMetadata"):
            job["status"] = "processing"

    safe_title = sanitize_filename(job["title"] or "%(title)s").replace("%", "%%") if job["prefix"] else "%(title)s"
    outtmpl = os.path.join(job["folder"], f"{job['prefix']}{safe_title}.%(ext)s")
    opts = {
        **base_opts(job),
        **format_opts(job["format"], *opts_snapshot),
        "outtmpl": {"default": outtmpl, "thumbnail": outtmpl},
        "noplaylist": True,
        "progress_hooks": [progress],
        "postprocessor_hooks": [pp_hook],
        "post_hooks": [post],
        "windowsfilenames": True,
        "overwrites": False,
    }
    try:
        os.makedirs(job["folder"], exist_ok=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            code = ydl.download([job["url"]])
        if job["cancel"]:
            job["status"] = "cancelled"
        elif code == 0 and final.get("path"):
            job.update(status="done", progress=1.0, path=final["path"], speed=None, eta=None)
            try:
                job["size"] = os.path.getsize(final["path"])
            except OSError:
                pass
            with _lock:
                history.insert(0, {"id": job["video_id"], "title": job["title"], "format": job["format"],
                                   "path": final["path"], "at": time.time()})
                del history[2000:]
                save_json(HISTORY_FILE, history)
        else:
            job["status"] = "failed"
            job["error"] = job["error"] or "Download failed"
    except Cancelled:
        job["status"] = "cancelled"
    except Exception as e:  # yt-dlp DownloadError etc.
        job["status"] = "failed"
        job["error"] = job["error"] or clean_error(e)
    finally:
        job.pop("_parts", None)
        if job["status"] == "cancelled":
            cleanup_partials(job)


def cleanup_partials(job):
    title = sanitize_filename(job["title"] or "")
    if not title:
        return
    for p in glob.glob(os.path.join(glob.escape(job["folder"]), f"{glob.escape(job['prefix'] + title)}*")):
        if p.endswith((".part", ".ytdl")) or ".part-Frag" in p or re.search(r"\.f\d+\.\w+$", p):
            try:
                os.remove(p)
            except OSError:
                pass


def enqueue(payload):
    fmt = payload.get("format", "mp4")
    if fmt not in FORMATS:
        raise ValueError("Unknown format")
    snap = (payload.get("quality", "1080"), payload.get("audio_quality", "320"), bool(payload.get("embed_metadata", True)))
    folder = payload.get("folder") or settings["folder"]

    if payload.get("type") == "playlist":
        gid = uuid.uuid4().hex[:10]
        sub = os.path.join(folder, sanitize_filename(payload.get("title") or "Playlist", restricted=False))
        selected = payload.get("entries", [])
        width = max(2, len(str(payload.get("total", len(selected)))))
        group = {"id": gid, "title": payload.get("title"), "thumbnail": payload.get("thumbnail"), "format": fmt,
                 "folder": sub, "jobs": [], "skipped": payload.get("skipped", []), "created": time.time()}
        groups[gid] = group
        for e in selected:
            prefix = f"{str(e['index']).zfill(width)} - " if payload.get("numbered", True) else ""
            j = new_job(e["url"], e["id"], e["title"], e.get("thumbnail"), fmt, sub, prefix, gid)
            group["jobs"].append(j["id"])
            pool.submit(run_job, j, snap)
        order.insert(0, ("group", gid))
        return {"group": gid}

    j = new_job(payload["url"], payload.get("id"), payload.get("title"), payload.get("thumbnail"), fmt, folder)
    pool.submit(run_job, j, snap)
    order.insert(0, ("job", j["id"]))
    return {"job": j["id"]}


def public_job(j):
    return {k: v for k, v in j.items() if not k.startswith("_") and k != "cancel"}


def snapshot():
    out = []
    for kind, oid in order[:200]:
        if kind == "job" and oid in jobs:
            out.append({"kind": "job", **public_job(jobs[oid])})
        elif kind == "group" and oid in groups:
            g = groups[oid]
            items = [public_job(jobs[j]) for j in g["jobs"] if j in jobs]
            out.append({"kind": "group", **{k: v for k, v in g.items() if k != "jobs"}, "items": items})
    return out


def reveal(path):
    if not path:
        return
    if sys.platform == "win32":
        if os.path.isfile(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            os.makedirs(path, exist_ok=True)
            os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R" if os.path.isfile(path) else "", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path) if os.path.isfile(path) else path])


def pick_folder(initial):
    code = (
        "import sys, tkinter as tk\nfrom tkinter import filedialog\n"
        "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
        "p = filedialog.askdirectory(initialdir=sys.argv[1], title='Choose download folder')\n"
        "print(p or '', end='')\n"
    )
    res = subprocess.run([sys.executable, "-c", code, initial], capture_output=True, text=True, timeout=600)
    return os.path.normpath(res.stdout) if res.stdout.strip() else None


# ── HTTP ─────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self.send(200, (ROOT / "static" / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/state":
            return self.send(200, {"items": snapshot(), "settings": settings, "ffmpeg": bool(FFMPEG_DIR)})
        return self.send(404, {"error": "not found"})

    def do_POST(self):
        # Only accept requests from our own page (blocks drive-by requests from other sites).
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"):
            return self.send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        try:
            p = self.body()
            if path == "/api/analyze":
                return self.send(200, analyze(p["url"].strip(), p.get("mode", "auto")))
            if path == "/api/download":
                return self.send(200, enqueue(p))
            if path == "/api/cancel":
                ids = [p["id"]] if p.get("id") in jobs else groups.get(p.get("id"), {}).get("jobs", [])
                for jid in ids:
                    if jobs[jid]["status"] in ("queued", "downloading", "processing"):
                        jobs[jid]["cancel"] = True
                        if jobs[jid]["status"] == "queued":
                            jobs[jid]["status"] = "cancelled"
                return self.send(200, {"ok": True})
            if path == "/api/retry":
                j = jobs.get(p["id"])
                if j and j["status"] in ("failed", "cancelled"):
                    j.update(status="queued", cancel=False, error=None, progress=0.0)
                    pool.submit(run_job, j, j["_opts"])
                return self.send(200, {"ok": True})
            if path == "/api/clear":
                for kind, oid in list(order):
                    ids = [oid] if kind == "job" else groups[oid]["jobs"]
                    if all(jobs[i]["status"] in ("done", "failed", "cancelled") for i in ids):
                        order.remove((kind, oid))
                return self.send(200, {"ok": True})
            if path == "/api/reveal":
                reveal(p.get("path") or settings["folder"])
                return self.send(200, {"ok": True})
            if path == "/api/pick-folder":
                chosen = pick_folder(settings["folder"])
                if chosen:
                    settings["folder"] = chosen
                    save_json(SETTINGS_FILE, settings)
                return self.send(200, {"folder": settings["folder"]})
            if path == "/api/settings":
                for k in ("folder", "format", "quality", "audio_quality", "embed_metadata"):
                    if k in p:
                        settings[k] = p[k]
                save_json(SETTINGS_FILE, settings)
                return self.send(200, settings)
            return self.send(404, {"error": "not found"})
        except yt_dlp.utils.DownloadError as e:
            return self.send(400, {"error": clean_error(e)})
        except Exception as e:
            return self.send(400, {"error": str(e)})


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"Grab is running at {url}  (Ctrl+C to quit)")
    if FFMPEG_DIR is None:
        print("WARNING: ffmpeg not found — MP3 conversion and HD merging won't work.")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
