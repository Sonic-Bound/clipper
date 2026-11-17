# PyInstaller build for Clipper.exe.   Build:  .venv\Scripts\python -m PyInstaller Clipper.spec --noconfirm
# One-folder build on purpose: the app starts itself as a helper process for every job and
# download, and a one-file exe would unpack ~400 MB to a temp folder each of those times.

import deno
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

datas = [("clipper/web", "clipper/web")]
binaries = [(deno.find_deno_bin(), "bin")]
hiddenimports = collect_submodules("uvicorn") + ["clipper.__main__", "clipper.desktop"]

for pkg in ("faster_whisper", "ctranslate2", "imageio_ffmpeg", "yt_dlp_ejs", "webview"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
datas += collect_data_files("certifi")

a = Analysis(
    ["clipper_exe.py"],
    pathex=["."],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Clipper",
    icon="clipper/web/clipper.ico",
    console=False,          # no console window; helper runs get their pipes attached in clipper_exe.py
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Clipper", upx=False)
