# Installs the plugin into Stream Deck's plugin folder.
#
#   powershell -ExecutionPolicy Bypass -File streamdeck\install.ps1
#
# Copies rather than symlinks: Stream Deck does not reliably follow links, and a
# copy means editing the repo cannot break an installed plugin mid-session.
# Re-run after changing plugin.js, then restart Stream Deck.

param(
    # Stop Stream Deck before copying and start it again afterwards. Required
    # for a reinstall: the running plugin holds bin\plugin.js open, so the copy
    # fails while Stream Deck is up.
    [switch]$Restart
)

$ErrorActionPreference = "Stop"

$source = Join-Path $PSScriptRoot "com.kai.mousebattery.sdPlugin"
$target = Join-Path $env:APPDATA "Elgato\StreamDeck\Plugins\com.kai.mousebattery.sdPlugin"

if (-not (Test-Path $source)) {
    Write-Error "Plugin source not found: $source"
}

# Assets are generated, not checked in as hand-drawn files.
if (-not (Test-Path (Join-Path $source "imgs\plugin\icon.png"))) {
    Write-Host "Assets missing - run: python streamdeck\build_assets.py"
    exit 1
}

$streamDeck = Get-Process StreamDeck -ErrorAction SilentlyContinue
$exePath = $null
if ($streamDeck) {
    $exePath = ($streamDeck | Select-Object -First 1).Path
    if ($Restart) {
        Write-Host "Stopping Stream Deck..."
        $streamDeck | Stop-Process -Force
        Start-Sleep -Seconds 3
    }
    else {
        Write-Host "Stream Deck is running and holds the plugin files open."
        Write-Host "Re-run with -Restart, or quit Stream Deck first:"
        Write-Host "  powershell -ExecutionPolicy Bypass -File streamdeck\install.ps1 -Restart"
        exit 1
    }
}

if (Test-Path $target) {
    Write-Host "Removing previous install..."
    Remove-Item $target -Recurse -Force
}

Write-Host "Installing to $target"
Copy-Item $source $target -Recurse -Force

$files = (Get-ChildItem $target -Recurse -File | Measure-Object).Count
Write-Host "Installed $files files."

if ($Restart -and $exePath) {
    Write-Host "Starting Stream Deck..."
    Start-Process $exePath
    Write-Host "Done. Give it a few seconds to load the plugin."
}
else {
    Write-Host ""
    Write-Host "Restart Stream Deck for it to load the plugin, then add the"
    Write-Host "'Mouse Battery' action to a key or dial."
}
