"""Runner: build the real application and drive every section.

Modal dialogs are replaced by recorders: a test has nobody to click them, and a
blocked messagebox hangs the whole run. Recording them also makes the
user-facing text assertable. A watchdog prints a stack dump and exits if a
section ever stops making progress.
"""
from __future__ import annotations

import faulthandler
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gui as g1  # noqa: E402
import gui2 as g2  # noqa: E402
from tkinter import messagebox  # noqa: E402

from trans.ui.app import App  # noqa: E402

DIALOGS: list[tuple[str, str, str]] = []


def _record(kind):
    def fn(title="", message="", **kwargs):
        DIALOGS.append((kind, str(title), str(message)))
        print("  >> 弹窗[%s] %s：%s" % (kind, title, message.replace("\n", " ")[:110]))
        # answer "yes" for confirmations so a close prompt does not block
        return True
    return fn


messagebox.showinfo = _record("信息")
messagebox.showwarning = _record("警告")
messagebox.showerror = _record("错误")
messagebox.askokcancel = _record("确认")
messagebox.askyesno = _record("是否")


def main() -> int:
    which = sys.argv[1:] or ["1", "2", "3", "4", "5", "6", "7", "8"]
    t0 = time.time()
    app = App()
    root = app.root
    root.withdraw()
    g1.pump(root, seconds=0.6)
    try:
        if "1" in which:
            g1.g1_startup(app, root)
        if "2" in which:
            g1.g2_routing(app, root)
        if "3" in which:
            g1.g3_plan_all_options(app, root)
        if "4" in which:
            g2.g4_real_runs(app, root)
        if "5" in which:
            g2.g5_sorting(app, root)
        if "6" in which:
            g2.g6_timeline(app, root)
        if "7" in which:
            g2.g7_cancel_and_keys(app, root)
        if "8" in which:
            g2.g8_housekeeping(app, root)
    finally:
        print()
        print("用时 %.1f 秒" % (time.time() - t0))
        print("弹窗共 %d 次" % len(DIALOGS))
        print("GUI 总体: %s" % ("全部通过" if _ok() else "有失败项"))
        try:
            root.destroy()
        except Exception:
            pass
    return 0 if _ok() else 1


def _ok() -> bool:
    return bool(getattr(g1, "_ok", True)) and bool(getattr(g2, "_ok", True))


if __name__ == "__main__":
    faulthandler.enable()
    faulthandler.dump_traceback_later(600, exit=True)
    sys.exit(main())
