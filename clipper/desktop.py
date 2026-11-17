"""Clipper as a desktop app: its own window (Edge WebView2), no browser.

Run with:  pythonw -m clipper.desktop   (Clipper.bat / the desktop shortcut do this)
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
import traceback
from pathlib import Path

from . import app as server

ICON = Path(__file__).resolve().parent / "web" / "clipper.ico"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Clipper"
LOG = DATA_DIR / "clipper.log"


def _log(msg: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def _message_box(text: str) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, text, "Clipper", 0x10)


def _start_server() -> str:
    port = server.free_port()
    srv = server.make_server(port)
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):  # wait until it accepts connections
        if srv.started:
            break
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}"


def _set_window_icon(window) -> None:
    """pywebview has no window-icon option on Windows, so set it on the WinForms form."""
    try:
        import clr  # noqa: F401  (pythonnet, used by pywebview)
        from System import Action
        from System.Drawing import Icon

        form = window.native
        form.Invoke(Action(lambda: setattr(form, "Icon", Icon(str(ICON)))))
    except Exception as e:  # noqa: BLE001
        _log(f"icon: {e}")


def main() -> None:
    if os.name == "nt":
        # own taskbar identity, so Windows shows Clipper's icon instead of Python's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Clipper.Desktop")
    url = _start_server()

    try:
        import webview
    except ImportError:
        import webbrowser

        webbrowser.open(url)
        input("pywebview is not installed; opened in the browser instead. Press Enter to quit.")
        return

    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True  # "Open on YouTube" goes to the browser

    window = webview.create_window(
        "Clipper", url, width=1240, height=880, min_size=(420, 560), background_color="#0d0f14",
        text_select=True,
    )

    def on_closing():
        busy = server.running_jobs()
        if not busy:
            return True
        live = any(j.kind == "live" for j in busy)
        msg = (f"{len(busy)} job{'s are' if len(busy) > 1 else ' is'} still running"
               + (" (including a live stream)" if live else "")
               + ".\n\nQuit anyway? Clips that are not finished will be lost.")
        return window.create_confirmation_dialog("Quit Clipper?", msg)

    window.events.closing += on_closing
    window.events.shown += lambda: _set_window_icon(window)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # private_mode=False + storage_path keeps the page's saved settings between launches
    webview.start(gui="edgechromium", private_mode=False, storage_path=str(DATA_DIR / "webview"))
    server.shutdown_jobs()


def run() -> None:
    try:
        main()
    except Exception:  # noqa: BLE001
        _log(traceback.format_exc())
        _message_box(f"Clipper could not start.\n\nDetails were written to:\n{LOG}")
        sys.exit(1)


if __name__ == "__main__":
    run()
