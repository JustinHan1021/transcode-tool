"""Output geometry, computed in Python rather than in ffmpeg expressions.

Doing this arithmetic here instead of in `crop=`/`scale=` has three payoffs:
exact results (ffmpeg's expression rounding turned a 608px crop into 606px),
the ability to show the user the real output size before anything runs, and
filters containing plain integers instead of quoted expression strings.

H.264/H.265 require even dimensions, so every result is rounded to even.
"""
from __future__ import annotations

from dataclasses import dataclass


def even(n: float) -> int:
    """Round to the nearest even integer, never below 2."""
    return max(2, int(round(n / 2.0)) * 2)


def even_floor(n: float) -> int:
    return max(2, int(n // 2) * 2)


# ------------------------------------------------------------------ scaling

def by_width(iw: int, ih: int, width: int, no_upscale: bool = False) -> tuple[int, int]:
    if width <= 0:
        return iw, ih
    w = min(width, iw) if no_upscale else width
    return even(w), even(ih * w / iw)


def by_height(iw: int, ih: int, height: int, no_upscale: bool = False) -> tuple[int, int]:
    if height <= 0:
        return iw, ih
    h = min(height, ih) if no_upscale else height
    return even(iw * h / ih), even(h)


def longest_side(iw: int, ih: int, limit: int, no_upscale: bool = False) -> tuple[int, int]:
    if limit <= 0:
        return iw, ih
    if no_upscale and max(iw, ih) <= limit:
        return iw, ih
    if iw >= ih:
        return even(limit), even(ih * limit / iw)
    return even(iw * limit / ih), even(limit)


def by_percent(iw: int, ih: int, percent: float) -> tuple[int, int]:
    if percent <= 0:
        return iw, ih
    f = percent / 100.0
    return even(iw * f), even(ih * f)


def fit_box(iw: int, ih: int, bw: int, bh: int, no_upscale: bool = False) -> tuple[int, int]:
    """Largest size that fits inside bw x bh while keeping the aspect ratio."""
    if bw <= 0 or bh <= 0:
        return iw, ih
    f = min(bw / iw, bh / ih)
    if no_upscale:
        f = min(f, 1.0)
    return even(iw * f), even(ih * f)


def stretch(iw: int, ih: int, w: int, h: int) -> tuple[int, int]:
    return max(2, even(w)), max(2, even(h))


# ------------------------------------------------------------------ cropping

def aspect_dims(iw: int, ih: int, rw: float, rh: float) -> tuple[int, int]:
    """Largest rw:rh rectangle that fits inside iw x ih, even-sized."""
    if rw <= 0 or rh <= 0:
        return iw, ih
    target = rw / rh
    source = iw / ih
    if source > target:
        h = ih
        w = ih * target
    else:
        w = iw
        h = iw / target
    w = min(even_floor(w), even_floor(iw))
    h = min(even_floor(h), even_floor(ih))
    return max(2, w), max(2, h)


@dataclass(frozen=True)
class Geometry:
    """Resolved crop + scale for one source."""
    crop_w: int = 0
    crop_h: int = 0
    out_w: int = 0
    out_h: int = 0

    @property
    def needs_crop(self) -> bool:
        return self.crop_w > 0 and self.crop_h > 0

    @property
    def needs_scale(self) -> bool:
        return self.out_w > 0 and self.out_h > 0

    def label(self) -> str:
        bits = []
        if self.needs_crop:
            bits.append(f"裁剪 {self.crop_w}x{self.crop_h}")
        if self.needs_scale:
            bits.append(f"缩放 {self.out_w}x{self.out_h}")
        return " · ".join(bits) if bits else "原尺寸"


def resolve(iw: int, ih: int, crop: "CropSpec | None", scale: "ScaleSpec | None") -> Geometry:
    """Apply crop first, then scale -- the order that makes a target size exact."""
    cw = ch = 0
    if crop and crop.mode != "none":
        cw, ch = crop.dims(iw, ih)

    base_w = cw or iw
    base_h = ch or ih

    ow = oh = 0
    if scale and scale.mode != "none":
        ow, oh = scale.dims(base_w, base_h)

    return Geometry(crop_w=cw, crop_h=ch, out_w=ow, out_h=oh)


@dataclass
class CropSpec:
    """mode: none | aspect | manual"""
    mode: str = "none"
    aspect_w: float = 1
    aspect_h: float = 1
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0

    def dims(self, iw: int, ih: int) -> tuple[int, int]:
        if self.mode == "aspect":
            return aspect_dims(iw, ih, self.aspect_w, self.aspect_h)
        if self.mode == "manual":
            w = max(2, even_floor(min(self.w, iw - self.x) if self.w else iw))
            h = max(2, even_floor(min(self.h, ih - self.y) if self.h else ih))
            return w, h
        return iw, ih


@dataclass
class ScaleSpec:
    """mode: none | width | height | longest | percent | box | exact"""
    mode: str = "none"
    width: int = 0
    height: int = 0
    longest: int = 0          # its own field: reusing width/height means a stale
                              # value in the unused one silently wins the max()
    percent: float = 100.0
    box_w: int = 0
    box_h: int = 0
    no_upscale: bool = True

    def dims(self, iw: int, ih: int) -> tuple[int, int]:
        m = self.mode
        if m == "width":
            return by_width(iw, ih, self.width, self.no_upscale)
        if m == "height":
            return by_height(iw, ih, self.height, self.no_upscale)
        if m == "longest":
            return longest_side(iw, ih, self.longest or max(self.width, self.height),
                                self.no_upscale)
        if m == "percent":
            return by_percent(iw, ih, self.percent)
        if m == "box":
            return fit_box(iw, ih, self.box_w or self.width, self.box_h or self.height,
                           self.no_upscale)
        if m == "exact":
            return stretch(iw, ih, self.width, self.height)
        return iw, ih


# -------------------------------------------------------- filter construction

def crop_filter(crop: CropSpec, iw: int, ih: int) -> str:
    cw, ch = crop.dims(iw, ih)
    if crop.mode == "manual":
        return f"crop=w={cw}:h={ch}:x={crop.x}:y={crop.y}"
    # Centre the crop box; both offsets are exact because we know all four numbers.
    x = int((iw - cw) / 2)
    y = int((ih - ch) / 2)
    return f"crop=w={cw}:h={ch}:x={x}:y={y}"


def scale_filter(out_w: int, out_h: int, flags: str = "lanczos") -> str:
    return f"scale=w={out_w}:h={out_h}:flags={flags}"
