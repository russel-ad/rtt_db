param([switch]$Batch, [switch]$HostScale, [switch]$Workbench, [switch]$OneDir, [ValidatePattern('^[a-z0-9_]*$')][string]$Suffix = '')
$ErrorActionPreference = 'Stop'
$executableName = if ($Batch) { 'rtt_t2_ble_dfu_batch' } else { 'rtt_t2_ble_dfu' }
$outputFolder = if ($Batch) { 'ble_dfu_batch' } else { 'ble_dfu' }
if ($HostScale) {
    $executableName = 'rtt_t2_ble_dfu_host_scale_rssi'
    $outputFolder = 'ble_dfu_host_scale'
}
if ($Workbench) {
    $executableName = 'rtt_t2_sc201_workbench'
    $outputFolder = 'sc201_workbench'
}
if ($OneDir) {
    $executableName += '_portable'
    $outputFolder += '_portable'
}
if ($Suffix) {
    $executableName += "_$Suffix"
    $outputFolder += "_$Suffix"
}
Push-Location -LiteralPath $PSScriptRoot
try {
    if (!(Test-Path -LiteralPath 'factory_pair/native/factory_mlink.dll')) {
        throw 'Missing factory pairing DLL. Run: python -m factory_pair.build_native --sdk E:/onm/SC201_master_omn'
    }
    $factoryPairBinary = (Join-Path $PSScriptRoot 'factory_pair/native/factory_mlink.dll') + ';factory_pair/native'
    $factoryPairManifest = (Join-Path $PSScriptRoot 'factory_pair/native/build.json') + ';factory_pair/native'
    $factoryPairGuide = (Join-Path $PSScriptRoot 'docs/BLE_FACTORY_PAIR.md') + ';docs'
    if (!(Test-Path -LiteralPath 'sc201_workbench/native/sc201_workbench.dll')) {
        throw 'Missing SC201 workbench DLL. Run: python -m sc201_workbench.build_native'
    }
    $workbenchManifest = (Join-Path $PSScriptRoot 'sc201_workbench/native/build.json') + ';sc201_workbench/native'
    $workbenchGuide = (Join-Path $PSScriptRoot 'docs/SC201_WORKBENCH.md') + ';docs'
    $workbenchBatchGuide = (Join-Path $PSScriptRoot 'docs/SC201_BATCH_OTA.md') + ';docs'
    $workbenchBinaries = @()
    # Prefer the checked HTTPS DLLs over the older copies in the PyQt5 wheel.
    foreach ($name in @('libcrypto-1_1-x64.dll', 'libssl-1_1-x64.dll', 'libcrypto-1_1.dll', 'libssl-1_1.dll')) {
        $runtimePath = Join-Path $PSScriptRoot "sc201_workbench/native/runtime/$name"
        $workbenchBinaries += @('--add-binary', "$runtimePath;PyQt5/Qt5/bin")
    }
    foreach ($runtime in Get-ChildItem -LiteralPath 'sc201_workbench/native/runtime' -Filter '*.dll') {
        if ($runtime.Name -notlike 'lib*') {
            $workbenchBinaries += @('--add-binary', "$($runtime.FullName);.")
        }
    }
    $bundleMode = if ($OneDir) { '--onedir' } else { '--onefile' }
    $ErrorActionPreference = 'Continue'
    & python -m PyInstaller --noconfirm $bundleMode --windowed `
        --name $executableName --icon (Join-Path $PSScriptRoot 'tool.ico') `
        --exclude-module scipy --exclude-module PySide6 --exclude-module PyQt6 `
        --hidden-import keyboard --hidden-import pystray._win32 `
        --collect-data certifi --hidden-import truststore._windows `
        --copy-metadata certifi --copy-metadata truststore `
        --collect-submodules bleak.backends.winrt --collect-all winrt --copy-metadata bleak `
        --collect-binaries sc201_workbench `
        --hidden-import PyQt5.QtBluetooth --hidden-import PyQt5.QtNetwork `
        --add-binary $factoryPairBinary `
        --add-data $factoryPairManifest `
        --add-data $factoryPairGuide `
        --add-data $workbenchManifest --add-data $workbenchGuide --add-data $workbenchBatchGuide `
        @workbenchBinaries `
        --distpath "dist/$outputFolder" --workpath "build/$outputFolder" --specpath "build/$outputFolder" `
        rtt_ble_launcher.py
    $buildExitCode = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($buildExitCode -ne 0) { throw "PyInstaller failed: $buildExitCode" }
    $deliveryFolder = Join-Path $PSScriptRoot "dist/$outputFolder"
    $appFolder = if ($OneDir) { Join-Path $deliveryFolder $executableName } else { $deliveryFolder }
    $licenseFolder = Join-Path $appFolder 'licenses'
    New-Item -ItemType Directory -Path $licenseFolder -Force | Out-Null
    Copy-Item -Path (Join-Path $PSScriptRoot 'sc201_workbench/licenses/*') `
        -Destination $licenseFolder -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'docs/SC201_WORKBENCH.md') `
        -Destination (Join-Path $appFolder 'SC201_WORKBENCH.md') -Force
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'docs/SC201_BATCH_OTA.md') `
        -Destination (Join-Path $appFolder 'SC201_BATCH_OTA.md') -Force
    if ($OneDir) {
        # Qt 5's SSL loader searches beside the executable. Keep this direct
        # lookup working even when the relocated bundle path contains Chinese.
        foreach ($name in @('libcrypto-1_1-x64.dll', 'libssl-1_1-x64.dll', 'libcrypto-1_1.dll', 'libssl-1_1.dll')) {
            Copy-Item -LiteralPath (Join-Path $PSScriptRoot "sc201_workbench/native/runtime/$name") `
                -Destination (Join-Path $appFolder $name) -Force
        }
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'docs/PORTABLE_START.md') `
            -Destination (Join-Path $appFolder 'PORTABLE_START.md') -Force
        $zipPath = "$deliveryFolder.zip"
        # Use shared file reads; Compress-Archive may fail while a scanner is
        # inspecting a freshly built dependency such as base_library.zip.
        & python -m zipfile -c $zipPath $appFolder
        if ($LASTEXITCODE -ne 0) { throw "Portable ZIP failed: $LASTEXITCODE" }
        Write-Host "Portable archive: $zipPath"
    }
    Write-Host "Application: $(Join-Path $appFolder "$executableName.exe")"
} finally {
    Pop-Location
}
