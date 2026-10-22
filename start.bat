@echo off
chcp 65001 >nul
cd /d %~dp0
py -m pip install -q -U --disable-pip-version-check yt-dlp gallery-dl
py -X utf8 -u bot.py
pause
