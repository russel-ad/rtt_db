"""Build a portable bootstrap and signed app-only release; never uploads files."""
from datetime import datetime
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_update import APP_ENTRY, APP_ID, APP_VERSION
from app_update.core import atomic_json, digest
from app_update.publish import load_key, make_release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-key', type=Path, required=True)
    parser.add_argument('--suffix', default='auto_update_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    parser.add_argument('--output-suffix', default='', help='Preserve an earlier local, unpublished candidate')
    args = parser.parse_args()
    if not args.suffix.replace('_', '').isalnum():
        parser.error('suffix must contain only letters, numbers and underscores')
    if args.output_suffix and not args.output_suffix.replace('_', '').isalnum():
        parser.error('output suffix must contain only letters, numbers and underscores')
    proof = ROOT / 'build' / args.suffix
    proof.mkdir(parents=True, exist_ok=False)
    base = ROOT / 'dist' / ('RTTTool_AutoUpdate_' + APP_VERSION + args.output_suffix)
    if base.exists():
        raise RuntimeError('Preserve the previous base distribution; choose a new version')
    settings = ROOT / 'app_update/release_config.json'
    assert settings.is_file(), 'Run the publisher keygen once before building'
    command = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT/'build_ble_dfu.ps1'),
               '-Workbench', '-OneDir', '-Suffix', args.suffix]
    with (proof/'application-build.log').open('wb') as log:
        result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    # The script's Python ZIP check is authoritative even if a shell wrapper
    # reports a nonzero status after native diagnostics on stderr.
    app_folder = ROOT/'dist'/('sc201_workbench_portable_' + args.suffix)/('rtt_t2_sc201_workbench_portable_' + args.suffix)
    source_exe = app_folder / ('rtt_t2_sc201_workbench_portable_' + args.suffix + '.exe')
    if not source_exe.is_file():
        raise RuntimeError('Application build failed; inspect application-build.log')
    launcher_dist = proof / 'launcher-dist'
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onedir', '--windowed',
               '--name', 'RTTTool', '--icon', str(ROOT/'tool.ico'), '--exclude-module', 'PyQt5',
               '--exclude-module', 'PySide6', '--exclude-module', 'numpy',
               '--distpath', str(launcher_dist), '--workpath', str(proof/'launcher-work'),
               '--specpath', str(proof), str(ROOT/'rtt_update_launcher.py')]
    with (proof/'launcher-build.log').open('wb') as log:
        subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
    shutil.copytree(launcher_dist/'RTTTool', base)
    installed = base/'app'/APP_VERSION
    shutil.copytree(app_folder, installed)
    (installed/source_exe.name).rename(installed/APP_ENTRY)
    atomic_json(installed/'app-release.json', {'app_id': APP_ID, 'version': APP_VERSION, 'entry': APP_ENTRY})
    shutil.copyfile(settings, base/'updater.json')
    shutil.copyfile(ROOT/'docs/AUTO_UPDATE.md', base/'AUTO_UPDATE.md')
    release = ROOT/'dist'/('RTTTool_Release_' + APP_VERSION + args.output_suffix)
    url = f'https://github.com/russel-ad/rtt_db/releases/download/v{APP_VERSION}/RTTTool-{APP_VERSION}-windows-x64.zip'
    signed = make_release(installed, release, APP_VERSION, url, load_key(args.private_key))
    archive = base.parent/(base.name + '.zip')
    if archive.exists():
        raise RuntimeError('Preserve an existing base ZIP')
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for path in sorted(base.rglob('*')):
            if path.is_file():
                output.write(path, base.name + '/' + path.relative_to(base).as_posix())
    manifest = dict(version=APP_VERSION, bootstrap=str(base/'RTTTool.exe'),
        bootstrap_zip=str(archive), bootstrap_zip_sha256=digest(archive),
        release_directory=str(release), signed_release=signed, application_build_exit=result.returncode,
        uploaded=False, private_key_in_package=False)
    atomic_json(proof/'build.json', manifest)
    print('Portable auto-update base: ' + str(archive))
    print('Signed release assets: ' + str(release))


if __name__ == '__main__':
    main()
