"""Update verification, bounded downloads and side-by-side installation.

Only the launcher changes current.json, while holding its session lock after
the GUI has exited. The GUI downloads and atomically publishes pending.json.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import ssl
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from . import APP_ENTRY, APP_ID, LAUNCHER_API

MAX_PACKAGE = 512 * 1024 * 1024
MAX_UNPACKED = 2 * 1024 * 1024 * 1024
MAX_FILES = 30000


class UpdateError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise UpdateError('Invalid application version')
    return tuple(map(int, value.split('.')))


def read_json(path, default=None):
    if not path.exists():
        return default
    if path.stat().st_size > 128 * 1024:
        raise UpdateError('Update metadata exceeds the size limit')
    return json.loads(path.read_text(encoding='utf-8-sig'))


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def digest(path):
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def switch_directory(staging, target):
    """Windows scanners can briefly hold freshly unpacked DLL/EXE handles.
    Retry only sharing/access failures, with a finite deadline; the current
    version pointer is unchanged throughout this operation.
    """
    deadline = time.monotonic() + 10
    while True:
        try:
            os.replace(staging, target)
            return
        except OSError as error:
            if getattr(error, 'winerror', None) not in (5, 32, 33) or time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


@contextmanager
def file_lock(path):
    """A process-owned lock; never infer exclusivity from a PID or lock file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise UpdateError('Another application/update session is running') from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def https_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise UpdateError('Update addresses must be HTTPS without embedded credentials')
    return url


class HttpsRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        https_url(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def fetch(url, *, offset=0, timeout=45, attempts=3, on_retry=lambda attempt: None):
    https_url(url)
    from .tls import VerifiedHTTPS
    headers = {'User-Agent': 'RTTTool-Updater/1', 'Cache-Control': 'no-cache', 'Accept-Encoding': 'identity'}
    if offset:
        headers['Range'] = 'bytes=' + str(offset) + '-'
    request = urllib.request.Request(url, headers=headers)
    for attempt in range(attempts):
        try:
            response = urllib.request.build_opener(HttpsRedirect(), VerifiedHTTPS()).open(request, timeout=timeout)
            try:
                https_url(response.geturl())
            except Exception:
                response.close()
                raise
            return response
        except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLEOFError) as error:
            if isinstance(error, urllib.error.HTTPError) and error.code not in (429, 500, 502, 503, 504):
                raise
            reason = getattr(error, 'reason', error)
            if isinstance(reason, ssl.SSLCertVerificationError) or attempt+1 == attempts:
                raise
            on_retry(attempt + 2)
            time.sleep(attempt + 1)


def verify(envelope, keys, *, now=None):
    try:
        payload = envelope['payload']
        public = base64.b64decode(keys[envelope['key_id']], validate=True)
        signature = base64.b64decode(envelope['signature'], validate=True)
        Ed25519PublicKey.from_public_bytes(public).verify(signature, canonical(payload))
        if payload['schema'] != 1 or payload['app_id'] != APP_ID or payload['platform'] != 'windows-x64':
            raise UpdateError('Release targets another application or platform')
        version(payload['version'])
        if payload['entry'] != APP_ENTRY or type(payload['min_launcher']) is not int or not 1 <= payload['min_launcher'] <= LAUNCHER_API:
            raise UpdateError('This release needs a newer base launcher')
        if type(payload['size']) is not int or not 0 < payload['size'] <= MAX_PACKAGE:
            raise UpdateError('Invalid update package length')
        if not re.fullmatch('[a-f0-9]{64}', payload['sha256']):
            raise UpdateError('Invalid update hash')
        https_url(payload['url'])
        published = datetime.fromisoformat(payload['published_at'])
        expires = datetime.fromisoformat(payload['expires_at'])
        current = now or datetime.now(timezone.utc)
        if published.tzinfo is None or expires.tzinfo is None or not published <= current < expires:
            raise UpdateError('Release metadata expired or is not yet valid')
        return payload
    except UpdateError:
        raise
    except Exception as error:
        raise UpdateError('Release signature or metadata is invalid') from error


def archive_path(home, payload):
    return home / 'cache' / (payload['version'] + '-' + payload['sha256'][:16] + '.zip')


def check_and_stage(home, settings, current_version, *, opener=None, progress=lambda text: None,
                    on_transfer=lambda data: None):
    """Network I/O only: never switches or executes an application."""
    if not settings.get('enabled') or not settings.get('public_keys') or not settings.get('feed_url'):
        return None
    custom_opener = opener
    if opener is None:
        def opener(url):
            return fetch(url, timeout=15, attempts=1)
    with file_lock(home / 'download.lock'):
        progress('正在检查上位机更新…')
        from .manifest import read_manifest
        envelope, manifest_source = read_manifest(settings, opener=opener, progress=progress)
        payload = verify(envelope, settings['public_keys'])
        atomic_json(home / 'manifest-source.json', {'url': manifest_source, 'version': payload['version'],
            'at': datetime.now(timezone.utc).isoformat()})
        seen = read_json(home / 'seen.json', {})
        last = max(version(seen.get('version', current_version)), version(current_version))
        if version(payload['version']) < last:
            raise UpdateError('Rejected an older release manifest')
        if payload['version'] == seen.get('version') and payload['sha256'] != seen['sha256']:
            raise UpdateError('The immutable release package changed')
        atomic_json(home / 'seen.json', {'version': payload['version'], 'sha256': payload['sha256']})
        state = read_json(home / 'current.json', {})
        if version(payload['version']) <= version(current_version) or payload['version'] in state.get('failed_versions', []):
            return None
        destination = archive_path(home, payload)
        destination.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        last_report = started
        previous_received = 0
        network_received = 0
        def report(phase, received, *, force=False):
            nonlocal last_report, previous_received, network_received
            if phase == 'download':
                network_received += max(0, received - previous_received)
            previous_received = received
            now = time.monotonic()
            if force or now - last_report >= 0.2:
                elapsed = max(0.0, now - started)
                on_transfer({'phase': phase, 'version': payload['version'], 'received': received,
                    'total': payload['size'], 'speed': network_received/elapsed if elapsed > 0 and phase == 'download' else 0.0,
                    'elapsed': elapsed})
                last_report = now
        report('connecting', 0, force=True)
        if not destination.exists() or destination.stat().st_size != payload['size'] or digest(destination) != payload['sha256']:
            from .download import download_package
            def open_package(offset):
                if custom_opener is not None:
                    # Keep the original test/custom transport contract. A
                    # transport without Range support safely restarts on 200.
                    return custom_opener(payload['url'])
                return fetch(payload['url'], offset=offset,
                    on_retry=lambda attempt: progress('正在重新连接下载服务器（' + str(attempt) + '/3）…'))
            download_package(destination, payload, open_response=open_package, progress=progress,
                on_progress=lambda phase, received: report(phase, received,
                    force=phase != 'download' or received == payload['size']))
        else:
            report('cached', payload['size'], force=True)
        atomic_json(home / 'pending.json', envelope)
        report('ready', payload['size'], force=True)
        return payload


def safe_members(archive):
    members = archive.infolist()
    if len(members) > MAX_FILES or sum(item.file_size for item in members) > MAX_UNPACKED:
        raise UpdateError('Unpacked update exceeds the size/file limit')
    names = set()
    for item in members:
        path = PurePosixPath(item.filename)
        parts = path.parts
        if not parts or path.is_absolute() or '\\' in item.filename or '..' in parts:
            raise UpdateError('Unsafe path in update archive')
        for part in parts:
            base = part.split('.')[0].upper()
            if (part in ('.', '..') or ':' in part or part.endswith((' ', '.')) or
                    base in {'CON', 'PRN', 'AUX', 'NUL'} or re.fullmatch(r'(COM|LPT)[1-9]', base)):
                raise UpdateError('Unsafe Windows file name in update archive')
        if stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1:
            raise UpdateError('Links/encrypted entries are not update files')
        name = '/'.join(parts).casefold()
        if name in names:
            raise UpdateError('Duplicate update archive path')
        names.add(name)
    return members


def install_pending(home, settings, bundled_version):
    """Caller holds session.lock and no GUI process exists."""
    envelope = read_json(home / 'pending.json')
    if envelope is None:
        return False
    payload = verify(envelope, settings['public_keys'])
    seen = read_json(home / 'seen.json', {})
    if version(payload['version']) < version(seen.get('version', payload['version'])):
        raise UpdateError('A newer release has already been observed')
    state = read_json(home / 'current.json', {'current': bundled_version, 'failed_versions': []})
    if version(payload['version']) <= version(state['current']) or payload['version'] in state.get('failed_versions', []):
        return False
    package = archive_path(home, payload)
    if not package.is_file() or package.stat().st_size != payload['size'] or digest(package) != payload['sha256']:
        raise UpdateError('Staged update package is incomplete or changed')
    versions = home / 'versions'
    versions.mkdir(parents=True, exist_ok=True)
    target = versions / payload['version']
    if target.exists():
        installed = read_json(target / 'installed.json', {})
        if installed.get('sha256') != payload['sha256']:
            raise UpdateError('An existing version has different contents')
    else:
        staging = versions / ('.stage-' + uuid.uuid4().hex)
        staging.mkdir()
        try:
            with zipfile.ZipFile(package) as archive:
                for item in safe_members(archive):
                    destination = staging.joinpath(*PurePosixPath(item.filename).parts)
                    if item.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(item) as source, destination.open('xb') as stream:
                            shutil.copyfileobj(source, stream, 1024 * 1024)
            release = read_json(staging / 'app-release.json', {})
            if release != {'app_id': APP_ID, 'version': payload['version'], 'entry': APP_ENTRY} or not (staging / APP_ENTRY).is_file():
                raise UpdateError('Package entry/version does not match the signed release')
            atomic_json(staging / 'installed.json', {'sha256': payload['sha256']})
            switch_directory(staging, target)
        finally:
            # Only remove the newly created staging directory, never data or an
            # arbitrary metadata-derived path. Check the absolute boundary first.
            if staging.exists():
                if staging.parent.resolve() != versions.resolve() or not staging.name.startswith('.stage-'):
                    raise UpdateError('Invalid staging cleanup boundary')
                shutil.rmtree(staging)
    atomic_json(home / 'current.json', dict(current=payload['version'], previous=state['current'],
        trial=True, failed_versions=state.get('failed_versions', [])))
    (home / 'pending.json').unlink(missing_ok=True)
    return True


def rollback(home):
    state = read_json(home / 'current.json')
    if not state or not state.get('trial') or not state.get('previous'):
        return False
    failed = sorted(set(state.get('failed_versions', []) + [state['current']]))
    atomic_json(home / 'current.json', {'current': state['previous'], 'trial': False, 'failed_versions': failed})
    return True
