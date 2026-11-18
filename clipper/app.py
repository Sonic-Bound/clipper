"""Local web app for the clipper: python -m clipper.app  (opens http://127.0.0.1:8765)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from .runtime import FROZEN, SOURCE_ROOT, clips_dir, module_cmd
from .youtube import PRIVACY, AuthError, Uploader, YouTubeAccount

CLIPS = clips_dir()
WEB = Path(__file__).resolve().parent / "web"
PORT = int(os.environ.get("CLIPPER_PORT", "8765"))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console windows popping up from the desktop app

app = FastAPI(title="Clipper")


def _applog(msg: str) -> None:
    log_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Clipper"
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / "clipper.log", "a", encoding="utf-8") as fh:
        fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


YT = YouTubeAccount()
UPLOADER = Uploader(YT, lambda: CLIPS, log=lambda m: _applog("youtube: " + m))


@app.on_event("startup")
def _start_uploader() -> None:
    UPLOADER.start()


class JobIn(BaseModel):
    url: str
    top: int = 5
    vertical: bool = True
    captions: bool = True
    model: str = "small"
    language: str = ""
    threshold: float = 3.5
    min_jump: float = 2.0
    pre: float = 25
    post: float = 10
    max_minutes: float = 0
    upload: bool = False  # publish each clip to YouTube as soon as it is made


class Job:
    def __init__(self, spec: JobIn):
        self.id = uuid.uuid4().hex[:8]
        self.spec = spec
        self.title = ""
        self.kind = "starting"   # starting | video | live
        self.status = "running"  # running | stopping | done | failed | stopped
        self.lines: list[str] = []
        self.clips = 0
        self.progress = ""       # "2/5" for videos
        self.chat_rate = ""      # "23 msgs / 30s" for live
        self.started = time.time()
        self.ended: float | None = None
        self.stop_file = Path(tempfile.gettempdir()) / f"clipper-stop-{self.id}"
        self.proc: subprocess.Popen | None = None

    def view(self, full_log: bool = False) -> dict:
        return {
            "id": self.id, "url": self.spec.url, "title": self.title, "kind": self.kind,
            "status": self.status, "clips": self.clips, "progress": self.progress,
            "chat_rate": self.chat_rate, "started": self.started, "ended": self.ended,
            "log": self.lines if full_log else self.lines[-60:],
        }


JOBS: dict[str, Job] = {}

_PATTERNS = [
    (re.compile(r"^Video: (.*) \(\d+h\d+m\d+s\)$"), "video"),
    (re.compile(r"^Recording live stream: (.*)$"), "live"),
]


def _ingest(job: Job, line: str) -> None:
    line = line.rstrip()
    if not line or "HF_TOKEN" in line or "unauthenticated requests to the HF Hub" in line:
        return
    job.lines.append(line)
    if len(job.lines) > 2000:
        del job.lines[:500]
    for rx, kind in _PATTERNS:
        m = rx.match(line)
        if m:
            job.title, job.kind = m.group(1), kind
    s = line.strip()
    if s.startswith("✔"):
        job.clips += 1
        # videos print "✔ <path>", live streams "✔ saved <path>"
        m = re.match(r"^✔\s+(?:saved\s+)?(.+\.mp4)$", s)
        if job.spec.upload and m:
            clip = Path(m.group(1).strip())
            UPLOADER.enqueue(clip.parent.name, clip.name)
    m = re.match(r"^\[(\d+)/(\d+)\]", s)
    if m:
        job.progress = f"{m.group(1)}/{m.group(2)}"
    m = re.search(r"chat: (\d+) msgs in last 30s", s)
    if m:
        job.chat_rate = f"{m.group(1)} msgs / 30 s"


def _run(job: Job) -> None:
    sp = job.spec
    cmd = [*module_cmd("clipper"), sp.url, "-o", str(CLIPS), "-n", str(sp.top),
           "--model", sp.model, "--threshold", str(sp.threshold), "--min-jump", str(sp.min_jump), "--pre", str(sp.pre), "--post", str(sp.post),
           "--stop-file", str(job.stop_file)]
    if not sp.vertical:
        cmd.append("--horizontal")
    if not sp.captions:
        cmd.append("--no-captions")
    if sp.language.strip():
        cmd += ["--language", sp.language.strip()]
    if sp.max_minutes > 0:
        cmd += ["--max-minutes", str(sp.max_minutes)]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", HF_HUB_DISABLE_PROGRESS_BARS="1")
    flags = (subprocess.CREATE_NEW_PROCESS_GROUP | NO_WINDOW) if os.name == "nt" else 0
    CLIPS.mkdir(parents=True, exist_ok=True)
    job.proc = subprocess.Popen(cmd, cwd=CLIPS if FROZEN else SOURCE_ROOT,  # from source, `-m clipper` must run from the project folder
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", env=env, creationflags=flags)
    for line in job.proc.stdout:
        _ingest(job, line)
    code = job.proc.wait()
    job.ended = time.time()
    if job.status == "stopping":
        job.status = "stopped"
    else:
        job.status = "done" if code == 0 else "failed"
    job.stop_file.unlink(missing_ok=True)


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
    else:
        os.killpg(pid, 9)


# ------------------------------------------------------------------ API

@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB / "index.html").read_text(encoding="utf-8")


@app.post("/api/jobs")
def start_job(spec: JobIn):
    if not re.match(r"^https?://(www\.|m\.|music\.)?(youtube\.com|youtu\.be)/", spec.url.strip()):
        raise HTTPException(400, "Paste a YouTube link (youtube.com or youtu.be).")
    spec.url = spec.url.strip()
    job = Job(spec)
    JOBS[job.id] = job
    threading.Thread(target=_run, args=(job,), daemon=True).start()
    return job.view()


@app.get("/api/jobs")
def list_jobs():
    return [j.view() for j in sorted(JOBS.values(), key=lambda j: j.started, reverse=True)]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = JOBS.get(job_id) or _404()
    return job.view(full_log=True)


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str):
    job = JOBS.get(job_id) or _404()
    if job.status != "running" or not job.proc:
        return job.view()
    job.status = "stopping"
    job.stop_file.touch()  # live: finish clips in progress, then exit
    if job.kind != "live":
        _kill_tree(job.proc.pid)
    else:
        def hard_stop():
            time.sleep(600)  # pending clips get 10 minutes to finish
            if job.proc.poll() is None:
                _kill_tree(job.proc.pid)
        threading.Thread(target=hard_stop, daemon=True).start()
    return job.view()


@app.post("/api/jobs/{job_id}/dismiss")
def dismiss_job(job_id: str):
    job = JOBS.get(job_id) or _404()
    if job.status in ("running", "stopping"):
        raise HTTPException(409, "Stop the job first.")
    del JOBS[job_id]
    return {"ok": True}


@app.get("/api/clips")
def list_clips():
    out = []
    if not CLIPS.exists():
        return out
    for folder in CLIPS.iterdir():
        if not folder.is_dir():
            continue
        meta = {}
        mf = folder / "clips.json"
        if mf.exists():
            try:
                meta = {m["file"]: m for m in json.loads(mf.read_text(encoding="utf-8"))}
            except Exception:  # noqa: BLE001
                meta = {}
        vid = re.search(r"\[([\w-]{11})\]$", folder.name)
        title = re.sub(r"\s*\[[\w-]{11}\]$", "", folder.name)
        for f in folder.iterdir():
            if f.suffix != ".mp4" or not f.name.startswith("clip_"):
                continue
            m = meta.get(f.name, {})
            moment = re.search(r"(\d+)h(\d+)m(\d+)s", f.stem)
            out.append({
                "folder": folder.name, "title": title, "file": f.name,
                # quote(): titles can contain '#', '?' or '%', which would otherwise break the address
                "src": f"/media/{quote(folder.name, safe='')}/{quote(f.name, safe='')}",
                "srt": (f"/media/{quote(folder.name, safe='')}/{quote(f.with_suffix('.srt').name, safe='')}"
                        if f.with_suffix(".srt").exists() else None),
                "moment": f"{int(moment[1])}:{moment[2]}:{moment[3]}" if moment and int(moment[1]) else
                          (f"{int(moment[2])}:{moment[3]}" if moment else ""),
                "score": m.get("score"), "source": m.get("source", "live chat" if "stream_time" in m else ""),
                "youtube": m.get("url") or (f"https://youtu.be/{vid[1]}" if vid else None),
                "size": f.stat().st_size, "created": f.stat().st_mtime,
                "live": "stream_time" in m,
                "upload": _upload_view(folder.name, f.name),
            })
    out.sort(key=lambda c: c["created"], reverse=True)
    return out


@app.get("/media/{folder}/{name}")
def media(folder: str, name: str):
    path = (CLIPS / folder / name).resolve()
    if CLIPS.resolve() not in path.parents or not path.is_file():
        _404()
    return FileResponse(path)


class FolderIn(BaseModel):
    folder: str = ""
    file: str = ""


@app.post("/api/reveal")
def reveal(body: FolderIn):
    """Open the clip's folder in Explorer (with the file selected)."""
    target = (CLIPS / body.folder / body.file).resolve() if body.folder else CLIPS.resolve()
    if target != CLIPS.resolve() and CLIPS.resolve() not in target.parents:
        _404()
    CLIPS.mkdir(exist_ok=True)
    if os.name == "nt":
        if target.is_file():
            subprocess.Popen(["explorer", "/select,", str(target)])
        else:
            os.startfile(target)  # noqa: S606
    return {"ok": True}


@app.post("/api/clips/delete")
def delete_clip(body: FolderIn):
    """Send a clip (and its caption files) to the Recycle Bin is not available from Python
    without extra deps, so this moves them into clips/_deleted instead (recoverable)."""
    folder = (CLIPS / body.folder).resolve()
    if CLIPS.resolve() not in folder.parents or not (folder / body.file).is_file():
        _404()
    trash = CLIPS / "_deleted" / body.folder
    trash.mkdir(parents=True, exist_ok=True)
    stem = Path(body.file).stem
    UPLOADER.cancel(body.folder, body.file)
    for ext in (".mp4", ".srt", ".ass"):
        f = folder / (stem + ext)
        if f.exists():
            f.replace(trash / f.name)
    return {"ok": True}


def _upload_view(folder: str, file: str) -> dict | None:
    u = UPLOADER.get(folder, file)
    if not u:
        return None
    return {k: u.get(k) for k in ("status", "url", "error", "progress", "privacy")}


# ------------------------------------------------------------------ YouTube

class YTClientIn(BaseModel):
    client_id: str = ""
    client_secret: str = ""
    client_json: str = ""


class YTSettingsIn(BaseModel):
    privacy: str = "private"


@app.get("/api/youtube")
def youtube_status():
    return {**YT.status(), **UPLOADER.summary()}


@app.post("/api/youtube/client")
def youtube_client(body: YTClientIn):
    try:
        YT.set_client(body.client_id, body.client_secret, body.client_json)
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(400, str(e)) from e
    return youtube_status()


@app.post("/api/youtube/connect")
def youtube_connect(request: Request):
    try:
        url = YT.auth_url(f"http://127.0.0.1:{request.url.port}/oauth/callback")
    except AuthError as e:
        raise HTTPException(400, str(e)) from e
    webbrowser.open(url)  # sign in happens in the user's normal browser
    return {"url": url}


_CALLBACK_PAGE = """<!doctype html><meta charset="utf-8"><title>Clipper</title>
<body style="font:16px 'Segoe UI',sans-serif;background:#0d0f14;color:#e9ecf2;display:grid;place-items:center;height:90vh">
<div style="text-align:center;max-width:520px"><div style="font-size:42px">{icon}</div><h2>{head}</h2>
<p style="color:#8c93a4">{text}</p></div></body>"""


@app.get("/oauth/callback", response_class=HTMLResponse)
def oauth_callback(code: str = "", state: str = "", error: str = ""):
    from html import escape

    if error or not code:
        msg = "You cancelled the sign-in." if error == "access_denied" else f"Google said: {error or 'no code'}"
        return _CALLBACK_PAGE.format(icon="✕", head="YouTube was not connected", text=escape(msg))
    try:
        channel = YT.finish_auth(code, state)
    except Exception as e:  # noqa: BLE001
        _applog(f"youtube: sign-in failed: {e}")
        return _CALLBACK_PAGE.format(icon="✕", head="YouTube was not connected", text=escape(str(e)))
    UPLOADER.resume()
    _applog(f"youtube: connected to channel {channel!r}")
    return _CALLBACK_PAGE.format(icon="✓", head=f"Connected to {escape(channel)}",
                                 text="You can close this tab and go back to Clipper.")


@app.post("/api/youtube/settings")
def youtube_settings(body: YTSettingsIn):
    try:
        YT.set_privacy(body.privacy)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return youtube_status()


@app.post("/api/youtube/disconnect")
def youtube_disconnect():
    YT.disconnect()
    return youtube_status()


@app.post("/api/youtube/upload")
def youtube_upload(body: FolderIn):
    if not (CLIPS / body.folder / body.file).is_file():
        _404()
    u = UPLOADER.enqueue(body.folder, body.file, force=True)
    if UPLOADER.paused_until and "sign-in" not in UPLOADER.pause_reason and "limit" not in UPLOADER.pause_reason:
        UPLOADER.resume()
    return _upload_view(body.folder, body.file) or u


@app.post("/api/youtube/cancel")
def youtube_cancel(body: FolderIn):
    UPLOADER.cancel(body.folder, body.file)
    return {"ok": True}


class ClientLog(BaseModel):
    msg: str


@app.post("/api/client-log")
def client_log(body: ClientLog):
    """The page reports problems (e.g. a clip that will not play) so they end up in the app log."""
    log_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Clipper"
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / "clipper.log", "a", encoding="utf-8") as fh:
        fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + "page: " + body.msg[:2000] + "\n")
    return {"ok": True}


def running_jobs() -> list[Job]:
    return [j for j in JOBS.values() if j.status in ("running", "stopping")]


def shutdown_jobs() -> None:
    """Kill every job's process tree (used when the app window closes)."""
    for job in running_jobs():
        if job.proc and job.proc.poll() is None:
            _kill_tree(job.proc.pid)


def free_port(start: int = PORT) -> int:
    import socket

    for candidate in range(start, start + 50):  # another program may already use the port
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", candidate)) != 0:
                return candidate
    raise RuntimeError("no free port")


def make_server(port: int):
    import uvicorn

    CLIPS.mkdir(exist_ok=True)
    # log_config=None: under pythonw there is no console for uvicorn's default log handlers
    return uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, log_level="warning"))


def _404():
    raise HTTPException(404, "Not found")


def main() -> None:
    """Browser version: python -m clipper.app"""
    port = free_port(int(os.environ.get("PORT", PORT)))
    url = f"http://127.0.0.1:{port}"
    print(f"Clipper is running at {url}  (close this window to quit)")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    make_server(port).run()


if __name__ == "__main__":
    main()
