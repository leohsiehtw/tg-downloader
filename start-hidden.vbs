' Starts the tray icon (tray.pyw), which runs bot.py in the background and restarts it if it crashes.
' A shortcut to this file sits in the Startup folder (Win+R, shell:startup).
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
sh.Run "cmd /c py -m pip install -q -U yt-dlp gallery-dl >> bot.log 2>&1", 0, True
sh.Run "pyw tray.pyw", 0, False
