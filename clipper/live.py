"""Watching a live stream: record it, follow chat, clip when chat spikes."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .chat import follow_live_chat
from .heat import HeatTracker, message_heat
from .render import cut_local, finish_clip
from .tools import ffmpeg_exe, run, ytdlp_cmd

LIVE_FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/b"
CHAT_LAG = 25  # YouTube delivers live chat in batches 10-20 s late; wait this long before scoring a second


def _fmt(t: float) -> str:
    t = int(t)
    return f"{t // 3600:d}h{t % 3600 // 60:02d}m{t % 60:02d}s"


def watch_live(url: str, info: dict, out_dir: Path, args) -> None:
    work = out_dir / "_work"
    work.mkdir(parents=True, exist_ok=True)
    rec = work / "recording.ts"
    rec.unlink(missing_ok=True)

    print(f"Recording live stream: {info.get('title')}")
    # yt-dlp resolves the HLS URLs; ffmpeg records them into MPEG-TS, which stays
    # readable (and cuttable) while it is still being written.
    urls = run(ytdlp_cmd("-f", LIVE_FORMAT, "-g", url)).stdout.split()
    if not urls:
        raise RuntimeError("Could not resolve live stream URLs")
    cmd = [ffmpeg_exe(), "-y", "-loglevel", "error"]
    for u in urls:
        cmd += ["-live_start_index", "-1", "-i", u]
    for i in range(len(urls)):
        cmd += ["-map", f"{i}"]
    cmd += ["-c", "copy", "-f", "mpegts", str(rec)]
    recorder = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=open(work / "recorder.log", "w"))
    while not (rec.exists() and rec.stat().st_size > 0):
        if recorder.poll() is not None:
            raise RuntimeError(f"Recorder exited before writing anything; see {work / 'recorder.log'}")
        time.sleep(0.2)
    rec_start = time.time()
    print(f"  recording started -> {rec}")

    stop = threading.Event()
    msgs: queue.Queue = queue.Queue()

    def chat_thread():
        try:
            for m in follow_live_chat(url, info["id"], work, stop):
                msgs.put(m)
        except Exception as e:  # noqa: BLE001
            print(f"  ! chat follower crashed: {e}")

    threading.Thread(target=chat_thread, daemon=True).start()
    print("  following chat... (Ctrl+C to stop)\n")

    tracker = HeatTracker(args.window, args.baseline)
    heat: dict[int, float] = {}
    next_sec = int(rec_start)
    spike = None  # (start_sec, peak_sec, peak_z, peak_rate)
    last_clip_peak = -1e9
    pool = ThreadPoolExecutor(max_workers=1)
    manifest: list[dict] = []
    n = 0
    received, last_status = 0, time.time()

    def make_clip(idx: int, peak_wall: float, z: float):
        pos = peak_wall - rec_start - args.live_offset
        start, end = max(0.0, pos - args.pre), pos + args.post
        wait = rec_start + end + 5 - time.time()
        if wait > 0:
            time.sleep(wait)  # let the recording catch up past the clip end
        name = f"clip_{idx:02d}_{_fmt(pos)}"
        try:
            raw = cut_local(rec, start, end, work / f"{name}_raw.mp4")
            final = finish_clip(raw, out_dir / f"{name}.mp4", captions=not args.no_captions,
                                vertical=args.vertical, whisper_model=args.model,
                                device=args.device, language=args.language)
            if not args.keep_raw:
                raw.unlink(missing_ok=True)
            print(f"  ✔ saved {final}")
            manifest.append({"file": final.name, "stream_time": pos, "start": start, "end": end, "score": z})
            (out_dir / "clips.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            print(f"  ! clip {idx} failed: {e}")

    try:
        while True:
            if recorder.poll() is not None:
                print("Stream ended (recorder exited).")
                break
            if args.stop_file and Path(args.stop_file).exists():
                print("Stop requested, finishing pending clips.")
                break
            if args.max_minutes and time.time() - rec_start > args.max_minutes * 60:
                print(f"Reached --max-minutes {args.max_minutes:g}, stopping.")
                break
            while True:
                try:
                    m = msgs.get_nowait()
                except queue.Empty:
                    break
                if m.t < rec_start - 5:
                    continue  # backlog from before we started watching
                sec = max(int(m.t), next_sec)  # very late messages count toward the next unscored second
                heat[sec] = heat.get(sec, 0.0) + message_heat(m.text, m.weight)
                received += 1
            if time.time() - last_status >= 30:
                print(f"  [{_fmt(time.time() - rec_start)}] chat: {received} msgs in last 30s, "
                      f"clips made: {len(manifest)}")
                received, last_status = 0, time.time()

            # score every second whose chat should have fully arrived by now
            while next_sec <= time.time() - CHAT_LAG:
                rate, z = tracker.push(heat.pop(next_sec, 0.0))
                if args.verbose:
                    print(f"    {_fmt(next_sec - rec_start)} rate={rate:5.2f} z={z:5.2f}")
                hot = z >= args.threshold and rate >= args.min_rate
                if spike is None and hot and next_sec - last_clip_peak >= args.min_gap:
                    spike = [next_sec, next_sec, z, rate]
                    print(f"  🔥 chat spike at {_fmt(next_sec - rec_start)} (z={z:.1f}, {rate:.1f} msg/s)")
                elif spike is not None:
                    if z > spike[2]:
                        spike[1], spike[2], spike[3] = next_sec, z, rate
                    if z < args.threshold / 2 or next_sec - spike[0] > 30:
                        n += 1
                        peak = spike[1] - args.window / 2
                        last_clip_peak = spike[1]
                        print(f"  ✂ clipping #{n} around {_fmt(peak - rec_start)} (peak z={spike[2]:.1f})")
                        pool.submit(make_clip, n, peak, spike[2])
                        spike = None
                next_sec += 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping... finishing pending clips.")
    finally:
        stop.set()
        pool.shutdown(wait=True)
        if recorder.poll() is None:
            try:
                recorder.communicate(b"q", timeout=10)
            except Exception:  # noqa: BLE001
                recorder.kill()
        if not args.keep_recording:
            time.sleep(1)
            rec.unlink(missing_ok=True)
