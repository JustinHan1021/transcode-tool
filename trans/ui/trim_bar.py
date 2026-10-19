"""A filmstrip timeline for choosing a segment of a clip.

Typing timestamps blind is not editing: to cut two seconds out of a recording
you have to see where those two seconds are. So this widget pulls a row of
thumbnails out of the file, draws them on a time ruler, and puts draggable
handles over them.

Alongside it sits a preview panel showing the frame at the current position with
the time it belongs to, and able to play the clip in place. Without that, finding
the moment you want means opening the file in another player and cross-referencing
timestamps, which is exactly the chore this panel exists to remove.

Thumbnails and playback frames come from worker threads; Tk images are only ever
created on the UI thread.
"""
from __future__ import annotations

import base64
import atexit
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from .. import ff
from ..media import MediaInfo
from .widgets import fmt_clock, px

MARGIN = 8
STRIP_H = 44
CANVAS_H = 74
HANDLE_W = 7
HANDLE_GRAB = 10          # pixels either side of a handle that grab it
MIN_SELECT = 0.05         # a shorter drag counts as a click
DRAG_SLOP = 3             # pixels of movement before a click becomes a drag
POLL_MS = 40
FRAME_THROTTLE = 0.12     # seconds between scrub frames while dragging

PLAY_FPS = 12
PREVIEW_WIDTH = 180       # logical px; the panel sits beside the filmstrip, so
                          # its height sets the whole row's height
PNG_SIG = b"\x89PNG\r\n\x1a\n"

QUICK_LENGTHS = ["0.5", "1", "2", "3", "5", "10", "30"]

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Scratch directories holding extracted thumbnails and preview frames. Tk does
# not call a child widget's destroy() when the root is torn down, so relying on
# destroy() alone leaves the last one behind on every exit. An exit hook makes
# the cleanup unconditional.
_TEMP_DIRS: set[str] = set()
_TEMP_LOCK = threading.Lock()


def _register_temp(path: Path) -> None:
    with _TEMP_LOCK:
        _TEMP_DIRS.add(str(path))


def _forget_temp(path: Path) -> None:
    with _TEMP_LOCK:
        _TEMP_DIRS.discard(str(path))


@atexit.register
def _cleanup_temp_dirs() -> None:
    with _TEMP_LOCK:
        paths = list(_TEMP_DIRS)
        _TEMP_DIRS.clear()
    for p in paths:
        shutil.rmtree(p, ignore_errors=True)


_SWEPT = False


def sweep_stale_temp(older_than_hours: float = 24.0) -> int:
    """Delete scratch left behind by an earlier run that died without cleanup.

    atexit covers a normal exit (and a closed window); a force-kill or a crash
    skips it, so a sweep on startup keeps those from piling up.
    """
    global _SWEPT
    if _SWEPT:
        return 0
    _SWEPT = True
    cutoff = time.time() - older_than_hours * 3600
    removed = 0
    try:
        entries = list(Path(tempfile.gettempdir()).glob("trans_thumbs_*"))
        entries += list(Path(tempfile.gettempdir()).glob("trans_preview_*"))
    except OSError:
        return 0
    for path in entries:
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def _popen_kwargs(**extra) -> dict:
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL, "creationflags": _NO_WINDOW}
    kwargs.update(extra)
    return kwargs


def _run(args, timeout=120, capture=False):
    try:
        return subprocess.run([str(a) for a in args], timeout=timeout,
                              **_popen_kwargs(stderr=subprocess.PIPE if capture
                                              else subprocess.DEVNULL))
    except Exception:
        return None


def split_png(buf: bytes):
    """Pull one complete PNG out of a byte stream.

    Returns (frame_bytes or None, leftover).
    """
    if not buf.startswith(PNG_SIG):
        start = buf.find(PNG_SIG)
        if start < 0:
            return None, buf[-len(PNG_SIG):] if len(buf) > len(PNG_SIG) else buf
        buf = buf[start:]
    pos = len(PNG_SIG)
    while True:
        if len(buf) < pos + 8:
            return None, buf
        length = int.from_bytes(buf[pos:pos + 4], "big")
        ctype = buf[pos + 4:pos + 8]
        end = pos + 12 + length
        if length < 0 or length > 64 * 1024 * 1024:
            return None, b""                  # desynchronised; drop it
        if len(buf) < end:
            return None, buf
        pos = end
        if ctype == b"IEND":
            return buf[:pos], buf[pos:]


def open_path(path: str) -> None:
    try:
        os.startfile(path)                      # noqa: S606 - deliberate shell open
    except AttributeError:
        subprocess.Popen(["xdg-open", path])


class TrimBar(ttk.Frame):
    """Segment picker with a preview panel.

    `on_change(start, end)` reports the selection in seconds; None means "no
    limit on that side".
    """

    def __init__(self, master, on_change=None):
        super().__init__(master)
        self.on_change = on_change
        self.info: MediaInfo | None = None
        self._duration = 0.0
        self._start: float | None = None
        self._end: float | None = None
        self._playhead = 0.0
        self._thumbs: list[tk.PhotoImage] = []
        self._preview_img: tk.PhotoImage | None = None
        self._placeholder: tk.PhotoImage | None = None
        self._playhead_items: list[int] = []
        self._drag: str | None = None
        self._drag_anchor = 0.0
        self._press_x = 0.0
        self._dragged = False
        self._queue: queue.Queue = queue.Queue()
        self._token = 0
        self._temp: Path | None = None
        self._thumb_count = 0
        self._thumb_after: str | None = None
        self._preview_after: str | None = None
        self._preview_at = 0.0
        self._poll_job: str | None = None
        self._controls: list[ttk.Widget] = []
        sweep_stale_temp()
        self._frame_q: queue.Queue = queue.Queue(maxsize=4)
        self._play_stop = threading.Event()
        self._play_proc: subprocess.Popen | None = None
        self._play_thread: threading.Thread | None = None
        self._play_tick_job: str | None = None
        self._play_index = 0
        self._play_from = 0.0
        self._playing = False

        self.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(self, height=px(CANVAS_H), background="#f3f4f6",
                                highlightthickness=0, cursor="arrow")
        self.canvas.grid(row=0, column=0, sticky="ew", padx=(0, px(8)))
        self.canvas.bind("<Configure>", self._on_resize)
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_hover)

        self._build_preview()

        bar = ttk.Frame(self)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(px(3), 0))

        def button(text, width, command):
            b = ttk.Button(bar, text=text, width=width, command=command)
            self._controls.append(b)
            return b

        button("设为起点", 9, lambda: self._set_edge("start")).pack(side="left")
        button("设为终点", 9, lambda: self._set_edge("end")).pack(side="left", padx=px(4))
        button("整段", 6, self.clear_range).pack(side="left")

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=px(10))
        self.var_len = tk.StringVar(value="2")
        self.cmb_len = ttk.Combobox(bar, textvariable=self.var_len, width=5,
                                    values=QUICK_LENGTHS)
        self.cmb_len.pack(side="left")
        self._controls.append(self.cmb_len)
        ttk.Label(bar, text="秒").pack(side="left", padx=(px(3), px(6)))
        button("从当前位置起", 13, lambda: self._quick("from")).pack(side="left")
        button("以当前位置居中", 14, lambda: self._quick("center")).pack(side="left",
                                                                       padx=px(4))

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=px(10))
        self.btn_play = button("▶ 播放", 10, self.toggle_play)
        self.btn_play.pack(side="left")
        self.btn_clip = button("带声音回放", 11, self.preview_clip)
        self.btn_clip.pack(side="left", padx=px(4))

        self.var_status = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.var_status, foreground="#1d4ed8",
                  wraplength=px(620), justify="left").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(px(4), 0))
        self.var_hint = tk.StringVar(value="")
        # Without a wraplength a long hint widens the frame and the filmstrip
        # jumps around every time the text changes.
        ttk.Label(self, textvariable=self.var_hint, foreground="#6b7280",
                  wraplength=px(700), justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w")

        self._set_enabled(False)

    def _build_preview(self) -> None:
        """Image plus its timestamp. The play button lives in the controls row
        below: stacking it here made this panel the tallest thing in the tab and
        pushed the rest of the settings off the bottom of the window."""
        box = ttk.Frame(self)
        box.grid(row=0, column=1, sticky="n")
        self.preview = tk.Label(box, background="#e5e7eb", foreground="#9ca3af",
                                text="预览", font=("Segoe UI", 9))
        self.preview.pack()
        self.var_clock = tk.StringVar(value="—")
        ttk.Label(box, textvariable=self.var_clock, foreground="#374151").pack(
            pady=(px(2), 0))

    # ------------------------------------------------------------------ state
    def _set_enabled(self, on: bool) -> None:
        for w in self._controls:
            if isinstance(w, ttk.Combobox):
                w.configure(state="readonly" if on else "disabled")
            else:
                w.configure(state="normal" if on else "disabled")
        self.canvas.configure(cursor="crosshair" if on else "arrow")

    def _place_holder(self, info: MediaInfo | None) -> None:
        """Keep the preview from changing size as frames come and go."""
        if info and info.width and info.height:
            width = max(px(120), int(px(PREVIEW_WIDTH) * info.width / info.height))
        else:
            width = px(PREVIEW_WIDTH)
        height = int(width * (info.height / info.width)) if (info and info.width) \
            else px(PREVIEW_WIDTH * 9 / 16)
        img = tk.PhotoImage(width=width, height=height)
        img.put("#e5e7eb", to=(0, 0, width, height))
        self._placeholder = img
        self.preview.configure(image=img, text="")

    @property
    def preview_width(self) -> int:
        return int(px(PREVIEW_WIDTH))

    @property
    def has_media(self) -> bool:
        return self._duration > 0

    def get_range(self) -> tuple[float | None, float | None]:
        return self._start, self._end

    # ------------------------------------------------------------------- load
    def load(self, info: MediaInfo | None, label: str = "") -> None:
        self._stop_play()
        self._token += 1
        self._cancel_preview()
        self._clear_temp()
        self._thumbs = []
        self._thumb_count = 0
        self.canvas.delete("all")
        self._preview_img = None
        self._playhead_items = []

        self.info = info
        self._start = self._end = None
        self._playhead = 0.0
        self._duration = 0.0
        self._place_holder(info)
        self.var_clock.set("—")
        self._ensure_poll()

        if info is None:
            self._set_enabled(False)
            self.var_status.set("")
            self.var_hint.set("选一个文件后，这里会出现它的时间轴和预览画面")
            self._draw()
            return
        if not info.has_video:
            self._set_enabled(False)
            self.var_status.set("")
            self.var_hint.set("这个文件没有画面，无法预览（音频页可以直接填时间等截取）")
            self._draw()
            return
        if not info.duration:
            self._set_enabled(False)
            self.var_status.set("")
            self.var_hint.set("这个文件读不到时长，无法显示时间轴")
            self._draw()
            return

        self._duration = float(info.duration)
        self._set_enabled(True)
        self.var_hint.set(f"{label}　点时间轴任意位置即可跳到并预览该处；"
                          f"拖动两端选择片段；▶ 播放可在这里直接看（无声）")
        self._refresh_status()
        self._draw()
        self._kick_thumbs()
        self._request_preview(immediate=True)

    def set_range(self, start: float | None, end: float | None,
                  notify: bool = False) -> None:
        """Mirror values typed into the time fields (notify=False)."""
        if not self.has_media:
            return
        new_start = None if start is None else max(0.0, min(start, self._duration))
        new_end = None if end is None else max(0.0, min(end, self._duration))
        if new_start is not None and new_end is not None and new_end < new_start:
            new_start, new_end = new_end, new_start
        if (new_start, new_end) == (self._start, self._end):
            return
        self._start, self._end = new_start, new_end
        self._refresh_status()
        self._draw()
        if notify and self.on_change:
            self.on_change(self._start, self._end)

    def clear_range(self) -> None:
        self.set_range(None, None, notify=True)

    # ------------------------------------------------------------- geometry
    def _span(self) -> tuple[float, float]:
        width = max(self.canvas.winfo_width(), 80)
        return float(MARGIN), float(width - MARGIN)

    def _time_to_x(self, t: float) -> float:
        left, right = self._span()
        if self._duration <= 0:
            return left
        return left + (max(0.0, min(t, self._duration)) / self._duration) * (right - left)

    def _x_to_time(self, x: float) -> float:
        left, right = self._span()
        if right <= left or self._duration <= 0:
            return 0.0
        frac = (x - left) / (right - left)
        return max(0.0, min(1.0, frac)) * self._duration

    def _effective(self) -> tuple[float, float]:
        return (self._start if self._start is not None else 0.0,
                self._end if self._end is not None else self._duration)

    def _whole_range_selected(self) -> bool:
        start, end = self._effective()
        return (start <= 1e-6) and (end >= self._duration - 1e-6)

    # ---------------------------------------------------------------- drawing
    def _on_resize(self, _event=None) -> None:
        if self.has_media:
            self._kick_thumbs()
        self._draw()

    def _desired_thumbs(self) -> int:
        width = max(self.canvas.winfo_width(), 80)
        return max(6, min(18, width // px(64)))

    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        self._playhead_items = []
        width = max(c.winfo_width(), 80)
        if not self.has_media:
            c.create_text(width / 2, px(CANVAS_H) / 2, text="（暂无时间轴）",
                          fill="#9ca3af")
            return

        top = px(8)
        bottom = top + px(STRIP_H)

        count = len(self._thumbs)
        for i, img in enumerate(self._thumbs):
            t = self._duration * (i / (count - 1)) if count > 1 else 0.0
            c.create_image(self._time_to_x(t), (top + bottom) / 2, image=img,
                           anchor="center")
        c.create_rectangle(MARGIN, top, width - MARGIN, bottom, outline="#d1d5db")

        for i in range(5):
            t = self._duration * i / 4
            x = self._time_to_x(t)
            c.create_line(x, bottom, x, bottom + px(4), fill="#9ca3af")
            c.create_text(min(max(x, px(20)), width - px(20)), bottom + px(12),
                          text=fmt_clock(t), fill="#6b7280", font=("Segoe UI", 7))

        start, end = self._effective()
        for a, b in ((0.0, start), (end, self._duration)):
            if b - a > 1e-6:
                c.create_rectangle(self._time_to_x(a), top, self._time_to_x(b), bottom,
                                   fill="#ffffff", outline="", stipple="gray50")

        x1, x2 = self._time_to_x(start), self._time_to_x(end)
        c.create_rectangle(x1, top, x2, bottom, outline="#2563eb", width=px(2))
        half = px(HANDLE_W) / 2
        for x in (x1, x2):
            c.create_rectangle(x - half, top - px(3), x + half, bottom + px(3),
                               fill="#2563eb", outline="")
        self._draw_playhead()

    def _draw_playhead(self) -> None:
        c = self.canvas
        top, bottom = px(8), px(8) + px(STRIP_H)
        ph = self._time_to_x(self._playhead)
        line = c.create_line(ph, top - px(7), ph, bottom + px(7), fill="#dc2626",
                             width=px(1), tags="playhead")
        tri = c.create_polygon(ph - px(4), top - px(7), ph + px(4), top - px(7), ph,
                               top - px(2), fill="#dc2626", tags="playhead")
        self._playhead_items = [line, tri]

    def _move_playhead(self) -> None:
        """Reposition just the marker: full redraws 12 times a second to follow
        playback would be wasteful."""
        if len(self._playhead_items) != 2:
            self._draw_playhead()
            return
        c = self.canvas
        top, bottom = px(8), px(8) + px(STRIP_H)
        ph = self._time_to_x(self._playhead)
        try:
            c.coords(self._playhead_items[0], ph, top - px(7), ph, bottom + px(7))
            c.coords(self._playhead_items[1], ph - px(4), top - px(7),
                     ph + px(4), top - px(7), ph, top - px(2))
        except tk.TclError:
            self._playhead_items = []

    # ------------------------------------------------------------ interaction
    def _on_hover(self, event) -> None:
        if not self.has_media or self._drag is not None:
            return
        x = self.canvas.canvasx(event.x)
        start, end = self._effective()
        near = (abs(x - self._time_to_x(start)) <= px(HANDLE_GRAB)
                or abs(x - self._time_to_x(end)) <= px(HANDLE_GRAB))
        self.canvas.configure(cursor="sb_h_double_arrow" if near else "crosshair")

    def _on_press(self, event) -> None:
        if not self.has_media:
            return
        x = self.canvas.canvasx(event.x)
        self._press_x = x
        self._dragged = False
        start, end = self._effective()
        tol = px(HANDLE_GRAB)
        if abs(x - self._time_to_x(start)) <= tol:
            self._drag = "start"
        elif abs(x - self._time_to_x(end)) <= tol:
            self._drag = "end"
        elif self._time_to_x(start) < x < self._time_to_x(end):
            self._drag = "band"
            self._drag_anchor = self._x_to_time(x)
        else:
            self._drag = "playhead"
            self._playhead = self._x_to_time(x)
            self._request_preview(immediate=True)
        self._refresh_status()
        self._draw()

    def _on_drag(self, event) -> None:
        if not self.has_media or self._drag is None:
            return
        x = self.canvas.canvasx(event.x)
        if abs(x - self._press_x) > px(DRAG_SLOP):
            self._dragged = True
        t = self._x_to_time(x)

        if self._drag == "start":
            limit = (self._end if self._end is not None else self._duration)
            self._start = max(0.0, min(t, limit - MIN_SELECT))
            self._playhead = self._start if not self._dragged else self._start
            self._request_preview()
        elif self._drag == "end":
            floor = self._start if self._start is not None else 0.0
            self._end = min(self._duration, max(t, floor + MIN_SELECT))
            self._playhead = self._end
            self._request_preview()
        elif self._drag == "band":
            if self._whole_range_selected():
                # Nothing to slide, so dragging inside is a scrub instead.
                self._playhead = t
                self._request_preview()
            else:
                start, end = self._effective()
                length = end - start
                new_start = max(0.0, min(start + (t - self._drag_anchor),
                                         self._duration - length))
                self._start = new_start
                self._end = new_start + length
                self._drag_anchor = t
        else:
            self._playhead = t
            self._request_preview()
        self._refresh_status()
        self._draw()

    def _on_release(self, _event=None) -> None:
        was = self._drag
        was_drag = self._dragged
        self._drag = None
        self._dragged = False
        if was in ("start", "end") and was_drag:
            if self.on_change:
                self.on_change(self._start, self._end)
        elif was == "band" and not was_drag:
            # A plain click inside the range is a seek, not a failed drag --
            # which is what made the playhead impossible to move before.
            self._playhead = self._x_to_time(self._press_x)
            self._request_preview(immediate=True)
            self._refresh_status()
            self._draw()
        elif was == "playhead":
            self._request_preview(immediate=True)
        self._refresh_status()

    def _set_edge(self, which: str) -> None:
        if not self.has_media:
            return
        t = self._playhead
        if which == "start":
            self._start = t
            if self._end is not None and self._end <= t:
                self._end = None
        else:
            self._end = t
            if self._start is not None and self._start >= t:
                self._start = None
        self._refresh_status()
        self._draw()
        if self.on_change:
            self.on_change(self._start, self._end)

    def _quick(self, mode: str) -> None:
        if not self.has_media:
            return
        try:
            length = float(self.var_len.get())
        except ValueError:
            return
        length = max(MIN_SELECT, min(length, self._duration))
        start = self._playhead - length / 2 if mode == "center" else self._playhead
        start = max(0.0, min(start, self._duration - length))
        self._start = start
        self._end = start + length
        self._playhead = start
        self._refresh_status()
        self._draw()
        self._request_preview(immediate=True)
        if self.on_change:
            self.on_change(self._start, self._end)

    def _refresh_status(self) -> None:
        if not self.has_media:
            self.var_status.set("")
            return
        start, end = self._effective()
        self.var_status.set(
            f"已选 {end - start:.2f} 秒（{fmt_clock(start)} → {fmt_clock(end)}）"
            f"　总长 {fmt_clock(self._duration)}　当前 {fmt_clock(self._playhead)}")
        self.var_clock.set(f"{fmt_clock(self._playhead)} / {fmt_clock(self._duration)}")

    # ------------------------------------------------------------ thumbnails
    def _clear_temp(self) -> None:
        if self._temp is not None:
            shutil.rmtree(self._temp, ignore_errors=True)
            _forget_temp(self._temp)
            self._temp = None

    def _temp_dir(self) -> Path:
        if self._temp is None or not self._temp.exists():
            self._temp = Path(tempfile.mkdtemp(prefix="trans_thumbs_"))
            _register_temp(self._temp)
        return self._temp

    def _kick_thumbs(self) -> None:
        if self._thumb_after:
            try:
                self.after_cancel(self._thumb_after)
            except Exception:
                pass

        def start():
            self._thumb_after = None
            wanted = self._desired_thumbs()
            if wanted == self._thumb_count:
                return
            self._thumb_count = wanted
            self._token += 1
            self._ensure_poll()
            # Resolved on the UI thread: the worker must not touch Tk.
            threading.Thread(target=self._extract_thumbs,
                             args=(self._token, wanted), daemon=True).start()

        self._thumb_after = self.after(80, start)

    def _extract_thumbs(self, token: int, count: int) -> None:
        info, duration = self.info, self._duration
        if info is None or not duration:
            return
        try:
            exe = str(ff.detect().ffmpeg)
        except Exception:
            return
        temp = Path(tempfile.mkdtemp(prefix="trans_thumbs_"))
        # Re-extraction (a resize, a reload) must not orphan the previous one.
        previous, self._temp = self._temp, temp
        if previous is not None and previous != temp:
            shutil.rmtree(previous, ignore_errors=True)
            _forget_temp(previous)
        _register_temp(temp)
        height = px(STRIP_H)
        paths: list[str] = []
        for i in range(count):
            if token != self._token:
                shutil.rmtree(temp, ignore_errors=True)
                _forget_temp(temp)
                return
            t = duration * (i / (count - 1)) if count > 1 else 0.0
            out = temp / f"t{i:02d}.png"
            _run([exe, "-hide_banner", "-loglevel", "error", "-y",
                  "-ss", f"{t:.3f}", "-i", str(info.path), "-frames:v", "1",
                  "-vf", f"scale=-2:{height}", "-f", "image2", str(out)])
            if out.exists() and out.stat().st_size:
                paths.append(str(out))
        if token == self._token:
            self._queue.put(("thumbs", token, paths))

    # -------------------------------------------------------- frame preview
    def _request_preview(self, immediate: bool = False) -> None:
        """Fetch the frame at the playhead, coalescing rapid requests.

        Called while dragging too, so the picture follows the handles instead of
        only appearing when you click.
        """
        if self._preview_after is not None:
            return                       # a fetch is already queued; it will use
                                         # whatever the playhead is by then
        now = time.time()
        delay = 0 if immediate else max(0, int((FRAME_THROTTLE
                                                - (now - self._preview_at)) * 1000))
        self._preview_after = self.after(delay, self._do_preview)

    def _cancel_preview(self) -> None:
        if self._preview_after:
            try:
                self.after_cancel(self._preview_after)
            except Exception:
                pass
            self._preview_after = None

    def _do_preview(self) -> None:
        self._preview_after = None
        self._preview_at = time.time()
        if self.info is None or not self.has_media:
            return
        out = self._temp_dir() / "_frame.png"
        threading.Thread(target=self._extract_frame,
                         args=(self._token, self._playhead, out), daemon=True).start()

    def _extract_frame(self, token: int, t: float, out: Path) -> None:
        try:
            exe = str(ff.detect().ffmpeg)
        except Exception:
            return
        _run([exe, "-hide_banner", "-loglevel", "error", "-y",
              "-ss", f"{t:.3f}", "-i", str(self.info.path), "-frames:v", "1",
              "-vf", f"scale=w={self.preview_width}:h=-2", "-f", "image2", str(out)])
        if out.exists() and out.stat().st_size:
            self._queue.put(("frame", token, str(out)))

    # ------------------------------------------------------------- playback
    def toggle_play(self) -> None:
        if self._playing:
            self._stop_play()
        else:
            self._start_play()

    def _start_play(self) -> None:
        if not self.has_media or self.info is None or self._playing:
            return
        start, end = self._effective()
        if not (start <= self._playhead < end):
            # outside the selection: play from wherever makes sense
            self._playhead = start if abs(self._playhead - end) < 1e-6 else self._playhead
        self._play_from = self._playhead
        self._play_index = 0
        self._playing = True
        self._play_stop.clear()
        while True:
            try:
                self._frame_q.get_nowait()
            except queue.Empty:
                break
        self.btn_play.configure(text="■ 停止")
        self.var_hint.set("正在预览播放（无声）。要带声音看，用「带声音回放」")
        self._play_thread = threading.Thread(target=self._reader,
                                             args=(self._play_from, end), daemon=True)
        self._play_thread.start()
        self._schedule_tick(0)

    def _reader(self, start: float, end: float) -> None:
        try:
            exe = str(ff.detect().ffmpeg)
        except Exception:
            return
        length = max(0.1, end - start)
        args = [exe, "-hide_banner", "-loglevel", "error",
                "-ss", f"{start:.3f}", "-i", str(self.info.path),
                "-t", f"{length:.3f}",
                "-vf", f"fps={PLAY_FPS},scale=w={self.preview_width}:h=-2",
                "-f", "image2pipe", "-vcodec", "png", "-"]
        try:
            proc = subprocess.Popen([str(a) for a in args],
                                    **_popen_kwargs(stdout=subprocess.PIPE))
        except Exception:
            return
        self._play_proc = proc
        buf = b""
        try:
            while not self._play_stop.is_set():
                chunk = proc.stdout.read(1 << 16)
                if not chunk:
                    break
                buf += chunk
                while True:
                    frame, buf = split_png(buf)
                    if frame is None:
                        break
                    payload = base64.b64encode(frame).decode("ascii")
                    while not self._play_stop.is_set():
                        try:
                            self._frame_q.put(payload, timeout=0.2)
                            break
                        except queue.Full:
                            continue
                    else:
                        return
        except Exception:
            pass
        finally:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=3)
            except Exception:
                pass
            self._play_proc = None

    def _schedule_tick(self, delay: int) -> None:
        self._play_tick_job = self.after(delay, self._play_tick)

    def _play_tick(self) -> None:
        self._play_tick_job = None
        if not self._playing:
            return
        try:
            payload = self._frame_q.get_nowait()
        except queue.Empty:
            # nothing yet (encoder warming up) or finished
            if self._play_thread is not None and self._play_thread.is_alive():
                self._schedule_tick(int(1000 / PLAY_FPS))
                return
            self._stop_play()
            return
        try:
            self._preview_img = tk.PhotoImage(data=payload)
        except tk.TclError:
            self._schedule_tick(int(1000 / PLAY_FPS))
            return
        self.preview.configure(image=self._preview_img, text="")
        self._playhead = min(self._play_from + self._play_index / PLAY_FPS,
                             self._duration)
        self._play_index += 1
        self._move_playhead()
        self._refresh_status()
        self._schedule_tick(int(1000 / PLAY_FPS))

    def _stop_play(self) -> None:
        was = self._playing
        self._playing = False
        self._play_stop.set()
        if self._play_tick_job:
            try:
                self.after_cancel(self._play_tick_job)
            except Exception:
                pass
            self._play_tick_job = None
        proc = self._play_proc
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
        if was:
            try:
                self.btn_play.configure(text="▶ 播放")
            except tk.TclError:
                pass

    # ------------------------------------------------------------------ poll
    def _ensure_poll(self) -> None:
        """One poll loop for the widget's lifetime.

        Stopping it whenever the queue looked empty killed it before any worker
        finished, and results then landed in a queue nobody was reading.
        """
        if self._poll_job is None:
            self._poll_job = self.after(POLL_MS, self._poll)

    def _poll(self) -> None:
        for _ in range(60):
            try:
                msg = self._queue.get_nowait()
            except queue.Empty:
                break
            kind = msg[0]
            if kind == "thumbs":
                _, token, paths = msg
                if token != self._token:
                    continue
                self._thumbs = []
                for p in paths:
                    try:
                        self._thumbs.append(tk.PhotoImage(file=p))
                    except tk.TclError:
                        continue
                self._draw()
            elif kind == "frame":
                _, token, path = msg
                if token != self._token or self._playing:
                    continue
                try:
                    self._preview_img = tk.PhotoImage(file=path)
                except tk.TclError:
                    continue
                self.preview.configure(image=self._preview_img, text="")
            elif kind == "clip":
                _, ok, path, message = msg
                self.btn_clip.configure(state="normal")
                if ok:
                    self.var_hint.set("已生成带声音的预览片段，正在用系统播放器打开…")
                    open_path(path)
                else:
                    self.var_hint.set(f"预览片段生成失败：{message}")
        self._poll_job = self.after(POLL_MS, self._poll)

    def destroy(self) -> None:
        self._stop_play()
        self._token += 1
        self._cancel_preview()
        for job in (self._poll_job, self._thumb_after):
            if job:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
        self._clear_temp()
        super().destroy()

    # ------------------------------------------------------- clip preview
    def preview_clip(self) -> None:
        """Render just the selection at low resolution and open it, with sound.

        Watching the chosen seconds is the difference between guessing the cut
        is right and knowing it.
        """
        if not self.has_media or self.info is None:
            return
        start, end = self._effective()
        length = end - start
        if length < MIN_SELECT:
            self.var_hint.set("选中的片段太短，没法回放")
            return
        self.var_hint.set("正在生成带声音的预览片段…")
        self.btn_clip.configure(state="disabled")
        threading.Thread(target=self._render_clip, args=(start, length),
                         daemon=True).start()

    def _render_clip(self, start: float, length: float) -> None:
        try:
            exe = str(ff.detect().ffmpeg)
        except Exception:
            return
        temp = Path(tempfile.mkdtemp(prefix="trans_preview_"))
        out = temp / "preview.mp4"
        args = [exe, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{start:.3f}", "-i", str(self.info.path),
                "-t", f"{length:.3f}", "-vf", f"scale=-2:{px(360)}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                "-pix_fmt", "yuv420p"]
        args += (["-c:a", "aac", "-b:a", "96k"] if self.info.has_audio else ["-an"])
        args += ["-movflags", "+faststart", str(out)]
        proc = _run(args, timeout=900, capture=True)
        ok = bool(proc and proc.returncode == 0 and out.exists()
                  and out.stat().st_size > 0)
        message = ""
        if not ok:
            tail = (proc.stderr or b"").decode("utf-8", "replace") if proc else ""
            lines = [l for l in tail.strip().splitlines() if l.strip()]
            message = lines[-1][:160] if lines else "未知错误"
        self._queue.put(("clip", ok, str(out), message))
