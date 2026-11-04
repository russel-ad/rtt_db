"""Verified update HTTPS, with native Windows chain building and public roots.

Use a local SSLContext, never a global ssl patch or a modified system store.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

import certifi
import truststore


def create_context():
    context = (truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT) if sys.platform == 'win32'
               else ssl.create_default_context())
    # Windows still validates its own chain first. These public roots support
    # machines whose system root update/cache is incomplete, in process memory.
    context.load_verify_locations(cafile=certifi.where())
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_alpn_protocols(['http/1.1'])
    return context


class VerifiedHTTPS(urllib.request.HTTPSHandler):
    def __init__(self):
        super().__init__(context=create_context())

    def https_open(self, request):
        try:
            return super().https_open(request)
        except urllib.error.URLError as error:
            # Redirect URLs may contain temporary signed queries. Keep only the
            # host that actually failed, never those query credentials.
            error.update_tls_host = urllib.parse.urlsplit(request.full_url).hostname
            raise


def certificate_error(error):
    reason = getattr(error, 'reason', error)
    return isinstance(reason, ssl.SSLCertVerificationError)


def diagnostics(error=None):
    bundle = Path(certifi.where())
    data = bundle.read_bytes() if bundle.is_file() else b''
    result = {'backend': 'Windows CryptoAPI' if sys.platform == 'win32' else 'OpenSSL',
        'truststore_version': truststore.__version__, 'certifi_version': certifi.__version__,
        'ca_bundle_present': bool(data), 'ca_bundle_sha256': hashlib.sha256(data).hexdigest(),
        'bundled_ca_count': data.count(b'-----BEGIN CERTIFICATE-----'),
        'certificate_verification_required': True, 'hostname_verification_required': True,
        'minimum_tls': 'TLSv1.2', 'system_certificate_store_modified': False,
        'ssl_cert_file_env_set': bool(os.environ.get('SSL_CERT_FILE')),
        'ssl_cert_dir_env_set': bool(os.environ.get('SSL_CERT_DIR'))}
    if error is not None:
        reason = getattr(error, 'reason', error)
        result.update(failed_host=getattr(error, 'update_tls_host', None),
            certificate_error=certificate_error(error),
            verify_code=getattr(reason, 'verify_code', None),
            verify_message=str(getattr(reason, 'verify_message', reason))[:1024])
    return result
