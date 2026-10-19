"""Video tab: container conversion, quality, resize, crop, trim, merge, GIF."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .. import geometry as G
from .. import media, plan
from ..presets import (ASPECT_PRESETS, BITRATES_KBPS, VIDEO_AUDIO_CODECS,
                      VIDEO_CONTAINERS, VIDEO_ENCODERS, ConcatOptions, GifSpec,
                      TrimSpec, VideoOptions)
from .base import FILE_TYPES_VIDEO, BaseTab
from .trim_bar import TrimBar
from .widgets import ChoiceBox, parse_time

SCALE_MODES = [
    ("none", "原始尺寸"),
    ("width", "按宽度"),
    ("height", "按高度"),
    ("longest", "限制最长边"),
    ("percent", "按百分比"),
    ("box", "限制在方框内"),
    ("exact", "精确尺寸"),
]

QUALITY_CHOICES = [
    ("smart", "智能（能无损就无损）"),
    ("lossless", "强制无损复制"),
    ("high", "高质量"),
    ("balanced", "均衡"),
    ("small", "小体积"),
    ("tiny", "极小体积"),
    ("target", "指定目标体积"),
]

AUDIO_CHOICES = [
    ("auto", "自动（能复制就复制）"),
    ("copy", "强制复制原音轨"),
    ("none", "不要音频"),
    ("aac", "AAC"),
    ("mp3", "MP3"),
    ("opus", "Opus"),
    ("flac", "FLAC 无损"),
    ("ac3", "AC3"),
    ("pcm_s16le", "PCM 无损"),
]

ASPECT_CHOICES = [(label, label) for label, _w, _h in ASPECT_PRESETS]
ASPECT_DIMS = {label: (w, h) for label, w, h in ASPECT_PRESETS}


class VideoTab(BaseTab):
    title = "视频"
    subtitle = "MP4 / MOV / MKV / WebM / AVI / TS / FLV / GIF 互转，裁剪、压缩、拼接"
    allowed_exts = media.VIDEO_EXTS
    filetypes = FILE_TYPES_VIDEO
    default_concurrency = 2
    supports_concat = True

    # ------------------------------------------------------------- option UI
    def build_options(self, parent) -> None:
        grid = parent
        for c in (1, 3, 5):
            grid.columnconfigure(c, weight=1)

        self.var_mode = tk.StringVar(value="convert")
        self.var_target = tk.StringVar(value="20")
        self.var_w = tk.StringVar(value="1080")
        self.var_h = tk.StringVar(value="1080")
        self.var_percent = tk.StringVar(value="50")
        self.var_no_upscale = tk.BooleanVar(value=True)
        self.var_t_start = tk.StringVar(value="")
        self.var_t_end = tk.StringVar(value="")
        self.var_precise = tk.BooleanVar(value=False)
        self.var_faststart = tk.BooleanVar(value=True)

        # row 0 -- mode
        row = ttk.Frame(grid)
        row.grid(row=0, column=0, columnspan=8, sticky="ew", padx=10, pady=(6, 2))
        ttk.Radiobutton(row, text="格式转换 / 压缩", value="convert",
                        variable=self.var_mode, command=self._mode_changed).pack(side="left")
        ttk.Radiobutton(row, text="拼接多个视频", value="concat",
                        variable=self.var_mode, command=self._mode_changed).pack(
            side="left", padx=(16, 0))
        self.lbl_mode_note = ttk.Label(row, text="", foreground="#6b7280")
        self.lbl_mode_note.pack(side="left", padx=(14, 0))

        # row 1 -- container / quality / target
        ttk.Label(grid, text="输出格式").grid(row=1, column=0, sticky="w", padx=(10, 4), pady=2)
        self._fmt_keys = self._available_containers()
        self.cmb_fmt = ChoiceBox(grid, [(k, VIDEO_CONTAINERS[k].label) for k in self._fmt_keys],
                                 "mp4", width=24, on_change=self._fmt_changed)
        self.cmb_fmt.grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Label(grid, text="画质").grid(row=1, column=2, sticky="e", padx=(14, 4), pady=2)
        self.cmb_quality = ChoiceBox(grid, QUALITY_CHOICES, "smart", width=22,
                                     on_change=self._option_changed)
        self.cmb_quality.grid(row=1, column=3, sticky="ew", pady=2)
        self.lbl_target = ttk.Label(grid, text="目标 MB")
        self.ent_target = ttk.Entry(grid, textvariable=self.var_target, width=8)
        self.lbl_target.grid(row=1, column=4, sticky="e", padx=(14, 4), pady=2)
        self.ent_target.grid(row=1, column=5, sticky="w", pady=2)

        # row 2 -- encoder
        ttk.Label(grid, text="编码器").grid(row=2, column=0, sticky="w", padx=(10, 4), pady=2)
        cont = VIDEO_CONTAINERS[self.cmb_fmt.key()]
        self.cmb_encoder = ChoiceBox(grid, self._encoder_choices(cont), "auto", width=24,
                                     on_change=self._option_changed)
        self.cmb_encoder.grid(row=2, column=1, sticky="ew", pady=2)
        self.lbl_encoder_note = ttk.Label(grid, text="", foreground="#6b7280")
        self.lbl_encoder_note.grid(row=2, column=2, columnspan=4, sticky="w",
                                   padx=(14, 4), pady=2)

        # row 3 -- audio
        ttk.Label(grid, text="音频").grid(row=3, column=0, sticky="w", padx=(10, 4), pady=2)
        self.cmb_audio = ChoiceBox(grid, self._audio_choices(cont), "auto", width=24,
                                   on_change=self._option_changed)
        self.cmb_audio.grid(row=3, column=1, sticky="ew", pady=2)
        ttk.Label(grid, text="码率").grid(row=3, column=2, sticky="e", padx=(14, 4), pady=2)
        self.cmb_abr = ChoiceBox(grid, [(str(b), str(b)) for b in BITRATES_KBPS], "128",
                                 width=8, on_change=self._option_changed)
        self.cmb_abr.grid(row=3, column=3, sticky="w", pady=2)
        ttk.Checkbutton(grid, text="MP4/MOV 加快速起播标记", variable=self.var_faststart
                        ).grid(row=3, column=4, columnspan=3, sticky="w",
                               padx=(14, 4), pady=2)

        # row 4 -- size
        ttk.Label(grid, text="画面尺寸").grid(row=4, column=0, sticky="w", padx=(10, 4), pady=2)
        self.cmb_scale = ChoiceBox(grid, SCALE_MODES, "none", width=24,
                                  on_change=self._option_changed)
        self.cmb_scale.grid(row=4, column=1, sticky="ew", pady=2)
        self.frm_size = ttk.Frame(grid)
        self.frm_size.grid(row=4, column=2, columnspan=5, sticky="w", padx=(14, 4), pady=2)
        ttk.Label(self.frm_size, text="宽").pack(side="left")
        self.ent_w = ttk.Entry(self.frm_size, textvariable=self.var_w, width=7)
        self.ent_w.pack(side="left", padx=(4, 8))
        ttk.Label(self.frm_size, text="高").pack(side="left")
        self.ent_h = ttk.Entry(self.frm_size, textvariable=self.var_h, width=7)
        self.ent_h.pack(side="left", padx=(4, 8))
        ttk.Label(self.frm_size, text="百分比").pack(side="left")
        self.ent_pct = ttk.Entry(self.frm_size, textvariable=self.var_percent, width=6)
        self.ent_pct.pack(side="left", padx=(4, 8))
        ttk.Checkbutton(self.frm_size, text="不放大",
                        variable=self.var_no_upscale).pack(side="left")

        # row 5 -- crop
        ttk.Label(grid, text="裁剪比例").grid(row=5, column=0, sticky="w", padx=(10, 4), pady=2)
        self.cmb_aspect = ChoiceBox(grid, ASPECT_CHOICES, "原始", width=24,
                                    on_change=self._option_changed)
        self.cmb_aspect.grid(row=5, column=1, sticky="ew", pady=2)
        self.lbl_aspect_note = ttk.Label(grid, text="从画面中心裁切，不会拉伸变形",
                                         foreground="#6b7280")
        self.lbl_aspect_note.grid(row=5, column=2, columnspan=4, sticky="w",
                                  padx=(14, 4), pady=2)

        # row 6 -- segment selection (a real timeline, not just two time fields)
        self.frm_trim = ttk.LabelFrame(grid, text="截取片段（剪辑）")
        self.frm_trim.grid(row=6, column=0, columnspan=8, sticky="ew",
                           padx=(10, 10), pady=(4, 1))
        self.frm_trim.columnconfigure(4, weight=1)

        ttk.Label(self.frm_trim, text="从").grid(row=0, column=0, sticky="w",
                                               padx=(8, 3), pady=(4, 1))
        self.ent_t_start = ttk.Entry(self.frm_trim, textvariable=self.var_t_start,
                                     width=10)
        self.ent_t_start.grid(row=0, column=1, sticky="w", pady=(4, 1))
        ttk.Label(self.frm_trim, text="到").grid(row=0, column=2, sticky="e",
                                               padx=(10, 3), pady=(4, 1))
        self.ent_t_end = ttk.Entry(self.frm_trim, textvariable=self.var_t_end, width=10)
        self.ent_t_end.grid(row=0, column=3, sticky="w", pady=(4, 1))
        ttk.Label(self.frm_trim, text="（1:30 或 90.5，留空为不限制）",
                  foreground="#9ca3af").grid(row=0, column=4, sticky="w",
                                             padx=(8, 8), pady=(4, 1))
        self.chk_precise = ttk.Checkbutton(self.frm_trim, text="逐帧精确（需重编码）",
                                          variable=self.var_precise)
        self.chk_precise.grid(row=0, column=5, sticky="e", padx=(4, 10), pady=(4, 1))

        self.trimbar = TrimBar(self.frm_trim, on_change=self._on_timeline_change)
        self.trimbar.grid(row=1, column=0, columnspan=6, sticky="ew",
                          padx=(8, 10), pady=(0, 5))

        # row 7 -- gif specifics
        self.frm_gif = ttk.Frame(grid)
        self.frm_gif.grid(row=7, column=0, columnspan=8, sticky="ew", padx=10, pady=2)
        ttk.Label(self.frm_gif, text="GIF 帧率").pack(side="left")
        self.cmb_gif_fps = ChoiceBox(self.frm_gif,
                                     [(v, v) for v in ("24", "20", "15", "12", "10", "8", "6")],
                                     "12", width=5, on_change=self._option_changed)
        self.cmb_gif_fps.pack(side="left", padx=(4, 12))
        ttk.Label(self.frm_gif, text="颜色数").pack(side="left")
        self.cmb_gif_colors = ChoiceBox(
            self.frm_gif, [(v, v) for v in ("256", "192", "128", "96", "64", "32")],
            "256", width=5, on_change=self._option_changed)
        self.cmb_gif_colors.pack(side="left", padx=(4, 12))
        ttk.Label(self.frm_gif, text="颜色越少体积越小",
                  foreground="#9ca3af").pack(side="left")

        for var in (self.var_w, self.var_h, self.var_percent, self.var_target):
            var.trace_add("write", lambda *_: self._option_changed())
        for var in (self.var_no_upscale, self.var_precise, self.var_faststart):
            var.trace_add("write", lambda *_: self._option_changed())
        # Typing times and dragging the timeline are two views of one selection.
        for var in (self.var_t_start, self.var_t_end):
            var.trace_add("write", lambda *_: self._trim_text_changed())

        self._timeline_path: str | None = None
        self._timeline_info = None
        self._mode_changed()

    # ---- segment selection
    def _on_timeline_change(self, start, end) -> None:
        """Timeline -> text fields."""
        self.var_t_start.set("" if start is None else f"{start:.2f}")
        self.var_t_end.set("" if end is None else f"{end:.2f}")

    def _trim_text_changed(self) -> None:
        """Text fields -> timeline."""
        if getattr(self, "trimbar", None) is not None:
            self.trimbar.set_range(parse_time(self.var_t_start.get()),
                                   parse_time(self.var_t_end.get()), notify=False)
        self._option_changed()

    def _sync_timeline(self) -> None:
        """Show the selected clip's filmstrip (merging has no per-clip trim)."""
        if not hasattr(self, "trimbar"):
            return
        if self.var_mode.get() == "concat":
            self.trimbar.load(None)
            self._timeline_path = None
            self._timeline_info = None
            return
        items = self.table.selected_items() or self.table.items
        item = items[0] if items else None
        path = item.path if item is not None else None
        info = item.info if item is not None else None
        # Also reload when the probe finishes: the path is unchanged, but the
        # media information the timeline needs has only just appeared.
        if path != self._timeline_path or info is not self._timeline_info:
            self._timeline_path = path
            self._timeline_info = info
            self.trimbar.load(info, label=item.name if item else "")
            self.trimbar.set_range(parse_time(self.var_t_start.get()),
                                   parse_time(self.var_t_end.get()), notify=False)

    def _on_selection(self, *args) -> None:
        super()._on_selection(*args)
        self._sync_timeline()

    # ---- capability-filtered lists
    def _available_containers(self) -> list[str]:
        keys = []
        for key, cont in VIDEO_CONTAINERS.items():
            if key == "gif":
                if (self.caps.has_encoder("gif") and self.caps.has_filter("palettegen")
                        and self.caps.has_filter("paletteuse")):
                    keys.append(key)
                continue
            if not self.caps.has_muxer(cont.muxer):
                continue
            if any(self.caps.has_encoder(e.encoder) for e in VIDEO_ENCODERS.values()
                   if e.family in cont.video):
                keys.append(key)
        return keys or ["mp4"]

    def _encoder_choices(self, cont) -> list[tuple[str, str]]:
        out = [("auto", "自动（按容器选最合适）")]
        for key, enc in VIDEO_ENCODERS.items():
            if enc.family not in cont.video:
                continue
            if not self.caps.has_encoder(enc.encoder):
                continue
            # A listed hardware encoder that fails a real trial encode is worse
            # than no option at all: the job would just fail at run time.
            if enc.hardware and not self.caps.encoding_works(enc.encoder):
                continue
            out.append((key, enc.label))
        return out

    def _audio_choices(self, cont) -> list[tuple[str, str]]:
        out = []
        for key, label in AUDIO_CHOICES:
            if key in ("auto", "copy", "none"):
                out.append((key, label))
            elif cont.audio and key in cont.audio:
                enc = VIDEO_AUDIO_CODECS.get(key)
                if enc is None or self.caps.has_encoder(enc[0]):
                    out.append((key, label))
        return out

    # ---- reactions
    def _fmt_changed(self, _key=None) -> None:
        cont = VIDEO_CONTAINERS.get(self.cmb_fmt.key(), VIDEO_CONTAINERS["mp4"])
        self.cmb_encoder.set_choices(self._encoder_choices(cont))
        self.cmb_audio.set_choices(self._audio_choices(cont))
        self._option_changed()

    def _mode_changed(self) -> None:
        concat = self.var_mode.get() == "concat"
        self.lbl_mode_note.configure(
            text="所有片段按「序号」列的顺序首尾相接；尺寸与裁剪不适用" if concat
            else "同一批文件共用下面的设置")

        # Merging ignores per-clip geometry and time ranges, so hide the controls
        # rather than leave settings that would be silently dropped.
        self.cmb_aspect.enable(not concat)
        self.lbl_aspect_note.configure(
            text="" if concat else "从画面中心裁切，不会拉伸变形")
        self.cmb_scale.enable(not concat)
        for w in (self.ent_t_start, self.ent_t_end, self.chk_precise):
            w.configure(state="disabled" if concat else "normal")
        if concat:
            self.frm_trim.grid_remove()
        else:
            self.frm_trim.grid()

        if concat:
            self.cmb_quality.set_choices(
                [(k, lab) for k, lab in QUALITY_CHOICES if k != "target"], "smart")
        else:
            self.cmb_quality.set_choices(QUALITY_CHOICES)

        self.cmb_encoder.set_choices(
            self._encoder_choices(VIDEO_CONTAINERS[self.cmb_fmt.key()]), "auto")
        self._option_changed()
        self._sync_timeline()

    def _option_changed(self, _key=None) -> None:
        key = self.cmb_fmt.key()
        if key == "gif":
            self.frm_gif.grid()
        else:
            self.frm_gif.grid_remove()

        mode = self.cmb_scale.key()
        concat = self.var_mode.get() == "concat"
        if concat:
            mode = "none"          # merging ignores per-clip geometry entirely
        self.ent_w.configure(state="normal" if mode in ("width", "longest", "box", "exact")
                             else "disabled")
        self.ent_h.configure(state="normal" if mode in ("height", "box", "exact")
                             else "disabled")
        self.ent_pct.configure(state="normal" if mode == "percent" else "disabled")

        if self.cmb_quality.key() == "target":
            self.lbl_target.grid()
            self.ent_target.grid()
        else:
            self.lbl_target.grid_remove()
            self.ent_target.grid_remove()

        enc = VIDEO_ENCODERS.get(self.cmb_encoder.key())
        note = ""
        if enc is not None:
            note = enc.note + ("（已实测可用）" if enc.hardware else "")
        elif key == "gif":
            note = "GIF 会自动缩放并生成两遍调色板"
        self.lbl_encoder_note.configure(text=note)
        self._on_selection()

    def refresh_capabilities(self) -> None:
        self._fmt_keys = self._available_containers()
        self.cmb_fmt.set_choices([(k, VIDEO_CONTAINERS[k].label) for k in self._fmt_keys])
        self._fmt_changed()

    # ------------------------------------------------------------- planning
    def _build_options_obj(self) -> VideoOptions:
        label = self.cmb_aspect.key()
        aw, ah = ASPECT_DIMS.get(label, (0, 0))
        crop = (G.CropSpec(mode="aspect", aspect_w=aw, aspect_h=ah) if aw and ah
                else G.CropSpec(mode="none"))

        mode = self.cmb_scale.key()
        scale = G.ScaleSpec(mode=mode, no_upscale=self.var_no_upscale.get())
        scale.width = _int(self.var_w.get(), 1080)
        scale.height = _int(self.var_h.get(), 1080)
        scale.longest = _int(self.var_w.get(), 1080)
        scale.box_w = scale.width
        scale.box_h = scale.height
        scale.percent = _float(self.var_percent.get(), 50.0)

        trim = TrimSpec(precise=self.var_precise.get())
        if self.var_mode.get() != "concat":
            trim.start = parse_time(self.var_t_start.get())
            trim.end = parse_time(self.var_t_end.get())

        fps = _int(self.cmb_gif_fps.key(), 12)
        gif = GifSpec(
            fps=fps,
            max_colors=_int(self.cmb_gif_colors.key(), 256),
            dither=("sierra2_4a" if fps >= 15
                    else "bayer:bayer_scale=5:diff_mode=rectangle"),
        )

        return VideoOptions(
            container=self.cmb_fmt.key(),
            mode=self.cmb_quality.key(),
            encoder_key=self.cmb_encoder.key(),
            target_mb=_float(self.var_target.get(), 20.0),
            crop=crop if crop.mode != "none" else None,
            scale=scale if scale.mode != "none" else None,
            trim=trim,
            audio_codec=self.cmb_audio.key(),
            audio_bitrate=_int(self.cmb_abr.key(), 128),
            faststart=self.var_faststart.get(),
            gif=gif,
        )

    def preview_scope(self, selected: list, all_items: list) -> list:
        if self.var_mode.get() == "concat":
            return all_items
        return selected or all_items

    def capture_options(self):
        opts = self._build_options_obj()
        return {
            "mode": self.var_mode.get(),
            "video": opts,
            # Merge honours the quality choice too: "智能" re-encodes when the
            # container needs it, "强制无损复制" trades compatibility for speed.
            "concat": ConcatOptions(container=opts.container, mode=opts.mode,
                                    encoder_key=opts.encoder_key,
                                    audio_bitrate=opts.audio_bitrate,
                                    faststart=opts.faststart),
        }

    def plan_one(self, item, out_dir, taken, options=None):
        opts = (options or self.capture_options())["video"]
        return plan.plan_video(item.info, out_dir, opts, taken=taken)

    def plan_batch(self, items, out_dir, taken, options=None):
        snap = options or self.capture_options()
        if snap["mode"] != "concat":
            return super().plan_batch(items, out_dir, taken, snap)
        if len(items) < 2:
            raise ValueError("拼接至少需要两个文件")
        job = plan.plan_concat([i.info for i in items], out_dir, snap["concat"],
                               taken=taken)
        return [(it, job) for it in items]

    def option_summary(self) -> str:
        if self.var_mode.get() == "concat":
            return f"拼接为 {self.cmb_fmt.label()}"
        opts = self._build_options_obj()
        bits = [opts.container.upper(), self.cmb_quality.label()]
        if opts.scale:
            bits.append("缩放")
        if opts.crop:
            bits.append("裁剪 " + self.cmb_aspect.label())
        if opts.trim.active:
            bits.append("截取时长")
        if opts.mode == "target":
            bits.append(f"目标 {opts.target_mb:g}MB")
        return " · ".join(bits)


def _int(text, fallback: int) -> int:
    try:
        return int(float(str(text).strip()))
    except (ValueError, TypeError):
        return fallback


def _float(text, fallback: float) -> float:
    try:
        return float(str(text).strip())
    except (ValueError, TypeError):
        return fallback
