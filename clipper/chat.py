"""Fetching YouTube metadata, chat replay, live chat and comment signals."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import yt_dlp

from .tools import ffmpeg_exe, run, ytdlp_cmd


@dataclass
class ChatMessage:
    t: float      # seconds: video offset (replay) or unix time (live)
    text: str
    weight: float = 1.0  # paid messages / memberships count extra


def video_info(url: str) -> dict:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "ffmpeg_location": ffmpeg_exe()}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


# ---------------------------------------------------------------- parsing

def _runs_text(runs: list[dict]) -> str:
    out = []
    for r in runs or []:
        if "text" in r:
            out.append(r["text"])
        elif "emoji" in r:
            emoji = r["emoji"]
            shortcuts = emoji.get("shortcuts") or []
            out.append(shortcuts[0] if shortcuts else emoji.get("emojiId", ""))
    return "".join(out)


_RENDERERS = {
    "liveChatTextMessageRenderer": 1.0,
    "liveChatPaidMessageRenderer": 3.0,
    "liveChatPaidStickerRenderer": 3.0,
    "liveChatMembershipItemRenderer": 2.0,
    "liveChatSponsorshipsGiftPurchaseAnnouncementRenderer": 3.0,
}


def parse_actions(actions: list[dict], live: bool, offset_ms=None) -> list[ChatMessage]:
    """Turn YouTube chat actions into messages (live: unix time; replay: video offset)."""
    msgs = []
    for act in actions:
        item = (act.get("addChatItemAction") or {}).get("item") or {}
        for key, weight in _RENDERERS.items():
            r = item.get(key)
            if not r:
                continue
            if live or offset_ms is None:
                usec = r.get("timestampUsec")
                if usec is None:
                    continue
                t = int(usec) / 1e6
            else:
                t = int(offset_ms) / 1000.0
            text = _runs_text((r.get("message") or {}).get("runs", []))
            msgs.append(ChatMessage(t=t, text=text, weight=weight))
    return msgs


def parse_chat_line(line: str, live: bool) -> list[ChatMessage]:
    """Parse one JSON line of yt-dlp's .live_chat.json output."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return []
    replay = obj.get("replayChatItemAction")
    actions = replay.get("actions", []) if replay else [obj] if "addChatItemAction" in obj else obj.get("actions", [])
    offset_ms = replay.get("videoOffsetTimeMsec") if replay else None
    return parse_actions(actions, live, offset_ms)


# ---------------------------------------------------------------- replay chat

def download_chat_replay(url: str, workdir: Path) -> list[ChatMessage]:
    """Download the full chat replay of a finished stream (empty list if none)."""
    workdir.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(workdir / "chat")
    try:
        run(ytdlp_cmd("--skip-download", "--write-subs", "--sub-langs", "live_chat", "-o", out_tmpl, url))
    except RuntimeError as e:
        print(f"  ! chat replay download failed: {str(e).splitlines()[0]}")
        return []
    files = list(workdir.glob("chat*.live_chat.json"))
    if not files:
        return []
    msgs: list[ChatMessage] = []
    with open(files[0], encoding="utf-8") as fh:
        for line in fh:
            msgs.extend(parse_chat_line(line, live=False))
    msgs.sort(key=lambda m: m.t)
    return msgs


# ---------------------------------------------------------------- live chat

def follow_live_chat(url: str, video_id: str, workdir: Path, stop, log=print) -> Iterator[ChatMessage]:
    """Yield live chat messages (unix timestamps) until `stop` (threading.Event) is set.

    Reads YouTube's unfiltered "Live chat" feed directly. yt-dlp only reads the default
    "Top chat" view for live streams, which hides most messages, so it is the fallback.
    """
    got_any = False
    try:
        for m in _follow_full_live_chat(video_id, stop):
            got_any = True
            yield m
        return
    except Exception as e:  # noqa: BLE001
        if stop.is_set():
            return
        log(f"  ! full live chat feed unavailable ({e}); falling back to yt-dlp (Top chat only)")
    if not got_any or not stop.is_set():
        yield from _follow_live_chat_ytdlp(url, workdir, stop)


_LIVE_CHAT_PAGE = "https://www.youtube.com/live_chat?is_popout=1&v={}"
_LIVE_CHAT_API = "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key={}&prettyPrint=false"


def _follow_full_live_chat(video_id: str, stop) -> Iterator[ChatMessage]:
    from yt_dlp.extractor.youtube import YoutubeBaseInfoExtractor
    from yt_dlp.networking import Request
    from yt_dlp.utils import traverse_obj

    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
    ie = YoutubeBaseInfoExtractor(ydl)
    page = ydl.urlopen(Request(_LIVE_CHAT_PAGE.format(video_id),
                               headers={"Accept-Language": "en-US,en;q=0.9"})).read().decode("utf-8", "replace")
    data = ie.extract_yt_initial_data(video_id, page)
    ytcfg = ie.extract_ytcfg(video_id, page)
    api_key = ytcfg.get("INNERTUBE_API_KEY")
    context = ytcfg.get("INNERTUBE_CONTEXT")
    if not (data and api_key and context):
        raise RuntimeError("chat page had no chat data")

    # The chat header has a menu: [Top chat, Live chat]. Pick the unfiltered "Live chat".
    items = traverse_obj(data, ("contents", "liveChatRenderer", "header", "liveChatHeaderRenderer", "viewSelector",
                                "sortFilterSubMenuRenderer", "subMenuItems")) or []
    full = [it for it in items if not str(it.get("title", "")).lower().startswith("top")] or items[1:2]
    continuation = traverse_obj(full, (0, "continuation", "reloadContinuationData", "continuation"))
    if not continuation:
        raise RuntimeError("no 'Live chat' option in the chat menu")

    visitor = traverse_obj(context, ("client", "visitorData"))
    headers = ie.generate_api_headers(ytcfg=ytcfg, visitor_data=visitor)
    headers["content-type"] = "application/json"
    failures = 0
    while continuation and not stop.is_set():
        try:
            body = json.dumps({"context": context, "continuation": continuation}).encode()
            resp = json.loads(ydl.urlopen(Request(_LIVE_CHAT_API.format(api_key), data=body,
                                                  headers=headers)).read())
            failures = 0
        except Exception:  # noqa: BLE001
            failures += 1
            if failures >= 5:
                raise
            time.sleep(2 * failures)
            continue
        chat = traverse_obj(resp, ("continuationContents", "liveChatContinuation")) or {}
        yield from parse_actions(chat.get("actions") or [], live=True)
        cont = traverse_obj(chat, ("continuations", 0, ("invalidationContinuationData", "timedContinuationData",
                                                        "reloadContinuationData")), get_all=False) or {}
        continuation = cont.get("continuation")
        wait = min(max((cont.get("timeoutMs") or 2000) / 1000, 1.0), 5.0)
        stop.wait(wait)


def _follow_live_chat_ytdlp(url: str, workdir: Path, stop) -> Iterator[ChatMessage]:
    """Fallback: tail the file yt-dlp writes while following the (Top) live chat."""
    from .tools import ytdlp_popen
    import subprocess

    workdir.mkdir(parents=True, exist_ok=True)
    for old in workdir.glob("livechat*.live_chat.json*"):
        old.unlink(missing_ok=True)
    proc = ytdlp_popen(
        "--skip-download", "--write-subs", "--sub-langs", "live_chat",
        "-o", str(workdir / "livechat"), url,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        fh = None
        buf = ""
        while not stop.is_set():
            if fh is None:
                # yt-dlp appends parsed lines to the .part file (raw poll responses go to -FragN files)
                files = [p for p in (workdir / "livechat.live_chat.json.part", workdir / "livechat.live_chat.json")
                         if p.exists()]
                if not files:
                    if proc.poll() is not None:
                        return
                    time.sleep(0.5)
                    continue
                fh = open(files[0], encoding="utf-8")
            chunk = fh.read()
            if not chunk:
                if proc.poll() is not None:
                    return
                time.sleep(0.5)
                continue
            buf += chunk
            *lines, buf = buf.split("\n")
            for line in lines:
                if line.strip():
                    yield from parse_chat_line(line, live=True)
    finally:
        if proc.poll() is None:
            proc.terminate()


# ---------------------------------------------------------------- normal videos

_TS_RE = re.compile(r"(?<![\d:])(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?![\d:])")


def heatmap_signal(info: dict) -> list[tuple[float, float]]:
    """YouTube 'Most replayed' graph as (time, value) points, if available."""
    return [((h["start_time"] + h["end_time"]) / 2, float(h["value"])) for h in info.get("heatmap") or []]


def comment_timestamp_signal(url: str, duration: float, max_comments: int = 2000) -> list[ChatMessage]:
    """Timestamps people mention in comments ('3:41 lmao'), weighted by likes."""
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True, "getcomments": True,
        "extractor_args": {"youtube": {"max_comments": [str(max_comments)], "comment_sort": ["top"]}},
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001
        print(f"  ! comment download failed: {e}")
        return []
    msgs = []
    for c in info.get("comments") or []:
        text = c.get("text") or ""
        likes = c.get("like_count") or 0
        for h, m, s in _TS_RE.findall(text):
            t = int(h or 0) * 3600 + int(m) * 60 + int(s)
            if 0 <= t <= duration:
                msgs.append(ChatMessage(t=float(t), text=text, weight=1.0 + min(likes, 500) ** 0.5))
    return msgs
