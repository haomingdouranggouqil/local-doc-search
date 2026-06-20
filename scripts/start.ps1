param(
    [ValidateSet("auto", "api", "cpu", "gpu")]
    [string]$Device = "api"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot
@("data", "data\txt", "data\doc", "data\pdf") | ForEach-Object {
    New-Item -ItemType Directory -Force $_ | Out-Null
}
New-Item -ItemType Directory -Force ".docsearch" | Out-Null

$OpenHelperPid = Join-Path $ProjectRoot ".docsearch\open-helper.pid"
$OpenHelperOut = Join-Path $ProjectRoot ".docsearch\open-helper.out.log"
$OpenHelperErr = Join-Path $ProjectRoot ".docsearch\open-helper.err.log"
$OpenHelperScript = Join-Path $ProjectRoot "scripts\open-helper.py"

function Start-OpenHelper {
    if (Test-Path $OpenHelperPid) {
        $ExistingPid = (Get-Content $OpenHelperPid -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($ExistingPid -and (Get-Process -Id ([int]$ExistingPid) -ErrorAction SilentlyContinue)) {
            Write-Host "Local file open helper is already running."
            return
        }
    }

    $Python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $Python) {
        $Python = Get-Command py -ErrorAction SilentlyContinue
    }
    if (-not $Python) {
        Write-Warning "Python was not found. The web app will still run, but the local file open button needs scripts\open-helper.py."
        return
    }

    $env:OPEN_HELPER_DATA_ROOT = (Resolve-Path (Join-Path $ProjectRoot "data")).Path
    $env:OPEN_HELPER_HOST = "127.0.0.1"
    $env:OPEN_HELPER_PORT = "8765"
    $env:OPEN_HELPER_ALLOWED_ORIGINS = "http://localhost:8517,http://127.0.0.1:8517"

    $Arguments = @()
    if ($Python.Name -eq "py.exe" -or $Python.Name -eq "py") {
        $Arguments += "-3"
    }
    $Arguments += $OpenHelperScript

    $Process = Start-Process `
        -FilePath $Python.Source `
        -ArgumentList $Arguments `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $OpenHelperOut `
        -RedirectStandardError $OpenHelperErr `
        -PassThru
    Set-Content -Path $OpenHelperPid -Value $Process.Id -Encoding ascii
    Write-Host "Started local file open helper on http://127.0.0.1:8765."
}

Start-OpenHelper

for ($i = 0; $i -lt 60; $i++) {
    docker info *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Starting API OCR stack."
        docker compose up -d --build
        exit 0
    }
    Start-Sleep -Seconds 3
}

throw "Docker is not ready after waiting for 180 seconds."
