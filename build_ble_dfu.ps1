param([switch]$Batch, [ValidatePattern('^[a-z0-9_]*$')][string]$Suffix = '')
$ErrorActionPreference = 'Stop'
$executableName = if ($Batch) { 'rtt_t2_ble_dfu_batch' } else { 'rtt_t2_ble_dfu' }
$outputFolder = if ($Batch) { 'ble_dfu_batch' } else { 'ble_dfu' }
if ($Suffix) {
    $executableName += "_$Suffix"
    $outputFolder += "_$Suffix"
}
Push-Location -LiteralPath $PSScriptRoot
try {
    $ErrorActionPreference = 'Continue'
    & python -m PyInstaller --noconfirm --onefile --windowed `
        --name $executableName --icon (Join-Path $PSScriptRoot 'tool.ico') `
        --exclude-module scipy --exclude-module PySide6 --exclude-module PyQt6 `
        --hidden-import keyboard --hidden-import pystray._win32 `
        --collect-submodules bleak.backends.winrt --collect-all winrt --copy-metadata bleak `
        --distpath "dist/$outputFolder" --workpath "build/$outputFolder" --specpath "build/$outputFolder" `
        rtt_ble_launcher.py
    $buildExitCode = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($buildExitCode -ne 0) { throw "PyInstaller failed: $buildExitCode" }
} finally {
    Pop-Location
}
