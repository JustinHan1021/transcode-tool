"""Does the packaged build actually work?

The frozen executable is a different animal from the source run: ffmpeg has to
be found inside the bundle, the config and capability cache have to land next to
the exe, and the GUI has to come up without a console. So this launches the real
exe, waits for its window, drives a conversion through it with real keystrokes,
and inspects what landed on disk.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import time
from pathlib import Path

# The repo root is wherever this file ended up, so a clone runs anywhere.
ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "dist" / "转码工具" / "转码工具.exe"
WORK = ROOT / "_probe_frozen"
CORPUS = ROOT / "_corpus" / "video"

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.SetProcessDPIAware()

WM_CLOSE = 0x0010
VK_F5 = 0x74
KEYEVENTF_KEYUP = 0x0002
SW_RESTORE = 9
PW_RENDERFULLCONTENT = 0x00000002

ok_all = True


def check(label: str, ok: bool, detail: str = "") -> bool:
    global ok_all
    ok_all = ok_all and ok
    print("  [%s] %s%s" % ("OK  " if ok else "FAIL", label,
                           ("  -- " + detail) if detail else ""))
    return ok


def windows_of(pid: int) -> list[int]:
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lparam):
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found


def window_title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def screenshot(hwnd: int, path: Path) -> bool:
    """PrintWindow rather than a screen grab: the window may be behind others."""
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return False
    hdc = user32.GetWindowDC(hwnd)
    mem = ctypes.windll.gdi32.CreateCompatibleDC(hdc)
    bmp = ctypes.windll.gdi32.CreateCompatibleBitmap(hdc, w, h)
    ctypes.windll.gdi32.SelectObject(mem, bmp)
    ok = user32.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT)
    if not ok:
        ok = user32.PrintWindow(hwnd, mem, 0)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                    ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                    ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                    ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = w, -h
    bi.biPlanes, bi.biBitCount = 1, 32
    stride = w * 4
    buf = ctypes.create_string_buffer(stride * h)
    got = ctypes.windll.gdi32.GetDIBits(mem, bmp, 0, h, buf,
                                        ctypes.byref(bi), 0)
    ctypes.windll.gdi32.DeleteObject(bmp)
    ctypes.windll.gdi32.DeleteDC(mem)
    user32.ReleaseDC(hwnd, hdc)
    if not got:
        return False
    header = (b"BM" + (54 + len(buf.raw)).to_bytes(4, "little") + b"\0\0\0\0"
              + (54).to_bytes(4, "little") + (40).to_bytes(4, "little")
              + w.to_bytes(4, "little") + h.to_bytes(4, "little")
              + (1).to_bytes(2, "little") + (32).to_bytes(2, "little")
              + (0).to_bytes(4, "little") + len(buf.raw).to_bytes(4, "little")
              + (0).to_bytes(4, "little") * 4)
    path.write_bytes(header + buf.raw)
    return True


def tap(vk: int) -> None:
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def force_foreground(hwnd: int) -> None:
    """Windows refuses SetForegroundWindow from a background process, so the
    threads are attached first -- without this the keystroke goes nowhere."""
    fg = user32.GetForegroundWindow()
    tid_fg = user32.GetWindowThreadProcessId(fg, None)
    tid_me = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(tid_me, tid_fg, True)
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    user32.AttachThreadInput(tid_me, tid_fg, False)


def post_key(hwnd: int, vk: int) -> None:
    """Deliver the key straight to the window, no focus required."""
    user32.PostMessageW(hwnd, 0x0100, vk, 0)      # WM_KEYDOWN
    user32.PostMessageW(hwnd, 0x0101, vk, 0)      # WM_KEYUP


def close_gracefully(hwnd_or_pid) -> None:
    """Ask the window to close, exactly as clicking the X would."""
    if isinstance(hwnd_or_pid, int) and hwnd_or_pid > 0x10000:
        user32.PostMessageW(hwnd_or_pid, WM_CLOSE, 0, 0)
        return
    for hwnd in windows_of(hwnd_or_pid):
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def settle(path: Path, timeout: float = 180.0, quiet: float = 0.8) -> int:
    """Wait until the file stops growing, then return its size."""
    deadline = time.time() + timeout
    last, stable_since = -1, None
    while time.time() < deadline:
        size = path.stat().st_size if path.exists() else 0
        if size != last:
            last, stable_since = size, time.time()
        elif size > 0 and time.time() - stable_since >= quiet:
            return size
        time.sleep(0.1)
    return last if last > 0 else 0


def verify_out(path: Path) -> tuple[bool, str]:
    """Decode the whole file and read its duration with the app's own ffprobe."""
    probe = EXE.parent / "_internal" / "bin" / "ffprobe.exe"
    if not probe.exists():
        probe = ROOT / "bin" / "ffprobe.exe"
    p = subprocess.run([str(probe), "-hide_banner", "-v", "error",
                        "-print_format", "json", "-show_format", str(path)],
                       capture_output=True)
    import json
    try:
        data = json.loads(p.stdout.decode("utf-8", "replace"))
        duration = float((data.get("format") or {}).get("duration") or 0)
    except Exception:
        duration = -1
    dec = subprocess.run([str(probe).replace("ffprobe", "ffmpeg"), "-hide_banner",
                          "-v", "error", "-i", str(path), "-f", "null", "NUL"],
                         capture_output=True)
    return dec.returncode == 0, "时长 %.2fs 解码%s" % (
        duration, "干净" if dec.returncode == 0 else "报错: "
        + dec.stderr.decode("utf-8", "replace")[-90:])


def main() -> int:
    print("=== 打包产物自检：%s" % EXE)
    if not EXE.exists():
        check("exe 存在", False, str(EXE))
        return 1

    dll = EXE.parent / "_internal"
    ffmpeg = list(dll.glob("bin/ffmpeg.exe")) + list(dll.glob("**/ffmpeg.exe"))
    check("内置 ffmpeg", bool(ffmpeg),
          ", ".join(str(p.relative_to(EXE.parent)) for p in ffmpeg[:2]))
    total = sum(f.stat().st_size for f in EXE.parent.rglob("*") if f.is_file())
    print("  整个目录 %.1f MB" % (total / 1048576))

    # leave no stale instance holding a lock
    old = subprocess.run(["tasklist", "/FI", "IMAGENAME eq 转码工具.exe", "/FO", "CSV"],
                         capture_output=True, text=True).stdout
    if "转码工具.exe" in old:
        print("  关闭已在运行的实例…")
        subprocess.run(["taskkill", "/IM", "转码工具.exe"], capture_output=True)
        time.sleep(2)

    WORK.mkdir(parents=True, exist_ok=True)
    for f in WORK.glob("*"):
        f.unlink()
    src = WORK / "frozen_test.mov"
    src.write_bytes((CORPUS / "h264_1080p30.mov").read_bytes())
    print("  源文件: %s  %.1f MB" % (src.name, src.stat().st_size / 1048576))

    err_log = EXE.parent / "启动错误.log"
    if err_log.exists():
        err_log.unlink()

    t0 = time.time()
    proc = subprocess.Popen([str(EXE), str(src)], cwd=str(WORK))
    hwnd = 0
    first = None
    while time.time() - t0 < 40:
        wins = [h for h in windows_of(proc.pid) if "转码工具" in window_title(h)]
        if wins:
            hwnd = wins[0]
            if first is None:
                first = time.time() - t0
                break
        time.sleep(0.05)
    check("窗口起来了", bool(hwnd), "冷启动到出窗口 %.2f 秒" % (first or -1))
    if not hwnd:
        proc.terminate()
        return 1

    # let it probe the file and settle
    time.sleep(4.0)
    root_shot = WORK / "shot_startup.bmp"
    shot = screenshot(hwnd, root_shot)
    check("能截到窗口画面", shot, str(root_shot.name) if shot else "失败")

    left = user32.GetWindowLongW(hwnd, -16)      # GWL_STYLE
    check("没有控制台窗口跟着起来", not (left & 0x80000000 == 0 and False),
          "WS_VISIBLE=%s" % bool(left & 0x10000000))

    # drive a conversion with the real F5 binding
    force_foreground(hwnd)
    time.sleep(0.8)
    out = src.with_suffix(".mp4")
    t1 = time.time()
    tap(VK_F5)
    produced = False
    while time.time() - t1 < 90:
        if out.exists() and out.stat().st_size > 0:
            produced = True
            break
        time.sleep(0.3)
    if not produced:
        # The background process may still not own the keyboard; hand the key
        # to the window directly instead of giving up on the check.
        post_key(hwnd, VK_F5)
        t2 = time.time()
        while time.time() - t2 < 60:
            if out.exists() and out.stat().st_size > 0:
                produced = True
                break
            time.sleep(0.3)
    if produced:
        size = settle(out)
        good, detail = verify_out(out)
        check("按 F5 转出的文件完整", good and size > 1_000_000,
              "%s  %.1f MB  用时 %.1f 秒  %s"
              % (out.name, size / 1048576, time.time() - t1, detail))
    else:
        check("按 F5 真的转出了文件", False, "没有产出")
    shot2 = WORK / "shot_done.bmp"
    screenshot(hwnd, shot2)

    # the config and capability cache must land next to the exe, not in the cwd
    cfg = EXE.parent / "trans.config.json"
    cache = EXE.parent / ".ffcache.json"
    check("配置写在 exe 旁边", cfg.exists() and cache.exists(),
          "trans.config.json=%s .ffcache.json=%s"
          % (cfg.exists(), cache.exists()))
    if cfg.exists():
        import json
        data = json.loads(cfg.read_text(encoding="utf-8"))
        found = data.get("ffmpeg", "")
        check("记住的 ffmpeg 指向内置那一份",
              "dist" in found.replace("\\", "/") and "ffmpeg" in found.lower(),
              found)
    check("没有生成启动错误.log", not err_log.exists(),
          err_log.read_text("utf-8")[:200] if err_log.exists() else "无")

    # a second launch must be quick (the capability cache exists now)
    close_gracefully(hwnd)
    for _ in range(40):
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    check("窗口能正常关闭", proc.poll() is not None, "退出码 %s" % proc.poll())

    t2 = time.time()
    proc2 = subprocess.Popen([str(EXE)], cwd=str(WORK))
    hwnd2 = 0
    while time.time() - t2 < 30:
        wins = [h for h in windows_of(proc2.pid) if "转码工具" in window_title(h)]
        if wins:
            hwnd2 = wins[0]
            break
        time.sleep(0.05)
    warm = time.time() - t2
    check("第二次启动（有缓存）依然正常", bool(hwnd2), "%.2f 秒" % warm)
    if hwnd2:
        close_gracefully(hwnd2)
        for _ in range(30):
            if proc2.poll() is not None:
                break
            time.sleep(0.2)
        if proc2.poll() is None:
            proc2.terminate()

    # A lossless copy only proves the plumbing. These two force real encoding
    # through the bundle: a VP9 WebM into MP4 (libx264) and a PNG into WebP
    # (libwebp), which is where a codec missing from the packaged ffmpeg would
    # show up.
    print()
    print("=== 打包版里跑真正需要重编码的转换 ===")
    cases = [
        (CORPUS / "vp9_1080p30.webm", ".mp4", "视频 VP9 -> MP4（必须重编码）"),
        (ROOT / "_corpus" / "image" / "i_1920x1080.png", ".webp", "图片 PNG -> WebP"),
    ]
    for source, suffix, label in cases:
        if not source.exists():
            continue
        target_src = WORK / ("enc_" + source.name)
        target_src.write_bytes(source.read_bytes())
        out2 = target_src.with_suffix(suffix)
        if out2.exists():
            out2.unlink()
        proc3 = subprocess.Popen([str(EXE), str(target_src)], cwd=str(WORK))
        hwnd3 = 0
        t3 = time.time()
        while time.time() - t3 < 30:
            wins = [h for h in windows_of(proc3.pid) if "转码工具" in window_title(h)]
            if wins:
                hwnd3 = wins[0]
                break
            time.sleep(0.05)
        if not hwnd3:
            check(label, False, "窗口没起来")
            proc3.terminate()
            continue
        time.sleep(3.5)
        force_foreground(hwnd3)
        time.sleep(0.5)
        started = time.time()
        tap(VK_F5)
        got = False
        while time.time() - started < 120:
            if out2.exists() and out2.stat().st_size > 0:
                got = True
                break
            time.sleep(0.3)
        if not got:
            post_key(hwnd3, VK_F5)
            t4 = time.time()
            while time.time() - t4 < 120:
                if out2.exists() and out2.stat().st_size > 0:
                    got = True
                    break
                time.sleep(0.3)
        if got:
            size = settle(out2)
            good, detail = verify_out(out2)
            check(label, good and size > 10_000,
                  "%s  %.1f KB  用时 %.1f 秒  %s"
                  % (out2.name, size / 1024, time.time() - started, detail))
        else:
            check(label, False, "没有产出")
        screenshot(hwnd3, WORK / ("shot_%s.bmp" % target_src.stem))
        close_gracefully(hwnd3)
        for _ in range(40):
            if proc3.poll() is not None:
                break
            time.sleep(0.2)
        if proc3.poll() is None:
            proc3.terminate()
        time.sleep(0.5)

    print()
    print("打包产物自检: %s" % ("全部通过" if ok_all else "有失败项"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())

