"""Speech-to-text with faster-whisper and short-form style ASS/SRT captions."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass
class Word:
    start: float
    end: float
    text: str


@lru_cache(maxsize=2)
def _model(name: str, device: str):
    from faster_whisper import WhisperModel

    compute = "float16" if device == "cuda" else "int8"
    print(f"  loading whisper model '{name}' on {device} (first run downloads it)...")
    return WhisperModel(name, device=device, compute_type=compute)


def _load_audio(media: Path):
    """16 kHz mono float32 via ffmpeg (avoids faster-whisper's PyAV version coupling)."""
    import subprocess

    import numpy as np

    from .tools import ffmpeg_exe

    pcm = subprocess.run(
        [ffmpeg_exe(), "-nostdin", "-i", str(media), "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
    ).stdout
    return np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0


def transcribe(media: Path, model: str = "small", device: str = "auto", language: str | None = None) -> list[Word]:
    if device == "auto":
        try:
            import ctranslate2

            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:  # noqa: BLE001
            device = "cpu"
    audio = _load_audio(media)
    words: list[Word] = []
    # VAD skips silence/noise but also throws away singing; fall back to no VAD if it finds nothing
    for vad in (True, False):
        segments, _ = _model(model, device).transcribe(
            audio, word_timestamps=True, vad_filter=vad, language=language
        )
        for seg in segments:
            for w in seg.words or []:
                text = w.word.strip()
                if not text:
                    continue
                # whisper splits "$250,000" / "it's" into pieces; glue a piece that continues
                # the previous word (no leading space, starts with punctuation) back onto it
                if words and not w.word.startswith(" ") and not text[0].isalnum():
                    words[-1].text += text
                    words[-1].end = w.end
                else:
                    words.append(Word(w.start, w.end, text))
        if words:
            break
    return words


def chunk_words(words: list[Word], max_words: int = 3, max_gap: float = 0.6, max_chars: int = 18) -> list[list[Word]]:
    """Group words into short punchy caption lines."""
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        too_long = len(" ".join(x.text for x in cur + [w])) > max_chars
        if cur and (len(cur) >= max_words or w.start - cur[-1].end > max_gap or too_long
                    or cur[-1].text[-1:] in ".?!"):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _srt_time(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ass_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def write_ass(words: list[Word], path: Path, vertical: bool, highlight: str = "00FFFF", uppercase: bool = True) -> None:
    """Captions with the currently spoken word highlighted (highlight is ASS BGR hex)."""
    w, h = (1080, 1920) if vertical else (1920, 1080)
    size = 80 if vertical else 72
    # Every caption is pinned to one fixed point: centre-anchored \pos events are never
    # moved by libass collision handling, so the text cannot drift up or down.
    x, y = w // 2, int(h * 0.70) if vertical else int(h * 0.85)
    pin = f"\\an5\\pos({x},{y})"
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Arial Black,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,3,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    # One event per spoken word: (start, end, text with that word coloured).
    events: list[tuple[float, float, str]] = []
    for chunk in chunk_words(words, max_chars=16 if vertical else 24):
        texts = [_ass_escape(x.text.upper() if uppercase else x.text) for x in chunk]
        for i, word in enumerate(chunk):
            end = chunk[i + 1].start if i + 1 < len(chunk) else word.end + 0.15
            # colour only: scaling the active word changed the line height and made it bounce
            parts = [f"{{\\c&H{highlight}&}}{t}{{\\c&HFFFFFF&}}" if j == i else t for j, t in enumerate(texts)]
            events.append((word.start, end, " ".join(parts)))

    lines = [header]
    for k, (start, end, text) in enumerate(events):
        if k + 1 < len(events):
            end = min(end, events[k + 1][0])  # never overlap the next caption (overlaps get stacked)
        if end <= start:
            continue
        lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Cap,,0,0,0,,{{{pin}}}{text}\n")
    path.write_text("".join(lines), encoding="utf-8")


def write_srt(words: list[Word], path: Path) -> None:
    out = []
    for n, chunk in enumerate(chunk_words(words, max_words=7, max_chars=42), 1):
        out.append(f"{n}\n{_srt_time(chunk[0].start)} --> {_srt_time(chunk[-1].end)}\n"
                   f"{' '.join(w.text for w in chunk)}\n")
    path.write_text("\n".join(out), encoding="utf-8")
