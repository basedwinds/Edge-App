# Runs backend (FastAPI/uvicorn) and frontend (Vite) dev servers together.
$root = "$PSScriptRoot"

Start-Process powershell -ArgumentList "-NoExit", "-File", "$root\run_backend.ps1"
Start-Process powershell -ArgumentList "-NoExit", "-File", "$root\run_frontend.ps1"

Write-Host "Backend:  http://127.0.0.1:8756/health"
Write-Host "Frontend: http://localhost:5173"
Write-Host "Both dev servers launching in separate windows. Close those windows to stop."
