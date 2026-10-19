"""Audio tab: format conversion, extraction from video, loudness, trim."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .. import media, plan
from ..presets import (AUDIO_CONTAINERS, BITRATES_KBPS, LOUDNESS_PRESETS,
                      SAMPLE_RATES, AudioOptions, TrimSpec)
from .base import FILE_TYPES_AUDIO, BaseTab
from .widgets import ChoiceBox, parse_time

CHANNEL_CHOICES = [("0", "保持原样"), ("1", "单声道"), ("2", "立体声")]
RATE_CHOICES = [("0", "保持原样")] + [(str(r), f"{r/1000:g} kHz") for r in SAMPLE_RATES]
LOUD_CHOICES = [(p[0], p[1]) for p in LOUDNESS_PRESETS]

AUDIO_ENCODER = {"mp3": "libmp3lame", "aac": "aac", "vorbis": "libvorbis",
                 "opus": "libopus", "flac": "flac", "pcm_s16le": "pcm_s16le",
                 "pcm_s24le": "pcm_s24le", "pcm_f32le": "pcm_f32le", "alac": "alac"}
LOSSLESS_CODECS = {"flac", "pcm_s16le", "pcm_s24le", "pcm_f32le", "alac"}


class AudioTab(BaseTab):
    title = "音频"
    subtitle = "MP3 / WAV / M4A / AAC / FLAC / OGG / Opus 互转，也能直接从视频里提取音频"
    allowed_exts = media.AUDIO_EXTS | media.VIDEO_EXTS
    filetypes = FILE_TYPES_AUDIO
    default_concurrency = 4

    def build_options(self, parent) -> None:
        grid = parent
        for c in (1, 3, 5):
            grid.columnconfigure(c, weight=1)

        self.var_t_start = tk.StringVar(value="")
        self.var_t_end = tk.StringVar(value="")
        self.var_copy = tk.BooleanVar(value=True)

        ttk.Label(grid, text="输出格式").grid(row=0, column=0, sticky="w", padx=(10, 4), pady=6)
        self._fmt_keys = self._available_formats()
        self.cmb_fmt = ChoiceBox(
            grid, [(k, AUDIO_CONTAINERS[k].label) for k in self._fmt_keys], "mp3",
            width=28, on_change=self._fmt_changed)
        self.cmb_fmt.grid(row=0, column=1, sticky="ew", pady=6)

        ttk.Label(grid, text="码率").grid(row=0, column=2, sticky="e", padx=(14, 4), pady=6)
        self.cmb_br = ChoiceBox(grid, [(str(b), str(b)) for b in BITRATES_KBPS], "192",
                                width=8, on_change=self._option_changed)
        self.cmb_br.grid(row=0, column=3, sticky="w", pady=6)
        self.lbl_hint = ttk.Label(grid, text="", foreground="#6b7280")
        self.lbl_hint.grid(row=0, column=4, columnspan=4, sticky="w", padx=(14, 4), pady=6)

        ttk.Label(grid, text="采样率").grid(row=1, column=0, sticky="w", padx=(10, 4), pady=6)
        self.cmb_rate = ChoiceBox(grid, RATE_CHOICES, "0", width=28,
                                  on_change=self._option_changed)
        self.cmb_rate.grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Label(grid, text="声道").grid(row=1, column=2, sticky="e", padx=(14, 4), pady=6)
        self.cmb_ch = ChoiceBox(grid, CHANNEL_CHOICES, "0", width=14,
                                on_change=self._option_changed)
        self.cmb_ch.grid(row=1, column=3, sticky="w", pady=6)
        ttk.Label(grid, text="做语音转写/字幕时，建议 16 kHz 单声道",
                  foreground="#9ca3af").grid(row=1, column=4, columnspan=4, sticky="w",
                                             padx=(14, 4), pady=6)

        ttk.Label(grid, text="音量标准化").grid(row=2, column=0, sticky="w",
                                            padx=(10, 4), pady=6)
        self.cmb_loud = ChoiceBox(grid, LOUD_CHOICES, "off", width=28,
                                  on_change=self._option_changed)
        self.cmb_loud.grid(row=2, column=1, sticky="ew", pady=6)
        ttk.Label(grid, text="让一批素材音量一致，平台播放时不会忽大忽小",
                  foreground="#6b7280").grid(row=2, column=2, columnspan=6, sticky="w",
                                             padx=(14, 4), pady=6)

        ttk.Label(grid, text="截取时间").grid(row=3, column=0, sticky="w", padx=(10, 4), pady=6)
        frm = ttk.Frame(grid)
        frm.grid(row=3, column=1, columnspan=4, sticky="w", pady=6)
        ttk.Label(frm, text="从").pack(side="left")
        self.ent_t_start = ttk.Entry(frm, textvariable=self.var_t_start, width=10)
        self.ent_t_start.pack(side="left", padx=(4, 2))
        ttk.Label(frm, text="到").pack(side="left", padx=(6, 0))
        self.ent_t_end = ttk.Entry(frm, textvariable=self.var_t_end, width=10)
        self.ent_t_end.pack(side="left", padx=(4, 8))
        ttk.Label(frm, text="(如 1:30 或 90.5，留空表示不截取)",
                  foreground="#9ca3af").pack(side="left")

        self.chk_copy = ttk.Checkbutton(
            grid, text="格式相同时直接复制音轨、不重编码（最快）", variable=self.var_copy)
        self.chk_copy.grid(row=4, column=0, columnspan=8, sticky="w", padx=10, pady=(2, 8))

        for var in (self.var_t_start, self.var_t_end, self.var_copy):
            var.trace_add("write", lambda *_: self._option_changed())
        self._fmt_changed()

    def _available_formats(self) -> list[str]:
        keys = []
        for key, cont in AUDIO_CONTAINERS.items():
            codec = cont.default_audio
            enc = AUDIO_ENCODER.get(codec, codec)
            if not self.caps.has_encoder(enc):
                continue
            if not self.caps.has_muxer(cont.muxer):
                continue
            keys.append(key)
        return keys or ["mp3"]

    def _fmt_changed(self, _key=None) -> None:
        cont = AUDIO_CONTAINERS[self.cmb_fmt.key()]
        lossless = cont.default_audio in LOSSLESS_CODECS
        self.cmb_br.enable(not lossless)
        hint = cont.label.split("·")[-1].strip() if "·" in cont.label else cont.label
        if self.cmb_fmt.key() == "opus":
            hint += "；采样率固定 48 kHz"
        self.lbl_hint.configure(text=hint)
        self._option_changed()

    def _option_changed(self, _key=None) -> None:
        self._on_selection()

    def refresh_capabilities(self) -> None:
        self._fmt_keys = self._available_formats()
        self.cmb_fmt.set_choices([(k, AUDIO_CONTAINERS[k].label) for k in self._fmt_keys])
        self._fmt_changed()

    def _build_options_obj(self) -> AudioOptions:
        return AudioOptions(
            container=self.cmb_fmt.key(),
            bitrate=_int(self.cmb_br.key(), 192),
            sample_rate=_int(self.cmb_rate.key(), 0),
            channels=_int(self.cmb_ch.key(), 0),
            loudness=self.cmb_loud.key() or "off",
            trim=TrimSpec(start=parse_time(self.var_t_start.get()),
                          end=parse_time(self.var_t_end.get())),
            copy_stream=self.var_copy.get(),
        )

    def capture_options(self):
        return self._build_options_obj()

    def plan_one(self, item, out_dir, taken, options=None):
        return plan.plan_audio(item.info, out_dir, options or self.capture_options(),
                               taken=taken)

    def option_summary(self) -> str:
        opts = self._build_options_obj()
        cont = AUDIO_CONTAINERS[opts.container]
        bits = [opts.container.upper()]
        if cont.default_audio not in LOSSLESS_CODECS:
            bits.append(f"{opts.bitrate}kbps")
        if opts.sample_rate:
            bits.append(f"{opts.sample_rate/1000:g}kHz")
        if opts.channels == 1:
            bits.append("单声道")
        elif opts.channels == 2:
            bits.append("立体声")
        if opts.loudness != "off":
            bits.append("音量标准化")
        if opts.trim.active:
            bits.append("截取")
        return " · ".join(bits)


def _int(text, fallback: int) -> int:
    try:
        return int(float(str(text).strip()))
    except (ValueError, TypeError):
        return fallback
