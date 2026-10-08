<#
.SYNOPSIS
  Register CMF's transcript pollers (and optionally the MCP server) as
  Windows Task Scheduler tasks. See deploy/pollers/README.md.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\pollers\windows\install.ps1
  powershell -ExecutionPolicy Bypass -File deploy\pollers\windows\install.ps1 -Pollers claude-code,codex -McpServer
  powershell -ExecutionPolicy Bypass -File deploy\pollers\windows\install.ps1 -WhatIf
  powershell -ExecutionPolicy Bypass -File deploy\pollers\windows\install.ps1 -Uninstall

.NOTES
  Tasks run as the current user with the S4U logon type ("run whether the
  user is logged on or not", no stored password), so no console window
  appears on each poll. S4U runs can't reach network shares with your
  credentials; local files and the internet work. Each run appends to
  %LOCALAPPDATA%\cmf\logs\cmf-<name>-poller.log.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
  [string[]] $Pollers = @("claude-code", "codex"),
  [switch] $All,
  [switch] $McpServer,
  [int] $Port = 8000,
  [int] $IntervalMinutes = 15,
  [string] $Python,
  [string] $Command = "tail",
  [string] $TaskFolder = "\CMF\",
  [switch] $Uninstall
)

$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
if (-not $Python) { $Python = Join-Path $Repo ".venv\Scripts\python.exe" }
if ($All) { $Pollers = @("claude-code", "codex", "cowork", "antigravity") }
$LogDir = Join-Path $env:LOCALAPPDATA "cmf\logs"

$Modules = @{
  "claude-code" = "server.adapters.claude_code.cli"
  "codex"       = "server.adapters.codex.cli"
  "cowork"      = "server.adapters.claude_cowork.cli"
  "antigravity" = "server.adapters.antigravity.cli"
}

function New-CmfAction([string] $PyArgs, [string] $LogName) {
  # cmd /c so stdout/stderr land in a log file; Python's own output is the run report.
  $log = Join-Path $LogDir $LogName
  New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"`"$Python`" $PyArgs >> `"$log`" 2>&1`"" `
    -WorkingDirectory $Repo
}

$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited

function Register-CmfTask([string] $Name, $Action, $Trigger, $Settings) {
  if ($PSCmdlet.ShouldProcess("$TaskFolder$Name", "Register-ScheduledTask")) {
    Register-ScheduledTask -TaskPath $TaskFolder -TaskName $Name -Action $Action -Trigger $Trigger `
      -Settings $Settings -Principal $Principal -Force | Out-Null
    Write-Host "registered $TaskFolder$Name"
  }
}

function Remove-CmfTask([string] $Name) {
  if ($PSCmdlet.ShouldProcess("$TaskFolder$Name", "Unregister-ScheduledTask")) {
    Unregister-ScheduledTask -TaskPath $TaskFolder -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "removed $TaskFolder$Name"
  }
}

if (-not $Uninstall -and -not $WhatIfPreference -and -not (Test-Path $Python)) {
  throw "No interpreter at $Python. Create the venv first (uv sync), or pass -Python."
}
if (-not $Uninstall) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }

foreach ($name in $Pollers) {
  if (-not $Modules.ContainsKey($name)) { throw "Unknown poller '$name' (use claude-code, codex, cowork, antigravity)." }
  $task = "cmf-$name-poller"
  if ($Uninstall) { Remove-CmfTask $task; continue }
  $action = New-CmfAction "-m $($Modules[$name]) $Command" "$task.log"
  # Starts a minute from now and repeats indefinitely.
  $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
  $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
  Register-CmfTask $task $action $trigger $settings
}

if ($McpServer) {
  $task = "cmf-mcp-server"
  if ($Uninstall) { Remove-CmfTask $task }
  else {
    $action = New-CmfAction "-m server.mcp --transport streamable-http --host 127.0.0.1 --port $Port" "$task.log"
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    # No time limit, restart on failure: it is a long-running server.
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
      -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-CmfTask $task $action $trigger $settings
  }
}
