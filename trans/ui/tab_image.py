"""Image tab: format conversion, resize, crop -- and merging stills into a GIF."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .. import geometry as G
from .. import media, plan
from ..presets import (ASPECT_PRESETS, FRAME_DELAYS, IMAGE_CONTAINERS, GifSpec,
                       ImageOptions, SequenceOptions)
from .base import FILE_TYPES_IMAGE, BaseTab
from .widgets import ChoiceBox

SCALE_MODES = [
    ("none", "原始尺寸"),
    ("width", "按宽度"),
    ("height", "按高度"),
    ("longest", "限制最长边"),
    ("percent", "按百分比"),
    ("box", "限制在方框内"),
    ("exact", "精确尺寸"),
]

ASPECT_CHOICES = [(label, label) for label, _w, _h in ASPECT_PRESETS]
ASPECT_DIMS = {label: (w, h) for label, w, h in ASPECT_PRESETS}

FORMAT_HINTS = {
    "jpg": "最通用；不支持透明，透明区域会被填充为白色",
    "png": "无损；支持透明；体积较大。品质滑块控制压缩强度，不影响画质",
    "webp": "体积通常比 JPG 小 25-35%；支持透明；现代平台都支持",
    "gif": "最多 256 色，适合动图/表情包；会自动生成调色板",
    "bmp": "无损未压缩，体积很大，一般只用于特定老软件",
    "tiff": "无损，印刷/归档用",
}

SEQUENCE_FORMATS = ("gif", "webp")
LOOP_CHOICES = [("0", "无限循环"), ("1", "只播一遍"), ("2", "播 2 遍"),
                ("3", "播 3 遍"), ("5", "播 5 遍"), ("10", "播 10 遍")]


class ImageTab(BaseTab):
    title = "图片"
    subtitle = "PNG / JPG / WebP / GIF / BMP / TIFF 互转，缩放裁剪，多图合成动图"
    allowed_exts = media.IMAGE_EXTS | {".gif"}
    filetypes = FILE_TYPES_IMAGE
    default_concurrency = 8

    def build_options(self, parent) -> None:
        grid = parent
        for c in (1, 3, 5):
            grid.columnconfigure(c, weight=1)

        self.var_mode = tk.StringVar(value="convert")
        self.var_quality = tk.StringVar(value="88")
        self.var_lossless = tk.BooleanVar(value=False)
        self.var_w = tk.StringVar(value="1080")
        self.var_h = tk.StringVar(value="1080")
        self.var_percent = tk.StringVar(value="50")
        self.var_no_upscale = tk.BooleanVar(value=True)
        self.var_animated = tk.BooleanVar(value=True)
        self.var_keep_meta = tk.BooleanVar(value=False)

        # --- row 0: what to do
        row = ttk.Frame(grid)
        row.grid(row=0, column=0, columnspan=8, sticky="ew", padx=10, pady=(6, 2))
        ttk.Radiobutton(row, text="每张各转一份", value="convert", variable=self.var_mode,
                        command=self._mode_changed).pack(side="left")
        ttk.Radiobutton(row, text="合成动图（所有图片拼成一个）", value="sequence",
                        variable=self.var_mode, command=self._mode_changed).pack(
            side="left", padx=(16, 0))
        self.lbl_mode_note = ttk.Label(row, text="", foreground="#6b7280")
        self.lbl_mode_note.pack(side="left", padx=(14, 0))

        # --- row 1: format + quality
        ttk.Label(grid, text="输出格式").grid(row=1, column=0, sticky="w", padx=(10, 4),
                                          pady=2)
        self._fmt_keys = self._available_formats()
        self.cmb_fmt = ChoiceBox(
            grid, [(k, IMAGE_CONTAINERS[k].label) for k in self._fmt_keys], None,
            width=28, on_change=self._fmt_changed)
        for key in ("webp", "jpg", "png"):
            if key in self._fmt_keys:
                self.cmb_fmt.set_key(key)
                break
        self.cmb_fmt.grid(row=1, column=1, sticky="ew", pady=2)

        ttk.Label(grid, text="品质").grid(row=1, column=2, sticky="e", padx=(14, 4), pady=2)
        self.scl_quality = ttk.Scale(grid, from_=1, to=100, orient="horizontal",
                                     variable=self.var_quality, command=self._quality_moved)
        self.scl_quality.grid(row=1, column=3, sticky="ew", pady=2)
        self.var_quality_label = tk.StringVar(value="88")
        ttk.Label(grid, textvariable=self.var_quality_label, width=4).grid(
            row=1, column=4, sticky="w", padx=(4, 8), pady=2)
        self.chk_lossless = ttk.Checkbutton(grid, text="WebP 无损",
                                            variable=self.var_lossless)
        self.chk_lossless.grid(row=1, column=5, columnspan=3, sticky="w",
                               padx=(4, 4), pady=2)

        self.lbl_hint = ttk.Label(grid, text="", foreground="#6b7280")
        self.lbl_hint.grid(row=2, column=0, columnspan=8, sticky="w", padx=10)

        # --- row 3: size
        ttk.Label(grid, text="尺寸").grid(row=3, column=0, sticky="w", padx=(10, 4), pady=2)
        self.cmb_scale = ChoiceBox(grid, SCALE_MODES, "none", width=28,
                                   on_change=self._option_changed)
        self.cmb_scale.grid(row=3, column=1, sticky="ew", pady=2)
        frm = ttk.Frame(grid)
        frm.grid(row=3, column=2, columnspan=6, sticky="w", padx=(14, 4), pady=2)
        ttk.Label(frm, text="宽").pack(side="left")
        self.ent_w = ttk.Entry(frm, textvariable=self.var_w, width=7)
        self.ent_w.pack(side="left", padx=(4, 8))
        ttk.Label(frm, text="高").pack(side="left")
        self.ent_h = ttk.Entry(frm, textvariable=self.var_h, width=7)
        self.ent_h.pack(side="left", padx=(4, 8))
        ttk.Label(frm, text="百分比").pack(side="left")
        self.ent_pct = ttk.Entry(frm, textvariable=self.var_percent, width=6)
        self.ent_pct.pack(side="left", padx=(4, 8))
        ttk.Checkbutton(frm, text="不放大", variable=self.var_no_upscale).pack(side="left")

        # --- row 4: crop + metadata
        ttk.Label(grid, text="裁剪比例").grid(row=4, column=0, sticky="w", padx=(10, 4),
                                          pady=2)
        self.cmb_aspect = ChoiceBox(grid, ASPECT_CHOICES, "原始", width=28,
                                    on_change=self._option_changed)
        self.cmb_aspect.grid(row=4, column=1, sticky="ew", pady=2)
        self.lbl_aspect_note = ttk.Label(grid, text="从画面中心裁切，不会拉伸变形",
                                        foreground="#6b7280")
        self.lbl_aspect_note.grid(row=4, column=2, columnspan=4, sticky="w",
                                  padx=(14, 4), pady=2)
        self.chk_keep = ttk.Checkbutton(grid, text="保留 EXIF 元数据",
                                        variable=self.var_keep_meta)
        self.chk_keep.grid(row=4, column=6, columnspan=2, sticky="w",
                           padx=(4, 4), pady=2)

        # --- row 5: animation (per-file mode)
        self.frm_anim = ttk.Frame(grid)
        self.frm_anim.grid(row=5, column=0, columnspan=8, sticky="ew", padx=10, pady=2)
        self.chk_anim = ttk.Checkbutton(self.frm_anim,
                                        text="保留动图（GIF / WebP 输入时）",
                                        variable=self.var_animated)
        self.chk_anim.pack(side="left")
        ttk.Label(self.frm_anim, text="GIF 帧率").pack(side="left", padx=(24, 4))
        self.cmb_gif_fps = ChoiceBox(
            self.frm_anim, [(v, v) for v in ("24", "20", "15", "12", "10", "8")],
            "12", width=5, on_change=self._option_changed)
        self.cmb_gif_fps.pack(side="left")
        ttk.Label(self.frm_anim, text="（仅动图输入时生效）",
                  foreground="#9ca3af").pack(side="left", padx=(8, 0))

        # --- row 6: slideshow settings (merge mode)
        self.frm_seq = ttk.LabelFrame(grid, text="合成动图设置")
        self.frm_seq.grid(row=6, column=0, columnspan=8, sticky="ew",
                          padx=(10, 10), pady=(4, 2))
        ttk.Label(self.frm_seq, text="每帧停留").pack(side="left", padx=(8, 4))
        self.cmb_delay = ChoiceBox(
            self.frm_seq, [(f"{d:g}", f"{d:g} 秒") for d in FRAME_DELAYS], "0.5",
            width=7, on_change=self._option_changed)
        self.cmb_delay.pack(side="left")
        ttk.Label(self.frm_seq, text="循环").pack(side="left", padx=(18, 4))
        self.cmb_loop = ChoiceBox(self.frm_seq, LOOP_CHOICES, "0", width=10,
                                  on_change=self._option_changed)
        self.cmb_loop.pack(side="left")
        ttk.Label(self.frm_seq, text="颜色数").pack(side="left", padx=(18, 4))
        self.cmb_colors = ChoiceBox(
            self.frm_seq, [(v, v) for v in ("256", "192", "128", "96", "64", "32")],
            "256", width=5, on_change=self._option_changed)
        self.cmb_colors.pack(side="left")
        self.lbl_seq_hint = ttk.Label(
            self.frm_seq, text="按「序号」列的顺序合成；拖动或排序可调整顺序",
            foreground="#6b7280")
        self.lbl_seq_hint.pack(side="left", padx=(18, 8))

        for var in (self.var_w, self.var_h, self.var_percent, self.var_quality):
            var.trace_add("write", lambda *_: self._option_changed())
        for var in (self.var_lossless, self.var_animated, self.var_no_upscale,
                    self.var_keep_meta):
            var.trace_add("write", lambda *_: self._option_changed())
        self._mode_changed()

    # ---------------------------------------------------------- capability
    def _available_formats(self) -> list[str]:
        keys = []
        for key, cont in IMAGE_CONTAINERS.items():
            if key == "gif":
                if (self.caps.has_encoder("gif") and self.caps.has_filter("palettegen")
                        and self.caps.has_filter("paletteuse")):
                    keys.append(key)
                continue
            if key == "webp":
                if (self.caps.has_encoder("libwebp")
                        or self.caps.has_encoder("libwebp_anim")):
                    keys.append(key)
                continue
            if self.caps.has_encoder(cont.default_video):
                keys.append(key)
        return keys or ["jpg"]

    def refresh_capabilities(self) -> None:
        self._fmt_keys = self._available_formats()
        self._refresh_formats()
        self._fmt_changed()

    def _refresh_formats(self) -> None:
        keys = self._fmt_keys
        if self.var_mode.get() == "sequence":
            keys = [k for k in keys if k in SEQUENCE_FORMATS] or ["gif"]
        current = self.cmb_fmt.key()
        self._visible_formats = keys
        self.cmb_fmt.set_choices([(k, IMAGE_CONTAINERS[k].label) for k in keys], current)
        if self.cmb_fmt.key() not in keys:
            self.cmb_fmt.set_key(keys[0])

    # ------------------------------------------------------------- reactions
    def _mode_changed(self) -> None:
        seq = self.var_mode.get() == "sequence"
        self.lbl_mode_note.configure(
            text="所有图片按「序号」顺序拼成一个动图" if seq else "每个文件各转一份")
        self._refresh_formats()
        if seq:
            self.frm_seq.grid()
            self.frm_anim.grid_remove()
            self.chk_keep.grid_remove()
            self.chk_lossless.grid_remove()
            self.lbl_hint.grid_remove()
            self.lbl_aspect_note.configure(text="每张各自从中心裁切，再统一缩放到同一尺寸")
        else:
            self.frm_seq.grid_remove()
            self.frm_anim.grid()
            self.chk_keep.grid()
            self.chk_lossless.grid()
            self.lbl_hint.grid()
            self.lbl_aspect_note.configure(text="从画面中心裁切，不会拉伸变形")
        self._fmt_changed()

    def _quality_moved(self, _value) -> None:
        self.var_quality_label.set(str(int(float(self.var_quality.get()))))

    def _fmt_changed(self, _key=None) -> None:
        key = self.cmb_fmt.key()
        self.lbl_hint.configure(text=FORMAT_HINTS.get(key, ""))
        seq = self.var_mode.get() == "sequence"
        # the per-image GIF frame rate does not apply when the frames come from
        # separate pictures -- there, each frame's time is the 每帧停留 setting
        if key == "gif" and not seq:
            self.frm_anim.grid()
        elif seq:
            self.frm_anim.grid_remove()
        self.chk_lossless.configure(state="normal" if key == "webp" else "disabled")
        if key != "webp":
            self.var_lossless.set(False)
        self.cmb_colors.enable(key == "gif")
        self._option_changed()

    def _option_changed(self, _key=None) -> None:
        mode = self.cmb_scale.key()
        self.ent_w.configure(state="normal" if mode in ("width", "longest", "box", "exact")
                             else "disabled")
        self.ent_h.configure(state="normal" if mode in ("height", "box", "exact")
                             else "disabled")
        self.ent_pct.configure(state="normal" if mode == "percent" else "disabled")
        self.var_quality_label.set(str(int(float(self.var_quality.get()))))
        self._on_selection()

    # -------------------------------------------------------------- planning
    def _crop_scale(self):
        label = self.cmb_aspect.key()
        aw, ah = ASPECT_DIMS.get(label, (0, 0))
        crop = (G.CropSpec(mode="aspect", aspect_w=aw, aspect_h=ah) if aw and ah
                else G.CropSpec(mode="none"))
        scale = G.ScaleSpec(mode=self.cmb_scale.key(),
                            no_upscale=self.var_no_upscale.get())
        scale.width = _int(self.var_w.get(), 1080)
        scale.height = _int(self.var_h.get(), 1080)
        scale.box_w = scale.width
        scale.box_h = scale.height
        scale.longest = scale.width
        scale.percent = _float(self.var_percent.get(), 50.0)
        return (crop if crop.mode != "none" else None,
                scale if scale.mode != "none" else None)

    def _build_options_obj(self) -> ImageOptions:
        crop, scale = self._crop_scale()
        return ImageOptions(
            container=self.cmb_fmt.key(),
            quality=int(float(self.var_quality.get())),
            lossless=self.var_lossless.get(),
            crop=crop,
            scale=scale,
            keep_metadata=self.var_keep_meta.get(),
            animated=self.var_animated.get(),
            gif=GifSpec(fps=_int(self.cmb_gif_fps.key(), 12)),
        )

    def _build_sequence_obj(self) -> SequenceOptions:
        crop, scale = self._crop_scale()
        return SequenceOptions(
            container=self.cmb_fmt.key() if self.cmb_fmt.key() in SEQUENCE_FORMATS else "gif",
            delay=_float(self.cmb_delay.key(), 0.5),
            loop=_int(self.cmb_loop.key(), 0),
            max_colors=_int(self.cmb_colors.key(), 256),
            quality=int(float(self.var_quality.get())),
            scale=scale,
            crop=crop,
        )

    def capture_options(self):
        if self.var_mode.get() == "sequence":
            return {"mode": "sequence", "sequence": self._build_sequence_obj()}
        return {"mode": "convert", "image": self._build_options_obj()}

    def plan_one(self, item, out_dir, taken, options=None):
        snap = options or self.capture_options()
        return plan.plan_image(item.info, out_dir, snap["image"], taken=taken)

    def plan_batch(self, items, out_dir, taken, options=None):
        snap = options or self.capture_options()
        if snap["mode"] != "sequence":
            return super().plan_batch(items, out_dir, taken, snap)
        if len(items) < 2:
            raise ValueError("合成动图至少要选两张图片")
        job = plan.plan_image_sequence([i.info for i in items], out_dir,
                                       snap["sequence"], taken=taken)
        return [(it, job) for it in items]

    def preview_scope(self, selected: list, all_items: list) -> list:
        if self.var_mode.get() == "sequence":
            return all_items
        return selected or all_items

    def option_summary(self) -> str:
        if self.var_mode.get() == "sequence":
            opts = self._build_sequence_obj()
            return (f"合成 {opts.container.upper()} · 每帧 {opts.delay:g} 秒 · "
                    f"{opts.max_colors} 色")
        opts = self._build_options_obj()
        bits = [opts.container.upper(), f"品质 {opts.quality}"]
        if opts.lossless:
            bits.append("无损")
        if opts.scale:
            bits.append("缩放")
        if opts.crop:
            bits.append("裁剪 " + self.cmb_aspect.label())
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
