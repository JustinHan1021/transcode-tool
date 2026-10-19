"""转码工具 — 启动入口。

双击本文件即可运行（.pyw 后缀不会留下黑色控制台窗口）。
也可以把视频 / 图片 / 音频文件直接拖到本文件或 启动.bat 上，程序会自动导入。

这里刻意把所有异常都接住：用 pythonw 启动时没有控制台，未处理的异常会让程序
悄悄消失，用户只看到"打不开"却拿不到任何线索。所以任何失败都会写成日志，
并用 Windows 原生弹窗显示出来。
"""
import os
import sys
import traceback

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

LOG_PATH = os.path.join(BASE, "启动错误.log")


def _report(title: str, detail: str) -> None:
    message = (f"{title}\n\n"
               f"详细信息已写入：\n{LOG_PATH}\n\n"
               f"{(detail or '').strip()[-1500:]}")
    try:
        with open(LOG_PATH, "w", encoding="utf-8") as fh:
            fh.write(f"{title}\n\n{detail}\n")
    except OSError:
        pass

    # A native message box works even when Tk is the thing that failed.
    shown = False
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "转码工具 — 启动失败", 0x10)
        shown = True
    except Exception:
        pass
    if not shown:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("转码工具 — 启动失败", message)
            root.destroy()
        except Exception:
            sys.stderr.write(message + "\n")


def _run() -> int:
    try:
        from trans.ui import main as launch
    except Exception:
        _report("程序启动失败：文件不完整或运行环境有问题。", traceback.format_exc())
        return 1

    try:
        return launch()
    except SystemExit:
        raise
    except Exception:
        _report("程序运行时发生未预期的错误。", traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(_run())
