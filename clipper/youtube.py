"""Publishing clips to the user's own YouTube channel (YouTube Data API v3).

The user creates their own Google Cloud OAuth client (type "Desktop app") and
pastes its ID and secret into Clipper; nothing goes through a third party.
Sign-in uses the standard loopback flow with PKCE: Google redirects back to
Clipper's own local server (http://127.0.0.1:<port>/oauth/callback).

Uploads run one at a time in a background thread. Each clip's state lives in
%LOCALAPPDATA%\\Clipper\\uploads.json, so queued uploads survive an app restart.
When YouTube's daily quota runs out, the queue waits until the quota resets
(midnight Pacific time) and carries on.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Clipper"
CONFIG_FILE = DATA_DIR / "youtube.json"
UPLOADS_FILE = DATA_DIR / "uploads.json"

# overridable so the flow can be tested against a fake server
AUTH_URL = os.environ.get("CLIPPER_GOOGLE_AUTH", "https://accounts.google.com/o/oauth2/v2/auth")
TOKEN_URL = os.environ.get("CLIPPER_GOOGLE_TOKEN", "https://oauth2.googleapis.com/token")
REVOKE_URL = os.environ.get("CLIPPER_GOOGLE_REVOKE", "https://oauth2.googleapis.com/revoke")
API = os.environ.get("CLIPPER_YT_API", "https://www.googleapis.com").rstrip("/")
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
PRIVACY = ("private", "unlisted", "public")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class QuotaError(Exception):
    """YouTube's daily upload allowance is used up."""


class AuthError(Exception):
    """The YouTube connection is missing, expired or was revoked."""


# --------------------------------------------------------------- secret storage
# The refresh token is a long-lived key to the user's channel, so on Windows it is
# encrypted with DPAPI (only this Windows user on this PC can decrypt it).

def _protect(text: str) -> str:
    if os.name != "nt":
        return "plain:" + text
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    data = text.encode()
    src = BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("could not encrypt the YouTube token")
    try:
        return "dpapi:" + base64.b64encode(ctypes.string_at(out.pbData, out.cbData)).decode()
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _unprotect(value: str) -> str:
    if value.startswith("plain:"):
        return value[6:]
    if not value.startswith("dpapi:"):
        return ""
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    data = base64.b64decode(value[6:])
    src = BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        return ""
    try:
        return ctypes.string_at(out.pbData, out.cbData).decode()
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def next_quota_reset(now: float | None = None) -> float:
    """YouTube quota resets at midnight Pacific time; 08:00 UTC is that or an hour after."""
    now_dt = datetime.fromtimestamp(now or time.time(), timezone.utc)
    reset = now_dt.replace(hour=8, minute=0, second=0, microsecond=0)
    if reset <= now_dt:
        reset += timedelta(days=1)
    return reset.timestamp()


def _api_error(r: requests.Response) -> tuple[str, str]:
    """(reason, message) from a Google API error response."""
    try:
        err = r.json().get("error", {})
        if isinstance(err, str):  # OAuth endpoints: {"error": "invalid_grant", "error_description": ...}
            return err, r.json().get("error_description", err)
        reason = (err.get("errors") or [{}])[0].get("reason", "") or err.get("status", "")
        return reason, err.get("message", r.text[:200])
    except Exception:  # noqa: BLE001
        return "", r.text[:200] or f"HTTP {r.status_code}"


class _ProgressFile:
    """A file that reports how much of it has been read (for upload progress)."""

    def __init__(self, path: Path, progress=None):
        self.fh = open(path, "rb")
        self.size = path.stat().st_size
        self.sent = 0
        self.progress = progress

    def __len__(self) -> int:
        return self.size

    def read(self, n: int = -1) -> bytes:
        block = self.fh.read(n)
        self.sent += len(block)
        if self.progress and self.size:
            self.progress(self.sent / self.size)
        return block

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.fh.close()


# ------------------------------------------------------------------ account
class YouTubeAccount:
    def __init__(self):
        self.lock = threading.RLock()
        self.cfg = _read_json(CONFIG_FILE, {})
        self._access: tuple[str, float] | None = None  # (token, expires_at)
        self._pending: dict[str, tuple[str, str]] = {}  # state -> (code_verifier, redirect_uri)

    # -- config
    def _save(self) -> None:
        _write_json(CONFIG_FILE, self.cfg)

    @property
    def configured(self) -> bool:
        return bool(self.cfg.get("client_id") and self.cfg.get("client_secret"))

    @property
    def connected(self) -> bool:
        return bool(self.cfg.get("refresh_token"))

    @property
    def privacy(self) -> str:
        return self.cfg.get("privacy", "private")

    def status(self) -> dict:
        return {"configured": self.configured, "connected": self.connected,
                "channel": self.cfg.get("channel", ""), "channel_id": self.cfg.get("channel_id", ""),
                "privacy": self.privacy, "client_id": self.cfg.get("client_id", "")}

    def set_client(self, client_id: str = "", client_secret: str = "", client_json: str = "") -> None:
        """Accepts the ID + secret, or the whole client_secret_*.json downloaded from Google."""
        if client_json.strip():
            data = json.loads(client_json)
            data = data.get("installed") or data.get("web") or data
            client_id, client_secret = data.get("client_id", ""), data.get("client_secret", "")
        client_id, client_secret = client_id.strip(), client_secret.strip()
        if not client_id.endswith(".apps.googleusercontent.com") and "CLIPPER_GOOGLE_AUTH" not in os.environ:
            raise ValueError("That doesn't look like an OAuth client ID (it ends in .apps.googleusercontent.com).")
        if not client_secret:
            raise ValueError("The client secret is missing.")
        with self.lock:
            if client_id != self.cfg.get("client_id"):
                self.cfg.pop("refresh_token", None)  # a token belongs to one client
                self.cfg.pop("channel", None)
                self._access = None
            self.cfg.update(client_id=client_id, client_secret=client_secret)
            self._save()

    def set_privacy(self, privacy: str) -> None:
        if privacy not in PRIVACY:
            raise ValueError("privacy must be private, unlisted or public")
        with self.lock:
            self.cfg["privacy"] = privacy
            self._save()

    # -- sign-in (loopback + PKCE)
    def auth_url(self, redirect_uri: str) -> str:
        if not self.configured:
            raise AuthError("Add your Google OAuth client ID and secret first.")
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        self._pending = {state: (verifier, redirect_uri)}  # only the latest attempt is valid
        q = {"client_id": self.cfg["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
             "scope": SCOPES, "access_type": "offline", "prompt": "consent", "state": state,
             "code_challenge": challenge, "code_challenge_method": "S256"}
        return AUTH_URL + "?" + urllib.parse.urlencode(q)

    def finish_auth(self, code: str, state: str) -> str:
        """Exchange the code from Google's redirect for tokens. Returns the channel name."""
        if state not in self._pending:
            raise AuthError("This sign-in link has expired. Press Connect YouTube again in Clipper.")
        verifier, redirect_uri = self._pending.pop(state)
        r = requests.post(TOKEN_URL, data={
            "code": code, "client_id": self.cfg["client_id"], "client_secret": self.cfg["client_secret"],
            "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier,
        }, timeout=30)
        if r.status_code != 200:
            raise AuthError("Google refused the sign-in: " + _api_error(r)[1])
        tok = r.json()
        if not tok.get("refresh_token"):
            raise AuthError("Google did not return a refresh token. Remove Clipper's access at "
                            "myaccount.google.com/permissions and connect again.")
        with self.lock:
            self.cfg["refresh_token"] = _protect(tok["refresh_token"])
            self._access = (tok["access_token"], time.time() + int(tok.get("expires_in", 3600)) - 60)
            self._save()
        return self.refresh_channel()

    def refresh_channel(self) -> str:
        r = requests.get(f"{API}/youtube/v3/channels", params={"part": "snippet", "mine": "true"},
                         headers=self._headers(), timeout=30)
        if r.status_code == 200 and r.json().get("items"):
            item = r.json()["items"][0]
            name = item["snippet"]["title"]
            with self.lock:
                self.cfg.update(channel=name, channel_id=item.get("id", ""))
                self._save()
            return name
        if r.status_code == 200:
            raise AuthError("This Google account has no YouTube channel yet. Create one on youtube.com first.")
        raise AuthError("Could not read your channel: " + _api_error(r)[1])

    def disconnect(self) -> None:
        with self.lock:
            token = _unprotect(self.cfg.get("refresh_token", ""))
            for k in ("refresh_token", "channel", "channel_id"):
                self.cfg.pop(k, None)
            self._access = None
            self._save()
        if token:
            try:
                requests.post(REVOKE_URL, data={"token": token}, timeout=15)
            except requests.RequestException:
                pass

    def access_token(self) -> str:
        with self.lock:
            if self._access and time.time() < self._access[1]:
                return self._access[0]
            refresh = _unprotect(self.cfg.get("refresh_token", ""))
            if not refresh:
                raise AuthError("YouTube is not connected.")
            r = requests.post(TOKEN_URL, data={
                "client_id": self.cfg["client_id"], "client_secret": self.cfg["client_secret"],
                "refresh_token": refresh, "grant_type": "refresh_token"}, timeout=30)
            if r.status_code != 200:
                reason, msg = _api_error(r)
                if reason in ("invalid_grant", "unauthorized_client", "invalid_client"):
                    self.cfg.pop("refresh_token", None)
                    self._save()
                    raise AuthError("YouTube sign-in expired or was removed. Connect YouTube again.")
                raise RuntimeError(f"Could not refresh the YouTube sign-in: {msg}")
            tok = r.json()
            self._access = (tok["access_token"], time.time() + int(tok.get("expires_in", 3600)) - 60)
            return self._access[0]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token()}"}

    # -- upload
    def upload(self, path: Path, meta: dict, progress=None) -> str:
        """Resumable upload of one video. Returns the new video's ID."""
        size = path.stat().st_size
        body = {
            "snippet": {"title": meta["title"], "description": meta["description"], "tags": meta.get("tags", []),
                        "categoryId": meta.get("category", "24")},
            "status": {"privacyStatus": meta.get("privacy", self.privacy), "selfDeclaredMadeForKids": False,
                       "embeddable": True},
        }
        r = requests.post(f"{API}/upload/youtube/v3/videos",
                          params={"uploadType": "resumable", "part": "snippet,status", "notifySubscribers": "true"},
                          headers={**self._headers(), "Content-Type": "application/json; charset=UTF-8",
                                   "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)},
                          data=json.dumps(body), timeout=60)
        self._raise_for(r)
        session = r.headers["Location"]
        # a sized file object makes requests send a plain Content-Length body (a generator would be sent
        # chunked, which together with Content-Length is an invalid request)
        with _ProgressFile(path, progress) as body_file:
            r = requests.put(session, data=body_file,
                             headers={**self._headers(), "Content-Type": "video/mp4"}, timeout=600)
        self._raise_for(r)
        return r.json()["id"]

    @staticmethod
    def _raise_for(r: requests.Response) -> None:
        if r.status_code in (200, 201):
            return
        reason, msg = _api_error(r)
        if reason in ("quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded", "dailyLimitExceeded"):
            raise QuotaError(msg)
        if r.status_code == 401:
            raise AuthError("YouTube sign-in expired. Connect YouTube again.")
        if reason == "youtubeSignupRequired":
            raise AuthError("This Google account has no YouTube channel yet. Create one on youtube.com first.")
        raise RuntimeError(f"YouTube upload failed ({r.status_code} {reason}): {msg}")


# ------------------------------------------------------------------ metadata
_DATE_SUFFIX = re.compile(r"\s+\d{4}-\d{2}-\d{2} \d{2}[_:]\d{2}$")  # yt-dlp appends this to live titles


def clean_title(folder_name: str) -> str:
    t = re.sub(r"\s*\[[\w-]{11}\]$", "", folder_name)
    t = _DATE_SUFFIX.sub("", t)
    t = t.replace(" _ ", " | ").replace("_", " ")  # yt-dlp swaps characters Windows can't store
    return re.sub(r"\s{2,}", " ", t).strip()


def _is_vertical(path: Path) -> bool:
    from .tools import ffmpeg_exe
    try:
        out = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                             errors="replace", creationflags=NO_WINDOW, timeout=30).stderr
        m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", out)
        return bool(m and int(m[2]) > int(m[1]))
    except Exception:  # noqa: BLE001
        return True


def _caption_text(path: Path, limit: int = 300) -> str:
    srt = path.with_suffix(".srt")
    if not srt.exists():
        return ""
    lines = [l.strip() for l in srt.read_text(encoding="utf-8", errors="replace").splitlines()]
    text = " ".join(l for l in lines if l and not l.isdigit() and "-->" not in l)
    return text[:limit].rsplit(" ", 1)[0] + "…" if len(text) > limit else text


def build_metadata(path: Path, entry: dict, privacy: str) -> dict:
    """Title, description and tags for one clip, from its folder and clips.json entry."""
    vid = re.search(r"\[([\w-]{11})\]$", path.parent.name)
    source = entry.get("url") or (f"https://youtu.be/{vid[1]}" if vid else "")
    stream_title = clean_title(path.parent.name)
    vertical = _is_vertical(path)
    suffix = " #shorts" if vertical else ""
    title = stream_title[: 100 - len(suffix)].rstrip() + suffix
    title = title.replace("<", "").replace(">", "")  # YouTube rejects angle brackets in titles
    quote = _caption_text(path)
    desc = []
    if quote:
        desc.append(f"“{quote}”\n")
    desc.append(f"Clipped from: {stream_title}")
    if source:
        desc.append(source)
    desc.append("\n#shorts #clips #highlights" if vertical else "\n#clips #highlights")
    return {"title": title, "description": "\n".join(desc).replace("<", "").replace(">", "")[:4900],
            "tags": ["clips", "highlights", "shorts"] if vertical else ["clips", "highlights"],
            "privacy": privacy}


# ------------------------------------------------------------------ queue
class Uploader:
    """Uploads clips one at a time in the background and remembers each clip's state."""

    def __init__(self, account: YouTubeAccount, clips_root, log=print):
        self.account = account
        self._root = clips_root  # a Path, or a function returning the clips folder
        self.log = log
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.state: dict[str, dict] = _read_json(UPLOADS_FILE, {})
        self.paused_until = 0.0
        self.pause_reason = ""
        for v in self.state.values():  # resume after a restart
            if v.get("status") == "uploading":
                v["status"] = "queued"
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background upload thread (once)."""
        if self._thread is None:
            self.wake.set()  # look at the queue straight away
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    @property
    def root(self) -> Path:
        return self._root() if callable(self._root) else self._root

    def resume(self) -> None:
        """Retry now (e.g. right after connecting YouTube)."""
        self.paused_until = 0.0
        self.pause_reason = ""
        self.wake.set()

    @staticmethod
    def key(folder: str, file: str) -> str:
        return f"{folder}/{file}"

    def get(self, folder: str, file: str) -> dict | None:
        return self.state.get(self.key(folder, file))

    def _set(self, key: str, **fields) -> None:
        with self.lock:
            entry = self.state.setdefault(key, {})
            if "status" in fields and "error" not in fields:
                entry.pop("error", None)  # a new state clears an old error message
            entry.update(fields, at=time.time())
            _write_json(UPLOADS_FILE, self.state)

    def enqueue(self, folder: str, file: str, force: bool = False) -> dict:
        key = self.key(folder, file)
        cur = self.state.get(key, {})
        if cur.get("status") == "done" and not force:
            return cur
        if cur.get("status") in ("queued", "uploading", "waiting"):
            return cur
        self._set(key, status="queued", queued=time.time(), progress=0)
        self.wake.set()
        return self.state[key]

    def cancel(self, folder: str, file: str) -> None:
        key = self.key(folder, file)
        if self.state.get(key, {}).get("status") in ("queued", "waiting", "failed"):
            with self.lock:
                self.state.pop(key, None)
                _write_json(UPLOADS_FILE, self.state)

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for v in self.state.values():
            counts[v.get("status", "?")] = counts.get(v.get("status", "?"), 0) + 1
        today = time.time() - 24 * 3600
        return {"counts": counts, "paused_until": self.paused_until, "pause_reason": self.pause_reason,
                "uploaded_24h": sum(1 for v in self.state.values() if v.get("status") == "done" and v.get("at", 0) > today)}

    def _next(self) -> str | None:
        with self.lock:
            waiting = [(v.get("queued", 0), k) for k, v in self.state.items() if v.get("status") in ("queued", "waiting")]
        return min(waiting)[1] if waiting else None

    def _run(self) -> None:
        while True:
            self.wake.wait(timeout=30)
            self.wake.clear()
            while True:
                if time.time() < self.paused_until:
                    break
                if not self.account.connected:
                    break
                key = self._next()
                if not key:
                    break
                self._upload_one(key)

    def _upload_one(self, key: str) -> None:
        folder, file = key.split("/", 1)
        path = self.root / folder / file
        if not path.exists():
            self._set(key, status="failed", error="The clip file no longer exists.")
            return
        entry = {}
        manifest = _read_json(path.parent / "clips.json", [])
        if isinstance(manifest, list):
            entry = next((m for m in manifest if m.get("file") == file), {})
        try:
            meta = build_metadata(path, entry, self.account.privacy)
            self._set(key, status="uploading", progress=0, title=meta["title"])
            last = [0.0]

            def progress(p):
                if p - last[0] >= 0.05 or p >= 1:
                    last[0] = p
                    self.state[key]["progress"] = round(p, 3)

            video_id = self.account.upload(path, meta, progress)
            self._set(key, status="done", video_id=video_id, url=f"https://youtu.be/{video_id}",
                      privacy=meta["privacy"], progress=1, attempts=0)
            self.pause_reason = ""
            self.log(f"uploaded {key} -> https://youtu.be/{video_id}")
        except QuotaError:
            self.paused_until = next_quota_reset()
            reset = datetime.fromtimestamp(self.paused_until)
            day = "today" if reset.date() == datetime.now().date() else "tomorrow"
            when = f"{reset:%H:%M} {day}"
            self.pause_reason = f"YouTube's daily upload limit is used up. Uploads continue after {when}."
            self._set(key, status="waiting", error=self.pause_reason)
            self.log(f"quota reached; uploads paused until {when}")
        except AuthError as e:
            self._set(key, status="waiting", error=str(e))
            self.pause_reason = str(e)
            self.paused_until = time.time() + 60  # re-check once reconnected
            self.log(f"upload of {key} needs sign-in: {e}")
        except Exception as e:  # noqa: BLE001  (network trouble, server errors)
            attempts = self.state.get(key, {}).get("attempts", 0) + 1
            if attempts >= 3:
                self._set(key, status="failed", error=str(e)[:300], attempts=attempts)
            else:
                self._set(key, status="queued", error=str(e)[:300], attempts=attempts)
                self.paused_until = time.time() + 30 * attempts
            self.log(f"upload of {key} failed (attempt {attempts}): {e}")
