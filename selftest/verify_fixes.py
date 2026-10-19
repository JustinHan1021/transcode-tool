"""Targeted re-checks for the four defects fixed this round."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import (CORPUS, FFMPEG, Out, info_of, run_job, short_clip,  # noqa: E402
                     temp_dir)

# The repo root is wherever this file ended up, so a clone runs anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from PIL import Image, ImageSequence  # noqa: E402
from trans import ff, media, plan  # noqa: E402
from trans.presets import (AudioOptions, BITRATES_KBPS, SAMPLE_RATES,  # noqa: E402
                           ImageOptions, TrimSpec, VideoOptions)

CAPS = ff.detect()
WORK = temp_dir("fixes")
ok_all = True


def line(label: str, ok: bool, detail: str = "") -> None:
    global ok_all
    ok_all = ok_all and ok
    print("  [%s] %s%s" % ("OK  " if ok else "FAIL", label,
                           ("  -- " + detail) if detail else ""))


print("=== 1) 动画 WebP 输入不再丢动画 ===")
print("  animated_webp_input = %s（修复前为 False）" % CAPS.animated_webp_input)
print("  notes = %s" % (CAPS.notes or "无"))
line("能力探测不再误报动画 WebP 不可读",
     CAPS.animated_webp_input and not any("静态 WebP" in n for n in CAPS.notes),
     "ffmpeg %s" % CAPS.version)

src = CORPUS / "image" / "anim.webp"
info = media.probe(src, use_cache=False)
print("  anim.webp 探测: duration=%s frames=%s is_animated=%s"
      % (info.duration, info.frames, info.is_animated))
for container in ("gif", "webp"):
    job = plan.plan_image(info_of(src), WORK, ImageOptions(container=container,
                                                           quality=80))
    res = run_job(job)
    if res.ok:
        im = Image.open(job.out)
        frames = sum(1 for _ in ImageSequence.Iterator(im))
        # PIL does not report per-frame delays for animated WebP, so the span
        # comes from our own probe (which recovers it by counting packets).
        span = info_of(job.out).duration or 0.0
        # The GIF path re-times to 12fps, so the frame count changes while the
        # 2-second span must not; a still would be one frame.
        line("动画 WebP -> %s 保留动画" % container,
             frames > 1 and abs(span - 2.0) <= 0.2,
             "输出 %d 帧、合计 %.2f 秒" % (frames, span))
    else:
        line("动画 WebP -> %s" % container, False, res.error[:80])
    job.cleanup()

print()
print("=== 2) 无损剪辑的时长 ===")
src = short_clip("kcut5.mp4", 5.0, "1280x720", 30)
cases = [(TrimSpec(1.0, 2.5), 1.5), (TrimSpec(1.2, 2.7), 1.5),
         (TrimSpec(3.0, None), 2.0), (TrimSpec(None, 2.0), 2.0),
         (TrimSpec(0.0, 1.0), 1.0), (TrimSpec(4.5, 5.0), 0.5)]
for trim, want in cases:
    job = plan.plan_video(info_of(src), WORK, VideoOptions(container="mp4",
                                                           mode="smart",
                                                           trim=trim))
    res = run_job(job)
    if res.ok:
        o = Out(job.out)
        line("无损剪辑 %-16s 期望 %.2fs" % (trim.describe(), want),
             abs(o.duration - want) <= 0.05, "得到 %.3fs" % o.duration)
    else:
        line("无损剪辑 %s" % trim.describe(), False, res.error[:80])
    job.cleanup()

print()
print("=== 3) 格式 × 采样率 × 码率 全部组合（再扫一遍）===")
CONTAINERS = [c for c in ("mp3", "m4a", "aac", "flac", "ogg", "opus")]
base = CORPUS / "audio" / "a_48k_stereo.wav"
fails = []
total = 0
for container in CONTAINERS:
    for rate in SAMPLE_RATES:
        for channels in (1, 2):
            for bitrate in BITRATES_KBPS:
                total += 1
                job = plan.plan_audio(
                    info_of(base), WORK,
                    AudioOptions(container=container, bitrate=bitrate,
                                 sample_rate=rate, channels=channels,
                                 copy_stream=False))
                res = run_job(job)
                good = res.ok and job.out.exists() and job.out.stat().st_size > 0
                if not good:
                    msg = (res.error or "").splitlines()[-1][:70] if res.error else "无输出"
                    fails.append((container, rate, channels, bitrate, msg))
                job.cleanup()
print("  组合 %d 个，失败 %d 个（修复前 69/864）" % (total, len(fails)))
for f in fails[:12]:
    print("     %s %dHz %dch %dk -> %s" % f)
line("所有可达组合都能编码", not fails)

print()
print("=== 4) 硬件编码 + 目标体积 不再偷换成软编 ===")
src = short_clip("tgt.mp4", 3.0, "1920x1080", 30)
for key in ("auto", "nvenc_h264", "nvenc_h265"):
    job = plan.plan_video(info_of(src), WORK,
                          VideoOptions(container="mp4", mode="target",
                                       target_mb=1.0, encoder_key=key))
    codecs = [a for st in job.steps for a in st.args]
    used = codecs[codecs.index("-c:v") + 1] if "-c:v" in codecs else "?"
    warned = any("两遍" in w for w in job.warnings)
    res = run_job(job)
    size = job.out.stat().st_size / 1048576 if job.out.exists() else 0
    line("%-11s 实际使用 %s" % (key, used),
         (used == "libx264" and not warned) if key == "auto"
         else (used.startswith(("h264_nvenc", "hevc_nvenc")) and warned),
         "警告=%s 产出 %.2fMB" % (warned, size))
    job.cleanup()

print()
print("=== 5) 码率钳制是否如实说明 ===")
cases = [("mp3", 320, 8000, 1), ("mp3", 192, 16000, 2),
         ("opus", 320, 48000, 1), ("opus", 320, 48000, 2),
         ("ogg", 192, 16000, 1), ("aac", 192, 48000, 2)]
for container, bitrate, rate, ch in cases:
    job = plan.plan_audio(info_of(base), WORK,
                          AudioOptions(container=container, bitrate=bitrate,
                                       sample_rate=rate, channels=ch,
                                       copy_stream=False))
    res = run_job(job)
    args = " ".join(job.steps[0].args)
    ratearr = "-q:a" if "-q:a" in args else "-b:a"
    val = args.split(ratearr)[1].split()[0]
    line("%-5s %4dk %5dHz %dch -> %s %s" % (container, bitrate, rate, ch,
                                            ratearr, val),
         res.ok, "note=%s | 警告=%s" % (job.note,
                                        job.warnings[0] if job.warnings else "无"))
    job.cleanup()

print()
print("总体: %s" % ("全部通过" if ok_all else "仍有失败项"))
