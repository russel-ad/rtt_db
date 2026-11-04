"""Build an isolated real 2.10.0 -> 2.10.1 updater test; never uploads."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_update import APP_ENTRY, APP_ID, APP_VERSION
from app_update.core import atomic_json, digest, read_json, safe_members, verify, version
from app_update.publish import load_key, make_release


def log(message):
    print(message, flush=True)


def run(command, directory, logfile):
    with logfile.open('wb') as output:
        result = subprocess.run(command, cwd=directory, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError('Build command failed; inspect ' + str(logfile))


def snapshot(destination):
    destination.mkdir()
    sources = [p for p in ROOT.iterdir() if p.is_file() and p.suffix in ('.py', '.ps1', '.ico', '.png')]
    for name in ('app_update', 'bds', 'onmicro_dfu', 'factory_pair', 'sc201_workbench', 'images', 'docs'):
        sources.extend((ROOT/name).rglob('*'))
    forbidden = {'__pycache__', '.git', '.venv', 'private', 'aaa_log', 'build', 'dist'}
    records = {}
    for source in sorted(sources):
        relative = source.relative_to(ROOT)
        if not source.is_file() or forbidden.intersection(relative.parts) or source.suffix in ('.pyc', '.pyo'):
            continue
        if source.is_symlink():
            raise RuntimeError('Unexpected source symlink')
        target = destination/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        records[relative.as_posix()] = digest(target)
    return records


def launcher_source(baseline, target):
    return '''"""Isolated updater test bootstrap; production data stays in its own profile."""
import os
from pathlib import Path
import sys
from app_update.core import atomic_json, read_json
from app_update.launcher import run_application

def main():
    root = Path(sys.executable).resolve().parent
    home = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'RTTToolUpdateTest' / 'v{target}' / 'updates'
    settings = read_json(root / 'updater.json', {{}})
    if len(sys.argv) == 3 and sys.argv[1] == '--launcher-check':
        atomic_json(Path(sys.argv[2]), {{'passed': True, 'bundled_version': '{baseline}',
            'target_version': '{target}', 'home': str(home), 'feed_url': settings.get('feed_url'), 'device_access': False}})
        return 0
    try:
        return run_application(root, home, settings, bundled_version='{baseline}')
    except Exception as error:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(error), 'RTT Update Test', 0x10)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
'''.format(baseline=baseline, target=target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suffix', default='update_test_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    parser.add_argument('--target-version', default='2.10.1')
    parser.add_argument('--tag', default='v2.10.1-test')
    parser.add_argument('--baseline-release', type=Path, default=ROOT/'dist/RTTTool_Release_2.10.0_r3')
    parser.add_argument('--private-key', type=Path, default=Path(os.environ['LOCALAPPDATA'])/'RTTToolPublisher/release.key')
    args = parser.parse_args()
    if not re.fullmatch('[a-z0-9_]+', args.suffix) or not re.fullmatch('[A-Za-z0-9._-]+', args.tag):
        parser.error('Unsafe build suffix or release tag')
    baseline = APP_VERSION
    assert version(args.target_version) > version(baseline)
    proof = ROOT/'build'/args.suffix
    proof.mkdir(parents=True, exist_ok=False)
    settings = read_json(ROOT/'app_update/release_config.json')
    baseline_envelope = read_json(args.baseline_release/'latest.json')
    baseline_payload = verify(baseline_envelope, settings['public_keys'])
    assert baseline_payload['version'] == baseline
    baseline_zip = args.baseline_release/('RTTTool-' + baseline + '-windows-x64.zip')
    assert digest(baseline_zip) == baseline_payload['sha256'] and baseline_zip.stat().st_size == baseline_payload['size']
    feed = 'https://github.com/russel-ad/rtt_db/releases/download/' + args.tag + '/latest.json'
    package_url = 'https://github.com/russel-ad/rtt_db/releases/download/' + args.tag + '/RTTTool-' + args.target_version + '-windows-x64.zip'
    base = ROOT/'dist'/('RTTTool_UpdateTest_' + baseline + '_to_' + args.target_version)
    release = ROOT/'dist'/('RTTTool_Release_' + args.target_version + '_test')
    if base.exists() or release.exists() or Path(str(base) + '.zip').exists():
        raise RuntimeError('Preserve the previous test build and choose another target version')
    atomic_json(proof/'checkpoint.json', {'status': 'BUILDING', 'tag': args.tag,
        'baseline': baseline, 'target': args.target_version, 'public_upload_authorized': True,
        'production_latest_to_preserve': 'v2.10.0', 'hardware_access': False})
    source = proof/'source'
    records = snapshot(source)
    version_file = source/'app_update/__init__.py'
    text = version_file.read_text(encoding='utf-8')
    assert text.count("APP_VERSION = '" + baseline + "'") == 1
    version_file.write_text(text.replace("APP_VERSION = '" + baseline + "'", "APP_VERSION = '" + args.target_version + "'"), encoding='utf-8')
    atomic_json(proof/'source_snapshot.json', {'original_source_sha256': records,
        'snapshot_version_sha256': digest(version_file), 'production_version': baseline,
        'target_version': args.target_version, 'production_source_changed': False})
    log('Building real application ' + args.target_version + ' from isolated sources...')
    run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(source/'build_ble_dfu.ps1'),
         '-Workbench', '-OneDir', '-Suffix', args.suffix], source, proof/'application-build.log')
    executable_name = 'rtt_t2_sc201_workbench_portable_' + args.suffix
    application = source/'dist'/('sc201_workbench_portable_' + args.suffix)/executable_name
    (application/(executable_name + '.exe')).rename(application/APP_ENTRY)
    atomic_json(application/'app-release.json', {'app_id': APP_ID, 'version': args.target_version, 'entry': APP_ENTRY})
    envelope = make_release(application, release, args.target_version, package_url, load_key(args.private_key))
    verify(envelope, settings['public_keys'])
    log('Building isolated test launcher...')
    entry = source/'rtt_update_test_launcher.py'
    entry.write_text(launcher_source(baseline, args.target_version), encoding='utf-8')
    launch_dist = proof/'launcher-dist'
    run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onedir', '--windowed',
         '--name', 'RTTTool_UpdateTest', '--icon', str(ROOT/'tool.ico'),
         '--exclude-module', 'PyQt5', '--exclude-module', 'PySide6', '--exclude-module', 'numpy',
         '--distpath', str(launch_dist), '--workpath', str(proof/'launcher-work'),
         '--specpath', str(proof), str(entry)], source, proof/'launcher-build.log')
    shutil.copytree(launch_dist/'RTTTool_UpdateTest', base)
    installed = base/'app'/baseline
    installed.mkdir(parents=True)
    with zipfile.ZipFile(baseline_zip) as archive:
        safe_members(archive)
        archive.extractall(installed)
    test_settings = dict(settings, feed_url=feed)
    atomic_json(base/'updater.json', test_settings)
    instructions = (ROOT/'docs/TEST_AUTO_UPDATE.md').read_text(encoding='utf-8')
    (base/'TEST_AUTO_UPDATE.md').write_text(instructions, encoding='utf-8')
    archive_path = base.parent/(base.name + '.zip')
    with zipfile.ZipFile(archive_path, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for path in sorted(base.rglob('*')):
            if path.is_file():
                output.write(path, base.name + '/' + path.relative_to(base).as_posix())
    package = release/('RTTTool-' + args.target_version + '-windows-x64.zip')
    records = []
    for path in (archive_path, package, release/'latest.json'):
        records.append({'path': str(path), 'bytes': path.stat().st_size, 'sha256': digest(path)})
    result = {'status': 'BUILT_PENDING_VALIDATION', 'created_at': datetime.now(timezone.utc).isoformat(),
        'repository': 'russel-ad/rtt_db', 'tag': args.tag, 'prerelease': True, 'make_latest': False,
        'baseline_version': baseline, 'target_version': args.target_version,
        'feed_url': feed, 'test_home': '%LOCALAPPDATA%/RTTToolUpdateTest/v' + args.target_version + '/updates',
        'launcher': str(base/'RTTTool_UpdateTest.exe'), 'application_folder': str(application),
        'assets': records, 'signed_release': envelope, 'uploaded': False,
        'private_key_in_package': False, 'production_source_changed': False}
    atomic_json(proof/'build.json', result)
    atomic_json(proof/'checkpoint.json', {'status': result['status'], 'tag': args.tag,
        'build_record': str(proof/'build.json'), 'uploaded': False, 'hardware_access': False})
    log('Test bundle: ' + str(archive_path))
    log('Signed target: ' + str(package))


if __name__ == '__main__':
    main()
