"""Video tab sweep: every container, encoder, quality tier, geometry, audio
codec, trim mode and target size, run for real and inspected on disk.

The strongest check here is `stream_hash`: it proves a "无损复制" claim by
comparing the raw packets, so a silent re-encode cannot pass.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import (CORPUS, FFMPEG, WORK, Out, begin, check, cleanup,  # noqa: E402
                     info_of, run_job, short_clip, stream_hash, summary,
                     temp_dir, _cache)

from trans import geometry as G, plan  # noqa: E402
from trans.presets import (AUDIO_CONTAINERS, VIDEO_CONTAINERS, VIDEO_ENCODERS,  # noqa: E402
                           VideoOptions, TrimSpec)

# A source with a keyframe every second, so a copy-based trim has somewhere
# sane to land. Default x264 puts one every 250 frames = 8s.
# ffprobe names a container with the whole family it can read, so the check has
# to look for a member of that list rather than an exact muxer name.
MUXER_NAMES = {
    "mp4": "mp4", "mov": "mov", "m4v": "mov", "mkv": "matroska",
    "webm": "webm", "avi": "avi", "ts": "mpegts", "flv": "flv", "gif": "gif",
}

KCUT = None


def kcut_clip() -> Path:
    global KCUT
    if KCUT and KCUT.exists():
        return KCUT
    WORK.mkdir(parents=True, exist_ok=True)
    out = WORK / "kcut5.mp4"
    if not out.exists():
        import subprocess
        subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=5",
                        "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=48000:duration=5",
                        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
                        "-pix_fmt", "yuv420p", "-g", "30", "-keyint_min", "30",
                        "-sc_threshold", "0", "-c:a", "aac", "-b:a", "128k",
                        "-shortest", str(out)], check=True)
    KCUT = out
    return out


# ------------------------------------------------------------- A: containers

def test_containers() -> None:
    begin("A) 输出格式 × 源编码 矩阵（智能模式）")
    srcs = [
        ("h264_1080p30.mp4", "h264", "aac"),
        ("vp9_1080p30.webm", "vp9", "opus"),
        ("mpeg4_1080p30.avi", "mpeg4", "mp3"),
        ("mpeg2.mp4", "mpeg2video", "mp2"),
        ("hevc_1080p30.mp4", "hevc", "aac"),
        ("mute_1080p30.mp4", "h264", ""),
        ("h264_1080p30.flv", "h264", "aac"),
        ("h264_1080p30.ts", "h264", "aac"),
    ]
    out_dir = temp_dir("A")
    for container in ("mp4", "mov", "m4v", "mkv", "webm", "avi", "ts", "flv"):
        for src_name, vcodec, acodec in srcs:
            src = CORPUS / "video" / src_name
            if not src.exists():
                continue
            info = info_of(src)
            job = plan.plan_video(info, out_dir, VideoOptions(container=container,
                                                             mode="smart"))
            res = run_job(job)
            label = "%-6s <- %-22s" % (container, src_name)
            if not res.ok:
                check(label, False, "失败: " + res.error.splitlines()[-1][:90]
                      if res.error else "失败")
                continue
            o = Out(job.out)
            ok, err = o.decodes()
            claimed_copy = "无损复制" in job.note
            problem = ""
            if not ok:
                problem = "输出无法完整解码: " + err
            elif MUXER_NAMES[container] not in [n.strip() for n in
                                                o.container.split(",")]:
                problem = "容器不符: %s" % o.container
            elif not o.video:
                problem = "没有画面流"
            elif claimed_copy and o.vcodec != vcodec:
                problem = "声称无损复制，画面上却是 %s（源为 %s）" % (o.vcodec, vcodec)
            check(label, not problem,
                  "%s | %s | %s" % (o.describe(), job.note, problem))
            job.cleanup()


# -------------------------------------------------------------- B: encoders

def test_encoders() -> None:
    begin("B) 编码器 × 画质档（每个输出都必须能解码）")
    src = short_clip("enc1080.mp4", 2.0, "1920x1080", 30)
    info = info_of(src)
    out_dir = temp_dir("B")
    for key, spec in VIDEO_ENCODERS.items():
        for tier in ("high", "balanced", "small", "tiny"):
            opts = VideoOptions(container="mkv", mode=tier, encoder_key=key)
            job = plan.plan_video(info, out_dir, opts)
            res = run_job(job)
            label = "%-11s %-8s" % (key, tier)
            if not res.ok:
                check(label, False, (res.error or "失败").splitlines()[-1][:100])
                job.cleanup()
                continue
            o = Out(job.out)
            ok, err = o.decodes()
            want = spec.family
            got_family = "hevc" if o.vcodec in ("hevc", "h265") else o.vcodec
            problem = ""
            if not ok:
                problem = "解码失败: " + err
            elif got_family != want:
                problem = "编码族不符: 期望 %s 得到 %s" % (want, o.vcodec)
            elif o.size == 0:
                problem = "空文件"
            check(label, not problem, "%s %s" % (o.describe(), problem))
            job.cleanup()


# -------------------------------------------------------------- C: geometry

def test_geometry() -> None:
    begin("C) 裁剪比例 × 缩放方式（输出尺寸必须与界面标注一致）")
    src = short_clip("geo1080.mp4", 1.0, "1920x1080", 30)
    portrait = short_clip("geoport.mp4", 1.0, "1080x1920", 30)
    out_dir = temp_dir("C")

    # Every aspect preset on a landscape source, cropped only.
    for name, rw, rh in [("原始", 0, 0), ("1:1 方形", 1, 1), ("4:3", 4, 3),
                         ("3:4", 3, 4), ("16:9", 16, 9), ("9:16 竖版", 9, 16),
                         ("3:2", 3, 2), ("2:3", 2, 3), ("2.35:1 宽银幕", 235, 100)]:
        crop = None if not rw else G.CropSpec(mode="aspect", aspect_w=rw, aspect_h=rh)
        opts = VideoOptions(container="mp4", mode="balanced", crop=crop)
        info = info_of(src)
        job = plan.plan_video(info, out_dir, opts)
        geo = G.resolve(info.width, info.height, crop, None)
        expect = (geo.crop_w or info.width, geo.crop_h or info.height) \
            if geo.needs_crop else (info.width, info.height)
        res = run_job(job)
        label = "裁剪 %-12s" % name
        if not res.ok:
            check(label, False, (res.error or "失败").splitlines()[-1][:100])
            job.cleanup()
            continue
        o = Out(job.out)
        ok, err = o.decodes()
        check(label, ok and o.dims == expect,
              "期望 %dx%d 得到 %dx%d %s" % (expect[0], expect[1], o.width,
                                            o.height, err if not ok else ""))
        job.cleanup()

    # Every scale mode.
    scale_cases = [
        ("按宽度 720", G.ScaleSpec(mode="width", width=720), (720, 404)),
        ("按高度 720", G.ScaleSpec(mode="height", height=720), (1280, 720)),
        ("最长边 640", G.ScaleSpec(mode="longest", longest=640), (640, 360)),
        ("百分比 50%", G.ScaleSpec(mode="percent", percent=50), (960, 540)),
        ("方框内 800x600", G.ScaleSpec(mode="box", box_w=800, box_h=600), (800, 450)),
        ("精确 640x480", G.ScaleSpec(mode="exact", width=640, height=480), (640, 480)),
        ("按宽度 2400（不放大）", G.ScaleSpec(mode="width", width=2400,
                                              no_upscale=True), (1920, 1080)),
    ]
    for label, scale, expect in scale_cases:
        info = info_of(src)
        job = plan.plan_video(info, out_dir, VideoOptions(container="mp4",
                                                          mode="balanced",
                                                          scale=scale))
        res = run_job(job)
        if not res.ok:
            check(label, False, (res.error or "失败").splitlines()[-1][:100])
            job.cleanup()
            continue
        o = Out(job.out)
        ok, err = o.decodes()
        check(label, ok and o.dims == expect,
              "期望 %dx%d 得到 %dx%d %s" % (expect[0], expect[1], o.width,
                                            o.height, err if not ok else ""))
        job.cleanup()

    # Crop then scale -- the combination that must land exactly on 1080x1920.
    crop = G.CropSpec(mode="aspect", aspect_w=9, aspect_h=16)
    scale = G.ScaleSpec(mode="exact", width=1080, height=1920)
    info = info_of(src)
    job = plan.plan_video(info, out_dir, VideoOptions(container="mp4",
                                                      mode="balanced",
                                                      crop=crop, scale=scale))
    res = run_job(job)
    if res.ok:
        o = Out(job.out)
        check("裁 9:16 再缩到 1080x1920", o.dims == (1080, 1920),
              "得到 %dx%d" % o.dims)
    else:
        check("裁 9:16 再缩到 1080x1920", False, res.error[:100])
    job.cleanup()

    # Portrait source stays portrait.
    info = info_of(portrait)
    job = plan.plan_video(info, out_dir, VideoOptions(
        container="mp4", mode="balanced",
        scale=G.ScaleSpec(mode="longest", longest=480)))
    res = run_job(job)
    if res.ok:
        o = Out(job.out)
        check("竖版素材缩到最长边 480", o.dims == (270, 480), "得到 %dx%d" % o.dims)
    else:
        check("竖版素材缩到最长边 480", False, res.error[:100])
    job.cleanup()

    # Odd-sized source: everything must still come out even.
    odd = CORPUS / "video" / "odd_641x359.mp4"
    for label, scale in [("奇数源 按宽度 400", G.ScaleSpec(mode="width", width=400)),
                         ("奇数源 按高度 200", G.ScaleSpec(mode="height", height=200))]:
        info = info_of(odd)
        job = plan.plan_video(info, out_dir, VideoOptions(container="mp4",
                                                          mode="balanced",
                                                          scale=scale))
        res = run_job(job)
        if res.ok:
            o = Out(job.out)
            even_ok = o.width % 2 == 0 and o.height % 2 == 0
            check(label, even_ok, "得到 %dx%d（必须为偶数）" % o.dims)
        else:
            check(label, False, res.error[:100])
        job.cleanup()


# ---------------------------------------------------------------- D: audio

def test_audio_codecs() -> None:
    begin("D) 视频容器 × 音频编码选择")
    src = short_clip("aud1080.mp4", 2.0, "1280x720", 30)
    out_dir = temp_dir("D")
    # (container, requested codec, expected ffmpeg codec)
    cases = [
        ("mp4", "auto", "aac"), ("mp4", "copy", "aac"), ("mp4", "none", ""),
        ("mp4", "aac", "aac"), ("mp4", "mp3", "mp3"), ("mp4", "ac3", "ac3"),
        ("mp4", "opus", "aac"),           # not legal in mp4 -> documented fallback
        ("mkv", "opus", "opus"), ("mkv", "flac", "flac"), ("mkv", "vorbis", "vorbis"),
        ("mkv", "pcm_s16le", "pcm_s16le"), ("mkv", "copy", "aac"),
        ("webm", "auto", "opus"), ("webm", "vorbis", "vorbis"),
        ("webm", "aac", "opus"),          # not legal in webm -> fallback
        ("avi", "auto", "mp3"), ("avi", "copy", "mp3"), ("avi", "mp3", "mp3"),
        ("mov", "copy", "aac"), ("ts", "copy", "aac"), ("flv", "copy", "aac"),
        ("mkv", "none", ""),
    ]
    for container, req, expect in cases:
        info = info_of(src)
        opts = VideoOptions(container=container, mode="smart", audio_codec=req)
        job = plan.plan_video(info, out_dir, opts)
        res = run_job(job)
        label = "%-5s 音频=%-10s" % (container, req)
        if not res.ok:
            check(label, False, (res.error or "失败").splitlines()[-1][:100])
            job.cleanup()
            continue
        o = Out(job.out)
        ok, err = o.decodes()
        got = o.acodec
        warn = "有提示" if job.warnings else "无提示"
        problem = ""
        if not ok:
            problem = "解码失败: " + err
        elif got != expect:
            problem = "期望 %s 得到 %s" % (expect or "无音轨", got or "无音轨")
        elif req in ("opus", "aac", "vorbis") and container in ("mp4", "webm") \
                and req not in VIDEO_CONTAINERS[container].audio \
                and not job.warnings:
            problem = "做了降级替换却没有告诉用户"
        check(label, not problem, "音频=%s %s %s" % (got or "无", warn, problem))
        job.cleanup()


# ----------------------------------------------------------------- E: trim

def test_trim() -> None:
    begin("E) 剪辑：起点/终点/精确/无损对齐")
    src = kcut_clip()
    info = info_of(src)
    src_dur = info.duration
    out_dir = temp_dir("E")
    print("  源: %s  %.2f 秒, 每 1 秒一个关键帧" % (src.name, src_dur))

    cases = [
        ("起 1.0 到 2.5（智能/无损）", TrimSpec(1.0, 2.5), "smart", 1.5, 0.6),
        ("起 1.0 到 2.5（均衡/重编码）", TrimSpec(1.0, 2.5), "balanced", 1.5, 0.10),
        ("只给起点 3.0（无损）", TrimSpec(3.0, None), "smart", 2.0, 0.6),
        ("只给终点 2.0（无损）", TrimSpec(None, 2.0), "smart", 2.0, 0.6),
        ("起 1.0 到 2.5 逐帧精确", TrimSpec(1.0, 2.5, precise=True), "smart", 1.5, 0.10),
        ("起 0.5 到 0.9（0.4 秒碎片）", TrimSpec(0.5, 0.9), "balanced", 0.4, 0.10),
    ]
    for label, trim, mode, want, tol in cases:
        info = info_of(src)
        job = plan.plan_video(info, out_dir, VideoOptions(container="mp4",
                                                          mode=mode, trim=trim))
        res = run_job(job)
        if not res.ok:
            check(label, False, (res.error or "失败").splitlines()[-1][:110])
            job.cleanup()
            continue
        o = Out(job.out)
        ok, err = o.decodes()
        drift = abs(o.duration - want)
        check(label, ok and drift <= tol,
              "期望 %.2fs 得到 %.2fs（差 %.2fs，容差 %.2f）%s"
              % (want, o.duration, drift, tol, err if not ok else ""))
        job.cleanup()

    # A copy-based trim must not quietly re-encode.
    info = info_of(src)
    job = plan.plan_video(info, out_dir, VideoOptions(container="mp4", mode="smart",
                                                      trim=TrimSpec(1.0, 2.5)))
    res = run_job(job)
    if res.ok:
        o = Out(job.out)
        check("无损剪辑确实是复制（不是偷偷重编码）", o.vcodec == "h264",
              "输出编码 %s" % o.vcodec)
    else:
        check("无损剪辑确实是复制", False, res.error[:100])
    job.cleanup()

    # frame accuracy: precise cut of 1.5s at 30fps ~ 45 frames
    info = info_of(src)
    job = plan.plan_video(info, out_dir, VideoOptions(
        container="mp4", mode="balanced", trim=TrimSpec(1.0, 2.5, precise=True)))
    res = run_job(job)
    if res.ok:
        o = Out(job.out)
        n = o.frames()
        check("逐帧精确剪辑帧数接近 45", 43 <= n <= 47, "实际 %d 帧" % n)
    else:
        check("逐帧精确剪辑帧数", False, res.error[:100])
    job.cleanup()


# --------------------------------------------------------- F: target size

def test_target_size() -> None:
    begin("F) 指定目标体积")
    src = short_clip("tgt1080.mp4", 3.0, "1920x1080", 30)
    out_dir = temp_dir("F")
    for mb in (0.5, 1.0, 2.0):
        info = info_of(src)
        job = plan.plan_video(info, out_dir, VideoOptions(container="mp4",
                                                          mode="target",
                                                          target_mb=mb))
        res = run_job(job)
        label = "目标 %.1fMB" % mb
        if not res.ok:
            check(label, False, (res.error or "失败").splitlines()[-1][:110])
            job.cleanup()
            continue
        o = Out(job.out)
        ok, err = o.decodes()
        got_mb = o.size / 1024 / 1024
        ratio = got_mb / mb
        check(label, ok and 0.6 <= ratio <= 1.15,
              "得到 %.2fMB（目标的 %.0f%%）%s %s"
              % (got_mb, ratio * 100, "两遍" if len(job.steps) == 2 else "单遍",
                 err if not ok else ""))
        job.cleanup()

    # Two passes only make sense for x264/x265; the hardware path must say so.
    info = info_of(src)
    job = plan.plan_video(info, out_dir, VideoOptions(container="mp4", mode="target",
                                                      target_mb=1.0,
                                                      encoder_key="nvenc_h264"))
    refused = any("两遍" in w for w in job.warnings)
    flat = [a for st in job.steps for a in st.args]
    used = flat[flat.index("-c:v") + 1] if "-c:v" in flat else "?"
    res = run_job(job)
    if res.ok:
        o = Out(job.out)
        check("硬件编码 + 目标体积：保留所选编码器并说明没有两遍",
              refused and used == "h264_nvenc",
              "实际编码器 %s | 说明=%s | 输出 %.2fMB"
              % (used, refused, o.size / 1024 / 1024))
    else:
        check("硬件编码 + 目标体积", False, res.error[:110])
    job.cleanup()


# ------------------------------------------------- G: overwrite protection

def test_overwrite() -> None:
    begin("G) 输出路径：绝不覆盖源、不覆盖已有产物、重名自动加序号")
    d = temp_dir("G")
    src = short_clip("same.mp4", 1.0, "640x360", 30)
    local = d / "same.mp4"
    shutil.copy2(src, local)
    info = info_of(local)
    before = local.read_bytes()

    job = plan.plan_video(info, d, VideoOptions(container="mp4", mode="balanced"))
    check("同名转换不落在源文件上", job.out != local, "输出 %s" % job.out.name)
    res = run_job(job)
    check("源文件未被改动", local.read_bytes() == before,
          "%d 字节" % len(before))

    # Plan the same job again: the earlier output exists, so a new name is due.
    job2 = plan.plan_video(info_of(local), d, VideoOptions(container="mp4",
                                                           mode="balanced"))
    check("重名自动加序号", job2.out != job.out and job2.out.exists() is False,
          "%s vs %s" % (job.out.name, job2.out.name))
    job.cleanup()

    # Two jobs on the same source in one batch must not collide either.
    taken: set = set()
    j3 = plan.plan_video(info_of(local), d, VideoOptions(container="mp4",
                                                         mode="balanced"), taken)
    j4 = plan.plan_video(info_of(local), d, VideoOptions(container="mkv",
                                                         mode="balanced"), taken)
    j5 = plan.plan_video(info_of(local), d, VideoOptions(container="mp4",
                                                         mode="balanced"), taken)
    check("同批任务不互相覆盖", len({j3.out, j4.out, j5.out}) == 3,
          " / ".join(sorted(p.name for p in (j3.out, j4.out, j5.out))))


def main() -> int:
    test_containers()
    test_encoders()
    test_geometry()
    test_audio_codecs()
    test_trim()
    test_target_size()
    test_overwrite()
    failed = summary("视频页")
    cleanup()
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
