param(
    [int]$Port = 8000,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $Root "runtime"
$OutLogPath = Join-Path $RuntimeDir "server.out.log"
$ErrLogPath = Join-Path $RuntimeDir "server.err.log"
$PythonPath = Join-Path $Root ".venv\Scripts\python.exe"

if (!(Test-Path $RuntimeDir)) {
    New-Item -ItemType Directory -Path $RuntimeDir | Out-Null
}

if (!(Test-Path $PythonPath)) {
    $PythonPath = "python"
}

$Listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($Item in $Listeners) {
    Stop-Process -Id $Item.OwningProcess -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 1

$Arguments = @(
    "-m",
    "uvicorn",
    "app.main:app",
    "--host",
    "127.0.0.1",
    "--port",
    "$Port"
)

Start-Process `
    -FilePath $PythonPath `
    -ArgumentList $Arguments `
    -WorkingDirectory $Root `
    -RedirectStandardOutput $OutLogPath `
    -RedirectStandardError $ErrLogPath `
    -WindowStyle Hidden

$HealthUrl = "http://127.0.0.1:$Port/api/health"
$AppUrl = "http://127.0.0.1:$Port/"
$Ready = $false

for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    try {
        $Health = Invoke-WebRequest -UseBasicParsing $HealthUrl -TimeoutSec 3
        if ($Health.StatusCode -eq 200) {
            $Ready = $true
            break
        }
    } catch {
    }
}

if (!$Ready) {
    Write-Host "Worldpanel Reader failed to start. Check logs:"
    Write-Host $OutLogPath
    Write-Host $ErrLogPath
    exit 1
}

Write-Host "Worldpanel Reader is running:"
Write-Host $AppUrl
Write-Host "Health:"
(Invoke-WebRequest -UseBasicParsing $HealthUrl).Content

if (!$NoBrowser) {
    Start-Process $AppUrl
}
