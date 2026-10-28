# Starts the backend and the console dev server for development.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$root\backend'; uv sync; uv run agentd run --fake"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$root\console'; npm install; npm run dev"

Write-Host "Backend and console starting in new windows. Console: http://localhost:5173"
