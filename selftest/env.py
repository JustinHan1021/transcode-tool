"""What is this machine actually using right now?"""
from __future__ import annotations

import sys
from pathlib import Path

# The repo root is wherever this file ended up, so a clone runs anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from trans import ff  # noqa: E402
from trans.presets import VIDEO_ENCODERS  # noqa: E402

caps = ff.detect()
print("ffmpeg      : %s" % caps.ffmpeg)
print("ffprobe     : %s" % caps.ffprobe)
print("version     : %s" % caps.version)
print("passthrough : %s" % caps.passthrough_args)
print("anim webp in: %s" % caps.animated_webp_input)
print("notes       : %s" % (caps.notes or "无"))
print("muxers seen : %d   filters: %d" % (len(caps.muxers), len(caps.filters)))
print()
print("硬件编码器实测:")
for key, spec in VIDEO_ENCODERS.items():
    if not spec.hardware:
        continue
    listed = caps.has_encoder(spec.encoder)
    works = caps.encoding_works(spec.encoder)
    print("  %-14s listed=%-5s trial=%s" % (spec.encoder, listed, works))
print()
need = {
    "libx264": "x264", "libx265": "x265", "libvpx-vp9": "vp9", "libvpx": "vp8",
    "mpeg4": "mpeg4", "libwebp": "webp enc", "libwebp_anim": "anim webp enc",
    "libmp3lame": "mp3", "aac": "aac", "libopus": "opus", "libvorbis": "vorbis",
    "flac": "flac", "ac3": "ac3", "pcm_s16le": "pcm",
}
print("软编/复用器齐备性:")
for enc, label in need.items():
    print("  %-14s %-14s %s" % (enc, label, "OK" if caps.has_encoder(enc) else "缺失"))
for mux in ("mp4", "matroska", "webm", "avi", "mpegts", "flv", "gif", "ipod",
            "adts", "wav", "flac", "ogg", "opus", "webp", "image2", "tiff"):
    if not caps.has_muxer(mux):
        print("  缺少 muxer: %s" % mux)
