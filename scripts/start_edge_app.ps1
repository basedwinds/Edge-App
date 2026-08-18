# Standalone launcher -- the whole point is that NOTHING owns this but Windows.
#
# WHY THIS EXISTS. Until 2026-08-17 the backend and frontend were started via
# Claude Code's preview system, which makes them CHILD PROCESSES of claude.exe:
#
#     python.exe (backend)
#       └─ powershell.exe  run_backend.ps1
#           └─ claude.exe          <-- close this and everything below dies
#
# So the app only ran while that editor was open. That is what caused the
# 35-hour blackout of 2026-08-16/17: ~2,700 auto-logged paper bets never
# recorded, and 25 real tracked bets sat ungraded until settlement was run by
# hand. The advice "just leave the machine on" was wrong -- the machine WAS on.
#
# Launched from the Startup folder, this is owned by the logon session instead,
# so it survives closing Claude Code, and comes back after a reboot.
#
# Hidden windows, deliberately: this runs every login and nobody wants two
# consoles appearing. Output still goes to the log files below.
$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null

# ALREADY-RUNNING CHECK, so a double login or a manual run cannot start a
# SECOND backend against the same SQLite file. Two instances both polling and
# both writing is the "unkillable worker" shape that has bitten this app before.
# EACH SERVICE IS CHECKED INDEPENDENTLY. The first version exited outright when
# the backend was already up, which meant a dead FRONTEND could never be
# recovered by re-running this -- the one case where you would actually run it
# by hand. Skip what is running, start what is not.
$busy = Get-NetTCPConnection -LocalPort 8756 -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    Add-Content -Path (Join-Path $logs "launcher.log") -Value "$(Get-Date -f s)  port 8756 already listening -- not starting a second backend"
} else {
    Start-Process powershell -WindowStyle Hidden -ArgumentList @(
        "-ExecutionPolicy","Bypass","-File",(Join-Path $PSScriptRoot "run_backend.ps1")
    ) -RedirectStandardOutput (Join-Path $logs "backend.out.log") `
      -RedirectStandardError  (Join-Path $logs "backend.err.log")
    Start-Sleep -Seconds 5
}

# SAME GUARD FOR THE FRONTEND, and it is not merely symmetry. Vite does NOT fail
# when its port is taken -- it says "Port 5173 is in use, trying another one..."
# and quietly binds 5174. Observed doing exactly that on 2026-08-17. The result
# is two UIs on two ports, and no way to tell from the browser which one you are
# looking at or how stale it is. A second BACKEND would corrupt data; a second
# FRONTEND just lies to you, which is not much better.
$feBusy = Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue
if ($feBusy) {
    Add-Content -Path (Join-Path $logs "launcher.log") -Value "$(Get-Date -f s)  port 5173 already listening -- not starting a second frontend"
} else {
    Start-Process powershell -WindowStyle Hidden -ArgumentList @(
        "-ExecutionPolicy","Bypass","-File",(Join-Path $PSScriptRoot "run_frontend.ps1")
    ) -RedirectStandardOutput (Join-Path $logs "frontend.out.log") `
      -RedirectStandardError  (Join-Path $logs "frontend.err.log")
}

# Says what it ACTUALLY did. The first version logged "started backend +
# frontend" unconditionally, including on runs where both guards fired and it
# started nothing -- a log that reports an action it did not take is worse
# than no log, because you trust it.
$did = @()
if (-not $busy)   { $did += "backend" }
if (-not $feBusy) { $did += "frontend" }
$what = if ($did.Count) { "started " + ($did -join " + ") } else { "nothing to do -- both already running" }
Add-Content -Path (Join-Path $logs "launcher.log") -Value "$(Get-Date -f s)  $what"
