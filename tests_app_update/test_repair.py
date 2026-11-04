import unittest
from unittest.mock import patch

from app_update import APP_ENTRY, APP_ID
from app_update.core import UpdateError, atomic_json, file_lock, read_json
from app_update.repair import base_version, prepare
from tests_app_update import test_update as fixtures


class RepairTests(unittest.TestCase):
    setUp = fixtures.UpdateTests.setUp
    opener = fixtures.UpdateTests.opener

    def original(self):
        root = self.root/'original portable'
        folder = root/'app/2.10.0'
        folder.mkdir(parents=True)
        (root/'RTTTool.exe').write_bytes(b'old launcher fixture')
        (folder/APP_ENTRY).write_bytes(b'old application fixture')
        atomic_json(folder/'app-release.json', {'app_id': APP_ID, 'version': '2.10.0', 'entry': APP_ENTRY})
        return root

    def test_repair_prepares_signed_update_without_installing_or_touching_configuration(self):
        root = self.original()
        data = self.home.parent/'data/config.json'
        atomic_json(data, {'marker': 'existing account and settings'})
        def fetch(url, **kwargs):
            return self.opener(url)
        with patch('app_update.core.fetch', side_effect=fetch):
            result = prepare(root, self.home, settings=self.settings)
        self.assertTrue(result['staged'])
        self.assertEqual(result['source_version'], '2.10.0')
        self.assertEqual(result['target_version'], '2.10.1')
        self.assertFalse(result['installed_by_repair'])
        self.assertFalse(result['hardware_access'])
        self.assertEqual(read_json(data)['marker'], 'existing account and settings')
        self.assertTrue((self.home/'pending.json').exists())
        self.assertFalse((self.home/'current.json').exists())
        self.assertEqual((root/'RTTTool.exe').read_bytes(), b'old launcher fixture')

    def test_running_application_blocks_repair_before_any_network_request(self):
        root = self.original()
        with file_lock(self.home/'session.lock'), patch('app_update.repair.check_and_stage') as download:
            with self.assertRaisesRegex(UpdateError, '请先退出上位机'):
                prepare(root, self.home, settings=self.settings)
            download.assert_not_called()

    def test_wrong_directory_is_rejected_before_any_update_state_change(self):
        root = self.root/'not a portable application'
        root.mkdir()
        with self.assertRaises(UpdateError):
            base_version(root)
        self.assertFalse(self.home.exists())

    def test_original_installed_version_is_used_instead_of_the_helper_version(self):
        root = self.original()
        atomic_json(self.home/'current.json', {'current': '2.10.2', 'trial': False})
        with patch('app_update.repair.check_and_stage', return_value=None) as download:
            result = prepare(root, self.home, settings=self.settings)
        self.assertEqual(download.call_args.args[2], '2.10.2')
        self.assertFalse(result['staged'])
        self.assertEqual(result['target_version'], '2.10.2')


if __name__ == '__main__':
    unittest.main()
