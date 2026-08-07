# Creates a Desktop shortcut for the A/B Sales Analyzer desktop app that can be
# pinned to the taskbar. Run once (right-click -> Run with PowerShell, or:
#   powershell -ExecutionPolicy Bypass -File make_desktop_shortcut.ps1
# ). It targets pythonw.exe (a real program, so Windows lets you pin it) with
# desktop_app.py as the argument.

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path

# Resolve pythonw.exe next to the active python; fall back to PATH.
$pyExe = (Get-Command python -ErrorAction SilentlyContinue).Source
if ($pyExe) { $pyw = Join-Path (Split-Path -Parent $pyExe) "pythonw.exe" }
if (-not $pyw -or -not (Test-Path $pyw)) {
    $pyw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
}
if (-not $pyw -or -not (Test-Path $pyw)) {
    Write-Error "pythonw.exe not found. Install Python (with the py launcher) and retry."
    exit 1
}

$lnkPath = Join-Path ([Environment]::GetFolderPath('Desktop')) "A-B Analyzer.lnk"
$icon = Join-Path $repo "assets\analyzer.ico"

$W = New-Object -ComObject WScript.Shell
$lnk = $W.CreateShortcut($lnkPath)
$lnk.TargetPath = $pyw
$lnk.Arguments = '"' + (Join-Path $repo "desktop_app.py") + '"'
$lnk.WorkingDirectory = $repo
if (Test-Path $icon) { $lnk.IconLocation = $icon }
$lnk.Description = "A/B Sales Analyzer (desktop window)"
$lnk.WindowStyle = 1
$lnk.Save()

Write-Output "Created: $lnkPath"
Write-Output "Now right-click it -> Show more options -> Pin to taskbar."
