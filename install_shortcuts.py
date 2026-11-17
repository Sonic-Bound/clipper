"""Adds 'Clipper' shortcuts to the Desktop and Start menu.  Run: .venv\\Scripts\\python install_shortcuts.py"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
EXE = ROOT / "dist" / "Clipper" / "Clipper.exe"  # built by: python -m PyInstaller Clipper.spec
ICON = ROOT / "clipper" / "web" / "clipper.ico"


def folder(name: str) -> Path:
    # ask Windows (the Desktop may be redirected, e.g. into OneDrive)
    out = subprocess.run(["powershell", "-NoProfile", "-Command", f"[Environment]::GetFolderPath('{name}')"],
                         capture_output=True, text=True).stdout.strip()
    return Path(out)


def make(lnk: Path) -> None:
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);"
        "$s.TargetPath=$env:TGT; $s.Arguments='-m clipper.desktop'; $s.WorkingDirectory=$env:WD;"
        "$s.IconLocation=$env:ICO; $s.Description='Clip YouTube chat spikes with captions'; $s.Save()"
    )
    if EXE.exists():  # prefer the built exe; fall back to running from source
        ps = ps.replace("$s.Arguments='-m clipper.desktop';", "$s.Arguments='';")
        env = dict(os.environ, LNK=str(lnk), TGT=str(EXE), WD=str(EXE.parent), ICO=str(EXE))
    else:
        env = dict(os.environ, LNK=str(lnk), TGT=str(PYW), WD=str(ROOT), ICO=str(ICON))
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env, check=True)
    print("created", lnk)


if __name__ == "__main__":
    make(folder("Desktop") / "Clipper.lnk")
    make(folder("Programs") / "Clipper.lnk")  # Start menu
