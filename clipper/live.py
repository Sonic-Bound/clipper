"""Watching a live stream: record it, follow chat, clip when chat spikes.

The stream is recorded in segments. If a recording stops (network drop, YouTube's
stream links expiring after some hours), we ask YouTube whether the stream is still
live; if it is, fresh links are fetched and a new segment starts. Clips that span a
reconnect are stitched together from both segments.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .chat import follow_live_chat, video_info
from .heat import LiveSpikeDetector, message_heat
from .render import cut_local, finish_clip
from .tools import ffmpeg_exe, run, ytdlp_cmd

LIVE_FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/b"
CHAT_LAG = 25  # YouTube delivers live chat in batches 10-20 s late; wait this long before scoring a second
RECONNECT_FOR = 15 * 60  # keep trying to reconnect for this long before giving up
RETRY_DELAYS = [3, 5, 10, 20, 30, 60]  # seconds between reconnect attempts (last one repeats)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _fmt(t: float) -> str:
    t = int(max(0, t))
    return f"{t // 3600:d}h{t % 3600 // 60:02d}m{t % 60:02d}s"


@dataclass
class Segment:
    path: Path
    start: float              # wall-clock time of the first recorded frame
    end: float | None = None  # wall-clock time the recording stopped (None = still recording)


class Recorder:
    """Records a live stream into numbered MPEG-TS segments (readable while being written)."""

    def __init__(self, url: str, work: Path):
        self.url = url
        self.work = work
        self.segments: list[Segment] = []
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def start(self) -> Segment:
        """Resolve fresh stream URLs and start a new segment. Raises if it can't."""
        urls = run(ytdlp_cmd("-f", LIVE_FORMAT, "-g", self.url)).stdout.split()
        if not urls:
            raise RuntimeError("could not resolve live stream URLs")
        path = self.work / f"recording_{len(self.segments) + 1:03d}.ts"
        path.unlink(missing_ok=True)
        cmd = [ffmpeg_exe(), "-y", "-loglevel", "error"]
        for u in urls:
            cmd += ["-live_start_index", "-1", "-i", u]
        for i in range(len(urls)):
            cmd += ["-map", f"{i}"]
        cmd += ["-c", "copy", "-f", "mpegts", str(path)]
        log = open(self.work / "recorder.log", "a", encoding="utf-8")
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log,
                                creationflags=NO_WINDOW)
        deadline = time.time() + 60
        while not (path.exists() and path.stat().st_size > 0):
            if proc.poll() is not None or time.time() > deadline:
                if proc.poll() is None:
                    proc.kill()
                raise RuntimeError(f"recorder wrote nothing (see {self.work / 'recorder.log'})")
            time.sleep(0.2)
        seg = Segment(path, time.time())
        with self.lock:
            self.proc = proc
            self.segments.append(seg)
        return seg

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def mark_stopped(self) -> None:
        with self.lock:
            if self.segments and self.segments[-1].end is None:
                self.segments[-1].end = time.time()

    def pieces(self, a: float, b: float) -> list[tuple[Path, float, float]]:
        """Parts of the wall-clock range [a, b] that were recorded: (file, start offset, end offset)."""
        out = []
        with self.lock:
            for seg in self.segments:
                seg_end = seg.end if seg.end is not None else time.time()
                lo, hi = max(a, seg.start), min(b, seg_end)
                if hi - lo >= 0.5:
                    out.append((seg.path, lo - seg.start, hi - seg.start))
        return out

    def stop(self) -> None:
        if self.alive():
            try:
                self.proc.communicate(b"q", timeout=10)
            except Exception:  # noqa: BLE001
                self.proc.kill()
        self.mark_stopped()

    def delete_files(self) -> None:
        for seg in self.segments:
            seg.path.unlink(missing_ok=True)


def cut_range(rec: Recorder, a: float, b: float, out: Path) -> Path | None:
    """Cut wall-clock range [a, b] out of the recording, stitching across reconnects."""
    parts = rec.pieces(a, b)
    if not parts:
        return None
    if len(parts) == 1:
        path, s, e = parts[0]
        return cut_local(path, s, e, out)
    files = []
    for i, (path, s, e) in enumerate(parts):
        files.append(cut_local(path, s, e, out.with_name(f"{out.stem}_part{i}.mp4")))
    listing = out.with_name(f"{out.stem}_parts.txt")
    listing.write_text("".join(f"file '{f.name}'\n" for f in files), encoding="utf-8")
    run([ffmpeg_exe(), "-y", "-f", "concat", "-safe", "0", "-i", listing.name, "-c", "copy", out.name],
        cwd=out.parent)
    for f in files:
        f.unlink(missing_ok=True)
    listing.unlink(missing_ok=True)
    return out


def _stop_requested(args) -> bool:
    return bool(args.stop_file and Path(args.stop_file).exists())


def _reconnect(rec: Recorder, url: str, args) -> str:
    """After the recording stopped: 'ok' (recording again), 'ended', 'stopped' or 'gave_up'."""
    deadline = time.time() + RECONNECT_FOR
    attempt = 0
    while time.time() < deadline:
        if _stop_requested(args):
            return "stopped"
        try:
            status = video_info(url).get("live_status")
            if status in ("was_live", "post_live", "not_live"):
                return "ended"
            if status == "is_live":
                rec.start()
                return "ok"
            print(f"  … stream status is '{status}', retrying")
        except Exception as e:  # noqa: BLE001  (usually: no internet right now)
            print(f"  … reconnect attempt {attempt + 1} failed: {str(e).splitlines()[0][:120]}")
        wait = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
        attempt += 1
        for _ in range(wait * 2):
            if _stop_requested(args):
                return "stopped"
            time.sleep(0.5)
    return "gave_up"


def watch_live(url: str, info: dict, out_dir: Path, args) -> None:
    work = out_dir / "_work"
    work.mkdir(parents=True, exist_ok=True)

    print(f"Recording live stream: {info.get('title')}")
    rec = Recorder(url, work)
    first = rec.start()
    t0 = first.start  # stream time 0 = when we started watching
    print(f"  recording started -> {first.path}")

    stop = threading.Event()
    msgs: queue.Queue = queue.Queue()

    def chat_thread():
        while not stop.is_set():
            try:
                for m in follow_live_chat(url, info["id"], work, stop):
                    msgs.put(m)
            except Exception as e:  # noqa: BLE001
                print(f"  ! chat reader error: {e}")
            if stop.is_set():
                break
            print("  ! chat connection lost, reconnecting in 10 s")
            stop.wait(10)

    threading.Thread(target=chat_thread, daemon=True).start()
    print("  following chat... (Ctrl+C to stop)\n")

    detector = LiveSpikeDetector(window=args.window, baseline=args.baseline, threshold=args.threshold,
                                 min_rate=args.min_rate, min_jump=args.min_jump, min_gap=args.min_gap,
                                 per_hour=args.top)
    heat: dict[int, float] = {}
    next_sec = int(t0)
    pool = ThreadPoolExecutor(max_workers=1)
    manifest: list[dict] = []
    n = 0
    received, last_status = 0, time.time()

    def make_clip(idx: int, peak_wall: float, z: float):
        moment = peak_wall - args.live_offset
        a, b = moment - args.pre, moment + args.post
        while time.time() < b + 5 and rec.alive():
            time.sleep(0.5)  # let the recording catch up past the clip end
        pos = moment - t0
        name = f"clip_{idx:02d}_{_fmt(pos)}"
        try:
            raw = cut_range(rec, a, b, work / f"{name}_raw.mp4")
            if raw is None:
                print(f"  ! clip {idx} skipped: that moment fell in a recording gap")
                return
            final = finish_clip(raw, out_dir / f"{name}.mp4", captions=not args.no_captions,
                                vertical=args.vertical, whisper_model=args.model,
                                device=args.device, language=args.language)
            if not args.keep_raw:
                raw.unlink(missing_ok=True)
            print(f"  ✔ saved {final}")
            manifest.append({"file": final.name, "stream_time": pos, "start": max(0.0, a - t0),
                             "end": b - t0, "score": z})
            (out_dir / "clips.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            print(f"  ! clip {idx} failed: {e}")

    try:
        while True:
            if _stop_requested(args):
                print("Stop requested, finishing pending clips.")
                break
            if args.max_minutes and time.time() - t0 > args.max_minutes * 60:
                print(f"Reached --max-minutes {args.max_minutes:g}, stopping.")
                break
            if not rec.alive():
                rec.mark_stopped()
                lost_at = time.time()
                print(f"  ⚠ recording stopped at {_fmt(lost_at - t0)}; checking whether the stream is still live...")
                outcome = _reconnect(rec, url, args)
                if outcome == "ok":
                    print(f"  ↻ reconnected after {time.time() - lost_at:.0f} s, recording continues "
                          f"(segment {len(rec.segments)})")
                elif outcome == "ended":
                    print("Stream ended.")
                    break
                elif outcome == "stopped":
                    print("Stop requested, finishing pending clips.")
                    break
                else:
                    print(f"Could not reconnect for {RECONNECT_FOR // 60} minutes, stopping.")
                    break

            while True:
                try:
                    m = msgs.get_nowait()
                except queue.Empty:
                    break
                if m.t < t0 - 5:
                    continue  # backlog from before we started watching
                sec = max(int(m.t), next_sec)  # very late messages count toward the next unscored second
                heat[sec] = heat.get(sec, 0.0) + message_heat(m.text, m.weight)
                received += 1
            if time.time() - last_status >= 30:
                print(f"  [{_fmt(time.time() - t0)}] chat: {received} msgs in last 30s, "
                      f"clips made: {len(manifest)}")
                received, last_status = 0, time.time()

            # score every second whose chat should have fully arrived by now
            while next_sec <= time.time() - CHAT_LAG:
                for ev in detector.push(next_sec, heat.pop(next_sec, 0.0)):
                    if ev[0] == "spike":
                        _, sec, z, rate, jump = ev
                        print(f"  🔥 chat spike at {_fmt(sec - t0)} (z={z:.1f}, {rate:.1f} msg/s, {jump:.1f}x normal)")
                    elif ev[0] == "skip":
                        print(f"  · chat spike at {_fmt(ev[1] - t0)} skipped: already {ev[2]} clips "
                              f"this hour (limit {args.top}/hour)")
                    else:
                        _, peak, z = ev
                        n += 1
                        print(f"  ✂ clipping #{n} around {_fmt(peak - t0)} (peak z={z:.1f})")
                        pool.submit(make_clip, n, peak, z)
                if args.verbose:
                    rate, z, jump = detector.last
                    print(f"    {_fmt(next_sec - t0)} rate={rate:5.2f} z={z:5.2f} x{jump:.1f}")
                next_sec += 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping... finishing pending clips.")
    finally:
        stop.set()
        pool.shutdown(wait=True)
        rec.stop()
        if not args.keep_recording:
            time.sleep(1)
            rec.delete_files()
            _tidy(work, out_dir)


def _tidy(work: Path, out_dir: Path) -> None:
    """Remove the work folder, and the video's folder too if no clip was made."""
    import shutil

    shutil.rmtree(work, ignore_errors=True)
    try:
        if not any(out_dir.glob("clip_*.mp4")):
            shutil.rmtree(out_dir, ignore_errors=True)
    except OSError:
        pass
