@echo off
rem Opens the Clipper desktop app (its own window, no console).
start "" "%~dp0.venv\Scripts\pythonw.exe" -m clipper.desktop
