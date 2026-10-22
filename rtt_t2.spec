# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

winrt_datas, winrt_binaries, winrt_hidden = collect_all('winrt')


a = Analysis(
    ['rtt_t2.py'],
    pathex=[],
    binaries=winrt_binaries,
    datas=winrt_datas + copy_metadata('bleak'),
    hiddenimports=['pystray._win32', 'keyboard'] + winrt_hidden + collect_submodules('bleak.backends.winrt'),
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
