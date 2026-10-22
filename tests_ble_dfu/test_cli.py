"""End-to-end CLI orchestration with a receiver model and fake reconnects."""

import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from onmicro_dfu.ble import BleTransport, verify_version
from onmicro_dfu.cli import main, parser, run
from onmicro_dfu.package import DfuError
from test_dfu import Receiver, make_zip


class Connection(Receiver):
    def __init__(self, *, software="V001_003", rom="0x6626A100", reboot=True):
        super().__init__()
        self.software = software
        self.rom = rom
        self.reboot = reboot
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def information(self):
        return {"software_version": self.software, "rom_id": self.rom,
                "execution_address": "0x00404000", "boot_marker": "0x46533252"}

    async def wait_for_reboot(self, timeout):
        if not self.reboot:
            raise DfuError("No automatic reboot observed")


class OfflineEntryTests(unittest.TestCase):
    def test_actual_cli_check_main_and_help(self):
        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder) / "image.zip"
            make_zip(package)
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--log", str(Path(folder) / "check.log"),
                                       "check", str(package)]), 0)
            self.assertEqual(json.loads(output.getvalue())["version"], "V001_004")
            self.assertIn("upgrade", parser().format_help())


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.package = Path(folder.name) / "image.zip"
        make_zip(self.package)
        self.args = parser().parse_args([
            "upgrade", "--address", "AA:BB:CC:DD:EE:FF",
            "--package", str(self.package), "--expect-version", "V001_004"])

    async def test_upgrade_verifies_version_after_commit_and_reboot(self):
        connection = Connection()
        verify = AsyncMock(return_value="V001_004")
        with patch("onmicro_dfu.ble.BleTransport", return_value=connection), \
                patch("onmicro_dfu.ble.verify_version", verify):
            self.assertEqual(await run(self.args), 0)
        self.assertTrue(connection.committed)
        self.assertTrue(connection.closed)
        verify.assert_awaited_once_with("AA:BB:CC:DD:EE:FF", "V001_004", 90.0, "public")

    async def test_rom_mismatch_and_same_version_do_not_write(self):
        for connection in (Connection(rom="0x6621E100"), Connection(software="V001_004")):
            with self.subTest(rom=connection.rom, version=connection.software):
                with patch("onmicro_dfu.ble.BleTransport", return_value=connection):
                    with self.assertRaises(DfuError):
                        await run(self.args)
                self.assertFalse(connection.commands)
                self.assertTrue(connection.closed)

    async def test_missing_reboot_is_not_success_or_own_disconnect(self):
        connection = Connection(reboot=False)
        verify = AsyncMock()
        with patch("onmicro_dfu.ble.BleTransport", return_value=connection), \
                patch("onmicro_dfu.ble.verify_version", verify):
            with self.assertRaisesRegex(DfuError, "reboot"):
                await run(self.args)
        self.assertTrue(connection.committed)
        verify.assert_not_awaited()

    async def test_wrong_post_reboot_version_is_not_success(self):
        connection = Connection()
        with patch("onmicro_dfu.ble.BleTransport", return_value=connection), \
                patch("onmicro_dfu.ble.verify_version", AsyncMock(side_effect=DfuError("wrong version"))):
            with self.assertRaisesRegex(DfuError, "wrong version"):
                await run(self.args)
        self.assertTrue(connection.committed)

    async def test_probe_creates_no_dfu_objects(self):
        connection = Connection()
        args = parser().parse_args(["probe", "--address", "AA:BB:CC:DD:EE:FF"])
        with patch("onmicro_dfu.ble.BleTransport", return_value=connection):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(await run(args), 0)
        self.assertFalse(connection.commands)
        self.assertEqual(json.loads(output.getvalue())["software_version"], "V001_003")

    async def test_version_reconnect_retries_and_requires_exact_match(self):
        versions = [b"V001_003", b"V001_004\0"]

        class Client:
            def __init__(self, *_, **__): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def read_gatt_char(self, *_, **__): return versions.pop(0)

        original_sleep = asyncio.sleep
        async def immediate(_):
            await original_sleep(0)

        with patch("onmicro_dfu.ble.bleak_classes", return_value=(Client, None)), \
                patch("onmicro_dfu.ble.asyncio.sleep", immediate):
            self.assertEqual(await verify_version("test", "V001_004", 1), "V001_004")
        self.assertEqual(versions, [])

    async def test_version_confirmation_timeout_never_accepts_old_version(self):
        class Client:
            def __init__(self, *_, **__): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def read_gatt_char(self, *_, **__): return b"V001_003"

        with patch("onmicro_dfu.ble.bleak_classes", return_value=(Client, None)):
            with self.assertRaisesRegex(DfuError, "not confirmed"):
                await verify_version("test", "V001_004", 0.01)

    async def test_ble_connection_failure_disconnects_cleanly(self):
        class Client:
            closed = False
            def __init__(self, *_, **__): pass
            async def connect(self): raise DfuError("connect failed")
            async def disconnect(self): Client.closed = True

        with patch("onmicro_dfu.ble.bleak_classes", return_value=(Client, None)):
            with self.assertRaisesRegex(DfuError, "connect failed"):
                async with BleTransport("test"):
                    self.fail("connection should have failed")
        self.assertTrue(Client.closed)

    async def test_notification_overflow_and_disconnect_wake_waiters(self):
        connection = BleTransport("test")
        for _ in range(65):
            connection.enqueue(connection.queue, b"\x60\x01\x01")
        with self.assertRaisesRegex(DfuError, "overflow"):
            await connection.receive(0.1)
        connection = BleTransport("test")
        connection.on_disconnected(None)
        with self.assertRaisesRegex(DfuError, "disconnected"):
            await connection.receive(0.1)


if __name__ == "__main__":
    unittest.main()
