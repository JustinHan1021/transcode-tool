"""Container-pair matrix for merging.

Two clips can have byte-identical stream parameters and still live in different
containers; that is exactly the case the concat demuxer mis-offsets, and it needs
no re-encoding at all, so it is the easiest one to miss. Every pair is merged and
the result checked for the right length AND the right number of frames -- a
merge can report a plausible duration while the two clips sit on top of each
other.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import CORPUS, FFMPEG, Out, info_of, run_job, temp_dir  # noqa: E402

# The repo root is wherever this file ended up, so a clone runs anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FFPROBE = ROOT / "bin" / "ffprobe.exe"
from trans import plan  # noqa: E402
from trans.presets import ConcatOptions  # noqa: E402

VID = CORPUS / "video"
work = temp_dir("concat_matrix")
fails: list[str] = []
rows: list[str] = []


def packet_stats(path: Path) -> tuple[int, float]:
    p = subprocess.run([str(FFPROBE), "-hide_banner", "-v", "error",
                        "-print_format", "json", "-show_packets",
                        "-select_streams", "v:0", str(path)], capture_output=True)
    try:
        packs = json.loads(p.stdout.decode("utf-8", "replace"))["packets"]
    except Exception:
        return -1, -1.0
    last = float(packs[-1].get("pts_time") or 0) if packs else -1.0
    return len(packs), last


def trial(label: str, names: list[str], container: str, mode: str = "smart") -> None:
    srcs = [VID / n for n in names]
    missing = [s for s in srcs if not s.exists()]
    if missing:
        return
    infos = [info_of(s) for s in srcs]
    want = sum(i.duration or 0 for i in infos)
    want_packets = 0
    for s in srcs:
        n, _ = packet_stats(s)
        want_packets += n
    job = plan.plan_concat(infos, work, ConcatOptions(container=container, mode=mode))
    res = run_job(job, concurrency=2)
    if not res.ok:
        fails.append("%s: %s" % (label, (res.error or "").splitlines()[-1][:90]))
        rows.append("  %-34s 失败 %s" % (label, (res.error or "")[:60]))
        job.cleanup()
        return
    o = Out(job.out)
    n, last = packet_stats(job.out)
    problems: list[str] = []
    if abs(o.duration - want) > 1.2 and abs(last - want) > 1.2:
        problems.append("时长 %.2fs 应为 %.2fs" % (o.duration, want))
    # Re-encoding a clip emits one extra frame at the tail (a 20.00s clip comes
    # back as 601 frames), which is not a merge defect; the timestamp span above
    # is what catches a clip losing its offset.
    if abs(n - want_packets) > 2:
        problems.append("帧数 %d 应为 %d" % (n, want_packets))
    steps = "".join("R" if s.label.startswith("统一") else "C" for s in job.steps[:-1])
    rows.append("  %-34s %-5s %-6s 时长 %6.2fs 帧 %4d %s"
                % (label, container, steps or "-", o.duration, n,
                   "OK" if not problems else " ".join(problems)))
    if problems:
        fails.append("%s: %s" % (label, " ".join(problems)))
    job.cleanup()


print("R=该片段被重编码，C=直接复制\n")

# same codec, different container -- no re-encode should be needed
print("=== 参数一致但容器不同（最容易踩坑）===")
for pair in [("h264_1080p30.mp4", "h264_1080p30.ts"),
             ("h264_1080p30.mp4", "h264_1080p30.flv"),
             ("h264_1080p30.mp4", "h264_1080p30.mov"),
             ("h264_1080p30.ts", "h264_1080p30.mp4"),
             ("h264_1080p30.flv", "h264_1080p30.mp4"),
             ("h264_1080p30.mov", "h264_1080p30.mp4"),
             ("h264_1080p30.ts", "h264_1080p30.flv")]:
    trial(" + ".join(pair), list(pair), "mp4")
    trial(" + ".join(pair), list(pair), "mkv")

print()
print("=== 编码不同（需要归一化）===")
for pair in [("h264_1080p30.mp4", "hevc_1080p30.mp4"),
             ("hevc_1080p30.mp4", "h264_1080p30.mp4"),
             ("h264_1080p30.mp4", "vp9_1080p30.webm"),
             ("vp9_1080p30.webm", "h264_1080p30.mp4"),
             ("mpeg4_1080p30.avi", "h264_1080p30.mp4"),
             ("h264_1080p30.mp4", "mpeg2.mp4")]:
    for container in ("mp4", "mkv", "mov"):
        trial(" + ".join(pair), list(pair), container)

print()
print("=== 三段混搭 ===")
trial("mp4 + ts + flv", ["h264_1080p30.mp4", "h264_1080p30.ts",
                         "h264_1080p30.flv"], "mp4")
trial("mp4 + hevc + webm", ["h264_1080p30.mp4", "hevc_1080p30.mp4",
                            "vp9_1080p30.webm"], "mkv")
trial("webm + mp4 + avi", ["vp9_1080p30.webm", "h264_1080p30.mp4",
                           "mpeg4_1080p30.avi"], "mkv")
trial("尺寸都不同", ["h264_1080p30.mp4", "tiny_160x90.mp4", "square_600.mp4"], "mp4")

print()
for r in rows:
    print(r)
print()
print("失败 %d 项" % len(fails))
for f in fails:
    print("  " + f)
