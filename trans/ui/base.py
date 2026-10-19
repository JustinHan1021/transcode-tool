"""Shared tab behaviour: batch list, planning, running and progress.

Each tab only supplies its option panel, the file types it accepts, and how to
turn one file into a Job. Everything else -- probing, concurrency, cancellation,
progress, per-file error reporting -- lives here.
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .. import plan
from ..engine import Engine
from .widgets import (HAS_DND, SORT_KEYS, FilePicker, FileTable, LogPanel,
                      ProbeWorker, ScrollArea, human_time, px)

FILE_TYPES_VIDEO = [
    ("视频文件", "*.mp4 *.mov *.mkv *.webm *.avi *.ts *.m4v *.flv *.wmv *.mpg "
                 "*.mpeg *.m2ts *.mts *.3gp *.ogv *.vob *.asf *.gif"),
    ("所有文件", "*.*"),
]
FILE_TYPES_IMAGE = [
    ("图片文件", "*.png *.jpg *.jpeg *.jfif *.webp *.bmp *.tif *.tiff *.gif *.avif "
                 "*.heic *.heif *.ico *.tga"),
    ("所有文件", "*.*"),
]
FILE_TYPES_AUDIO = [
    ("音频/视频文件", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.oga *.opus *.wma "
                      "*.aiff *.aif *.amr *.ac3 *.mp4 *.mov *.mkv *.avi *.webm *.flv"),
    ("所有文件", "*.*"),
]


class BaseTab(ttk.Frame):
    # ---- overridden by each tab
    title = ""
    subtitle = ""
    allowed_exts: set[str] = set()
    filetypes = FILE_TYPES_VIDEO
    default_concurrency = 2
    supports_concat = False

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.caps = app.caps
        self.engine: Engine | None = None
        self._events: queue.Queue = queue.Queue()
        self._runner: threading.Thread | None = None
        self._running_items: list = []
        self._by_job: dict[int, object] = {}
        self._groups: dict[int, list] = {}
        self._logged_jobs: set[int] = set()
        self._job_started: dict[int, float] = {}
        self._job_step: dict[int, str] = {}
        self._preview_after: str | None = None

        self.var_same_dir = tk.BooleanVar(value=True)
        self.var_out_dir = tk.StringVar(value=str(Path.home() / "Desktop"))
        self.var_concurrency = tk.IntVar(value=self.default_concurrency)

        self._build()
        # Probing happens on a worker thread, which cannot touch Tk. A poll loop
        # is how those results reach the table -- so it has to run from the
        # start, not only while a conversion is in flight.
        self._was_running = False
        self._pump()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        head = ttk.Frame(self)
        head.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 6))
        head.columnconfigure(1, weight=1)
        ttk.Label(head, text=self.title, font=("Segoe UI", 13, "bold")).grid(
            row=0, column=0, sticky="w")
        ttk.Label(head, text=self.subtitle, foreground="#6b7280").grid(
            row=0, column=1, sticky="w", padx=(10, 0))

        paned = ttk.Panedwindow(self, orient="vertical")
        paned.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 10))

        # ---- batch list
        box = ttk.LabelFrame(paned, text="待处理文件")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        paned.add(box, weight=0)

        self.table = FileTable(box, on_change=self._on_selection, on_drop=self._on_drop)
        self.table.grid(row=0, column=0, sticky="nsew", padx=8, pady=(6, 4))

        bar = ttk.Frame(box)
        bar.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 8))
        ttk.Button(bar, text="添加文件", command=self.add_files).pack(side="left")
        ttk.Button(bar, text="添加文件夹", command=self.add_folder).pack(side="left", padx=4)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(bar, text="移除所选", command=self.table.remove_selected).pack(side="left")
        ttk.Button(bar, text="清空", command=self.table.clear).pack(side="left", padx=4)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(bar, text="上移", width=6,
                   command=lambda: self.table.move(-1)).pack(side="left")
        ttk.Button(bar, text="下移", width=6,
                   command=lambda: self.table.move(1)).pack(side="left", padx=4)

        ttk.Label(bar, text="排序").pack(side="left", padx=(8, 4))
        self.var_sort = tk.StringVar(value=SORT_KEYS[0][1])
        self.var_sort_desc = tk.BooleanVar(value=False)
        self.cmb_sort = ttk.Combobox(bar, textvariable=self.var_sort, state="readonly",
                                     width=13, values=[label for _k, label in SORT_KEYS])
        self.cmb_sort.pack(side="left")
        self.cmb_sort.bind("<<ComboboxSelected>>", lambda e: self._apply_sort())
        self.btn_sort_dir = ttk.Button(bar, text="升序", width=6,
                                       command=self._toggle_sort_dir)
        self.btn_sort_dir.pack(side="left", padx=4)

        hint = ("拖进列表，或直接拖动某一行调整顺序" if HAS_DND
                else "把文件拖到 启动.bat 上也可批量导入；用上移/下移调顺序")
        ttk.Label(bar, text=hint, foreground="#9ca3af").pack(side="right")

        # ---- options (scrollable: the settings and the trim panel together are
        # taller than a scaled 1080p screen can show at once, and the screen
        # height is not something the app can negotiate)
        self.opts_area = ScrollArea(paned)
        paned.add(self.opts_area, weight=1)
        self.opts = ttk.LabelFrame(self.opts_area.body, text="转换设置")
        self.opts.pack(fill="both", expand=True)
        self.opts.columnconfigure(0, weight=1)
        self.build_options(self.opts)

        # ---- run bar
        run = ttk.Frame(self)
        run.grid(row=2, column=0, sticky="ew", padx=12)
        run.columnconfigure(3, weight=1)

        self.btn_run = ttk.Button(run, text="开始转换", width=14, command=self.start)
        self.btn_run.grid(row=0, column=0)
        self.btn_cancel = ttk.Button(run, text="取消", width=8, state="disabled",
                                     command=self.cancel)
        self.btn_cancel.grid(row=0, column=1, padx=6)

        ttk.Label(run, text="同时处理").grid(row=0, column=2, padx=(14, 4))
        self.spin_conc = ttk.Spinbox(run, from_=1, to=16, width=3,
                                     textvariable=self.var_concurrency)
        self.spin_conc.grid(row=0, column=3, sticky="w")

        self.var_progress = tk.StringVar(value="就绪")
        ttk.Label(run, textvariable=self.var_progress, foreground="#374151").grid(
            row=0, column=4, sticky="e", padx=8)

        # ---- output location + preview
        bottom = ttk.Frame(self)
        bottom.grid(row=3, column=0, sticky="ew", padx=12, pady=(8, 4))
        bottom.columnconfigure(1, weight=1)
        ttk.Label(bottom, text="输出位置").grid(row=0, column=0, sticky="w", padx=(0, 8))
        picker = FilePicker(bottom, self.var_same_dir, self.var_out_dir)
        picker.grid(row=0, column=1, sticky="ew")
        self.picker = picker

        self.var_preview = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.var_preview, foreground="#1d4ed8",
                  wraplength=px(980), justify="left").grid(
            row=4, column=0, sticky="w", padx=12, pady=(2, 6))

        self.log = LogPanel(self, height=4)
        self.log.grid(row=5, column=0, sticky="ew", padx=12, pady=(0, 12))

        if HAS_DND:
            self.table.register_drop(self.opts)
            self.table.register_drop(self)

    # ---- hooks
    def build_options(self, parent) -> None:      # pragma: no cover - overridden
        raise NotImplementedError

    def plan_one(self, item, out_dir, taken, options=None):   # pragma: no cover
        raise NotImplementedError

    def capture_options(self):
        """Snapshot the option widgets into plain data.

        Called on the UI thread. Planning happens on a worker thread, and Tk
        variables may not be touched from there -- reading one raises
        "main thread is not in main loop", which would fail every job.
        """
        return None

    def plan_batch(self, items, out_dir, taken, options=None) -> list[tuple]:
        """Return (item, job) pairs.

        Most tabs plan one job per file. Merging returns the same job for every
        participating file, which the run loop collapses into a single job with
        several rows attached to it.
        """
        return [(it, self.plan_one(it, out_dir, taken, options)) for it in items]

    def option_summary(self) -> str:
        return ""

    def refresh_capabilities(self) -> None:
        """Called once the trial-encode pass finishes, so the encoder lists can
        gain or lose entries without a restart."""
        return None

    # ------------------------------------------------------------- file input
    def _accept(self, paths, notify: bool = True) -> bool:
        """Add files that belong to this tab. Returns whether any were taken."""
        from .widgets import collect_files
        files = collect_files(paths, self.allowed_exts)
        if not files:
            if notify:
                self.log.write("没有找到可处理的文件（本页只接受 %s）。"
                               % "、".join(sorted(e.lstrip('.') for e in self.allowed_exts)))
            return False
        added = self.table.add_paths(files)
        self._probe(added)
        return True

    def add_files(self) -> None:
        patterns = " ".join(t[1] for t in self.filetypes[:1])
        paths = filedialog.askopenfilenames(title="选择文件", filetypes=self.filetypes)
        if paths:
            self._accept(list(paths))

    def add_folder(self) -> None:
        d = filedialog.askdirectory(title="选择文件夹")
        if d:
            self._accept([d])

    def _on_drop(self, paths) -> None:
        self._accept(paths)

    def _apply_sort(self) -> None:
        key = next((k for k, label in SORT_KEYS if label == self.var_sort.get()),
                   SORT_KEYS[0][0])
        self.table.sort_by(key, ascending=not self.var_sort_desc.get())
        self.log.write("已按「%s」%s排序"
                       % (self.var_sort.get(),
                          "降序" if self.var_sort_desc.get() else "升序"))

    def _toggle_sort_dir(self) -> None:
        self.var_sort_desc.set(not self.var_sort_desc.get())
        self.btn_sort_dir.configure(text="降序" if self.var_sort_desc.get() else "升序")
        self._apply_sort()

    def _probe(self, items) -> None:
        if not items:
            return
        for it in items:
            it.status = "分析中"
            self.table.refresh(it)
        worker = getattr(self, "_probe_worker", None)
        if worker is not None and worker.busy:
            worker.cancel()
        self._probe_worker = ProbeWorker(self._probe_result)
        self._probe_worker.start([(it, it.path) for it in items])

    def _probe_result(self, item) -> None:
        self._events.put(("probe", item))

    # ------------------------------------------------------------ run control
    def start(self) -> None:
        if self.engine is not None and self.engine.is_running:
            return
        items = [i for i in self.table.items]
        if not items:
            messagebox.showinfo("没有文件", "请先添加要处理的文件。")
            return
        bad = [i for i in items if i.info is None]
        if bad:
            messagebox.showwarning(
                "还有文件未就绪",
                "以下文件尚未读取完成或无法读取，请稍候或先移除：\n" +
                "\n".join(i.name for i in bad[:8]))
            return
        out_dir = self.picker.resolved()
        if out_dir is not None:
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("输出目录不可用", str(exc))
                return

        for it in items:
            it.status = "等待"
            it.progress = 0.0
            it.elapsed = 0.0
            it.error = ""
            it.job = None
            self.table.refresh(it)
        self.log.clear()
        self._logged_jobs.clear()
        self._job_started.clear()
        self._job_step.clear()
        self.log.write("开始处理 %d 个文件 · %s" % (len(items), self.option_summary()))

        self._running_items = items
        self.btn_run.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.spin_conc.configure(state="disabled")

        conc = max(1, int(self.var_concurrency.get() or 1))
        options = self.capture_options()
        self._runner = threading.Thread(
            target=self._run_all, args=(items, out_dir, conc, options), daemon=True)
        self._was_running = True
        self._runner.start()

    def _run_all(self, items, out_dir, conc, options) -> None:
        taken: set[str] = set()
        try:
            planned = self.plan_batch(items, out_dir, taken, options)
        except Exception as exc:
            for it in items:
                it.status = "失败"
                it.error = str(exc)
                self._events.put(("item", it))
            self._events.put(("finished_all", 0, 0))
            return

        jobs = []
        groups: list[list] = []
        seen: set[int] = set()
        for it, job in planned:
            it.job = job
            if id(job) in seen:
                groups[jobs.index(job)].append(it)
                continue
            seen.add(id(job))
            jobs.append(job)
            groups.append([it])
        # The engine numbers jobs by their position in the list it is handed,
        # so the lookup has to be keyed by that index -- not by object identity.
        self._by_job = {idx: group[0] for idx, group in enumerate(groups)}
        self._groups = dict(enumerate(groups))

        if not jobs:
            self._events.put(("finished_all", 0, 0))
            return

        # Report what the planner decided before any bytes are written.
        for it, job in planned:
            self._events.put(("note", it))

        engine = Engine(self.caps.ffmpeg, on_event=self._on_event, log_lines=40)
        self.engine = engine
        results = []
        engine.run(jobs, concurrency=conc,
                   on_complete=lambda rs: results.extend(rs))
        ok = sum(1 for r in results if r.ok)
        self._events.put(("finished_all", ok, len(jobs)))

    def _on_event(self, ev: dict) -> None:
        self._events.put(("engine", ev))

    def cancel(self) -> None:
        if self.engine is not None:
            self.engine.cancel_all()
        self.log.write("已请求取消，正在结束 ffmpeg 进程…")

    # ------------------------------------------------------------ event pump
    def _pump(self) -> None:
        drained = 0
        try:
            while drained < 400:
                kind, *rest = self._events.get_nowait()
                drained += 1
                self._handle(kind, rest)
        except queue.Empty:
            pass

        active = self._runner is not None and self._runner.is_alive()
        if self._was_running and not active:
            self._was_running = False
            self._end_run()
        self.after(120, self._pump)

    def _handle(self, kind: str, rest) -> None:
        if kind == "probe":
            item = rest[0]
            item.status = "失败" if item.error else "等待"
            self.table.refresh(item)
            self._on_selection()
        elif kind == "engine":
            self._engine_event(rest[0])
        elif kind == "item":
            self.table.refresh(rest[0])
        elif kind == "plan_failed":
            self.log.write(f"[无法生成转换方案] {rest[0]}")
        elif kind == "note":
            item = rest[0]
            job = item.job
            jid = id(job)
            if jid in self._logged_jobs:
                return
            self._logged_jobs.add(jid)
            group = next((g for g in self._groups.values()
                          if g and g[0].job is job), [item])
            names = ", ".join(i.name for i in group)
            self.log.write(f"{names}  →  {os.path.basename(str(job.out))}   [{job.note}]")
            for w in job.warnings:
                self.log.write(f"    ! {w}")
        elif kind == "finished_all":
            ok, total = rest
            self.var_progress.set(f"完成 {ok}/{total}")
            self.log.write(f"—— 全部结束：成功 {ok} 个，失败 {total - ok} 个 ——")

    def _engine_event(self, ev: dict) -> None:
        etype = ev["type"]
        jid = ev.get("id")
        item = self._by_job.get(jid)
        if item is None:
            return
        group = self._groups.get(jid, [item])

        if etype == "started":
            self._job_started[jid] = time.time()
            for member in group:
                member.status = "转换中"
                self.table.refresh(member)

        elif etype in ("progress", "tick"):
            fraction = ev.get("fraction")
            for member in group:
                # Never leave a participating row looking idle: a merge has one
                # job behind several rows, and only one of them used to move.
                if member.status in ("等待", "分析中"):
                    member.status = "转换中"
                if fraction is not None:
                    member.progress = fraction
                member.elapsed = time.time() - self._job_started.get(jid, time.time())
                self.table.refresh(member)
            self._show_running_status(jid, ev)

        elif etype == "step":
            self._job_step[jid] = ev.get("label", "")
            self._show_running_status(jid, {})

        elif etype == "finished":
            ok = bool(ev.get("ok"))
            cancelled = bool(ev.get("cancelled"))
            for member in group:
                if ok:
                    member.status = "完成"
                    member.progress = 1.0
                elif cancelled:
                    member.status = "已取消"
                else:
                    member.status = "失败"
                    member.error = ev.get("error", "")
                member.elapsed = ev.get("elapsed", 0.0)
                self.table.refresh(member)
            if ok:
                self.log.write(f"[完成] {item.name} → {os.path.basename(str(item.job.out))}"
                               f"  {human_time(ev.get('elapsed', 0.0))}")
            else:
                self.log.write(f"[失败] {item.name}\n    {ev.get('error', '')}")

        elif etype == "drained":
            pass

    def _show_running_status(self, jid: int, ev: dict) -> None:
        """Keep the status line alive.

        It used to sit on "就绪" for the whole run, which made a long job look
        like a hung one. Now it reports the step, the source position already
        processed, the measured speed, and an estimate of what is left.
        """
        group = self._groups.get(jid, [])
        if not group:
            return
        fraction = ev.get("fraction")
        if fraction is None:
            others = [m.progress for m in group if m.progress]
            fraction = max(others) if others else None
        speed = ev.get("speed")
        step = ev.get("step") or self._job_step.get(jid, "")

        parts = []
        started = self._job_started.get(jid)
        if fraction is not None and fraction > 0:
            parts.append(f"{fraction * 100:.0f}%")
            if started:
                elapsed = time.time() - started
                if elapsed > 3 and fraction > 0.02:
                    left = elapsed * (1 - fraction) / fraction
                    parts.append("约剩 " + human_time(left))
        if speed:
            parts.append(f"{speed:.1f}x")
        if step:
            parts.append(step)
        if len(group) > 1:
            parts.insert(0, f"{len(group)} 个片段")
        if parts:
            self.var_progress.set("转换中 · " + " · ".join(parts))

    def _end_run(self) -> None:
        self._runner = None
        self.btn_run.configure(state="normal")
        self.btn_cancel.configure(state="disabled")
        self.spin_conc.configure(state="normal")
        done = [i for i in self._running_items if i.status == "完成"]
        failed = [i for i in self._running_items if i.status == "失败"]
        if self._running_items and not any(
                i.status in ("转换中", "等待") for i in self._running_items):
            if failed:
                self.var_progress.set(f"完成 {len(done)} · 失败 {len(failed)}")
            else:
                self.var_progress.set(f"全部完成 {len(done)}/{len(self._running_items)}")
        self._show_output_hint(done)

    def _show_output_hint(self, done) -> None:
        if not done:
            return
        folder = Path(done[0].job.out).parent
        self.log.write(f"输出目录：{folder}")

    # -------------------------------------------------------------- preview
    def _on_selection(self, *_args) -> None:
        if self._preview_after:
            try:
                self.after_cancel(self._preview_after)
            except Exception:
                pass
        self._preview_after = self.after(120, self._update_preview)

    def preview_scope(self, selected: list, all_items: list) -> list:
        """Which files the preview line describes.

        Merging always spans the whole list, so a selection there would make the
        preview complain about having too few files when the run would be fine.
        """
        return selected or all_items

    def _update_preview(self) -> None:
        self._preview_after = None
        if self.btn_run["state"] == "disabled":
            return
        items = self.preview_scope(self.table.selected_items(), list(self.table.items))
        if not items:
            self.var_preview.set(self._hint_text())
            return
        ready = [i for i in items if i.info is not None and not i.info.warn]
        if not ready:
            item = items[0]
            if item.info is None:
                self.var_preview.set("正在读取文件信息…")
            else:
                self.var_preview.set(f"⚠ {item.info.warn}")
            return
        try:
            pairs = self.plan_batch(ready, self.picker.resolved(), set(),
                                    self.capture_options())
        except Exception as exc:
            self.var_preview.set(f"当前设置无法处理：{exc}")
            return
        if not pairs:
            self.var_preview.set("")
            return
        item, job = pairs[0]
        if not job.steps:
            self.var_preview.set(job.warnings[0] if job.warnings else "")
            return
        extra = ""
        if len(pairs) > 1:
            if all(p[1] is job for p in pairs):
                # A merge: every row feeds one output, so say so.
                extra = f"\n{len(pairs)} 个片段将合并为一个文件"
            else:
                extra = f"\n另外 {len(pairs) - 1} 个文件使用同样设置"
        # Show what the planner has to say *before* the run starts: whether this
        # is a copy or a re-encode, how much content that means, and any
        # compatibility caveat. Silence here is what makes a long job look hung.
        notes = "".join(f"\n· {w}" for w in job.warnings[:2])
        self.var_preview.set(
            f"{item.name}  →  {os.path.basename(str(job.out))}\n"
            f"{job.note}{notes}{extra}")

    def _hint_text(self) -> str:
        return self.subtitle

    def show_command(self, item) -> str:
        """The exact command line, for the log -- useful when something fails."""
        if item.job is None:
            return ""
        lines = []
        for i, step in enumerate(item.job.steps, 1):
            lines.append(f"  步骤{i} {step.label}: \"{self.caps.ffmpeg}\" "
                         + " ".join(str(a) for a in step.args))
        return "\n".join(lines)

    def open_output_folder(self) -> None:
        done = [i for i in self.table.items if i.status == "完成" and i.job]
        if not done:
            return
        folder = Path(done[0].job.out).parent
        try:
            os.startfile(folder)                      # noqa: S606 - Windows shell open
        except AttributeError:
            subprocess.Popen(["xdg-open", str(folder)])
