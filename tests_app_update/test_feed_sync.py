import base64
from pathlib import Path
import unittest

from app_update.core import UpdateError, atomic_json, canonical, read_json
from tests_app_update import test_update as fixtures
from tools.sync_update_feed import synchronize


class FeedSyncTests(unittest.TestCase):
    setUp = fixtures.UpdateTests.setUp

    def test_valid_signed_manifest_is_copied_without_touching_its_payload(self):
        output = self.root/'mirror/stable.json'
        synchronize(self.root/'release/latest.json', output, self.settings)
        self.assertEqual(read_json(output), self.envelope)

    def test_tampered_manifest_never_overwrites_the_mirror(self):
        output = self.root/'stable.json'
        atomic_json(output, self.envelope)
        before = output.read_bytes()
        changed = self.root/'changed.json'
        self.envelope['payload']['size'] += 1
        atomic_json(changed, self.envelope)
        with self.assertRaises(UpdateError):
            synchronize(changed, output, self.settings)
        self.assertEqual(output.read_bytes(), before)

    def test_an_older_but_valid_signature_cannot_downgrade_the_mirror(self):
        output = self.root/'stable.json'
        self.envelope['payload']['version'] = '2.10.2'
        self.envelope['signature'] = base64.b64encode(self.key.sign(canonical(self.envelope['payload']))).decode()
        atomic_json(output, self.envelope)
        before = output.read_bytes()
        with self.assertRaises(UpdateError):
            synchronize(self.root/'release/latest.json', output, self.settings)
        self.assertEqual(output.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
