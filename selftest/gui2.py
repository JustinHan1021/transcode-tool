"""Part 2 of the GUI sweep: real conversions, sorting, dragging, the timeline,
cancellation and the keyboard shortcuts.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gui import (AUD, IMG, VID, add_files, appmod, check, log_text, pump,  # noqa: E402
                 run_tab, section)
from harness import CORPUS, FFMPEG, Out, temp_dir  # noqa: E402

# The repo root is wherever this file ended up, so a clone runs anywhere.
ROOT = Path(__file__).resolve().parent.parent
from PIL import Image, ImageSequence  # noqa: E402
import tkinter as tk  # noqa: E402


# ------------------------------------------------------------ G4 real runs

def g4_real_runs(app, root) -> None:
    section("G4) 真实转换：三页各跑一遍，并检查界面反馈")
    video, image, audio = app.tabs
    out = temp_dir("G4")

    # ---- video: MOV -> MKV, lossless copy
    video.table.clear()
    add_files(video, [VID / "h264_1080p30.mov"], root)
    video.cmb_fmt.set_key("mkv")
    video.cmb_quality.set_key("smart")
    video.cmb_scale.set_key("none")
    video.cmb_aspect.set_key("原始")
    video.var_t_start.set("")
    video.var_t_end.set("")
    video.var_same_dir.set(False)
    video.var_out_dir.set(str(out))
    video.log.clear()
    done = run_tab(video, root)
    o = Out(video.table.items[0].job.out) if video.table.items[0].job else Out(out / "x")
    check("视频：MOV→MKV 无损复制", done and o.exists and o.vcodec == "h264",
          "%s %s" % (o.describe() if o.exists else "无输出",
                     video.table.items[0].status))
    check("视频：日志写出了输出名与方案",
          "→" in log_text(video) and "mkv" in log_text(video).lower(),
          log_text(video).strip().splitlines()[-3:][0][:70]
          if log_text(video).strip() else "无")
    check("视频：状态显示全部完成", "完成" in video.var_progress.get(),
          video.var_progress.get())

    # ---- image: PNG -> WebP with a resize
    image.table.clear()
    add_files(image, [IMG / "i_1920x1080.png"], root)
    image.var_mode.set("convert")
    image.cmb_fmt.set_key("webp")
    image.cmb_scale.set_key("longest")
    image.var_w.set("640")
    image.var_same_dir.set(False)
    image.var_out_dir.set(str(out))
    image.log.clear()
    done = run_tab(image, root)
    o = Out(image.table.items[0].job.out) if image.table.items[0].job else None
    check("图片：PNG→WebP 并缩到 640", done and o and o.exists
          and o.width == 640 and o.vcodec == "webp",
          o.describe() if o and o.exists else "无输出")

    # ---- image: 多图合成动图, order follows the 序号 column
    image.table.clear()
    seq = [IMG / f"seq{i:02d}.png" for i in range(1, 6)]
    add_files(image, seq, root)
    image.var_mode.set("sequence")
    image.cmb_fmt.set_key("gif")
    image.cmb_delay.set_key("0.5")
    image.cmb_loop.set_key("0")
    image.var_same_dir.set(False)
    image.var_out_dir.set(str(out))
    image.log.clear()
    done = run_tab(image, root)
    jobs = {id(i.job) for i in image.table.items if i.job}
    outcome = None
    for it in image.table.items:
        if it.job is not None:
            outcome = it.job
            break
    if done and outcome is not None and outcome.out.exists():
        n = sum(1 for _ in ImageSequence.Iterator(Image.open(outcome.out)))
        exts = {Path(i.path).name for i in image.table.items}
        check("图片：5 张图合成一个动图", len(jobs) == 1 and n == 5,
              "输出 %d 帧，参与 %d 个文件，状态 %s"
              % (n, len(image.table.items), image.table.items[0].status))
        check("图片：合成时所有行都显示进度",
              all(i.status in ("完成",) for i in image.table.items),
              "、".join(sorted({i.status for i in image.table.items})))
    else:
        check("图片：5 张图合成一个动图", False, "没有产出")
    image.var_mode.set("convert")

    # ---- audio: WAV -> MP3, 16k mono for transcription
    audio.table.clear()
    add_files(audio, [AUD / "a_48k_stereo.wav"], root)
    audio.cmb_fmt.set_key("mp3")
    audio.cmb_br.set_key("192")
    audio.cmb_rate.set_key("16000")
    audio.cmb_ch.set_key("1")
    audio.var_same_dir.set(False)
    audio.var_out_dir.set(str(out))
    audio.log.clear()
    done = run_tab(audio, root)
    o = Out(audio.table.items[0].job.out) if audio.table.items[0].job else None
    check("音频：WAV→MP3 16kHz 单声道", done and o and o.exists
          and o.sample_rate == 16000 and o.channels == 1 and o.acodec == "mp3",
          o.describe() if o and o.exists else "无输出")

    # ---- audio: extract from a video, lossless
    audio.table.clear()
    add_files(audio, [VID / "h264_1080p30.mp4"], root)
    audio.cmb_fmt.set_key("m4a")
    audio.cmb_rate.set_key("0")
    audio.cmb_ch.set_key("0")
    audio.var_copy.set(True)
    audio.var_same_dir.set(False)
    audio.var_out_dir.set(str(out))
    audio.log.clear()
    done = run_tab(audio, root)
    o = Out(audio.table.items[0].job.out) if audio.table.items[0].job else None
    check("音频：从视频里无损提取音轨", done and o and o.exists
          and o.acodec == "aac" and "无损提取" in log_text(audio),
          "%s | %s" % (o.describe() if o and o.exists else "无输出",
                       audio.table.items[0].status))

    # ---- video: merge two clips
    video.table.clear()
    add_files(video, [VID / "h264_1080p30.mp4", VID / "hevc_1080p30.mp4"], root)
    video.var_mode.set("concat")
    pump(root, seconds=0.3)
    video.cmb_fmt.set_key("mkv")
    video.cmb_quality.set_key("smart")
    video.var_same_dir.set(False)
    video.var_out_dir.set(str(out))
    video.log.clear()
    done = run_tab(video, root, timeout=900)
    job = next((i.job for i in video.table.items if i.job), None)
    if done and job is not None and job.out.exists():
        o = Out(job.out)
        want = 40.0
        check("视频：拼接两个不同编码的片段", abs(o.duration - want) <= 1.0,
              "时长 %.2fs（期望约 %.0fs）编码 %s 状态 %s"
              % (o.duration, want, o.vcodec, video.table.items[0].status))
        check("视频：拼接时两行共享同一个任务",
              len({id(i.job) for i in video.table.items}) == 1)
    else:
        check("视频：拼接两个不同编码的片段", False, "没有产出")
    video.var_mode.set("convert")
    pump(root, seconds=0.2)


# ------------------------------------------------- G5 sorting and dragging

def g5_sorting(app, root) -> None:
    section("G5) 媒体池：排序、上移下移、拖动改顺序")
    video, _image, _audio = app.tabs
    video.table.clear()
    d = temp_dir("G5")
    names = ["pic10.mp4", "pic2.mp4", "pic1.mp4", "record (10).mp4", "B.mp4"]
    sizes = {"pic10.mp4": 1000, "pic2.mp4": 3000, "pic1.mp4": 500,
             "record (10).mp4": 2000, "B.mp4": 4000}
    for n in names:
        p = d / n
        shutil.copy2(VID / "h264_1080p30.mov", p)
    # give them different sizes so size sorting is observable
    for n, target in sizes.items():
        p = d / n
        with open(p, "r+b") as fh:
            fh.truncate(target)

    video._accept([str(d / n) for n in names])
    pump(root, until=lambda: all(i.info is not None for i in video.table.items),
         timeout=180)

    def order() -> list[str]:
        return [Path(i.path).name for i in video.table.items]

    def shown() -> list[str]:
        return [video.table.tree.set(i.row_id, "index")
                for i in video.table.items]

    video.var_sort.set("文件名")
    video.var_sort_desc.set(False)
    video._apply_sort()
    pump(root, seconds=0.2)
    check("按文件名排序：数字感知（pic2 在 pic10 前）",
          order().index("pic2.mp4") < order().index("pic10.mp4"),
          " → ".join(order()))
    check("序号列与顺序一致", shown() == [str(i + 1) for i in range(5)],
          "、".join(shown()))

    video.var_sort.set("文件大小")
    video.var_sort_desc.set(True)
    video.btn_sort_dir.configure(text="降序")
    video._apply_sort()
    pump(root, seconds=0.2)
    big_first = order()[0] == "B.mp4"
    increasing = [sizes[n] for n in order()]
    check("按大小降序", big_first and increasing == sorted(increasing, reverse=True),
          " → ".join("%s(%d)" % (n, sizes[n]) for n in order()))

    video.var_sort.set("原始加入顺序")
    video.var_sort_desc.set(False)
    video._apply_sort()
    pump(root, seconds=0.2)
    check("按原始加入顺序还原", order() == names, " → ".join(order()))
    video._apply_sort()

    # 上移 / 下移 buttons, driven through the real selection
    video.table.tree.selection_set([video.table.items[0].row_id])
    pump(root, seconds=0.1)
    before = order()
    video.table.move(1)
    pump(root, seconds=0.1)
    check("「下移」把选中行往后挪一位",
          order()[1] == before[0] and order()[0] == before[1],
          "%s -> %s" % (before[:2], order()[:2]))
    video.table.move(-1)
    pump(root, seconds=0.1)
    check("「上移」还原", order() == before, " → ".join(order()))

    # real drag: press row 0, move to row 2, release
    tree = video.table.tree
    root.deiconify()
    pump(root, seconds=0.4)
    mapping = {Path(i.path).name: i.row_id for i in video.table.items}
    moving = order()[0]
    b0 = tree.bbox(mapping[moving])
    b2 = tree.bbox(mapping[order()[2]])
    if b0 and b2:
        tree.event_generate("<ButtonPress-1>", x=b0[0] + 20, y=b0[1] + b0[3] // 2)
        pump(root, seconds=0.05)
        tree.event_generate("<B1-Motion>", x=b2[0] + 20, y=b2[1] + b2[3] // 2)
        pump(root, seconds=0.05)
        tree.event_generate("<ButtonRelease-1>", x=b2[0] + 20, y=b2[1] + b2[3] // 2)
        pump(root, seconds=0.3)
        now = order()
        check("拖动第 1 行到第 3 行", now[2] == moving,
              "拖动 %s，结果 %s" % (moving, " → ".join(now)))
        check("拖动后序号重排", shown() == [str(i + 1) for i in range(5)],
              "、".join(shown()))
        check("拖动后顺序与列表一致",
              [Path(video.table.items[i].path).name
               for i in range(5)] == now)
    else:
        check("拖动第 1 行到第 3 行", False, "拿不到行坐标（窗口未映射）")

    # removing and clearing
    video.table.tree.selection_set([video.table.items[0].row_id])
    n = len(video.table.items)
    video.table.remove_selected()
    pump(root, seconds=0.2)
    check("移除所选", len(video.table.items) == n - 1,
          "%d -> %d" % (n, len(video.table.items)))
    video.table.clear()
    pump(root, seconds=0.2)
    check("清空", not video.table.items and not video.table.tree.get_children())


# ------------------------------------------------------------ G6 timeline

def g6_timeline(app, root) -> None:
    section("G6) 剪辑时间轴：缩略图、拖动选区、预览、播放")
    video, _i, _a = app.tabs
    video.table.clear()
    add_files(video, [VID / "h264_1080p30.mp4"], root)
    root.deiconify()
    pump(root, seconds=1.0)
    bar = video.trimbar
    check("加载后时间轴可用", bar.has_media, "时长 %.2f" % (bar._duration or 0))

    # thumbnails arrive from a worker thread
    pump(root, until=lambda: len(bar._thumbs) > 0, timeout=60)
    check("胶片条缩略图已抽出", len(bar._thumbs) > 0, "%d 张" % len(bar._thumbs))

    # preview frame follows the playhead
    bar.set_range(1.0, 3.0)
    pump(root, seconds=0.2)
    start, end = bar.get_range()
    check("设置选区后能读回", abs((start or 0) - 1.0) < 0.05
          and abs((end or 0) - 3.0) < 0.05, "%s → %s" % (start, end))
    # Dragging a handle is what pushes the numbers into the time fields:
    # set_range() is deliberately one-way, mirroring typed values into the
    # timeline, so the two cannot chase each other in a loop.
    canvas = bar.canvas
    y = int(canvas.winfo_height() / 2)
    x_end = int(bar._time_to_x(bar._end))
    x_target = int(bar._time_to_x(2.0))
    canvas.event_generate("<ButtonPress-1>", x=x_end, y=y)
    pump(root, seconds=0.05)
    canvas.event_generate("<B1-Motion>", x=x_target, y=y)
    pump(root, seconds=0.05)
    canvas.event_generate("<ButtonRelease-1>", x=x_target, y=y)
    pump(root, seconds=0.3)
    s2, e2 = bar.get_range()
    check("拖动结束端能改选区", abs((e2 or 0) - 2.0) < 0.15,
          "%.2f -> %.2f" % (s2 or 0, e2 or 0))
    check("拖动后时间文本跟着变",
          video.var_t_start.get().startswith("1.0")
          and video.var_t_end.get().startswith(("2.0", "1.9")),
          "%s / %s" % (video.var_t_start.get(), video.var_t_end.get()))

    video.var_t_start.set("2.00")
    video.var_t_end.set("4.00")
    pump(root, seconds=0.3)
    start, end = bar.get_range()
    check("改文本时间轴跟着变", abs((start or 0) - 2.0) < 0.05
          and abs((end or 0) - 4.0) < 0.05, "%s → %s" % (start, end))

    # moving the playhead must produce a preview frame
    bar._playhead = 2.0
    bar._move_playhead()
    pump(root, until=lambda: bar._preview_img is not None, timeout=30)
    check("播放头处能取到预览画面", bar._preview_img is not None,
          "时间标签 %s" % bar.var_clock.get())
    check("预览时间标签是「当前/总长」", "/" in bar.var_clock.get(),
          bar.var_clock.get())

    # quick select
    video.cmb_scale.set_key("none")
    bar._quick("from_here")
    pump(root, seconds=0.3)
    s, e = bar.get_range()
    check("快捷选取「从当前位置起」", s is not None and e is not None
          and e > s, "%.2f → %.2f" % (s or -1, e or -1))

    # the plan must honour what the timeline shows
    job = video.plan_one(video.table.items[0], None, set(),
                         video.capture_options())
    check("时间轴选区进入计划", job.expected and job.expected > 0,
          "%s | %s" % (job.note, "%.2fs" % (job.expected or 0)))
    job.cleanup()

    bar.clear_range()
    pump(root, seconds=0.2)
    check("清除选区后文本框清空",
          video.var_t_start.get() == "" and video.var_t_end.get() == "",
          "%r / %r" % (video.var_t_start.get(), video.var_t_end.get()))

    # play / stop
    bar.toggle_play()
    pump(root, seconds=1.2)
    playing = bar._playing
    bar.toggle_play()
    pump(root, seconds=0.5)
    check("窗口内播放能启动并停止", playing and not bar._playing,
          "播放中=%s -> %s" % (playing, bar._playing))


# ------------------------------------------------ G7 cancellation and keys

def g7_cancel_and_keys(app, root) -> None:
    section("G7) 取消、快捷键、错误处理")
    video, image, _a = app.tabs

    # a slow job so there is time to cancel
    video.table.clear()
    add_files(video, [VID / "vp9_1080p30.webm"], root)
    video.cmb_fmt.set_key("webm")
    video.cmb_quality.set_key("high")
    d = temp_dir("G7")
    video.var_same_dir.set(False)
    video.var_out_dir.set(str(d))
    video.log.clear()
    video.start()
    pump(root, until=lambda: video.engine is not None
         and video.engine.is_running, timeout=30)
    pump(root, seconds=1.2)
    progress_seen = video.var_progress.get()
    root.event_generate("<Escape>")
    pump(root, seconds=0.3)
    pump(root, until=lambda: video._runner is None, timeout=90)
    item = video.table.items[0]
    leftover = item.job.out.exists() if item.job else False
    check("Esc 能取消正在跑的任务", item.status == "已取消", item.status)
    check("取消后不留下半截输出文件", not leftover, str(item.job.out))
    check("运行中状态栏显示进度而不是「就绪」", progress_seen != "就绪",
          progress_seen[:70])
    check("取消有日志说明", "取消" in log_text(video),
          log_text(video).strip().splitlines()[-1][:60]
          if log_text(video).strip() else "无")

    # F5 starts a run
    video.table.clear()
    add_files(video, [VID / "h264_1080p30.mp4"], root)
    video.cmb_fmt.set_key("mkv")
    video.cmb_quality.set_key("smart")
    pump(root, seconds=0.3)
    root.event_generate("<F5>")
    pump(root, until=lambda: video._runner is not None, timeout=20)
    started = video._runner is not None
    pump(root, until=lambda: video._runner is None, timeout=180)
    check("F5 开始转换", started and video.table.items[0].status == "完成",
          video.table.items[0].status)

    # Ctrl+L clears the log
    root.event_generate("<Control-l>")
    pump(root, seconds=0.3)
    check("Ctrl+L 清空日志", log_text(video).strip() == "",
          repr(log_text(video)[:40]))

    # a broken file must fail visibly, not silently
    video.table.clear()
    video._accept([str(CORPUS / "odd" / "text_renamed.mp4")])
    pump(root, until=lambda: all(i.info is not None for i in video.table.items),
         timeout=90)
    item = video.table.items[0]
    unreadable = item.info is None or bool(item.info.warn)
    check("假视频被识别为无法读取", unreadable,
          (item.info.warn if item.info and item.info.warn else "info=None"))
    video.log.clear()
    video.start()
    pump(root, seconds=0.8)
    check("无法读取的文件不会静默通过",
          item.status in ("失败", "等待") or "未就绪" in log_text(video)
          or "无法" in log_text(video), item.status)
    if video._runner is not None:
        video.cancel()
        pump(root, until=lambda: video._runner is None, timeout=60)

    # an empty file
    video.table.clear()
    video._accept([str(CORPUS / "odd" / "empty.mp4")])
    pump(root, seconds=1.5)
    check("空文件被拒或被标为不可读",
          not video.table.items or video.table.items[0].info is None
          or bool(video.table.items[0].info.warn),
          video.table.items[0].status if video.table.items else "未加入")

    # a truncated file: readable header, broken tail
    video.table.clear()
    d = temp_dir("G7b")
    add_files(video, [CORPUS / "odd" / "truncated.mp4"], root)
    video.cmb_fmt.set_key("mkv")
    video.cmb_quality.set_key("balanced")
    video.var_same_dir.set(False)
    video.var_out_dir.set(str(d))
    video.log.clear()
    before = len(gui_run.DIALOGS) if False else 0
    run_tab(video, root, timeout=600)
    item = video.table.items[0]
    shown = video.table.tree.set(item.row_id, "info")
    refused = item.info is None and item.status == "失败"
    check("截断的文件：行内说明原因，且不会静默失败",
          refused and "读取失败" in shown,
          "状态=%s 信息列=%r" % (item.status, shown[:70]))

    # output location: same folder as the source
    video.table.clear()
    src = d / "same_dir_test.mp4"
    shutil.copy2(VID / "h264_1080p30.mp4", src)
    add_files(video, [src], root)
    video.cmb_fmt.set_key("mkv")
    video.cmb_quality.set_key("smart")
    video.var_same_dir.set(True)
    pump(root, seconds=0.3)
    run_tab(video, root)
    job = video.table.items[0].job
    check("输出到源文件所在目录",
          job is not None and Path(job.out).parent == d
          and Path(job.out).name == "same_dir_test.mkv",
          str(job.out) if job else "无")

    for tab in app.tabs:
        tab.table.clear()


# --------------------------------------------------------- G8 temp cleanup

def g8_housekeeping(app, root) -> None:
    section("G8) 临时目录清理（强杀残留）")
    from trans.ui import trim_bar
    tmp = Path(os.environ.get("TEMP", "C:/Windows/Temp"))
    stale = tmp / "trans_thumbs_stale_test"
    fresh = tmp / "trans_thumbs_fresh_test"
    for d in (stale, fresh):
        d.mkdir(exist_ok=True)
        (d / "x.png").write_bytes(b"x")
    old = time.time() - 48 * 3600
    os.utime(stale, (old, old))
    trim_bar._SWEPT = False
    removed = trim_bar.sweep_stale_temp()
    check("启动清理会删掉超过 24 小时的残留目录",
          removed >= 1 and not stale.exists(), "清理了 %d 个" % removed)
    check("刚产生的临时目录不会被误删", fresh.exists())
    shutil.rmtree(fresh, ignore_errors=True)


