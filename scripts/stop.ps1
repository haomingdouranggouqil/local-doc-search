$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

$OpenHelperPid = Join-Path $ProjectRoot ".docsearch\open-helper.pid"
if (Test-Path $OpenHelperPid) {
    $ExistingPid = (Get-Content $OpenHelperPid -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($ExistingPid -and (Get-Process -Id ([int]$ExistingPid) -ErrorAction SilentlyContinue)) {
        Stop-Process -Id ([int]$ExistingPid) -Force
        Write-Host "Stopped local file open helper."
    }
    Remove-Item -LiteralPath $OpenHelperPid -Force -ErrorAction SilentlyContinue
}

docker compose down
