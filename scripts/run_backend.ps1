# Backend launcher.
#
# DEFAULTS TO STABLE (no --reload), which is the opposite of what this script
# used to do, and the change is deliberate.
#
# `--reload` makes uvicorn watch every backend .py file and restart the worker
# on each save. That restart re-warms every rating service from its crawl
# caches, and while it runs, requests just queue. Measured 2026-08-12 right
# after a five-file edit: /health took 45.9s mid-reload, then 1.3s once
# settled; /cfb/markets 0.90s then 0.29s. Nothing was broken -- the app was
# rebooting. This is the documented "--reload gotcha" in CLAUDE.md, and it is
# what "the app is loading extremely slowly sometimes" and "cfb is taking a
# while to warm up" actually were.
#
# The person USING the app gets nothing from --reload; it only helps whoever is
# editing the code, and it actively hurts the user by stalling the app whenever
# a file changes underneath them. So normal use is stable, and reload is opt-in.
#
# Trade-off, stated plainly: in stable mode a code change does NOT take effect
# until the backend is restarted.
param(
    # Watch files and auto-restart. For editing the backend, not for using the app.
    [switch]$Reload
)

Set-Location "$PSScriptRoot\..\backend"

$uvicornArgs = @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8756")
if ($Reload) {
    Write-Host "Backend starting WITH --reload (auto-restarts on file save; expect stalls while it re-warms)." -ForegroundColor Yellow
    $uvicornArgs += "--reload"
} else {
    Write-Host "Backend starting in STABLE mode (no auto-reload). Restart it to pick up code changes." -ForegroundColor Green
}

& ".\.venv\Scripts\python.exe" @uvicornArgs
