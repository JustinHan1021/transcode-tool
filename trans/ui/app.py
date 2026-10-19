"""Main window: capability report, the three tabs, and app startup."""
from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk

from .. import ff
from ..presets import VIDEO_ENCODERS
from .base import BaseTab
from .tab_audio import AudioTab
from .tab_image import ImageTab
from .tab_video import VideoTab
from .widgets import HAS_DND, init_scale, px

APP_NAME = "转码工具"


def enable_dpi_awareness() -> None:
    """Without this the window is blurry on a scaled display."""
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _make_root():
    if HAS_DND:
        try:
            from tkinterdnd2 import TkinterDnD
            return TkinterDnD.Tk()
        except Exception:
            pass
    return tk.Tk()


def elide(text: str, columns: int) -> str:
    """Truncate by display width: a CJK glyph is about twice as wide as a space."""
    width = 0
    for i, ch in enumerate(text):
        width += 2 if ord(ch) > 0x2000 else 1
        if width > columns:
            return text[: max(1, i - 1)] + "…"
    return text


def hw_encoder_summary(caps) -> str:
    worked = [VIDEO_ENCODERS[k].label for k, v in VIDEO_ENCODERS.items()
              if v.hardware and caps.encoding_works(v.encoder)]
    if worked:
        return "硬件编码可用：" + "、".join(worked)
    listed = [v.encoder for v in VIDEO_ENCODERS.values()
              if v.hardware and caps.has_encoder(v.encoder)]
    if listed:
        return f"硬件编码调用失败（{', '.join(listed)} 与当前驱动不兼容），已自动使用 CPU 编码"
    return "使用 CPU 编码"


class App:
    def __init__(self):
        self.root = _make_root()
        self.root.title(f"{APP_NAME} — 视频 / 图片 / 音频")
        # The process is DPI-aware, so sizes are in physical pixels and must be
        # scaled with the display, or the layout is cramped at 125%/150%.
        self.scale = init_scale(self.root)
        self.root.geometry(f"{px(1180)}x{px(880)}")
        self.root.minsize(px(1040), px(800))

        self._events: queue.Queue = queue.Queue()
        self.caps = None
        self.tabs: list[BaseTab] = []

        self._setup_fonts()
        self._build_status()

        self.body = ttk.Frame(self.root)
        self.body.pack(fill="both", expand=True)
        self.nb = ttk.Notebook(self.body)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(8, 4))

        self._detect_capabilities()
        self._build_tabs()
        self._bind_keys()
        self.root.after(120, self._pump)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ setup
    def _setup_fonts(self) -> None:
        available = set(tkfont.families())
        for family in ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
                       "Noto Sans CJK SC", "Segoe UI"):
            if family in available:
                try:
                    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                                 "TkHeadingFont", "TkTooltipFont"):
                        f = tkfont.nametofont(name)
                        f.configure(family=family, size=9)
                    break
                except tk.TclError:
                    continue
        try:
            ttk.Style().configure("Treeview", rowheight=px(24))
        except tk.TclError:
            pass

    def _build_status(self) -> None:
        bar = ttk.Frame(self.root, relief="flat")
        bar.pack(fill="x", side="bottom")
        bar.columnconfigure(1, weight=1)
        self.var_status = tk.StringVar(value="正在检测 ffmpeg…")
        self.var_hw = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.var_status, foreground="#374151").grid(
            row=0, column=0, sticky="w", padx=(10, 12), pady=px(5))
        ttk.Label(bar, textvariable=self.var_hw, foreground="#6b7280").grid(
            row=0, column=1, sticky="e", padx=(0, 10), pady=px(5))
        ttk.Separator(self.root, orient="horizontal").pack(fill="x", side="bottom")

    def _set_hw_status(self) -> None:
        text = hw_encoder_summary(self.caps)
        notes = "；".join(self.caps.notes)
        if notes:
            text += "   ·   " + notes
        self.var_hw.set(elide(text, 62))

    def _detect_capabilities(self) -> None:
        """Resolve and probe ffmpeg before building the tabs: the tab dropdowns
        only ever list formats this build can actually produce."""
        try:
            self.caps = ff.detect()
        except ff.FFNotFound as exc:
            self._show_missing_ffmpeg(exc)
            return
        self.var_status.set(elide(
            f"ffmpeg {self.caps.version} · {self.caps.ffmpeg}"
            + ("" if self.caps.ffprobe else "  ·  未找到 ffprobe（已启用兼容解析）"), 62))
        self._set_hw_status()

    def _show_missing_ffmpeg(self, exc: ff.FFNotFound) -> str:
        msg = (
            "没有找到 ffmpeg，无法进行任何转码。\n\n"
            "解决办法（任选其一）：\n"
            "1. 把 ffmpeg.exe 放到本程序目录下的 bin 文件夹里（推荐，最干净）\n"
            "2. 安装 ffmpeg 并把它加入系统 PATH 环境变量\n"
            "   下载地址：https://www.gyan.dev/ffmpeg/builds/  或  https://github.com/BtbN/FFmpeg-Builds\n"
            "3. 如果已经装了某个自带 ffmpeg 的软件（OBS、Blender 等），\n"
            "   可以把它的 ffmpeg.exe 复制到本程序的 bin 文件夹\n\n"
            f"程序已经搜索了 {exc.tried} 个位置。\n"
            f"也可以手动指定：在 {ff.CONFIG_FILE} 里写入 "
            '{"ffmpeg": "你的完整路径\\\\ffmpeg.exe"}'
        )
        self.root.withdraw()
        messagebox.showerror(f"{APP_NAME} — 缺少 ffmpeg", msg)
        self.root.destroy()
        raise SystemExit(1)

    def _build_tabs(self) -> None:
        if self.caps is None:
            return
        for cls in (VideoTab, ImageTab, AudioTab):
            try:
                tab = cls(self.nb, self)
            except Exception as exc:                    # keep other tabs usable
                frame = ttk.Frame(self.nb)
                ttk.Label(frame, text=f"{cls.title} 页初始化失败：{exc}",
                          foreground="#b91c1c").pack(padx=20, pady=20)
                self.nb.add(frame, text=cls.title)
                continue
            self.tabs.append(tab)
            self.nb.add(tab, text=cls.title)
        if self.tabs:
            self.nb.select(0)
        # Trial-encoding the hardware encoders costs a second or two; doing it
        # after the window is up keeps startup snappy.
        threading.Thread(target=self._verify_async, daemon=True).start()

    def _verify_async(self) -> None:
        try:
            profiles = {k: {"hardware": v.hardware, "codec": v.encoder,
                            "smoke": list(v.smoke)}
                        for k, v in VIDEO_ENCODERS.items()}
            self.caps.verify_encoders(profiles)
        except Exception:
            pass
        self._events.put(("caps", None))

    def _bind_keys(self) -> None:
        self.root.bind("<Control-o>", lambda e: self._active_tab().add_files())
        self.root.bind("<Control-O>", lambda e: self._active_tab().add_folder())
        self.root.bind("<F5>", lambda e: self._active_tab().start())
        self.root.bind("<Escape>", lambda e: self._active_tab().cancel())
        self.root.bind("<Control-l>", lambda e: self._active_tab().log.clear())

    # ------------------------------------------------------------------ misc
    def _active_tab(self) -> BaseTab | None:
        idx = self.nb.index(self.nb.select()) if self.tabs else None
        if idx is None or idx >= len(self.tabs):
            return self.tabs[0] if self.tabs else None
        return self.tabs[idx]

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "caps":
                    self._set_hw_status()
                    for tab in self.tabs:
                        try:
                            tab.refresh_capabilities()
                        except Exception:
                            pass
        except queue.Empty:
            pass
        self.root.after(200, self._pump)

    def _on_close(self) -> None:
        running = [t for t in self.tabs if t.engine is not None and t.engine.is_running]
        if running:
            if not messagebox.askokcancel(APP_NAME, "还有任务正在转换，确定要退出吗？"):
                return
            for t in running:
                t.engine.cancel_all()
        self.root.destroy()

    def add_initial_files(self, paths) -> None:
        """Route files handed in at startup (drag onto the .bat, "open with").

        They land on the visible tab only if it takes them; otherwise the first
        tab that does gets the files and comes to the front. Dropping images on
        the launcher should not silently do nothing just because the video tab
        happened to be showing.
        """
        if not paths:
            return
        active = self._active_tab()
        if active is not None and active._accept(list(paths), notify=False):
            return
        for i, tab in enumerate(self.tabs):
            if tab is active:
                continue
            if tab._accept(list(paths), notify=False):
                self.nb.select(i)
                return
        if active is not None:
            active._accept(list(paths))            # report the mismatch there

    def run(self) -> None:
        self.root.mainloop()


def main(argv=None) -> int:
    enable_dpi_awareness()
    args = list(argv if argv is not None else sys.argv[1:])
    files = [a for a in args if os.path.exists(a)]
    app = App()
    if files:
        app.root.after(300, lambda: app.add_initial_files(files))
    app.run()
    return 0
