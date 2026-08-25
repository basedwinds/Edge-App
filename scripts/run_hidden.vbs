' TRULY hidden launcher for start_edge_app.ps1.
'
' powershell.exe -WindowStyle Hidden still CREATES a console window and then
' hides it, which flashes and steals focus. On a machine the user games on,
' a watchdog firing every 5 minutes means a focus steal every 5 minutes --
' user-reported twice on 2026-08-22.
'
' Task Scheduler's "Hidden" setting does NOT help: it hides the task from the
' Task Scheduler LIST, not the window. That misreading is why the first fix
' did not work.
'
' WScript.Shell.Run with intWindowStyle 0 never creates a window in the first
' place, so there is nothing to flash. bWaitOnReturn = False so the task action
' returns immediately.
Dim shell, ps, script
Set shell = CreateObject("WScript.Shell")
ps = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """
script = "C:\Users\awaws\Downloads\nfl-edge-app\scripts\start_edge_app.ps1"""
shell.Run ps & script, 0, False
