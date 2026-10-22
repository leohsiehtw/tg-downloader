@echo off
chcp 65001 >nul
cd /d %~dp0
where py >nul 2>nul || (echo Please install Python first: https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^) & pause & exit /b 1)
where ffmpeg >nul 2>nul || winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
if not exist "%ProgramFiles%\7-Zip\7z.exe" winget install -e --id 7zip.7zip --accept-source-agreements --accept-package-agreements
py -m pip install -q -U --disable-pip-version-check -r requirements.txt
py configure.py
pause
