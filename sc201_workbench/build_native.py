"""Build the original C++ workbench as a Qt widget DLL, without editing its checkout."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
NATIVE = Path(__file__).resolve().parent / 'native'


def find_qt(source: Path) -> Path:
    if os.environ.get('SC201_QT_DIR'):
        return Path(os.environ['SC201_QT_DIR'])
    for cache in sorted((source / 'build').glob('windows_package_*/compile/CMakeCache.txt'), reverse=True):
        match = re.search(r'^Qt5_DIR:PATH=(.+)$', cache.read_text(encoding='utf-8'), re.M)
        if match:
            candidate = Path(match.group(1).strip()).parents[2]
            if (candidate / 'lib/cmake/Qt5/Qt5Config.cmake').is_file():
                return candidate
    raise RuntimeError('Pass --qt-dir pointing to the Qt 5.15.2 MSVC x64 SDK')


def inputs(source: Path) -> dict[str, str]:
    paths = [source / 'CMakeLists.txt']
    for folder in ('src', 'config', 'cmake'):
        paths.extend(path for path in (source / folder).rglob('*') if path.is_file())
    return {path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


def run(command: list[str], log: Path):
    with log.open('w', encoding='utf-8') as output:
        result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, text=True,
                                encoding='utf-8', errors='replace')
    if result.returncode:
        raise RuntimeError(f'Build failed ({result.returncode}); see {log}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('E:/onm/sc201_host'))
    parser.add_argument('--qt-dir', type=Path)
    parser.add_argument('--build-dir', type=Path, default=ROOT / 'build/sc201_workbench_native_product')
    parser.add_argument('--generator', default='Visual Studio 18 2026')
    parser.add_argument('--tests', action='store_true', help='Also build isolated batch OTA native tests')
    args = parser.parse_args()
    source = args.source.resolve()
    qt = args.qt_dir or find_qt(source)
    before = inputs(source)
    args.build_dir.mkdir(parents=True, exist_ok=True)
    cmake = shutil.which('cmake')
    if not cmake:
        raise RuntimeError('CMake is not installed')
    print(f'Configuring native workbench from {source}', flush=True)
    run([cmake, '-S', str(source), '-B', str(args.build_dir), '-G', args.generator, '-A', 'x64',
         f'-DRTT_WORKBENCH_SOURCE={NATIVE.as_posix()}', '-DMLINK_HOST_BUILD_TESTS=OFF',
         f'-DRTT_WORKBENCH_BUILD_TESTS={"ON" if args.tests else "OFF"}',
         f'-DCMAKE_PROJECT_sc201_host_INCLUDE={(NATIVE / "InjectWorkbench.cmake").as_posix()}',
         f'-DCMAKE_PREFIX_PATH={qt.as_posix()}',
         f'-DMLINK_MBEDTLS_SOURCE={(source / "build/deps/mbedtls-3.6.7").as_posix()}'],
        args.build_dir / 'configure.log')
    print('Compiling original UI, workflows and frozen protocol libraries', flush=True)
    targets = ['sc201_workbench'] + (['test_rtt_batch_ota'] if args.tests else [])
    run([cmake, '--build', str(args.build_dir), '--config', 'Release', '--target', *targets,
         '--parallel', '4'], args.build_dir / 'build.log')
    if before != inputs(source):
        raise RuntimeError('The source checkout changed during the build; output was not deployed')
    output = args.build_dir / 'Release/sc201_workbench.dll'
    shutil.copy2(output, NATIVE / output.name)
    runtime = NATIVE / 'runtime'
    runtime.mkdir(exist_ok=True)
    ssl = source / 'build/windows_package_20261030/deps/openssl'
    for name in ('libcrypto-1_1-x64.dll', 'libssl-1_1-x64.dll'):
        if not (ssl / name).is_file():
            raise RuntimeError(f'Missing Qt HTTPS dependency: {ssl / name}')
        shutil.copy2(ssl / name, runtime / name)
        # The PyQt5 QtNetwork build also probes the suffix without "-x64".
        # Supply the same checked x64 library for both loader conventions.
        shutil.copy2(ssl / name, runtime / name.replace('-x64', ''))
    redist = Path('C:/Program Files (x86)/Microsoft Visual Studio/18/BuildTools/VC/Redist/MSVC')
    crt = next(iter(sorted(redist.glob('*/x64/Microsoft.VC145.CRT'), reverse=True)), None)
    if not crt:
        raise RuntimeError('MSVC runtime directory not found')
    for path in crt.glob('*.dll'):
        shutil.copy2(path, runtime / path.name)
    licenses = NATIVE.parent / 'licenses'
    shutil.copytree(source / 'tools/packaging/licenses', licenses, dirs_exist_ok=True)
    shutil.copy2(source / 'build/deps/mbedtls-3.6.7/LICENSE', licenses / 'MbedTLS-LICENSE.txt')
    manifest = {'source_directory': str(source), 'source_data_directory': str(source / 'private/workbench'),
                'qt_version': '5.15.2', 'source_inputs_sha256': before,
                'batch_ota': {'available': True, 'default_concurrency': 2, 'min_concurrency': 1, 'max_concurrency': 4,
                              'separate_sessions_and_backends': True, 'success_requires_post_reboot_version': True,
                              'throughput_preferred_default': False, 'throughput_api': 'Windows 11 BluetoothLEDevice6',
                              'compact_ota_log_default': True, 'refresh_interval_ms': 250,
                              'transfer_and_total_clocks': True, 'single_outstanding_att_write': True,
                              'prepare_before_transfer_concurrency_min': 3, 'sequential_app_preparation': True,
                              'att_fitted_blocks_default': False, 'windows_write_timing_summary': True,
                              'event_driven_ota_default': True, 'immediate_poll_budget': 8,
                              'block_confirmation_timing_summary': True},
                'device_browser': {'rssi_filter': True, 'rssi_filter_default': True,
                                   'rssi_min_dbm_default': -60, 'hidden_devices_checked': False,
                                   'filter_preferences_persisted': True},
                'library_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
                'integration_inputs_sha256': {path.relative_to(NATIVE).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                                             for path in NATIVE.rglob('*') if path.is_file()
                                             and (path.suffix in {'.h', '.cpp', '.cmake'} or path.name == 'CMakeLists.txt')},
                'source_git_head': subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip(),
                'mlink_git_head': subprocess.check_output(['git', '-C', str(source / 'libmlink'), 'rev-parse', 'HEAD'], text=True).strip()}
    (NATIVE / 'build.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Built {output.name}; original checkout unchanged; no device/server operations', flush=True)


if __name__ == '__main__':
    main()
