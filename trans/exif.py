"""EXIF orientation handling.

The ffmpeg build on this machine (4.3.1) ignores the EXIF orientation tag
completely: a 200x100 landscape JPEG tagged orientation=6 comes back out as
200x100 instead of the 100x200 a viewer would show. Phone photos are the main
image input for this kind of tool, so every one of them would land rotated 90
degrees. We therefore read the tag ourselves and turn it into the equivalent
filter chain.

Parsing is deliberately dependency-free: a JPEG's APP1 segment contains a plain
TIFF structure, so the same IFD walker handles both formats.
"""
from __future__ import annotations

import struct
from pathlib import Path

ORIENTATION_TAG = 0x0112

# EXIF orientation value -> ffmpeg filter chain that produces the upright image.
# ffmpeg's transpose: 0 = 90 CCW + vflip, 1 = 90 CW, 2 = 90 CCW, 3 = 90 CW + vflip.
ORIENTATION_FILTERS: dict[int, str] = {
    1: "",                      # already upright
    2: "hflip",                 # mirror horizontal
    3: "hflip,vflip",           # rotate 180
    4: "vflip",                 # mirror vertical
    5: "transpose=0",           # mirror horizontal + rotate 270 CW
    6: "transpose=1",           # rotate 90 CW
    7: "transpose=3",           # mirror horizontal + rotate 90 CW
    8: "transpose=2",           # rotate 270 CW
}


def _read_ifd0_orientation(buf: bytes, tiff_start: int) -> int:
    """Parse the IFD0 orientation tag out of a TIFF-structured buffer."""
    if tiff_start + 8 > len(buf):
        return 1
    order = buf[tiff_start:tiff_start + 2]
    if order == b"II":
        endian = "<"
    elif order == b"MM":
        endian = ">"
    else:
        return 1
    try:
        if struct.unpack_from(endian + "H", buf, tiff_start + 2)[0] != 42:
            return 1
        ifd_off = struct.unpack_from(endian + "I", buf, tiff_start + 4)[0]
        ifd = tiff_start + ifd_off
        if ifd + 2 > len(buf):
            return 1
        count = struct.unpack_from(endian + "H", buf, ifd)[0]
        # A malformed count would walk off the end; sanity-cap it.
        if count > 4096:
            return 1
        for i in range(count):
            entry = ifd + 2 + i * 12
            if entry + 12 > len(buf):
                return 1
            tag, ftype, n = struct.unpack_from(endian + "HHI", buf, entry)
            if tag != ORIENTATION_TAG:
                continue
            if ftype != 3 or n < 1:      # 3 == SHORT
                return 1
            value = struct.unpack_from(endian + "H", buf, entry + 8)[0]
            return value if 1 <= value <= 8 else 1
    except (struct.error, IndexError):
        return 1
    return 1


def _from_jpeg(data: bytes) -> int:
    if not data.startswith(b"\xff\xd8"):
        return 1
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        # Standalone markers carry no payload.
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker == 0xDA:            # start of scan -- no more metadata
            return 1
        seg_len = struct.unpack_from(">H", data, pos + 2)[0]
        if seg_len < 2:
            return 1
        if marker == 0xE1:
            payload = data[pos + 4:pos + 2 + seg_len]
            if payload.startswith(b"Exif\x00\x00"):
                return _read_ifd0_orientation(payload, 6)
        pos += 2 + seg_len
    return 1


def orientation(path: str | Path) -> int:
    """EXIF orientation (1-8) of a JPEG or TIFF, or 1 if absent/undeterminable."""
    p = Path(path)
    ext = p.suffix.lower()
    try:
        # Metadata always lives near the front; 256 KB is far more than enough
        # and keeps this cheap even for huge scans.
        with p.open("rb") as fh:
            data = fh.read(262144)
    except OSError:
        return 1

    if ext in (".jpg", ".jpeg", ".jfif") or data.startswith(b"\xff\xd8"):
        return _from_jpeg(data)
    if ext in (".tif", ".tiff"):
        return _read_ifd0_orientation(data, 0)
    return 1


def describe(value: int) -> str:
    return {
        1: "正常", 2: "水平镜像", 3: "旋转 180°", 4: "垂直镜像",
        5: "镜像并旋转 270°", 6: "旋转 90°", 7: "镜像并旋转 90°", 8: "旋转 270°",
    }.get(value, "未知")
