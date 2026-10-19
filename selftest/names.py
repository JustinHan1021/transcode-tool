"""Does the app cope with filenames ffmpeg cannot guess a format from?

ffmpeg derives the muxer from the extension, and a full-width period (。) before
it defeats that. The app always passes an explicit `-f`, so it should be immune
-- this proves it rather than assuming.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

# The repo root is wherever this file ended up, so a clone runs anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from trans import ff, media, plan
from trans.engine import Engine
from trans.presets import AudioOptions, ImageOptions, TrimSpec, VideoOptions

caps = ff.detect()
FFMPEG = str(caps.ffmpeg)
work = Path(tempfile.mkdtemp(prefix="names_"))

# names that defeat ffmpeg's extension sniffing, plus other awkward ones
NAMES = ["点号.和中文。.mp4", "两个。。句号.mp4", "末尾是句号。.mp4",
         "带 ' 引号.mp4", "带 & 和 # 号.mp4", "带[方括号]和(圆括号).mp4",
         "带 多 个 空 格.mp4", "··重复点.mp4"]
src = ROOT / "_corpus" / "video" / "audio_only_5s.mp4"
print("源: %s" % src.name)
print()
print("=== 1) 直接探测这些文件 ===")
made = []
for n in NAMES:
    p = work / n
    shutil.copy2(src, p)
    made.append(p)
    try:
        info = media.probe(p, use_cache=False)
        print("  %-24s 探测 OK  %s  时长 %.2f" % (n[:24], info.summary()[:34],
                                              info.duration or 0))
    except Exception as exc:
        print("  %-24s 探测失败: %s" % (n[:24], exc))

print()
print("=== 2) 走完整流程转换（视频 -> mkv 无损复制）===")
jobs = []
for p in made:
    info = media.probe(p, use_cache=False)
    job = plan.plan_video(info, None, VideoOptions(container="mkv", mode="smart"),
                          taken=set())
    jobs.append(job)

results = []
Engine(caps.ffmpeg, on_event=lambda e: None, log_lines=25).run(
    jobs, concurrency=3, on_complete=lambda rs: results.extend(rs))
ok_all = True
for job, r in zip(jobs, results):
    ok = r.ok and job.out.exists() and job.out.stat().st_size > 0
    ok_all = ok_all and ok
    print("  %-26s -> %-26s %s" % (job.src.name[:26], job.out.name[:26],
                                   "OK %.1fKB" % (job.out.stat().st_size / 1024)
                                   if ok else "失败: " + r.error[:60]))

print()
print("=== 3) 图片与音频也试一个 ===")
img_src = ROOT / "_corpus" / "image" / "i_1920x1080.png"
for name, kind in [("图片。。.webp", "image"), ("音频。。.mp3", "audio")]:
    if kind == "image":
        p = work / name
        shutil.copy2(img_src, p)
        job = plan.plan_image(media.probe(p, use_cache=False), None,
                              ImageOptions(container="webp", quality=85), taken=set())
    else:
        p = work / name
        shutil.copy2(ROOT / "_corpus" / "audio" / "a.mp3", p)
        job = plan.plan_audio(media.probe(p, use_cache=False), None,
                              AudioOptions(container="wav"), taken=set())
    res = []
    Engine(caps.ffmpeg, on_event=lambda e: None, log_lines=20).run(
        [job], concurrency=1, on_complete=lambda rs: res.extend(rs))
    ok = res and res[0].ok and job.out.exists()
    ok_all = ok_all and bool(ok)
    print("  %-16s -> %-16s %s" % (name, job.out.name,
                                   "OK %.1fKB" % (job.out.stat().st_size / 1024)
                                   if ok else "失败: " + (res[0].error[:60] if res else "?")))

print()
print("=== 4) 截取也试一下（copy 路径）===")
p = work / "截取。。.mp4"
shutil.copy2(src, p)
job = plan.plan_video(media.probe(p, use_cache=False), None,
                      VideoOptions(container="mp4", mode="smart",
                                   trim=TrimSpec(1, 2)), taken=set())
res = []
Engine(caps.ffmpeg, on_event=lambda e: None, log_lines=20).run(
    [job], concurrency=1, on_complete=lambda rs: res.extend(rs))
ok = res and res[0].ok and job.out.exists()
ok_all = ok_all and bool(ok)
print("  %s -> %s  %s" % (job.src.name, job.out.name,
                          "OK" if ok else "失败: " + (res[0].error[:70] if res else "?")))

print()
print("=== 结论 ===")
print("  全部通过：显式 -f 让文件名不会影响输出" if ok_all
      else "  有失败项，需要进一步处理")
shutil.rmtree(work, ignore_errors=True)
