"""Resume signed packages after interrupted HTTPS reads; never installs them."""
from __future__ import annotations

import http.client
import os
import re
import ssl
import time
import urllib.error

from .core import UpdateError, digest

NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError,
                  ssl.SSLError, http.client.IncompleteRead)


class DownloadInterrupted(UpdateError):
    """The partial download is retained for a later check."""


def retryable(error):
    reason = getattr(error, 'reason', error)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return False
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (429, 500, 502, 503, 504)
    return isinstance(error, NETWORK_ERRORS)


def response_span(response, offset, total):
    """Validate response positioning before any bytes are appended."""
    status = getattr(response, 'status', 200)
    headers = getattr(response, 'headers', {})
    if headers.get('Content-Encoding', 'identity').lower() != 'identity':
        raise UpdateError('Update response has an unsupported content encoding')
    if status == 206:
        match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', headers.get('Content-Range', ''))
        if not match:
            raise UpdateError('Update download has an invalid Content-Range')
        start, end, length = map(int, match.groups())
        if start != offset or length != total or not start <= end < total:
            raise UpdateError('Update download Content-Range does not match the signed package')
        count = end - start + 1
    elif status == 200:
        # A server/proxy may ignore Range. Replace the prefix instead of
        # appending a full response to it.
        start, count = 0, total
    else:
        raise UpdateError('Unexpected update response status: ' + str(status))
    length = headers.get('Content-Length')
    if length is not None and (not length.isdecimal() or int(length) != count):
        raise UpdateError('Update package hash/length does not match')
    return start, count


def download_package(destination, payload, *, open_response, progress=lambda text: None,
                     on_progress=lambda phase, received: None, attempts=6):
    temporary = destination.with_suffix('.part')
    total = payload['size']
    destination.parent.mkdir(parents=True, exist_ok=True)
    if temporary.is_symlink():
        raise UpdateError('Refusing a linked partial update file')
    if temporary.exists() and (temporary.stat().st_size > total or
            temporary.stat().st_size == total and digest(temporary) != payload['sha256']):
        temporary.unlink()
    started = time.monotonic()
    try:
        for attempt in range(attempts):
            offset = temporary.stat().st_size if temporary.exists() else 0
            if offset == total:
                break
            on_progress('resuming' if offset else 'connecting', offset)
            progress(('正在续传上位机 ' if offset else '正在后台下载上位机 ') + payload['version'] + '…')
            try:
                with open_response(offset) as response:
                    start, remaining = response_span(response, offset, total)
                    with temporary.open('ab' if start else 'wb') as stream:
                        if start != offset:
                            on_progress('resuming', start)
                        received = start
                        def save(data):
                            nonlocal remaining, received
                            if len(data) > remaining:
                                raise UpdateError('Update package hash/length does not match')
                            stream.write(data)
                            remaining -= len(data)
                            received += len(data)
                            on_progress('download', received)
                        try:
                            while remaining:
                                if time.monotonic() - started > 900:
                                    raise DownloadInterrupted('Update download interrupted; partial progress saved after time limit')
                                try:
                                    data = response.read(min(64 * 1024, remaining))
                                except http.client.IncompleteRead as error:
                                    save(error.partial)
                                    raise
                                if not data:
                                    raise ConnectionError('Update response ended before all bytes were received')
                                save(data)
                            if response.read(1):
                                raise UpdateError('Update package hash/length does not match')
                        finally:
                            stream.flush()
                            os.fsync(stream.fileno())
            except NETWORK_ERRORS as error:
                if not retryable(error):
                    raise
                offset = temporary.stat().st_size if temporary.exists() else 0
                if offset == total:
                    # A TLS shutdown error after all signed bytes arrived
                    # cannot invalidate a matching cryptographic digest.
                    break
                on_progress('retrying', offset)
                if attempt + 1 == attempts:
                    raise DownloadInterrupted('Update download interrupted; partial progress saved: '
                        + str(offset) + '/' + str(total) + ' bytes; ' + str(error)) from error
                progress('下载连接中断，已保留进度，正在续传（' + str(attempt + 2) + '/' + str(attempts) + '）…')
                time.sleep(min(attempt + 1, 4))
        if not temporary.exists() or temporary.stat().st_size != total:
            raise DownloadInterrupted('Update download interrupted; partial progress saved')
        on_progress('verify', total)
        if digest(temporary) != payload['sha256']:
            raise UpdateError('Update package hash/length does not match')
        os.replace(temporary, destination)
    except DownloadInterrupted:
        on_progress('paused', temporary.stat().st_size if temporary.exists() else 0)
        raise
    except UpdateError:
        temporary.unlink(missing_ok=True)
        raise
