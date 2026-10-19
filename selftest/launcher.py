"""The launcher the user actually double-clicks.

`启动.bat` picks a windowed interpreter, finds the entry script by extension and
forwards any dropped files as arguments. None of that is exercised by running the
source directly, so this starts the batch file with a file argument and checks
that the window comes up, that a conversion runs, and that no console window is
left behind.
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
BAT = ROOT / "启动.bat"
WORK = ROOT / "_probe_launch"
CORPUS = ROOT / "_corpus" / "video"

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.SetProcessDPIAware()

VK_F5 = 0x74
WM_CLOSE = 0x0010
KEYEVENTF_KEYUP = 0x0002
ok_all = True


def check(label: str, ok: bool, detail: str = "") -> None:
    global ok_all
    ok_all = ok_all and ok
    print("  [%s] %s%s" % ("OK  " if ok else "FAIL", label,
                           ("  -- " + detail) if detail else ""))


def all_windows():
    out = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _l):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 2)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            out.append((hwnd, pid.value, buf.value, cls.value))
        return True

    user32.EnumWindows(cb, 0)
    return out


def find_tool() -> tuple[int, int, str]:
    for hwnd, pid, title, cls in all_windows():
        if "转码工具" in title and "启动失败" not in title:
            return hwnd, pid, cls
    return 0, 0, ""


def force_foreground(hwnd: int) -> None:
    fg = user32.GetForegroundWindow()
    tid_fg = user32.GetWindowThreadProcessId(fg, None)
    tid_me = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(tid_me, tid_fg, True)
    user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    user32.AttachThreadInput(tid_me, tid_fg, False)


def tap(vk: int) -> None:
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def settle(path: Path, timeout: float = 120.0) -> int:
    last, since = -1, None
    end = time.time() + timeout
    while time.time() < end:
        size = path.stat().st_size if path.exists() else 0
        if size != last:
            last, since = size, time.time()
        elif size > 0 and time.time() - since >= 0.8:
            return size
        time.sleep(0.1)
    return max(0, last)


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    print("=== 启动.bat（用户实际双击的入口）")
    WORK.mkdir(parents=True, exist_ok=True)
    for f in WORK.glob("*"):
        f.unlink()
    src = WORK / "launch_test.mov"
    src.write_bytes((CORPUS / "h264_1080p30.mov").read_bytes())

    err = ROOT / "启动错误.log"
    if err.exists():
        err.unlink()

    t0 = time.time()
    proc = subprocess.Popen(["cmd", "/c", str(BAT), str(src)], cwd=str(ROOT),
                            creationflags=0x08000000)
    hwnd = pid = 0
    cls = ""
    while time.time() - t0 < 60:
        hwnd, pid, cls = find_tool()
        if hwnd:
            break
        time.sleep(0.1)
    check("启动.bat 能拉起窗口", bool(hwnd),
          "%.2f 秒，窗口类 %s" % (time.time() - t0, cls))
    if not hwnd:
        proc.terminate()
        return 1

    check("没有控制台窗口残留",
          not any(c == "ConsoleWindowClass" for _h, _p, _t, c in all_windows()
                  if "cmd" in _t.lower() or _t == ""),
          "可见窗口: %s" % [t for _h, _p, t, c in all_windows()
                            if c == "ConsoleWindowClass"][:3])
    check("没有生成启动错误.log", not err.exists())

    # wait for the probe, then convert
    time.sleep(4.5)
    force_foreground(hwnd)
    time.sleep(0.6)
    out = src.with_suffix(".mp4")
    started = time.time()
    tap(VK_F5)
    size = 0
    while time.time() - started < 90:
        if out.exists() and out.stat().st_size > 1_000_000:
            time.sleep(1.0)
            size = settle(out)
            break
        time.sleep(0.3)
    check("拖到 启动.bat 上的文件能被转换", size > 1_000_000,
          "%s  %.1f MB  用时 %.1f 秒" % (out.name, size / 1048576,
                                         time.time() - started))
    check("输出与源文件等大（无损复制）",
          abs(size - src.stat().st_size) < 262144,
          "源 %.1f MB / 出 %.1f MB" % (src.stat().st_size / 1048576,
                                       size / 1048576))

    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    time.sleep(2.0)
    hwnd2, _pid2, _cls2 = find_tool()
    check("关闭后窗口消失", not hwnd2)
    # Only the interpreter this test started. A blanket `taskkill /IM python.exe`
    # takes this very process down with it and the buffered report is lost.
    if pid:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)

    print()
    print("启动入口自检: %s" % ("全部通过" if ok_all else "有失败项"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    import faulthandler
    faulthandler.enable()
    faulthandler.dump_traceback_later(420, exit=True)
    sys.exit(main())
