<#
.SYNOPSIS
    Register (or remove) a Windows scheduled task that runs the full
    pipeline unattended every day.

.DESCRIPTION
    The task runs scripts\run_pipeline.py with --keep-data --skip-tests
    (no questions; synthetic data is not regenerated). Output goes to
    logs\pipeline_<timestamp>.log; the exit code (0 success, 1 warnings,
    2 failure, 130 cancelled) is recorded by Task Scheduler, and
    logs\health_latest.json is refreshed for alerting.

.EXAMPLE
    .\scripts\schedule_pipeline.ps1                 # daily at 02:00
    .\scripts\schedule_pipeline.ps1 -At "05:30"
    .\scripts\schedule_pipeline.ps1 -Remove
#>
param(
    [string]$At = "02:00",
    [string]$TaskName = "RetailDataPlatform-Pipeline",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task $TaskName."
    return
}

if (-not (Test-Path $Python)) {
    throw "Virtual environment not found at $Python. Create it first (see README)."
}

$Action = New-ScheduledTaskAction -Execute $Python `
    -Argument "scripts\run_pipeline.py --keep-data --skip-tests" `
    -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -Daily -At $At
# Do not start a second run while one is still going; stop runaway runs.
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "Retail Data Platform v2: ingestion, validation, warehouse load and reconciliation." -Force | Out-Null

Write-Host "Scheduled $TaskName daily at $At (runs as $env:USERNAME)."
Write-Host "Check results with: python scripts\health_check.py"
