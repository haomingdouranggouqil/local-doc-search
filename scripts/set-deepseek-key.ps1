$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$EnvPath = Join-Path $ProjectRoot ".env"
$Key = Read-Host "Paste DEEPSEEK_API_KEY"

if (-not (Test-Path $EnvPath)) {
    Copy-Item (Join-Path $ProjectRoot ".env.example") $EnvPath
}

$lines = Get-Content $EnvPath -ErrorAction SilentlyContinue
$updated = $false
$lines = $lines | ForEach-Object {
    if ($_ -match "^DEEPSEEK_API_KEY=") {
        $updated = $true
        "DEEPSEEK_API_KEY=$Key"
    } else {
        $_
    }
}
if (-not $updated) {
    $lines += "DEEPSEEK_API_KEY=$Key"
}
$lines | Set-Content -Path $EnvPath -Encoding UTF8
Write-Host "Saved DEEPSEEK_API_KEY to .env"
