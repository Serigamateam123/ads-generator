' Runs "Start Static Ads Generator.bat" with its window completely hidden.
' The .bat still does its port-cleanup + starts ui_server.py, which opens
' exactly one browser tab once the server is ready — no console window,
' no duplicate tabs.
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\haliza.LUXOR\.claude\skills\static-remix"
WshShell.Run """C:\Users\haliza.LUXOR\.claude\skills\static-remix\Start Static Ads Generator.bat""", 0, False
