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
    # KEEP THE DEAD PROCESS'S LOG. -RedirectStandardError TRUNCATES, so the
    # restart that recovers the app also destroys the only record of why it
    # died. Found the hard way on 2026-08-22: the backend was down ~7.6 hours
    # across a live sprint, and by the time anyone looked, the log had been
    # overwritten by the very restart that fixed it. Roll it aside first and
    # keep the last few.
    $errLog = Join-Path $logs "backend.err.log"
    if ((Test-Path $errLog) -and (Get-Item $errLog).Length -gt 0) {
        Move-Item $errLog (Join-Path $logs ("backend.err.{0:yyyyMMdd-HHmmss}.log" -f (Get-Date))) -Force -ErrorAction SilentlyContinue
        Get-ChildItem (Join-Path $logs "backend.err.*.log") -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -Skip 5 |
            Remove-Item -Force -ErrorAction SilentlyContinue
    }
    Start-Process powershell -WindowStyle Hidden -ArgumentList @(
        "-ExecutionPolicy","Bypass","-File",(Join-Path $PSScriptRoot "run_backend.ps1")
    ) -RedirectStandardOutput (Join-Path $logs "backend.out.log") `
      -RedirectStandardError  $errLog
    Start-Sleep -Seconds 5
}

# SELF-HEAL THE WATCHDOG.
#
# On 2026-08-22 the EdgeAppWatchdog task was found DISABLED. Nothing else was:
# all eight other Edge* tasks were Ready. It had last run at 00:35 and the
# backend died some time after, so nothing recovered it for ~7.6 hours -- across
# the F1 sprint, which meant no pricing, no paper logging and no settlement while
# a real bet was live. The Task Scheduler operational log is disabled on this
# machine, so the cause is not recoverable; what IS fixable is that the recovery
# mechanism had a single point of failure and no way to notice its own absence.
#
# Any run of this script -- from the watchdog, the backstop task, the Startup
# shortcut, or by hand -- now repairs the task. Cheap, idempotent, and it means
# the next login heals it even if every scheduled path is dead.
foreach ($wd in "EdgeAppWatchdog", "EdgeAppWatchdogBackstop") {
    try {
        $t = Get-ScheduledTask -TaskName $wd -ErrorAction Stop
        if ($t.State -eq "Disabled") {
            Enable-ScheduledTask -TaskName $wd -ErrorAction Stop | Out-Null
            Add-Content -Path (Join-Path $logs "launcher.log") -Value "$(Get-Date -f s)  WARNING $wd was DISABLED -- re-enabled it"
        }
    } catch { }
}

# FRONTEND: REACHABILITY, NOT JUST A BOUND PORT.
#
# The original guard asked only whether something was LISTENING on 5173, which
# is a weaker question than it looks. On 2026-08-21 the user reported "the
# backend/frontend are not showing" while BOTH were up: Vite had bound
# [::1]:5173 -- the IPv6 loopback -- and nothing at all on 127.0.0.1, so the
# page loaded at localhost and was refused at 127.0.0.1. A port check passes
# happily in that state, and in any state where the server is bound but wedged.
# So probe it like a browser would.
#
# Vite does also quietly bind 5174 when 5173 is taken (observed 2026-08-17),
# which is the other reason this cannot just be "is the port free" -- that is
# now additionally guarded by strictPort in vite.config.ts.
function Test-Reachable($url, $timeoutSec) {
    try {
        # -UseBasicParsing: without it this wants IE's DOM engine and can hang
        # on a machine where IE was never initialised.
        $r = Invoke-WebRequest -Uri $url -TimeoutSec $timeoutSec -UseBasicParsing -ErrorAction Stop
        return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500)
    } catch { return $false }
}

# BOTH loopback spellings count as healthy. Probing only 127.0.0.1 would call an
# IPv6-only bind "wedged" and kill it on every pass -- a restart loop that fights
# a config problem instead of reporting it. vite.config.ts pins IPv4, so this is
# belt and braces, not the fix.
$feUrls = @("http://127.0.0.1:5173/", "http://localhost:5173/")
$feOk = $false
for ($try = 1; $try -le 3 -and -not $feOk; $try++) {
    foreach ($u in $feUrls) { if (Test-Reachable $u 10) { $feOk = $true; break } }
    if (-not $feOk -and $try -lt 3) { Start-Sleep -Seconds 3 }
}

$feBusy = Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue
$feAction = "healthy"
if (-not $feOk) {
    # Bound but not answering: the process is wedged, so free the port first --
    # otherwise the new Vite finds 5173 taken and strictPort makes it exit.
    # Killing a dev server costs nothing; it holds no state and writes no data.
    # (The BACKEND is deliberately NOT treated this way -- see below.)
    if ($feBusy) {
        foreach ($c in $feBusy) {
            $tree = @($c.OwningProcess)
            try {
                $kids = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($c.OwningProcess)" -ErrorAction Stop
                foreach ($k in $kids) { $tree += $k.ProcessId }
            } catch { }
            foreach ($procId in $tree) { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue }
        }
        Add-Content -Path (Join-Path $logs "launcher.log") -Value "$(Get-Date -f s)  frontend BOUND BUT UNREACHABLE -- killed it to restart"
        Start-Sleep -Seconds 4
    }
    Start-Process powershell -WindowStyle Hidden -ArgumentList @(
        "-ExecutionPolicy","Bypass","-File",(Join-Path $PSScriptRoot "run_frontend.ps1")
    ) -RedirectStandardOutput (Join-Path $logs "frontend.out.log") `
      -RedirectStandardError  (Join-Path $logs "frontend.err.log")
    $feAction = "started"
}

# BACKEND: REPORTED, NEVER AUTO-KILLED, and the asymmetry is deliberate.
#
# It owns the SQLite database, runs nine pollers, and is legitimately slow at
# times -- a cold start and a mid-reload /health have both been measured in the
# tens of seconds, and some routes take 110-178s. A probe-and-kill rule would
# eventually shoot a HEALTHY backend in the middle of a write, and two instances
# against one SQLite file is the failure this script exists to prevent. A dead
# backend is already handled: the port is free, so the guard above starts one.
# An unreachable-but-listening backend is a human decision, so say so loudly.
if ($busy) {
    if (Test-Reachable "http://127.0.0.1:8756/health" 30) {
        Add-Content -Path (Join-Path $logs "launcher.log") -Value "$(Get-Date -f s)  backend healthy"
    } else {
        Add-Content -Path (Join-Path $logs "launcher.log") -Value "$(Get-Date -f s)  WARNING backend is listening but /health did not answer in 30s -- NOT killed (may be a cold start or a long poll); investigate if this repeats"
    }
}

# Says what it ACTUALLY did. The first version logged "started backend +
# frontend" unconditionally, including on runs where both guards fired and it
# started nothing -- a log that reports an action it did not take is worse
# than no log, because you trust it.
$did = @()
if (-not $busy)     { $did += "backend" }
if ($feAction -eq "started") { $did += "frontend" }
if ($did.Count) { $what = "started " + ($did -join " + ") } else { $what = "nothing to do -- both already running" }
Add-Content -Path (Join-Path $logs "launcher.log") -Value "$(Get-Date -f s)  $what"
