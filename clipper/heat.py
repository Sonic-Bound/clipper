"""Detecting moments where chat 'hikes up'.

Each second gets a heat value: number of messages (weighted: superchats count
more, hype words/emotes count more). The value is smoothed over a short window
and compared with a rolling baseline (median + MAD of the previous few
minutes), so a busy stream and a quiet stream are both judged against their own
normal level. A spike is a local peak whose robust z-score clears a threshold.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from statistics import median
from typing import Iterable

HYPE_PATTERNS = [
    r"\bl+o+l+\b", r"\blmf?a+o+\b", r"\bkek\w*\b", r"\bomega\w*\b", r"\bpog\w*\b",
    r"\bw+\b", r"\bclip\s*(it|that)\b", r"\bno\s*way\b", r"\bwtf\b", r"\bomg\b",
    r"\bhaha+\b", r"\bxd+\b", r"\bholy\b", r"\binsane\b", r"\bbro\b", r"\?\?\?",
    r"!!!", r"😂|🤣|💀|🔥|😭|😱|🤯|‼",
]
_HYPE_RE = re.compile("|".join(HYPE_PATTERNS), re.IGNORECASE)


def message_heat(text: str, weight: float = 1.0) -> float:
    bonus = 0.5 if _HYPE_RE.search(text or "") else 0.0
    if text and len(text) >= 4 and text.isupper():
        bonus += 0.25  # ALL CAPS
    return weight + bonus


@dataclass
class Spike:
    peak: float      # time of peak chat activity (seconds)
    score: float     # robust z-score at the peak
    rate: float      # smoothed heat at the peak (per second)
    start: float = 0.0
    end: float = 0.0


class HeatTracker:
    """Streaming smoothed-rate + rolling-baseline scorer (one call per second)."""

    def __init__(self, window: int = 10, baseline: int = 300):
        self.window = deque(maxlen=window)
        self.history = deque(maxlen=baseline)

    def push(self, heat_this_second: float) -> tuple[float, float, float]:
        """Returns (smoothed rate, z-score, jump = rate / normal rate)."""
        self.window.append(heat_this_second)
        rate = sum(self.window) / len(self.window)
        if len(self.history) >= 30:
            hist = list(self.history)
            med = median(hist)
            mad = median(abs(x - med) for x in hist)
            spread = max(1.4826 * mad, 0.25 * med, 0.2)
            z = (rate - med) / spread
            jump = rate / max(med, 0.05)
        else:
            z, jump = 0.0, 1.0
        self.history.append(rate)
        return rate, z, jump


class LiveSpikeDetector:
    """Decides, second by second, when a live chat spike should become a clip.

    Rules, so a marathon with a huge, always-busy chat is not clipped end to end:
      * the smoothed chat rate must clear the z-score threshold AND be at least
        `min_jump` times the stream's normal rate (last `baseline` seconds);
      * after a clip, chat must calm back down (z < 1 for 20 s) before another spike
        counts, so one long hype wave gives one clip, not one a minute;
      * at least `min_gap` seconds between clips, and at most `per_hour` clips in
        any rolling hour (0 = no limit).
    push() returns events: ("spike", sec, z, rate, jump), ("skip", sec, count) or
    ("clip", peak_sec, z).
    """

    CALM_Z, CALM_SECS = 1.0, 20

    def __init__(self, *, window=10, baseline=300, threshold=3.5, min_rate=0.3, min_jump=2.0,
                 min_gap=60, per_hour=5):
        self.tracker = HeatTracker(window, baseline)
        self.window, self.threshold, self.min_rate, self.min_jump = window, threshold, min_rate, min_jump
        self.min_gap, self.per_hour = min_gap, per_hour
        self.spike = None  # [start_sec, peak_sec, peak_z]
        self.last_peak = -1e9
        self.calm, self.calm_secs = True, 0
        self.clip_times: list[float] = []
        self.limit_noted = False
        self.last = (0.0, 0.0, 1.0)  # (rate, z, jump) of the latest second

    def push(self, sec: int, heat: float) -> list[tuple]:
        rate, z, jump = self.last = self.tracker.push(heat)
        events: list[tuple] = []
        if not self.calm:
            self.calm_secs = self.calm_secs + 1 if z < self.CALM_Z else 0
            self.calm = self.calm_secs >= self.CALM_SECS
        hot = z >= self.threshold and rate >= self.min_rate and jump >= self.min_jump
        if self.spike is None:
            if hot and self.calm and sec - self.last_peak >= self.min_gap:
                self.clip_times = [t for t in self.clip_times if sec - t < 3600]
                if self.per_hour and len(self.clip_times) >= self.per_hour:
                    if not self.limit_noted:
                        events.append(("skip", sec, len(self.clip_times)))
                        self.limit_noted = True
                else:
                    self.limit_noted = False
                    self.spike = [sec, sec, z]
                    events.append(("spike", sec, z, rate, jump))
        else:
            if z > self.spike[2]:
                self.spike[1], self.spike[2] = sec, z
            if z < self.threshold / 2 or sec - self.spike[0] > 30:
                start, peak, peak_z = self.spike
                self.last_peak = peak
                self.clip_times.append(start)
                self.calm, self.calm_secs = False, 0
                self.spike = None
                # the smoothing window trails, so the burst centre is ~window/2 earlier
                events.append(("clip", peak - self.window / 2, peak_z))
        return events


def per_second(points: Iterable[tuple[float, float]], t0: float, length: int) -> list[float]:
    bins = [0.0] * max(length, 1)
    for t, h in points:
        i = int(t - t0)
        if 0 <= i < len(bins):
            bins[i] += h
    return bins


def find_spikes(
    bins: list[float],
    *,
    window: int = 10,
    baseline: int = 300,
    threshold: float = 3.0,
    min_gap: int = 45,
    top: int = 10,
    min_rate: float = 0.3,
    min_jump: float = 1.0,
) -> list[Spike]:
    """Offline spike search over a whole video's per-second heat.

    A spike must clear the z-score threshold AND be at least `min_jump` times the
    normal chat rate, so a chat that is busy all the time is not clipped constantly.
    """
    tracker = HeatTracker(window, baseline)
    scored = [tracker.push(h) for h in bins]

    # The first minutes have no baseline yet; score them against the global median instead.
    if len(scored) > 30:
        rates = sorted(s[0] for s in scored)
        med = rates[len(rates) // 2]
        mad = sorted(abs(r - med) for r in rates)[len(rates) // 2]
        spread = max(1.4826 * mad, 0.25 * med, 0.2)
        for i in range(min(baseline // 2, len(scored))):
            r = scored[i][0]
            scored[i] = (r, (r - med) / spread, r / max(med, 0.05))

    candidates = []
    for i, (rate, z, jump) in enumerate(scored):
        if z < threshold or rate < min_rate or jump < min_jump:
            continue
        lo, hi = max(0, i - window), min(len(scored), i + window + 1)
        if z >= max(s[1] for s in scored[lo:hi]):
            # smoothing window is trailing, so the burst centre is ~window/2 earlier
            candidates.append(Spike(peak=max(0, i - window / 2), score=z, rate=rate))

    candidates.sort(key=lambda s: s.score, reverse=True)
    chosen: list[Spike] = []
    for s in candidates:
        if all(abs(s.peak - c.peak) >= min_gap for c in chosen):
            chosen.append(s)
        if len(chosen) >= top:
            break
    return sorted(chosen, key=lambda s: s.peak)


def spikes_from_curve(curve: list[tuple[float, float]], top: int, min_gap: int) -> list[Spike]:
    """Peaks of a pre-computed engagement curve (e.g. YouTube 'Most replayed')."""
    if not curve:
        return []
    vals = [v for _, v in curve]
    med = median(vals)
    peaks = []
    for i, (t, v) in enumerate(curve):
        left = curve[i - 1][1] if i else -1
        right = curve[i + 1][1] if i + 1 < len(curve) else -1
        if v >= left and v >= right and v > med:
            peaks.append(Spike(peak=t, score=v / (med or 1e-6), rate=v))
    peaks.sort(key=lambda s: s.score, reverse=True)
    chosen: list[Spike] = []
    for s in peaks:
        if all(abs(s.peak - c.peak) >= min_gap for c in chosen):
            chosen.append(s)
        if len(chosen) >= top:
            break
    return sorted(chosen, key=lambda s: s.peak)


def clip_window(peak: float, pre: float, post: float, duration: float | None = None) -> tuple[float, float]:
    """Chat reacts *after* the moment, so most of the clip is before the peak."""
    start = max(0.0, peak - pre)
    end = peak + post
    if duration:
        end = min(end, duration)
    return start, end
