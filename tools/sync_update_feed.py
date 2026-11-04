"""Copy a verified release envelope into the repository mirror; never pushes."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_update.core import UpdateError, atomic_json, read_json, verify, version


def synchronize(manifest, output, settings):
    envelope = read_json(manifest)
    payload = verify(envelope, settings['public_keys'])
    if output.exists():
        previous = verify(read_json(output), settings['public_keys'])
        if version(payload['version']) < version(previous['version']):
            raise UpdateError('Refusing to downgrade the public feed mirror')
        if payload['version'] == previous['version'] and payload['sha256'] != previous['sha256']:
            raise UpdateError('Refusing to replace an immutable mirrored release')
    atomic_json(output, envelope)
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT/'updates/stable.json')
    args = parser.parse_args()
    payload = synchronize(args.manifest, args.output, read_json(ROOT/'app_update/release_config.json'))
    print('Verified mirror prepared for v' + payload['version'] + ': ' + str(args.output))
    print('Publish and verify the matching Release assets before committing/pushing this feed.')


if __name__ == '__main__':
    main()
