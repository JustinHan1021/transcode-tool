"""Build a realistic media corpus.

Synthetic test patterns encode trivially and hide problems, so the corpus mixes
colour bars, moving gradients, real screen recordings from the user's machine,
and deliberately awkward files (VFR, odd dimensions, huge images, broken files,
paths with spaces and CJK).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# The repo root is wherever this file ended up, so a clone runs anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from trans import ff  # noqa: E402

FFMPEG = str(ff.detect().ffmpeg)
BASE = ROOT / "_corpus"
CACHE = BASE / "cache"
VIDEO = BASE / "video"
AUDIO = BASE / "audio"
IMAGE = BASE / "image"
ODD = BASE / "odd"
REAL = BASE / "real"


def sh(*args, check=True):
    p = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args],
                       capture_output=True)
    if check and p.returncode != 0:
        raise RuntimeError(" ".join(str(a) for a in args)[:120] + " -> "
                           + p.stderr.decode("utf-8", "replace")[-200:])


def fresh() -> None:
    for d in (VIDEO, AUDIO, IMAGE, ODD, REAL):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)


def master_clip() -> Path:
    """One 20-second 1080p30 source with audio and motion, reused everywhere."""
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / "master.mp4"
    if not p.exists():
        sh("-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30:d=20",
           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=20",
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
           "-c:a", "aac", "-b:a", "128k", "-shortest", str(p))
    return p


def build_video() -> list[Path]:
    m = master_clip()
    made = []
    specs = [
        ("h264_1080p30.mp4", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                              "-c:a", "aac", "-b:a", "128k"]),
        ("h264_1080p30.mov", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                              "-c:a", "aac", "-b:a", "128k"]),
        ("hevc_1080p30.mp4", ["-c:v", "libx265", "-preset", "ultrafast", "-crf", "28",
                              "-tag:v", "hvc1", "-c:a", "aac"]),
        ("vp9_1080p30.webm", ["-c:v", "libvpx-vp9", "-crf", "34", "-b:v", "0",
                              "-row-mt", "1", "-cpu-used", "5", "-c:a", "libopus"]),
        ("vp8_720p25.webm", ["-vf", "scale=1280:720", "-r", "25", "-c:v", "libvpx",
                             "-crf", "12", "-b:v", "0", "-deadline", "good",
                             "-cpu-used", "5", "-an"]),
        ("mpeg4_1080p30.avi", ["-c:v", "mpeg4", "-q:v", "4", "-c:a", "libmp3lame",
                               "-q:a", "4"]),
        ("h264_1080p30.ts", ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                             "-f", "mpegts"]),
        ("h264_1080p30.flv", ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                              "-f", "flv"]),
        ("mute_1080p30.mp4", ["-c:v", "libx264", "-preset", "ultrafast", "-an"]),
        ("square_600.mp4", ["-vf", "crop=600:600:(iw-600)/2:(ih-600)/2",
                            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac"]),
        ("portrait_1080x1920.mp4", ["-vf", "crop=608:1080:(iw-608)/2:0",
                                    "-c:v", "libx264", "-preset", "ultrafast",
                                    "-c:a", "aac"]),
        ("tiny_160x90.mp4", ["-vf", "scale=160:90", "-c:v", "libx264",
                             "-preset", "ultrafast", "-c:a", "aac"]),
        ("odd_641x359.mp4", ["-vf", "scale=642:360", "-c:v", "libx264",
                             "-preset", "ultrafast", "-c:a", "aac"]),
        ("mpeg2.mp4", ["-c:v", "mpeg2video", "-q:v", "4", "-c:a", "mp2"]),
        ("vfr_screenrec.webm", ["-c:v", "libvpx-vp9", "-crf", "40", "-b:v", "0",
                               "-row-mt", "1", "-cpu-used", "5", "-an"]),
        ("audio_only_5s.mp4", ["-t", "5", "-vn", "-c:a", "aac"]),
    ]
    for name, extra in specs:
        out = VIDEO / name
        sh("-i", str(m), *extra, str(out))
        made.append(out)

    # the two awkward ones need special handling
    sh("-i", str(m), "-vf", "fps=12,scale=2560:1154", "-c:v", "libvpx-vp9", "-crf", "40",
       "-b:v", "0", "-row-mt", "1", "-cpu-used", "5", "-an", str(VIDEO / "vfr_tall.webm"))
    made.append(VIDEO / "vfr_tall.webm")
    # a container whose header lies about the frame size is copied from a real one
    real = REAL / "screenrec.webm"
    if real.exists():
        shutil.copy2(real, VIDEO / "real_webm.webm")
        made.append(VIDEO / "real_webm.webm")
    return made


def build_audio() -> list[Path]:
    m = master_clip()
    made = []
    for name, extra in [
        ("a.mp3", ["-c:a", "libmp3lame", "-b:a", "192k"]),
        ("a_16k_mono.wav", ["-c:a", "pcm_s16le", "-ar", "16000", "-ac", "1"]),
        ("a_48k_stereo.wav", ["-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"]),
        ("a.m4a", ["-c:a", "aac", "-b:a", "192k"]),
        ("a.aac", ["-c:a", "aac", "-b:a", "128k", "-f", "adts"]),
        ("a.flac", ["-c:a", "flac"]),
        ("a.ogg", ["-c:a", "libvorbis", "-q:a", "5"]),
        ("a.opus", ["-c:a", "libopus", "-b:a", "96k"]),
    ]:
        sh("-i", str(m), "-vn", *extra, str(AUDIO / name))
        made.append(AUDIO / name)
    return made


def build_image() -> list[Path]:
    made = []
    src = CACHE / "still.png"
    if not src.exists():
        sh("-f", "lavfi", "-i", "testsrc2=s=1920x1080", "-frames:v", "1", str(src))
    # an image with real alpha
    alpha = CACHE / "alpha.png"
    if not alpha.exists():
        sh("-f", "lavfi", "-i",
           "color=c=orange:s=800x600:d=0.1,format=rgba,"
           "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(lt(hypot(X-400,Y-300),220),255,0)'",
           "-frames:v", "1", str(alpha))
    huge = CACHE / "huge.png"
    if not huge.exists():
        sh("-f", "lavfi", "-i", "testsrc2=s=8256x5504", "-frames:v", "1", str(huge))

    specs = [
        ("i_1920x1080.png", src, 1, ["-frames:v", "1"]),
        ("i_rgba.png", alpha, 1, ["-frames:v", "1"]),
        ("i_opaque.jpg", src, 1, ["-frames:v", "1", "-q:v", "2", "-pix_fmt", "yuvj420p"]),
        ("i.webp", src, 1, ["-frames:v", "1", "-c:v", "libwebp", "-quality", "85"]),
        ("i_alpha.webp", alpha, 1, ["-frames:v", "1", "-c:v", "libwebp",
                                    "-quality", "85"]),
        ("i.bmp", src, 1, ["-frames:v", "1"]),
        ("i.tiff", src, 1, ["-frames:v", "1", "-compression_algo", "lzw"]),
        ("i_huge.png", huge, 1, ["-frames:v", "1"]),
        ("i_small.png", src, 1, ["-frames:v", "1", "-vf", "scale=64:36"]),
    ]
    for name, source, _n, extra in specs:
        out = IMAGE / name
        sh("-i", str(source), *extra, str(out))
        made.append(out)

    # animated inputs
    sh("-i", str(master_clip()), "-t", "2", "-vf", "fps=10,scale=320:-2", "-c:v",
       "gif", "-loop", "0", str(IMAGE / "anim.gif"))
    made.append(IMAGE / "anim.gif")
    sh("-i", str(master_clip()), "-t", "2", "-vf", "fps=10,scale=320:-2", "-c:v",
       "libwebp_anim", "-quality", "75", "-loop", "0", str(IMAGE / "anim.webp"))
    made.append(IMAGE / "anim.webp")

    # a photo carrying EXIF orientation (what a phone produces)
    from PIL import Image as PILImage
    im = PILImage.new("RGB", (1200, 800), (40, 90, 160))
    ex = im.getexif()
    ex[274] = 6
    p = IMAGE / "photo_rot.jpg"
    im.save(p, quality=92, exif=ex)
    made.append(p)

    # a frame set for the slideshow feature, deliberately mixed sizes
    for i, (w, h, c) in enumerate([(1600, 900, "red"), (800, 600, "green"),
                                   (1000, 1000, "blue"), (1400, 400, "yellow"),
                                   (640, 480, "purple")]):
        sh("-f", "lavfi", "-i", f"color=c={c}:s={w}x{h}:d=0.1", "-frames:v", "1",
           str(IMAGE / f"seq{i + 1:02d}.png"))
        made.append(IMAGE / f"seq{i + 1:02d}.png")
    return made


def build_odd() -> list[Path]:
    """Files that should fail cleanly rather than crash or hang."""
    made = []
    p = ODD / "empty.mp4"
    p.write_bytes(b"")
    made.append(p)
    p = ODD / "text_renamed.mp4"
    p.write_bytes(b"this is not a video at all\n" * 200)
    made.append(p)
    p = ODD / "truncated.mp4"
    data = (master_clip()).read_bytes()
    p.write_bytes(data[: len(data) // 3])
    made.append(p)
    p = ODD / "one_pixel.png"
    sh("-f", "lavfi", "-i", "color=c=black:s=1x1:d=0.1", "-frames:v", "1", str(p))
    made.append(p)
    return made


def build_special_paths() -> list[Path]:
    """Names that break naive command construction."""
    d = BASE / "特殊 路径 with spaces & symbols"
    d.mkdir(parents=True, exist_ok=True)
    made = []
    for name in ["视频 文件 (1) [测试].mp4", "with'quote.mp4", "with&ampersand.mp4",
                 "点号.和中文。mp4"]:
        out = d / name
        sh("-t", "3", "-i", str(master_clip()), "-c", "copy", str(out))
        made.append(out)
    long_name = ("很长的名字" * 20) + ".mp4"
    out = d / long_name
    sh("-t", "3", "-i", str(master_clip()), "-c", "copy", str(out))
    made.append(out)
    return made


def real_files() -> list[Path]:
    """Optional: real recordings of your own, which catch what synthetic clips miss.

    Point REAL_SOURCE at a folder (or pass it as the first argument) and the
    first few screen recordings in it get copied in. Synthetic material encodes
    trivially and hides problems that only show up on genuine footage, so this is
    worth doing -- but nothing here may depend on one particular home directory.
    """
    import os
    raw = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TRANS_REAL_SOURCE", ""))
    src = Path(raw) if raw else None
    out: list[Path] = []
    if src is None or not src.is_dir():
        return out
    REAL.mkdir(parents=True, exist_ok=True)
    patterns = ("*.webm", "*.mp4", "*.mov", "*.mkv")
    found: list[Path] = []
    for pattern in patterns:
        found.extend(sorted(src.glob(pattern)))
    for f in found[:4]:
        target = REAL / f.name.replace(" ", "_")
        try:
            if not target.exists():
                shutil.copy2(f, target)
        except OSError:
            continue
        out.append(target)
    return out


def main() -> None:
    fresh()
    print("生成素材…")
    v = build_video()
    print("  视频 %d 个" % len(v))
    a = build_audio()
    print("  音频 %d 个" % len(a))
    i = build_image()
    print("  图片 %d 个" % len(i))
    o = build_odd()
    print("  异常 %d 个" % len(o))
    s = build_special_paths()
    print("  特殊路径 %d 个" % len(s))
    r = real_files()
    print("  真实录屏 %d 个%s" % (len(r), "" if r else
                                 "（可选：传入素材目录以加入真实录屏）"))
    print()
    for d in (VIDEO, AUDIO, IMAGE, ODD, REAL):
        print("%s:" % d.name)
        for f in sorted(d.iterdir()):
            print("   %-34s %8.1f KB" % (f.name, f.stat().st_size / 1024))
    print()
    print("特殊路径目录:")
    for f in s:
        print("   %s  %.1f KB" % (f.name[:60], f.stat().st_size / 1024))


if __name__ == "__main__":
    main()
