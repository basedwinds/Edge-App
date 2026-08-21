# Registers the watchdog Scheduled Task. Re-runnable: unregisters first.
#
# WHY A WATCHDOG AND NOT JUST THE STARTUP SHORTCUT. The shortcut fires at LOGON.
# This machine is left running for days, so a service that dies at 2am stays dead
# until the next login -- exactly what happened to the frontend on 2026-08-18.
# Logon-only does not match how the machine is used.
#
# SAFE AT A 10-MINUTE INTERVAL because start_edge_app.ps1 is idempotent: it checks
# ports 8756 and 5173 independently and starts only what is missing. A run with
# both healthy logs "nothing to do" and exits.
#
# BUILT FROM RAW XML, not New-ScheduledTaskTrigger, and that is not a style
# choice. The cmdlets cannot express "repeat forever": [TimeSpan]::MaxValue is
# rejected ("P99999999DT23H59M59S out of range") and [TimeSpan]::Zero is rejected
# ("PT0S"). In the XML schema you get indefinite repetition by OMITTING
# <Duration> entirely, which no cmdlet parameter can produce.
$ErrorActionPreference = "Stop"
$name   = "EdgeAppWatchdog"
$script = Join-Path $PSScriptRoot "start_edge_app.ps1"
$user   = "$env:USERDOMAIN\$env:USERNAME"

Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue

# THE TIME TRIGGER IS THE ONE THAT ACTUALLY KEEPS THIS ALIVE.
#
# The LogonTrigger arms its repetition only AT LOGON. This machine is left up for
# days, so once that logon is behind you the task goes dormant and
# Get-ScheduledTaskInfo reports an EMPTY NextRunTime. Observed 2026-08-21:
# State "Ready", LastResult 0, LastRunTime 08/18 07:51, NextRunTime blank -- and
# the app had been down for hours with the watchdog sitting there doing nothing.
# That is the exact failure this file set out to prevent ("a service that dies at
# 2am stays dead until the next login"); the logon-only trigger reintroduced it.
#
# The CalendarTrigger's StartBoundary is deliberately in the PAST so registering
# arms it immediately instead of at the next midnight, and <Duration> is omitted
# so the repetition runs forever (same trick as the logon one).
#
# NOTE FOR ANYONE EDITING THE XML BELOW: an XML comment may not contain a double
# hyphen. Putting this note inside the here-string is what made
# Register-ScheduledTask fail with "task XML is malformed" -- and because the
# script unregisters FIRST, that failure left the machine with NO watchdog at all.
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Restarts the edge app backend/frontend if either has stopped. Runs at logon and every 10 minutes.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
      <Repetition>
        <Interval>PT10M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </CalendarTrigger>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$user</UserId>
      <Repetition>
        <Interval>PT10M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$user</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "$script"</Arguments>
    </Exec>
  </Actions>
</Task>
"@

Register-ScheduledTask -TaskName $name -Xml $xml | Out-Null
$t = Get-ScheduledTask -TaskName $name
Write-Output ("REGISTERED: {0}   state={1}" -f $t.TaskName, $t.State)
foreach ($tr in $t.Triggers) { Write-Output ("  repeats every: " + $tr.Repetition.Interval + "  (blank Duration = forever)") }
