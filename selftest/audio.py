"""Audio tab sweep.

Loudness is measured on the produced file rather than assumed from the filter:
`loudnorm` is a two-stage filter and it is easy to ask for -14 LUFS and get
something else.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import (CAPS, CORPUS, FFMPEG, Out, begin, check, cleanup,  # noqa: E402
                     info_of, run_job, run_plan_direct, short_clip, summary,
                     temp_dir)

from trans import plan  # noqa: E402
from trans.presets import AUDIO_CONTAINERS, AudioOptions, TrimSpec  # noqa: E402

AUD = CORPUS / "audio"
VID = CORPUS / "video"
WANT_CODEC = {"mp3": "mp3", "m4a": "aac", "wav": "pcm_s16le", "aac": "aac",
              "flac": "flac", "ogg": "vorbis", "opus": "opus"}


def loudness(path: Path) -> float | None:
    """Integrated loudness of a file, in LUFS, measured by ffmpeg itself."""
    rc, out, err = __import__("trans.ff", fromlist=["x"]).run(
        FFMPEG, ["-hide_banner", "-nostats", "-i", str(path), "-af",
                 "loudnorm=print_format=json", "-f", "null", "NUL"], timeout=300)
    text = err + out
    m = re.search(r'"input_i"\s*:\s*"(-?[\d.]+)"', text)
    if m and m.group(1) not in ("-inf", "inf"):
        return float(m.group(1))
    m = re.search(r"input_i\s*:\s*(-?[\d.]+)", text)
    return float(m.group(1)) if m else None


# ------------------------------------------------------------- A: containers

def test_containers() -> None:
    begin("A) 音频格式转换矩阵（8 个源 × 7 个输出格式）")
    sources = ["a.mp3", "a.m4a", "a.aac", "a.flac", "a.ogg", "a.opus",
               "a_16k_mono.wav", "a_48k_stereo.wav"]
    out_dir = temp_dir("AA")
    for name in sources:
        src = AUD / name
        if not src.exists():
            continue
        for container, want in WANT_CODEC.items():
            info = info_of(src)
            opts = AudioOptions(container=container, bitrate=192)
            job = plan.plan_audio(info, out_dir, opts)
            res = run_job(job)
            label = "%-16s -> %-5s" % (name, container)
            if not res.ok:
                check(label, False, (res.error or "失败").splitlines()[-1][:100])
                job.cleanup()
                continue
            o = Out(job.out)
            ok, err = o.decodes()
            problem = ""
            if not ok:
                problem = "解码失败: " + err
            elif o.acodec != want:
                problem = "编码不符: 期望 %s 得到 %s" % (want, o.acodec)
            elif not o.audio:
                problem = "没有音轨"
            check(label, not problem,
                  "%s %dHz %dch %.2fs %.0fKB %s"
                  % (o.acodec, o.sample_rate, o.channels, o.duration,
                     o.size / 1024, problem))
            job.cleanup()


# ----------------------------------------------------------------- B: copying

def test_lossless_copy() -> None:
    begin("B) 无损提取（勾选「格式相同时直接复制音轨」）")
    out_dir = temp_dir("AB")
    cases = [
        ("a.m4a", "m4a", True, "aac", "AAC -> M4A"),
        ("a.m4a", "aac", True, "aac", "AAC -> ADTS 裸流"),
        ("a.mp3", "mp3", True, "mp3", "MP3 -> MP3"),
        ("a.flac", "flac", True, "flac", "FLAC -> FLAC"),
        ("a.opus", "opus", True, "opus", "Opus -> Opus"),
        ("a.ogg", "ogg", True, "vorbis", "Vorbis -> OGG"),
    ]
    for name, container, copy_ok, codec, label in cases:
        src = AUD / name
        info = info_of(src)
        job = plan.plan_audio(info, out_dir, AudioOptions(container=container,
                                                          copy_stream=copy_ok))
        # ffmpeg's own Stream mapping says whether the stream was copied; raw
        # packet hashes cannot, because a new container rewrites the framing
        # (ADTS headers around the same AAC frames, for instance).
        _, mapping = run_plan_direct(job)
        copied = "(copy)" in mapping
        o = Out(job.out)
        check(label, o.acodec == codec and copied,
              "编码=%s 音轨%s | %s | %s"
              % (o.acodec, "直接复制" if copied else "被重编码", job.note,
                 mapping.replace("\n", " ; ")[:60]))
        job.cleanup()

    # copy_stream off must actually re-encode.
    info = info_of(AUD / "a.m4a")
    job = plan.plan_audio(info, out_dir, AudioOptions(container="m4a",
                                                      copy_stream=False))
    _, mapping = run_plan_direct(job)
    check("关掉复制后确实重编码",
          "(copy)" not in mapping and "无损提取" not in job.note,
          "%s | %s" % (job.note, mapping.replace("\n", " ; ")[:70]))
    job.cleanup()


# ------------------------------------------------------------- C: resampling

def test_resample() -> None:
    begin("C) 采样率 / 声道（语音转写用 16kHz 单声道）")
    out_dir = temp_dir("AC")
    src = AUD / "a_48k_stereo.wav"
    cases = [
        (16000, 1, "16kHz 单声道（语音转写）"),
        (44100, 2, "44.1kHz 立体声"),
        (8000, 1, "8kHz 单声道"),
        (0, 0, "保持源规格"),
    ]
    for rate, ch, label in cases:
        info = info_of(src)
        job = plan.plan_audio(info, out_dir, AudioOptions(container="wav",
                                                          sample_rate=rate,
                                                          channels=ch))
        res = run_job(job)
        if not res.ok:
            check(label, False, (res.error or "失败").splitlines()[-1][:100])
            job.cleanup()
            continue
        o = Out(job.out)
        want_rate = rate or info.sample_rate
        want_ch = ch or info.channels
        check(label, o.sample_rate == want_rate and o.channels == want_ch,
              "得到 %dHz %dch（期望 %dHz %dch）| %s"
              % (o.sample_rate, o.channels, want_rate, want_ch, job.note))
        job.cleanup()

    # Opus can only do 48kHz; asking for 16k must say so, not fail.
    info = info_of(src)
    job = plan.plan_audio(info, out_dir, AudioOptions(container="opus",
                                                      sample_rate=16000))
    res = run_job(job)
    said = any("48" in w for w in job.warnings)
    if res.ok:
        o = Out(job.out)
        check("Opus 强制 48kHz 并说明", o.sample_rate == 48000 and said,
              "%dHz | 警告 %s" % (o.sample_rate, job.warnings))
    else:
        check("Opus 强制 48kHz 并说明", False, res.error[:100])
    job.cleanup()


# --------------------------------------------------------------- D: loudness

def test_loudness() -> None:
    begin("D) 音量标准化（实测输出响度）")
    out_dir = temp_dir("AD")
    src = AUD / "a_48k_stereo.wav"
    before = loudness(src)
    print("  源响度 %.1f LUFS" % (before if before is not None else float("nan")))
    for key, target in [("douyin", -14.0), ("podcast", -16.0), ("broadcast", -23.0)]:
        info = info_of(src)
        job = plan.plan_audio(info, out_dir, AudioOptions(container="wav",
                                                          loudness=key))
        res = run_job(job)
        label = "响度标准化 %s（目标 %.0f LUFS）" % (key, target)
        if not res.ok:
            check(label, False, (res.error or "失败").splitlines()[-1][:110])
            job.cleanup()
            continue
        got = loudness(job.out)
        check(label, got is not None and abs(got - target) <= 1.5,
              "实测 %.1f LUFS" % (got if got is not None else float("nan")))
        job.cleanup()

    # A batch of different sources must end level with each other.
    jobs = []
    for name in ("a_16k_mono.wav", "a_48k_stereo.wav", "a.mp3"):
        info = info_of(AUD / name)
        jobs.append(plan.plan_audio(info, out_dir, AudioOptions(container="wav",
                                                                loudness="douyin")))
    for job in jobs:
        run_job(job)
    got = [loudness(j.out) for j in jobs if j.out.exists()]
    spread = max(got) - min(got) if len(got) == 3 and all(
        g is not None for g in got) else 99
    check("不同来源统一后响度一致", spread <= 1.5,
          "三个结果: %s，极差 %.2f LU"
          % (["%.1f" % g if g is not None else "?" for g in got], spread))
    for job in jobs:
        job.cleanup()


# ------------------------------------------------------------------ E: trim

def test_trim() -> None:
    begin("E) 音频剪辑")
    out_dir = temp_dir("AE")
    src = AUD / "a_48k_stereo.wav"
    info = info_of(src)
    print("  源时长 %.2f 秒" % info.duration)
    src_dur = info.duration or 20.0
    cases = [
        (TrimSpec(1.0, 3.0), 2.0, "起 1 到 3"),
        (TrimSpec(2.0, None), src_dur - 2.0, "只给起点 2"),
        (TrimSpec(None, 5.0), 5.0, "只给终点 5"),
        (TrimSpec(0.0, 0.25), 0.25, "起 0 到 0.25（极小片段）"),
    ]
    for trim, want, label in cases:
        info = info_of(src)
        job = plan.plan_audio(info, out_dir, AudioOptions(container="wav", trim=trim))
        res = run_job(job)
        if not res.ok:
            check(label, False, (res.error or "失败").splitlines()[-1][:100])
            job.cleanup()
            continue
        o = Out(job.out)
        drift = abs(o.duration - want)
        check(label, drift <= 0.10,
              "期望 %.2fs 得到 %.2fs（差 %.2fs）" % (want, o.duration, drift))
        job.cleanup()

    # Same trim on a lossy format, and to a different container.
    for container in ("mp3", "m4a"):
        info = info_of(src)
        job = plan.plan_audio(info, out_dir, AudioOptions(container=container,
                                                          trim=TrimSpec(1.0, 3.0)))
        res = run_job(job)
        if res.ok:
            o = Out(job.out)
            check("剪辑同时转 %s" % container.upper(), abs(o.duration - 2.0) <= 0.1,
                  "得到 %.2fs" % o.duration)
        else:
            check("剪辑同时转 %s" % container.upper(), False, res.error[:100])
        job.cleanup()


# ------------------------------------------------------ F: from video files

def test_from_video() -> None:
    begin("F) 从视频里提音频（音频页也要能收视频）")
    out_dir = temp_dir("AF")
    for name, container, want in [("h264_1080p30.mp4", "mp3", "mp3"),
                                  ("h264_1080p30.mp4", "m4a", "aac"),
                                  ("h264_1080p30.mp4", "wav", "pcm_s16le"),
                                  ("vp9_1080p30.webm", "mp3", "mp3"),
                                  ("mpeg4_1080p30.avi", "m4a", "aac"),
                                  ("h264_1080p30.mov", "flac", "flac")]:
        src = VID / name
        if not src.exists():
            continue
        info = info_of(src)
        job = plan.plan_audio(info, out_dir, AudioOptions(container=container))
        res = run_job(job)
        label = "%-22s -> %s" % (name, container)
        if not res.ok:
            check(label, False, (res.error or "失败").splitlines()[-1][:110])
            job.cleanup()
            continue
        o = Out(job.out)
        ok, err = o.decodes()
        check(label, o.acodec == want and not o.video and ok,
              "%s 时长 %.2fs %s" % (o.acodec, o.duration, err))
        job.cleanup()

    # A video with no audio track must not silently produce an empty file.
    src = VID / "mute_1080p30.mp4"
    info = info_of(src)
    job = plan.plan_audio(info, out_dir, AudioOptions(container="mp3"))
    res = run_job(job)
    produced = job.out.exists() and job.out.stat().st_size > 0
    check("无音轨的视频不会被当成成功", not (res.ok and not produced),
          "结果 ok=%s 产出=%s 错误=%s" % (res.ok, produced,
                                          (res.error or "无").splitlines()[-1][:70]))
    job.cleanup()


# ------------------------------------------------------- G: bitrate sanity

def test_bitrate() -> None:
    begin("G) 码率设置确实生效")
    out_dir = temp_dir("AG")
    src = AUD / "a_48k_stereo.wav"
    sizes = {}
    for br in (64, 320):
        info = info_of(src)
        job = plan.plan_audio(info, out_dir, AudioOptions(container="mp3",
                                                          bitrate=br))
        res = run_job(job)
        if res.ok:
            sizes[br] = Out(job.out).size
        job.cleanup()
    if len(sizes) == 2:
        ratio = sizes[320] / max(1, sizes[64])
        check("320k 明显大于 64k（约 5 倍）", 3.5 <= ratio <= 6.5,
              "64k %.0fKB vs 320k %.0fKB（%.1f 倍）"
              % (sizes[64] / 1024, sizes[320] / 1024, ratio))
    else:
        check("320k 明显大于 64k", False, "有失败的转换")

    # WAV must be lossless regardless of the bitrate box.
    for br in (64, 320):
        info = info_of(src)
        job = plan.plan_audio(info, out_dir, AudioOptions(container="wav",
                                                          bitrate=br))
        res = run_job(job)
        if res.ok:
            check("WAV 忽略码率设置（无损）",
                  "kbps" not in job.note, job.note)
        else:
            check("WAV 忽略码率设置（无损）", False, res.error[:100])
        job.cleanup()


def main() -> int:
    test_containers()
    test_lossless_copy()
    test_resample()
    test_loudness()
    test_trim()
    test_from_video()
    test_bitrate()
    failed = summary("音频页")
    cleanup()
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)

