"""Differences between running from source and running as the frozen Clipper.exe."""

from __future__ import annotations

import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
SOURCE_ROOT = Path(__file__).resolve().parent.parent


def module_cmd(module: str) -> list[str]:
    """Command that runs `python -m <module>`; inside the exe, the exe runs it itself."""
    if FROZEN:
        return [sys.executable, "--module", module]
    return [sys.executable, "-u", "-m", module]


def clips_dir() -> Path:
    """Where clips are saved: the project's clips/ from source, Videos\\Clipper for the exe."""
    if FROZEN:
        return Path.home() / "Videos" / "Clipper"
    return SOURCE_ROOT / "clips"


def bundled_bin_dir() -> Path | None:
    """Folder with helper programs shipped inside the exe (deno)."""
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "bin"
    return None
