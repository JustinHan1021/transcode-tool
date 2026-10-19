"""Media inspection.

Uses ffprobe when it exists, and otherwise parses the human-readable output of
`ffmpeg -i`, which carries every field we need. That fallback matters: plenty of
shipped or third-party ffmpeg builds have no ffprobe next to them, and the app
must still be able to read a file's duration, resolution and codecs.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import exif
from . import ff
from .ff import codec_family, run

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".jfif", ".webp", ".bmp", ".tif", ".tiff",
              ".avif", ".heic", ".heif", ".jxl", ".ico", ".tga", ".dds", ".exr"}
ANIMATED_CAPABLE = {".gif", ".webp", ".png", ".avif"}  # apng / animated webp / animated avif

# The format names ffmpeg reports for the *animated* variants of a still-image
# extension. Only these can hide a frame sequence behind, say, a .webp name, so
# only these are worth a packet-counting pass -- a plain photo must not pay for
# one.
ANIMATED_FORMATS = {"webp_anim", "apng", "animated_png"}

# Anything above this is a timebase, not a frame rate. Chrome's screen recorder
# writes WebM with a 1 ms timebase, so ffprobe reports r_frame_rate=1000/1 for a
# clip that actually runs at ~17 fps; treating that as real makes ffmpeg
# duplicate frames up to 1000 per second.
MAX_PLAUSIBLE_FPS = 240.0


def _sane_fps(value: float) -> float:
    if not value or value < 1.0 or value > MAX_PLAUSIBLE_FPS:
        return 0.0
    return value

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".ts", ".m4v", ".flv",
              ".wmv", ".mpg", ".mpeg", ".m2ts", ".mts", ".3gp", ".ogv", ".vob",
              ".rmvb", ".asf", ".f4v", ".mxf", ".gif"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus",
              ".wma", ".aiff", ".aif", ".ape", ".alac", ".amr", ".ac3", ".mid"}


@dataclass
class Stream:
    index: int
    kind: str            # video | audio | subtitle | data
    codec: str           # normalised family
    raw_codec: str = ""
    profile: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    pix_fmt: str = ""
    sample_rate: int = 0
    channels: int = 0
    bitrate: int = 0      # bits/s, 0 when unknown
    # Seconds per tick, as ffprobe spells it ("1/15360"). Concatenation needs it:
    # the concat demuxer requires every input to share one time base, and a
    # stream copy quietly carries the source's own.
    time_base: str = ""


@dataclass
class MediaInfo:
    path: Path
    kind: str = "unknown"          # video | audio | image | unknown
    container: str = ""
    duration: float | None = None  # seconds; None for stills without a duration
    size: int = 0
    streams: list[Stream] = field(default_factory=list)
    title: str = ""
    error: str = ""
    warn: str = ""                 # non-fatal problem worth showing the user
    frames: int = 0                # counted only when the duration had to be recovered
    vfr: bool = False              # header declared no usable constant frame rate
    declared_size: tuple[int, int] | None = None   # what the header claims, if wrong
    exif_rotated: bool = False     # the decoder already applied EXIF orientation

    # -- convenience --------------------------------------------------------
    @property
    def video_stream(self) -> Stream | None:
        for s in self.streams:
            if s.kind == "video":
                return s
        return None

    @property
    def audio_stream(self) -> Stream | None:
        for s in self.streams:
            if s.kind == "audio":
                return s
        return None

    @property
    def has_video(self) -> bool:
        return self.video_stream is not None

    @property
    def has_audio(self) -> bool:
        return self.audio_stream is not None

    @property
    def vcodec(self) -> str:
        s = self.video_stream
        return s.codec if s else ""

    @property
    def acodec(self) -> str:
        s = self.audio_stream
        return s.codec if s else ""

    @property
    def width(self) -> int:
        s = self.video_stream
        return s.width if s else 0

    @property
    def height(self) -> int:
        s = self.video_stream
        return s.height if s else 0

    @property
    def fps(self) -> float:
        s = self.video_stream
        return s.fps if s else 0.0

    @property
    def pix_fmt(self) -> str:
        s = self.video_stream
        return s.pix_fmt if s else ""

    @property
    def sample_rate(self) -> int:
        s = self.audio_stream
        return s.sample_rate if s else 0

    @property
    def channels(self) -> int:
        s = self.audio_stream
        return s.channels if s else 0

    @property
    def time_base(self) -> str:
        s = self.video_stream
        return s.time_base if s else ""

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}" if self.width else ""

    @property
    def effective_fps(self) -> float:
        """The real average frame rate, even when the header lies.

        A VFR screen recording has no constant rate to report, so the honest
        number comes from the frames we counted divided by its duration.
        """
        if self.fps:
            return self.fps
        if self.frames and self.duration:
            return self.frames / self.duration
        return 0.0

    @property
    def bitrate(self) -> int:
        if self.size and self.duration:
            return int(self.size * 8 / self.duration)
        return 0

    @property
    def is_animated(self) -> bool:
        """Animated GIF/WebP/APNG: a still-image extension but a real duration."""
        if self.path.suffix.lower() not in ANIMATED_CAPABLE:
            return False
        return bool(self.duration and self.duration > 0.15)

    @property
    def duration_label(self) -> str:
        if not self.duration:
            return "--:--"
        total = int(round(self.duration))
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    @property
    def size_label(self) -> str:
        n = float(self.size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024 or unit == "TB":
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024
        return ""

    def summary(self) -> str:
        bits = []
        if self.has_video:
            bits.append(self.vcodec or "video")
            if self.resolution:
                bits.append(self.resolution)
                if self.declared_size:
                    bits.append(f"头部声明{self.declared_size[0]}x{self.declared_size[1]}")
            eff = self.effective_fps
            if self.fps:
                bits.append(f"{self.fps:g}fps")
            elif eff:
                # Say so plainly: a fixed rate here would be a fabrication.
                bits.append(f"平均{eff:.1f}fps 可变" if self.vfr else f"{eff:.1f}fps")
        if self.has_audio:
            bits.append(self.acodec or "audio")
            if self.sample_rate:
                bits.append(f"{self.sample_rate/1000:g}k")
            if self.channels:
                bits.append({1: "mono", 2: "stereo"}.get(self.channels, f"{self.channels}ch"))
        return " · ".join(bits)


class ProbeError(RuntimeError):
    pass


# ------------------------------------------------------------------ ffprobe

def _from_ffprobe(path: Path) -> MediaInfo:
    caps = ff.detect()
    args = ["-hide_banner", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(path)]
    rc, out, err = run(caps.ffprobe, args, timeout=60)
    if rc != 0 or not out.strip():
        raise ProbeError(err.strip()[:400] or "ffprobe 无法解析该文件")
    data = json.loads(out)

    fmt = data.get("format") or {}
    info = MediaInfo(path=path, container=fmt.get("format_name", ""),
                     size=int(float(fmt.get("size") or 0)),
                     title=(fmt.get("tags") or {}).get("title", ""))
    dur = fmt.get("duration")
    if dur not in (None, "N/A"):
        try:
            info.duration = float(dur)
        except ValueError:
            info.duration = None

    for s in data.get("streams", []):
        stype = s.get("codec_type", "")
        if stype == "attachment":
            continue
        raw = s.get("codec_name", "")
        st = Stream(index=int(s.get("index", 0)), kind=stype,
                    codec=codec_family(raw), raw_codec=raw,
                    profile=str(s.get("profile", "") or ""))
        if stype == "video":
            st.width = int(s.get("width") or 0)
            st.height = int(s.get("height") or 0)
            st.pix_fmt = s.get("pix_fmt") or ""
            st.time_base = str(s.get("time_base") or "")
            # Some containers report 0/0 for the average; fall back to the other.
            raw_fps = (_parse_ratio(s.get("avg_frame_rate"))
                       or _parse_ratio(s.get("r_frame_rate")))
            st.fps = _sane_fps(raw_fps)
            if raw_fps and not st.fps:
                info.vfr = True          # header carries a timebase, not a rate
        elif stype == "audio":
            st.sample_rate = int(s.get("sample_rate") or 0)
            st.channels = int(s.get("channels") or 0)
        try:
            st.bitrate = int(s.get("bit_rate") or 0)
        except (TypeError, ValueError):
            st.bitrate = 0
        info.streams.append(st)

    _classify(info)
    return info


def _parse_ratio(value) -> float:
    if not value or value in ("N/A", "0/0"):
        return 0.0
    try:
        if "/" in str(value):
            num, den = str(value).split("/", 1)
            num, den = float(num), float(den)
            return num / den if den else 0.0
        return float(value)
    except (ValueError, ZeroDivisionError):
        return 0.0


# ------------------------------------------------------- ffmpeg -i fallback

_CONTAINER_RE = re.compile(r"^\s*Input #\d+,\s*([^,]+),\s*from\s*'", re.M)
_DURATION_RE = re.compile(r"^\s*Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)", re.M)
_TITLE_RE = re.compile(r"^\s*title\s*:\s*(.+)$", re.M)
_STREAM_RE = re.compile(
    r"^\s*Stream #\d+:(?P<idx>\d+)(?:\[[^\]]*\])?(?:\((?P<lang>[^)]*)\))?:\s*"
    r"(?P<kind>Video|Audio|Subtitle|Data|Attachment):\s*(?P<rest>.*)$", re.M)
_FPS_RE = re.compile(r"([\d.]+)\s*fps")
_DIM_RE = re.compile(r"(?<![\d.])(\d{2,5})x(\d{2,5})(?![\d.])")
_HZ_RE = re.compile(r"(\d+)\s*Hz")
_KBPS_RE = re.compile(r"(\d+)\s*kb/s")
_FOURCC_RE = re.compile(r"\(([0-9a-z_]{2,6})\s*/\s*0x[0-9a-fA-F]+\)")
_PROFILE_RE = re.compile(r"^\(([^)/]+?)\)")


def _from_ffmpeg_i(path: Path) -> MediaInfo:
    caps = ff.detect()
    rc, out, err = run(caps.ffmpeg, ["-hide_banner", "-i", str(path)], timeout=60)
    text = err or out
    # `ffmpeg -i` always exits non-zero ("At least one output file must be
    # specified"); a genuine failure shows up as missing stream/duration info.
    if "Invalid data found" in text or "No such file" in text:
        line = next((l.strip() for l in text.splitlines()
                     if "Invalid data" in l or "No such file" in l), "")
        raise ProbeError(line[:300] or "无法解析该文件")

    info = MediaInfo(path=path)
    m = _CONTAINER_RE.search(text)
    if m:
        info.container = m.group(1).strip()
    m = _DURATION_RE.search(text)
    if m:
        h, mm, ss = m.group(1), m.group(2), m.group(3)
        info.duration = int(h) * 3600 + int(mm) * 60 + float(ss)
    m = _TITLE_RE.search(text)
    if m:
        info.title = m.group(1).strip()

    for sm in _STREAM_RE.finditer(text):
        rest = sm.group("rest").strip()
        kind_word = sm.group("kind").lower()
        if kind_word == "attachment":
            continue
        kind = {"video": "video", "audio": "audio",
                "subtitle": "subtitle", "data": "data"}[kind_word]
        parts = rest.split(",", 1)
        head = parts[0].strip()
        tail = parts[1] if len(parts) > 1 else ""

        raw_codec = head.split()[0].strip("()") if head else ""
        st = Stream(index=int(sm.group("idx")), kind=kind,
                    codec=codec_family(raw_codec), raw_codec=raw_codec)
        pm = _PROFILE_RE.match(head[len(raw_codec):].strip())
        if pm:
            st.profile = pm.group(1).strip()
        fm = _FOURCC_RE.search(head)
        if fm:
            st.codec = codec_family(fm.group(1))

        if kind == "video":
            dm = None
            for cand in _DIM_RE.finditer(tail):
                # Skip the "SAR 1:1 DAR 16:9" block, which also looks like WxH.
                if cand.group(2) in ("1",) and cand.group(1) in ("1",):
                    continue
                dm = cand
                break
            if dm:
                st.width, st.height = int(dm.group(1)), int(dm.group(2))
            fpsm = _FPS_RE.search(tail)
            if fpsm:
                st.fps = _sane_fps(float(fpsm.group(1)))
            elif re.search(r"\d+(?:\.\d+)?k tbr", tail) or re.search(r"\d+k tbn", tail):
                # ffmpeg prints "1k tbr, 1k tbn, 1k tbc" and no "fps" token for a
                # millisecond timebase.
                info.vfr = True
            # "15360 tbn" / "1k tbn": ticks per second, spelled as a ratio.
            tbn = re.search(r"([\d.]+)(k?) tbn", tail)
            if tbn:
                ticks = float(tbn.group(1)) * (1000 if tbn.group(2) else 1)
                st.time_base = "1/%d" % int(ticks)
            # Pixel format is the bare token before the dimensions.
            if dm:
                before = tail[:dm.start()].rstrip(" ,")
                token = before.split(",")[-1].strip().split()[-1] if before else ""
                if re.fullmatch(r"[a-z0-9_]{3,20}", token or ""):
                    st.pix_fmt = token
            else:
                for tok in re.findall(r"[a-z0-9_]+p(?:10|12|14|16)?le|[a-z0-9_]*420p|gray\w*", tail):
                    st.pix_fmt = tok
                    break
        elif kind == "audio":
            hzm = _HZ_RE.search(tail)
            if hzm:
                st.sample_rate = int(hzm.group(1))
            names = {"mono": 1, "stereo": 2, "quad": 4,
                     "5.1": 6, "5.1(side)": 6, "7.1": 8}
            for name, n in names.items():
                if re.search(r"(?<![\w.])" + re.escape(name) + r"(?![\w.])", tail):
                    st.channels = n
                    break
        kbps = _KBPS_RE.search(tail)
        if kbps:
            st.bitrate = int(kbps.group(1)) * 1000
        info.streams.append(st)

    if not info.streams:
        raise ProbeError("未识别到任何媒体流")
    _classify(info)
    return info


# ---------------------------------------------------------------- dispatch

def _classify(info: MediaInfo) -> None:
    ext = info.path.suffix.lower()
    if ext in IMAGE_EXTS or (ext == ".gif" and not info.has_audio):
        info.kind = "image"
    elif info.has_video:
        info.kind = "video"
    elif info.has_audio:
        info.kind = "audio"
    else:
        info.kind = "unknown"


_CACHE: dict[str, tuple[float, int, MediaInfo]] = {}


def _measure_duration(path: Path, caps) -> tuple[float | None, int]:
    """Recover the duration (and the frame count) by a packet-copy pass.

    A WebM written by Chrome's MediaRecorder has no Duration element at all, so
    the header genuinely has none and no amount of probing finds one. Reading
    every packet is cheap compared with decoding them -- 0.08s for a 45 MB
    recording -- and without a duration there is no progress bar, no ETA, and no
    way to size a bitrate budget.
    """
    args = ["-hide_banner", "-nostats", "-loglevel", "info", "-i", str(path),
            "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
            "-f", "null", os.devnull]
    try:
        _, out, err = run(caps.ffmpeg, args, timeout=1800)
    except Exception:
        return None, 0
    text = err + out

    duration = None
    stamps = re.findall(r"time=(\d+):(\d\d):(\d\d(?:\.\d+)?)", text)
    if stamps:
        h, mm, ss = stamps[-1]
        value = int(h) * 3600 + int(mm) * 60 + float(ss)
        if value > 0:
            duration = value

    frames = 0
    counts = re.findall(r"frame=\s*(\d+)", text)
    if counts:
        frames = int(counts[-1])
    return duration, frames


def _decoded_size(path: Path, caps, duration: float | None) -> tuple[int, int] | None:
    """The frame size the decoder actually produces.

    Chrome's screen recorder writes a WebM whose track header claims a size the
    coded frames do not have -- 2560x1238 declared against 2560x1154 decoded,
    which is a 6.8% aspect error. Geometry taken from the header would stretch
    the picture. One decoded frame settles it, for about a tenth of a second.
    """
    if not caps.has_filter("showinfo"):
        return None
    seeks: list[float | None] = []
    if duration and duration > 2.0:
        seeks.append(min(1.0, duration / 2))
    seeks.append(None)                 # very short clips: read from the start
    for seek in seeks:
        args = ["-hide_banner", "-loglevel", "info"]
        if seek is not None:
            args += ["-ss", f"{seek:.3f}"]
        args += ["-i", str(path), "-frames:v", "1", "-vf", "showinfo",
                 "-f", "null", os.devnull]
        try:
            _, out, err = run(caps.ffmpeg, args, timeout=180)
        except Exception:
            return None
        hits = re.findall(r"\bs:(\d{2,5})x(\d{2,5})", err + out)
        if hits:
            return int(hits[-1][0]), int(hits[-1][1])
    return None


def _unusable(info: MediaInfo) -> bool:
    """A probe can succeed and still tell us nothing useful.

    An animated WebP, for instance, comes back from ffprobe with width 0/height 0
    and no duration on builds whose WebP demuxer cannot read animation. Treating
    that as a valid result would silently mis-plan every later step.
    """
    if info.kind in ("video", "image", "unknown"):
        return info.width <= 0 or info.height <= 0
    return False


def probe(path: str | Path, use_cache: bool = True,
          measure: bool = True) -> MediaInfo:
    """Inspect a file, preferring ffprobe and falling back to `ffmpeg -i`."""
    p = Path(path)
    try:
        st = p.stat()
    except OSError as exc:
        raise ProbeError(f"文件不可读: {exc}") from exc

    key = str(p.resolve()).lower()
    if use_cache:
        hit = _CACHE.get(key)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]

    caps = ff.detect()
    info: MediaInfo | None = None
    fallback: MediaInfo | None = None

    if caps.ffprobe is not None:
        try:
            cand = _from_ffprobe(p)
            fallback = cand
            if not _unusable(cand):
                info = cand
        except Exception:
            # ffprobe can reject containers ffmpeg itself reads happily, so a
            # failure here is not a verdict -- try the parser before giving up.
            pass

    if info is None:
        try:
            cand = _from_ffmpeg_i(p)
            fallback = cand
            if not _unusable(cand):
                info = cand
        except ProbeError:
            if fallback is None:
                raise

    # Both routes came up empty on the essentials: keep whatever was read so the
    # file can still be listed, and say plainly why it is not usable.
    if info is None:
        info = fallback
    if info is None:
        raise ProbeError("无法解析该文件")
    if _unusable(info):
        info.warn = _explain_unreadable(p, caps)

    # A missing duration is not a missing file: recover it by counting packets so
    # progress, ETA and bitrate maths all have something real to work with.
    #
    # Animated WebP needs this even though it is "an image": ffprobe reports no
    # duration for it at all, and without one `is_animated` stays False, so a
    # 20-frame animation was converted as a single still and the animation was
    # lost without a word.
    animated_image = (info.kind == "image"
                      and p.suffix.lower() in ANIMATED_CAPABLE
                      and (info.container or "").lower() in ANIMATED_FORMATS)
    if measure and info.duration is None and (info.kind in ("video", "audio")
                                              or animated_image):
        duration, frames = _measure_duration(p, caps)
        if duration:
            info.duration = duration
        info.frames = frames

    # The header's frame size is not always the frame size, and -- separately --
    # some ffmpeg builds apply EXIF rotation while others ignore it. One decoded
    # frame settles both. Only oriented JPEG/TIFF need the check, so ordinary
    # images do not pay for it.
    check_size = info.kind == "video"
    if info.kind == "image" and exif.orientation(p) != 1:
        check_size = True
    if measure and check_size and info.width and info.height:
        real = _decoded_size(p, caps, info.duration)
        stream = info.video_stream
        if real and stream is not None and real != (info.width, info.height):
            info.declared_size = (info.width, info.height)
            stream.width, stream.height = real
            # Dimensions swapped exactly = the decoder turned the picture upright.
            if (info.kind == "image"
                    and real == (info.declared_size[1], info.declared_size[0])):
                info.exif_rotated = True

    if not info.size:
        info.size = st.st_size
    _CACHE[key] = (st.st_mtime, st.st_size, info)
    return info


def _explain_unreadable(p: Path, caps) -> str:
    if p.suffix.lower() == ".webp":
        return (f"无法读取该 WebP 的画面尺寸。当前 ffmpeg ({caps.version or '未知'}) "
                f"的 WebP 解复用器不支持动画 WebP，只能作为输出格式；"
                f"如需处理动画 WebP 输入，请换用 ffmpeg 4.4+ 的构建。")
    return (f"无法读取该文件的画面尺寸，当前 ffmpeg ({caps.version or '未知'}) "
            f"可能不支持此格式。")
