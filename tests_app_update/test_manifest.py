import base64
import io
import json
import ssl
import unittest
import urllib.error
from unittest.mock import patch

from app_update.core import UpdateError, archive_path, check_and_stage, digest, read_json
from app_update.manifest import (MAX_MANIFEST, OFFICIAL_API, OFFICIAL_FEED,
                                 ManifestUnavailable, read_manifest, sources)
from tests_app_update import test_update as fixtures


class ManifestTests(unittest.TestCase):
    setUp = fixtures.UpdateTests.setUp
    opener = fixtures.UpdateTests.opener

    def wrapper(self):
        raw = json.dumps(self.envelope).encode()
        encoded = base64.b64encode(raw).decode()
        return json.dumps({'type': 'file', 'encoding': 'base64', 'size': len(raw),
            'content': '\n'.join(encoded[i:i+60] for i in range(0, len(encoded), 60))}).encode()

    def test_api_route_stages_a_signed_update_when_release_and_raw_routes_are_unavailable(self):
        self.settings['feed_url'] = OFFICIAL_FEED
        calls = []
        def fetch(url, **kwargs):
            calls.append((url, kwargs))
            if url == OFFICIAL_API:
                return io.BytesIO(self.wrapper())
            self.assertNotEqual(url, OFFICIAL_FEED)
            self.assertNotIn('raw.githubusercontent.com', url)
            return io.BytesIO(self.package.read_bytes())
        with patch('app_update.core.fetch', side_effect=fetch):
            payload = check_and_stage(self.home, self.settings, '2.10.0')
        self.assertEqual(calls[0][0], OFFICIAL_API)
        self.assertEqual(calls[0][1], {'timeout': 15, 'attempts': 1})
        self.assertEqual(digest(archive_path(self.home, payload)), payload['sha256'])
        self.assertEqual(read_json(self.home/'manifest-source.json')['url'], OFFICIAL_API)

    def test_read_timeout_retries_the_response_body_and_then_succeeds(self):
        calls = []
        class TimeoutResponse(io.BytesIO):
            def read(self, size=-1):
                raise TimeoutError('The read operation timed out')
        def opener(url):
            calls.append(url)
            return TimeoutResponse() if len(calls) == 1 else self.opener(url)
        with patch('app_update.manifest.time.sleep'):
            envelope, url = read_manifest(self.settings, opener=opener)
        self.assertEqual(len(calls), 2)
        self.assertEqual(envelope, self.envelope)

    def test_api_rate_limit_falls_back_to_release_without_retrying_the_same_route(self):
        self.settings['feed_url'] = OFFICIAL_FEED
        calls = []
        def opener(url):
            calls.append(url)
            if url == OFFICIAL_API:
                raise urllib.error.HTTPError(url, 403, 'API limit fixture', None, None)
            return self.opener(url)
        with patch('app_update.manifest.time.sleep') as delay:
            envelope, source = read_manifest(self.settings, opener=opener)
        self.assertEqual(calls, [OFFICIAL_API, OFFICIAL_FEED])
        self.assertEqual(source, OFFICIAL_FEED)
        self.assertEqual(envelope, self.envelope)
        delay.assert_not_called()

    def test_all_failed_sources_are_bounded_and_report_the_actual_errors(self):
        self.settings['feed_url'] = OFFICIAL_FEED
        calls = []
        def opener(url):
            calls.append(url)
            raise TimeoutError('The read operation timed out')
        with patch('app_update.manifest.time.sleep'), self.assertRaises(ManifestUnavailable) as raised:
            read_manifest(self.settings, opener=opener)
        self.assertEqual(calls, [OFFICIAL_API, OFFICIAL_API, OFFICIAL_FEED, OFFICIAL_FEED])
        self.assertEqual(len(raised.exception.attempts), 4)
        self.assertTrue(all(item['error_type'] == 'TimeoutError' for item in raised.exception.attempts))
        self.assertFalse((self.home/'pending.json').exists())

    def test_api_wrapper_does_not_bypass_inner_signature_verification(self):
        self.settings['feed_url'] = OFFICIAL_FEED
        self.envelope['payload']['url'] = 'https://example.org/changed.zip'
        calls = []
        def fetch(url, **kwargs):
            calls.append(url)
            self.assertEqual(url, OFFICIAL_API)
            return io.BytesIO(self.wrapper())
        with patch('app_update.core.fetch', side_effect=fetch), self.assertRaises(UpdateError):
            check_and_stage(self.home, self.settings, '2.10.0')
        self.assertEqual(calls, [OFFICIAL_API])
        self.assertFalse((self.home/'pending.json').exists())

    def test_malformed_api_wrapper_size_and_encoding_are_rejected(self):
        from app_update.manifest import decode
        for change in ({'size': MAX_MANIFEST+1}, {'size': 1}, {'encoding': 'utf-8'}, {'content': '%%%invalid%%%'}):
            wrapper = json.loads(self.wrapper())
            wrapper.update(change)
            with self.subTest(change=change), self.assertRaises(UpdateError):
                decode(json.dumps(wrapper).encode(), OFFICIAL_API)

    def test_explicit_test_channel_does_not_use_the_stable_mirror(self):
        self.settings['feed_url'] = 'https://github.com/russel-ad/rtt_db/releases/download/v2.10.1-test/latest.json'
        self.assertEqual(sources(self.settings), [self.settings['feed_url']])

    def test_certificate_failure_does_not_retry_or_switch_sources(self):
        self.settings['feed_url'] = OFFICIAL_FEED
        calls = []
        def opener(url):
            calls.append(url)
            raise urllib.error.URLError(ssl.SSLCertVerificationError('fixture certificate'))
        with patch('app_update.manifest.time.sleep') as delay, self.assertRaises(urllib.error.URLError):
            read_manifest(self.settings, opener=opener)
        self.assertEqual(calls, [OFFICIAL_API])
        delay.assert_not_called()

    def test_manifest_reads_are_size_limited(self):
        with self.assertRaises(UpdateError):
            read_manifest(self.settings, opener=lambda url: io.BytesIO(b'x'*(MAX_MANIFEST+1)))


if __name__ == '__main__':
    unittest.main()
