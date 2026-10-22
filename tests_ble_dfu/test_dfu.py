"""Protocol/failure tests run without a radio or firmware project.

The receiver model enforces the OnMicro state transitions and full-image
commit, including stale Flash offsets after disconnect. It is not a substitute
for testing the physical controller, firmware, Flash, and reboot sequence.
"""

import asyncio
import binascii
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from onmicro_dfu.package import DfuError, FirmwarePackage, LEGACY_LIMIT, ROM_ID
from onmicro_dfu.protocol import (CHECKSUM, CREATE, EXECUTE, PRN, SELECT,
                                  CommitUncertain, DfuRejected, DfuSession, TransferOptions)
from onmicro_dfu.cli import parser, run


def image_bytes(size=4117, version=b"V001_004"):
    image = bytearray((n * 17 + 3) % 256 for n in range(size))
    struct.pack_into("<II", image, 36, 0x404000, 0x46533252)
    struct.pack_into("<I", image, 52, ROM_ID)
    image[128:128 + len(version) + 1] = version + b"\0"
    return bytes(image)


def dat_bytes(image, protocol=6):
    return struct.pack("<IHHHHBBHII", 0x01BFDF55, protocol, 0, 0, 1, 0, 0,
                       binascii.crc_hqx(image, 0), ROM_ID, len(image))


def make_zip(path, *, image=None, dat=None, manifest=None):
    image = image if image is not None else image_bytes()
    dat = dat if dat is not None else dat_bytes(image)
    manifest = manifest if manifest is not None else {
        "manifest": {"application": {"bin_file": "firmware.bin", "dat_file": "firmware.dat"}}}
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("firmware.bin", image)
        archive.writestr("firmware.dat", dat)


class Receiver:
    """Independent in-memory peer, with deterministic failure injection."""
    max_packet_size = 128

    def __init__(self, *, stale_offset=0, drop_packet=None, bad_crc=False,
                 final="ok", malformed=None, data_write_failure=False):
        self.queue = asyncio.Queue()
        self.active = None
        self.obj_size = 0
        self.obj = bytearray()
        self.dat = None
        self.prn = 0
        self.prn_count = 0
        self.flash_offset = stale_offset
        self.created_data = False
        self.data_packets = 0
        self.drop_packet = drop_packet
        self.bad_crc = bad_crc
        self.final = final
        self.malformed = malformed
        self.data_write_failure = data_write_failure
        self.commands = []
        self.data_executes = 0
        self.committed = False
        self.flash = None

    def reply(self, opcode, payload=b"", status=1):
        packet = bytes([0x60, opcode, status]) + payload
        if self.malformed == opcode:
            packet = b"\x60\x7f\x01"
        self.queue.put_nowait(packet)

    def checksum(self):
        crc = binascii.crc32(self.obj)
        if self.bad_crc and self.active == 2:
            crc ^= 1
        self.reply(CHECKSUM, struct.pack("<II", len(self.obj), crc))

    async def write_control(self, packet):
        self.commands.append(packet)
        opcode = packet[0]
        if opcode == SELECT:
            kind = packet[1]
            if kind == 2 and self.flash_offset and not self.created_data:
                raise AssertionError("SELECT DATA accessed stale image cache before reset")
            self.active = kind
            self.prn_count = 0
            offset = 0 if kind == 1 else self.flash_offset
            self.reply(opcode, struct.pack("<III", 0x10000000, offset, 0))
        elif opcode == CREATE:
            self.obj_size = struct.unpack_from("<I", packet, 2)[0]
            self.obj = bytearray()
            if packet[1] == 2:
                if self.dat is None or self.obj_size != struct.unpack_from("<I", self.dat, 20)[0]:
                    self.reply(opcode, status=5)
                    return
                self.flash_offset = 0
                self.created_data = True
            self.reply(opcode)
        elif opcode == PRN:
            self.prn = struct.unpack_from("<H", packet, 1)[0]
            self.reply(opcode)
        elif opcode == CHECKSUM:
            self.checksum()
        elif opcode == EXECUTE:
            if len(self.obj) != self.obj_size:
                self.reply(opcode, status=5)
                return
            if self.active == 1:
                self.dat = bytes(self.obj)
                self.reply(opcode)
            else:
                self.data_executes += 1
                self.flash = bytes(self.obj)
                if self.final == "reject":
                    self.reply(opcode, status=4)
                    return
                self.committed = True
                if self.final == "disconnect":
                    self.queue.put_nowait(DfuError("BLE disconnected"))
                elif self.final != "lost_ack":
                    self.reply(opcode)
        else:
            raise AssertionError("Unknown control opcode")

    async def write_data(self, packet):
        if self.active not in (1, 2):
            raise AssertionError("Data written before SELECT")
        if len(packet) > self.max_packet_size:
            raise AssertionError("Packet exceeds ATT capability")
        if self.active == 2:
            self.data_packets += 1
            if self.data_write_failure:
                raise DfuError("BLE write failed")
            if self.data_packets == self.drop_packet:
                return
        self.obj.extend(packet)
        if len(self.obj) > self.obj_size:
            raise AssertionError("Object overrun")
        if self.active == 2:
            self.flash_offset = len(self.obj) // 128 * 128
        self.prn_count += 1
        if self.prn and self.prn_count >= self.prn:
            self.prn_count = 0
            self.checksum()

    async def receive(self, timeout):
        try:
            packet = await asyncio.wait_for(self.queue.get(), min(timeout, 0.05))
        except TimeoutError as exc:
            raise DfuError("No DFU response") from exc
        if isinstance(packet, Exception):
            raise packet
        return packet


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "firmware.zip"

    def test_valid_package_and_version(self):
        make_zip(self.path)
        p = FirmwarePackage.load(self.path)
        self.assertEqual(p.version, "V001_004")
        self.assertEqual(p.image, image_bytes())

    def test_legacy_protocol_three(self):
        image = image_bytes()
        make_zip(self.path, dat=dat_bytes(image, 3))
        self.assertEqual(FirmwarePackage.load(self.path).rom_id, ROM_ID)

    def test_bad_crc(self):
        make_zip(self.path, dat=dat_bytes(image_bytes()[:-1] + b"\x00"))
        with self.assertRaisesRegex(DfuError, "CRC16"):
            FirmwarePackage.load(self.path)

    def test_bad_length(self):
        dat = bytearray(dat_bytes(image_bytes()))
        struct.pack_into("<I", dat, 20, 10)
        make_zip(self.path, dat=dat)
        with self.assertRaises(DfuError):
            FirmwarePackage.load(self.path)

    def test_wrong_rom_and_link_address(self):
        for offset, value in [(52, 0x6621E100), (36, 0x40E000), (40, 0)]:
            with self.subTest(offset=offset):
                image = bytearray(image_bytes())
                struct.pack_into("<I", image, offset, value)
                make_zip(self.path, image=image)
                with self.assertRaises(DfuError):
                    FirmwarePackage.load(self.path)

    def test_refuse_signed_multi_image_or_raw(self):
        for offset, value in [(0, 0), (4, 99), (8, 1), (10, 2), (12, 0x5F)]:
            with self.subTest(offset=offset):
                dat = bytearray(dat_bytes(image_bytes()))
                dat[offset] = value
                make_zip(self.path, dat=dat)
                with self.assertRaises(DfuError):
                    FirmwarePackage.load(self.path)

    def test_legacy_size_boundary_and_explicit_expanded_layout(self):
        make_zip(self.path, image=image_bytes(LEGACY_LIMIT))
        self.assertEqual(len(FirmwarePackage.load(self.path).image), LEGACY_LIMIT)
        make_zip(self.path, image=image_bytes(LEGACY_LIMIT + 1))
        with self.assertRaises(DfuError):
            FirmwarePackage.load(self.path)
        self.assertEqual(len(FirmwarePackage.load(self.path, expanded=True).image), LEGACY_LIMIT + 1)
        make_zip(self.path, image=image_bytes(400 * 1024 + 1))
        with self.assertRaises(DfuError):
            FirmwarePackage.load(self.path, expanded=True)

    def test_manifest_refuses_other_images_and_bad_paths(self):
        for manifest in [[], {"manifest": []}, {"manifest": {"softdevice": {}}},
                         {"manifest": {"application": {"bin_file": "../bad", "dat_file": "firmware.dat"}}}]:
            with self.subTest(manifest=manifest):
                make_zip(self.path, manifest=manifest)
                with self.assertRaises(DfuError):
                    FirmwarePackage.load(self.path)

    def test_malformed_zip_and_dat(self):
        self.path.write_bytes(b"not a zip")
        with self.assertRaises(DfuError):
            FirmwarePackage.load(self.path)
        make_zip(self.path, dat=b"short")
        with self.assertRaises(DfuError):
            FirmwarePackage.load(self.path)


class TransferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "test.zip"
        make_zip(self.path)
        self.package = FirmwarePackage.load(self.path)

    async def test_full_transfer_fragments_prn_and_only_one_data_commit(self):
        for size, prn in [(20, 1), (20, 8), (128, 8), (1, 255), (0, 8)]:
            with self.subTest(size=size, prn=prn):
                receiver = Receiver()
                progress = []
                session = DfuSession(receiver, TransferOptions(size, prn),
                                     lambda got, total: progress.append((got, total)))
                await session.transfer(self.package)
                self.assertEqual(receiver.flash, self.package.image)
                self.assertEqual(receiver.data_executes, 1)
                self.assertTrue(receiver.committed)
                self.assertEqual(progress[-1], (len(self.package.image), len(self.package.image)))
                self.assertTrue(receiver.queue.empty())

    async def test_new_transfer_resets_stale_flash_state_before_select(self):
        receiver = Receiver(stale_offset=128 * 300)
        await DfuSession(receiver, TransferOptions()).transfer(self.package)
        self.assertEqual(receiver.flash, self.package.image)

    async def test_exact_prn_window_has_no_stale_checksum_reply(self):
        make_zip(self.path, image=image_bytes(4096))
        p = FirmwarePackage.load(self.path)
        receiver = Receiver()
        await DfuSession(receiver, TransferOptions(128, 8)).transfer(p)
        self.assertTrue(receiver.queue.empty())

    async def test_crc_failure_does_not_commit_or_retransmit(self):
        receiver = Receiver(bad_crc=True)
        with self.assertRaisesRegex(DfuError, "checksum mismatch"):
            await DfuSession(receiver, TransferOptions(20, 8)).transfer(self.package)
        self.assertFalse(receiver.committed)
        self.assertEqual(receiver.data_executes, 0)
        self.assertEqual(receiver.data_packets, 8)

    async def test_dropped_write_times_out_without_execute(self):
        receiver = Receiver(drop_packet=3)
        with self.assertRaises(DfuError):
            await DfuSession(receiver, TransferOptions(20, 8)).transfer(self.package)
        self.assertEqual(receiver.data_packets, 8)
        self.assertEqual(receiver.data_executes, 0)

    async def test_data_write_failure_never_commits(self):
        receiver = Receiver(data_write_failure=True)
        with self.assertRaises(DfuError):
            await DfuSession(receiver, TransferOptions()).transfer(self.package)
        self.assertFalse(receiver.committed)

    async def test_final_disconnect_or_lost_ack_is_uncertain_not_retried(self):
        for failure in ("disconnect", "lost_ack"):
            with self.subTest(failure=failure):
                receiver = Receiver(final=failure)
                with self.assertRaises(CommitUncertain):
                    await DfuSession(receiver, TransferOptions()).transfer(self.package)
                self.assertTrue(receiver.committed)
                self.assertEqual(receiver.data_executes, 1)

    async def test_explicit_final_rejection_is_not_success_or_ambiguous(self):
        receiver = Receiver(final="reject")
        with self.assertRaises(DfuRejected):
            await DfuSession(receiver, TransferOptions()).transfer(self.package)
        self.assertFalse(receiver.committed)

    async def test_malformed_select_stops_before_data(self):
        receiver = Receiver(malformed=SELECT)
        with self.assertRaisesRegex(DfuError, "Unexpected"):
            await DfuSession(receiver, TransferOptions()).transfer(self.package)
        self.assertEqual(receiver.data_packets, 0)

    async def test_explicit_chunk_exceeding_att_limit_is_refused(self):
        receiver = Receiver()
        receiver.max_packet_size = 20
        with self.assertRaises(DfuError):
            DfuSession(receiver, TransferOptions(128))

    async def test_offline_check_never_imports_or_uses_bleak(self):
        args = parser().parse_args(["check", str(self.path)])
        with patch("onmicro_dfu.ble.bleak_classes", side_effect=AssertionError("radio accessed")):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(await run(args), 0)
        self.assertEqual(json.loads(output.getvalue())["version"], "V001_004")

    async def test_version_mismatch_stops_before_connection(self):
        args = parser().parse_args(["upgrade", "--address", "AA:BB:CC:DD:EE:FF",
                                   "--package", str(self.path), "--expect-version", "V001_005"])
        with patch("onmicro_dfu.ble.bleak_classes", side_effect=AssertionError("radio accessed")):
            with self.assertRaisesRegex(DfuError, "not requested"):
                await run(args)


class OptionTests(unittest.TestCase):
    def test_invalid_flow_control_values(self):
        for kwargs in [{"prn": 0}, {"prn": 256}, {"chunk_size": -1},
                       {"timeout": 0}, {"packet_delay": -1}, {"timeout": float("nan")}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(DfuError):
                TransferOptions(**kwargs)


if __name__ == "__main__":
    unittest.main()
