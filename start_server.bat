@echo off
REM Double-click this to (re)start the Music Downloader web UI.
REM Shows a console window with live server output — if it can't start
REM (e.g. port 8787 already in use), you'll see the error instead of
REM nothing happening. The window closes itself as soon as the server
REM process stops, for any reason (crash, port conflict, or you closing it).
REM Once it's running, use http://100.122.54.14:8787
cd /d "%~dp0"
"C:\Users\Oliver\AppData\Local\Programs\Python\Python313\python.exe" web_server.py
