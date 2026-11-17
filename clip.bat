@echo off
rem Usage: clip.bat <youtube-url> [options]   (see README.md)
"%~dp0.venv\Scripts\python.exe" -m clipper %*
