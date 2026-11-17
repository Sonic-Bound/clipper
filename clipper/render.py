"""Getting clip footage and burning captions into it."""

from __future__ import annotations

from pathlib import Path

from .captions import transcribe, write_ass, write_srt
from .tools import ffmpeg_exe, run, ytdlp_cmd

FORMAT = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b"


def download_section(url: str, start: float, end: float, out: Path) -> Path:
    """Download only [start, end] of a YouTube video."""
    out.parent.mkdir(parents=True, exist_ok=True)
    for old in out.parent.glob(out.stem + ".*"):
        old.unlink()
    run(ytdlp_cmd(
        "-f", FORMAT, "--merge-output-format", "mp4",
        "--download-sections", f"*{start:.2f}-{end:.2f}", "--force-keyframes-at-cuts",
        "-o", str(out.with_suffix(".%(ext)s")), url,
    ))
    files = [p for p in out.parent.glob(out.stem + ".*") if p.suffix in (".mp4", ".mkv", ".webm")]
    if not files:
        raise RuntimeError("yt-dlp produced no file for section")
    return files[0]


def cut_local(src: Path, start: float, end: float, out: Path) -> Path:
    """Cut a section out of a local (possibly still-growing) recording."""
    out.parent.mkdir(parents=True, exist_ok=True)
    run([ffmpeg_exe(), "-y", "-ss", f"{start:.2f}", "-i", str(src), "-t", f"{end - start:.2f}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
         str(out)])
    return out


def finish_clip(
    raw: Path,
    final: Path,
    *,
    captions: bool = True,
    vertical: bool = True,
    whisper_model: str = "small",
    device: str = "auto",
    language: str | None = None,
) -> Path:
    """Transcribe the raw clip and render the final video with burned-in captions."""
    workdir = final.parent
    vf = []
    if vertical:
        # centre crop to 9:16, then scale to 1080x1920
        vf.append("crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',scale=1080:1920:flags=lanczos,setsar=1")
    if captions:
        words = transcribe(raw, whisper_model, device, language)
        ass = final.with_suffix(".ass")
        write_ass(words, ass, vertical)
        write_srt(words, final.with_suffix(".srt"))
        if words:
            # relative filename + cwd avoids Windows drive-letter escaping in the filter
            vf.append(f"subtitles={ass.name}")
    cmd = [ffmpeg_exe(), "-y", "-i", str(raw.resolve())]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", f"_rendering_{final.name}"]
    run(cmd, cwd=workdir)
    # rename only when complete, so the app never shows a half-written clip
    (workdir / f"_rendering_{final.name}").replace(final)
    return final
