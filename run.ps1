# Levanta la app y la abre en el navegador.
#   .\run.ps1              -> http://127.0.0.1:8000
#   .\run.ps1 -Port 9000   -> otro puerto
#   .\run.ps1 -NoBrowser   -> no abrir el navegador

param(
    [int]$Port = 8000,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

foreach ($tool in @("ffmpeg", "ffprobe")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Host "Falta '$tool' en el PATH. Instalalo con: winget install Gyan.FFmpeg" -ForegroundColor Red
        exit 1
    }
}

Write-Host "Comprobando dependencias de Python..." -ForegroundColor DarkGray
python -m pip install --quiet --disable-pip-version-check -r requirements.txt
if (-not $?) {
    Write-Host "No se pudieron instalar las dependencias." -ForegroundColor Red
    exit 1
}

$url = "http://127.0.0.1:$Port"
Write-Host ""
Write-Host "  PS4 Theme Asset Creator (legacy)" -ForegroundColor Cyan
Write-Host "  $url" -ForegroundColor White
Write-Host "  Ctrl+C para detener" -ForegroundColor DarkGray
Write-Host ""

if (-not $NoBrowser) {
    Start-Job -ScriptBlock { Start-Sleep -Seconds 2; Start-Process $using:url } | Out-Null
}

$env:CZAR_LEGACY = "1"
python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
