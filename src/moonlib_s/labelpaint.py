"""Paint a landform plan onto a picture, for callers without a browser.

The page draws its names with a canvas: the plan says where every box, marker
and rim goes, and the browser turns that into pixels at whatever resolution the
reader is looking at.  An animation has no page in the loop -- its frames are
rendered, encoded and written by the server -- so the same plan has to become
pixels here.

Why Pillow, and why the font is looked up rather than shipped
-------------------------------------------------------------
The labels are real text, often Chinese, and the layout's boxes were measured
against the *reader's* font.  A hand-rolled stroke font (the 5x7 bitmap in
:mod:`moonlib_s.caption` exists for that reason) cannot draw 雨海.  Pillow can,
but it needs a font file, and a font is a large, licensed asset that this
package does not ship.  The function therefore reads the plan's own font size
and looks for a CJK-capable system font, preferring the families the page names
in its CSS (PingFang / Hiragino / Microsoft YaHei / Noto CJK).  ``MOONLIB_LABEL_FONT``
overrides the search for a machine with an unusual layout.

Pillow stays an optional import: this module raises a clear error when it is
missing instead of making every install depend on it.
"""

from __future__ import annotations

import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .annotations import AnnotationPlan, marker_radius_px
from .errors import MoonlibError

__all__ = ["find_label_font", "paint_annotation_plan"]

#: Fonts that carry both CJK and Latin, in the order the page's CSS names them.
#: The ``index`` is the face inside a ``.ttc`` collection; Pillow needs it, and
#: the bold face of Hiragino Sans GB is face 2 rather than a second file.
_FONT_CANDIDATES: Tuple[Tuple[str, int, int], ...] = (
    # (path, regular face index, bold face index)
    ("/System/Library/Fonts/PingFang.ttc", 0, 1),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0, 2),
    ("/System/Library/Fonts/STHeiti Light.ttc", 0, 0),
    ("/System/Library/Fonts/STHeiti Medium.ttc", 0, 0),
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0, 0),
    ("/Library/Fonts/Arial Unicode.ttf", 0, 0),
    ("C:/Windows/Fonts/msyh.ttc", 0, 1),
    ("C:/Windows/Fonts/msyhbd.ttc", 0, 0),
    ("C:/Windows/Fonts/simhei.ttf", 0, 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0, 1),
    ("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf", 0, 0),
    ("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", 0, 1),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0, 0),
    ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0, 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0, 0),
)


def _pillow() -> Any:
    """Pillow's drawing modules, or a message that says what to install."""
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore
    except ImportError as error:  # pragma: no cover - depends on the install
        raise MoonlibError(
            "动画里的月面地貌标注需要 Pillow；请安装 "
            "`pip install 'moonlib-s[video]'`（或 Pillow）后重试。"
        ) from error
    return Image, ImageDraw, ImageFont


def _candidates() -> List[Tuple[str, int, int]]:
    override = os.environ.get("MOONLIB_LABEL_FONT")
    found = list(_FONT_CANDIDATES)
    if override:
        found.insert(0, (override, 0, 0))
    if any(path and Path(path).is_file() for path, _, _ in found):
        return found
    # Anything matplotlib carries is a last resort: its DejaVu faces have no
    # CJK, but an English-only label is still better than no labels at all.
    # Imported only when no system font was found -- matplotlib is a heavy
    # import and the hunt is cached anyway.
    try:
        import importlib.util

        # Located, not imported: matplotlib is a presentation-layer package and
        # this module only wants the fonts it happens to carry.  find_spec()
        # gives the install path without pulling the heavy import in.
        spec = importlib.util.find_spec("matplotlib")
        if spec is not None and spec.origin:
            bundled = Path(spec.origin).resolve().parent / "mpl-data/fonts/ttf"
            found.append((str(bundled / "DejaVuSans.ttf"), 0, 0))
            found.append((str(bundled / "DejaVuSans-Bold.ttf"), 0, 0))
    except Exception:  # pragma: no cover - matplotlib is optional
        pass
    return found


@lru_cache(maxsize=8)
def _font_path(bold: bool = False) -> Tuple[str, int]:
    """The first font on this machine that exists, as ``(path, face index)``."""
    for path, regular, heavy in _candidates():
        if not path or not Path(path).is_file():
            continue
        return path, (heavy if bold else regular)
    raise MoonlibError(
        "找不到可用的标注字体。设置环境变量 MOONLIB_LABEL_FONT 指向一个含中文的 "
        ".ttf/.ttc 字体（例如 PingFang、思源黑体或微软雅黑）后重试。"
    )


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False) -> Any:
    _, _, ImageFont = _pillow()
    path, index = _font_path(bold)
    size = max(6, int(size))
    try:
        return ImageFont.truetype(path, size, index=index)
    except OSError:
        # A collection whose bold face is not where the table says: the regular
        # face still draws, and bold is a preference rather than a requirement.
        return ImageFont.truetype(path, size, index=0)


def find_label_font(bold: bool = False) -> str:
    """Path of the font this module will draw with (for diagnostics)."""
    return _font_path(bold)[0]


# ---------------------------------------------------------------------------
# colour and geometry helpers
# ---------------------------------------------------------------------------
_NAMED = {
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "red": (255, 0, 0),
    "green": (0, 128, 0),
    "blue": (0, 0, 255),
}


def _channel(text: str, scale: float) -> int:
    value = float(text.strip())
    if text.strip().endswith("%"):
        value = value * 255.0 / 100.0
    if scale > 1.0:                     # already 0-255
        return max(0, min(255, int(round(value))))
    return max(0, min(255, int(round(value * 255.0))))


def _rgba(value: Any, fallback: Tuple[int, int, int, int] = (255, 255, 255, 255),
          alpha: float = 1.0) -> Tuple[int, int, int, int]:
    """A CSS-ish colour string as RGBA, without importing a colour library."""
    red, green, blue, opacity = fallback
    text = str(value or "").strip().lower()
    if text.startswith("#"):
        body = text[1:]
        if len(body) == 3:
            body = "".join(ch * 2 for ch in body)
        if len(body) in (4, 6, 8) and all(ch in "0123456789abcdef" for ch in body):
            red, green, blue = (int(body[i:i + 2], 16) for i in (0, 2, 4))
            if len(body) == 8:
                opacity = int(body[6:8], 16)
    elif text.startswith("rgb"):
        inside = text[text.find("(") + 1:text.rfind(")")]
        parts = [part for part in inside.replace("/", ",").split(",") if part.strip()]
        if len(parts) >= 3:
            red = _channel(parts[0], 1.0 if "." in parts[0] else 255.0)
            green = _channel(parts[1], 1.0 if "." in parts[1] else 255.0)
            blue = _channel(parts[2], 1.0 if "." in parts[2] else 255.0)
            if len(parts) >= 4:
                opacity = _channel(parts[3], 1.0 if "." in parts[3] else 255.0)
    elif text in _NAMED:
        red, green, blue = _NAMED[text]
    return (
        int(red), int(green), int(blue),
        max(0, min(255, int(round(opacity * max(0.0, min(1.0, float(alpha))))))),
    )


def _image_from_frame(frame: Any) -> Tuple[Any, bool]:
    """``(PIL RGB image, was_float)`` for a display frame."""
    Image, _, _ = _pillow()
    data = np.asarray(frame)
    if data.ndim == 2:
        data = np.repeat(data[:, :, None], 3, axis=2)
    if data.ndim != 3 or data.shape[2] < 3:
        raise MoonlibError(
            f"paint_annotation_plan wants an (h, w, 3) frame; got {data.shape}."
        )
    was_float = data.dtype.kind == "f"
    if was_float:
        values = np.nan_to_num(data[:, :, :3], nan=0.0, posinf=1.0, neginf=0.0)
        pixels = np.rint(np.clip(values, 0.0, 1.0) * 255.0).astype(np.uint8)
    elif data.dtype == np.uint8:
        pixels = np.ascontiguousarray(data[:, :, :3])
    else:
        raise MoonlibError(
            "paint_annotation_plan takes uint8 or float display pixels; got "
            f"{data.dtype}."
        )
    return Image.fromarray(pixels), was_float


def _dashed_line(draw: Any, points: Sequence[Tuple[float, float]],
                 colour: Tuple[int, int, int, int], width: int,
                 pattern: Sequence[float], dash: bool) -> None:
    """Polyline with a dash (or dot) pattern, in frame pixels."""
    if not points or len(points) < 2:
        return
    on = max(1.0, float(pattern[0]) if pattern else 6.0)
    off = max(1.0, float(pattern[1]) if len(pattern) > 1 else on * 0.7)
    if not dash:
        draw.line([tuple(point) for point in points], fill=colour, width=width,
                  joint="curve")
        return
    period = on + off
    travelled = 0.0
    segment_start = points[0]
    for point in points[1:]:
        ax, ay = segment_start
        bx, by = point
        length = math.hypot(bx - ax, by - ay)
        if length <= 1e-6:
            segment_start = point
            continue
        position = 0.0
        while position < length:
            phase = travelled % period
            if phase < on:
                step = min(on - phase, length - position)
                t0 = position / length
                t1 = (position + step) / length
                draw.line(
                    [(ax + (bx - ax) * t0, ay + (by - ay) * t0),
                     (ax + (bx - ax) * t1, ay + (by - ay) * t1)],
                    fill=colour, width=width,
                )
            else:
                step = min(period - phase, length - position)
            position += max(step, 1e-4)
            travelled += max(step, 1e-4)
        segment_start = point


def _scaled_width(value: Any, font_px: float, reference: float = 13.0) -> float:
    """A width typed in "screen pixels at the reference font" in frame pixels."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, number) * max(1.0, float(font_px)) / reference


def _item_colour(style: Dict[str, Any], item: Any) -> Tuple[int, int, int, int]:
    palette = style.get("class_colors") or {}
    fallback = _rgba(style.get("text_color"), (238, 242, 255, 255))
    if style.get("uniform"):
        return fallback
    return _rgba(palette.get(getattr(item, "feature_class", "other")),
                 fallback[:3] + (255,))


# ---------------------------------------------------------------------------
# the painter
# ---------------------------------------------------------------------------
def paint_annotation_plan(
    frame: Any,
    plan: AnnotationPlan,
    *,
    style: Optional[Dict[str, Any]] = None,
    rotation_deg: float = 0.0,
    mirror: str = "none",
) -> np.ndarray:
    """Return *frame* with the plan's names, markers and rims painted on it.

    *style* mirrors the page's own label style (see ``labelStyleFor()`` in
    ``web/static/app.js``): colours, a uniform-colour switch, bold type, the
    text and marker strokes, and the rim's colour/width/pattern.  Widths are
    given in the screen pixels the panel shows and are converted against the
    plan's font size, because that is the size the reader was looking at when
    they typed them.
    """
    Image, ImageDraw, _ = _pillow()
    ink = dict(style or {})
    font_px = max(6.0, float(plan.font_px))
    size = max(6, int(round(font_px)))
    bold = bool(ink.get("bold"))
    font = _font(size, bold)
    base = _image_from_frame(frame)
    picture, was_float = base
    width, height = picture.size

    item_colour = _item_colour
    text_stroke = _rgba(ink.get("text_stroke"), (12, 14, 20, 235))
    marker_stroke = _rgba(ink.get("marker_stroke"), text_stroke)
    text_stroke_px = int(round(_scaled_width(ink.get("text_stroke_width", 2), font_px)))
    marker_stroke_px = _scaled_width(ink.get("marker_stroke_width", 1.0), font_px)
    rim_width = _scaled_width(ink.get("outline_width", 1.0), font_px)
    rim_colour = _rgba(ink.get("outline_color"), (180, 214, 255, 240))
    rim_style = str(ink.get("outline_style") or "solid")
    dash_pattern = (6.0, 4.0) if rim_style == "dash" else (1.5, 3.0)
    dashed = rim_style in ("dash", "dot")

    # Two layers: a lit name is fully opaque, a name on the night side is half
    # faded exactly as the page draws it.
    layers = {True: Image.new("RGBA", (width, height), (0, 0, 0, 0)),
              False: Image.new("RGBA", (width, height), (0, 0, 0, 0))}
    draws = {key: ImageDraw.Draw(layer) for key, layer in layers.items()}

    def alpha_for(item: Any) -> float:
        return 1.0 if getattr(item, "illuminated", True) else 0.5

    # Rims first, so a marker or a name is never hidden by its own outline --
    # the same order paintLabels() uses on the page.
    if rim_style != "none" and rim_width > 0.0:
        for item in plan.items:
            outline = getattr(item, "outline", None)
            if not outline:
                continue
            lit = bool(getattr(item, "illuminated", True))
            draw = draws[lit]
            points = [(float(x), float(y)) for x, y in outline]
            # A dark pass for contrast over bright highlands, then the class
            # colour on top; the page draws the same two passes.
            _dashed_line(draw, points, (12, 14, 20, 140),
                         max(1, int(round(rim_width + 1.1))), dash_pattern, dashed)
            colour = (item_colour(ink, item)
                      if ink.get("uniform") else
                      _rgba((ink.get("class_colors") or {}).get(
                          getattr(item, "feature_class", "other")),
                          rim_colour[:3] + (240,)))
            _dashed_line(draw, points, colour,
                         max(1, int(round(rim_width))), dash_pattern, dashed)

    # Markers.
    for item in plan.items:
        lit = bool(getattr(item, "illuminated", True))
        draw = draws[lit]
        colour = item_colour(ink, item)
        radius = max(1.4, float(marker_radius_px(item.radius_px, font_px)))
        box = [item.x - radius, item.y - radius, item.x + radius, item.y + radius]
        draw.ellipse(box, fill=colour)
        if marker_stroke_px >= 0.6:
            draw.ellipse(box, outline=marker_stroke,
                         width=max(1, int(round(marker_stroke_px))))

    # Names, and the leader from a name to its marker.
    for item in plan.items:
        if not item.labelled or item.label_x is None or item.label_y is None:
            continue
        lit = bool(getattr(item, "illuminated", True))
        draw = draws[lit]
        colour = item_colour(ink, item)
        left = float(item.label_x)
        top = float(item.label_y)
        right = left + float(item.label_w)
        bottom = top + float(item.label_h)
        mid_y = 0.5 * (top + bottom)
        if getattr(item, "leader", False):
            end_x = min(max(float(item.x), left), right)
            end_y = min(max(float(item.y), top), bottom)
            draw.line([(float(item.x), float(item.y)), (end_x, end_y)],
                      fill=colour, width=max(1, int(round(font_px * 0.09))))
        text_x = left + 3.0
        if bold:
            # Pillow draws the character's own outlines; a face without a bold
            # variant is thickened by drawing the same string a pixel to the
            # right, which is what a canvas' synthetic bold does too.
            for offset in ((0.0, 0.0), (0.7, 0.0)):
                draw.text((text_x + offset[0], mid_y + offset[1]), item.label,
                          font=font, fill=colour, anchor="lm",
                          stroke_width=text_stroke_px, stroke_fill=text_stroke)
        else:
            draw.text((text_x, mid_y), item.label, font=font, fill=colour,
                      anchor="lm", stroke_width=text_stroke_px,
                      stroke_fill=text_stroke)

    if rotation_deg or str(mirror or "none") not in ("none", ""):
        # The frame itself may have been rotated/mirrored by 后期处理 after it
        # was rendered; the names were laid out in the unprocessed camera, so
        # the overlay has to take the same transform.  Text turns with the
        # picture, exactly as the page's rotated canvas does.
        from .instrument import orient_image

        for key, layer in layers.items():
            values = np.asarray(layer, dtype=np.float32)
            turned = orient_image(values, rotation_deg=rotation_deg, mirror=mirror)
            layers[key] = Image.fromarray(
                np.rint(np.clip(turned, 0.0, 255.0)).astype(np.uint8)
            )

    # The night side is half faded, like the page's `alphaFor()`: it was asked
    # for, but it is not observable, and full strength would make it look lit.
    faded = layers[False].getchannel("A").point(lambda value: int(round(value * 0.5)))
    layers[False].putalpha(faded)
    composed = Image.alpha_composite(picture.convert("RGBA"), layers[True])
    composed = Image.alpha_composite(composed, layers[False])
    result = np.asarray(composed.convert("RGB"))
    return result.astype(np.float32) / 255.0 if was_float else result
