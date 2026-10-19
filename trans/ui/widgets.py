"""Reusable Tk widgets: the file table, the drop handling and small helpers."""
from __future__ import annotations

import os
import re
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, ttk

try:                                    # optional: drag & drop onto the window
    from tkinterdnd2 import DND_FILES

    HAS_DND = True
except Exception:                       # pragma: no cover - depends on install
    DND_FILES = None
    HAS_DND = False

from .. import media

STATUS_COLORS = {
    "等待": "#6b7280",
    "分析中": "#2563eb",
    "转换中": "#2563eb",
    "完成": "#15803d",
    "失败": "#b91c1c",
    "已取消": "#a16207",
}

# --- DPI scaling -----------------------------------------------------------
# The process declares itself DPI-aware, so Tk works in physical pixels while
# font sizes stay in points and are scaled by the display DPI. Any hard-coded
# pixel value therefore has to be scaled too, or the layout comes out cramped
# on a 150% display.
SCALE = 1.0


def init_scale(root) -> float:
    global SCALE
    try:
        SCALE = max(1.0, float(root.winfo_fpixels("1i")) / 96.0)
    except Exception:
        SCALE = 1.0
    return SCALE


def px(value: float) -> int:
    return int(round(value * SCALE))

class ChoiceBox(ttk.Combobox):
    """A dropdown that shows a label but yields a stable key.

    Keeping the two apart in one place is what stops internal identifiers such
    as "vp9" or "smart" from leaking into the interface, which is exactly what
    happens when a combobox's value doubles as its display text.
    """

    def __init__(self, master, choices, default=None, width=None, on_change=None):
        self._choices: list[tuple[str, str]] = []
        self._var = tk.StringVar()
        super().__init__(master, textvariable=self._var, state="readonly")
        if width:
            self.configure(width=width)
        self.on_change = on_change
        self.set_choices(choices, default)
        self.bind("<<ComboboxSelected>>", self._selected)

    def _selected(self, _event=None):
        if self.on_change:
            self.on_change(self.key())

    # -- mapping
    def key(self) -> str:
        label = self._var.get()
        for k, lab in self._choices:
            if lab == label:
                return k
        return self._choices[0][0] if self._choices else ""

    def label(self) -> str:
        return self._var.get()

    def set_key(self, key: str) -> None:
        for k, lab in self._choices:
            if k == key:
                self._var.set(lab)
                return
        if self._choices:
            self._var.set(self._choices[0][1])

    def set_choices(self, choices, keep: str | None = None) -> None:
        previous = keep if keep is not None else self.key()
        self._choices = [(str(k), str(lab)) for k, lab in choices]
        self.configure(values=[lab for _, lab in self._choices])
        self.set_key(previous)

    def enable(self, enabled: bool) -> None:
        self.configure(state="readonly" if enabled else "disabled")


BAR_FULL = "\u2588"
BAR_EMPTY = "\u2591"


def progress_bar(fraction: float, width: int = 12) -> str:
    f = max(0.0, min(1.0, fraction))
    filled = int(round(f * width))
    return f"{BAR_FULL * filled}{BAR_EMPTY * (width - filled)} {f * 100:3.0f}%"


def human_size(n: int) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return ""


def human_time(sec: float) -> str:
    if sec < 60:
        return f"{sec:.1f}s"
    m, s = divmod(int(sec), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _natural_key(text: str):
    """Sort key that treats digit runs as numbers."""
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", text)]


def parse_drop(data: str) -> list[str]:
    """Split a Tk drop payload into paths.

    Windows wraps paths containing spaces in braces, so a plain split() would
    break on exactly the files most likely to be dropped.
    """
    out: list[str] = []
    buf = ""
    depth = 0
    for ch in data:
        if ch == "{":
            depth += 1
            if depth == 1:
                continue
        elif ch == "}":
            depth -= 1
            if depth == 0:
                out.append(buf)
                buf = ""
                continue
        if depth == 0 and ch in " \t":
            if buf:
                out.append(buf)
                buf = ""
        else:
            buf += ch
    if buf:
        out.append(buf)
    return [p for p in (s.strip() for s in out) if p]


def collect_files(paths, allowed_exts: set[str], recursive: bool = True) -> list[str]:
    """Expand dropped paths into media files, keeping order and de-duplicating.

    Explicitly named files are filtered by extension too, not just the contents
    of folders. Without that a tab accepts anything at all, so dropping an image
    onto the video tab would quietly convert it when the point of the per-tab
    type lists is that each tab takes only what it can handle.
    """
    found: list[str] = []
    seen: set[str] = set()

    def push(p: str) -> None:
        key = os.path.normcase(os.path.abspath(p))
        if key in seen:
            return
        seen.add(key)
        found.append(p)

    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            pattern = "**/*" if recursive else "*"
            try:
                for child in sorted(p.glob(pattern)):
                    if child.is_file() and child.suffix.lower() in allowed_exts:
                        push(str(child))
            except OSError:
                continue
        elif p.is_file() and p.suffix.lower() in allowed_exts:
            push(str(p))
    return found


class ScrollArea(ttk.Frame):
    """A vertically scrollable container.

    The settings panel (options plus the trim panel) is taller than a 900px
    window on a scaled display can show at once, and the screen height is a hard
    limit -- so the panel scrolls instead of silently clipping its own controls.
    Put content into `.body`.
    """

    def __init__(self, master):
        super().__init__(master)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")

        self.body = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body)
        self.canvas.bind("<Configure>", self._on_canvas)

        # The wheel has to follow the pointer: binding only the canvas means it
        # stops working as soon as the cursor is over a child control.
        self.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_body(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    def _wheel(self, event) -> None:
        if not self._scrollable():
            return
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def _scrollable(self) -> bool:
        return self.body.winfo_reqheight() > self.canvas.winfo_height()

    def see_bottom(self) -> None:
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)


def parse_time(text: str) -> float | None:
    """Accept "90", "1:30", "1:30.5" or "0:01:30.5" as seconds."""
    s = (text or "").strip().replace("：", ":")
    if not s:
        return None
    try:
        parts = s.split(":")
        if len(parts) > 3:
            return None
        total = 0.0
        for p in parts:
            total = total * 60 + float(p)
        return max(0.0, total)
    except ValueError:
        return None


def fmt_clock(sec: float) -> str:
    m, s = divmod(max(0.0, sec), 60)
    h, m = divmod(int(m), 60)
    if h:
        return f"{h}:{m:02d}:{s:04.1f}"
    return f"{int(m)}:{s:04.1f}"


@dataclass
class FileItem:
    path: str
    row_id: str = ""
    seq: int = 0                  # insertion order, so "original order" can be restored
    info: media.MediaInfo | None = None
    status: str = "等待"
    progress: float = 0.0
    elapsed: float = 0.0
    detail: str = ""
    error: str = ""
    job: object | None = None

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def ext(self) -> str:
        return os.path.splitext(self.path)[1].lower().lstrip(".")

    @property
    def size(self) -> int:
        if self.info is not None:
            return self.info.size
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    @property
    def duration(self) -> float:
        return (self.info.duration or 0.0) if self.info else 0.0

    @property
    def mtime(self) -> float:
        try:
            return os.path.getmtime(self.path)
        except OSError:
            return 0.0

    def sort_value(self, key: str):
        if key == "name":
            return _natural_key(self.name)
        if key == "ext":
            return (self.ext, _natural_key(self.name))
        if key == "size":
            return self.size
        if key == "duration":
            return self.duration
        if key == "mtime":
            return self.mtime
        return self.seq

    def describe(self) -> str:
        if self.info is None:
            return ""
        if self.info.warn:
            return "⚠ " + self.info.warn
        parts = [self.info.summary()]
        if self.info.duration:
            parts.append(self.info.duration_label)
        parts.append(human_size(self.info.size))
        return " · ".join(p for p in parts if p)


# The order the 序号 column reflects, and for a merge the order clips are joined in.
SORT_KEYS = [
    ("index", "原始加入顺序"),
    ("name", "文件名"),
    ("ext", "文件类型"),
    ("size", "文件大小"),
    ("duration", "时长"),
    ("mtime", "修改时间"),
]


class FileTable(ttk.Frame):
    """The batch list. Progress is drawn as a text bar in a cell, which keeps
    a few hundred rows cheap -- an embedded widget per row would not be."""

    COLUMNS = (("index", "序号", 46, "center"),
               ("name", "文件", 250, "w"),
               ("info", "信息", 330, "w"),
               ("status", "状态", 80, "center"),
               ("progress", "进度", 130, "center"),
               ("elapsed", "耗时", 70, "center"))

    def __init__(self, master, on_change=None, on_drop=None):
        super().__init__(master)
        self.on_change = on_change
        self.on_drop = on_drop
        self.items: list[FileItem] = []
        self._counter = 0

        columns = [c[0] for c in self.COLUMNS]
        self.tree = ttk.Treeview(self, columns=columns, show="headings",
                                 selectmode="extended", height=4)
        for key, title, width, anchor in self.COLUMNS:
            self.tree.heading(key, text=title, anchor=anchor)
            self.tree.column(key, width=px(width), minwidth=px(width // 2),
                             anchor=anchor, stretch=(key in ("name", "info")))
        for status, colour in STATUS_COLORS.items():
            self.tree.tag_configure(status, foreground=colour)
        self.tree.tag_configure("odd", background="#f7f8fa")

        vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.tree.bind("<<TreeviewSelect>>", lambda e: self._changed())
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        # drag a row to reorder; the number column follows
        self.tree.bind("<ButtonPress-1>", self._drag_press)
        self.tree.bind("<B1-Motion>", self._drag_motion)
        self.tree.bind("<ButtonRelease-1>", self._drag_release)
        if HAS_DND and on_drop is not None:
            self.tree.drop_target_register(DND_FILES)
            self.tree.dnd_bind("<<Drop>>", self._dropped)

    # -- dnd ----------------------------------------------------------------
    def _dropped(self, event):
        paths = parse_drop(event.data)
        if paths and self.on_drop:
            self.on_drop(paths)

    def register_drop(self, widget):
        if HAS_DND and self.on_drop is not None:
            try:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._dropped)
            except Exception:
                pass

    # -- mutation -----------------------------------------------------------
    def add_paths(self, paths: list[str]) -> list[FileItem]:
        existing = {os.path.normcase(os.path.abspath(i.path)) for i in self.items}
        added: list[FileItem] = []
        for p in paths:
            key = os.path.normcase(os.path.abspath(p))
            if key in existing:
                continue
            existing.add(key)
            self._counter += 1
            item = FileItem(path=p, seq=self._counter)
            item.row_id = f"r{self._counter}"
            self.items.append(item)
            self.tree.insert("", "end", iid=item.row_id,
                             values=self._values(item),
                             tags=(item.status, "odd" if len(self.items) % 2 else ""))
            added.append(item)
        self._recolour()
        self._renumber()
        self._changed()
        return added

    def _recolour(self) -> None:
        for i, item in enumerate(self.items):
            self.tree.item(item.row_id,
                           tags=(item.status, "odd" if i % 2 else ""))

    def _values(self, item: FileItem) -> tuple:
        index = self.items.index(item) + 1 if item in self.items else 0
        return (str(index), item.name, item.detail or item.describe(), item.status,
                progress_bar(item.progress) if item.status in ("转换中", "完成") else "",
                human_time(item.elapsed) if item.elapsed else "")

    def _renumber(self) -> None:
        """The number is the processing order, which is also the merge order --
        without it there is no way to tell which clip comes first."""
        for i, item in enumerate(self.items):
            try:
                self.tree.set(item.row_id, "index", str(i + 1))
            except tk.TclError:
                pass

    def sort_by_name(self) -> None:
        """Order by file name, comparing digit runs as numbers."""
        self.sort_by("name", True)

    def sort_by(self, key: str, ascending: bool = True) -> None:
        """Reorder by a file attribute.

        Values come from the filesystem as well as from probing, so sorting
        works even before a file has been analysed.
        """
        self.items.sort(key=lambda it: it.sort_value(key), reverse=not ascending)
        for i, item in enumerate(self.items):
            self.tree.move(item.row_id, "", i)
        self._recolour()
        self._renumber()
        self._changed()

    # ---- drag to reorder ---------------------------------------------------
    def _drag_press(self, event) -> None:
        self._drag_row = self.tree.identify_row(event.y)
        self._drag_y = event.y
        self._dragged = False

    def _drag_motion(self, event) -> None:
        row = getattr(self, "_drag_row", "")
        if not row:
            return
        if not self._dragged:
            if abs(event.y - self._drag_y) < 6:
                return
            self._dragged = True
            self.tree.configure(cursor="hand2")
        target = self.tree.identify_row(event.y)
        if not target or target == row:
            return
        # live reorder: drop the dragged row where the pointer is
        src = next((i for i, it in enumerate(self.items) if it.row_id == row), None)
        dst = next((i for i, it in enumerate(self.items) if it.row_id == target), None)
        if src is None or dst is None or src == dst:
            return
        self.items.insert(dst, self.items.pop(src))
        self.tree.move(row, "", dst)
        self._recolour()

    def _drag_release(self, _event=None) -> None:
        if getattr(self, "_dragged", False):
            self.tree.configure(cursor="")
            self._renumber()
            self._changed()
        self._drag_row = ""
        self._dragged = False

    def refresh(self, item: FileItem) -> None:
        try:
            self.tree.item(item.row_id, values=self._values(item),
                           tags=(item.status,))
        except tk.TclError:
            pass

    def remove_selected(self) -> None:
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self.items = [i for i in self.items if i.row_id != iid]
        self._recolour()
        self._renumber()
        self._changed()

    def clear(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        self._changed()

    def selected_items(self) -> list[FileItem]:
        sel = set(self.tree.selection())
        return [i for i in self.items if i.row_id in sel]

    def move(self, delta: int) -> None:
        """Reorder the selected rows -- this is how merge order is set."""
        sel = set(self.tree.selection())
        if not sel:
            return
        indices = [i for i, it in enumerate(self.items) if it.row_id in sel]
        order = range(len(self.items)) if delta < 0 else range(len(self.items) - 1, -1, -1)
        moved = False
        for i in order:
            if i not in indices:
                continue
            j = i + delta
            if j < 0 or j >= len(self.items) or j in indices:
                continue
            self.items[i], self.items[j] = self.items[j], self.items[i]
            moved = True
        if not moved:
            return
        for i, item in enumerate(self.items):
            self.tree.move(item.row_id, "", i)
        self._recolour()
        self._renumber()
        self.tree.selection_set([i.row_id for i in self.items if i.row_id in sel])

    def _changed(self) -> None:
        if self.on_change:
            self.on_change()


class FilePicker(ttk.Frame):
    """Output directory choice: next to the source, or a folder you pick."""

    def __init__(self, master, var_same_dir: tk.BooleanVar, var_dir: tk.StringVar):
        super().__init__(master)
        self.var_same = var_same_dir
        self.var_dir = var_dir
        self.columnconfigure(3, weight=1)

        ttk.Radiobutton(self, text="输出到源文件所在目录", value=True,
                        variable=self.var_same,
                        command=self._sync).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(self, text="输出到指定目录", value=False,
                        variable=self.var_same,
                        command=self._sync).grid(row=0, column=1, sticky="w", padx=(14, 8))
        self.entry = ttk.Entry(self, textvariable=self.var_dir)
        self.entry.grid(row=0, column=3, sticky="ew", padx=(0, 6))
        self.btn = ttk.Button(self, text="浏览…", width=8, command=self._browse)
        self.btn.grid(row=0, column=4)
        self._sync()

    def _sync(self) -> None:
        state = "disabled" if self.var_same.get() else "normal"
        self.entry.configure(state=state)
        self.btn.configure(state=state)

    def _browse(self) -> None:
        initial = self.var_dir.get() or os.path.expanduser("~")
        chosen = filedialog.askdirectory(title="选择输出目录", initialdir=initial)
        if chosen:
            self.var_dir.set(chosen)

    def resolved(self) -> Path | None:
        if self.var_same.get():
            return None
        d = self.var_dir.get().strip()
        return Path(d) if d else None


class ProbeWorker:
    """Probes added files off the UI thread and reports back via a callback."""

    def __init__(self, on_result, on_done=None):
        self.on_result = on_result
        self.on_done = on_done
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def cancel(self) -> None:
        self._cancel.set()

    def start(self, pairs) -> None:
        self._cancel.clear()

        def run():
            for item, path in pairs:
                if self._cancel.is_set():
                    break
                try:
                    info = media.probe(path)
                except Exception as exc:
                    item.info = None
                    item.detail = f"读取失败: {exc}"
                    item.error = str(exc)
                else:
                    item.info = info
                    item.detail = info.summary()
                    if info.warn:
                        item.error = info.warn
                self.on_result(item)
            if self.on_done:
                self.on_done()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()


class LogPanel(ttk.Frame):
    """Collapsible detail pane; shows the active file's note/warnings/errors."""

    def __init__(self, master, height=7):
        super().__init__(master)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.text = tk.Text(self, height=height, wrap="word", relief="flat",
                            background="#fbfbfd", foreground="#374151",
                            font=("Consolas", 9), padx=8, pady=6,
                            insertwidth=0, highlightthickness=0)
        self.text.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")
        self.text.configure(state="disabled")

    def write(self, message: str, replace: bool = False) -> None:
        self.text.configure(state="normal")
        if replace:
            self.text.delete("1.0", "end")
        self.text.insert("end", message.rstrip() + "\n")
        self.text.see("end")
        self.text.configure(state="disabled")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
