$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonVersion = (Get-Content -Raw -LiteralPath (Join-Path $projectRoot '.python-version')).Trim()
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
$uvExecutable = if ($uvCommand) { $uvCommand.Source } else { Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
if (-not (Test-Path -LiteralPath $uvExecutable)) { throw 'Install uv before running setup.ps1.' }
$savedVariables = @{}
foreach ($variableName in @('UV_PYTHON_INSTALL_DIR', 'UV_PROJECT_ENVIRONMENT', 'PLAYWRIGHT_BROWSERS_PATH')) {
    $savedVariables[$variableName] = [Environment]::GetEnvironmentVariable($variableName, 'Process')
}
Push-Location -LiteralPath $projectRoot
try {
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $projectRoot '.python-runtime'
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $projectRoot '.venv'
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $projectRoot '.browsers'
    & $uvExecutable python install $pythonVersion --install-dir $env:UV_PYTHON_INSTALL_DIR --no-bin --no-registry --no-progress
    if ($LASTEXITCODE -ne 0) { throw 'Independent Python installation failed.' }
    $runtimePython = & $uvExecutable python find $pythonVersion --managed-python --no-python-downloads
    if ($LASTEXITCODE -ne 0) { throw 'Project Python runtime was not found.' }
    $runtimePython = ($runtimePython | Select-Object -Last 1).Trim()
    & $uvExecutable sync --locked --project $projectRoot --python $runtimePython --no-python-downloads --link-mode copy --no-progress
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    $projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    & $uvExecutable pip check --python $projectPython
    if ($LASTEXITCODE -ne 0) { throw 'Dependency validation failed.' }
    & $projectPython -m playwright install chromium --no-shell
    if ($LASTEXITCODE -ne 0) { throw 'Chromium installation failed.' }
    & $projectPython (Join-Path $projectRoot 'main.py') check --browser
    if ($LASTEXITCODE -ne 0) { throw 'Browser startup check failed.' }
    Write-Output "Ready. PyCharm interpreter: $projectPython"
} finally {
    Pop-Location
    foreach ($variableName in $savedVariables.Keys) {
        [Environment]::SetEnvironmentVariable($variableName, $savedVariables[$variableName], 'Process')
    }
}
