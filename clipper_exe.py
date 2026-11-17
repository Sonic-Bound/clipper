"""Entry point of Clipper.exe.

  Clipper.exe                          -> opens the desktop app
  Clipper.exe --module clipper ARGS    -> runs the clipper CLI (the app starts jobs this way)
  Clipper.exe --module yt_dlp ARGS     -> runs yt-dlp (the clipper downloads this way)
"""

import os
import sys


def _attach_std_streams() -> None:
    """A windowed exe starts without stdout/stderr; hook up the pipes the parent passed, if any."""
    if os.name != "nt":
        return
    import ctypes
    import msvcrt

    k32 = ctypes.windll.kernel32
    k32.GetStdHandle.restype = ctypes.c_void_p
    for name, std in (("stdout", -11), ("stderr", -12)):
        stream = getattr(sys, name)
        if stream is not None:
            try:
                stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            except Exception:  # noqa: BLE001
                pass
            continue
        handle = k32.GetStdHandle(std)
        try:
            if handle in (None, 0, ctypes.c_void_p(-1).value):
                raise OSError
            fd = msvcrt.open_osfhandle(handle, os.O_WRONLY)
            new = open(fd, "w", encoding="utf-8", errors="replace", buffering=1)
        except OSError:
            new = open(os.devnull, "w", encoding="utf-8")
        setattr(sys, name, new)


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "--module":
        module = sys.argv[2]
        sys.argv = [module, *sys.argv[3:]]
        _attach_std_streams()
        if module == "clipper":
            from clipper.__main__ import main as clipper_main

            raise SystemExit(clipper_main())
        if module == "yt_dlp":
            import yt_dlp

            yt_dlp.main()
            return
        raise SystemExit(f"unknown module: {module}")

    from clipper.desktop import run

    run()


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    main()
