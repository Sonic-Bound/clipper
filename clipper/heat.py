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

    def push(self, heat_this_second: float) -> tuple[float, float]:
        """Returns (smoothed rate, z-score)."""
        self.window.append(heat_this_second)
        rate = sum(self.window) / len(self.window)
        if len(self.history) >= 30:
            hist = list(self.history)
            med = median(hist)
            mad = median(abs(x - med) for x in hist)
            spread = max(1.4826 * mad, 0.25 * med, 0.2)
            z = (rate - med) / spread
        else:
            z = 0.0
        self.history.append(rate)
        return rate, z


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
) -> list[Spike]:
    """Offline spike search over a whole video's per-second heat."""
    tracker = HeatTracker(window, baseline)
    scored = [tracker.push(h) for h in bins]

    # The first minutes have no baseline yet; score them against the global median instead.
    if len(scored) > 30:
        rates = sorted(r for r, _ in scored)
        med = rates[len(rates) // 2]
        mad = sorted(abs(r - med) for r in rates)[len(rates) // 2]
        spread = max(1.4826 * mad, 0.25 * med, 0.2)
        for i in range(min(baseline // 2, len(scored))):
            r, _ = scored[i]
            scored[i] = (r, (r - med) / spread)

    candidates = []
    for i, (rate, z) in enumerate(scored):
        if z < threshold or rate < min_rate:
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
