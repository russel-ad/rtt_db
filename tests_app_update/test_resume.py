import http.client
import io
import ssl
import unittest
from unittest.mock import patch

from app_update.core import UpdateError, archive_path, atomic_json, check_and_stage, digest
from app_update.download import DownloadInterrupted, download_package
from tests_app_update import test_update as fixtures


class Response(io.BytesIO):
    def __init__(self, data, *, start=0, total=None, status=200):
        super().__init__(data)
        self.status = status
        self.headers = {'Content-Length': str(len(data))}
        if status == 206:
            self.headers['Content-Range'] = f'bytes {start}-{start + len(data) - 1}/{total}'


class BrokenResponse(Response):
    def read(self, size=-1):
        if self.tell() >= 97:
            raise ssl.SSLEOFError('fixture TLS connection closed during the body')
        return super().read(min(size, 97 - self.tell()))


class ResumeTests(unittest.TestCase):
    setUp = fixtures.UpdateTests.setUp
    opener = fixtures.UpdateTests.opener

    def download(self, open_response, **kwargs):
        return download_package(archive_path(self.home, self.envelope['payload']),
            self.envelope['payload'], open_response=open_response, **kwargs)

    def partial(self, data):
        path = archive_path(self.home, self.envelope['payload']).with_suffix('.part')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_mid_body_tls_drop_resumes_at_exact_saved_offset_then_stages(self):
        data = self.package.read_bytes()
        offsets, phases = [], []
        def fetch(url, *, offset=0, **kwargs):
            if url.endswith('latest.json'):
                return self.opener(url)
            offsets.append(offset)
            if len(offsets) == 1:
                return BrokenResponse(data)
            return Response(data[offset:], start=offset, total=len(data), status=206)
        with patch('app_update.core.fetch', side_effect=fetch), patch('app_update.download.time.sleep'):
            payload = check_and_stage(self.home, self.settings, '2.10.0', on_transfer=phases.append)
        self.assertEqual(offsets, [0, 97])
        self.assertEqual(digest(archive_path(self.home, payload)), payload['sha256'])
        self.assertTrue((self.home/'pending.json').exists())
        self.assertFalse((self.home/'current.json').exists())
        self.assertIn('retrying', [item['phase'] for item in phases])
        self.assertEqual(phases[-1]['phase'], 'ready')

    def test_exhausted_retries_keep_partial_for_the_next_check(self):
        data = self.package.read_bytes()
        calls = []
        def interrupted(offset):
            calls.append(offset)
            if len(calls) == 1:
                return BrokenResponse(data)
            raise TimeoutError('fixture disconnected network')
        with patch('app_update.download.time.sleep'), self.assertRaises(DownloadInterrupted):
            self.download(interrupted)
        partial = archive_path(self.home, self.envelope['payload']).with_suffix('.part')
        self.assertEqual(calls, [0, 97, 97, 97, 97, 97])
        self.assertEqual(partial.read_bytes(), data[:97])
        self.assertFalse((self.home/'pending.json').exists())
        offsets = []
        def recovered(offset):
            offsets.append(offset)
            return Response(data[offset:], start=offset, total=len(data), status=206)
        self.download(recovered)
        self.assertEqual(offsets, [97])
        self.assertFalse(partial.exists())

    def test_range_ignored_by_server_restarts_instead_of_appending(self):
        data = self.package.read_bytes()
        self.partial(data[:97])
        offsets = []
        def full_response(offset):
            offsets.append(offset)
            return Response(data)
        self.download(full_response)
        self.assertEqual(offsets, [97])
        self.assertEqual(archive_path(self.home, self.envelope['payload']).read_bytes(), data)

    def test_wrong_range_position_or_total_is_rejected_before_append(self):
        data = self.package.read_bytes()
        for header in (f'bytes 0-{len(data)-98}/{len(data)}',
                       f'bytes 97-{len(data)-1}/{len(data)+1}', 'bytes */999', ''):
            with self.subTest(header=header):
                self.partial(data[:97])
                response = Response(data[97:], start=97, total=len(data), status=206)
                response.headers['Content-Range'] = header
                with self.assertRaises(UpdateError):
                    self.download(lambda offset: response)
                self.assertFalse(archive_path(self.home, self.envelope['payload']).exists())

    def test_corrupted_partial_is_discarded_after_final_hash_check(self):
        data = self.package.read_bytes()
        partial = self.partial(b'x' * 97)
        with self.assertRaises(UpdateError):
            self.download(lambda offset: Response(data[offset:], start=offset, total=len(data), status=206))
        self.assertFalse(partial.exists())
        self.assertFalse(archive_path(self.home, self.envelope['payload']).exists())

    def test_complete_valid_partial_needs_no_network(self):
        self.partial(self.package.read_bytes())
        self.download(lambda offset: self.fail('A complete verified partial must be reused'))
        self.assertEqual(digest(archive_path(self.home, self.envelope['payload'])), self.envelope['payload']['sha256'])

    def test_content_length_mismatch_does_not_stage_a_package(self):
        response = Response(self.package.read_bytes())
        response.headers['Content-Length'] = '10'
        with self.assertRaises(UpdateError):
            self.download(lambda offset: response)

    def test_certificate_error_during_resume_is_never_retried(self):
        partial = self.partial(self.package.read_bytes()[:97])
        offsets = []
        def denied(offset):
            offsets.append(offset)
            raise ssl.SSLCertVerificationError('fixture untrusted certificate')
        with patch('app_update.download.time.sleep') as delay, self.assertRaises(ssl.SSLCertVerificationError):
            self.download(denied)
        self.assertEqual(offsets, [97])
        delay.assert_not_called()
        self.assertEqual(partial.stat().st_size, 97)

    def test_incomplete_read_bytes_are_saved_before_reconnect(self):
        data = self.package.read_bytes()
        class Incomplete(Response):
            def read(self, size=-1):
                raise http.client.IncompleteRead(data[:97], len(data)-97)
        calls = []
        def open_response(offset):
            calls.append(offset)
            return Incomplete(data) if not offset else Response(data[offset:], start=offset, total=len(data), status=206)
        with patch('app_update.download.time.sleep'):
            self.download(open_response)
        self.assertEqual(calls, [0, 97])

    def test_tls_shutdown_after_all_signed_bytes_can_be_verified(self):
        data = self.package.read_bytes()
        class LateClose(Response):
            def read(self, size=-1):
                if self.tell() == len(data):
                    raise ssl.SSLEOFError('fixture missing TLS close_notify')
                return super().read(size)
        self.download(lambda offset: LateClose(data))
        self.assertEqual(digest(archive_path(self.home, self.envelope['payload'])), self.envelope['payload']['sha256'])

    def test_range_request_keeps_identity_encoding_and_https_validation(self):
        from app_update.core import fetch
        from types import SimpleNamespace
        from unittest.mock import Mock
        response = Response(b'part')
        response.geturl = lambda: 'https://example.org/asset'
        transport = SimpleNamespace(open=Mock(return_value=response))
        with patch('app_update.core.urllib.request.build_opener', return_value=transport):
            fetch('https://example.org/asset', offset=12345)
        request = transport.open.call_args.args[0]
        self.assertEqual(request.get_header('Range'), 'bytes=12345-')
        self.assertEqual(request.get_header('Accept-encoding'), 'identity')


if __name__ == '__main__':
    unittest.main()
