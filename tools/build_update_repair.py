"""Build a small onedir recovery utility; never upload or install anything."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_update import APP_VERSION
from app_update.core import atomic_json, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suffix', required=True)
    args = parser.parse_args()
    if not args.suffix.replace('_', '').isalnum():
        parser.error('Invalid build suffix')
    proof = ROOT/'build'/args.suffix
    proof.mkdir(exist_ok=False)
    name = 'RTTTool_UpdateRepair_' + APP_VERSION
    dist = ROOT/'dist'/name
    archive = dist.parent/(dist.name + '.zip')
    if dist.exists() or archive.exists():
        raise RuntimeError('Preserve existing artifacts; choose a new version')
    temporary = proof/'dist'
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onedir', '--windowed',
        '--name', 'RTTTool_UpdateRepair', '--icon', str(ROOT/'tool.ico'),
        '--paths', str(ROOT), '--add-data', str(ROOT/'app_update/release_config.json') + ';app_update',
        '--collect-data', 'certifi', '--hidden-import', 'truststore._windows',
        '--copy-metadata', 'certifi', '--copy-metadata', 'truststore',
        '--exclude-module', 'PyQt5', '--exclude-module', 'PySide6', '--exclude-module', 'numpy',
        '--distpath', str(temporary), '--workpath', str(proof/'work'), '--specpath', str(proof),
        str(ROOT/'tools/repair_auto_update.py')]
    with (proof/'build.log').open('wb') as log:
        subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
    shutil.copytree(temporary/'RTTTool_UpdateRepair', dist)
    shutil.copyfile(ROOT/'docs/UPDATE_DOWNLOAD_REPAIR.md', dist/'使用说明.md')
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for path in sorted(dist.rglob('*')):
            if path.is_file():
                output.write(path, dist.name + '/' + path.relative_to(dist).as_posix())
    atomic_json(proof/'build.json', {'version': APP_VERSION, 'zip': str(archive),
        'exe': str(dist/'RTTTool_UpdateRepair.exe'), 'bytes': archive.stat().st_size,
        'sha256': digest(archive), 'uploaded': False, 'hardware_access': False})
    print(str(archive), flush=True)


if __name__ == '__main__':
    main()
