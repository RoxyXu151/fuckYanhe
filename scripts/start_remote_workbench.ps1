param(
    [string]$SshHost = 'model-server',
    [string]$RemoteDirectory = 'classroom-models',
    [int]$AsrRemotePort = 18765,
    [int]$EventRemotePort = 18766,
    [string]$EventModel = 'Qwen3-4B',
    [string]$DataDirectory = '',
    [switch]$ConfigureOnly
)
$ErrorActionPreference = 'Stop'
if ($SshHost -notmatch '^[a-zA-Z0-9][a-zA-Z0-9_.-]*$' -or $RemoteDirectory -notmatch '^[a-zA-Z0-9_./~-]+$') { throw 'Use a configured SSH alias and a simple remote directory path.' }
if ($AsrRemotePort -lt 1 -or $AsrRemotePort -gt 65535 -or $EventRemotePort -lt 1 -or $EventRemotePort -gt 65535) { throw 'Invalid model port.' }
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$stateRoot = Join-Path $projectRoot 'runs/remote'
New-Item -ItemType Directory -Force -Path $stateRoot | Out-Null
$keyFile = Join-Path $stateRoot 'service.key'
if (-not (Test-Path -LiteralPath $keyFile)) {
    & scp "${SshHost}:$RemoteDirectory/service.key" $keyFile
    if ($LASTEXITCODE -ne 0) { throw 'Cannot obtain the dedicated model service credential.' }
}
if (-not (Get-NetTCPConnection -LocalPort 18765 -State Listen -ErrorAction SilentlyContinue)) {
    $tunnel = Start-Process -FilePath ssh -ArgumentList @('-N', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
        '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', '-L', "127.0.0.1:18765:127.0.0.1:$AsrRemotePort",
        '-L', "127.0.0.1:18766:127.0.0.1:$EventRemotePort", $SshHost) -WindowStyle Hidden -PassThru
    $tunnel.Id | Set-Content -LiteralPath (Join-Path $stateRoot 'tunnel.pid')
}
$env:YANHE_ASR_DEPLOYMENT = 'self-hosted'
$env:YANHE_EVENT_DEPLOYMENT = 'self-hosted'
$env:YANHE_ASR_PROVIDER = 'openai-compatible'
$env:YANHE_ASR_BASE_URL = 'http://127.0.0.1:18765/v1'
$env:YANHE_ASR_MODEL = 'Qwen3-ASR-1.7B+ForcedAligner-0.6B'
$env:YANHE_ASR_API_KEY = (Get-Content -Raw -LiteralPath $keyFile).Trim()
$env:YANHE_BASE_URL = 'http://127.0.0.1:18766/v1'
$env:YANHE_MODEL = $EventModel
$env:YANHE_QWEN_THINKING = '1'
$env:YANHE_API_KEY = $env:YANHE_ASR_API_KEY
Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue
if (-not $DataDirectory) { $DataDirectory = Join-Path $projectRoot 'runs/webui' }
New-Item -ItemType Directory -Force -Path $DataDirectory | Out-Null
$temporaryRoot = Join-Path $DataDirectory 'temp'
New-Item -ItemType Directory -Force -Path $temporaryRoot | Out-Null
$env:TEMP = $temporaryRoot
$env:TMP = $temporaryRoot
$env:YANHE_DOWNLOAD_WORKERS = '2'
$env:YANHE_SEGMENT_WORKERS = '4'
$env:YANHE_ANALYSIS_WORKERS = '1'
if ($ConfigureOnly) { return }
& (Join-Path $projectRoot '.venv/Scripts/python.exe') -m yanhe_sentinel webui --data-dir $DataDirectory --no-browser
