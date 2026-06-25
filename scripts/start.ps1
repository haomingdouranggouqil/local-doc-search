param(
    [ValidateSet("auto", "api", "cpu", "gpu")]
    [string]$Device = "auto"
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
$StartLog = Join-Path $ProjectRoot ".docsearch\start.log"
$DataRoot = (Resolve-Path (Join-Path $ProjectRoot "data")).Path

function Write-Log {
    param([string]$Message)
    $Line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $StartLog -Value $Line -Encoding utf8
    Write-Host $Message
}

function Get-OpenHelperHealth {
    try {
        return Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 2
    } catch {
        return $null
    }
}

function Stop-StaleOpenHelper {
    if (-not (Test-Path $OpenHelperPid)) {
        return
    }
    $ExistingPid = (Get-Content $OpenHelperPid -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($ExistingPid -and (Get-Process -Id ([int]$ExistingPid) -ErrorAction SilentlyContinue)) {
        Stop-Process -Id ([int]$ExistingPid) -Force
        Write-Log "Stopped stale local file open helper process $ExistingPid."
    }
    Remove-Item -LiteralPath $OpenHelperPid -Force -ErrorAction SilentlyContinue
}

function Start-OpenHelper {
    $Health = Get-OpenHelperHealth
    if ($Health -and $Health.ok -and $Health.data_root -eq $DataRoot) {
        Write-Log "Local file open helper is healthy."
        return
    }

    Stop-StaleOpenHelper

    $Python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $Python) {
        $Python = Get-Command py -ErrorAction SilentlyContinue
    }
    if (-not $Python) {
        throw "Python was not found. The local file open button needs scripts\open-helper.py."
    }

    $env:OPEN_HELPER_DATA_ROOT = $DataRoot
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
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        $Health = Get-OpenHelperHealth
        if ($Health -and $Health.ok -and $Health.data_root -eq $DataRoot) {
            Write-Log "Started local file open helper on http://127.0.0.1:8765."
            return
        }
        if ($Process.HasExited) {
            break
        }
    }
    throw "Local file open helper failed to start. Check .docsearch\open-helper.err.log."
}

function Start-DockerDesktopIfNeeded {
    docker info *> $null
    if ($LASTEXITCODE -eq 0) {
        return
    }

    $Candidates = @(
        (Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"),
        (Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop.exe"),
        (Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop\Docker Desktop.exe")
    )
    foreach ($Candidate in $Candidates) {
        if (Test-Path $Candidate) {
            Write-Log "Starting Docker Desktop."
            Start-Process -FilePath $Candidate -WindowStyle Hidden | Out-Null
            return
        }
    }
}

function Wait-DockerReady {
    Start-DockerDesktopIfNeeded
    for ($i = 0; $i -lt 120; $i++) {
        docker info *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Log "Docker is ready."
            return
        }
        Start-Sleep -Seconds 3
    }
    throw "Docker is not ready after waiting for 360 seconds."
}

function Wait-HttpOk {
    param(
        [string]$Uri,
        [string]$Name,
        [int]$Seconds = 120
    )
    $Deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $Deadline) {
        try {
            Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 3 | Out-Null
            Write-Log "$Name is healthy."
            return
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    throw "$Name did not become healthy at $Uri."
}

function Test-NvidiaGpuPresent {
    $NvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if (-not $NvidiaSmi) {
        return $false
    }
    nvidia-smi *> $null
    return $LASTEXITCODE -eq 0
}

function Get-ComposeArgs {
    $Args = @("-f", "docker-compose.yml")
    $UseGpu = $false
    if ($Device -eq "gpu") {
        $UseGpu = $true
    } elseif ($Device -eq "auto") {
        $UseGpu = Test-NvidiaGpuPresent
    }
    if ($UseGpu) {
        $Args += @("-f", "docker-compose.gpu.yml")
        Write-Log "NVIDIA GPU detected; starting Docker Compose with GPU access."
    } else {
        Write-Log "Starting Docker Compose without GPU access."
    }
    return $Args
}

function Start-ComposeStack {
    $ComposeArgs = Get-ComposeArgs
    docker compose @ComposeArgs up -d --build
    if ($LASTEXITCODE -eq 0) {
        return
    }
    if ($ComposeArgs -contains "docker-compose.gpu.yml") {
        Write-Log "GPU compose startup failed; retrying without GPU access."
        docker compose -f docker-compose.yml up -d --build
        if ($LASTEXITCODE -eq 0) {
            return
        }
    }
    throw "Docker Compose startup failed."
}

Start-OpenHelper
Wait-DockerReady

Write-Log "Starting API OCR stack."
Start-ComposeStack

Start-OpenHelper
Wait-HttpOk -Uri "http://127.0.0.1:8000/api/health" -Name "Backend API"
Wait-HttpOk -Uri "http://127.0.0.1:8517/" -Name "Frontend"
Write-Log "Local document search stack is ready."
