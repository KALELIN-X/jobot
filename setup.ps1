$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $venvPython)) {
    if (!(Get-Command py -ErrorAction SilentlyContinue)) {
        throw 'Install Python 3.12 or newer from python.org (including the Python launcher), then run setup.ps1 again.'
    }
    & py -3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'
    if ($LASTEXITCODE -ne 0) {
        throw 'Python 3.12 or newer is required. Install it, then run setup.ps1 again.'
    }
    Write-Host 'Creating the project virtual environment...'
    & py -3 -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not create .venv. Check your Python installation and folder permissions.'
    }
}

& $venvPython -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'
if ($LASTEXITCODE -ne 0) {
    throw 'The project environment needs Python 3.12 or newer. Create a fresh project environment before installing.'
}

Write-Host 'Installing the pinned Python dependencies...'
& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    throw 'Dependency installation failed. Check your Internet connection and the pip output above, then rerun setup.ps1.'
}

Write-Host 'Installing the Chromium browser used by the application worker...'
& $venvPython -m playwright install chromium --only-shell
if ($LASTEXITCODE -ne 0) {
    throw 'Chromium installation failed. Check your Internet connection, then rerun setup.ps1.'
}

& $venvPython -m pip check
if ($LASTEXITCODE -ne 0) {
    throw 'Dependency verification failed. Review the pip output above before launching.'
}

Write-Host ''
Write-Host 'Jobot setup complete. Launch with:'
Write-Host '  powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1'
Write-Host 'Then open http://127.0.0.1:8765/ in your browser.'
