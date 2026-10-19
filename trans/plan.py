"""Turning user options into concrete ffmpeg invocations.

Design notes that matter on real files:

* Streams are decided independently. A MOV that is already H.264 gets its video
  copied into MP4 while only the audio is re-encoded -- the common phone-footage
  case, and it finishes in milliseconds.
* Every geometry value is a literal integer computed from the probed source
  size, so the size shown in the UI is the size that comes out.
* `-ss` before `-i` is both fast and, when re-encoding, frame-accurate. Only
  `-c copy` is forced to a keyframe, so precision there has to be paid for with
  a re-encode, and we say so rather than silently cutting at the wrong frame.
* Concatenation decides compatibility up front. The concat demuxer exits 0 even
  when stream parameters differ, and produces a file with garbage frame timing,
  so "it didn't fail" is not evidence that a copy-based merge is valid.
"""
from __future__ import annotations

import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import exif
from . import ff
from . import geometry as G
from .media import MediaInfo
from .presets import (AUDIO_CONTAINERS, CONTAINER_DEMUXER, GIF_PRESETS,
                      IMAGE_CONTAINERS, LOUDNESS_PRESETS, LOSSLESS_AUDIO,
                      VIDEO_AUDIO_CODECS, VIDEO_CONTAINERS, VIDEO_ENCODERS,
                      AudioOptions, ConcatOptions, Container, GifSpec,
                      ImageOptions, SequenceOptions, VideoEncoder, VideoOptions,
                      audio_bitrate_args, video_quality_args)

# ffmpeg encoder for each codec family, used when we must reproduce a source
# stream exactly (concatenation normalisation).
FAMILY_ENCODER = {
    "h264": "libx264", "hevc": "libx265", "vp9": "libvpx-vp9", "vp8": "libvpx",
    "mpeg4": "mpeg4", "mpeg2video": "mpeg2video", "av1": "libaom-av1",
    "aac": "aac", "mp3": "libmp3lame", "opus": "libopus", "vorbis": "libvorbis",
    "flac": "flac", "ac3": "ac3", "eac3": "eac3", "dts": "dca",
    "alac": "alac", "pcm_s16le": "pcm_s16le", "pcm_s24le": "pcm_s24le",
}


# --------------------------------------------------------------- job model

@dataclass
class Step:
    """A single ffmpeg process."""
    args: list[str] = field(default_factory=list)
    label: str = ""
    expected: float | None = None    # seconds of output, drives the progress bar
    writes_output: bool = False
    # Runs in the worker just before ffmpeg starts: creates the scratch
    # directories and writes the concat list. Deferring this keeps planning
    # side-effect free, which matters because the UI plans on every option
    # change to build its preview -- eager mkdtemp left a temp directory behind
    # for every preview of a job that was never run.
    prepare: object | None = None


@dataclass
class Job:
    out: Path
    kind: str = "video"              # video | image | audio
    src: Path | None = None
    steps: list[Step] = field(default_factory=list)
    note: str = ""
    warnings: list[str] = field(default_factory=list)
    temps: list[Path] = field(default_factory=list)
    expected: float | None = None

    @property
    def label(self) -> str:
        return self.src.name if self.src else self.out.name

    def cleanup(self) -> None:
        for t in self.temps:
            _rmtree(t)


def _rmtree(path: Path) -> None:
    try:
        if path.is_dir():
            for child in path.iterdir():
                try:
                    child.unlink()
                except OSError:
                    pass
            path.rmdir()
        else:
            path.unlink()
    except OSError:
        pass


# ------------------------------------------------------------------ helpers

def fmt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def unique_path(src: Path | None, out_dir: Path | None, ext: str,
                stem: str | None = None, taken: set | None = None) -> Path:
    """Never overwrite the source and never silently clobber an earlier output.

    `taken` carries the reservations of the batch being planned. Without it two
    jobs on the same source (different settings, or the same file added twice)
    would compute the same path -- they are planned before anything is written,
    so the filesystem alone cannot tell them apart.
    """
    directory = Path(out_dir) if out_dir else (src.parent if src else Path.cwd())
    base = stem or (src.stem if src else "output")

    def claim(candidate: Path) -> bool:
        if candidate.exists():
            return False
        if taken is not None:
            key = str(candidate).lower()
            if key in taken:
                return False
            taken.add(key)
        return True

    candidate = directory / f"{base}.{ext}"
    if claim(candidate):
        return candidate
    for n in range(1, 10000):
        candidate = directory / f"{base} ({n}).{ext}"
        if claim(candidate):
            return candidate
    return directory / f"{base}_{int(time.time())}.{ext}"


def _make_temp_path() -> Path:
    """A unique scratch path under the system temp folder, not yet created."""
    return Path(tempfile.gettempdir()) / f"trans_{uuid.uuid4().hex[:12]}"


def _ensure_dirs(*paths: Path):
    def prepare() -> None:
        for p in paths:
            p.mkdir(parents=True, exist_ok=True)
    return prepare


def _movflags(cont: Container, want: bool) -> list[str]:
    return ["-movflags", "+faststart"] if cont.faststart and want else []


def _hevc_tag(cont: Container, family: str) -> list[str]:
    return ["-tag:v", "hvc1"] if cont.hevc_tag and family == "hevc" else []


def _pick_sw_encoder(family: str) -> VideoEncoder:
    for enc in VIDEO_ENCODERS.values():
        if enc.family == family and not enc.hardware:
            return enc
    return VIDEO_ENCODERS["h264"]


# --------------------------------------------------- stream decision (video)

def _resolve_encoder(opts: VideoOptions, cont: Container, src_family: str,
                     must_encode: bool, allow_loose: bool = False
                     ) -> tuple[str, VideoEncoder | None]:
    """Returns ("copy" | "encode", encoder).

    `allow_loose` permits codecs the container accepts but players may not
    (VP9 or Opus inside MP4). That is a lossless, instant copy, so it is worth
    doing when asked -- but not silently, because the file may then fail to open
    in an editor.
    """
    if opts.encoder_key != "auto":
        enc = VIDEO_ENCODERS.get(opts.encoder_key)
        if enc is not None:
            # An explicit codec request that matches what the file already holds
            # should not cost a re-encode.
            if (enc.family == src_family and not must_encode
                    and opts.mode in ("smart", "lossless")):
                return "copy", enc
            return "encode", enc

    if opts.mode in ("smart", "lossless") and not must_encode:
        if src_family and src_family in cont.video:
            return "copy", None
        if allow_loose and src_family and src_family in cont.loose:
            return "copy", None

    return "encode", _pick_sw_encoder(cont.default_video)


def _audio_plan(info: MediaInfo, cont: Container, opts: VideoOptions,
                warnings: list[str], allow_loose: bool = False
                ) -> tuple[str, list[str]]:
    """Returns ("copy" | "encode" | "none", extra args)."""
    if not info.has_audio or opts.audio_codec == "none":
        return "none", ["-an"]

    src = info.acodec
    requested = opts.audio_codec
    accepted = cont.audio + (cont.loose if allow_loose else ())

    if requested == "auto":
        if src and src in accepted:
            return "copy", ["-c:a", "copy"]
        codec = cont.default_audio or "aac"
    elif requested == "copy":
        if src and src in accepted:
            return "copy", ["-c:a", "copy"]
        codec = cont.default_audio or "aac"
        warnings.append(f"{cont.ext} 不支持 {src or '未知'} 音频，已改为 {codec} 重编码")
    else:
        codec = requested
        if cont.audio and codec not in cont.audio:
            fallback = cont.default_audio or "aac"
            warnings.append(f"{cont.ext} 不支持 {codec} 音频，已改为 {fallback}")
            codec = fallback

    enc = VIDEO_AUDIO_CODECS.get(codec) or VIDEO_AUDIO_CODECS["aac"]
    rate = 48000 if codec == "opus" else info.sample_rate
    rate_args, _label = audio_bitrate_args(codec, opts.audio_bitrate, rate,
                                           info.channels, warnings)
    args = ["-c:a", enc[0], *rate_args]
    if codec == "opus":
        args += ["-ar", "48000"]
    return "encode", args


def _hms(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} 小时 {m} 分"
    if m:
        return f"{m} 分 {s} 秒"
    return f"{s} 秒"


def _loose_used(info: MediaInfo, cont: Container, vmode: str, amode: str) -> bool:
    """True when the copy only works because the container tolerates a codec
    that players and editors may not."""
    v = (vmode == "copy" and info.vcodec and info.vcodec not in cont.video
         and info.vcodec in cont.loose)
    a = (amode == "copy" and info.acodec and info.acodec not in cont.audio
         and info.acodec in cont.loose)
    return bool(v or a)


def _copy_lossless_hint(info: MediaInfo, cont: Container) -> str:
    """Name a container that could hold this file without re-encoding."""
    for key, other in VIDEO_CONTAINERS.items():
        if key == cont.ext or key == "gif":
            continue
        if info.vcodec and info.vcodec in other.video and (
                not info.has_audio or info.acodec in other.audio):
            return other.ext.upper()
    return ""


def _passthrough() -> list[str]:
    """Keep source frame timing instead of resampling it.

    Required whenever the input declares a millisecond timebase (screen
    recordings do): the header's r_frame_rate is 1000/1, so without this ffmpeg
    duplicates every frame up to 1000 per second.
    """
    try:
        return list(ff.detect().passthrough_args)
    except Exception:
        return []


# ------------------------------------------------------------ gif planning

def _default_gif_scale(src_w: int, src_h: int, limit: int = 480):
    """GIF has no interframe compression worth the name; 1080p makes for
    tens of megabytes, so an untouched scale gets a sane default."""
    if max(src_w, src_h) <= limit:
        return None
    return G.longest_side(src_w, src_h, limit)


def _movie_path(path: Path) -> str:
    """A path in the form the `movie` filter expects.

    It parses forward-slash paths and needs the drive colon escaped, otherwise
    the colon is read as an option separator.
    """
    return str(path).replace("\\", "/").replace(":", "\\:")


# ------------------------------------------------- image sequence planning

def plan_image_sequence(infos: list[MediaInfo], out_dir: Path | None,
                        opts: SequenceOptions, taken: set | None = None) -> Job:
    """Merge many stills into one animated GIF or WebP.

    Each picture becomes one frame, shown for `delay` seconds, in list order.

    Frame timing is pinned inside the filter graph (`setpts=N*<centiseconds>` on
    a 1/1000 grid) and the output frame rate is set to match. Two things were
    tried first and are wrong here: the `fps` filter resamples by timestamp and
    drops frames, and `-framerate` on each image input does not survive the
    concat filter. Only the combination below reproduces the requested delay
    exactly, and it also copes with images of differing sizes.
    """
    key = opts.container if opts.container in ("gif", "webp") else "gif"
    first = infos[0]
    out = unique_path(first.path, out_dir, key,
                      stem=f"{first.path.stem}_动画", taken=taken)
    job = Job(out=out, kind="image", src=first.path)

    if len(infos) == 1:
        job.warnings.append("合成动图至少要两张图片")
        return job
    if len(infos) > 300:
        job.warnings.append(f"{len(infos)} 张图片可能超出系统命令行长度限制")

    # ---- target size: from the first picture unless the user chose one
    iw, ih = first.width, first.height
    if opts.crop is not None and opts.crop.mode != "none":
        iw, ih = opts.crop.dims(iw, ih)
    auto_scaled = False
    if opts.scale is not None and opts.scale.mode != "none":
        ow, oh = opts.scale.dims(iw, ih)
    else:
        auto = _default_gif_scale(iw, ih, 480)
        if auto:
            ow, oh = auto
            auto_scaled = True
        else:
            ow, oh = iw, ih
    if auto_scaled:
        job.warnings.append(f"未指定尺寸，已自动缩到最长边 480（输出 {ow}x{oh}）")

    # ---- one scaled, identically-formatted stream per picture
    parts: list[str] = []
    for i, info in enumerate(infos):
        chain: list[str] = []
        if opts.crop is not None and opts.crop.mode != "none" and info.width:
            chain.append(G.crop_filter(opts.crop, info.width, info.height))
        chain.append(f"scale=w={ow}:h={oh}:flags=lanczos")
        chain += ["setsar=1", "format=rgba"]
        parts.append(f"[{i}:v]" + ",".join(chain) + f"[a{i}]")

    n = len(infos)
    joined = "".join(f"[a{i}]" for i in range(n))
    centis = max(1, int(round(opts.delay * 100)))
    # frame i at i*delay, on a 1/1000 s grid
    sequence = (f"{joined}concat=n={n}:v=1:a=0,settb=1/1000,setpts=N*{centis * 10}")

    if key == "gif":
        # split feeds one branch into palettegen for a single global palette and
        # the other into paletteuse. Checked against the write-palette-to-disk
        # route: identical pixels, one process instead of two.
        graph = (",".join(parts) + ";" + sequence + ",split[m][p2];"
                 f"[m]palettegen=max_colors={opts.max_colors}:stats_mode=diff[p];"
                 f"[p2][p]paletteuse=dither={opts.dither}[out]")
        extra = ["-loop", str(opts.loop)]
    else:
        graph = ",".join(parts) + ";" + sequence + "[out]"
        extra = ["-c:v", "libwebp_anim", "-quality", str(opts.quality),
                 "-loop", str(opts.loop)]

    args: list[str] = []
    for info in infos:
        args += ["-i", str(info.path)]
    args += ["-filter_complex", graph, "-map", "[out]",
             "-r", f"100/{centis}", *extra, "-f", key, str(out)]

    label = "合成动图"
    job.steps.append(Step(args=args, label=label, writes_output=True))
    job.note = (f"{n} 张图合成动图 · 每帧 {opts.delay:g} 秒 · "
                f"总时长 {n * opts.delay:.1f} 秒 · {ow}x{oh}")
    if key == "gif":
        job.note += f" · {opts.max_colors} 色"
    job.expected = n * opts.delay
    return job


def _has_movie_filter() -> bool:
    try:
        return ff.detect().has_filter("movie")
    except Exception:
        return False


def _plan_gif(info: MediaInfo, out_dir: Path | None, opts: VideoOptions, taken: set | None = None) -> Job:
    spec: GifSpec = opts.gif
    out = unique_path(info.path, out_dir, "gif", taken=taken)
    job = Job(out=out, kind="video", src=info.path)

    src_w, src_h = info.width, info.height
    chain: list[str] = []
    if opts.crop is not None and opts.crop.mode != "none":
        cw, ch = opts.crop.dims(src_w, src_h)
        chain.append(G.crop_filter(opts.crop, src_w, src_h))
        src_w, src_h = cw, ch

    if opts.scale is not None and opts.scale.mode != "none":
        ow, oh = opts.scale.dims(src_w, src_h)
        chain.append(G.scale_filter(ow, oh))
    else:
        auto = _default_gif_scale(src_w, src_h)
        if auto:
            ow, oh = auto
            chain.append(G.scale_filter(ow, oh))
            job.warnings.append(f"未指定尺寸，已自动缩到最长边 480px（输出 {ow}x{oh}）")
        else:
            ow, oh = src_w, src_h

    # The two passes need the range expressed differently.
    #
    # Palette pass: the limit must be an *input* option. palettegen emits its one
    # frame only at end of input, so an output-side limit cannot stop it and
    # ffmpeg decodes the whole rest of the file (9.3s of work for a 2-second GIF
    # out of a 6-minute recording).
    #
    # Write pass: the limit must be an *output* option. With two inputs feeding
    # paletteuse, an input-side limit makes the graph fail to finalise
    # ("Error marking filters as finished").
    seek: list[str] = []
    if opts.trim.start:
        seek = ["-ss", fmt_time(opts.trim.start)]
    dur = opts.trim.duration(info.duration)
    limit = ["-t", fmt_time(dur)] if dur else []

    # fps first: dropping frames before scaling is much cheaper than scaling
    # frames that are about to be thrown away.
    parts = [f"fps={spec.fps}", *chain]
    base = ",".join(parts)
    animated = info.kind == "video" or info.is_animated
    if not animated:
        # A single frame cannot go through the fps filter -- it emits nothing.
        base = ",".join(chain)

    if animated and not spec.single_pass:
        tmp = _make_temp_path()
        job.temps.append(tmp)
        pal = tmp / "palette.png"
        job.steps.append(Step(
            args=[*seek, *limit, "-i", str(info.path), "-vf",
                  f"{base},palettegen=max_colors={spec.max_colors}:stats_mode=diff",
                  str(pal)],
            label="生成调色板", expected=dur, prepare=_ensure_dirs(tmp)))

        # The palette is pulled in through the `movie` filter rather than as a
        # second demuxer input. With two inputs, reading one of these WebMs from
        # position 0 makes the filtergraph fail to finalise ("Error marking
        # filters as finished") -- no option combination fixes it, but a filter
        # source sidesteps the whole problem.
        if _has_movie_filter():
            graph = (f"movie='{_movie_path(pal)}'[p];[0:v]{base}[x];"
                     f"[x][p]paletteuse=dither={spec.dither}")
            step_args = [*seek, "-i", str(info.path), *limit,
                         "-filter_complex", graph, "-loop", str(spec.loop), str(out)]
        else:
            step_args = [*seek, "-i", str(info.path), "-i", str(pal), *limit, "-lavfi",
                         f"{base}[x];[x][1:v]paletteuse=dither={spec.dither}",
                         "-loop", str(spec.loop), str(out)]
        job.steps.append(Step(args=step_args, label="写入 GIF", expected=dur,
                              writes_output=True, prepare=_ensure_dirs(tmp)))
        job.note = f"两遍调色板 · {spec.fps}fps · {spec.max_colors} 色 · {ow}x{oh}"
    else:
        parts = ([base] if base else []) + ["split[a][b]"]
        graph = (",".join(parts) + f";[a]palettegen=max_colors={spec.max_colors}"
                 f":stats_mode=single[p];[b][p]paletteuse=dither={spec.dither}")
        job.steps.append(Step(
            args=[*seek, *limit, "-i", str(info.path), "-vf", graph, "-frames:v", "1",
                  "-loop", str(spec.loop), str(out)],
            label="写入 GIF", writes_output=True))
        job.note = f"单帧调色板 · {spec.max_colors} 色 · {ow}x{oh}"

    job.expected = dur
    return job


# ---------------------------------------------------------- video planning

def plan_video(info: MediaInfo, out_dir: Path | None, opts: VideoOptions, taken: set | None = None) -> Job:
    if opts.container == "gif":
        return _plan_gif(info, out_dir, opts, taken)

    cont = VIDEO_CONTAINERS.get(opts.container) or VIDEO_CONTAINERS["mp4"]
    out = unique_path(info.path, out_dir, cont.ext, taken=taken)
    job = Job(out=out, kind="video", src=info.path, warnings=[])
    warnings = job.warnings

    # ---- geometry (literal integers from the probed size)
    filters: list[str] = []
    geo = G.resolve(info.width, info.height, opts.crop, opts.scale)
    if geo.needs_crop and opts.crop is not None:
        filters.append(G.crop_filter(opts.crop, info.width, info.height))
    if geo.needs_scale:
        filters.append(G.scale_filter(geo.out_w, geo.out_h))

    trim = opts.trim
    geometry_change = bool(filters)
    # Anything that touches the picture or re-times it rules out a stream copy.
    must_encode = (geometry_change or trim.precise
                   or opts.mode in ("high", "balanced", "small", "tiny", "target"))
    # "强制无损复制" is the only mode allowed to trade player compatibility for
    # an instant, lossless result.
    allow_loose = opts.mode == "lossless"

    if trim.precise and not geometry_change:
        warnings.append("精确裁剪必须重编码，画面会再压缩一次")

    if info.declared_size and geometry_change:
        warnings.append(
            f"该文件头部声明 {info.declared_size[0]}x{info.declared_size[1]}，"
            f"实际画面却是 {info.width}x{info.height}；已按实际画面计算，"
            f"否则画面会被纵向拉伸")

    vmode, enc = _resolve_encoder(opts, cont, info.vcodec, must_encode, allow_loose)
    if opts.mode == "lossless" and vmode == "encode":
        warnings.append(f"{cont.ext} 装不下源文件原有的编码，已改为重编码")

    amode, aargs = _audio_plan(info, cont, opts, warnings, allow_loose)

    pre: list[str] = ["-ss", fmt_time(trim.start)] if trim.start else []
    post: list[str] = []
    if trim.active:
        dur = trim.duration(info.duration)
        if dur:
            post = ["-t", fmt_time(dur)]
    job.expected = trim.duration(info.duration)

    family = info.vcodec
    if vmode == "copy":
        video_args = ["-c:v", "copy"]
        mode_note = "无损复制（切点对齐关键帧）" if (trim.active and not trim.precise) else "无损复制"
    else:
        if enc is None:
            enc = _pick_sw_encoder(cont.default_video)
        family = enc.family
        tier = "balanced" if opts.mode in ("smart", "lossless") else opts.mode
        video_args = ["-c:v", enc.encoder, *video_quality_args(enc, tier),
                      "-pix_fmt", "yuv420p"]
        # Only when the header's rate is unusable: otherwise leave normal
        # constant-rate sources exactly as they were.
        if info.vfr or not info.fps:
            video_args += _passthrough()
        mode_note = f"{enc.label} 重编码"

    if vmode == "copy" and amode == "copy":
        if _loose_used(info, cont, vmode, amode):
            warnings.append(
                f"无损复制：{info.vcodec}+{info.acodec} 直接装进 {cont.ext.upper()}，"
                f"瞬时可完成、画质零损失。但这种组合 QuickTime、剪映、Premiere 等"
                f"可能不认，要最大兼容请把画质改成「均衡」")
    elif vmode == "encode" and job.expected:
        warnings.append(
            f"需要重新编码约 {_hms(job.expected)} 的内容，耗时取决于素材和机器；"
            f"运行时下方会显示实时速度与剩余时间")
        alt = _copy_lossless_hint(info, cont)
        if alt:
            warnings.append(
                f"如果只是要换封装、不需要 MP4 这种具体格式，输出格式选 {alt} "
                f"可以直接无损复制，几秒就能完成")

    if opts.mode == "target" and vmode == "encode" and job.expected:
        return _plan_target_size(info, out, cont, opts, enc, amode, aargs,
                                filters, pre, post, job)

    # No `-copyts` here. With it, `-t` is measured against the *original*
    # timeline while the seek has already moved the output to a later point, so
    # asking for 1.0s-2.5s of a 5s clip yielded 0.53s, and cutting after 3.0s
    # yielded a file with no streams at all. `-ss` before `-i` already rebases
    # the output to zero, which is what makes `-t` mean "this much after the
    # cut" -- measured on 6 ranges, 0.500/1.000/2.000s now match to 17ms.
    tail: list[str] = []
    if opts.strip_metadata:
        tail += ["-map_metadata", "-1"]
    tail += _hevc_tag(cont, family)
    tail += _movflags(cont, opts.faststart)

    args = [*pre, "-i", str(info.path), "-map", "0:v:0?"]
    if amode != "none":
        args += ["-map", "0:a:0?"]
    if filters:
        args += ["-vf", ",".join(filters)]
    args += [*video_args, *aargs, *post, *tail, "-f", cont.muxer, str(out)]

    job.steps.append(Step(args=args, label=mode_note, expected=job.expected,
                          writes_output=True))
    bits = [mode_note]
    if filters:
        bits.append(geo.label())
    if amode == "copy":
        bits.append("音频复制")
    elif amode == "none":
        bits.append("无音频")
    if trim.active:
        bits.append("裁剪 " + trim.describe())
    job.note = " · ".join(bits)
    return job


def _plan_target_size(info: MediaInfo, out: Path, cont: Container, opts: VideoOptions,
                      enc: VideoEncoder | None, amode: str, aargs: list[str],
                      filters: list[str], pre: list[str], post: list[str],
                      job: Job) -> Job:
    duration = opts.trim.duration(info.duration) or 0.0
    if enc is None:
        enc = _pick_sw_encoder("h265" if cont.default_video == "h265" else "h264")

    total_kbits = opts.target_mb * 8192
    audio_kbps = 0 if amode == "none" else opts.audio_bitrate
    # Container overhead gets 2%.
    video_kbps = max(60, int(total_kbits * 0.98 / duration - audio_kbps))
    job.warnings.append(f"目标 {opts.target_mb:g}MB / {duration:.1f}s → "
                        f"视频码率约 {video_kbps}kbps（音频 {audio_kbps}kbps）")

    two_pass = enc.encoder in ("libx264", "libx265")
    if not two_pass:
        # Honour the encoder the user picked and fall back to single-pass: a
        # weak CPU may be exactly why they chose the GPU. Swapping in libx264
        # behind their back to get two-pass would silently ignore that choice.
        job.warnings.append(f"{enc.label} 没有可靠的两遍编码，改用单遍平均码率，"
                            f"实际体积可能偏离目标")

    tmp = _make_temp_path()
    job.temps.append(tmp)
    log = tmp / "pass"

    if two_pass:
        p1 = [*pre, "-i", str(info.path), "-map", "0:v:0?", "-an",
              "-c:v", enc.encoder, "-preset", "veryfast", "-b:v", f"{video_kbps}k"]
        if filters:
            p1 += ["-vf", ",".join(filters)]
        p1 += ["-pass", "1", "-passlogfile", str(log), "-f", "null", os.devnull]
        job.steps.append(Step(args=p1, label="第一遍分析", expected=duration,
                              prepare=_ensure_dirs(tmp)))

    p2 = [*pre, "-i", str(info.path), "-map", "0:v:0?"]
    if amode != "none":
        p2 += ["-map", "0:a:0?"]
    if filters:
        p2 += ["-vf", ",".join(filters)]
    p2 += ["-c:v", enc.encoder, "-b:v", f"{video_kbps}k", "-pix_fmt", "yuv420p"]
    if enc.encoder in ("libx264", "libx265"):
        p2 += ["-preset", "veryfast"]
    elif enc.encoder == "libvpx-vp9":
        p2 += ["-row-mt", "1", "-cpu-used", "3"]
    if two_pass:
        p2 += ["-pass", "2", "-passlogfile", str(log)]
    p2 += [*aargs, *post]
    p2 += _hevc_tag(cont, enc.family)
    p2 += _movflags(cont, opts.faststart)
    p2 += ["-f", cont.muxer, str(out)]
    job.steps.append(Step(args=p2, label="第二遍写入" if two_pass else "写入",
                          expected=duration, writes_output=True,
                          prepare=_ensure_dirs(tmp)))
    job.note = f"目标 {opts.target_mb:g}MB · {enc.label} · {video_kbps}kbps"
    return job


# ---------------------------------------------------------- image planning

def _transposed_dims(info: MediaInfo) -> tuple[str, int, int, int]:
    """Returns (filter, orientation, width, height) with orientation applied.

    The filter is empty when the decoder has already turned the picture upright
    -- newer ffmpeg builds do, older ones ignore the tag -- so applying our own
    transpose on top would rotate it twice.
    """
    if info.exif_rotated:
        return "", 1, info.width, info.height
    if info.path.suffix.lower() not in (".jpg", ".jpeg", ".jfif", ".tif", ".tiff"):
        return "", 1, info.width, info.height
    value = exif.orientation(info.path)
    filt = exif.ORIENTATION_FILTERS.get(value, "")
    w, h = info.width, info.height
    if filt in ("transpose=1", "transpose=2"):
        w, h = h, w
    return filt, value, w, h


def _has_alpha(pix_fmt: str) -> bool:
    pf = (pix_fmt or "").lower()
    return any(tok in pf for tok in ("rgba", "bgra", "argb", "abgr", "yuva",
                                     "ya8", "pal8", "gbrap"))


def _image_quality_args(key: str, opts: ImageOptions) -> list[str]:
    q = max(1, min(100, opts.quality))
    if key == "jpg":
        # -q:v is inverted: 2 is best, 31 is worst.
        qv = int(round(2 + (100 - q) * 29 / 100.0))
        return ["-q:v", str(max(2, min(31, qv))), "-pix_fmt", "yuvj420p"]
    if key == "webp":
        if opts.lossless:
            return ["-c:v", "libwebp", "-lossless", "1", "-compression_level", "6"]
        return ["-c:v", "libwebp", "-quality", str(q), "-compression_level", "5"]
    if key == "png":
        # PNG is lossless; the slider buys compression effort, not quality.
        return ["-compression_level", str(max(0, min(9, int(round(q / 100.0 * 9)))))]
    if key == "tiff":
        return ["-compression_algo", "lzw"]
    return []


def plan_image(info: MediaInfo, out_dir: Path | None, opts: ImageOptions, taken: set | None = None) -> Job:
    cont = IMAGE_CONTAINERS.get(opts.container) or IMAGE_CONTAINERS["webp"]
    key = cont.ext
    out = unique_path(info.path, out_dir, cont.ext, taken=taken)
    job = Job(out=out, kind="image", src=info.path)

    transpose, ori, src_w, src_h = _transposed_dims(info)
    if transpose:
        job.warnings.append(f"已按 EXIF 方向自动旋正（{exif.describe(ori)}）")
    elif info.exif_rotated:
        job.warnings.append("该照片带 EXIF 方向，已由 ffmpeg 自动旋正")

    geo = G.resolve(src_w, src_h, opts.crop, opts.scale)
    filters: list[str] = []
    if transpose:
        filters.extend(transpose.split(","))
    if geo.needs_crop and opts.crop is not None:
        filters.append(G.crop_filter(opts.crop, src_w, src_h))
    if geo.needs_scale:
        filters.append(G.scale_filter(geo.out_w, geo.out_h))

    out_w = geo.out_w or src_w
    out_h = geo.out_h or src_h
    keep_animation = opts.animated and info.is_animated and key in ("gif", "webp")

    # A GIF at full source resolution is unusable -- a 8256x5504 scan turns into
    # a hundred-megabyte file -- so an untouched size gets the same sane cap the
    # video-to-GIF path uses.
    if key == "gif" and not geo.needs_scale:
        auto = _default_gif_scale(src_w, src_h)
        if auto:
            out_w, out_h = auto
            filters.append(G.scale_filter(out_w, out_h))
            geo = G.Geometry(crop_w=geo.crop_w, crop_h=geo.crop_h,
                             out_w=out_w, out_h=out_h)
            job.warnings.append(
                f"未指定尺寸，已自动缩到最长边 480（输出 {out_w}x{out_h}）")

    if info.declared_size and (geo.needs_crop or geo.needs_scale):
        job.warnings.append(
            f"该文件头部声明 {info.declared_size[0]}x{info.declared_size[1]}，"
            f"实际画面却是 {info.width}x{info.height}；已按实际画面计算")

    # ---- GIF
    if key == "gif":
        spec = GIF_PRESETS["balanced"]
        colors = max(8, min(256, int(round(opts.quality / 100.0 * 256))))
        if keep_animation:
            base = ",".join([f"fps={spec.fps}", *filters])
            tmp = _make_temp_path()
            job.temps.append(tmp)
            pal = tmp / "palette.png"
            job.steps.append(Step(
                args=["-i", str(info.path), "-vf",
                      f"{base},palettegen=max_colors={colors}:stats_mode=diff", str(pal)],
                label="生成调色板", prepare=_ensure_dirs(tmp)))
            if _has_movie_filter():
                graph = (f"movie='{_movie_path(pal)}'[p];[0:v]{base}[x];"
                         f"[x][p]paletteuse=dither={spec.dither}")
                step_args = ["-i", str(info.path), "-filter_complex", graph,
                             "-loop", "0", str(out)]
            else:
                step_args = ["-i", str(info.path), "-i", str(pal), "-lavfi",
                             f"{base}[x];[x][1:v]paletteuse=dither={spec.dither}",
                             "-loop", "0", str(out)]
            job.steps.append(Step(args=step_args, label="写入 GIF", writes_output=True,
                                  prepare=_ensure_dirs(tmp)))
            job.note = f"GIF 动图 · {spec.fps}fps · {colors} 色 · {out_w}x{out_h}"
        else:
            # No fps filter for a single frame: fed one frame, the fps filter
            # emits nothing at all and ffmpeg writes a zero-byte GIF. A one-frame
            # GIF has no frame rate to set anyway.
            chain = [*filters, "split[a][b]"]
            graph = (",".join(chain) + f";[a]palettegen=max_colors={colors}"
                     f":stats_mode=single[p];"
                     f"[b][p]paletteuse=dither={spec.dither}")
            job.steps.append(Step(
                args=["-i", str(info.path), "-vf", graph, "-frames:v", "1",
                      "-loop", "0", str(out)],
                label="写入 GIF", writes_output=True))
            job.note = f"GIF 静图 · {colors} 色 · {out_w}x{out_h}"
        return job

    # ---- animated WebP
    if key == "webp" and keep_animation:
        args = ["-i", str(info.path)]
        if filters:
            args += ["-vf", ",".join(filters)]
        args += ["-c:v", "libwebp_anim", "-quality", str(max(1, min(100, opts.quality))),
                 "-loop", "0", "-f", "webp", str(out)]
        job.steps.append(Step(args=args, label="写入动图 WebP", writes_output=True))
        job.note = f"动图 WebP · {out_w}x{out_h}"
        return job

    src_alpha = _has_alpha(info.pix_fmt)
    flatten = key == "jpg" and src_alpha
    bg = opts.background or "white"

    args = ["-i", str(info.path), *_image_quality_args(key, opts)]
    if flatten:
        # JPEG has no alpha channel; composite onto a solid colour instead of
        # letting ffmpeg drop the channel onto whatever it defaults to.
        # overlay's format option only accepts "rgb" on ffmpeg 4.x ("rgb24" is
        # a later addition and is rejected outright by 4.3).
        chain = ",".join(filters) if filters else "null"
        lavfi = (f"[0:v]{chain}[fg];color=c={bg}:s={out_w}x{out_h}[bg];"
                 f"[bg][fg]overlay=format=rgb:shortest=1[out]")
        args += ["-filter_complex", lavfi, "-map", "[out]", "-frames:v", "1"]
        job.warnings.append("JPEG 不支持透明，透明区域已填充白色")
    else:
        if filters:
            args += ["-vf", ",".join(filters)]
        if not keep_animation:
            args += ["-frames:v", "1"]

    if opts.keep_metadata and not transpose:
        args += ["-map_metadata", "0"]
    else:
        if opts.keep_metadata and transpose:
            job.warnings.append("已旋正，为免方向被重复应用而未保留元数据")
        args += ["-map_metadata", "-1"]

    if cont.muxer != "image2":
        args += ["-f", cont.muxer]
    args.append(str(out))

    job.steps.append(Step(args=args, label="转换", writes_output=True))
    bits = [f"{out_w}x{out_h}"]
    if geo.needs_crop:
        bits.insert(0, f"裁剪 {geo.crop_w}x{geo.crop_h}")
    if key == "jpg":
        bits.append(f"品质 {opts.quality}")
    job.note = " · ".join(bits)
    return job


# ---------------------------------------------------------- audio planning

def plan_audio(info: MediaInfo, out_dir: Path | None, opts: AudioOptions, taken: set | None = None) -> Job:
    cont = AUDIO_CONTAINERS.get(opts.container) or AUDIO_CONTAINERS["mp3"]
    out = unique_path(info.path, out_dir, cont.ext, taken=taken)
    job = Job(out=out, kind="audio", src=info.path)
    warnings = job.warnings

    codec = cont.default_audio
    trim = opts.trim
    job.expected = trim.duration(info.duration)

    filters: list[str] = []
    if opts.loudness and opts.loudness != "off":
        target = next((p[2] for p in LOUDNESS_PRESETS if p[0] == opts.loudness), None)
        if target:
            filters.append(f"loudnorm=I={target:g}:TP=-1.0:LRA=11")
        else:
            warnings.append("未知的响度目标，已跳过")

    sample_rate = opts.sample_rate
    if codec == "opus":
        if sample_rate and sample_rate != 48000:
            warnings.append("Opus 只支持 48kHz，采样率已按 48000 处理")
        sample_rate = 48000

    channels = opts.channels
    can_copy = (opts.copy_stream and not filters and not trim.active
                and bool(info.acodec) and info.acodec == codec
                and (not sample_rate or sample_rate == info.sample_rate)
                and (not channels or channels == info.channels))

    pre = ["-ss", fmt_time(trim.start)] if trim.start else []
    post: list[str] = []
    if trim.active:
        dur = trim.duration(info.duration)
        if dur:
            post = ["-t", fmt_time(dur)]

    if can_copy:
        args = [*pre, "-i", str(info.path), "-vn", "-map", "0:a:0?", "-c:a", "copy",
                *post, "-f", cont.muxer, str(out)]
        job.steps.append(Step(args=args, label="无损提取", expected=job.expected,
                              writes_output=True))
        job.note = f"无损提取 {codec.upper()} 音轨 · 不重编码"
        return job

    enc_name = FAMILY_ENCODER.get(codec, codec)
    # The bitrate the user picked may exceed what this codec can carry at the
    # chosen sample rate and channel count; the helper clamps and says so.
    rate_args, rate_label = audio_bitrate_args(
        codec, opts.bitrate, sample_rate or info.sample_rate,
        channels or info.channels, warnings)
    args = [*pre, "-i", str(info.path), "-vn", "-map", "0:a:0?", "-c:a", enc_name]
    args += rate_args
    if filters:
        args += ["-af", ",".join(filters)]
    if sample_rate:
        args += ["-ar", str(sample_rate)]
    if channels:
        args += ["-ac", str(channels)]
    args += post
    args += _movflags(cont, True)
    args += ["-f", cont.muxer, str(out)]

    job.steps.append(Step(args=args, label=f"{cont.ext.upper()} 编码",
                          expected=job.expected, writes_output=True))

    bits = [codec.upper()]
    if rate_label:
        bits.append(rate_label)
    if sample_rate:
        bits.append(f"{sample_rate/1000:g}kHz")
    if channels:
        bits.append({1: "单声道", 2: "立体声"}.get(channels, f"{channels}声道"))
    if filters:
        bits.append("响度标准化")
    if trim.active:
        bits.insert(0, "裁剪 " + trim.describe())
    job.note = " · ".join(bits)
    return job


# --------------------------------------------------------- concat planning

_COMPARE_KEYS = ("vcodec", "width", "height", "pix_fmt", "acodec",
                 "sample_rate", "channels", "has_audio")


def _concat_spec(info: MediaInfo) -> dict:
    return {
        "vcodec": info.vcodec, "width": info.width, "height": info.height,
        "fps": round(info.fps, 3), "pix_fmt": info.pix_fmt,
        "profile": (info.video_stream.profile if info.video_stream else ""),
        "acodec": info.acodec, "sample_rate": info.sample_rate,
        "channels": info.channels, "has_audio": info.has_audio,
    }


def _compatible(a: dict, b: dict) -> bool:
    for k in _COMPARE_KEYS:
        if a.get(k) != b.get(k):
            return False
    return abs((a.get("fps") or 0) - (b.get("fps") or 0)) <= 0.01


def _demuxer_of(info: MediaInfo) -> str:
    """Which demuxer ffmpeg will read this file back with.

    ffprobe reports it as the leading name of the format list ("mov,mp4,m4a,..."
    for the whole QuickTime family), which is exactly the granularity that
    matters for concatenation.
    """
    name = (info.container or "").split(",")[0].strip().lower()
    if name:
        return name
    cont = VIDEO_CONTAINERS.get(info.path.suffix.lower().lstrip("."))
    return CONTAINER_DEMUXER.get(cont.ext, "") if cont is not None else ""


def _timescale_of(info: MediaInfo) -> int:
    """Video ticks per second, or 0 when the time base is not a plain 1/N.

    Used to pin a copied clip's time base to the reference's: the concat demuxer
    needs every entry to share one, and a stream copy otherwise keeps whatever
    the source had.
    """
    tb = (info.time_base or "").strip()
    num, _, den = tb.partition("/")
    if num == "1" and den.isdigit() and int(den) > 0:
        return int(den)
    return 0


def _write_concat_list(paths: list[Path], target: Path) -> None:
    lines = []
    for p in paths:
        # Absolute, forward slashes, escaped quotes. Absolute because the concat
        # demuxer resolves relative entries against the list file's own folder
        # (ffmpeg 9 does this; the old build happened to use the working
        # directory), and forward slashes because backslashes are escapes.
        s = str(Path(p).resolve()).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{s}'")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _normalize_args(info: MediaInfo, spec: dict, muxer: str, out: Path,
                    v_encoder: str, v_args: list[str], a_encoder: str,
                    add_silence: bool, rewrap: bool = False,
                    extra_out: list[str] | None = None) -> list[str]:
    """Re-encode one clip so it matches `spec` exactly.

    `rewrap` copies the streams into a new container instead. That is all a clip
    needs when its parameters already match and only its wrapper or time base
    differs -- which the merge path has to fix, because a concat list whose
    entries disagree loses the offsets. Copying keeps that lossless and instant
    where re-encoding would cost quality and minutes.
    """
    extra = list(extra_out or [])
    if rewrap:
        return ["-i", str(info.path), "-map", "0:v:0?", "-map", "0:a:0?",
                "-c", "copy", *extra, "-f", muxer, str(out)]

    args = ["-i", str(info.path)]
    if add_silence:
        dur = info.duration or 1.0
        layout = "mono" if (spec.get("channels") or 2) == 1 else "stereo"
        rate = spec.get("sample_rate") or 44100
        args += ["-f", "lavfi", "-t", fmt_time(dur), "-i",
                 f"anullsrc=r={rate}:cl={layout}"]

    args += ["-map", "0:v:0?"]
    args += ["-map", "1:a:0?" if add_silence else "0:a:0?"]

    if spec.get("width") and spec.get("height"):
        args += ["-vf", f"scale=w={spec['width']}:h={spec['height']}:flags=bicubic"]
    args += ["-c:v", v_encoder, *v_args, "-pix_fmt", "yuv420p"]
    if spec.get("fps"):
        args += ["-r", f"{spec['fps']:g}"]
    else:
        # No usable rate in the header, so pin the output to the source timing.
        # Without this the encoder honours the bogus 1000/1 tbr and duplicates
        # every frame to reach it.
        args += _passthrough()

    args += ["-c:a", a_encoder]
    if a_encoder not in ("flac", "pcm_s16le", "pcm_s24le", "alac"):
        args += ["-b:a", "192k"]
    if spec.get("sample_rate"):
        args += ["-ar", str(spec["sample_rate"])]
    if spec.get("channels"):
        args += ["-ac", str(spec["channels"])]
    if a_encoder == "libopus":
        args += ["-ar", "48000"]
    if add_silence:
        args += ["-shortest"]
    args += extra
    args += ["-f", muxer, str(out)]
    return args


def plan_concat(infos: list[MediaInfo], out_dir: Path | None,
                opts: ConcatOptions, taken: set | None = None) -> Job:
    cont = VIDEO_CONTAINERS.get(opts.container) or VIDEO_CONTAINERS["mp4"]
    ref_info = infos[0]
    ref = _concat_spec(ref_info)
    out = unique_path(ref_info.path, out_dir, cont.ext,
                      stem=f"{ref_info.path.stem}_合并", taken=taken)
    job = Job(out=out, kind="video", src=None)
    job.expected = sum(i.duration or 0 for i in infos) or None

    if len(infos) < 2:
        job.warnings.append("至少需要两个文件才能拼接")
        return job

    tmp = _make_temp_path()
    job.temps.append(tmp)

    container_ok = (bool(ref["vcodec"]) and ref["vcodec"] in cont.video
                    and (not ref["has_audio"] or ref["acodec"] in cont.audio))
    # Legal in this container but not universally playable.
    loose_ok = (bool(ref["vcodec"]) and ref["vcodec"] in cont.video + cont.loose
                and (not ref["has_audio"] or ref["acodec"] in cont.audio + cont.loose))
    allow_loose = opts.mode == "lossless"
    use_copy = container_ok or (loose_ok and allow_loose)

    if not ref["has_audio"] and any(i.has_audio for i in infos):
        job.warnings.append("第一个文件没有音轨，输出将不含音频；"
                            "要保留音频请把带音频的文件排在第一位")

    if use_copy and not container_ok:
        job.warnings.append(
            f"无损拼接：{ref['vcodec']}+{ref['acodec']} 直接装进 "
            f"{cont.ext.upper()}，几秒即可完成、画质零损失。但这种组合在 "
            f"QuickTime、剪映、Premiere 里可能无法识别")
    elif not use_copy:
        total = sum(i.duration or 0 for i in infos)
        alternative = ""
        for key, other in VIDEO_CONTAINERS.items():
            if key in (cont.ext, "gif"):
                continue
            if (ref["vcodec"] in other.video
                    and (not ref["has_audio"] or ref["acodec"] in other.audio)):
                alternative = other.ext.upper()
                break
        if alternative:
            job.warnings.append(
                f"这些片段的编码是 {ref['vcodec']}+{ref['acodec']}，"
                f"{cont.ext.upper()} 无法直接装下，所以要整段重新编码"
                + (f"，约 {_hms(total)} 的内容可能需要几分钟到十几分钟" if total else "")
                + f"。如果只是想把它们合成一个文件，输出格式选 {alternative} 可以"
                  f"无损复制、几秒完成")
        else:
            alt_cmd = ""
            for key, other in VIDEO_CONTAINERS.items():
                if key != cont.ext and key != "gif" and ref["vcodec"] in other.video + other.loose:
                    alt_cmd = other.ext.upper()
                    break
            job.warnings.append(
                f"{cont.ext.upper()} 装不下源文件的编码，全部片段将重新编码"
                + (f"（约 {_hms(total)} 的内容，可能需要几分钟）" if total else "")
                + (f"。画质改为「强制无损复制」也可以不改编码直接拼接，"
                   f"但兼容性会下降" if loose_ok else ""))

    # What every clip must end up looking like.
    if use_copy:
        spec = ref
        v_family = ref["vcodec"]
        a_family = ref["acodec"]
    else:
        spec = dict(ref)
        v_family = cont.default_video
        a_family = cont.default_audio or "aac"
        spec["vcodec"] = v_family
        spec["acodec"] = a_family
        spec["pix_fmt"] = "yuv420p"

    v_encoder = FAMILY_ENCODER.get(v_family, "libx264")
    v_args = (["-preset", "veryfast", "-crf", "20"] if v_encoder == "libx264"
              else ["-preset", "veryfast", "-crf", "24"] if v_encoder == "libx265"
              else ["-row-mt", "1", "-cpu-used", "4", "-crf", "32", "-b:v", "0"]
              if v_encoder == "libvpx-vp9" else [])
    a_encoder = FAMILY_ENCODER.get(a_family, "aac")

    # Reference with audio means silent clips get filled in; reference without
    # audio means everyone else's audio is dropped so the streams line up.
    add_silence = bool(ref["has_audio"])

    # ---- make the concat list homogeneous
    #
    # Every entry handed to the concat demuxer has to agree on the codecs, the
    # demuxer and the time base. It is not forgiving when they do not: measured
    # on ffmpeg 9.0.2, an MP4 followed by an MKV produced a merge reporting half
    # its length with both clips sitting on top of each other; an MKV followed by
    # an MP4 turned two 2-second clips into a 195-second file; and an MP4
    # followed by a TS holding the same H.264 turned 40 seconds into 234,
    # because the stream copy carried the TS's 1/90000 time base against the
    # MP4's 1/15360.
    #
    # So each clip is brought into line, and the output container is the scratch
    # container -- which is what lets the rebuilt files agree with the clips that
    # need no work at all:
    #   * parameters already match, only the wrapper or time base differs
    #     -> copy the streams across (lossless, instant)
    #   * anything else -> re-encode
    out_family = CONTAINER_DEMUXER.get(cont.ext, "")
    common_scale = _timescale_of(ref_info) if out_family == "mov" else 1000
    # Only the QuickTime family takes an explicit timescale; Matroska always
    # writes 1/1000, and the rest fix their own from the stream.
    scale_args = (["-video_track_timescale", str(common_scale)]
                  if out_family == "mov" and common_scale else [])

    def _tb_matches(info: MediaInfo) -> bool:
        if out_family == "mov":
            # Nothing can be pinned without a plain 1/N reference time base, so
            # every clip gets rebuilt and they all end up on the muxer's scale.
            return bool(common_scale) and _timescale_of(info) == common_scale
        return True

    wanted: list[bool] = []
    rewrap: list[bool] = []
    for info in infos:
        reencode = not _compatible(_concat_spec(info), spec)
        if not reencode and not ref["has_audio"] and info.has_audio:
            reencode = True
        copied = False
        if not reencode:
            # Same streams, but the wrapper or the time base has to change.
            if (_demuxer_of(info) != out_family or not _tb_matches(info)):
                copied = True
        wanted.append(reencode or copied)
        rewrap.append(copied)

    ordered: list[Path] = []
    rebuilt = 0
    rewrapped = 0
    for idx, info in enumerate(infos):
        if wanted[idx]:
            target = tmp / f"norm_{idx:03d}.{cont.ext}"
            job.steps.append(Step(
                args=_normalize_args(info, spec, cont.muxer, target, v_encoder,
                                     v_args, a_encoder,
                                     add_silence and not info.has_audio,
                                     rewrap=rewrap[idx],
                                     extra_out=scale_args),
                label=(f"统一封装 {idx + 1}/{len(infos)}" if rewrap[idx]
                       else f"统一格式 {idx + 1}/{len(infos)}"),
                expected=info.duration, prepare=_ensure_dirs(tmp)))
            ordered.append(target)
            if rewrap[idx]:
                rewrapped += 1
            else:
                rebuilt += 1
        else:
            ordered.append(info.path)

    if rebuilt and rewrapped:
        job.note = (f"已重编码 {rebuilt} 个片段、为对齐封装复制 {rewrapped} 个 / "
                    f"共 {len(infos)} 个")
    elif rebuilt:
        job.note = f"已统一 {rebuilt} / {len(infos)} 个片段的编码参数"
    elif rewrapped:
        job.note = f"为对齐封装复制了 {rewrapped} 个片段（画面未重新编码）"
    else:
        job.note = "参数完全一致，直接无损拼接"

    list_file = tmp / "concat.txt"
    args = ["-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy"]
    args += _hevc_tag(cont, v_family)
    args += _movflags(cont, opts.faststart)
    args += ["-f", cont.muxer, str(out)]

    def prepare_concat() -> None:
        tmp.mkdir(parents=True, exist_ok=True)
        _write_concat_list(ordered, list_file)

    job.steps.append(Step(args=args, label="拼接", expected=job.expected,
                          writes_output=True, prepare=prepare_concat))
    return job
