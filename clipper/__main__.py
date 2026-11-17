"""CLI: python -m clipper <youtube-url> [options]"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import chat
from .heat import clip_window, find_spikes, message_heat, per_second, spikes_from_curve
from .render import download_section, finish_clip


def _fmt(t: float) -> str:
    t = int(t)
    return f"{t // 3600:d}h{t % 3600 // 60:02d}m{t % 60:02d}s"


def _safe(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", s).strip()[:80] or "video"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clipper",
        description="Clip the moments where YouTube chat blows up, with burned-in captions.",
    )
    p.add_argument("url", help="YouTube video / live stream URL")
    p.add_argument("-o", "--out", default="clips", help="output folder (default: clips)")
    p.add_argument("-n", "--top", type=int, default=5, help="max clips for a recorded video, or per hour on a live stream (default 5)")
    p.add_argument("--pre", type=float, default=25, help="seconds before the chat peak (default 25)")
    p.add_argument("--post", type=float, default=10, help="seconds after the chat peak (default 10)")
    p.add_argument("--source", choices=["auto", "chat", "heatmap", "comments"], default="auto",
                   help="signal for normal videos: chat replay, 'Most replayed' graph, or timestamped comments")

    d = p.add_argument_group("spike detection")
    d.add_argument("--threshold", type=float, default=3.5, help="robust z-score needed to count as a spike (default 3.5)")
    d.add_argument("--window", type=int, default=10, help="smoothing window in seconds (default 10)")
    d.add_argument("--baseline", type=int, default=300, help="baseline length in seconds (default 300)")
    d.add_argument("--min-gap", type=int, default=60, help="min seconds between clips (default 60)")
    d.add_argument("--skip-start", type=float, default=90,
                   help="ignore the first N seconds of a recorded stream (greeting flood) (default 90)")
    d.add_argument("--min-rate", type=float, default=0.3, help="min messages/sec at a spike (default 0.3)")
    d.add_argument("--min-jump", type=float, default=2.0,
                   help="chat must reach this many times its normal rate to count as a spike (default 2.0)")

    c = p.add_argument_group("captions / output")
    c.add_argument("--no-captions", action="store_true", help="skip transcription and captions")
    c.add_argument("--horizontal", dest="vertical", action="store_false",
                   help="keep the original 16:9 frame (default: 9:16 vertical for Shorts / TikTok / Reels)")
    c.add_argument("--vertical", dest="vertical", action="store_true", default=True, help=argparse.SUPPRESS)
    c.add_argument("--model", default="small", help="whisper model: tiny, base, small, medium, large-v3 (default small)")
    c.add_argument("--device", default="auto", help="auto, cpu or cuda")
    c.add_argument("--language", default=None, help="force spoken language code, e.g. en, hi")
    c.add_argument("--keep-raw", action="store_true", help="keep uncaptioned source clips")

    lv = p.add_argument_group("live streams")
    lv.add_argument("--live-offset", type=float, default=3,
                    help="extra seconds chat lags behind the video on live streams (default 3)")
    lv.add_argument("--max-minutes", type=float, default=0, help="stop watching after N minutes (default: until stream ends)")
    lv.add_argument("--keep-recording", action="store_true", help="keep the full stream recording")

    p.add_argument("--stop-file", default=None,
                   help="stop gracefully (finishing clips in progress) when this file appears")
    p.add_argument("--dry-run", action="store_true", help="only list the detected moments")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def detect_vod_spikes(url: str, info: dict, work: Path, args) -> tuple[list, str]:
    duration = float(info.get("duration") or 0)
    has_chat = "live_chat" in (info.get("subtitles") or {})

    if args.source in ("auto", "chat") and has_chat:
        print("Downloading chat replay (long streams can take a few minutes)...")
        msgs = chat.download_chat_replay(url, work)
        print(f"  {len(msgs)} chat messages")
        if msgs:
            length = int(max(duration, msgs[-1].t)) + 1
            bins = per_second(((m.t, message_heat(m.text, m.weight)) for m in msgs), 0, length)
            for i in range(min(int(args.skip_start), len(bins))):
                bins[i] = 0.0
            return find_spikes(bins, window=args.window, baseline=args.baseline, threshold=args.threshold,
                               min_gap=args.min_gap, top=args.top, min_rate=args.min_rate,
                               min_jump=args.min_jump), "chat replay"
    elif args.source == "chat":
        print("This video has no chat replay.")

    if args.source in ("auto", "heatmap"):
        # the graph marks the moment itself (not a delayed reaction), so centre the clip on it;
        # the first few % are always high because everyone watches the intro
        shift = args.pre - (args.pre + args.post) / 2
        curve = [(t + shift, v) for t, v in chat.heatmap_signal(info) if t > max(10, 0.03 * duration)]
        if curve:
            return spikes_from_curve(curve, args.top, args.min_gap), "'Most replayed' graph"
        if args.source == "heatmap":
            print("No 'Most replayed' data for this video.")

    if args.source in ("auto", "comments"):
        print("Scanning comments for timestamps...")
        msgs = chat.comment_timestamp_signal(url, duration)
        print(f"  {len(msgs)} timestamp mentions")
        if msgs:
            # a timestamp points at the moment itself, so centre it in the window
            bins = per_second(((m.t + args.pre - (args.pre + args.post) / 2, m.weight) for m in msgs), 0,
                              int(duration) + 1)
            return find_spikes(bins, window=5, baseline=args.baseline, threshold=2.0,
                               min_gap=args.min_gap, top=args.top, min_rate=0.05), "timestamped comments"
    return [], "nothing"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    print("Fetching video info...")
    info = chat.video_info(args.url)
    vid = info.get("id", "video")
    out_dir = Path(args.out) / f"{_safe(info.get('title') or vid)} [{vid}]"
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "_work"

    if info.get("is_live") or info.get("live_status") == "is_live":
        from .live import watch_live

        watch_live(args.url, info, out_dir, args)
        return 0
    if info.get("live_status") == "is_upcoming":
        print("This stream hasn't started yet. Run again once it is live.")
        return 1

    duration = float(info.get("duration") or 0)
    print(f"Video: {info.get('title')} ({_fmt(duration)})")
    spikes, source = detect_vod_spikes(args.url, info, work, args)
    if not spikes:
        print("No hype moments found. Try lowering --threshold or --min-rate.")
        return 1

    print(f"\nTop moments from {source}:")
    for i, s in enumerate(spikes, 1):
        s.start, s.end = clip_window(s.peak, args.pre, args.post, duration or None)
        print(f"  #{i}  {_fmt(s.start)} -> {_fmt(s.end)}   peak {_fmt(s.peak)}  score {s.score:.1f}")
    if args.dry_run:
        return 0

    manifest = []
    for i, s in enumerate(spikes, 1):
        if args.stop_file and Path(args.stop_file).exists():
            print("Stop requested, skipping remaining clips.")
            break
        name = f"clip_{i:02d}_{_fmt(s.peak)}"
        print(f"\n[{i}/{len(spikes)}] {name}")
        try:
            print("  downloading section...")
            raw = download_section(args.url, s.start, s.end, work / f"{name}_raw.mp4")
            print("  captioning + rendering..." if not args.no_captions else "  rendering...")
            final = finish_clip(raw, out_dir / f"{name}.mp4", captions=not args.no_captions,
                                vertical=args.vertical, whisper_model=args.model,
                                device=args.device, language=args.language)
            if not args.keep_raw:
                raw.unlink(missing_ok=True)
            print(f"  ✔ {final}")
            manifest.append({"file": final.name, "start": s.start, "end": s.end, "peak": s.peak,
                              "score": round(s.score, 2), "source": source,
                              "url": f"https://youtu.be/{vid}?t={int(s.start)}"})
        except Exception as e:  # noqa: BLE001
            print(f"  ! failed: {e}")
    (out_dir / "clips.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nDone: {len(manifest)} clip(s) in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
