@echo off
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'wscript.exe' -and $_.CommandLine -like '*start-hidden.vbs*') -or ($_.Name -match '^(py|pythonw?)\.exe$' -and ($_.CommandLine -like '*bot.py*' -or $_.CommandLine -like '*tray.pyw*')) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
del "%~dp0.heartbeat" 2>nul
py -X utf8 -c "import time;open(r'%~dp0bot.log','a',encoding='utf-8').write(time.strftime('%%Y-%%m-%%d %%H:%%M:%%S')+' [stop.bat] \u624b\u52d5\u505c\u6b62\n');print('\u6a5f\u5668\u4eba\u5df2\u505c\u6b62\u3002')"
