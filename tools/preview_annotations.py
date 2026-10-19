#!/usr/bin/env python3
"""Draw a simulated frame with its landform labels, the way the page will.

The web client draws its labels on a canvas, which is awkward to inspect from a
terminal and impossible to diff.  This tool renders the same frame through the
same code path -- :func:`moonlib_s.render_moon` or :func:`moonlib_s.render_region`,
then :func:`moonlib_s.annotate_frame` -- and draws the result with Pillow, so a
change to the projection or the layout can be looked at instead of guessed at.

It needs Pillow, which arrives with matplotlib (``pip install 'moonlib_s[full]'``).

Usage
-----
    ./.venv/bin/python tools/preview_annotations.py
    ./.venv/bin/python tools/preview_annotations.py --labels 45 --width 900
    ./.venv/bin/python tools/preview_annotations.py --when "2026-09-26 20:00" \
        --lat 30 --lon 120 --out out/annotated-disk.png
    ./.venv/bin/python tools/preview_annotations.py --region \
        --target-lon -11.36 --target-lat -43.31 --fov-radius-arcsec 900
"""

from __future__ import annotations

import argparse
import math
import time
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from _bootstrap import ROOT, use_src  # noqa: E402

moonlib_s = use_src()

import moonlib_s  # noqa: E402
from moonlib_s.annotations import annotate_frame, disk_frame, region_frame  # noqa: E402

#: The page's palette, one colour per landform class.  The page owns this table
#: -- colour is presentation -- and this tool carries a copy so that what it
#: draws is what the page draws; ``tests/test_webapp.py`` compares the two and
#: fails if they drift apart.
CLASS_COLORS = {
    "basin": "#e6b566",
    "mare": "#86b3a8",
    "crater": "#a8c6e8",
    "lacus": "#8fc6cc",
    "sinus": "#8fa8dc",
    "palus": "#a9b47a",
    "rima": "#e09a9a",
    "vallis": "#c8a0d4",
    "mons": "#dcc98a",
    "dorsum": "#bfa886",
    "rupes": "#d4a179",
    "catena": "#9aa8cc",
    "promontorium": "#9ed4b4",
    "statio": "#e9a86b",
    "dome": "#c9b98a",
    "dome": "#c9b98a",
    "pit": "#9fd0c0",
    "pyroclastic": "#d9a0b4",
    "imp": "#bcd0a0",
    "impact": "#f0c0a0",
    "albedo": "#c9c9dc",
    "lunar100": "#d9b8f2",
    "other": "#b9bec9",
}

#: Fonts that can draw both the Latin and the Chinese names, best first.
FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

def find_font(explicit: Optional[str] = None) -> str:
    for candidate in ([explicit] if explicit else []) + list(FONT_CANDIDATES):
        if candidate and Path(candidate).is_file():
            return candidate
    raise SystemExit("no usable font found; pass --font /path/to/font.ttf")


def _rgb(text: str) -> Tuple[int, int, int]:
    text = str(text).lstrip("#")
    if len(text) == 3:
        text = "".join(character * 2 for character in text)
    return tuple(int(text[index:index + 2], 16) for index in (0, 2, 4))


def draw_annotations(
    image: Any,
    plan: Any,
    font_path: str,
    box_outline: bool = False,
    outline_width: float = 1.0,
    outline_style: str = "solid",
    outline_color: str = "#b4d6ff",
    text_color: str = "#eef2ff",
    text_stroke: str = "#0c0e14",
    text_stroke_width: float = 2.0,
    bold: bool = False,
    class_colors: Optional[dict] = None,
    uniform: bool = False,
) -> Any:
    """Return a copy of *image* with markers, leaders and names drawn on it.

    *image* is the renderer's own float frame in ``[0, 1]``; the conversion to
    bytes is the same one :func:`moonlib_s.plotting.encode_png` makes, so the
    preview and the page look at identical pixels.
    """
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    pixels = (np.clip(np.asarray(image), 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    canvas = Image.fromarray(pixels).convert("RGB")
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype(font_path, max(8, int(round(plan.font_px))))
        if bold:
            font = ImageFont.truetype(font_path, max(8, int(round(plan.font_px))))
            font.set_variation_by_axes([700]) if hasattr(font, "set_variation_by_axes") else None
    except OSError:  # pragma: no cover - font without variations
        pass
    stroke = max(0, int(round(text_stroke_width)))
    ink = _rgb(text_color)
    # The colour drawn *around* a label is what --outline-color names;
    # --text-stroke is the same thing under its older name.  Reading
    # outline_color here is what makes that flag do something: it was being
    # converted into a local that nothing used, so changing it had no effect.
    outline_ink = _rgb(outline_color)
    radius = max(1.6, plan.font_px * 0.16)
    palette = CLASS_COLORS if class_colors is None else class_colors

    def colour_of(item: Any) -> Tuple[int, int, int]:
        """The class colour, or the single one when the page is set that way."""
        if uniform:
            return ink
        return _rgb(palette.get(getattr(item, "feature_class", "other"), text_color))

    # The dash patterns the page draws with, scaled by the line width the way
    # ``app.js`` scales them (``dash.map((v) => v * rim.width)``).  Without this
    # the tool accepted --outline-style dash|dot and drew a solid line, so the
    # preview it exists to give disagreed with the picture it was previewing.
    dash_pattern = {"dash": (6.0, 4.0), "dot": (1.5, 3.0)}.get(outline_style)

    def stroke_rim(points: List[Tuple[float, float]], colour: Tuple[int, int, int],
                   width: float) -> None:
        """Closed polyline, dashed to match the page when asked for one."""
        line_width = max(1, int(round(width)))
        ring = points + [points[0]]
        if not dash_pattern:
            draw.line(ring, fill=colour, width=line_width, joint="curve")
            return
        on, off = (length * line_width for length in dash_pattern)
        for start, end in zip(ring[:-1], ring[1:]):
            segment = math.hypot(end[0] - start[0], end[1] - start[1])
            if segment <= 0.0:
                continue
            ux, uy = (end[0] - start[0]) / segment, (end[1] - start[1]) / segment
            walked, drawing = 0.0, True
            while walked < segment:
                span = min(on if drawing else off, segment - walked)
                if drawing:
                    draw.line(
                        [(start[0] + ux * walked, start[1] + uy * walked),
                         (start[0] + ux * (walked + span),
                          start[1] + uy * (walked + span))],
                        fill=colour, width=line_width,
                    )
                walked += span
                drawing = not drawing

    # Rims first: they carry the size, and everything else must stay on top.
    # Two passes, dark then coloured, because one colour cannot read against both
    # the bright highlands and the shadowed floor -- the same trick the page uses.
    if outline_style != "none":
        for item in plan.items:
            if not item.outline:
                continue
            stroke_rim([tuple(point) for point in item.outline],
                       (12, 14, 20), outline_width + 1.1)
        for item in plan.items:
            if not item.outline:
                continue
            stroke_rim([tuple(point) for point in item.outline],
                       colour_of(item), outline_width)
    for item in plan.items:
        draw.ellipse(
            [item.x - radius, item.y - radius, item.x + radius, item.y + radius],
            fill=colour_of(item),
            outline=outline_ink,
            width=1,
        )
    for item in plan.items:
        if not item.labelled:
            continue
        left, top = float(item.label_x), float(item.label_y)
        right, bottom = left + item.label_w, top + item.label_h
        centre_y = top + item.label_h / 2.0
        if item.leader:
            anchor_x = min(max(item.x, left), right)
            anchor_y = min(max(item.y, top), bottom)
            draw.line([item.x, item.y, anchor_x, anchor_y], fill=colour_of(item), width=1)
        if box_outline:
            draw.rectangle([left, top, right, bottom], outline=(90, 120, 200), width=1)
        draw.text(
            (left + 3.0, centre_y),
            item.label,
            font=font,
            fill=colour_of(item),
            stroke_width=stroke,
            stroke_fill=outline_ink,
            anchor="lm",
        )
    return canvas


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--when", default="2026-09-26 20:00", help="local time of the frame")
    parser.add_argument("--lat", type=float, default=30, help="observer latitude")
    parser.add_argument("--lon", type=float, default=120, help="observer longitude")
    parser.add_argument("--width", type=int, default=760, help="frame width in pixels")
    parser.add_argument("--labels", type=int, default=30, help="label budget")
    parser.add_argument("--font-px", type=float, default=13.0, help="label font size")
    parser.add_argument("--min-radius-px", type=float, default=2.5)
    parser.add_argument("--language", default="zh", choices=("zh", "en", "both"),
                        help="which name the labels carry")
    parser.add_argument("--outline-min-radius-px", type=float, default=9.0,
                        help="apparent radius at which a rim outline is drawn")
    parser.add_argument("--outline-width", type=float, default=1.0,
                        help="rim line width in pixels, as the page draws it")
    parser.add_argument("--outline-style", default="solid",
                        choices=("solid", "dash", "dot", "none"))
    parser.add_argument("--outline-color", default="#b4d6ff")
    parser.add_argument("--text-color", default="#eef2ff")
    parser.add_argument("--text-stroke", default="#0c0e14")
    parser.add_argument("--text-stroke-width", type=float, default=2.0)
    parser.add_argument("--bold", action="store_true", help="bold label text")
    parser.add_argument("--all-kinds", action="store_true", help="keep unlit features too")
    parser.add_argument("--classes", help="comma-separated class keys (see --list-classes)")
    parser.add_argument("--list-classes", action="store_true", help="print the classes and exit")
    parser.add_argument("--uniform", action="store_true",
                        help="one colour for everything, as the page's 统一配色 does")
    parser.add_argument("--box-outline", action="store_true", help="draw label boxes (debug)")
    parser.add_argument("--font", help="font file for the labels")
    parser.add_argument("--out", type=Path, help="PNG to write")
    parser.add_argument("--region", action="store_true", help="close-up instead of whole disk")
    parser.add_argument("--target-lon", type=float, default=-11.36)
    parser.add_argument("--target-lat", type=float, default=-43.31)
    parser.add_argument("--fov-radius-arcsec", type=float, default=900.0)
    parser.add_argument("--rotation-deg", type=float, default=0.0)
    parser.add_argument("--terrain-limit", type=int, default=2,
                        help="pyramid level for the render (higher is quicker)")
    args = parser.parse_args(argv)
    if args.list_classes:
        from moonlib_s.annotations import CLASS_LABELS, FEATURE_CLASSES

        for key in FEATURE_CLASSES:
            zh, en = CLASS_LABELS[key]
            print(f"  {key:14s} {zh}  {en}  {CLASS_COLORS.get(key, '')}")
        return 0
    classes = [key.strip() for key in args.classes.split(",")] if args.classes else None

    site = moonlib_s.Observer(lat=args.lat, lon=args.lon, name="preview")
    view = moonlib_s.view_geometry(site, args.when)
    settings = moonlib_s.MoonRenderSettings(
        width=args.width,
        height=args.width,
        samples_per_pixel=1,
        shadow_steps=48,
        tone="srgb",
        keep_components=True,
        pyramid_level=args.terrain_limit,
    )

    started = time.time()
    if args.region:
        span_km = 2.0 * view.distance_km * math.tan(
            math.radians(args.fov_radius_arcsec / 3600.0)
        )
        render = moonlib_s.render_region(
            view, args.target_lon, args.target_lat, span_km=span_km,
            rotation_deg=args.rotation_deg, settings=settings,
            terrain=moonlib_s.open_terrain(),
        )
        frame = region_frame(
            view, args.target_lon, args.target_lat, span_km,
            settings.width, settings.height, args.rotation_deg,
            terrain=moonlib_s.open_terrain(),
        )
    else:
        render = moonlib_s.render_moon(view=view, settings=settings, terrain=moonlib_s.open_terrain())
        frame = disk_frame(view, settings.width, settings.height, settings.disk_fraction)
    print(f"rendered in {time.time() - started:.1f} s")

    plan = annotate_frame(
        frame,
        max_labels=args.labels,
        min_radius_px=args.min_radius_px,
        only_illuminated=not args.all_kinds,
        font_px=args.font_px,
        language=args.language,
        classes=classes,
        outline_min_radius_px=args.outline_min_radius_px,
        # The same height field the picture was traced from, so the names land
        # on the pixels they name rather than on the sphere under them.
        terrain=moonlib_s.open_terrain(),
    )
    counts = plan.to_dict()["counts"]
    shown_classes = ", ".join(
        f"{key} {value}" for key, value in (counts.get("classes") or {}).items() if value
    )
    print(f"classes: {shown_classes or '—'}")
    print(f"labels: {counts['labelled']} / budget {args.labels} "
          f"({counts['outlined']} outlines, in frame {counts['in_frame']}, "
          f"too small {counts['too_small']}, unlit {counts['unlit']}, "
          f"over budget {counts['over_budget']}, unplaced {counts['unplaced']})")

    canvas = draw_annotations(render.image, plan, find_font(args.font), args.box_outline,
                              args.outline_width, args.outline_style, args.outline_color,
                              args.text_color, args.text_stroke, args.text_stroke_width,
                              args.bold, uniform=args.uniform)
    default = ROOT / "out" / ("annotated-region.png" if args.region else "annotated-disk.png")
    target = Path(args.out).resolve() if args.out else default
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target)
    try:
        shown = target.relative_to(ROOT)
    except ValueError:
        shown = target
    print(f"written: {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
