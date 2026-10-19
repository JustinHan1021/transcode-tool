"""Image tab sweep.

Colour is checked by decoding the output back to pixels, not by trusting the
command: "透明区域已填充白色" is only true if the pixel really is white, and
PNG's quality slider is only harmless if the pixels come back identical.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import (CAPS, CORPUS, FFMPEG, Out, begin, check, cleanup,  # noqa: E402
                     info_of, run_job, summary, temp_dir)

from PIL import Image, ImageSequence  # noqa: E402
from trans import geometry as G, plan  # noqa: E402
from trans.presets import ImageOptions, SequenceOptions, FRAME_DELAYS  # noqa: E402

IMG = CORPUS / "image"


def pixels(path: Path) -> Image.Image | None:
    """Decode the first frame to RGB(A) for inspection."""
    try:
        im = Image.open(path)
        im.load()
        return im.convert("RGBA")
    except Exception:
        return None


def px_at(im: Image.Image, x: int, y: int) -> tuple[int, int, int, int]:
    return im.getpixel((x, y))


# ------------------------------------------------------------- A: containers

def test_containers() -> None:
    begin("A) 图片格式转换矩阵（每个源 × 每个输出格式）")
    sources = ["i_1920x1080.png", "i_opaque.jpg", "i.webp", "i.bmp", "i.tiff",
               "i_small.png", "odd/one_pixel.png"]
    out_dir = temp_dir("IA")
    for name in sources:
        src = IMG / name if not name.startswith("odd/") else CORPUS / name
        if not src.exists():
            continue
        for container in ("jpg", "png", "webp", "bmp", "tiff", "gif"):
            info = info_of(src)
            opts = ImageOptions(container=container, quality=88)
            job = plan.plan_image(info, out_dir, opts)
            res = run_job(job)
            label = "%-16s -> %-5s" % (Path(name).name, container)
            if not res.ok:
                check(label, False, (res.error or "失败").splitlines()[-1][:100])
                job.cleanup()
                continue
            o = Out(job.out)
            im = pixels(job.out)
            problem = ""
            if im is None:
                problem = "输出无法解出像素"
            elif o.size == 0:
                problem = "空文件"
            elif (container != "gif"
                  and (im.width, im.height) != (info.width, info.height)):
                # GIF is capped at 480px on purpose; every other format keeps
                # the source size when no size is asked for.
                problem = "尺寸变了 %dx%d -> %dx%d" % (info.width, info.height,
                                                      im.width, im.height)
            check(label, not problem,
                  "%dx%d %s %.1fKB %s" % (o.width, o.height, o.vcodec,
                                          o.size / 1024, problem))
            job.cleanup()


# ----------------------------------------------------------- B: transparency

def test_transparency() -> None:
    begin("B) 透明处理（不能出现黑块/红块）")
    src = IMG / "i_rgba.png"
    out_dir = temp_dir("IB")
    src_im = pixels(src)
    src_corner = px_at(src_im, 2, 2)
    print("  源 i_rgba.png 左上角像素 RGBA = %s（应为全透明）" % (src_corner,))

    # PNG -> JPEG: the transparent corner must become the chosen background.
    info = info_of(src)
    job = plan.plan_image(info, out_dir, ImageOptions(container="jpg", quality=90))
    res = run_job(job)
    if res.ok:
        im = pixels(job.out)
        corner = px_at(im, 2, 2)[:3]
        white = all(c > 240 for c in corner)
        check("透明 PNG -> JPEG 自动铺白底", white,
              "左上角得到 RGB%s，警告: %s" % (corner, job.warnings))
    else:
        check("透明 PNG -> JPEG 自动铺白底", False, res.error[:100])
    job.cleanup()

    # A custom background colour must be honoured.
    info = info_of(src)
    job = plan.plan_image(info, out_dir, ImageOptions(container="jpg", quality=90,
                                                      background="red"))
    res = run_job(job)
    if res.ok:
        im = pixels(job.out)
        corner = px_at(im, 2, 2)[:3]
        reddish = corner[0] > 200 and corner[1] < 80 and corner[2] < 80
        check("可指定其他背景色（红）", reddish, "左上角得到 RGB%s" % (corner,))
    else:
        check("可指定其他背景色（红）", False, res.error[:100])
    job.cleanup()

    # PNG / WebP must keep the alpha channel intact.
    for container in ("png", "webp"):
        info = info_of(src)
        job = plan.plan_image(info, out_dir, ImageOptions(container=container,
                                                          quality=90))
        res = run_job(job)
        if res.ok:
            im = pixels(job.out)
            corner = px_at(im, 2, 2)
            center = px_at(im, im.width // 2, im.height // 2)
            ok = corner[3] < 20 and center[3] > 200
            check("%s 保留透明通道" % container.upper(), ok,
                  "角落 alpha=%d 中心 alpha=%d" % (corner[3], center[3]))
        else:
            check("%s 保留透明通道" % container.upper(), False, res.error[:100])
        job.cleanup()


# ------------------------------------------------------------------ C: EXIF

def test_exif() -> None:
    begin("C) 手机照片 EXIF 方向（旋正一次，不能两次）")
    src = IMG / "photo_rot.jpg"
    info = info_of(src)
    print("  源: 1200x800 存储 + 方向标签 6（要求顺时针 90°）")
    print("  探测结果: %dx%d  exif_rotated(解码器已代劳)=%s"
          % (info.width, info.height, info.exif_rotated))
    out_dir = temp_dir("IC")
    for container in ("jpg", "png", "webp"):
        info = info_of(src)
        job = plan.plan_image(info, out_dir, ImageOptions(container=container,
                                                          quality=90))
        res = run_job(job)
        label = "EXIF 照片 -> %s" % container
        if not res.ok:
            check(label, False, (res.error or "失败").splitlines()[-1][:100])
            job.cleanup()
            continue
        o = Out(job.out)
        # Upright means portrait: the long edge is vertical.
        check(label, o.dims == (800, 1200),
              "期望 800x1200（竖正）得到 %dx%d" % o.dims)
        job.cleanup()

    # The written file must not carry a stale orientation tag.
    info = info_of(src)
    job = plan.plan_image(info, out_dir, ImageOptions(container="jpg", quality=90,
                                                      keep_metadata=True))
    res = run_job(job)
    if res.ok:
        try:
            ex = Image.open(job.out).getexif()
            ori = ex.get(274, 1)
        except Exception:
            ori = -1
        check("保留元数据时不会留下旧方向标签", ori in (1, None),
              "输出 EXIF 方向 = %s" % ori)
    else:
        check("保留元数据时不会留下旧方向标签", False, res.error[:100])
    job.cleanup()


# ---------------------------------------------------------------- D: quality

def test_quality() -> None:
    begin("D) 品质滑块的作用（JPEG/WebP 变体积，PNG 必须像素无损）")
    src = IMG / "i_1920x1080.png"
    out_dir = temp_dir("ID")
    sizes = {}
    for q in (10, 50, 95):
        info = info_of(src)
        job = plan.plan_image(info, out_dir, ImageOptions(container="jpg", quality=q))
        res = run_job(job)
        if res.ok:
            sizes[q] = Out(job.out).size
        job.cleanup()
    if len(sizes) == 3:
        check("JPEG 品质越低体积越小", sizes[10] < sizes[50] < sizes[95],
              "10→%.0fKB 50→%.0fKB 95→%.0fKB"
              % (sizes[10] / 1024, sizes[50] / 1024, sizes[95] / 1024))
    else:
        check("JPEG 品质越低体积越小", False, "有失败的转换")

    # PNG: compression level only, pixels must be bit-identical to the source.
    src_im = pixels(src)
    for q in (1, 100):
        info = info_of(src)
        job = plan.plan_image(info, out_dir, ImageOptions(container="png", quality=q))
        res = run_job(job)
        if not res.ok:
            check("PNG 品质=%d 像素无损" % q, False, res.error[:100])
            job.cleanup()
            continue
        im = pixels(job.out)
        same = im.tobytes() == src_im.tobytes()
        check("PNG 品质=%d 像素无损" % q, same,
              "像素%s" % ("完全一致" if same else "不一致"))
        job.cleanup()

    # WebP lossless switch.
    info = info_of(src)
    job = plan.plan_image(info, out_dir, ImageOptions(container="webp", quality=95,
                                                      lossless=True))
    res = run_job(job)
    if res.ok:
        im = pixels(job.out)
        check("WebP 无损模式像素一致", im.tobytes() == src_im.tobytes(),
              "%.0fKB" % (Out(job.out).size / 1024))
    else:
        check("WebP 无损模式像素一致", False, res.error[:100])
    job.cleanup()


# ------------------------------------------------------------- E: animations

def test_animation() -> None:
    begin("E) 动图：GIF/WebP 输入转出时是否保留动画")
    out_dir = temp_dir("IE")
    for src_name, frames_hint in [("anim.gif", 20), ("anim.webp", 20)]:
        src = IMG / src_name
        for container in ("gif", "webp"):
            info = info_of(src)
            job = plan.plan_image(info, out_dir, ImageOptions(container=container,
                                                              quality=80))
            res = run_job(job)
            label = "%-10s -> %-5s 保留动画" % (src_name, container)
            if not res.ok:
                check(label, False, (res.error or "失败").splitlines()[-1][:100])
                job.cleanup()
                continue
            try:
                n = sum(1 for _ in ImageSequence.Iterator(Image.open(job.out)))
            except Exception:
                n = 0
            check(label, n > 1, "输出帧数 = %d（源约 %d 帧）" % (n, frames_hint))
            job.cleanup()
        # To a still format: first frame only, and it must work.
        info = info_of(src)
        job = plan.plan_image(info, out_dir, ImageOptions(container="jpg", quality=85))
        res = run_job(job)
        label = "%-10s -> jpg 取第一帧" % src_name
        check(label, res.ok and Out(job.out).size > 0,
              (res.error or "OK")[:80] if not res.ok else "%.0fKB"
              % (Out(job.out).size / 1024))
        job.cleanup()

    # The "保留动图" switch turned off: one frame.
    info = info_of(IMG / "anim.gif")
    job = plan.plan_image(info, out_dir, ImageOptions(container="gif", quality=80,
                                                      animated=False))
    res = run_job(job)
    if res.ok:
        n = sum(1 for _ in ImageSequence.Iterator(Image.open(job.out)))
        check("关掉「保留动图」后只出一帧", n == 1, "输出 %d 帧" % n)
    else:
        check("关掉「保留动图」后只出一帧", False, res.error[:100])
    job.cleanup()


# ------------------------------------------------------------- F: gif sizing

def test_gif_sizing() -> None:
    begin("F) 图片转 GIF 的尺寸保护")
    out_dir = temp_dir("IF")
    for name, expect_max in [("i_1920x1080.png", 480), ("i_huge.png", 480),
                             ("i_small.png", 64)]:
        src = IMG / name
        info = info_of(src)
        job = plan.plan_image(info, out_dir, ImageOptions(container="gif",
                                                          quality=85))
        res = run_job(job)
        label = "%-18s -> GIF 自动限尺寸" % name
        if not res.ok:
            check(label, False, (res.error or "失败").splitlines()[-1][:100])
            job.cleanup()
            continue
        o = Out(job.out)
        longest = max(o.dims)
        want = min(expect_max, max(info.width, info.height))
        check(label, longest == want,
              "源 %dx%d -> 输出 %dx%d，体积 %.0fKB，%s"
              % (info.width, info.height, o.width, o.height, o.size / 1024,
                 job.warnings[0] if job.warnings else "无警告"))
        job.cleanup()

    # Explicit size must win over the cap.
    src = IMG / "i_1920x1080.png"
    info = info_of(src)
    job = plan.plan_image(info, out_dir, ImageOptions(
        container="gif", quality=85,
        scale=G.ScaleSpec(mode="width", width=800)))
    res = run_job(job)
    if res.ok:
        o = Out(job.out)
        check("指定尺寸优先于自动上限", o.width == 800,
              "输出 %dx%d" % o.dims)
    else:
        check("指定尺寸优先于自动上限", False, res.error[:100])
    job.cleanup()

    # A huge image must stay big when the target is JPEG.
    src = IMG / "i_huge.png"
    info = info_of(src)
    job = plan.plan_image(info, out_dir, ImageOptions(container="jpg", quality=85))
    res = run_job(job)
    if res.ok:
        o = Out(job.out)
        check("JPEG 不做自动限尺寸（8256x5504 保持原样）", o.width == 8256,
              "输出 %dx%d %.1fMB" % (o.width, o.height, o.size / 1048576))
    else:
        check("JPEG 不做自动限尺寸", False, res.error[:100])
    job.cleanup()


# ------------------------------------------------------------- G: crop/scale

def test_geometry() -> None:
    begin("G) 图片裁剪 + 缩放（先裁后缩，尺寸必须精确）")
    src = IMG / "i_1920x1080.png"
    out_dir = temp_dir("IG")
    cases = [
        ("裁 1:1 再缩到 800x800",
         G.CropSpec(mode="aspect", aspect_w=1, aspect_h=1),
         G.ScaleSpec(mode="exact", width=800, height=800), (800, 800)),
        ("裁 9:16 再缩到 1080x1920",
         G.CropSpec(mode="aspect", aspect_w=9, aspect_h=16),
         G.ScaleSpec(mode="exact", width=1080, height=1920), (1080, 1920)),
        ("只裁 4:3",
         G.CropSpec(mode="aspect", aspect_w=4, aspect_h=3), None, (1440, 1080)),
        ("手动裁 500x300 @(10,20)",
         G.CropSpec(mode="manual", x=10, y=20, w=500, h=300), None, (500, 300)),
        ("缩到最长边 320", None, G.ScaleSpec(mode="longest", longest=320),
         (320, 180)),
    ]
    for label, crop, scale, expect in cases:
        info = info_of(src)
        job = plan.plan_image(info, out_dir, ImageOptions(container="png", crop=crop,
                                                          scale=scale))
        res = run_job(job)
        if not res.ok:
            check(label, False, (res.error or "失败").splitlines()[-1][:100])
            job.cleanup()
            continue
        im = pixels(job.out)
        check(label, im is not None and (im.width, im.height) == expect,
              "期望 %dx%d 得到 %s" % (expect[0], expect[1],
                                      "%dx%d" % (im.width, im.height) if im else "无"))
        job.cleanup()


# ------------------------------------------------------------- H: sequence

def test_sequence() -> None:
    begin("H) 多图合成动图（序号顺序、每帧延时、循环、颜色数、尺寸不一）")
    out_dir = temp_dir("IH")
    seq = [IMG / f"seq{i:02d}.png" for i in range(1, 6)]
    infos = [info_of(p) for p in seq]
    print("  5 张不同尺寸的图: %s"
          % ", ".join("%dx%d" % (i.width, i.height) for i in infos))

    # Exact delay per frame.
    for delay in (0.05, 0.2, 0.5, 1.0, 2.0, 5.0):
        opts = SequenceOptions(container="gif", delay=delay, loop=0, max_colors=128)
        job = plan.plan_image_sequence(infos, out_dir, opts)
        res = run_job(job)
        label = "合成 GIF 每帧 %.2f 秒" % delay
        if not res.ok:
            check(label, False, (res.error or "失败").splitlines()[-1][:110])
            job.cleanup()
            continue
        try:
            im = Image.open(job.out)
            delays = [f.info.get("duration", 0) for f in ImageSequence.Iterator(im)]
        except Exception as exc:
            check(label, False, "无法读取延时: %s" % exc)
            job.cleanup()
            continue
        want_ms = delay * 1000
        worst = max(abs(d - want_ms) for d in delays) if delays else 999
        check(label, len(delays) == 5 and worst <= 10,
              "帧数=%d 各帧延时(ms)=%s 期望 %.0fms"
              % (len(delays), delays[:6], want_ms))
        job.cleanup()

    # Mixed sizes must all come out the same size.
    info = info_of(seq[0])
    opts = SequenceOptions(container="gif", delay=0.5, max_colors=256)
    job = plan.plan_image_sequence(infos, out_dir, opts)
    res = run_job(job)
    if res.ok:
        im = Image.open(job.out)
        dims = {(f.width, f.height) for f in ImageSequence.Iterator(im)}
        check("尺寸不一的图合成后整齐统一", len(dims) == 1,
              "各帧尺寸 = %s，note=%s" % (dims, job.note))
    else:
        check("尺寸不一的图合成后整齐统一", False, res.error[:110])
    job.cleanup()

    # Loop count.
    for loop in (0, 1, 3):
        opts = SequenceOptions(container="gif", delay=0.3, loop=loop)
        job = plan.plan_image_sequence(infos, out_dir, opts)
        res = run_job(job)
        label = "循环设置 = %d" % loop
        if not res.ok:
            check(label, False, (res.error or "失败").splitlines()[-1][:100])
            job.cleanup()
            continue
        try:
            got = Image.open(job.out).info.get("loop", None)
        except Exception:
            got = None
        check(label, got == loop, "文件里记录的循环 = %s" % got)
        job.cleanup()

    # Animated WebP output.
    opts = SequenceOptions(container="webp", delay=0.5, quality=80)
    job = plan.plan_image_sequence(infos, out_dir, opts)
    res = run_job(job)
    if res.ok:
        o = Out(job.out)
        check("合成动图 WebP", o.vcodec in ("webp", "webp_anim") and o.size > 0,
              "%s %dx%d %.0fKB" % (o.vcodec, o.width, o.height, o.size / 1024))
    else:
        check("合成动图 WebP", False, res.error[:110])
    job.cleanup()

    # Explicit size on the sequence.
    opts = SequenceOptions(container="gif", delay=0.5,
                           scale=G.ScaleSpec(mode="exact", width=400, height=300))
    job = plan.plan_image_sequence(infos, out_dir, opts)
    res = run_job(job)
    if res.ok:
        o = Out(job.out)
        check("合图可指定输出尺寸", o.dims == (400, 300), "输出 %dx%d" % o.dims)
    else:
        check("合图可指定输出尺寸", False, res.error[:110])
    job.cleanup()

    # One picture is not a sequence.
    job = plan.plan_image_sequence(infos[:1], out_dir, SequenceOptions())
    check("只有一张图时明确提示", any("至少" in w for w in job.warnings),
          "%s" % job.warnings)
    job.cleanup()

    # Colour count has to actually shrink the palette -- which needs frames with
    # more than 16 colours in them, so the flat test cards will not show it.
    gradient = []
    for i in range(3):
        src = CORPUS / "cache" / ("grad%d.png" % i)
        if not src.exists():
            import subprocess
            subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i",
                            "testsrc2=s=800x600:d=0.1", "-frames:v", "1",
                            str(src)], check=True)
        gradient.append(info_of(src))
    sizes = {}
    for colors in (16, 256):
        opts = SequenceOptions(container="gif", delay=0.5, max_colors=colors)
        job = plan.plan_image_sequence(gradient, out_dir, opts)
        res = run_job(job)
        if res.ok:
            sizes[colors] = Out(job.out).size
        job.cleanup()
    if len(sizes) == 2:
        check("颜色数变少体积变小", sizes[16] < sizes[256],
              "16色 %.0fKB vs 256色 %.0fKB"
              % (sizes[16] / 1024, sizes[256] / 1024))
    else:
        check("颜色数变少体积变小", False, "有失败的转换")

    # The manifest of supported delays must all be whole hundredths.
    bad = [d for d in FRAME_DELAYS if abs(d * 100 - round(d * 100)) > 0.001]
    check("每帧延时都是整百分之一秒", not bad, "异常值: %s" % bad)


def main() -> int:
    test_containers()
    test_transparency()
    test_exif()
    test_quality()
    test_animation()
    test_gif_sizing()
    test_geometry()
    test_sequence()
    failed = summary("图片页")
    cleanup()
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
