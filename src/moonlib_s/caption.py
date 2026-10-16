"""Burned-in captions: the instant written into a corner of every frame.

An animation is a run of instants, and the one thing a viewer cannot recover
from the pictures is *which* instants they are.  A time stamp in the corner is
therefore part of the file rather than a nicety: a four-hour run of a shadow
crossing a crater is not reproducible without it.

Why a hand-rolled font instead of a text library
------------------------------------------------
``PIL.ImageDraw.text`` would need Pillow *and* a font file that happens to exist
on the machine, and a proportional font would draw ``11:11`` narrower than
``20:20`` -- so the stamp would shift from frame to frame inside one video,
which is exactly what "the format has to line up" rules out.  A caption is ASCII
digits, a few letters and the punctuation of an ISO-8601 instant, so this module
carries its own 5x7 bitmap: ten digits, the letters of ``UTC`` and ``h``, and
``- : + . space``.  Every glyph is five columns wide and advances by six, so a
fixed-width string lands on exactly the same pixels in every frame *by
construction*, and nothing is ever looked up on disk.

The stamp is drawn on both of the picture conventions the encoder has: 8-bit
display frames and scene-linear float frames, where "white" is ``1.0``.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import numpy as np

from .errors import MoonlibError

__all__ = ["GLYPH_ADVANCE", "GLYPH_HEIGHT", "CORNERS", "stamp_text", "stamp_frame",
           "text_size", "fit_scale"]

#: Cell the glyphs are drawn in, before scaling.
GLYPH_WIDTH = 5
GLYPH_HEIGHT = 7
#: One blank column between characters: monospaced text needs the gap.
GLYPH_ADVANCE = GLYPH_WIDTH + 1

_FONT: Dict[str, Tuple[str, ...]] = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    ":": ("00000", "00100", "00100", "00000", "00100", "00100", "00000"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    " ": ("00000",) * GLYPH_HEIGHT,
    # The three letters an instant needs, plus the unit of the zone offset.
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "h": ("10000", "10000", "10110", "11001", "10001", "10001", "10001"),
}

#: Where a caption may sit, as ``(horizontal, vertical)`` alignment.
CORNERS: Dict[str, Tuple[str, str]] = {
    "left-top": ("left", "top"),
    "right-top": ("right", "top"),
    "left-bottom": ("left", "bottom"),
    "right-bottom": ("right", "bottom"),
}


def stamp_text(when_utc: Any, offset_hours: float = 0.0) -> str:
    """The caption for one instant: ``2026-10-16 14:35 UTC+8h``.

    The offset is the *page's* time zone, not the machine's: the boxes the
    reader filled in are in that zone, and a caption in another one would make
    the file disagree with the settings that produced it.  The magnitude is
    written the way a zone offset is spoken -- ``UTC+8h``, ``UTC-3.5h``,
    ``UTC+0h`` -- so it can never be mistaken for part of the clock.
    """
    moment = _as_utc(when_utc)
    offset = float(offset_hours)
    if not math.isfinite(offset):
        offset = 0.0
    local = moment + timedelta(hours=offset)
    return (
        f"{local.year:04d}-{local.month:02d}-{local.day:02d} "
        f"{local.hour:02d}:{local.minute:02d} "
        f"UTC{'+' if offset >= 0 else '-'}{abs(offset):g}h"
    )


def _as_utc(value: Any) -> datetime:
    """An aware UTC datetime from an ISO string or a datetime."""
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value or "").strip()
        if not text:
            raise MoonlibError("stamp_text needs an instant; got an empty value.")
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MoonlibError(
                f"stamp_text cannot read {text!r} as an instant."
            ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _glyph(character: str) -> np.ndarray:
    """One character as a ``(7, 5)`` boolean cell grid."""
    rows = _FONT.get(character)
    if rows is None:
        # Captions are generated by this module and never typed by hand, so an
        # unknown character is a caller's bug -- but a blank cell keeps a long
        # run going rather than failing the whole animation at frame 400.
        rows = _FONT[" "]
    return np.array([[cell == "1" for cell in row] for row in rows], dtype=bool)


def text_size(text: str, scale: int) -> Tuple[int, int]:
    """How large *text* is drawn at *scale*, in pixels: ``(width, height)``."""
    step = max(1, int(scale))
    if not text:
        return 0, 0
    return (len(text) * GLYPH_ADVANCE - 1) * step, GLYPH_HEIGHT * step


def fit_scale(text: str, width: int, *, share: float = 0.45, cap: int = 16) -> int:
    """The largest whole scale whose text fits in *share* of *width*.

    Whole numbers on purpose: at half-integer scales the strokes land on
    different pixel patterns from character to character, and a caption that
    looks even is the whole point of a bitmap font.  A whole scale gives every
    stroke the same weight in every frame of a run.
    """
    if not text:
        return 1
    units = len(text) * GLYPH_ADVANCE - 1
    if units <= 0 or width <= 0:
        return 1
    return max(1, min(int(cap), int(round(float(share) * float(width) / units))))


def _rasterise(
    coverage: np.ndarray, text: str, left: int, top: int, step: int
) -> None:
    """OR the text into a coverage map: 1.0 inside a stroke, 0.0 outside.

    Cells are drawn as ``step x step`` blocks, so the glyph's own 5x7 grid is
    exactly preserved at every scale -- which is what makes two different
    instants land on the same pixel columns.
    """
    height, width = coverage.shape
    for index, character in enumerate(text):
        cells = _glyph(character)
        origin_x = left + index * GLYPH_ADVANCE * step
        for row in range(GLYPH_HEIGHT):
            for column in range(GLYPH_WIDTH):
                if not cells[row, column]:
                    continue
                x0 = origin_x + column * step
                y0 = top + row * step
                x1 = min(width, x0 + step)
                y1 = min(height, y0 + step)
                if x1 <= x0 or y1 <= y0 or x0 < 0 or y0 < 0:
                    continue
                coverage[y0:y1, x0:x1] = 1.0


def stamp_frame(
    image: Any,
    text: str,
    *,
    corner: str = "left-top",
    margin_px: int = 16,
    scale: Optional[int] = None,
    colour: float = 1.0,
) -> np.ndarray:
    """Return a copy of *image* with *text* written into one corner.

    *image* is ``(height, width, 3)`` -- or ``(height, width)``, which is
    treated as one channel -- either 8-bit display pixels or scene-linear
    floats; the caller's own convention decides what *colour* means, and the
    default of 1.0 is white in both (255 of 255, and diffuse white).

    *margin_px* is measured in the frame's own pixels, from the edge the text is
    aligned to.  That is what makes a caption on a 4000 px frame and one on a
    600 px frame sit the same distance from the corner, and it is why the stamp
    is applied *after* the frame is resized rather than before.
    """
    values = np.asarray(image)
    if values.ndim == 2:
        values = values[:, :, None]
    if values.ndim != 3:
        raise MoonlibError(
            f"stamp_frame wants a (height, width, channels) frame, got {values.shape}."
        )
    horizontal, vertical = CORNERS.get(corner, CORNERS["left-top"])
    height, width = int(values.shape[0]), int(values.shape[1])
    margin = max(0, int(margin_px))
    step = max(1, int(scale) if scale else fit_scale(text, width))
    if text_size(text, step)[0] + 2 * margin > width or \
            text_size(text, step)[1] + 2 * margin > height:
        # Rather than let the caption run off the frame, shrink it until it
        # fits: the reader asked for a margin, and a stamp that is a little
        # small is still readable where a clipped one is not.
        step = max(1, min(step, fit_scale(text, max(1, width - 2 * margin),
                                          share=1.0)))
    text_w, text_h = text_size(text, step)
    left = margin if horizontal == "left" else max(0, width - margin - text_w)
    top = margin if vertical == "top" else max(0, height - margin - text_h)

    coverage = np.zeros((height, width, 1), dtype=np.float32)
    _rasterise(coverage[:, :, 0], text, left, top, step)
    return _composite(values, coverage, float(colour))


def _composite(values: np.ndarray, coverage: np.ndarray, colour: float) -> np.ndarray:
    """Paint the caption over the frame, keeping the frame's own dtype.

    A hard `max` rather than an alpha blend: the caption is a mark on the
    picture, and a camera burns it in the same way -- where the stroke is, the
    stroke's colour is, whether it landed on the black of the sky or the grey of
    a mare.  It also means the stamp cannot *darken* a bright frame, which is
    what a naive blend would do to the sunlit limb it happened to cross.
    """
    out = values.copy()
    # A 2-D mask: the coverage map is made with a channel axis so that the same
    # rasteriser can serve a grey frame, but indexing a plane with it needs the
    # mask the frame's own shape has.
    ink = np.asarray(coverage).reshape(out.shape[0], out.shape[1]) > 0.0
    if not ink.any():
        return out
    if np.issubdtype(out.dtype, np.integer):
        limit = float(np.iinfo(out.dtype).max)
        stroke = min(limit, max(0.0, colour) * limit)
        for channel in range(out.shape[2]):
            plane = out[:, :, channel]
            plane[ink] = np.maximum(plane[ink], np.rint(stroke).astype(out.dtype))
        return out
    stroke = float(colour)
    for channel in range(out.shape[2]):
        plane = out[:, :, channel]
        plane[ink] = np.maximum(plane[ink], stroke)
    return out
