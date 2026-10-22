"""Validate an OnMicro ZIP without extracting or connecting to a device."""

from __future__ import annotations

import binascii
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import struct
import zipfile


LEGACY_LIMIT = 280 * 1024
EXPANDED_LIMIT = 400 * 1024
ROM_ID = 0x6626A100


class DfuError(Exception):
    """An actionable package, transport, or protocol failure."""


@dataclass(frozen=True)
class FirmwarePackage:
    command: bytes
    image: bytes
    rom_id: int
    crc16: int
    version: str | None
    sha256: str

    @classmethod
    def load(cls, path: Path, *, expanded: bool = False) -> FirmwarePackage:
        limit = EXPANDED_LIMIT if expanded else LEGACY_LIMIT
        try:
            with zipfile.ZipFile(path) as archive:
                names = [entry.filename for entry in archive.infolist()]
                if len(names) != len(set(names)):
                    raise DfuError("ZIP contains duplicate entries")

                def read(name: str, maximum: int) -> bytes:
                    if not isinstance(name, str) or "\\" in name:
                        raise DfuError("Invalid filename in manifest")
                    member = PurePosixPath(name)
                    if member.is_absolute() or ".." in member.parts or ":" in name:
                        raise DfuError("Invalid filename in manifest")
                    entry = archive.getinfo(name)
                    if entry.is_dir() or entry.file_size > maximum:
                        raise DfuError(f"ZIP entry {name!r} exceeds {maximum} bytes")
                    return archive.read(entry)

                manifest = json.loads(read("manifest.json", 16 * 1024))
                images = manifest["manifest"]
                if set(images) != {"application"}:
                    raise DfuError("Only APP-only packages are supported; no MBR/CFG/RAW")
                app = images["application"]
                command = read(app["dat_file"], 128)
                image = read(app["bin_file"], limit)
        except DfuError:
            raise
        except (OSError, ValueError, KeyError, TypeError, AttributeError,
                zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
            raise DfuError(f"Cannot read OnMicro package: {exc}") from exc

        if len(command) != 24:
            raise DfuError("Expected a 24-byte unsigned OnMicro APP DAT")
        magic, protocol, reserved, flags, count = struct.unpack_from("<IHHHH", command)
        image_type, image_reserved, crc16, rom_id, size = struct.unpack_from("<BBHII", command, 12)
        if (magic != 0x01BFDF55 or protocol not in (3, 6) or reserved or flags or
                count != 1 or image_type != 0 or image_reserved):
            raise DfuError("Unsupported DAT: expected one unsigned OnMicro APP image")
        if not 56 <= len(image) <= limit or size != len(image):
            raise DfuError("APP length is invalid or differs from DAT")
        if binascii.crc_hqx(image, 0) != crc16:
            raise DfuError("APP CRC16 differs from DAT")
        if rom_id != ROM_ID or struct.unpack_from("<I", image, 52)[0] != rom_id:
            raise DfuError("APP/DAT ROM ID must be 0x6626A100 for this project")
        address, marker = struct.unpack_from("<II", image, 36)
        if address != 0x00404000 or marker != 0x46533252:
            raise DfuError("APP is not a normal-boot image linked at 0x00404000")
        versions = {match.decode("ascii") for match in
                    re.findall(rb"(V[0-9]{3}(?:_[0-9A-Za-z]+)*)\x00", image)}
        return cls(command, image, rom_id, crc16,
                   next(iter(versions)) if len(versions) == 1 else None,
                   hashlib.sha256(image).hexdigest())

    def describe(self) -> dict:
        return {"version": self.version, "bytes": len(self.image),
                "rom_id": f"0x{self.rom_id:08X}", "crc16": f"{self.crc16:04X}",
                "crc32": f"{binascii.crc32(self.image):08X}", "sha256": self.sha256}
