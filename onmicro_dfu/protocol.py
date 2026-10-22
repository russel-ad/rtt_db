"""OnMicro object protocol, independent of Bleak and of the command line.

See projects/service/OTA/onmicro_dfu.c. An image is ONE data object;
Nordic-style intermediate Execute calls would activate an incomplete image.
"""

from __future__ import annotations

import asyncio
import binascii
from dataclasses import dataclass
import math
import struct
from typing import Callable, Protocol

from .package import DfuError, FirmwarePackage


CREATE, PRN, CHECKSUM, EXECUTE, SELECT = 1, 2, 3, 4, 6


class CommitUncertain(DfuError):
    """Final Execute may have activated the image. Never blindly retry it."""


class DfuRejected(DfuError):
    """The receiver explicitly returned a failure status."""


class Transport(Protocol):
    max_packet_size: int

    async def write_control(self, packet: bytes) -> None: ...
    async def write_data(self, packet: bytes) -> None: ...
    async def receive(self, timeout: float) -> bytes: ...


@dataclass(frozen=True)
class TransferOptions:
    chunk_size: int = 0
    prn: int = 8
    timeout: float = 15.0
    final_timeout: float = 60.0
    packet_delay: float = 0.0

    def __post_init__(self) -> None:
        if not 1 <= self.prn <= 255:
            raise DfuError("PRN must be 1..255 (firmware stores it as uint8_t)")
        if not 0 <= self.chunk_size <= 512:
            raise DfuError("Chunk size must be 0 (auto) or 1..512")
        if (not all(math.isfinite(n) for n in (self.timeout, self.final_timeout, self.packet_delay))
                or self.timeout <= 0 or self.final_timeout <= 0 or self.packet_delay < 0):
            raise DfuError("Timeouts must be positive; packet delay must be nonnegative")


class DfuSession:
    def __init__(self, transport: Transport, options: TransferOptions,
                 progress: Callable[[int, int], None] | None = None,
                 before_commit: Callable[[], None] | None = None):
        self.transport = transport
        self.options = options
        self.progress = progress or (lambda offset, size: None)
        self.before_commit = before_commit or (lambda: None)
        maximum = transport.max_packet_size
        requested = options.chunk_size or min(128, maximum)
        if maximum < 1 or requested > maximum:
            raise DfuError(f"Chunk size {requested} exceeds connection limit {maximum}")
        self.chunk_size = requested

    async def response(self, opcode: int, size: int = 3, *, final: bool = False) -> bytes:
        packet = await self.transport.receive(
            self.options.final_timeout if final else self.options.timeout)
        if len(packet) < 3 or packet[0] != 0x60 or packet[1] != opcode:
            raise DfuError(f"Unexpected DFU response for 0x{opcode:02X}: {packet.hex()}")
        if packet[2] != 1:
            raise DfuRejected(f"DFU opcode 0x{opcode:02X} rejected, status=0x{packet[2]:02X}")
        if len(packet) != size:
            raise DfuError(f"DFU response length {len(packet)} != {size}")
        return packet[3:]

    async def command(self, opcode: int, payload: bytes = b"", size: int = 3) -> bytes:
        await self.transport.write_control(bytes([opcode]) + payload)
        return await self.response(opcode, size)

    async def check(self, offset: int, crc: int, *, receipt: bool = False) -> None:
        if not receipt:
            await self.transport.write_control(bytes([CHECKSUM]))
        body = await self.response(CHECKSUM, 11)
        actual = struct.unpack("<II", body)
        if actual != (offset, crc):
            raise DfuError(f"DFU checksum mismatch: device offset={actual[0]} "
                           f"CRC32={actual[1]:08X}; PC offset={offset} CRC32={crc:08X}. "
                           "Stopped; no data or Execute is retried.")

    async def transfer(self, package: FirmwarePackage) -> None:
        # SELECT sets obj_actived; CREATE alone does not select an object.
        body = await self.command(SELECT, b"\x01", 15)
        if struct.unpack_from("<I", body)[0] < len(package.command):
            raise DfuError("Device command object limit is smaller than DAT")
        await self.command(CREATE, b"\x01" + struct.pack("<I", len(package.command)))
        await self.command(PRN, b"\x00\x00")
        for start in range(0, len(package.command), self.chunk_size):
            await self.transport.write_data(package.command[start:start + self.chunk_size])
        await self.check(len(package.command), binascii.crc32(package.command))
        await self.command(EXECUTE)

        # Reset m_data_offset BEFORE SELECT DATA. Following an earlier interrupted
        # connection, SELECT-first can enter update_env_img_info_by_cache() with
        # stale offsets and a newly uploaded DAT. We deliberately do not resume.
        await self.command(CREATE, b"\x02" + struct.pack("<I", len(package.image)))
        body = await self.command(SELECT, b"\x02", 15)
        maximum, offset, crc = struct.unpack("<III", body)
        if maximum < len(package.image) or offset or crc:
            raise DfuError("Device did not initialize an empty data object")
        await self.command(PRN, struct.pack("<H", self.options.prn))

        crc = 0
        count = 0
        self.progress(0, len(package.image))
        for start in range(0, len(package.image), self.chunk_size):
            packet = package.image[start:start + self.chunk_size]
            await self.transport.write_data(packet)
            crc = binascii.crc32(packet, crc)
            offset = start + len(packet)
            count += 1
            if self.options.packet_delay:
                await asyncio.sleep(self.options.packet_delay)
            if count == self.options.prn:
                await self.check(offset, crc, receipt=True)
                count = 0
                self.progress(offset, len(package.image))
        if count:
            await self.check(len(package.image), crc)
            self.progress(len(package.image), len(package.image))

        # Only this Execute commits the data image and updates MBR. A disconnect
        # or missing ACK here is ambiguous, never a reason to repeat Execute.
        self.before_commit()
        try:
            await self.transport.write_control(bytes([EXECUTE]))
            await self.response(EXECUTE, final=True)
        except DfuRejected:
            raise
        except (DfuError, TimeoutError, OSError) as exc:
            raise CommitUncertain("Final Execute was sent but success was not confirmed. "
                                  "The device may already have switched firmware. "
                                  "Use probe to check its version before another upgrade.") from exc
