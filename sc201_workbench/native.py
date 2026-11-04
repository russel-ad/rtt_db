"""Small C ABI adapter for the original SC201 Qt workbench."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import sys

NATIVE_DIRECTORY = Path(__file__).resolve().parent / 'native'
_runtime_handles = []
_directory_handles = []


class WorkbenchError(RuntimeError):
    pass


def build_manifest() -> dict:
    path = NATIVE_DIRECTORY / 'build.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def prepare_runtime():
    """Keep app-local CRT/HTTPS libraries loaded for Qt's delayed SSL loader."""
    if sys.platform != 'win32' or _runtime_handles:
        return
    runtime = NATIVE_DIRECTORY / 'runtime'
    if runtime.is_dir():
        # Qt 5's SSL discovery checks PATH before resolving loaded libraries.
        os.environ['PATH'] = str(runtime) + os.pathsep + os.environ.get('PATH', '')
        _directory_handles.append(os.add_dll_directory(str(runtime)))
        for name in ('vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll',
                     'libcrypto-1_1-x64.dll', 'libssl-1_1-x64.dll',
                     'libcrypto-1_1.dll', 'libssl-1_1.dll'):
            path = runtime / name
            if path.is_file():
                _runtime_handles.append(ctypes.WinDLL(str(path)))


class NativeWorkbench:
    def __init__(self):
        if sys.platform != 'win32':
            raise WorkbenchError('SC201 工作台组件需要 Windows x64。')
        path = NATIVE_DIRECTORY / 'sc201_workbench.dll'
        if not path.is_file():
            raise WorkbenchError('尚未构建工作台组件，请运行 python -m sc201_workbench.build_native。')
        prepare_runtime()
        # Resolve Qt imports from the host's single PyQt5 runtime before loading
        # the C++ widget. No second QApplication/Qt DLL tree is introduced.
        from PyQt5 import QtBluetooth, QtNetwork, QtWidgets, sip
        from PyQt5.QtCore import QT_VERSION_STR
        self.sip = sip
        self.widget_type = QtWidgets.QWidget
        try:
            self.library = ctypes.CDLL(str(path))
        except OSError as error:
            raise WorkbenchError(f'工作台组件加载失败：{error}') from error
        api = self.library
        api.rtt_sc201_qt_version.restype = ctypes.c_char_p
        api.rtt_sc201_last_error.restype = ctypes.c_char_p
        api.rtt_sc201_create.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        api.rtt_sc201_create.restype = ctypes.c_void_p
        api.rtt_sc201_state.argtypes = [ctypes.c_void_p]
        api.rtt_sc201_state.restype = ctypes.c_char_p
        api.rtt_sc201_shutdown.argtypes = [ctypes.c_void_p]
        api.rtt_sc201_shutdown.restype = ctypes.c_int
        if api.rtt_sc201_qt_version().decode('ascii') != QT_VERSION_STR:
            raise WorkbenchError('工作台与 RTT 使用的 Qt 版本不一致，请重新构建组件。')

    def error(self) -> str:
        return (self.library.rtt_sc201_last_error() or b'').decode('utf-8', errors='replace')

    def create(self, parent, directory: Path):
        handle = self.library.rtt_sc201_create(self.sip.unwrapinstance(parent), str(directory.resolve()))
        if not handle:
            raise WorkbenchError(self.error())
        return self.sip.wrapinstance(handle, self.widget_type)

    def state(self, widget) -> dict:
        return json.loads(self.library.rtt_sc201_state(self.sip.unwrapinstance(widget)).decode('utf-8'))

    def shutdown(self, widget) -> bool:
        return bool(self.library.rtt_sc201_shutdown(self.sip.unwrapinstance(widget)))
