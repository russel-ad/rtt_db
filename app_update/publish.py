"""Offline release signing. Private signing keys never enter an application ZIP."""
from __future__ import annotations
import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from . import APP_ENTRY, APP_ID, LAUNCHER_API
from .core import MAX_PACKAGE, UpdateError, atomic_json, canonical, digest, https_url, read_json, safe_members, version


def dpapi(data, *, decrypt=False):
    if os.name != 'nt':
        raise UpdateError('Publisher key protection requires the Windows user account')
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise UpdateError('Windows could not protect/unprotect the release key')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(output.data)


def keygen(private_path, settings_path, feed_url):
    if private_path.exists():
        raise UpdateError('A release key already exists; preserve it for future releases')
    https_url(feed_url)
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    private_path.parent.mkdir(parents=True, exist_ok=True)
    with private_path.open('xb') as stream:
        stream.write(b'RTT-DPAPI-1\n' + dpapi(private))
    key_id = hashlib.sha256(public).hexdigest()[:16]
    atomic_json(settings_path, dict(enabled=True, feed_url=feed_url,
                                    public_keys={key_id: base64.b64encode(public).decode('ascii')}))
    return key_id


def load_key(path):
    data = path.read_bytes()
    prefix = b'RTT-DPAPI-1\n'
    if not data.startswith(prefix):
        raise UpdateError('Not a protected RTT publisher key')
    return Ed25519PrivateKey.from_private_bytes(dpapi(data[len(prefix):], decrypt=True))


def make_release(folder, output, release_version, url, private_key, *, now=None):
    version(release_version)
    https_url(url)
    expected = {'app_id': APP_ID, 'version': release_version, 'entry': APP_ENTRY}
    if read_json(folder / 'app-release.json') != expected or not (folder / APP_ENTRY).is_file():
        raise UpdateError('Build app-release.json and application version first; do not relabel old binaries')
    forbidden = {'private', 'config.json', 'aaa_log', '.git', 'updater.json'}
    if any(path.name in forbidden for path in folder.iterdir()):
        raise UpdateError('Release source contains user data or updater settings')
    output.mkdir(parents=True, exist_ok=True)
    package = output / ('RTTTool-' + release_version + '-windows-x64.zip')
    if package.exists() or (output / 'latest.json').exists():
        raise UpdateError('Choose a new release output directory')
    with zipfile.ZipFile(package, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(folder.rglob('*')):
            if path.is_symlink():
                raise UpdateError('Release directories cannot contain symbolic links')
            if path.is_file():
                archive.write(path, path.relative_to(folder).as_posix())
    with zipfile.ZipFile(package) as archive:
        safe_members(archive)
    if package.stat().st_size > MAX_PACKAGE:
        raise UpdateError('Release exceeds the download limit')
    current = now or datetime.now(timezone.utc)
    payload = dict(schema=1, app_id=APP_ID, version=release_version, platform='windows-x64',
        url=url, size=package.stat().st_size, sha256=digest(package), entry=APP_ENTRY,
        min_launcher=LAUNCHER_API, published_at=current.isoformat(), expires_at=(current + timedelta(days=90)).isoformat())
    public = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    envelope = dict(payload=payload, key_id=hashlib.sha256(public).hexdigest()[:16],
                    signature=base64.b64encode(private_key.sign(canonical(payload))).decode('ascii'))
    atomic_json(output / 'latest.json', envelope)
    return envelope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    generate = commands.add_parser('keygen')
    generate.add_argument('--private-key', type=Path, required=True)
    generate.add_argument('--settings', type=Path, required=True)
    generate.add_argument('--feed-url', required=True)
    release = commands.add_parser('release')
    release.add_argument('--private-key', type=Path, required=True)
    release.add_argument('--app-folder', type=Path, required=True)
    release.add_argument('--output', type=Path, required=True)
    release.add_argument('--version', required=True)
    release.add_argument('--url', required=True)
    args = parser.parse_args()
    if args.command == 'keygen':
        print('Public release key ID: ' + keygen(args.private_key, args.settings, args.feed_url))
    else:
        manifest = make_release(args.app_folder, args.output, args.version, args.url, load_key(args.private_key))
        print('Signed release: ' + manifest['payload']['version'])


if __name__ == '__main__':
    main()
