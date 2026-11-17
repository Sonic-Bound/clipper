"""Locating ffmpeg and running yt-dlp / ffmpeg as subprocesses."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path


def _expose_deno() -> None:
    """YouTube extraction needs a JS runtime; put the pip-installed deno on PATH for yt-dlp."""
    from .runtime import bundled_bin_dir

    bundled = bundled_bin_dir()
    if bundled and (bundled / "deno.exe").exists():
        os.environ["PATH"] = str(bundled) + os.pathsep + os.environ.get("PATH", "")
        return
    if shutil.which("deno"):
        return
    try:
        import deno

        os.environ["PATH"] = str(Path(deno.find_deno_bin()).parent) + os.pathsep + os.environ.get("PATH", "")
    except Exception:  # noqa: BLE001
        print("  ! no JavaScript runtime found; run: pip install deno  (YouTube may fail without it)")


_expose_deno()


@lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    """System ffmpeg if installed, otherwise the bundled imageio-ffmpeg binary."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def run(cmd: list[str], cwd: Path | None = None, quiet: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE if quiet else None,
        stderr=subprocess.PIPE if quiet else None,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-2000:]
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd[:4])} ...\n{tail}")
    return proc


def ytdlp_cmd(*args: str) -> list[str]:
    """yt-dlp invoked through this interpreter, with our ffmpeg wired in."""
    from .runtime import module_cmd

    return [*module_cmd("yt_dlp"), "--ffmpeg-location", ffmpeg_exe(), "--no-warnings", *args]


def ytdlp_popen(*args: str, **kw) -> subprocess.Popen:
    return subprocess.Popen(ytdlp_cmd(*args), **kw)
