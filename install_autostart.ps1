# Makes the dashboard start automatically when you log in to Windows, and adds a desktop shortcut.
#   powershell -ExecutionPolicy Bypass -File install_autostart.ps1            # install
#   powershell -ExecutionPolicy Bypass -File install_autostart.ps1 -Remove    # undo
param([switch]$Remove)

$root     = Split-Path -Parent $MyInvocation.MyCommand.Path
$startup  = [Environment]::GetFolderPath("Startup")
$desktop  = [Environment]::GetFolderPath("Desktop")
$autoLink = Join-Path $startup "Bedrock Observability Dashboard.lnk"
$deskUrl  = Join-Path $desktop "Bedrock Observability Dashboard.url"

if ($Remove) {
    Remove-Item $autoLink, $deskUrl -ErrorAction SilentlyContinue
    Write-Output "Removed auto-start and desktop shortcut. (A running dashboard keeps running until you log off.)"
    return
}

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($autoLink)
$lnk.TargetPath = "$env:WINDIR\System32\wscript.exe"
$lnk.Arguments = "`"$root\start_dashboard.vbs`""
$lnk.WorkingDirectory = $root
$lnk.Description = "Starts the Bedrock observability dashboard at http://localhost:8501"
$lnk.Save()

"[InternetShortcut]`r`nURL=http://localhost:8501/`r`n" | Set-Content -Path $deskUrl -Encoding ASCII

Write-Output "Auto-start installed: $autoLink"
Write-Output "Desktop shortcut:     $deskUrl"
