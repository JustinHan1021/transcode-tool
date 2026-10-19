"""Does the release zip actually work for someone who just downloaded it?

Extract it into a clean temp folder, start the app the way a user would, and put
a real file through it with a real keystroke. Also check the licence files are
present, since GPLv3 requires them to travel with the binary.

Nothing here uses the working tree: the point is to test the archive, not the
source it came from.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ZIP = ROOT / "_release" / "transcode-tool-windows-x64.zip"
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


def windows_of(pid: int) -> list[int]:
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _l):
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 2)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if "转码工具" in buf.value:
                found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found


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


def post_key(hwnd: int, vk: int) -> None:
    user32.PostMessageW(hwnd, 0x0100, vk, 0)
    user32.PostMessageW(hwnd, 0x0101, vk, 0)


def settle(path: Path, timeout: float = 150.0) -> int:
    last, since = -1, None
    end = time.time() + timeout
    while time.time() < end:
        size = path.stat().st_size if path.exists() else 0
        if size != last:
            last, since = size, time.time()
        elif size > 0 and time.time() - since >= 1.0:
            return size
        time.sleep(0.15)
    return max(0, last)


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    print("=== 发布包可用性验证：%s" % ZIP.name)
    if not ZIP.exists():
        check("压缩包存在", False, str(ZIP))
        return 1
    check("压缩包大小合理", 20 * 1024 ** 2 < ZIP.stat().st_size < 500 * 1024 ** 2,
          "%.1f MB" % (ZIP.stat().st_size / 1048576))

    # ---- a clean folder, as if it were just downloaded
    stage = Path(tempfile.mkdtemp(prefix="relcheck_"))
    print("  解压到 %s" % stage)
    t0 = time.time()
    with zipfile.ZipFile(ZIP) as zf:
        names = zf.namelist()
        zf.extractall(stage)
    check("压缩包结构：单一顶层目录", all(n.startswith("转码工具/") for n in names),
          "顶层: %s" % sorted({n.split('/')[0] for n in names}))
    check("解压耗时合理", time.time() - t0 < 180, "%.1f 秒" % (time.time() - t0))

    app_dir = stage / "转码工具"
    exe = app_dir / "转码工具.exe"
    check("exe 存在", exe.exists())
    ff = list((app_dir / "_internal").rglob("ffmpeg.exe"))
    check("内置 ffmpeg", bool(ff), str(ff[0].relative_to(app_dir)) if ff else "缺失")
    for rel in ("LICENSE", "THIRD-PARTY-NOTICES.md", "licenses/GPL-3.0.txt"):
        check("附带 %s（GPLv3 要求）" % rel, (app_dir / rel).exists())
    gpl = app_dir / "licenses" / "GPL-3.0.txt"
    if gpl.exists():
        txt = gpl.read_text(encoding="utf-8", errors="ignore")
        check("GPLv3 全文完整", "GNU GENERAL PUBLIC LICENSE" in txt
              and "Version 3" in txt and len(txt) > 30000, "%d 字节" % len(txt))
    check("不带构建者的运行态配置",
          not (app_dir / "trans.config.json").exists()
          and not (app_dir / ".ffcache.json").exists())

    # ---- run it, exactly as the user would
    work = stage / "userfiles"
    work.mkdir()
    src = work / "release_test.mov"
    src.write_bytes((CORPUS / "h264_1080p30.mov").read_bytes())

    err_log = app_dir / "启动错误.log"
    t1 = time.time()
    proc = subprocess.Popen([str(exe), str(src)], cwd=str(work))
    hwnd = 0
    while time.time() - t1 < 60:
        wins = windows_of(proc.pid)
        if wins:
            hwnd = wins[0]
            break
        time.sleep(0.1)
    check("解压后双击能启动", bool(hwnd), "%.2f 秒出窗口" % (time.time() - t1))
    if not hwnd:
        proc.terminate()
        return 1

    time.sleep(4.5)
    check("没有启动错误日志", not err_log.exists(),
          err_log.read_text("utf-8", errors="ignore")[:200] if err_log.exists() else "无")

    force_foreground(hwnd)
    time.sleep(0.8)
    out = src.with_suffix(".mp4")
    started = time.time()
    tap(VK_F5)
    while time.time() - started < 120:
        if out.exists() and out.stat().st_size > 1_000_000:
            break
        time.sleep(0.3)
    if not (out.exists() and out.stat().st_size > 1_000_000):
        post_key(hwnd, VK_F5)
        t2 = time.time()
        while time.time() - t2 < 120:
            if out.exists() and out.stat().st_size > 1_000_000:
                break
            time.sleep(0.3)

    size = settle(out) if out.exists() else 0
    check("按 F5 真的转出了文件", size > 1_000_000,
          "%s  %.1f MB  用时 %.1f 秒"
          % (out.name, size / 1048576, time.time() - started))
    check("输出与源等大（无损复制）",
          abs(size - src.stat().st_size) < 262144,
          "源 %.1f MB / 出 %.1f MB"
          % (src.stat().st_size / 1048576, size / 1048576))

    ffprobe = app_dir / "_internal" / "bin" / "ffprobe.exe"
    if out.exists() and ffprobe.exists():
        p = subprocess.run([str(ffprobe), "-hide_banner", "-v", "error",
                            "-print_format", "json", "-show_format", str(out)],
                           capture_output=True)
        try:
            dur = float(json.loads(p.stdout.decode("utf-8", "replace"))["format"]["duration"])
        except Exception:
            dur = -1
        check("产物时长正确", abs(dur - 20.0) < 0.5, "%.2f 秒" % dur)

    # ---- where does it keep its config? next to the exe, not elsewhere
    cfg = app_dir / "trans.config.json"
    if cfg.exists():
        data = json.loads(cfg.read_text(encoding="utf-8"))
        found = data.get("ffmpeg", "")
        check("配置写在 exe 旁边，且指向内置 ffmpeg",
              stage.name in found and "_internal" in found,
              found.replace(str(stage), "<解压目录>"))
    else:
        check("配置写在 exe 旁边", False, "没有生成")

    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    for _ in range(40):
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    check("能正常关闭", proc.poll() is not None, "退出码 %s" % proc.poll())
    if proc.poll() is None:
        proc.terminate()

    print()
    print("  验证目录: %s（保留，便于你亲自看一眼）" % stage)
    print("发布包验证: %s" % ("全部通过" if ok_all else "有失败项"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    import faulthandler
    faulthandler.enable()
    faulthandler.dump_traceback_later(600, exit=True)
    sys.exit(main())
