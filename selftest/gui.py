"""End-to-end through the real widgets.

Nothing here re-implements the app: it builds the actual Tk application, feeds
it real files, moves real dropdowns, drags real rows and presses real keys. The
point is to catch what a planner-level test cannot -- an option that exists in
the UI but cannot be planned, a control that does nothing, a routing decision
that sends a file to the wrong tab.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import CORPUS, FFMPEG, WORK, Out, info_of, temp_dir  # noqa: E402

# The repo root is wherever this file ended up, so a clone runs anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import trans.ui.app as appmod  # noqa: E402
from trans.ui.app import App  # noqa: E402

VID = CORPUS / "video"
IMG = CORPUS / "image"
AUD = CORPUS / "audio"
_ok = True


def check(label: str, ok: bool, detail: str = "") -> bool:
    global _ok
    _ok = _ok and ok
    print("  [%s] %s%s" % ("OK  " if ok else "FAIL", label,
                           ("  -- " + detail) if detail else ""))
    return ok


def section(name: str) -> None:
    print()
    print("=" * 78)
    print(name)
    print("=" * 78)


def pump(root, seconds: float = 0.0, until=None, timeout: float = 240.0) -> bool:
    """Run the Tk event loop without blocking, until `until` or a timeout."""
    t0 = time.time()
    while True:
        try:
            root.update()
        except tk.TclError:
            return False
        if until is not None:
            if until():
                return True
        elif time.time() - t0 >= seconds:
            return True
        if time.time() - t0 > timeout:
            return False
        time.sleep(0.02)


def add_files(tab, paths, root) -> None:
    tab._accept([str(p) for p in paths])
    # wait for the probe to finish: planning needs info on every row
    pump(root, until=lambda: all(i.info is not None for i in tab.table.items),
         timeout=180)


def run_tab(tab, root, timeout: float = 300.0) -> bool:
    tab.start()
    done = pump(root, until=lambda: tab._runner is None, timeout=timeout)
    pump(root, seconds=0.4)
    return done


def log_text(tab) -> str:
    return tab.log.text.get("1.0", "end")


# ---------------------------------------------------------------- G1 startup

def g1_startup(app, root) -> None:
    section("G1) 启动：三个页面都建起来，能力探测完成")
    check("三个标签页", len(app.tabs) == 3,
          "、".join(t.title for t in app.tabs))
    check("标题正确", [t.title for t in app.tabs] == ["视频", "图片", "音频"])
    check("ffmpeg 已识别", app.caps is not None and app.caps.version,
          app.caps.version if app.caps else "无")
    check("默认选中视频页", app.nb.index(app.nb.select()) == 0)
    hw = appmod.hw_encoder_summary(app.caps)
    check("硬件编码状态有文案", bool(hw), hw[:70])
    for t in app.tabs:
        check("%s 页可规划摘要" % t.title, isinstance(t.option_summary(), str),
              t.option_summary()[:60])


# ------------------------------------------------------- G2 import and routing

def g2_routing(app, root) -> None:
    section("G2) 导入与按类型路由（启动参数 / 拖放）")
    for tab in app.tabs:
        tab.table.clear()
    video, image, audio = app.tabs

    # A file valid for several pages lands on the page in front -- that is
    # deliberate: dropping a video while on the audio tab means you want its
    # audio. So routing is checked from the startup state, which is the case
    # the launcher creates.
    app.nb.select(0)
    app.add_initial_files([str(VID / "h264_1080p30.mp4")])
    pump(root, seconds=0.4)
    check("视频文件会切到视频页", app.nb.index(app.nb.select()) == 0
          and len(video.table.items) == 1, "当前页 %s"
          % app.nb.index(app.nb.select()))

    app.nb.select(0)
    app.add_initial_files([str(IMG / "i_1920x1080.png")])
    pump(root, seconds=0.4)
    check("图片文件会切到图片页", app.nb.index(app.nb.select()) == 1
          and len(image.table.items) == 1)

    app.nb.select(0)
    app.add_initial_files([str(AUD / "a.mp3")])
    pump(root, seconds=0.4)
    check("音频文件会切到音频页", app.nb.index(app.nb.select()) == 2
          and len(audio.table.items) == 1)

    # A video on the audio tab is legitimate: that is how audio is extracted.
    for tab in app.tabs:
        tab.table.clear()
    app.nb.select(0)
    app.add_initial_files([str(VID / "h264_1080p30.mp4")])
    pump(root, seconds=0.4)
    before = len(video.table.items)
    app.add_initial_files([str(VID / "mute_1080p30.mp4")])
    pump(root, seconds=0.4)
    check("视频页收下第二个视频", len(video.table.items) == before + 1,
          "%d -> %d" % (before, len(video.table.items)))

    # Wrong type must be refused with a message rather than accepted.
    image.log.clear()
    taken = image._accept([str(VID / "h264_1080p30.mp4")])
    pump(root, seconds=0.2)
    msg = log_text(image)
    check("图片页拒收视频并说明原因", not taken and "只接受" in msg,
          msg.strip().splitlines()[-1][:80] if msg.strip() else "无提示")

    # A folder: only the matching types come in.
    for tab in app.tabs:
        tab.table.clear()
    image._accept([str(IMG)])
    pump(root, until=lambda: all(i.info is not None for i in image.table.items),
         timeout=180)
    exts = {Path(i.path).suffix.lower() for i in image.table.items}
    check("添加文件夹时按扩展名过滤", exts <= {".png", ".jpg", ".jpeg", ".webp",
                                             ".bmp", ".tif", ".tiff", ".gif"},
          "%d 个文件，类型 %s" % (len(image.table.items), sorted(exts)))

    # Duplicates must not be added twice.
    image.table.clear()
    odd = CORPUS / "odd"
    n = 0
    image._accept([str(IMG / "i_small.png"), str(IMG / "i_small.png")])
    pump(root, seconds=0.3)
    check("同一文件不会重复加入", len(image.table.items) == 1,
          "%d 个" % len(image.table.items))
    n = len(image.table.items)
    image._accept([str(IMG / "i_small.png")])
    pump(root, seconds=0.3)
    check("再次加入同一文件仍然无效", len(image.table.items) == n,
          "%d -> %d" % (n, len(image.table.items)))
    for tab in app.tabs:
        tab.table.clear()
    pump(root, seconds=0.2)


# ------------------------------------------------ G3 every dropdown is plannable

def g3_plan_all_options(app, root) -> None:
    section("G3) 每一种下拉取值都必须能生成方案（UI → 计划 全枚举）")
    video, image, audio = app.tabs
    out = temp_dir("G3")

    # ---- video
    video.table.clear()
    video._accept([str(VID / "h264_1080p30.mp4")])
    pump(root, until=lambda: all(i.info is not None for i in video.table.items),
         timeout=120)
    item = video.table.items[0]
    fails: list[str] = []
    tried = 0

    containers = list(video._fmt_keys)
    mode_keys = [k for k, _l in video.cmb_quality._choices]
    for container in containers:
        video.cmb_fmt.set_key(container)
        for mode in mode_keys:
            video.cmb_quality.set_key(mode)
            enc_keys = [k for k, _l in video.cmb_encoder._choices]
            for enc in enc_keys:
                video.cmb_encoder.set_key(enc)
                audio_keys = [k for k, _l in video.cmb_audio._choices]
                for acodec in audio_keys:
                    video.cmb_audio.set_key(acodec)
                    for br in ("64", "192", "320"):
                        video.cmb_abr.set_key(br)
                        tried += 1
                        try:
                            opts = video.capture_options()
                            job = video.plan_one(item, out, set(), opts)
                            if not job.steps and not job.warnings:
                                fails.append("%s/%s/%s/%s/%s 无步骤无说明"
                                             % (container, mode, enc, acodec, br))
                        except Exception as exc:
                            fails.append("%s/%s/%s/%s/%s -> %s: %s"
                                         % (container, mode, enc, acodec, br,
                                            type(exc).__name__, exc))
    check("视频：格式×画质×编码器×音频×码率 全部可规划",
          not fails, "%d 种组合" % tried if not fails
          else "失败 %d 种，例如 %s" % (len(fails), fails[0][:90]))
    video.cmb_fmt.set_key("mp4")
    video.cmb_quality.set_key("smart")
    video.cmb_encoder.set_key("auto")
    video.cmb_audio.set_key("auto")

    # scale modes / aspects / gif settings
    fails = []
    tried = 0
    for mode in [k for k, _l in video.cmb_scale._choices]:
        video.cmb_scale.set_key(mode)
        for label in [k for k, _l in video.cmb_aspect._choices]:
            video.cmb_aspect.set_key(label)
            tried += 1
            try:
                job = video.plan_one(item, out, set(), video.capture_options())
                if not job.steps and not job.warnings:
                    fails.append("%s/%s 无步骤无说明" % (mode, label))
            except Exception as exc:
                fails.append("%s/%s -> %s: %s" % (mode, label,
                                                  type(exc).__name__, exc))
    check("视频：尺寸方式×裁剪比例 全部可规划", not fails,
          "%d 种组合" % tried if not fails else "失败 %s" % fails[:2])
    video.cmb_scale.set_key("none")
    video.cmb_aspect.set_key("原始")

    fails = []
    for fps in [k for k, _l in video.cmb_gif_fps._choices]:
        for colors in [k for k, _l in video.cmb_gif_colors._choices]:
            video.cmb_fmt.set_key("gif")
            video.cmb_gif_fps.set_key(fps)
            video.cmb_gif_colors.set_key(colors)
            try:
                job = video.plan_one(item, out, set(), video.capture_options())
                if not job.steps:
                    fails.append("%sfps/%s色" % (fps, colors))
            except Exception as exc:
                fails.append("%s/%s -> %s" % (fps, colors, exc))
    check("视频：GIF 帧率×颜色数 全部可规划", not fails, str(fails[:3]))
    video.cmb_fmt.set_key("mp4")

    # ---- image
    image.table.clear()
    image._accept([str(IMG / "i_1920x1080.png")])
    pump(root, until=lambda: all(i.info is not None for i in image.table.items),
         timeout=120)
    item = image.table.items[0]
    fails = []
    tried = 0
    for fmt in ["jpg", "png", "webp", "bmp", "tiff", "gif"]:
        image.cmb_fmt.set_key(fmt)
        for q in (1, 50, 88, 100):
            image.var_quality.set(str(q))
            for mode in [k for k, _l in image.cmb_scale._choices]:
                image.cmb_scale.set_key(mode)
                for aspect in [k for k, _l in image.cmb_aspect._choices]:
                    image.cmb_aspect.set_key(aspect)
                    for keep in (True, False):
                        image.var_keep_meta.set(keep)
                        tried += 1
                        try:
                            job = image.plan_one(item, out, set(),
                                                 image.capture_options())
                            if not job.steps and not job.warnings:
                                fails.append("%s/%s/%s/%s" % (fmt, q, mode, aspect))
                        except Exception as exc:
                            fails.append("%s/%s/%s/%s -> %s: %s"
                                         % (fmt, q, mode, aspect,
                                            type(exc).__name__, exc))
    check("图片：格式×品质×尺寸×裁剪×元数据 全部可规划", not fails,
          "%d 种组合" % tried if not fails
          else "失败 %d，例如 %s" % (len(fails), fails[0][:80]))
    image.cmb_fmt.set_key("webp")
    image.cmb_scale.set_key("none")
    image.cmb_aspect.set_key("原始")

    # sequence mode -- needs at least two pictures, which is what the tab enforces
    image.table.clear()
    image._accept([str(IMG / f"seq{i:02d}.png") for i in range(1, 4)])
    pump(root, until=lambda: all(i.info is not None for i in image.table.items),
         timeout=120)
    seq_items = list(image.table.items)
    fails = []
    tried = 0
    for fmt in ("gif", "webp"):
        for delay in [k for k, _l in image.cmb_delay._choices]:
            for loop in [k for k, _l in image.cmb_loop._choices]:
                for colors in [k for k, _l in image.cmb_colors._choices]:
                    tried += 1
                    image.var_mode.set("sequence")
                    image.cmb_fmt.set_key(fmt)
                    image.cmb_delay.set_key(delay)
                    image.cmb_loop.set_key(loop)
                    image.cmb_colors.set_key(colors)
                    try:
                        pairs = image.plan_batch(seq_items, out, set(),
                                                 image.capture_options())
                        job = pairs[0][1]
                        if not job.steps and not job.warnings:
                            fails.append("%s/%s/%s/%s" % (fmt, delay, loop, colors))
                    except Exception as exc:
                        fails.append("%s/%s/%s/%s -> %s"
                                     % (fmt, delay, loop, colors, exc))
    check("图片：合成动图 全部设置可规划", not fails,
          "%d 种组合" % tried if not fails else "失败 %s" % fails[:3])

    # Fewer than two pictures must be refused with a readable message.
    image.var_mode.set("sequence")
    try:
        image.plan_batch(seq_items[:1], out, set(), image.capture_options())
        refused = False
        message = ""
    except ValueError as exc:
        refused = True
        message = str(exc)
    check("合成动图只有一张图时明确报错", refused and "至少" in message, message)
    image.var_mode.set("convert")
    image.table.clear()

    # ---- audio
    audio.table.clear()
    audio._accept([str(AUD / "a_48k_stereo.wav"), str(VID / "h264_1080p30.mp4")])
    pump(root, until=lambda: all(i.info is not None for i in audio.table.items),
         timeout=120)
    fails = []
    tried = 0
    for item in audio.table.items:
        for fmt in ["mp3", "m4a", "wav", "aac", "flac", "ogg", "opus"]:
            audio.cmb_fmt.set_key(fmt)
            for br in [k for k, _l in audio.cmb_br._choices]:
                audio.cmb_br.set_key(br)
                for rate in [k for k, _l in audio.cmb_rate._choices]:
                    audio.cmb_rate.set_key(rate)
                    for ch in [k for k, _l in audio.cmb_ch._choices]:
                        audio.cmb_ch.set_key(ch)
                        for loud in [k for k, _l in audio.cmb_loud._choices]:
                            audio.cmb_loud.set_key(loud)
                            tried += 1
                            try:
                                job = audio.plan_one(item, out, set(),
                                                     audio.capture_options())
                                if not job.steps and not job.warnings:
                                    fails.append("%s/%s/%s/%s/%s/%s"
                                                 % (Path(item.path).name, fmt, br,
                                                    rate, ch, loud))
                            except Exception as exc:
                                fails.append("%s %s/%s/%s/%s/%s -> %s: %s"
                                             % (Path(item.path).name, fmt, br, rate,
                                                ch, loud, type(exc).__name__, exc))
    check("音频：格式×码率×采样率×声道×响度 全部可规划", not fails,
          "%d 种组合" % tried if not fails
          else "失败 %d，例如 %s" % (len(fails), fails[0][:90]))
    audio.cmb_fmt.set_key("mp3")
    audio.cmb_br.set_key("192")
    audio.cmb_rate.set_key("0")
    audio.cmb_ch.set_key("0")
    audio.cmb_loud.set_key("off")
    for tab in app.tabs:
        tab.table.clear()
    pump(root, seconds=0.2)

