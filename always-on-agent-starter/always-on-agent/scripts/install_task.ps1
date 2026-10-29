<#
.SYNOPSIS
  Registers (or removes) the Windows Task Scheduler task "Always-On Agent" (prompt 1.17, F01).

.DESCRIPTION
  The task runs `uv run agentd run` from this repository's backend folder, as you, only while you
  are logged on (Outlook, Credential Manager and the token cache are yours):
    - at log-on;
    - when you unlock the workstation (after a sleep the process may have been stopped);
    - every 15 minutes, in case it is not running.
  A second start while agentd runs exits at once (single-instance lock), so these triggers never
  make two agents. If agentd crashes, Task Scheduler restarts it (3 times, 1 minute apart); the
  15-minute trigger covers anything longer. Output goes to logs\agentd.out.log.

  The task needs no administrator rights. It does not wake the laptop: after sleep the agent
  catches up on what it missed (`agentd status` shows "Last wake from sleep").

.EXAMPLE
  .\scripts\install_task.ps1              # install or update
  .\scripts\install_task.ps1 -Uninstall   # remove the task (agentd keeps its data)
  .\scripts\install_task.ps1 -StartNow    # install and start it now
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$StartNow,
    [string]$TaskName = "Always-On Agent"
)
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$logs = Join-Path $root "logs"
$user = "$env:USERDOMAIN\$env:USERNAME"

if ($Uninstall) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "No task named '$TaskName'. Nothing to remove."
        exit 0
    }
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed '$TaskName'. Your data, settings and secrets are kept."
    Write-Host "If agentd is still running, stop it with Ctrl+C in its window or end the 'uv' process."
    exit 0
}

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    throw "uv was not found on PATH. Install it (https://docs.astral.sh/uv/) and run this script again."
}
if (-not (Test-Path (Join-Path $backend "pyproject.toml"))) {
    throw "Cannot find $backend\pyproject.toml. Run this script from the repository's scripts folder."
}
New-Item -ItemType Directory -Force -Path $logs | Out-Null

# A hidden PowerShell window runs uv and appends everything it prints to the log.
$log = Join-Path $logs "agentd.out.log"
$command = "Set-Location -LiteralPath '$backend'; & '$uv' run agentd run *>> '$log'"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$command`"" `
    -WorkingDirectory $backend

# At log-on, on unlock, and every 15 minutes (indefinitely) in case it stopped.
$atLogOn = New-ScheduledTaskTrigger -AtLogOn -User $user
$stateChange = Get-CimClass -Namespace Root/Microsoft/Windows/TaskScheduler -ClassName MSFT_TaskSessionStateChangeTrigger
$onUnlock = New-CimInstance -CimClass $stateChange -ClientOnly -Property @{
    StateChange = 8  # TASK_SESSION_UNLOCK (SessionUnlock)
    UserId      = $user
    Enabled     = $true
}
$every15 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 15)

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

$description = "Runs the Always-On Agent (uv run agentd run) from $backend at log-on, on unlock and every 15 minutes if it is not running. Remove with scripts\install_task.ps1 -Uninstall."
Register-ScheduledTask -TaskName $TaskName -Description $description `
    -Action $action -Trigger @($atLogOn, $onUnlock, $every15) -Settings $settings -Principal $principal `
    -Force | Out-Null

Write-Host "Registered '$TaskName' for $user."
Write-Host "  Runs:     $uv run agentd run   (in $backend)"
Write-Host "  Triggers: at log-on, on unlock, every 15 minutes if not running"
Write-Host "  Log:      $log"
if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started. Check it with: uv run agentd status   (in $backend)"
} else {
    Write-Host "It starts at your next log-on or unlock, or within 15 minutes. Start it now with -StartNow."
}
