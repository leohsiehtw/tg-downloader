@echo off
call "%~dp0stop.bat" >nul
timeout /t 2 /nobreak >nul
start "" wscript "%~dp0start-hidden.vbs"
py -X utf8 -c "print('\u5df2\u5728\u80cc\u666f\u91cd\u65b0\u555f\u52d5\uff0c\u53f3\u4e0b\u89d2\u6703\u51fa\u73fe\u5716\u793a\u3002\u7d00\u9304\u5728 bot.log\u3002')"
timeout /t 3 >nul
