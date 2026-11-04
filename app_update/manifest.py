"""Read signed release metadata through bounded, independent HTTPS routes."""
from __future__ import annotations

import base64
import http.client
import json
import ssl
import time
import urllib.error
import urllib.parse

from .core import UpdateError, https_url

OFFICIAL_FEED = 'https://github.com/russel-ad/rtt_db/releases/latest/download/latest.json'
OFFICIAL_API = 'https://api.github.com/repos/russel-ad/rtt_db/contents/updates/stable.json'
MAX_MANIFEST = 65536
READ_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError,
               http.client.IncompleteRead, json.JSONDecodeError)


class ManifestUnavailable(UpdateError):
    def __init__(self, attempts):
        self.attempts = attempts
        super().__init__('Update manifest unavailable: ' + '; '.join(
            item['error_type'] + ': ' + item['error'] for item in attempts))


def sources(settings):
    feed = https_url(settings['feed_url'])
    # Old installed launchers retain their original updater.json. Derive the
    # official mirror here so upgrading the app does not require changing the
    # launcher's trust settings. Explicit test/custom feeds remain isolated.
    result = [OFFICIAL_API, feed] if feed == OFFICIAL_FEED else [feed]
    for item in settings.get('feed_fallbacks', []):
        item = https_url(item)
        if item not in result:
            result.append(item)
    return result


def decode(raw, url):
    if len(raw) > MAX_MANIFEST:
        raise UpdateError('Release manifest exceeds 64 KiB')
    envelope = json.loads(raw)
    if not isinstance(envelope, dict):
        raise UpdateError('Release manifest must be a JSON object')
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname == 'api.github.com' and '/contents/' in parsed.path and 'payload' not in envelope:
        if (envelope.get('type') != 'file' or envelope.get('encoding') != 'base64'
                or type(envelope.get('size')) is not int or not 0 < envelope['size'] <= MAX_MANIFEST
                or not isinstance(envelope.get('content'), str)):
            raise UpdateError('GitHub update manifest wrapper is invalid')
        try:
            content = base64.b64decode(''.join(envelope['content'].split()), validate=True)
        except (ValueError, UnicodeError) as error:
            raise UpdateError('GitHub update manifest content is invalid') from error
        if len(content) != envelope['size'] or len(content) > MAX_MANIFEST:
            raise UpdateError('GitHub update manifest size is invalid')
        # The wrapper is only a transport. The caller always verifies the inner
        # Ed25519 envelope and all immutable package metadata before using it.
        envelope = json.loads(content)
    return envelope


def read_manifest(settings, *, opener, progress=lambda text: None):
    endpoints = sources(settings)
    errors = []
    for index, url in enumerate(endpoints):
        for attempt in range(2):
            progress('正在读取上位机更新信息（入口 ' + str(index+1) + '/' + str(len(endpoints)) + '）…')
            try:
                with opener(url) as response:
                    raw = response.read(MAX_MANIFEST+1)
                return decode(raw, url), url
            except READ_ERRORS as error:
                reason = getattr(error, 'reason', error)
                if isinstance(reason, ssl.SSLCertVerificationError):
                    raise
                errors.append({'url': url, 'attempt': attempt+1,
                    'error_type': type(error).__name__, 'error': str(error)[:1024]})
                # A missing file or API rate/permission error should try the
                # other HTTPS route immediately, without retrying this route.
                if isinstance(error, urllib.error.HTTPError) and error.code not in (429, 500, 502, 503, 504):
                    break
                if attempt == 0:
                    progress('读取更新信息中断，正在重新连接…')
                    time.sleep(1)
        if index+1 < len(endpoints):
            progress('当前更新入口不可用，正在切换备用入口…')
    raise ManifestUnavailable(errors)
