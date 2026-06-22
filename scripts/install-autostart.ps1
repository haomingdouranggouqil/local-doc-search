$ErrorActionPreference = "Stop"

$TaskName = "LocalDocSearch"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$StartScript = Join-Path $ProjectRoot "scripts\start.ps1"
$UserId = "{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME
$StartupShortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "LocalDocSearch.lnk"

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$StartScript`"" `
    -WorkingDirectory $ProjectRoot

$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

function Install-StartupShortcut {
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($StartupShortcut)
    $Shortcut.TargetPath = "powershell.exe"
    $Shortcut.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$StartScript`""
    $Shortcut.WorkingDirectory = $ProjectRoot
    $Shortcut.WindowStyle = 7
    $Shortcut.Description = "Start local OCR document search with Docker Compose."
    $Shortcut.Save()
    Write-Host "Installed startup shortcut: $StartupShortcut"
}

try {
    $Principal = New-ScheduledTaskPrincipal `
        -UserId $UserId `
        -LogonType Interactive `
        -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Principal $Principal `
        -Description "Start local OCR document search with Docker Compose." `
        -Force | Out-Null

    Remove-Item -LiteralPath $StartupShortcut -Force -ErrorAction SilentlyContinue
    Write-Host "Installed scheduled task: $TaskName"
} catch {
    Write-Warning "Could not register scheduled task: $($_.Exception.Message)"
    Install-StartupShortcut
}
