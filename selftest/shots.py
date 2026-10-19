"""Capture the screenshots the README needs.

Four real window states: the video tab with a clip loaded and a range selected,
merge mode with three clips queued, the image tab in merge-into-one-GIF mode, and
the audio tab. Files are loaded for real so the pictures show what the app looks
like in use rather than four empty windows.

`var_mode.set()` only changes the variable -- a real user click also runs the
radio button's command, which is what shows the mode's own settings and redoes
the preview. Both are done here so the capture matches what a user sees.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import shutil
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from harness import CORPUS, temp_dir  # noqa: E402
from trans.ui.app import App  # noqa: E402

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
user32.SetProcessDPIAware()

OUT = ROOT / "docs" / "screenshots"
PW_RENDERFULLCONTENT = 0x00000002
# A neutral output folder: the real default is the user's Desktop, which is not
# something to publish a picture of.
DEMO_OUT = r"D:\转码输出"


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


def find_hwnd(pid: int) -> int:
    hits: list[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _l):
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 2)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if "转码工具" in buf.value:
                hits.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return hits[0] if hits else 0


def grab(hwnd: int) -> bytes:
    """PrintWindow into a top-down 32-bit DIB, returned as BMP bytes."""
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise RuntimeError("窗口尺寸为 0，可能还没映射")

    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    if not user32.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT):
        user32.PrintWindow(hwnd, mem, 0)

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = w, -h
    bi.biPlanes, bi.biBitCount = 1, 32
    buf = ctypes.create_string_buffer(w * 4 * h)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(hwnd, hdc)

    # A negative biHeight asks for top-down rows, but the BMP header below
    # declares a positive height (bottom-up), so the rows are reversed here or
    # the picture comes out upside down.
    stride = w * 4
    rows = [buf.raw[i * stride:(i + 1) * stride] for i in range(h)]
    pixels = b"".join(reversed(rows))

    header = (b"BM" + (54 + len(pixels)).to_bytes(4, "little") + b"\0\0\0\0"
              + (54).to_bytes(4, "little") + (40).to_bytes(4, "little")
              + w.to_bytes(4, "little") + h.to_bytes(4, "little")
              + (1).to_bytes(2, "little") + (32).to_bytes(2, "little")
              + (0).to_bytes(4, "little") + len(pixels).to_bytes(4, "little")
              + (0).to_bytes(4, "little") * 4)
    return header + pixels


def pump(root, seconds: float = 0.0, until=None, timeout: float = 120.0) -> bool:
    end = time.time() + timeout
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
        if time.time() > end:
            return False
        time.sleep(0.02)


def load(tab, paths, root) -> None:
    tab.table.clear()
    tab._accept([str(p) for p in paths])
    pump(root, until=lambda: all(i.info is not None for i in tab.table.items),
         timeout=180)
    pump(root, seconds=0.5)


def shoot(root, hwnd: int, name: str) -> None:
    pump(root, seconds=0.7)
    (OUT / (name + ".bmp")).write_bytes(grab(hwnd))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    app = App()
    root = app.root
    video, image, audio = app.tabs
    work = temp_dir("shots")

    root.deiconify()
    root.geometry("+40+30")
    root.lift()
    pump(root, seconds=1.2)
    hwnd = find_hwnd(os.getpid())
    if not hwnd:
        print("找不到窗口")
        return 1

    for tab in app.tabs:
        tab.var_same_dir.set(False)
        tab.var_out_dir.set(DEMO_OUT)

    # ---- 1) video: three clips queued, one selected, a range chosen, preview up
    load(video, [CORPUS / "video" / n for n in
                 ("h264_1080p30.mov", "h264_1080p30.ts", "vp9_1080p30.webm")], root)
    video.table.tree.selection_set([video.table.items[0].row_id])
    bar = video.trimbar
    pump(root, until=lambda: len(bar._thumbs) > 0, timeout=90)
    bar.set_range(4.0, 11.5, notify=True)
    bar._playhead = 6.5
    bar._move_playhead()
    bar._refresh_status()
    bar._draw()
    bar._request_preview(immediate=True)
    pump(root, until=lambda: bar._preview_img is not None, timeout=30)
    video.cmb_fmt.set_key("mp4")
    video.cmb_quality.set_key("balanced")
    video._option_changed()
    pump(root, seconds=0.5)
    shoot(root, hwnd, "video")

    # ---- 2) video in merge mode
    video.var_mode.set("concat")
    video._mode_changed()                  # what clicking the radio button does
    pump(root, seconds=0.5)
    shoot(root, hwnd, "merge")
    video.var_mode.set("convert")
    video._mode_changed()

    # ---- 3) image: merging five stills into one GIF
    app.nb.select(1)
    pump(root, seconds=0.4)
    load(image, [CORPUS / "image" / ("seq%02d.png" % i) for i in range(1, 6)], root)
    image.var_mode.set("sequence")
    image._mode_changed()
    image.cmb_fmt.set_key("gif")
    image.cmb_delay.set_key("0.5")
    image.cmb_colors.set_key("256")
    image._fmt_changed()
    image._option_changed()
    pump(root, seconds=0.6)
    shoot(root, hwnd, "image")

    # ---- 4) audio: extract from a video, 16 kHz mono, loudness normalisation
    app.nb.select(2)
    pump(root, seconds=0.4)
    load(audio, [CORPUS / "audio" / "a_48k_stereo.wav",
                 CORPUS / "video" / "h264_1080p30.mp4"], root)
    audio.cmb_fmt.set_key("mp3")
    audio.cmb_rate.set_key("16000")
    audio.cmb_ch.set_key("1")
    audio.cmb_loud.set_key("douyin")
    audio._fmt_changed()
    audio._option_changed()
    pump(root, seconds=0.6)
    shoot(root, hwnd, "audio")

    root.destroy()
    shutil.rmtree(work, ignore_errors=True)

    ffmpeg = ROOT / "bin" / "ffmpeg.exe"
    for bmp in sorted(OUT.glob("*.bmp")):
        png = bmp.with_suffix(".png")
        # Captured at the display's physical size (1.5x here), so a 1.5x
        # downscale gives a crisp picture without a 9 MB file.
        subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                        "-i", str(bmp), "-vf", "scale=iw/1.5:-1:flags=lanczos",
                        "-compression_level", "9", str(png)], check=True)
        print("  %-12s %6.0f KB" % (png.name, png.stat().st_size / 1024))
        bmp.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
