"""Display-version compatibility with complete package and workflow checks.

Generated APP-only fixtures have real DAT/NR4 integrity fields. No external
firmware checkout, BLE radio, user data, or physical device is required.
"""
import asyncio
import binascii
import hashlib
import os
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from onmicro_dfu.controller import DfuController
from onmicro_dfu.package import DfuError, FirmwarePackage, HOST_LAYOUT
from onmicro_dfu.protocol import TransferOptions
from test_dfu import Receiver, image_bytes, make_zip


def host_fixture(*versions, counter=5, model=b'SC200-D'):
    raw = bytearray(1024)
    struct.pack_into('<II', raw, 0, 0x20010000, 0x409301)
    struct.pack_into('<4I', raw, 512, 0x4449344e, 0x75247524, 1, counter)
    identity = b'SC201:MODEL='+model+b'\0SC201:HW=DFT-SC20X-HUB\0'
    payload = identity+b'\0'.join(versions)+b'\0'
    raw[600:600+len(payload)] = payload
    return seal_host(bytes(raw), counter)


def seal_host(raw, counter=5):
    footer = bytearray(256)
    struct.pack_into('<10I', footer, 0, 0x4934524e, 1, 0x75247524, 0xa16627,
                     0x409000, len(raw), counter, 1, 0, binascii.crc32(raw))
    footer[40:72] = hashlib.sha256(raw).digest()
    struct.pack_into('<I', footer, 252, binascii.crc32(footer[:252]))
    image = raw+footer
    command = struct.pack('<IHHHHIII', 0x01bfdf55, 6, 0, 0, 1, 0, counter, len(image))
    return command, image


class PackageVersionTests(unittest.TestCase):
    def test_host_accepts_both_separators_without_changing_payload_or_counter(self):
        for version in (b'V001-010', b'V001_006', b'V002', b'V001-010_rc1'):
            with self.subTest(version=version):
                command, image = host_fixture(version)
                package = FirmwarePackage.host_image(command, image)
                self.assertEqual(package.version, version.decode('ascii'))
                self.assertEqual(package.image, image)
                self.assertEqual(package.command, command)
                self.assertEqual(package.release_counter, 5)
                self.assertEqual(package.sha256, hashlib.sha256(image).hexdigest())

    def test_duplicate_version_strings_still_identify_one_version(self):
        self.assertEqual(FirmwarePackage.host_image(*host_fixture(b'V001-010', b'V001-010')).version,
                         'V001-010')

    def test_host_refuses_multiple_distinct_versions_including_different_separators(self):
        for versions in ((b'V001-010', b'V001-011'), (b'V001-010', b'V001_010'),
                         (b'V001_006', b'V001_007')):
            with self.subTest(versions=versions), self.assertRaisesRegex(DfuError, '唯一的显示版本'):
                FirmwarePackage.host_image(*host_fixture(*versions))

    def test_missing_or_malformed_version_remains_an_error_for_host(self):
        for value in (b'', b'V00x-010', b'V001--010', b'V001-010!'):
            with self.subTest(value=value), self.assertRaisesRegex(DfuError, '唯一的显示版本'):
                FirmwarePackage.host_image(*host_fixture(value))

    def test_hyphen_version_does_not_bypass_integrity_identity_or_DAT_checks(self):
        command, image = host_fixture(b'V001-010')
        for offset in (0, 512, 650, len(image)-1):
            changed = bytearray(image)
            changed[offset] ^= 1
            with self.subTest(image_offset=offset), self.assertRaises(DfuError):
                FirmwarePackage.host_image(command, bytes(changed))
        for offset in (4, 6, 8, 10, 12, 16, 20):
            changed = bytearray(command)
            changed[offset] ^= 1
            with self.subTest(DAT_offset=offset), self.assertRaises(DfuError):
                FirmwarePackage.host_image(bytes(changed), image)
        with self.assertRaises(DfuError):
            FirmwarePackage.host_image(*host_fixture(b'V001-010', model=b'SC201-S'))
        raw = bytearray(image[:-256])
        struct.pack_into('<II', raw, 0, 0x20010000, 0x404301)
        with self.assertRaisesRegex(DfuError, '执行地址'):
            FirmwarePackage.host_image(*seal_host(bytes(raw)))

    def test_zip_loading_keeps_host_and_scale_profiles_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'renamed_without_version.zip'
            command, image = host_fixture(b'V001-010')
            make_zip(path, image=image, dat=command)
            self.assertEqual(FirmwarePackage.load(path, expanded=HOST_LAYOUT).version, 'V001-010')
            for profile in (False, True):
                with self.subTest(profile=profile), self.assertRaises(DfuError):
                    FirmwarePackage.load(path, expanded=profile)

    def test_scale_recognizes_hyphen_and_underscore_versions_from_the_image(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'no_version_in_filename.zip'
            for version in (b'V001-010', b'V001_006'):
                make_zip(path, image=image_bytes(version=version))
                for profile in (False, True):
                    with self.subTest(version=version, profile=profile):
                        self.assertEqual(FirmwarePackage.load(path, expanded=profile).version,
                                         version.decode('ascii'))

    def test_ambiguous_scale_version_is_not_silently_chosen(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'V001-010.zip'
            make_zip(path, image=image_bytes(version=b'V001-010\0V001_010'))
            self.assertIsNone(FirmwarePackage.load(path).version)


class VersionWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_hyphen_version_requires_exact_target_and_post_reboot_counter(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'host.zip'
            command, image = host_fixture(b'V001-010')
            make_zip(path, image=image, dat=command)

            class Connection(Receiver):
                def __init__(self):
                    super().__init__()
                    self.client = SimpleNamespace(is_connected=True)
                    self.address = 'AA:BB:CC:DD:EE:01'
                    self.address_type = 'public'
                    self.disconnected = asyncio.Event()

                async def information(self):
                    return dict(layout=HOST_LAYOUT, execution_address='0x00409000',
                                release_counter=4, software_version='V001_006')

                async def wait_for_reboot(self, timeout): pass
                async def __aexit__(self, *_): pass

            connection = Connection()
            controller = DfuController(lambda *_: None)
            controller.connection = connection
            with self.assertRaisesRegex(DfuError, '目标版本与固件包不一致'):
                await controller.upgrade(path, 'V001_010', TransferOptions(), expanded=HOST_LAYOUT)
            self.assertFalse(connection.commands)
            verified = AsyncMock(return_value='V001-010')
            with patch('onmicro_dfu.controller.verify_version', verified):
                result = await controller.upgrade(path, 'V001-010', TransferOptions(), expanded=HOST_LAYOUT)
            self.assertTrue(connection.committed)
            self.assertEqual(connection.flash, image)
            self.assertEqual(result['version'], 'V001-010')
            self.assertEqual(verified.await_args.kwargs, dict(expected_counter=5, expected_bytes=len(image)))


class VersionPanelTests(unittest.TestCase):
    def test_hyphen_host_package_loads_with_exact_target_and_stays_offline(self):
        from test_panel import APP, FakeWorker
        from ble_dfu_panel import BleDfuWidget
        from PyQt5 import sip
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'LOCALAPPDATA': folder}):
            path = Path(folder)/'host.zip'
            command, image = host_fixture(b'V001-010')
            make_zip(path, image=image, dat=command)
            panel = BleDfuWidget(worker_factory=FakeWorker)
            try:
                panel.layout_combo.setCurrentIndex(2)
                panel.load_package(path)
                self.assertIsNotNone(panel.package)
                self.assertEqual(panel.version_input.text(), 'V001-010')
                self.assertIn('校验通过', panel.package_info.text())
                self.assertIn('安装计数 5', panel.package_info.text())
                self.assertFalse(panel.start_btn.isEnabled())
                self.assertFalse(panel.worker.actions)
            finally:
                panel._finish_shutdown()
                sip.delete(panel)
                APP.processEvents()
