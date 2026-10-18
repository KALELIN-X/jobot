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
if ($existingPage -and $existingPage.Content -match '<title>JobRadar') {
    Write-Host 'JobRadar is already running. Open http://127.0.0.1:8765/ in your browser.'
    Write-Host 'Keep its original server terminal open. This launch did not start a second server.'
    return
}

Write-Host 'Starting JobRadar at http://127.0.0.1:8765/'
Write-Host 'Open that address in your browser. Keep this terminal open; Ctrl+C stops the server.'
Write-Host 'A previously enabled schedule resumes only while its saved approval is still valid.'
& $venvPython -m jobradar
if ($LASTEXITCODE -ne 0) {
    throw 'JobRadar stopped with an error. Check the output above; port 8765 may already be in use, or setup.ps1 may need to be rerun.'
}
