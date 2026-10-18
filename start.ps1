$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $venvPython)) {
    throw 'Run powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 first.'
}

$existingPage = $null
try {
    $existingPage = Invoke-WebRequest -Uri 'http://127.0.0.1:8765/' -UseBasicParsing -TimeoutSec 2
} catch {
    # A stopped server is expected on a normal launch.
}
if ($existingPage -and $existingPage.Content -match '<title>(?:JobRadar|Jobot)') {
    Write-Host 'Jobot is already running. Open http://127.0.0.1:8765/ in your browser.'
    Write-Host 'Keep its original server terminal open. This launch did not start a second server.'
    return
}

Write-Host 'Starting Jobot at http://127.0.0.1:8765/'
Write-Host 'Open that address in your browser. Keep this terminal open; Ctrl+C stops the server.'
Write-Host 'A previously enabled schedule resumes only while its saved approval is still valid.'
& $venvPython -m jobradar
if ($LASTEXITCODE -ne 0) {
    throw 'Jobot stopped with an error. Check the output above; port 8765 may already be in use, or setup.ps1 may need to be rerun.'
}
