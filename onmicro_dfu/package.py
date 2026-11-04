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
HOST_LAYOUT = "host752"
HOST_LIMIT = 752 * 1024


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
    layout: str = "scale"
    release_counter: int | None = None
    hardware: str | None = None
    model: str | None = None

    def validate_device(self, info: dict) -> None:
        if self.layout == HOST_LAYOUT:
            current = info.get("release_counter")
            if (info.get("layout") != HOST_LAYOUT or info.get("execution_address") != "0x00409000"
                    or type(current) is not int or not 0 < current < 0xffffffff):
                raise DfuError("此包用于752 KiB主机，连接设备未报告匹配的主机DFU布局")
            if type(self.release_counter) is not int or not 0 < self.release_counter < 0xffffffff:
                raise DfuError("主机固件安装计数无效")
            # Upgrade/reinstall/downgrade all retain package integrity checks.
            # Old strict receivers may still reject at the command stage.
        elif (info.get("rom_id") != f"0x{self.rom_id:08X}" or
              info.get("execution_address") != "0x00404000" or
              info.get("boot_marker") != "0x46533252" or info.get("layout") == HOST_LAYOUT):
            raise DfuError("从机固件与设备ROM ID或0x404000启动布局不匹配")

    @classmethod
    def host_image(cls, command: bytes, image: bytes) -> FirmwarePackage:
        if len(command) != 24 or not 784 <= len(image) <= HOST_LIMIT:
            raise DfuError("主机APP长度或DAT格式不正确")
        magic, protocol, reserved, flags, count, image_word, counter, size = struct.unpack("<IHHHHIII", command)
        if (magic != 0x01bfdf55 or protocol not in (3, 6) or reserved or flags or
                count != 1 or image_word or size != len(image) or not 0 < counter < 0xffffffff):
            raise DfuError("主机仅支持单APP、无签名、带有效安装计数的DAT")
        raw, footer = image[:-256], image[-256:]
        sp, pc = struct.unpack_from("<II", raw)
        compiled = struct.unpack_from("<4I", raw, 512)
        values = struct.unpack_from("<10I", footer)
        if compiled != (0x4449344e, 0x75247524, 1, counter):
            raise DfuError("主机APP编译身份、布局或安装计数与DAT不一致")
        if (not 0x20000000 < sp <= 0x2003fe00 or sp % 8 or not pc & 1 or
                not 0x409000 <= (pc & ~1) < 0x409000 + len(raw)):
            raise DfuError("主机向量表与0x409000执行地址不匹配")
        if values != (0x4934524e, 1, 0x75247524, 0xa16627, 0x409000,
                      len(raw), counter, 1, 0, binascii.crc32(raw)):
            raise DfuError("主机镜像尾部身份、芯片、大小、计数或CRC32错误")
        if (hashlib.sha256(raw).digest() != footer[40:72] or
                binascii.crc32(footer[:252]) != struct.unpack_from("<I", footer, 252)[0]):
            raise DfuError("主机镜像SHA-256或尾部CRC32错误")
        target = b"SC201:MODEL=SC200-D\0SC201:HW=DFT-SC20X-HUB\0"
        models = set(re.findall(rb"SC201:MODEL=([!-~]+)\x00", raw))
        hardware = set(re.findall(rb"SC201:HW=([!-~]+)\x00", raw))
        if target not in raw or models != {b"SC200-D"} or hardware != {b"DFT-SC20X-HUB"}:
            raise DfuError("主机包必须包含唯一的SC200-D / DFT-SC20X-HUB完整目标标记")
        versions = {m.decode("ascii") for m in re.findall(rb"(V[0-9]{3}(?:_[0-9A-Za-z]+)*)\x00", raw)}
        if len(versions) != 1:
            raise DfuError("主机包必须包含唯一的显示版本")
        return cls(command, image, 0xa16627, binascii.crc_hqx(image, 0), next(iter(versions)),
                   hashlib.sha256(image).hexdigest(), HOST_LAYOUT, counter, "DFT-SC20X-HUB", "SC200-D")

    @classmethod
    def load(cls, path: Path, *, expanded: bool = False) -> FirmwarePackage:
        if expanded not in (False, True, HOST_LAYOUT):
            raise DfuError("未知设备分区配置")
        limit = HOST_LIMIT if expanded == HOST_LAYOUT else EXPANDED_LIMIT if expanded else LEGACY_LIMIT
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
        if expanded == HOST_LAYOUT:
            return cls.host_image(command, image)
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
                "crc32": f"{binascii.crc32(self.image):08X}", "sha256": self.sha256,
                "layout": self.layout, "release_counter": self.release_counter,
                "hardware": self.hardware, "model": self.model}
