from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from app_update.core import UpdateError, fetch
from app_update.repair import check_https
from app_update.tls import create_context, diagnostics


class VerifiedTlsTests(unittest.TestCase):
    """Real handshakes with private test roots; never change an OS trust store."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='rtt-verified-tls-')
        cls.root = Path(cls.directory.name).resolve()
        cls.ca_key = ec.generate_private_key(ec.SECP256R1())
        cls.now = datetime.now(timezone.utc)
        cls.subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'RTT isolated TLS test root')])
        cls.ca = (x509.CertificateBuilder().subject_name(cls.subject).issuer_name(cls.subject)
            .public_key(cls.ca_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(cls.now-timedelta(days=5)).not_valid_after(cls.now+timedelta(days=5))
            .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
            .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
            .sign(cls.ca_key, hashes.SHA256()))
        cls.ca_pem = cls.ca.public_bytes(serialization.Encoding.PEM).decode('ascii')

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def certificate(self, hostname='localhost', *, expired=False):
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
        certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(self.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(self.now-timedelta(days=2))
            .not_valid_after(self.now+timedelta(days=-1 if expired else 1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(self.ca_key, hashes.SHA256()))
        prefix = str(certificate.serial_number)
        cert_file, key_file = self.root/(prefix+'.pem'), self.root/(prefix+'.key')
        cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_file, key_file)
        return context

    @contextmanager
    def server(self, context, *, redirect=None):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(5)
        port = listener.getsockname()[1]
        connections = []
        def serve():
            try:
                connection, _ = listener.accept()
                connections.append(True)
                with connection, context.wrap_socket(connection, server_side=True) as secure:
                    secure.settimeout(5)
                    if secure.recv(8192):
                        body = b'verified HTTPS fixture'
                        response = ('HTTP/1.1 302 Found\r\nLocation: '+redirect+'\r\nContent-Length: 0\r\n\r\n').encode() if redirect else (
                            b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(body)).encode()+b'\r\nConnection: close\r\n\r\n'+body)
                        secure.sendall(response)
            except (OSError, ssl.SSLError):
                pass  # A client rejecting the fixture certificate closes TLS.
        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        try:
            yield 'https://localhost:'+str(port)+'/test', connections
        finally:
            listener.close()
            worker.join(timeout=6)
            self.assertFalse(worker.is_alive(), 'The isolated TLS server must finish')

    def trusted_context(self):
        context = create_context()
        context.load_verify_locations(cadata=self.ca_pem)
        return context

    def test_context_keeps_verification_and_does_not_patch_global_ssl(self):
        original = ssl.SSLContext, ssl._create_default_https_context
        context = create_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(original, (ssl.SSLContext, ssl._create_default_https_context))
        info = diagnostics()
        self.assertGreater(info['bundled_ca_count'], 50)
        self.assertFalse(info['system_certificate_store_modified'])
        if sys.platform == 'win32':
            self.assertEqual(info['backend'], 'Windows CryptoAPI')

    def test_unknown_issuer_is_rejected_before_any_http_body_or_retry(self):
        with self.server(self.certificate()) as (url, connections), patch('urllib.request.getproxies', return_value={}), patch('app_update.core.time.sleep') as delay:
            with self.assertRaises(urllib.error.URLError) as raised:
                fetch(url, timeout=4)
            self.assertIsInstance(raised.exception.reason, ssl.SSLCertVerificationError)
            self.assertEqual(diagnostics(raised.exception)['failed_host'], 'localhost')
            self.assertEqual(len(connections), 1)
            delay.assert_not_called()

    def test_an_explicit_in_memory_ca_allows_a_real_verified_handshake(self):
        context = self.trusted_context()
        with self.server(self.certificate()) as (url, _), patch('urllib.request.getproxies', return_value={}), patch('app_update.tls.create_context', return_value=context):
            with fetch(url, timeout=4, attempts=1) as response:
                self.assertEqual(response.read(), b'verified HTTPS fixture')

    def test_wrong_hostname_is_rejected_even_with_a_trusted_issuer(self):
        context = self.trusted_context()
        with self.server(self.certificate('another.invalid')) as (url, _), patch('urllib.request.getproxies', return_value={}), patch('app_update.tls.create_context', return_value=context):
            with self.assertRaises(urllib.error.URLError) as raised:
                fetch(url, timeout=4, attempts=1)
            self.assertIsInstance(raised.exception.reason, ssl.SSLCertVerificationError)

    def test_expired_certificate_is_rejected_even_with_a_trusted_issuer(self):
        context = self.trusted_context()
        with self.server(self.certificate(expired=True)) as (url, _), patch('urllib.request.getproxies', return_value={}), patch('app_update.tls.create_context', return_value=context):
            with self.assertRaises(urllib.error.URLError) as raised:
                fetch(url, timeout=4, attempts=1)
            self.assertIsInstance(raised.exception.reason, ssl.SSLCertVerificationError)

    def test_redirect_tls_failure_records_the_actual_host_without_a_signed_query(self):
        context = self.trusted_context()
        with self.server(self.certificate()) as (target, _):
            target = target.replace('localhost', '127.0.0.1')+'?token=private-fixture-value'
            with self.server(self.certificate(), redirect=target) as (url, _), patch('urllib.request.getproxies', return_value={}), patch('app_update.tls.create_context', return_value=context):
                with self.assertRaises(urllib.error.URLError) as raised:
                    fetch(url, timeout=4, attempts=1)
                details = diagnostics(raised.exception)
                self.assertEqual(details['failed_host'], '127.0.0.1')
                self.assertNotIn('private-fixture-value', json.dumps(details))

    def test_verified_https_cannot_redirect_to_http(self):
        context = self.trusted_context()
        with self.server(self.certificate(), redirect='http://localhost/must-not-connect') as (url, _), patch('urllib.request.getproxies', return_value={}), patch('app_update.tls.create_context', return_value=context):
            with self.assertRaises(UpdateError):
                fetch(url, timeout=4, attempts=1)

    def test_certificate_probe_failure_has_actionable_details_and_does_not_stage(self):
        reason = ssl.SSLCertVerificationError('fixture missing issuer')
        reason.verify_code = 20
        error = urllib.error.URLError(reason)
        error.update_tls_host = 'api.github.com'
        with patch('app_update.core.fetch', side_effect=error):
            report = check_https()
        self.assertFalse(report['passed'])
        self.assertFalse(report['installed'])
        self.assertFalse(report['staged'])
        self.assertTrue(report['https']['certificate_error'])
        self.assertEqual(report['https']['failed_host'], 'api.github.com')
        self.assertEqual(report['https']['verify_code'], 20)


if __name__ == '__main__':
    unittest.main()
