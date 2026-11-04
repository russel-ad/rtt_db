# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata, collect_dynamic_libs
from pathlib import Path

winrt_datas, winrt_binaries, winrt_hidden = collect_all('winrt')
workbench_runtime = Path('sc201_workbench/native/runtime')
workbench_binaries = collect_dynamic_libs('sc201_workbench')
workbench_binaries += [(str(workbench_runtime / name), 'PyQt5/Qt5/bin') for name in
                      ('libcrypto-1_1-x64.dll', 'libssl-1_1-x64.dll', 'libcrypto-1_1.dll', 'libssl-1_1.dll')]
workbench_binaries += [(str(path), '.') for path in workbench_runtime.glob('*.dll')
                      if not path.name.startswith('lib')]


a = Analysis(
    ['rtt_t2.py'],
    pathex=[],
    binaries=winrt_binaries + workbench_binaries + [('factory_pair/native/factory_mlink.dll', 'factory_pair/native')],
    datas=winrt_datas + copy_metadata('bleak') + [('factory_pair/native/build.json', 'factory_pair/native'),
                                              ('docs/BLE_FACTORY_PAIR.md', 'docs'),
                                              ('sc201_workbench/native/build.json', 'sc201_workbench/native'),
                                              ('docs/SC201_WORKBENCH.md', 'docs'),
                                              ('docs/SC201_BATCH_OTA.md', 'docs')],
    hiddenimports=['pystray._win32', 'keyboard', 'PyQt5.QtBluetooth', 'PyQt5.QtNetwork'] + winrt_hidden + collect_submodules('bleak.backends.winrt'),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PySide6', 'PyQt6', 'scipy'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='rtt_t2',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
