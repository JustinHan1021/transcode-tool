"""Container / codec tables and option dataclasses.

Kept apart from plan.py so the UI can build its dropdowns from the same source
of truth that builds the commands.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ------------------------------------------------------------------ containers

@dataclass(frozen=True)
class Container:
    ext: str
    muxer: str
    video: tuple[str, ...]          # compatible video codec families
    audio: tuple[str, ...]          # compatible audio codec families
    default_video: str
    default_audio: str
    faststart: bool = False
    hevc_tag: bool = False          # tag HEVC as hvc1 so QuickTime/Apple accept it
    label: str = ""
    # Legal in this container but poorly supported by players, editors and
    # platform uploaders. Copying one of these is lossless and instant, so it is
    # worth offering -- but never by default, and always with a warning.
    loose: tuple[str, ...] = ()


VIDEO_CONTAINERS: dict[str, Container] = {
    "mp4": Container("mp4", "mp4", ("h264", "hevc", "mpeg4", "av1"),
                     ("aac", "mp3", "ac3", "eac3", "alac"), "h264", "aac",
                     faststart=True, hevc_tag=True,
                     loose=("vp8", "vp9", "opus", "vorbis", "flac", "pcm_s16le"),
                     label="MP4  · 最通用，平台首选"),
    "mov": Container("mov", "mov", ("h264", "hevc", "mpeg4", "prores", "mjpeg", "av1"),
                     ("aac", "mp3", "ac3", "alac", "pcm_s16le", "pcm_s24le"),
                     "h264", "aac", faststart=True, hevc_tag=True,
                     loose=("vp9", "opus", "vorbis"),
                     label="MOV  · 剪辑软件/苹果生态"),
    "m4v": Container("m4v", "mp4", ("h264", "hevc", "mpeg4", "av1"),
                     ("aac", "mp3", "ac3", "eac3", "alac"), "h264", "aac",
                     faststart=True, hevc_tag=True,
                     loose=("vp8", "vp9", "opus", "vorbis", "flac"),
                     label="M4V  · 苹果视频"),
    "mkv": Container("mkv", "matroska", ("h264", "hevc", "vp8", "vp9", "av1", "mpeg4",
                                         "mpeg2video", "theora"),
                     ("aac", "mp3", "opus", "vorbis", "flac", "ac3", "eac3", "dts",
                      "pcm_s16le", "alac"), "h264", "aac",
                     label="MKV  · 万能封装，兼容一切"),
    "webm": Container("webm", "webm", ("vp8", "vp9", "av1"), ("opus", "vorbis"),
                      "vp9", "opus", label="WebM · 网页/小体积"),
    "avi": Container("avi", "avi", ("mpeg4", "mjpeg"),
                     ("mp3", "ac3", "pcm_s16le"), "mpeg4", "mp3",
                     loose=("h264", "aac"),
                     label="AVI  · 老设备/老软件"),
    "ts": Container("ts", "mpegts", ("h264", "hevc", "mpeg2video"),
                    ("aac", "mp3", "ac3", "eac3"), "h264", "aac",
                    label="TS   · 直播流/机顶盒"),
    "flv": Container("flv", "flv", ("h264",), ("aac", "mp3"), "h264", "aac",
                     label="FLV  · 直播推流"),
    "gif": Container("gif", "gif", ("gif",), (), "gif", "",
                     label="GIF  · 动图/表情包"),
}

AUDIO_CONTAINERS: dict[str, Container] = {
    "mp3":  Container("mp3", "mp3", (), ("mp3",), "", "mp3",
                      label="MP3  · 最通用"),
    "m4a":  Container("m4a", "ipod", (), ("aac", "alac"), "", "aac",
                      faststart=True, label="M4A  · AAC，同码率音质更好"),
    "wav":  Container("wav", "wav", (), ("pcm_s16le", "pcm_s24le", "pcm_f32le"),
                      "", "pcm_s16le", label="WAV  · 无损未压缩，剪辑用"),
    "aac":  Container("aac", "adts", (), ("aac",), "", "aac",
                      label="AAC  · 裸流，通用性一般"),
    "flac": Container("flac", "flac", (), ("flac",), "", "flac",
                      label="FLAC · 无损压缩"),
    "ogg":  Container("ogg", "ogg", (), ("vorbis", "opus"), "", "vorbis",
                      label="OGG  · Vorbis"),
    "opus": Container("opus", "opus", (), ("opus",), "", "opus",
                      label="OPUS · 同码率最优，48kHz"),
}

# Audio codec used when writing into a *video* container.
VIDEO_AUDIO_CODECS: dict[str, tuple[str, str]] = {
    # key: (ffmpeg encoder, display label)
    "aac":     ("aac", "AAC"),
    "mp3":     ("libmp3lame", "MP3"),
    "opus":    ("libopus", "Opus"),
    "vorbis":  ("libvorbis", "Vorbis"),
    "flac":    ("flac", "FLAC 无损"),
    "ac3":     ("ac3", "AC3"),
    "pcm_s16le": ("pcm_s16le", "PCM 无损"),
}

# Which demuxer reads a container back -- named as ffprobe reports it, so it can
# be compared against a probed file's format name.
#
# This matters for concatenation: the concat demuxer offsets each file by the
# previous one's duration, and it loses step when the entries come from
# different demuxers. Measured on ffmpeg 9.0.2, an MP4 followed by an MKV gave a
# merge reporting half its length with both clips on top of each other, and an
# MKV followed by an MP4 turned two 2-second clips into a 195-second file.
# Entries that share a demuxer merge correctly.
CONTAINER_DEMUXER: dict[str, str] = {
    "mp4": "mov", "mov": "mov", "m4v": "mov",
    "mkv": "matroska", "webm": "matroska",
    "avi": "avi", "ts": "mpegts", "flv": "flv", "gif": "gif",
}

LOSSLESS_AUDIO = {"flac", "pcm_s16le", "pcm_s24le", "pcm_f32le", "alac"}

# ------------------------------------------------------------------ bitrate

# The bitrate dropdown and the sample-rate dropdown are independent, but the
# encoders are not. Measured on this build, 69 of 864 combinations the UI can
# produce failed outright -- libvorbis rejects 192 kbps at 16 kHz mono with
# "encoder setup failed", libopus refuses anything over 256 kbps per channel --
# and MP3 silently ignores what the MPEG version selected by the sample rate
# cannot carry. Clamping here is what keeps every reachable combination working.
_VORBIS_QUALITY = ((320, 9), (256, 8), (192, 6), (160, 5), (128, 4),
                   (96, 2), (64, 0), (48, -1))


def _mp3_ceiling(sample_rate: int) -> int:
    """MPEG-1 carries 320 kbps, MPEG-2 160, MPEG-2.5 (8-12 kHz) only 64."""
    if sample_rate >= 32000:
        return 320
    if sample_rate >= 16000:
        return 160
    return 64


def audio_bitrate_args(codec: str, bitrate: int, sample_rate: int,
                       channels: int,
                       warnings: list[str] | None = None) -> tuple[list[str], str]:
    """Encoder arguments for one audio codec, plus how to describe them.

    Returns ([], label) for lossless codecs, which ignore bitrate entirely.
    """
    if codec in LOSSLESS_AUDIO:
        return [], ""

    rate = sample_rate or 0
    chans = channels or 2

    if codec == "vorbis":
        # Quality, not bitrate: libvorbis scales the rate itself, so every
        # sample-rate/channel combination stays encodable. The ladder below is
        # the standard one, which targets the listed bitrate at 44.1 kHz stereo.
        quality = next((q for ceiling, q in _VORBIS_QUALITY if bitrate >= ceiling),
                       -2)
        if rate and rate < 32000:
            return (["-q:a", str(quality)],
                    f"质量档 {quality}（低采样率下按比例降低）")
        return ["-q:a", str(quality)], f"质量档 {quality}"

    limit = 0
    reason = ""
    if codec == "opus":
        limit, reason = 256 * chans, "Opus 每声道上限 256kbps"
    elif codec == "mp3":
        limit, reason = _mp3_ceiling(rate), f"{rate/1000:g}kHz 下 MP3 的上限"

    if limit and bitrate > limit:
        if warnings is not None:
            warnings.append(f"{reason}，码率已从 {bitrate} 调整为 {limit}kbps")
        return ["-b:a", f"{limit}k"], f"{limit}kbps"
    return ["-b:a", f"{bitrate}k"], f"{bitrate}kbps"


# --------------------------------------------------------------------- codecs

@dataclass(frozen=True)
class VideoEncoder:
    key: str
    label: str
    encoder: str
    family: str
    hardware: bool = False
    smoke: tuple[str, ...] = ()
    note: str = ""


VIDEO_ENCODERS: dict[str, VideoEncoder] = {
    "h264": VideoEncoder("h264", "H.264 / AVC", "libx264", "h264",
                         note="兼容性最好，所有平台通吃"),
    "h265": VideoEncoder("h265", "H.265 / HEVC", "libx265", "hevc",
                         note="同画质体积约小 40%，编码较慢"),
    "vp9": VideoEncoder("vp9", "VP9", "libvpx-vp9", "vp9",
                        note="WebM 专用，压缩率高"),
    "vp8": VideoEncoder("vp8", "VP8", "libvpx", "vp8",
                        note="WebM 旧编码"),
    "mpeg4": VideoEncoder("mpeg4", "MPEG-4", "mpeg4", "mpeg4",
                          note="AVI 老格式"),
    # --- hardware. Offered only when a real trial encode succeeds, which also
    # --- rules out builds whose NVENC headers are too old for the driver.
    "nvenc_h264": VideoEncoder(
        "nvenc_h264", "H.264 硬件加速 (NVIDIA NVENC)", "h264_nvenc", "h264",
        hardware=True,
        smoke=("-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "23",
               "-pix_fmt", "yuv420p"),
        note="走显卡、不占 CPU；同码率体积明显大于软编，通常也不更快"),
    "nvenc_h265": VideoEncoder(
        "nvenc_h265", "H.265 硬件加速 (NVIDIA NVENC)", "hevc_nvenc", "hevc",
        hardware=True,
        smoke=("-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "25",
               "-pix_fmt", "yuv420p"),
        note="走显卡 H.265；高分辨率重编码时比软编快，体积接近"),
    "nvenc_av1": VideoEncoder(
        "nvenc_av1", "AV1 硬件加速 (NVIDIA NVENC)", "av1_nvenc", "av1",
        hardware=True,
        smoke=("-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "30",
               "-pix_fmt", "yuv420p"),
        note="40 系显卡的 AV1 编码，体积小；老播放器支持一般"),
    "amf_h264": VideoEncoder("amf_h264", "H.264 硬件加速 (AMD AMF)", "h264_amf", "h264",
                             hardware=True,
                             smoke=("-quality", "balanced", "-rc", "cqp",
                                    "-qp_i", "22", "-qp_p", "24", "-qp_b", "26",
                                    "-pix_fmt", "yuv420p"),
                             note="走显卡、不占 CPU；同码率体积明显大于软编，速度不一定更快"),
    "amf_h265": VideoEncoder("amf_h265", "H.265 硬件加速 (AMD AMF)", "hevc_amf", "hevc",
                             hardware=True,
                             smoke=("-quality", "balanced", "-rc", "cqp",
                                    "-qp_i", "24", "-qp_p", "26", "-qp_b", "28",
                                    "-pix_fmt", "yuv420p"),
                             note="走显卡、不占 CPU；同码率体积明显大于软编，速度不一定更快"),
}

# AMF quality mode: (quality, qp_i, qp_p, qp_b)
_AMF_TIERS = {
    "high":     ("quality", 20, 22, 24),
    "balanced": ("balanced", 23, 25, 27),
    "small":    ("balanced", 27, 29, 31),
    "tiny":     ("speed", 31, 33, 35),
}

_SW_TIERS = {
    # encoder -> tier -> args
    "libx264": {
        "high":     ["-preset", "medium", "-crf", "20"],
        "balanced": ["-preset", "veryfast", "-crf", "23"],
        "small":    ["-preset", "veryfast", "-crf", "26"],
        "tiny":     ["-preset", "veryfast", "-crf", "30"],
    },
    "libx265": {
        "high":     ["-preset", "medium", "-crf", "24"],
        "balanced": ["-preset", "veryfast", "-crf", "28"],
        "small":    ["-preset", "veryfast", "-crf", "31"],
        "tiny":     ["-preset", "veryfast", "-crf", "34"],
    },
    "libvpx-vp9": {
        # -row-mt keeps VP9 from crawling; -cpu-used trades speed for size.
        "high":     ["-crf", "30", "-b:v", "0", "-row-mt", "1", "-cpu-used", "2"],
        "balanced": ["-crf", "33", "-b:v", "0", "-row-mt", "1", "-cpu-used", "3"],
        "small":    ["-crf", "36", "-b:v", "0", "-row-mt", "1", "-cpu-used", "4"],
        "tiny":     ["-crf", "40", "-b:v", "0", "-row-mt", "1", "-cpu-used", "5"],
    },
    "libvpx": {
        "high":     ["-crf", "6", "-b:v", "0", "-deadline", "good", "-cpu-used", "2"],
        "balanced": ["-crf", "10", "-b:v", "0", "-deadline", "good", "-cpu-used", "4"],
        "small":    ["-crf", "14", "-b:v", "0", "-deadline", "good", "-cpu-used", "5"],
        "tiny":     ["-crf", "18", "-b:v", "0", "-deadline", "good", "-cpu-used", "5"],
    },
    "mpeg4": {
        "high":     ["-q:v", "3"],
        "balanced": ["-q:v", "5"],
        "small":    ["-q:v", "7"],
        "tiny":     ["-q:v", "10"],
    },
    # NVENC uses p1-p7 presets from ffmpeg 4.4 onwards; the tuner plus a VBR
    # quality target is the closest thing to a CRF mode it offers.
    "h264_nvenc": {
        "high":     ["-preset", "p7", "-tune", "hq", "-rc", "vbr", "-cq", "19"],
        "balanced": ["-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "23"],
        "small":    ["-preset", "p4", "-tune", "hq", "-rc", "vbr", "-cq", "26"],
        "tiny":     ["-preset", "p3", "-tune", "hq", "-rc", "vbr", "-cq", "30"],
    },
    "hevc_nvenc": {
        "high":     ["-preset", "p7", "-tune", "hq", "-rc", "vbr", "-cq", "21"],
        "balanced": ["-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "25"],
        "small":    ["-preset", "p4", "-tune", "hq", "-rc", "vbr", "-cq", "28"],
        "tiny":     ["-preset", "p3", "-tune", "hq", "-rc", "vbr", "-cq", "32"],
    },
    "av1_nvenc": {
        "high":     ["-preset", "p7", "-tune", "hq", "-rc", "vbr", "-cq", "26"],
        "balanced": ["-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "30"],
        "small":    ["-preset", "p4", "-tune", "hq", "-rc", "vbr", "-cq", "34"],
        "tiny":     ["-preset", "p3", "-tune", "hq", "-rc", "vbr", "-cq", "38"],
    },
}

QUALITY_TIERS = [
    ("high", "高质量（体积较大）"),
    ("balanced", "均衡（推荐）"),
    ("small", "小体积"),
    ("tiny", "极小体积"),
]

# Resolve order for a quality tier: the encoder's own args, then the muxer.
TERMINAL_VIDEO_ARGS = ["-pix_fmt", "yuv420p"]


AMF_ENCODERS = ("h264_amf", "hevc_amf")


def video_quality_args(enc: VideoEncoder, tier: str) -> list[str]:
    """Quality arguments for one encoder.

    Dispatched on the ffmpeg encoder name, not on `hardware`: NVENC and AMF
    share nothing -- AMF takes `-rc cqp -qp_i/...`, NVENC takes `-preset p1-p7
    -tune -rc vbr -cq` and rejects the AMF spelling outright.
    """
    if enc.encoder in AMF_ENCODERS:
        mode, qp_i, qp_p, qp_b = _AMF_TIERS.get(tier, _AMF_TIERS["balanced"])
        return ["-quality", mode, "-rc", "cqp",
                "-qp_i", str(qp_i), "-qp_p", str(qp_p), "-qp_b", str(qp_b)]
    table = _SW_TIERS.get(enc.encoder, _SW_TIERS["libx264"])
    return list(table.get(tier, table["balanced"]))


# ------------------------------------------------------------------- presets

# Mirrors of the UI dropdowns, so headless use and the GUI agree.
VIDEO_MODES = [
    ("smart",    "智能（推荐）", "编码兼容就无损复制，不兼容才重编码；优先保证正常播放"),
    ("lossless", "强制无损复制（最快）", "坚决不重编码；必要时装进兼容性较差的组合，程序会提示"),
    ("high",     "高质量", "重编码，画质优先"),
    ("balanced", "均衡", "重编码，画质与体积平衡"),
    ("small",    "小体积", "重编码，压得更狠"),
    ("tiny",     "极小体积", "重编码，画质换体积"),
    ("target",   "指定体积", "两遍编码，尽量贴近目标大小"),
]

BITRATES_KBPS = [320, 256, 192, 160, 128, 96, 64, 48, 32]

SAMPLE_RATES = [48000, 44100, 32000, 24000, 22050, 16000, 12000, 8000]

LOUDNESS_PRESETS = [
    ("off", "不处理", 0.0, ""),
    ("douyin", "短视频平台 (-14 LUFS)", -14.0, "抖音/快手/小红书/视频号"),
    ("podcast", "播客/音频平台 (-16 LUFS)", -16.0, "苹果播客/小宇宙/喜马拉雅"),
    ("broadcast", "广播标准 (-23 LUFS)", -23.0, "EBU R128"),
    ("youtube", "YouTube (-14 LUFS)", -14.0, "油管/西瓜"),
]

IMAGE_CONTAINERS: dict[str, Container] = {
    "jpg":  Container("jpg", "image2", ("mjpeg",), (), "mjpeg", "", label="JPEG  · 最通用，不支持透明"),
    "png":  Container("png", "image2", ("png",), (), "png", "", label="PNG   · 无损，支持透明"),
    "webp": Container("webp", "webp", ("webp",), (), "webp", "", label="WebP  · 体积小，支持透明"),
    "bmp":  Container("bmp", "image2", ("bmp",), (), "bmp", "", label="BMP   · 无损未压缩，体积大"),
    "tiff": Container("tiff", "image2", ("tiff",), (), "tiff", "", label="TIFF  · 无损，印刷/归档"),
    "gif":  Container("gif", "gif", ("gif",), (), "gif", "", label="GIF   · 动图，256 色"),
}

ASPECT_PRESETS = [
    ("原始", 0, 0),
    ("1:1 方形", 1, 1),
    ("4:3", 4, 3),
    ("3:4", 3, 4),
    ("16:9", 16, 9),
    ("9:16 竖版", 9, 16),
    ("3:2", 3, 2),
    ("2:3", 2, 3),
    ("2.35:1 宽银幕", 235, 100),
]


# ------------------------------------------------------------------ options

@dataclass
class TrimSpec:
    start: float | None = None
    end: float | None = None
    precise: bool = False       # force re-encode so the cut is frame-accurate

    @property
    def active(self) -> bool:
        return self.start is not None or self.end is not None

    def duration(self, source_duration: float | None) -> float | None:
        if not self.active:
            return source_duration
        start = self.start or 0.0
        if self.end is not None:
            return max(0.0, self.end - start)
        if source_duration:
            return max(0.0, source_duration - start)
        return None

    def describe(self) -> str:
        if not self.active:
            return ""
        a = _sec_label(self.start or 0.0)
        b = _sec_label(self.end) if self.end is not None else "末尾"
        return f"{a} → {b}"


def _sec_label(sec: float | None) -> str:
    if sec is None:
        return "--"
    sec = max(0.0, sec)
    m, s = divmod(sec, 60)
    return f"{int(m)}:{s:04.1f}"


@dataclass
class GifSpec:
    fps: int = 12
    max_colors: int = 256
    dither: str = "bayer:bayer_scale=5:diff_mode=rectangle"
    loop: int = 0                    # 0 = infinite
    single_pass: bool = False


GIF_PRESETS = {
    "high":     GifSpec(fps=15, max_colors=256, dither="sierra2_4a"),
    "balanced": GifSpec(fps=12, max_colors=256, dither="bayer:bayer_scale=5:diff_mode=rectangle"),
    "small":    GifSpec(fps=10, max_colors=128, dither="bayer:bayer_scale=4:diff_mode=rectangle"),
    "tiny":     GifSpec(fps=8, max_colors=64, dither="bayer:bayer_scale=3:diff_mode=rectangle"),
}


@dataclass
class VideoOptions:
    container: str = "mp4"
    mode: str = "smart"
    encoder_key: str = "auto"
    target_mb: float = 20.0
    crop: object | None = None       # geometry.CropSpec
    scale: object | None = None      # geometry.ScaleSpec
    trim: TrimSpec = field(default_factory=TrimSpec)
    audio_codec: str = "auto"        # auto | copy | none | aac | mp3 | ...
    audio_bitrate: int = 128
    faststart: bool = True
    strip_metadata: bool = False
    gif: GifSpec = field(default_factory=GifSpec)
    container_override: str = ""      # the user explicitly changed the container


@dataclass
class ImageOptions:
    container: str = "webp"
    quality: int = 88                 # 1-100, lossy formats
    lossless: bool = False            # webp/png
    crop: object | None = None
    scale: object | None = None
    keep_metadata: bool = False
    animated: bool = True             # preserve animation for gif/webp in+out
    gif: GifSpec = field(default_factory=GifSpec)
    background: str = ""              # flatten transparency onto this colour


@dataclass
class AudioOptions:
    container: str = "mp3"
    bitrate: int = 192
    sample_rate: int = 0              # 0 = keep source
    channels: int = 0                 # 0 = keep source
    loudness: str = "off"
    trim: TrimSpec = field(default_factory=TrimSpec)
    copy_stream: bool = True          # allow lossless stream copy when possible


@dataclass
class ConcatOptions:
    container: str = "mp4"
    mode: str = "smart"
    encoder_key: str = "auto"
    audio_codec: str = "auto"
    audio_bitrate: int = 128
    faststart: bool = True


# Per-frame display time for a stills slideshow. Centisecond values, because a
# GIF stores delays in hundredths of a second and anything else drifts.
FRAME_DELAYS = [0.05, 0.1, 0.2, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0]


@dataclass
class SequenceOptions:
    """Many stills -> one animated GIF / WebP."""
    container: str = "gif"           # gif | webp
    delay: float = 0.5               # seconds each frame is shown
    loop: int = 0                    # 0 = forever
    max_colors: int = 256            # GIF palette size
    dither: str = "bayer:bayer_scale=5:diff_mode=rectangle"
    quality: int = 80                # animated WebP
    scale: object | None = None      # geometry.ScaleSpec
    crop: object | None = None       # geometry.CropSpec
