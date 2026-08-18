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

$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Restarts the edge app backend/frontend if either has stopped. Runs at logon and every 10 minutes.</Description>
  </RegistrationInfo>
  <Triggers>
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
