$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectDir
$pythonCommand = $null
$pythonArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($version in @('-3.13', '-3.12', '-3.11', '-3.14')) {
        & py $version -c 'import sys; assert (3,11) <= sys.version_info[:2] < (3,15)' 2>$null
        if ($LASTEXITCODE -eq 0) { $pythonCommand = 'py'; $pythonArgs = @($version); break }
    }
}
if (-not $pythonCommand -and (Get-Command python -ErrorAction SilentlyContinue)) {
    & python -c 'import sys; assert (3,11) <= sys.version_info[:2] < (3,15)' 2>$null
    if ($LASTEXITCODE -eq 0) { $pythonCommand = 'python' }
}
if (-not $pythonCommand) { throw 'Install 64-bit Python 3.11-3.14, then run this script again.' }
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    & $pythonCommand @pythonArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the project virtual environment.' }
}
& '.\.venv\Scripts\python.exe' -c 'import sys,struct; assert (3,11) <= sys.version_info[:2] < (3,15) and struct.calcsize("P")==8'
if ($LASTEXITCODE -ne 0) { throw 'The existing .venv needs a supported 64-bit Python.' }
& '.\.venv\Scripts\python.exe' -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed.' }
& '.\.venv\Scripts\python.exe' -m pip install -e '.[media]'
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& '.\.venv\Scripts\wechat-pulse.exe' --json demo --output '.\demo-output'
if ($LASTEXITCODE -ne 0) { throw 'Offline demo failed.' }
& '.\.venv\Scripts\wechat-pulse.exe' --json doctor
if ($LASTEXITCODE -ne 0) { throw 'Environment check failed.' }
Write-Host 'Ready. Open demo-output/briefing.html. Next: docs/INSTALL.md.'
