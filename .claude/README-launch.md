# Why there is no launch.json here

`launch.json` told Claude Code's preview system how to start this app's backend
(port 8756) and frontend (port 5173). It is retired -- kept as
`launch.json.retired` for reference only.

**The app is no longer started by Claude Code.** It is owned by Windows Task
Scheduler ("EdgeAppWatchdog" -> `scripts/start_edge_app.ps1`), which is the whole
point: as a preview-system child process the app died whenever the editor closed,
and that caused the 35-hour blackout of 2026-08-16/17 (~2,700 paper bets never
logged, 25 real bets ungraded).

**Leaving this file in place actively fought that fix.** With it present, the
preview system treats 8756 and 5173 as ITS ports. Every session that opens a
preview tries to claim them, and reports the running app as an obstacle --
"Port 8756 is required by this server but is in use by python.exe (PID ...).
Stop that process to free port 8756 and try again." Comply once and the
Task-Scheduler-owned server is dead. The frontend is the one that loses in
practice, because the backend gets more scrutiny before anyone kills it.

## What to do instead

* **To view the app:** open a browser tab at the URL -- `preview_start` with
  `{url: "http://127.0.0.1:5173"}`. No dev server needed; one is already running.
* **To restart it:** `Start-ScheduledTask -TaskName EdgeAppWatchdog`, or run
  `scripts/start_edge_app.ps1` directly. It starts only what is missing and now
  probes reachability, so it also recovers a server that is bound but wedged.
* **Never** stop the process holding 8756 or 5173 to "free" the port. That
  process IS the app.
