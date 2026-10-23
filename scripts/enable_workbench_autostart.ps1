param([string]$SshHost='model-server', [string]$RemoteDirectory='classroom-models',
    [int]$AsrRemotePort=18765, [int]$EventRemotePort=18766,
    [string]$EventModel='Qwen3-4B', [string]$DataDirectory='')
$ErrorActionPreference = 'Stop'
if ($SshHost -notmatch '^[a-zA-Z0-9][a-zA-Z0-9_.-]*$' -or $RemoteDirectory -notmatch '^[a-zA-Z0-9_./~-]+$') { throw 'Invalid SSH alias or remote directory.' }
if ($AsrRemotePort -lt 1 -or $AsrRemotePort -gt 65535 -or $EventRemotePort -lt 1 -or $EventRemotePort -gt 65535) { throw 'Invalid model port.' }
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonWindowless = Join-Path $projectRoot '.venv/Scripts/pythonw.exe'
$watchdog = Join-Path $PSScriptRoot 'supervise_workbench.py'
if (-not (Test-Path -LiteralPath $pythonWindowless)) { throw 'Project Python is missing.' }
$stateRoot = Join-Path $projectRoot 'runs/remote'
New-Item -ItemType Directory -Force -Path $stateRoot | Out-Null
if (-not $DataDirectory) { $DataDirectory = Join-Path $projectRoot 'runs/webui' }
@{SshHost=$SshHost;RemoteDirectory=$RemoteDirectory;AsrRemotePort=$AsrRemotePort;EventRemotePort=$EventRemotePort;EventModel=$EventModel;DataDirectory=$DataDirectory} | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $stateRoot 'supervisor.json')
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$command = '"' + $pythonWindowless + '" "' + $watchdog + '"'
New-Item -Path $runKey -Force | Out-Null
New-ItemProperty -Path $runKey -Name 'fuckYanheWorkbench' -Value $command -PropertyType String -Force | Out-Null
Start-Process -FilePath $pythonWindowless -ArgumentList @('"' + $watchdog + '"') -WorkingDirectory $projectRoot -WindowStyle Hidden
Write-Output 'Workbench recovery enabled for this Windows user; no password stored.'
