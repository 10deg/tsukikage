#!/usr/bin/env python3
"""Local web page for the lunar simulation.

Serves one page plus a small JSON API.  Everything the page does goes through
:func:`moonlib.render_moon` and :func:`moonlib.render_region`, so the browser is
only a front end for the same code the command line runs.

Run it with::

    ./.venv/bin/python web/server.py --port 8765

and open the URL it prints.

Design notes
------------
The terrain pyramid is opened once, at startup, and reused for every request:
baking it on the first click would make that click take a minute.  Views are
cached against ``(instant, latitude, longitude)`` because the page asks for the
disk geometry on every keystroke.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import itertools
import json
import math
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional, Sequence, Tuple
from urllib.parse import parse_qs, quote, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

import moonlib  # noqa: E402
from moonlib.annotations import CLASS_LABELS, FEATURE_CLASSES, feature_class  # noqa: E402
from moonlib.annotations import (  # noqa: E402
    disk_frame, plan_annotations, region_frame,
)
from moonlib.crescent import crescent_geometry  # noqa: E402
from moonlib.events import event_metrics  # noqa: E402
from moonlib.labelpaint import find_label_font, paint_annotation_plan  # noqa: E402
from moonlib.models import MoonRenderSettings  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"

#: Where the page's settings files live.  A directory of small ``.conf`` files
#: the user can copy around, commit, or hand to someone else; the page lists it
#: and loads one by name.
DEFAULT_SETTINGS_DIR = Path(__file__).resolve().parent / "settings"
SETTINGS_SUFFIX = ".conf"
#: A settings file is one page of JSON; anything larger is not one.
SETTINGS_MAX_BYTES = 262_144
SETTINGS_NAME_MAX = 60

#: Preview renders are for aiming, not for looking at: small, one sample per
#: pixel, and few shadow steps.
PREVIEW_WIDTH = 560

#: Processing that changes nothing, for the "what the geometry produced" pane.
IDENTITY_PROCESSING = {
    "blur_strength": 0.0,
    "sharpen_strength": 0.0,
    "saturation": 1.0,
    "contrast": 1.0,
    "brightness": 0.0,
    "gain": 1.0,
    "offset": 0.0,
    "gamma": 1.0,
}
PREVIEW_SAMPLES = 1
PREVIEW_SHADOW_STEPS = 24
#: A whole-disk frame is at most 10000 px across, i.e. 370 m/px: 64 steps is
#: already finer than the terrain it samples.  Close-ups are the ones that need
#: depth, and 1.3.4 settled on 256 there (a grazing shadow edge is quantised by
#: the step length, and below that the tips read as a staircase).
DISK_SHADOW_STEPS = 64
#: Shadow steps for a close-up still.  64 was the historical default, but at a
#: grazing Sun its geometric march takes ~12 km strides at the far end and
#: quantises the shadow boundary into visible rectangular blocks.  256 cuts that
#: to about 3 km and keeps the 1.3.3 tip shape; 512 is cleaner still but costs
#: roughly twice as much again.  Animations stay at 48: every frame pays it.
REGION_SHADOW_STEPS = 256

#: Hard pixel caps.  A preview is re-rendered on every change of time or place,
#: The page's preview menu tops out here.  It is re-rendered on every change of
#: time or place, so the largest useful interactive size is 3000 px; above that
#: the wait stops feeling like a preview.  An output render is the user's call
#: -- the pane renders exactly what it is asked for -- so its cap is only a
#: memory guard against a typo, not a bound on useful work.  It matches the
#: whole-disk export's ceiling: a fine regional product (0.0592 km/px) over a
#: 500 km box needs ~8 700 px, and the old 8192 cut that request off just short.
MAX_PREVIEW_PX = 3000
MAX_OUTPUT_PX = 10000
#: Default decimation of the colour mosaic cache: 2 keeps the old 19 px/deg
#: (~1.6 km/px) bake, which is small.  A finer cache (0 is the source's full
#: 76 px/deg, ~0.4 km/px and ~5.4 GB) can be baked explicitly; the server then
#: detects and uses the finest cache already on disk without changing this
#: default for a fresh installation.
ALBEDO_DETAIL = 2
#: The full disk has its own, much higher ceiling: it is the one picture worth
#: rendering at the terrain data's own limit, it is saved deliberately rather
#: than on every keystroke, and 10 000 px across is where the shipped LOLA
#: pyramid runs out of real detail (the disk is 0.94 of the frame).
MAX_DISK_PX = 10000
#: The smallest picture any endpoint will render: below this a "frame" is a
#: thumbnail with no room for a label, and the layout constants stop meaning
#: anything.
MIN_PX = 64

#: The finest scale worth rendering: the local LOLA DEM's own pixel size.  Below
#: it the ray march would interpolate the same terrain sample over and over --
#: more pixels, more minutes, no more detail.  This is the fallback for when no
#: raster is on disk; when one is, :func:`terrain_km_per_px` measures it.
TERRAIN_KM_PER_PX = 0.118

#: Ground kilometres per degree on the 1737.4 km sphere: what turns a raster's
#: width into pixels per degree, and back.
_KM_PER_DEGREE = math.radians(1.0) * 1737.4

#: Animations are a sequence of renders, so they run as a background job and the
#: page polls: a whole lunation at 1000 px is minutes, which no HTTP request can
#: wait for.  The finished MP4 is kept under the cache directory, and only the
#: How long a new render waits for a cancelled one to give the slot back.
#:
#: A frame stops at the end of the band it is tracing, and a band at 3000 px
#: with 2x2 sampling takes a few seconds; a reader who presses 终止 and then
#: 生成 should wait that out rather than be told to try again.
_RENDER_HANDOVER_SECONDS = 20.0


def _combined_cancel(
    frame_cancel: threading.Event,
    client_cancel: Optional[Callable[[], bool]],
) -> Callable[[], bool]:
    """One cancel predicate for the renderer: 终止 *or* a gone client.

    A browser tab closed mid-render used to leave the frame marching to the end
    of the image while a reloaded page was told "上一张还在渲染".  Publishing
    the disconnect on the frame's own event is what lets the next request wait
    for the handover instead of being refused by a frame nobody will ever see.
    """
    if client_cancel is None:
        return frame_cancel.is_set

    def cancelled() -> bool:
        if frame_cancel.is_set():
            return True
        if client_cancel():
            frame_cancel.set()
            return True
        return False

    return cancelled

#: last few are kept -- an animation is a file, not a session, so a handful is a
#: courtesy and a hundred is a leak.
ANIMATION_KEEP = 3
#: Ceiling on the frames one request may ask for.  A typo in the step (1 instead
#: of 3600) is the difference between a minute and a fortnight of ray marching,
#: and the guard is cheaper than cancelling it.
ANIMATION_MAX_FRAMES = 4000


def terrain_km_per_px() -> float:
    """Pixel size of the finest global DEM on disk, in kilometres.

    Measured from the raster's own width, not guessed from its file name.  The
    page clamps the finest scale it offers to this number, so a product whose
    name does not happen to carry a metric pixel size -- SLDEM2015 is
    distributed as so many pixels per degree -- would be sitting on disk while
    the page refused to ask for the detail it holds.
    """
    try:
        import moonlib
        sources = moonlib.terrain.global_dem_sources()
        if sources:
            return float(sources[0][1])
    except Exception:
        pass
    return TERRAIN_KM_PER_PX

#: Label defaults for /api/annotations.  The page sends its own when the user
#: picks a density, but a request that only says "annotate this frame" should
#: still come back with something sensible.
DEFAULT_LABELS = 30
DEFAULT_MIN_RADIUS_PX = 2.5
DEFAULT_LABEL_FONT_PX = 13.0
#: Official Chinese names unless the page asks otherwise.
DEFAULT_LANGUAGE = "zh"
#: Apparent radius at which a round feature also gets its rim drawn.
DEFAULT_OUTLINE_RADIUS_PX = 9.0


# ---------------------------------------------------------------------------
# request parsing
# ---------------------------------------------------------------------------
def _f(payload: Dict[str, Any], key: str, default: float) -> float:
    """A float from one field of a request block, else *default*.

    This delegates to :func:`_finite` rather than doing its own ``float()``.
    The two used to be separate, and only ``_finite`` rejected a non-finite
    number, so the light options were guarded while the telescope, camera,
    selection and output blocks were not: a request carrying ``NaN`` for the
    aperture built a telescope whose plate scale was ``NaN``, and the frame
    came back with a NaN in its payload -- which is not JSON, so the page's
    ``JSON.parse`` rejected the whole response.  One coercer, one policy.
    """
    return _finite(payload.get(key), default)


def _opt_f(payload: Dict[str, Any], key: str) -> Optional[float]:
    value = _finite(payload.get(key), float("nan"))
    return None if value != value else value


def _obstacle_rects(value: Any) -> Tuple[Tuple[float, float, float, float], ...]:
    """Rectangles a label layout must keep clear of, in frame pixels.

    The page sends these for the parts of the picture that are already drawn by
    hand -- the movable scale bar, an inset -- so a landform name cannot be set
    on top of them.  Each entry is ``[x, y, width, height]``; a malformed entry
    is a bad request rather than a silently ignored rectangle, because the only
    caller is our own page and a silently dropped obstacle would show up as a
    label sitting on the scale bar with no way to see why.
    """
    if value in (None, (), []):
        return ()
    if not isinstance(value, (list, tuple)):
        raise moonlib.MoonlibError("obstacles must be a list of [x, y, width, height].")
    rects = []
    for entry in value:
        if not isinstance(entry, (list, tuple)) or len(entry) < 4:
            raise moonlib.MoonlibError(
                "each obstacle must have four numbers: [x, y, width, height]."
            )
        try:
            x, y, width, height = (float(entry[index]) for index in range(4))
        except (TypeError, ValueError):
            raise moonlib.MoonlibError(
                "obstacle coordinates must be numbers: [x, y, width, height]."
            ) from None
        if not all(math.isfinite(number) for number in (x, y, width, height)):
            raise moonlib.MoonlibError("obstacle coordinates must be finite numbers.")
        if width <= 0.0 or height <= 0.0:
            continue
        rects.append((x, y, width, height))
    return tuple(rects)


def parse_train(payload: Dict[str, Any]) -> moonlib.ImagingTrain:
    """Build the imaging train, falling back to the documented defaults.

    A blank field means "use the default", which is how the page can start with
    an 8-inch f/10 SCT and an IMX533C without the user typing anything.
    """
    scope = payload.get("telescope") or {}
    camera = payload.get("camera") or {}
    aperture = _f(scope, "aperture_mm", moonlib.DEFAULT_TELESCOPE.aperture_mm)
    focal_length = _opt_f(scope, "focal_length_mm")
    focal_ratio = None if focal_length else _f(
        scope, "focal_ratio", moonlib.DEFAULT_TELESCOPE.native_focal_ratio
    )
    telescope = moonlib.Telescope(
        aperture_mm=aperture,
        focal_ratio=focal_ratio,
        focal_length_mm=focal_length,
        barlow=_f(scope, "barlow", 1.0),
        back_focus=_f(scope, "back_focus", 1.0),
        obstruction=_f(scope, "obstruction", 0.34),
    )
    imx = moonlib.DEFAULT_CAMERA
    cam = moonlib.Camera(
        width_px=int(_f(camera, "width_px", imx.width_px)),
        height_px=int(_f(camera, "height_px", imx.height_px)),
        pixel_um=_f(camera, "pixel_um", imx.pixel_um),
        color=bool(camera.get("color", True)),
    )
    return moonlib.ImagingTrain(telescope, cam)


def _light_option(request: Dict[str, Any], block: str, key: str) -> Any:
    """One optical option, from the light block, a named block, or the top level.

    The page's controls describe the *light*, so ``lightPayload()`` sends them
    under one ``light`` key; a request may also carry its own copy inside a
    block (``preview`` for the disk preview, ``output`` for a close-up), and
    older clients put them at the top level.  The export path has no ``preview``
    block at all, so reading only that block used to silently render the saved
    file with the default photometric model instead of the one on screen.
    Returns a one-key mapping, ready for the parsers that follow, or ``{}``.
    """
    for bag in (request.get("light"), request.get(block), request):
        if isinstance(bag, dict) and bag.get(key) not in (None, ""):
            return {key: bag[key]}
    return {}


def _optical_model(bag: Any) -> str:
    """The photometric model a request asks for, or the project default.

    Bad values are not an error: a render that refuses to run because a stored
    .conf carries a model this build does not know is worse than one that runs
    with the default and says so.
    """
    value = str((bag or {}).get("photometric_model") or "").strip()
    converter = getattr(moonlib, "as_photometric_model", None)
    try:
        if converter is not None and value:
            return converter(value).value
    except Exception:
        pass
    return "lunar_lambert"


def _sun_samples(bag: Any) -> int:
    """How many points across the Sun's disc to sample; 1 is a point source.

    One sample is what the renderer has always used, and it is the fast end of
    the trade: a point Sun gives hard shadow edges, while a handful of samples
    across the disc gives the penumbra real shadows have.
    """
    try:
        return max(1, min(32, int((bag or {}).get("sun_samples") or 1)))
    except (TypeError, ValueError):
        return 1


def _parallel(value: Any) -> int:
    """How many CPU workers one render may use; ``0`` means "let the library".

    The page's 并行数 is a ceiling on *this* render rather than a process-wide
    setting: two requests must not fight over ``MOONLIB_BAND_WORKERS``.  A
    value above the machine's own count is clamped there, because spawning
    more threads than cores only adds context switches.
    """
    try:
        wanted = int(float(value))
    except (TypeError, ValueError):
        return 0
    if wanted <= 0:
        return 0
    return max(1, min(wanted, os.cpu_count() or 1))


def _finite(value: Any, default: float) -> float:
    """A float from a JSON field, or *default* for anything unusable.

    The page leaves optional fields blank, and a stored ``.conf`` may hold a
    string; neither should stop a render.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _component(bag: Any, key: str = "component") -> str:
    """Which radiance component a request wants.

    ``image`` is the normal display picture; ``sunlit`` and ``earthshine`` are
    the two physical halves exposed on their own, which is how a real
    earthshine photograph is assembled.
    """
    value = str((bag or {}).get(key) or "image").strip().lower()
    return value if value in ("image", "sunlit", "earthshine") else "image"


def _sky_ambient(bag: Any) -> float:
    """Skylight on shadowed ground, as a fraction of the incident sunlight.

    Optional and clamped, so a blank field or a stored ``.conf`` cannot make a
    render absurd: the term is a per-point horizon integral, and a value of a
    hundred would be brighter than the sunlight it is supposed to be a fraction
    of.
    """
    return max(0.0, min(20.0, _finite((bag or {}).get("sky_ambient"), 0.0)))


def _roughness(bag: Any) -> float:
    """Mean surface slope for micro-relief shadowing, degrees.

    Clamped to the range the reduction is documented for: past about 45 deg the
    surface is a boulder field and the one-parameter form is no longer saying
    anything useful, and a negative value would *brighten* shadowed ground.
    """
    return max(0.0, min(45.0, _finite((bag or {}).get("roughness_deg"), 0.0)))


def _terrain_denoise(bag: Any) -> int:
    """0 off, 1 light, 2 strong; anything else means off."""
    try:
        value = int((bag or {}).get("terrain_denoise") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(2, value))


def _surface_texture(bag: Any) -> bool:
    """Whether the colour albedo mosaic is drawn.

    Missing or empty means yes, which is what every older request and stored
    .conf expects.  The page sends ``false`` for 黑白地貌.
    """
    value = (bag or {}).get("surface_texture")
    if value is None or value == "":
        return True
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {
        "0", "false", "off", "no", "none", "relief", "mono", "monochrome",
    }


def _tint(value: Any) -> Optional[Tuple[float, float, float]]:
    """The earthshine's spectral weighting, from whatever the request holds.

    Accepted as ``"0.87,0.96,1.17"`` or as a three-element list, because the
    page's control is a text field and a stored ``.conf`` holds a string.  The
    values are *relative*: the renderer normalises them by their mean, so only
    their ratios matter and any positive triple is usable.  Anything else --
    wrong count, non-numeric, non-positive -- means "use the default".
    """
    if value is None or value == "":
        return None
    if isinstance(value, (list, tuple)):
        # A .conf or a JSON client may hand over the triple directly.
        parts = [str(part) for part in value]
    else:
        parts = [part for part in re.split(r"[,;\s]+", str(value).strip()) if part]
    if len(parts) != 3:
        return None
    try:
        triple = tuple(float(part) for part in parts)
    except (TypeError, ValueError):
        return None
    if any(not math.isfinite(part) or part <= 0.0 for part in triple):
        return None
    return triple  # type: ignore[return-value]


def _earthshine(bag: Any) -> Dict[str, Any]:
    """Earthshine settings for one render.

    The physics is already done: the ephemeris supplies the Earth's phase,
    distance and hence the earthshine-to-sunlight irradiance ratio, so this
    only carries the artist's knobs and the choice of Earth albedo.  A blank
    albedo means "whatever the ephemeris default is" (0.367), and a blank gain
    means 1.0 -- the physically correct value.  Gain above 1 is a deliberate
    HDR composite, exactly like the CLI's ``--earthshine-gain``.
    """
    block = bag if isinstance(bag, dict) else {}
    albedo = _finite(block.get("earth_geometric_albedo"), 0.0)
    if not 0.0 < albedo <= 1.0:
        albedo = moonlib.moonview.DEFAULT_EARTH_GEOMETRIC_ALBEDO
    settings: Dict[str, Any] = {
        "earthshine": bool(block.get("on", True)),
        "earthshine_gain": max(0.0, min(100.0, _finite(block.get("gain"), 1.0))),
        "earth_geometric_albedo": albedo,
        "earth_samples": int(
            max(1, min(64, _finite(block.get("earth_samples"), 13)))
        ),
    }
    tint = _tint(block.get("tint"))
    if tint is not None:
        settings["earthshine_tint"] = tint
    return settings


#: Earth albedo / phase-function presets for the earthshine *curve*.  These are
#: display choices, not changes to the rendered disk: the render still uses the
#: Earth albedo from the simulation settings.
CURVE_EARTH_MODELS = {
    "simulation": {
        "label": "自定义（当前模拟设置）",
        "albedo": None,
        "phase": "lambert",
        "g": -0.33,
    },
    "allen": {
        "label": "Allen 1976 · 0.367 · Lambert",
        "albedo": 0.367,
        "phase": "lambert",
        "g": -0.33,
    },
    "vband": {
        "label": "V 波段 · 0.434 · Lambert",
        "albedo": 0.434,
        "phase": "lambert",
        "g": -0.33,
    },
    "robinson": {
        "label": "Robinson 2026 · 0.242 · Lambert",
        "albedo": 0.242,
        "phase": "lambert",
        "g": -0.33,
    },
    "robinson_hg": {
        "label": "Robinson 2026 · 0.23 · HG 相位",
        "albedo": 0.23,
        "phase": "hg",
        "g": -0.33,
    },
}


def _curve_earth_model(request: Dict[str, Any],
                       default_albedo: float) -> Dict[str, Any]:
    """Resolve the curve's Earth-albedo/phase preset.

    ``custom`` follows the simulation's Earth albedo; the named presets use the
    published value frozen into the menu.  Unknown keys fall back to ``custom``
    so a stale settings file cannot break the curve.
    """
    key = str(request.get("earthshine_model") or "simulation").strip().lower()
    preset = CURVE_EARTH_MODELS.get(key, CURVE_EARTH_MODELS["simulation"])
    if preset["albedo"] is None:
        albedo = float(default_albedo)
    else:
        albedo = float(preset["albedo"])
    if not 0.0 < albedo <= 1.0:
        albedo = moonlib.moonview.DEFAULT_EARTH_GEOMETRIC_ALBEDO
    return {
        "key": key if key in CURVE_EARTH_MODELS else "simulation",
        "label": preset["label"],
        "albedo": albedo,
        "phase": preset["phase"],
        "g": float(preset["g"]),
    }


#: The earthshine numbers a render reports back: the ephemeris values the frame
#: was actually drawn with, not the ones the request asked for.
_EARTHSHINE_STAT_KEYS = (
    "earthshine_on",
    "earthshine_ratio",
    "earthshine_magnitude",
    "earthshine_apparent_magnitude",
    "earth_illuminated_percent",
    "earth_phase_angle_deg",
    "earth_angular_diameter_deg",
    "earth_distance_km",
    "earth_geometric_albedo",
    "earthshine_gain",
    "earthshine_tint",
)


def _earthshine_stats(stats: Dict[str, Any]) -> Dict[str, Any]:
    """Pick the earthshine report out of a render's stats."""
    return {key: stats[key] for key in _EARTHSHINE_STAT_KEYS if key in stats}


def _apply_region_mask(image: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Zero everything outside a corrected selection.

    The arrays are float radiance (one channel or RGB); the mask is a 2-D
    coverage map.  Keeping this in one place means the cached linear frame and
    the finished picture can never disagree about where the selection was.
    """
    array = np.asarray(image, dtype=np.float32)
    coverage = np.asarray(mask, dtype=np.float32)
    if array.ndim == 2:
        return array * coverage
    return array * coverage[..., None]


def _ellipse_region_mask(width: int, height: int) -> np.ndarray:
    """A filled ellipse inscribed in the close-up frame.

    The ellipse selection is the sky footprint; the close-up camera covers its
    bounding rectangle, so the ellipse touches the middle of each edge.  The
    preview is masked to that exact shape, which is what makes the on-screen
    frame agree with the elliptical selector and the round model.
    """
    from PIL import Image, ImageDraw

    width = max(8, int(width))
    height = max(8, int(height))
    image = Image.new("L", (width, height), 0)
    ImageDraw.Draw(image).ellipse((0, 0, width - 1, height - 1), fill=255)
    return np.asarray(image, dtype=np.float32) / 255.0


def _orthorectify_ellipse(
    image: np.ndarray,
    view: Any,
    region: Any,
    ellipse: Any,
    rotation_deg: float,
    terrain: Any,
) -> np.ndarray:
    """Resample a perspective close-up into the corrected ellipse frame.

    The renderer produces a photograph: near the limb the selected ground
    patch is foreshortened.  A projection-corrected selection asks for the
    ground patch itself, so the finished frame is resampled through the same
    tangent plane the live preview uses.  The output keeps the requested pixel
    size; the standard ellipse is fitted inside it, with black around it.
    """
    array = np.asarray(image, dtype=np.float32)
    height, width = array.shape[:2]
    frame = region_frame(
        view,
        float(region.lon_deg), float(region.lat_deg), float(region.span_km),
        int(width), int(height),
        rotation_deg=float(rotation_deg), terrain=terrain,
        target_elevation_km=float(region.target_elevation_km),
    )
    half_w = max(float(ellipse.ground_half_width_km), 1e-6)
    half_h = max(float(ellipse.ground_half_height_km), 1e-6)
    aspect = half_w / half_h
    if width / max(height, 1) > aspect:
        ellipse_w = height * aspect
        ellipse_h = float(height)
    else:
        ellipse_w = float(width)
        ellipse_h = width / max(aspect, 1e-9)
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float64)
    u = (xx + 0.5 - width / 2.0) / max(ellipse_w / 2.0, 1e-9)
    v = (height / 2.0 - (yy + 0.5)) / max(ellipse_h / 2.0, 1e-9)
    inside = u * u + v * v <= 1.0
    gx = u * half_w
    gy = v * half_h
    reference = float(ellipse.reference_radius_km)
    height_km = np.sqrt(np.maximum(reference * reference - gx * gx - gy * gy, 0.0))
    points = (
        height_km[..., None] * np.asarray(ellipse.up0)[None, None, :]
        + gx[..., None] * np.asarray(ellipse.u)[None, None, :]
        + gy[..., None] * np.asarray(ellipse.v)[None, None, :]
    )
    flat = (points / reference).reshape(-1, 3)
    projected = frame.project_directions(flat)
    src_x = np.asarray(projected["x"], dtype=np.float64).reshape(height, width)
    src_y = np.asarray(projected["y"], dtype=np.float64).reshape(height, width)
    facing = np.asarray(projected["facing"], dtype=bool).reshape(height, width)
    inside &= facing & (src_x >= 0.0) & (src_x <= width - 1.0) \
        & (src_y >= 0.0) & (src_y <= height - 1.0)
    x0 = np.floor(src_x).astype(np.int64)
    y0 = np.floor(src_y).astype(np.int64)
    x1 = np.clip(x0 + 1, 0, width - 1)
    y1 = np.clip(y0 + 1, 0, height - 1)
    x0 = np.clip(x0, 0, width - 1)
    y0 = np.clip(y0, 0, height - 1)
    fx = (src_x - np.floor(src_x))[..., None]
    fy = (src_y - np.floor(src_y))[..., None]
    if array.ndim == 2:
        fx = fx[..., 0]
        fy = fy[..., 0]
    top = array[y0, x0] * (1.0 - fx) + array[y0, x1] * fx
    bottom = array[y1, x0] * (1.0 - fx) + array[y1, x1] * fx
    sampled = top * (1.0 - fy) + bottom * fy
    out = np.zeros_like(array)
    mask = inside[..., None] if array.ndim == 3 else inside
    out = np.where(mask, sampled, out)
    return out


def _projected_ellipse_region_mask(
    view: Any,
    ellipse: Any,
    width: int,
    height: int,
    target_elevation_km: float,
    terrain: Any,
) -> np.ndarray:
    """Rasterise the elliptical selector's *deformed* footprint.

    A plain ellipse in the finished frame is the sky-plane shape.  Against the
    limb that shape is wrong: the cone of rays meets the sphere in a curve that
    runs along the limb, and the page draws exactly that curve.  The mask has
    to use the same one, or the exported picture is cut by a circle the reader
    never drew.

    The boundary is computed on the reference sphere by
    :func:`moonlib.selection_ellipse_boundary` and projected through the
    renderer's own frame, so the mask and the picture cannot disagree about
    where the selector went.
    """
    from PIL import Image, ImageDraw

    width = max(8, int(width))
    height = max(8, int(height))
    distance = float(view.distance_km)
    span_km = 2.0 * distance * math.tan(
        math.radians(float(ellipse.fov_radius_arcsec) / 3600.0)
    )
    frame = region_frame(
        view, float(ellipse.lon_deg), float(ellipse.lat_deg), span_km, width, height,
        rotation_deg=float(ellipse.rotation_deg), terrain=terrain,
        target_elevation_km=float(target_elevation_km),
    )
    lonlat = ellipse.boundary_lonlat(721)
    projected = frame.project(lonlat[:, 1], lonlat[:, 0], 0.0)
    scale = min(1.0, 2048.0 / max(width, height))
    mask_w = max(8, int(round(width * scale)))
    mask_h = max(8, int(round(height * scale)))
    points = [
        (float(x) * scale, float(y) * scale)
        for x, y in zip(projected["x"], projected["y"])
        if math.isfinite(float(x)) and math.isfinite(float(y))
    ]
    if len(points) < 3:
        # A degenerate projection (a selection almost exactly on the far-side
        # limit) still has to produce a frame; fall back to the sky ellipse
        # rather than masking the whole picture black.
        return _ellipse_region_mask(width, height)
    canvas = Image.new("L", (mask_w, mask_h), 0)
    ImageDraw.Draw(canvas).polygon(points, fill=255)
    if (mask_w, mask_h) != (width, height):
        canvas = canvas.resize((width, height), Image.BILINEAR)
    return np.asarray(canvas, dtype=np.float32) / 255.0


def _corrected_region_mask(
    view: Any,
    correction: Any,
    lon_deg: float,
    lat_deg: float,
    rotation_deg: float,
    width: int,
    height: int,
    target_elevation_km: float,
    terrain: Any,
) -> np.ndarray:
    """Rasterise the irregular selection into the rendered frame.

    The close-up camera covers the smallest sky box around the irregular
    selection.  Every boundary sample is projected through that same camera
    (``region_frame`` is the renderer's own frame), and the polygon between
    them is filled.  The mask is built at a capped resolution and resized, so a
    10 000 px frame does not allocate a second 10 000 px polygon buffer.
    """
    from PIL import Image, ImageDraw

    width = max(8, int(width))
    height = max(8, int(height))
    distance = float(view.distance_km)
    span_km = 2.0 * distance * math.tan(
        math.radians(float(correction.fov_radius_arcsec) / 3600.0)
    )
    frame = region_frame(
        view, float(lon_deg), float(lat_deg), span_km, width, height,
        rotation_deg=float(rotation_deg), terrain=terrain,
        target_elevation_km=float(target_elevation_km),
    )
    lonlat = correction.boundary_lonlat(361)
    projected = frame.project(lonlat[:, 1], lonlat[:, 0], 0.0)
    scale = min(1.0, 2048.0 / max(width, height))
    mask_w = max(8, int(round(width * scale)))
    mask_h = max(8, int(round(height * scale)))
    points = [
        (float(x) * scale, float(y) * scale)
        for x, y in zip(projected["x"], projected["y"])
    ]
    canvas = Image.new("L", (mask_w, mask_h), 0)
    if len(points) >= 3:
        ImageDraw.Draw(canvas).polygon(points, fill=255)
    if (mask_w, mask_h) != (width, height):
        canvas = canvas.resize((width, height), Image.BILINEAR)
    return np.asarray(canvas, dtype=np.float32) / 255.0


#: Tone curves for the single-component exports, mirroring
#: :func:`moonlib.plotting.write_radiance_png`: the CLI writes the earthshine
#: with a linear tone auto-exposed to its own range, and the page has to produce
#: the same picture or the two are not the same product.
_COMPONENT_TONES = {"sunlit": "srgb", "earthshine": "linear"}


def _component_exposure(values: np.ndarray) -> float:
    """The scalar that exposes one component to its own range.

    Sunlight and earthshine are about twelve magnitudes apart, so a component
    written at the scene's exposure -- the one the renderer meters for the
    sunlit surface -- is pure black.  This is the same rule
    :func:`_component_image` applies to the full disk, and the same one the CLI
    applies to its radiance exports: the 99.9th percentile of the positive
    samples, which is the percentile the renderer itself meters on, brought to
    white.  It is returned as a *scale* rather than as a finished picture
    because the close-up's component still has to go through the instrument the
    user set up: the auto-exposure is one number, and blur, noise, sharpening,
    orientation and the display controls are all defined at whatever exposure
    they are handed.
    """
    positive = np.asarray(values, dtype=np.float64)
    positive = positive[positive > 0.0]
    if positive.size == 0:
        return 1.0
    return 1.0 / max(float(np.percentile(positive, 99.9)), 1e-30)


def _component_image(render: Any, component: str) -> np.ndarray:
    """The display image, or one of the two physical components on its own.

    Sunlight and earthshine differ by about twelve magnitudes, so a component
    written at the scene's exposure would be pure black; each is exposed to its
    own range instead.  ``image`` ignores this and returns what the render
    already produced.
    """
    if component == "image":
        return render.image
    from moonlib.moonview import apply_tone_mapping

    field = getattr(render, component, None)
    if field is None:  # pragma: no cover - both are kept for every web render
        raise moonlib.MoonlibError(f"这一帧没有保留 {component} 分量。")
    values = np.asarray(field, dtype=np.float64)
    positive = values[values > 0.0]
    if positive.size == 0:
        return apply_tone_mapping(
            values, tone=_COMPONENT_TONES[component], auto_exposure=False
        )
    # The same 99.9th percentile the renderer meters on, so the two agree about
    # what "correctly exposed" means.
    peak = float(np.percentile(positive, 99.9))
    return apply_tone_mapping(
        values / max(peak, 1e-30),
        tone=_COMPONENT_TONES[component],
        auto_exposure=False,
    )


def _component_linear(render: Any, component: str) -> np.ndarray:
    """One component exposed to its own range: the field ``_component_image`` tones.

    A component save (地照 off, so the sunlit half alone) is metered on its own
    99.9th percentile, which is not the exposure the whole frame was metered at.
    The HDR pair has to be built from the same one the base picture uses, or its
    gain map would be anchored on a different white than the picture it sits in.
    """
    field = np.asarray(getattr(render, component), dtype=np.float64)
    return field * _component_exposure(field)


class _ExposedRender:
    """Just enough of a ``MoonRender`` for :func:`moonlib.sdr_hdr_pair`.

    The pair only reads ``image``, the two radiance components and
    ``stats["exposure_scale"]`` -- so one component that has already been
    exposed can be handed over as if it were a whole render, with the exposure
    folded into the array (``exposure_scale = 1``).
    """

    def __init__(self, image: np.ndarray, linear: np.ndarray) -> None:
        self.image = image
        self.sunlit = linear
        self.earthshine = None
        self.stats = {"exposure_scale": 1.0}


def _tz_offset(request: Dict[str, Any]) -> float:
    """Hours to add to UTC for the times shown on the page."""
    value = (request.get("when") or {}).get("tz_offset_hours")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 8.0


def parse_observer(when: Dict[str, Any]) -> moonlib.Observer:
    site = when.get("observer") or {}
    return moonlib.Observer(
        lat=_f(site, "lat", 30),
        lon=_f(site, "lon", 120),
        name=str(site.get("name") or "observer"),
    )


def parse_viewpoint(request: Dict[str, Any]) -> str:
    """The requested viewpoint, with a safe fallback to the normal Earth view.

    A hand-edited ``.conf`` or an old bookmark must not make a render fail: an
    unknown value means "near side", not a 400.  The page only sends the two
    known values, so this is a guard rather than a user-facing choice.
    """
    when = request.get("when") or {}
    value = when.get("viewpoint")
    if value is None:
        value = request.get("viewpoint")
    try:
        return moonlib.as_viewpoint(value).value
    except Exception:
        return "earth"


# ---------------------------------------------------------------------------
# the two things the page can ask for
# ---------------------------------------------------------------------------
def _finest_baked_albedo_detail(dem_source: Optional[Path]) -> Optional[int]:
    """The finest colour cache already baked for *dem_source*, if any.

    The default bake stays at ``albedo_detail=2`` so a fresh checkout does not
    silently spend gigabytes.  A reader who asks for the high-precision colour
    cache (``albedo_detail=0``) should get it on the next server start without
    having to edit code, so the server prefers what is already on disk.
    """
    if dem_source is None:
        return None
    wanted = str(Path(dem_source).expanduser().resolve())
    root = moonlib.terrain.terrain_cache_root()
    details = []
    for manifest in sorted(root.glob("*/manifest.json")):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            source = payload.get("source", {}).get("dem", {}).get("path")
            request = payload.get("request") or {}
            if source is None or "albedo_detail" not in request:
                continue
            if str(Path(source).expanduser().resolve()) != wanted:
                continue
            details.append(int(request["albedo_detail"]))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return min(details) if details else None


def _baked_dem_sources() -> Dict[str, float]:
    """``{source path: dem detail}`` for every pyramid already on disk.

    A pyramid costs gigabytes and, from a 34 GB product, the better part of an
    hour.  Which ones exist is therefore part of "what this installation is
    built around": the app keeps drawing from the product it already has a cache
    for and only switches when the reader asks.
    """
    root = moonlib.terrain.terrain_cache_root()
    found: Dict[str, float] = {}
    for manifest in sorted(root.glob("*/manifest.json")):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            source = payload["source"]["dem"]["path"]
            detail = int(payload["request"]["dem_detail"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        # A bake made through the CLI may record a relative path; the menu
        # discovers absolute ones.  Compare the resolved paths, not the strings.
        found[str(Path(source).expanduser().resolve())] = float(detail)
    return found


def _dem_source_file() -> Path:
    """Where the chosen product is remembered between runs."""
    return moonlib.cache_dir() / "dem_source.json"


def _remembered_source() -> Optional[Path]:
    """The product the reader picked last time, if it is still on disk."""
    try:
        chosen = Path(str(json.loads(
            _dem_source_file().read_text(encoding="utf-8"))["id"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return chosen if any(path == chosen for path, _ in moonlib.discover_dem_sources()) else None


def _pyramid_dem_source() -> Optional[Path]:
    """The product the full-disk pyramid is built from.

    The finest one that already has a pyramid, else the finest there is.  This
    is deliberately *not* the reader's 数据源 choice: switching source must not
    mean "the next start spends an hour baking 34 GB", and a close-up reads its
    pixels straight from the chosen product anyway.  Baking a different source
    stays an explicit act -- drop the file in and run the terrain bake.
    """
    sources = moonlib.discover_dem_sources()
    if not sources:
        return None
    baked = _baked_dem_sources()
    for path, _km in sources:
        if str(path.expanduser().resolve()) in baked:
            return path
    # Nothing is baked yet.  Prefer a conventional ``ldem_<ppd>`` product over
    # the SLDEM hybrid when both are at the same resolution: the full disk gains
    # nothing from SLDEM's stereo fill, and baking the 34 GB hybrid on first
    # render would be a surprise.  A machine that only has SLDEM still gets it.
    for path, _km in sources:
        if path.stem.lower().startswith("ldem_"):
            return path
    return sources[0][0]


_BLENDER_PREVIEW_TEMPLATE = r'''# -*- coding: utf-8 -*-
import bpy, math
from mathutils import Vector

GLB = __GLB__
OUT = __OUT__
SOLAR_ALT = __SOLAR_ALT__
SOLAR_AZ = __SOLAR_AZ__
HAS_SOLAR = __HAS_SOLAR__
DEFAULT_SCENE = __DEFAULT_SCENE__
NOTE = __NOTE__

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=GLB)
model = next((o for o in bpy.data.objects if o.type == 'MESH'), None)
if model is None:
    raise RuntimeError("GLB 中没有可用的网格")

corners = [model.matrix_world @ Vector(c) for c in model.bound_box]
min_x = min(v.x for v in corners); max_x = max(v.x for v in corners)
min_y = min(v.y for v in corners); max_y = max(v.y for v in corners)
min_z = min(v.z for v in corners); max_z = max(v.z for v in corners)
center = Vector(((min_x + max_x) / 2.0, (min_y + max_y) / 2.0, (min_z + max_z) / 2.0))
dims = Vector((max_x - min_x, max_y - min_y, max_z - min_z))
size = max(dims.x, dims.y, dims.z, 1e-6)
scale = size / 0.2


def set_render(scene):
    engines = [item.identifier for item in
               bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items]
    scene.render.engine = 'BLENDER_EEVEE_NEXT' if 'BLENDER_EEVEE_NEXT' in engines else 'BLENDER_EEVEE'
    scene.eevee.taa_render_samples = 64
    scene.render.resolution_x = 1600
    scene.render.resolution_y = 1200
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.film_transparent = False
    try:
        scene.view_settings.view_transform = 'AgX'
    except Exception:
        pass
    try:
        scene.view_settings.look = 'AgX - Medium High Contrast'
    except Exception:
        pass


def make_world(name, color, strength):
    world = bpy.data.worlds.new(name)
    world.use_nodes = True
    nodes = world.node_tree.nodes
    bg = nodes.get("Background")
    if bg is None:
        bg = nodes.new("ShaderNodeBackground")
        out = nodes.get("World Output") or nodes.new("ShaderNodeOutputWorld")
        world.node_tree.links.new(bg.outputs[0], out.inputs[0])
    bg.inputs["Color"].default_value = color
    bg.inputs["Strength"].default_value = strength
    return world


def sun_euler(alt_deg, az_deg):
    alt = math.radians(alt_deg)
    az = math.radians(az_deg)
    to_sun = Vector((
        math.sin(az) * math.cos(alt),
        math.cos(az) * math.cos(alt),
        math.sin(alt),
    ))
    return (-to_sun).to_track_quat('-Z', 'Y').to_euler()


def make_sun(name, alt_deg, az_deg, energy, scene):
    data = bpy.data.lights.new(name, type='SUN')
    data.energy = energy
    data.angle = math.radians(0.53)
    data.use_shadow = True
    try:
        data.use_temperature = True
        data.temperature = 5778.0
    except Exception:
        data.color = (1.0, 0.96, 0.90)
    obj = bpy.data.objects.new(name, data)
    scene.collection.objects.link(obj)
    obj.rotation_euler = sun_euler(alt_deg, az_deg)
    return obj


studio = bpy.context.scene
studio.name = "Studio"
bpy.ops.mesh.primitive_plane_add(
    size=size * 8.0,
    location=(center.x, center.y, min_z - size * 0.002),
)
floor = bpy.context.object
floor.name = "Studio floor"
fm = bpy.data.materials.new("Studio floor")
fm.use_nodes = True
b = fm.node_tree.nodes.get("Principled BSDF")
b.inputs["Base Color"].default_value = (0.035, 0.040, 0.050, 1.0)
b.inputs["Roughness"].default_value = 0.92
floor.data.materials.append(fm)
studio.world = make_world("Studio world", (0.010, 0.015, 0.030, 1.0), 0.35)

target = bpy.data.objects.new("Camera target", None)
studio.collection.objects.link(target)
target.location = center

cam_data = bpy.data.cameras.new("Camera_ThreeQuarter")
cam = bpy.data.objects.new("Camera_ThreeQuarter", cam_data)
studio.collection.objects.link(cam)
cam.location = center + Vector((0.58, -0.72, 0.38)).normalized() * (size * 2.2)
cam_data.lens = 55.0
cam_data.sensor_width = 36.0
cam_data.clip_start = size * 0.002
cam_data.clip_end = size * 100.0
tr = cam.constraints.new(type='TRACK_TO')
tr.target = target
tr.track_axis = 'TRACK_NEGATIVE_Z'
tr.up_axis = 'UP_Y'
studio.camera = cam

top_data = bpy.data.cameras.new("Camera_Top")
top = bpy.data.objects.new("Camera_Top", top_data)
studio.collection.objects.link(top)
top.location = (center.x, center.y, max_z + size * 2.0)
top.rotation_euler = (0.0, 0.0, 0.0)
top_data.type = 'ORTHO'
top_data.ortho_scale = max(dims.x, dims.y) * 1.15
top_data.clip_start = size * 0.002
top_data.clip_end = size * 100.0

make_sun("Low side sun", 13.0, 250.0, 7.0, studio)
fill_data = bpy.data.lights.new("Cool fill", type='AREA')
fill_data.energy = 0.65 * scale * scale
fill_data.shape = 'DISK'
fill_data.size = 0.7 * scale
fill_data.color = (0.55, 0.70, 1.0)
fill = bpy.data.objects.new("Cool fill", fill_data)
studio.collection.objects.link(fill)
fill.location = center + Vector((-0.28, 0.22, 0.40)) * scale
fill.rotation_euler = (center - fill.location).to_track_quat('-Z', 'Y').to_euler()

rim_data = bpy.data.lights.new("Right rim", type='AREA')
rim_data.energy = 1.4 * scale * scale
rim_data.shape = 'DISK'
rim_data.size = 0.45 * scale
rim_data.color = (0.65, 0.80, 1.0)
rim = bpy.data.objects.new("Right rim", rim_data)
studio.collection.objects.link(rim)
rim.location = center + Vector((0.42, -0.06, 0.13)) * scale
rim.rotation_euler = (center - rim.location).to_track_quat('-Z', 'Y').to_euler()

set_render(studio)
studio.view_settings.exposure = 0.35
studio.render.filepath = "//blender_preview.png"


def link_scene(name):
    scene = bpy.data.scenes.new(name)
    for obj in (model, target, cam, top):
        if obj.name not in scene.collection.objects:
            scene.collection.objects.link(obj)
    scene.camera = cam
    set_render(scene)
    return scene


lunar = link_scene("Lunar Sun")
lunar.world = make_world("Lunar sky", (0.0, 0.0, 0.0, 1.0), 0.0)
if HAS_SOLAR:
    make_sun("Lunar Sun (infinite)", SOLAR_ALT, SOLAR_AZ, 6.0, lunar)
else:
    make_sun("Lunar Sun (infinite)", 25.0, 250.0, 6.0, lunar)
lunar.view_settings.exposure = 0.35
lunar.render.filepath = "//blender_lunar_sun.png"

term = link_scene("Lunar Terminator")
term.world = make_world("Lunar terminator sky", (0.0, 0.0, 0.0, 1.0), 0.0)
make_sun("Terminator Sun (infinite)", 10.0, 250.0, 9.0, term)
term.view_settings.exposure = 0.0
term.render.filepath = "//blender_lunar_terminator.png"

text = bpy.data.texts.new("场景说明")
text.write(NOTE)

target_scene = bpy.data.scenes.get(DEFAULT_SCENE) or studio
bpy.context.window.scene = target_scene
bpy.ops.wm.save_as_mainfile(filepath=OUT, compress=True)
print("SAVED", OUT, [s.name for s in bpy.data.scenes], "DEFAULT", target_scene.name)
'''


def _blender_preview_script(
    glb_path: Path,
    blend_path: Path,
    note: str,
    solar_alt: float,
    solar_az: float,
    has_solar: bool,
    default_scene: str = "Studio",
) -> str:
    """The Python script Blender runs to turn the exported GLB into a scene.

    Blender is optional at run time but is the only thing that can write a
    ``.blend``: the web server generates the same GLB the page exports, imports
    it here, adds the studio/lunar light rig and cameras, and saves a compressed
    file with the texture already packed.
    """
    return (
        _BLENDER_PREVIEW_TEMPLATE
        .replace("__GLB__", repr(str(glb_path)))
        .replace("__OUT__", repr(str(blend_path)))
        .replace("__SOLAR_ALT__", repr(float(solar_alt)))
        .replace("__SOLAR_AZ__", repr(float(solar_az)))
        .replace("__HAS_SOLAR__", repr(bool(has_solar)))
        .replace("__DEFAULT_SCENE__", repr(str(default_scene)))
        .replace("__NOTE__", repr(str(note)))
    )


def _find_blender() -> Optional[Path]:
    """Locate a Blender executable, without requiring it for the rest of the app."""
    override = os.environ.get("MOONLIB_BLENDER")
    if override:
        path = Path(override).expanduser()
        if path.is_file():
            return path
    found = shutil.which("blender")
    if found:
        return Path(found)
    for path in sorted(
        Path("/Applications").glob("Blender*.app/Contents/MacOS/Blender"),
        reverse=True,
    ):
        if path.is_file():
            return path
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env_name)
        if not base:
            continue
        for path in sorted(
            Path(base).glob("Blender Foundation/Blender*/blender.exe"),
            reverse=True,
        ):
            if path.is_file():
                return path
    return None


def _run_blender(
    blender: Path,
    script_path: Path,
    blend_path: Path,
    timeout: float = 180.0,
) -> None:
    command = [
        str(blender),
        "-b",
        "--factory-startup",
        "--python-exit-code", "1",
        "--python", str(script_path),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise moonlib.MoonlibError(
            f"Blender 可执行文件不存在：{blender}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise moonlib.MoonlibError(
            "Blender 生成预览场景超时。"
        ) from exc
    if result.returncode != 0 or not blend_path.is_file() or blend_path.stat().st_size == 0:
        lines = (result.stderr or result.stdout or "").strip().splitlines()[-6:]
        detail = " / ".join(lines) if lines else f"退出码 {result.returncode}"
        raise moonlib.MoonlibError(f"Blender 生成预览场景失败：{detail}")


_BLENDER_GLOBE_TEMPLATE = r'''# -*- coding: utf-8 -*-
import bpy, math
from mathutils import Vector, Matrix

GLB = __GLB__
OUT = __OUT__
SUB_OBSERVER_LON = __SUB_OBSERVER_LON__
SUB_OBSERVER_LAT = __SUB_OBSERVER_LAT__
SUBSOLAR_LON = __SUBSOLAR_LON__
SUBSOLAR_LAT = __SUBSOLAR_LAT__
NOTE = __NOTE__

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=GLB)
model = next((o for o in bpy.data.objects if o.type == 'MESH'), None)
if model is None:
    raise RuntimeError("GLB 中没有可用的网格")

corners = [model.matrix_world @ Vector(c) for c in model.bound_box]
min_x = min(v.x for v in corners); max_x = max(v.x for v in corners)
min_y = min(v.y for v in corners); max_y = max(v.y for v in corners)
min_z = min(v.z for v in corners); max_z = max(v.z for v in corners)
center = Vector(((min_x + max_x) / 2.0, (min_y + max_y) / 2.0, (min_z + max_z) / 2.0))
dims = Vector((max_x - min_x, max_y - min_y, max_z - min_z))
radius = max(dims.x, dims.y, dims.z) / 2.0
if radius <= 0.0:
    radius = 0.1


def set_render(scene):
    engines = [item.identifier for item in
               bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items]
    scene.render.engine = 'BLENDER_EEVEE_NEXT' if 'BLENDER_EEVEE_NEXT' in engines else 'BLENDER_EEVEE'
    scene.eevee.taa_render_samples = 64
    scene.render.resolution_x = 1600
    scene.render.resolution_y = 1600
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.film_transparent = False
    try:
        scene.view_settings.view_transform = 'AgX'
    except Exception:
        pass
    try:
        scene.view_settings.look = 'AgX - Medium High Contrast'
    except Exception:
        pass


def make_world(name, color, strength):
    world = bpy.data.worlds.new(name)
    world.use_nodes = True
    nodes = world.node_tree.nodes
    bg = nodes.get("Background")
    if bg is None:
        bg = nodes.new("ShaderNodeBackground")
        out = nodes.get("World Output") or nodes.new("ShaderNodeOutputWorld")
        world.node_tree.links.new(bg.outputs[0], out.inputs[0])
    bg.inputs["Color"].default_value = color
    bg.inputs["Strength"].default_value = strength
    return world


def direction(lon_deg, lat_deg):
    lon = math.radians(lon_deg)
    lat = math.radians(lat_deg)
    return Vector((
        math.cos(lat) * math.cos(lon),
        math.cos(lat) * math.sin(lon),
        math.sin(lat),
    ))


def make_sun(name, to_sun, energy, scene):
    data = bpy.data.lights.new(name, type='SUN')
    data.energy = energy
    data.angle = math.radians(0.53)
    data.use_shadow = True
    try:
        data.use_temperature = True
        data.temperature = 5778.0
    except Exception:
        data.color = (1.0, 0.96, 0.90)
    obj = bpy.data.objects.new(name, data)
    scene.collection.objects.link(obj)
    obj.rotation_euler = (-to_sun).to_track_quat('-Z', 'Y').to_euler()
    return obj


# The phase camera sits at the current sub-observer point, with lunar north up.
obs = direction(SUB_OBSERVER_LON, SUB_OBSERVER_LAT)
lon = math.radians(SUB_OBSERVER_LON)
lat = math.radians(SUB_OBSERVER_LAT)
east = Vector((-math.sin(lon), math.cos(lon), 0.0))
north = Vector((-math.sin(lat) * math.cos(lon),
                -math.sin(lat) * math.sin(lon),
                math.cos(lat)))
if north.length < 1e-9:
    north = Vector((1.0, 0.0, 0.0))
right = north.cross(obs)
if right.length < 1e-9:
    right = east
right.normalize()
north = obs.cross(right)
north.normalize()
cam_data = bpy.data.cameras.new("Camera_Phase")
cam = bpy.data.objects.new("Camera_Phase", cam_data)
bpy.context.scene.collection.objects.link(cam)
cam.location = center + obs * (radius * 3.2)
rot = Matrix((right, north, obs)).transposed()
cam.matrix_world = Matrix.Translation(cam.location) @ rot.to_4x4()
cam_data.lens = 50.0
cam_data.sensor_width = 36.0
cam_data.clip_start = radius * 0.005
cam_data.clip_end = radius * 100.0

top_data = bpy.data.cameras.new("Camera_Top")
top = bpy.data.objects.new("Camera_Top", top_data)
bpy.context.scene.collection.objects.link(top)
top.location = center + Vector((0.0, 0.0, radius * 3.2))
top.rotation_euler = (0.0, 0.0, 0.0)
top_data.type = 'ORTHO'
top_data.ortho_scale = radius * 2.15
top_data.clip_start = radius * 0.005
top_data.clip_end = radius * 100.0

# Current phase: the Sun is exactly at the current subsolar point.
phase = bpy.context.scene
phase.name = "Current phase"
phase.camera = cam
phase.world = make_world("Deep space", (0.006, 0.008, 0.014, 1.0), 0.18)
make_sun("Phase Sun (infinite)", direction(SUBSOLAR_LON, SUBSOLAR_LAT), 6.0, phase)
set_render(phase)
phase.view_settings.exposure = 0.25
phase.render.filepath = "//moon_globe_phase.png"

text = bpy.data.texts.new("场景说明")
text.write(NOTE)

bpy.context.window.scene = phase
bpy.ops.wm.save_as_mainfile(filepath=OUT, compress=True)
print("SAVED", OUT, [s.name for s in bpy.data.scenes])
'''


def _blender_globe_script(
    glb_path: Path,
    blend_path: Path,
    note: str,
    sub_observer_lon: float,
    sub_observer_lat: float,
    subsolar_lon: float,
    subsolar_lat: float,
) -> str:
    return (
        _BLENDER_GLOBE_TEMPLATE
        .replace("__GLB__", repr(str(glb_path)))
        .replace("__OUT__", repr(str(blend_path)))
        .replace("__SUB_OBSERVER_LON__", repr(float(sub_observer_lon)))
        .replace("__SUB_OBSERVER_LAT__", repr(float(sub_observer_lat)))
        .replace("__SUBSOLAR_LON__", repr(float(subsolar_lon)))
        .replace("__SUBSOLAR_LAT__", repr(float(subsolar_lat)))
        .replace("__NOTE__", repr(str(note)))
    )


class MoonService:
    """Holds the terrain and the view cache, so requests stay cheap."""

    def __init__(self, settings_dir: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        # Held for the length of a ray march, so two of them cannot run at once.
        self._render_lock = threading.Lock()
        # Blender is a separate process and needs a whole scene directory; one
        # preview at a time keeps two imports from competing for memory.
        self._blender_lock = threading.Lock()
        # The whole-Moon mesh and equirectangular albedo are independent of the
        # selection; build them once per terrain object.
        self._globe_lock = threading.Lock()
        self._globe_cache: Optional[Tuple[int, bytes, Dict[str, Any]]] = None
        # Low-resolution equirectangular textures for the full-disk 3D view.
        self._texture_cache: Dict[Tuple[Any, ...], bytes] = {}
        # The flag the running render polls.  Set by /api/cancel, replaced at the
        # start of every render: a cancel that arrives after the frame it was
        # meant for must not stop the next one.
        self._cancel_event = threading.Event()
        self._terrain = None
        #: Which product the full-disk pyramid is drawn from, and (through the
        #: library preference) the one close-up windows are cut from.  Starts at
        #: the product this machine already has a pyramid for, so dropping a
        #: finer raster into ``kernels/`` does not silently re-bake the world.
        self._dem_source = _pyramid_dem_source()
        # The 数据源 menu chooses what *windows and the reported floor* are made
        # of; the pyramid above is a separate, slower decision.
        moonlib.set_preferred_dem(_remembered_source() or self._dem_source)
        self._views: Dict[Tuple[str, float, float], Any] = {}
        self._terrain_seconds = 0.0
        self.settings = SettingsStore(settings_dir or DEFAULT_SETTINGS_DIR)
        # The last geometric render, kept as *linear radiance*.  Blur, sharpen,
        # tone and the level controls are all operations on this array, so they
        # must not force the ray march to run again: a slider drag would other-
        # wise cost a full render every time.  Keyed by a counter the client
        # echoes back, so a stale page cannot process the wrong frame.
        self._linear: Optional[np.ndarray] = None
        # The same frame with its two physical halves kept apart, at the same
        # exposure as ``_linear`` -- they add up to it exactly.  Keeping them is
        # what lets 分量出图 hand back the sunlit or the earthshine on its own,
        # and it costs one array the march has already produced: the renderer
        # traced the two separately and summed them.
        self._linear_sunlit: Optional[np.ndarray] = None
        self._linear_earthshine: Optional[np.ndarray] = None
        self._linear_id: Optional[str] = None
        self._linear_meta: Dict[str, Any] = {}
        self._linear_context: Dict[str, Any] = {}
        # The terrain the cached close-up was actually traced against.  It may
        # be a finer regional window than ``self.terrain()``; annotations have
        # to stand on the same ground as the pixels, not on a coarser pyramid.
        self._linear_terrain: Any = None
        # The view and camera the cached frame was rendered with.  Labels have
        # to describe *that* frame, not whatever the observer fields say now: a
        # page that changes the time and then labels the picture still on
        # screen would otherwise draw names for a geometry nobody can see.
        self._linear_view: Any = None
        self._catalogue: Optional[Tuple[Any, ...]] = None
        self._feature_index: Optional[Dict[str, Any]] = None
        # Background animations: the job table is small and guarded by its own
        # lock, because a thread writes into it while requests read from it.
        self._animations: Dict[str, Dict[str, Any]] = {}
        self._animation_seq = 0
        self._animation_lock = threading.Lock()
        #: The running job's cancel flag, if any.  A preview must not preempt a
        #: background animation: that would silently turn a film into a still.
        self._animation_cancel: Optional[threading.Event] = None
        #: Region downloads (数据源 → 下载此区域).  Small, guarded by its own
        #: lock, and keyed the same way the animations are.
        self._patches: Dict[str, Dict[str, Any]] = {}
        self._patch_seq = 0
        self._patch_lock = threading.Lock()
        self._counter = 0

    def terrain(self):
        with self._lock:
            if self._terrain is None:
                started = time.time()
                finest = _finest_baked_albedo_detail(self._dem_source)
                detail = ALBEDO_DETAIL if finest is None else finest
                self._terrain = moonlib.open_terrain(
                    dem=self._dem_source, albedo_detail=detail
                )
                self._terrain_seconds = time.time() - started
            return self._terrain

    # -- 数据源 -----------------------------------------------------------
    def data_sources(self) -> Dict[str, Any]:
        """Every global DEM this install can draw, plus the ones it knows about.

        Discovery, not a hard-coded list: the menu shows what actually parsed as
        a global raster, so `ldem_512.tif` appearing after the download is the
        same event as it appearing here.  ``selected`` is the product in use --
        the one chosen last time, or the one this machine already has a pyramid
        for.  ``online`` names the products that are *not* on this machine and
        says how to get them; a page that offers a source it cannot draw is
        worse than one that says "run this".
        """
        found = moonlib.discover_dem_sources()
        chosen = moonlib.preferred_dem()
        sources = []
        for path, km in found:
            stat = path.stat()
            # A one-line provenance note where the file name alone does not say
            # what it is.  The SLDEM global mosaic is a hybrid: its mid-latitudes
            # are the stereo-filled product and its polar caps are LOLA.
            lowered = path.name.lower()
            if "sldem" in lowered:
                note = ("±60° 内为 SLDEM2015（辉夜号 TC 立体填缝），"
                        "极区复用 LOLA 512")
            elif "ldem_512" in lowered:
                note = "LOLA GDR V3.0，59.2 m 全球拼接"
            else:
                note = ""
            sources.append({
                "id": str(path),
                "name": path.name,
                "note": note,
                "km_per_px": float(km),
                "metres_per_px": km * 1000.0,
                "pixels_per_degree": int(round(_KM_PER_DEGREE / km)),
                "bytes": int(stat.st_size),
                "mtime": int(stat.st_mtime),
            })
        active = moonlib.global_dem_sources()
        # The online group is the *patch* catalogue, not a second copy of the
        # local one: these are the products a region download can fetch, and
        # none of them is on disk as a global raster (that is why they are
        # "online").  Claiming one is 已在本机 because a local source happens to
        # have the same pixel size would be a lie about which data is being
        # drawn.
        from moonlib import online as online_products

        online = [
            {**entry, "available": False}
            for entry in online_products.catalogue()
        ]
        return {
            "sources": sources,
            "selected": str(chosen) if chosen is not None else None,
            # What the page may offer as the finest scale: the resolution of the
            # product in use, which is what a render would actually have.
            "floor_km_per_px": float(active[0][1]) if active else None,
            "online": online,
        }

    def set_data_source(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Switch the DEM every later frame is drawn from.

        ``None``/``"auto"`` goes back to the finest raster on disk.  A path that
        is not one of the discovered global rasters is refused: accepting it
        would leave the page claiming a source it is not drawing.
        """
        wanted = request.get("id")
        if wanted in (None, "", "auto"):
            chosen = moonlib.set_preferred_dem(_pyramid_dem_source())
        else:
            available = {path.resolve() for path, _ in moonlib.discover_dem_sources()}
            candidate = Path(str(wanted)).expanduser().resolve()
            if candidate not in available:
                raise moonlib.MoonlibError(
                    f"数据源不在可用列表里：{candidate.name}"
                )
            chosen = moonlib.set_preferred_dem(candidate)
        # The *pyramid* is deliberately left alone.  A full-disk frame at the
        # page's 10000 px ceiling is 370 m/px and only ever samples the 474 m
        # level, so rebuilding an 11 GB pyramid for a finer product would buy
        # nothing and cost an hour; what a finer product does buy is close-up
        # windows, which are cut straight from the source file and need no
        # pyramid at all.  Choosing a source therefore changes what close-ups
        # and the 物理分辨率 floor are made of, immediately.
        with self._lock:
            self._views.clear()
        try:
            _dem_source_file().parent.mkdir(parents=True, exist_ok=True)
            _dem_source_file().write_text(
                json.dumps({"id": str(chosen) if chosen is not None else None,
                            "name": chosen.name if chosen is not None else None},
                           ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:  # pragma: no cover - a read-only cache is not fatal
            pass
        payload = self.data_sources()
        return {"selected": str(chosen) if chosen is not None else None,
                "sources": payload["sources"],
                "floor_km_per_px": payload["floor_km_per_px"],
                # What the full disk will keep drawing from: a statement about
                # the picture on screen, which the source choice does not move.
                "pyramid": Path(self._dem_source).name if self._dem_source else None}

    def catalogue(self) -> Tuple[Any, ...]:
        """Named landforms, read once: the gazetteer is 9 000 rows of CSV."""
        with self._lock:
            if self._catalogue is None:
                self._catalogue = moonlib.annotation_catalogue()
            return self._catalogue

    def view(self, when_utc: str, site: moonlib.Observer,
             earth_albedo: float = 0.0, viewpoint: str = "earth"):
        return self.report(when_utc, site, earth_albedo, viewpoint)["view"]

    def report(self, when_utc: str, site: moonlib.Observer,
               earth_albedo: float = 0.0, viewpoint: str = "earth"
               ) -> Dict[str, Any]:
        """Geometry plus the full moon_report, cached together.

        The report includes rise, transit and set, which are found by root
        solving and are far too slow to redo on every keystroke; the page asks
        for it on each change, so it is cached against the same key.

        A non-default Earth albedo is part of that key rather than a detail of
        the render: it is read once, while the view is built, so two albedos are
        two views -- and the earthshine the frame reports has to be the one it
        was drawn with.  The viewpoint is part of the key for the same reason:
        a far-side frame and a near-side frame are different views of the same
        instant, and must not hand each other's geometry back.
        """
        key = (
            str(when_utc),
            round(site.lat, 6),
            round(site.lon, 6),
            round(float(earth_albedo or 0.0), 6),
            str(viewpoint),
        )
        with self._lock:
            cached = self._views.get(key)
        if cached is None:
            view = moonlib.view_geometry(
                site, when_utc, earth_albedo=earth_albedo or None,
                viewpoint=viewpoint,
            )
            try:
                report = moonlib.moon_report(site, when_utc).to_dict()
            except Exception:  # pragma: no cover - the report is a nicety
                report = {}
            cached = {"view": view, "report": report}
            with self._lock:
                if len(self._views) > 64:
                    self._views.clear()
                self._views[key] = cached
        return cached

    def limits(self) -> Dict[str, Any]:
        """What the page may ask for, so it can promise the same numbers."""
        return {
            "preview_px": MAX_PREVIEW_PX,
            "output_px": MAX_OUTPUT_PX,
            "disk_px": MAX_DISK_PX,
            "min_px": MIN_PX,
            "terrain_km_per_px": terrain_km_per_px(),
            # Published so the page scales the rim threshold with everything
            # else when it asks for a plan at the export resolution, instead of
            # carrying its own copy of the number.
            "label_outline_px": DEFAULT_OUTLINE_RADIUS_PX,
            # The animation's frame ceiling, so the page can say "this will be
            # refused" before the reader spends a press finding out.
            "animation_max_frames": ANIMATION_MAX_FRAMES,
            # The page's 并行数 field must not promise more threads than the
            # host can run, and the renderer clamps to the same number.
            "cpu_count": os.cpu_count() or 1,
            # The page prints this in its header, so "which build is this?" has
            # an answer without opening a terminal.
            "version": moonlib.__version__,
        }

    # -- sky chart ------------------------------------------------------
    def sky(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Moon and Sun tracks in azimuth/elevation for the sky window.

        The page draws the chart itself, so this returns samples rather than a
        picture: it is the same ``az_el`` the library's matplotlib figure uses,
        but every point can then be labelled, connected and re-ranged by the
        reader without asking the server again.

        The window is centred on the page's own instant and is *symmetric* --
        ``span_hours`` is the half-width.  Samples are returned in UTC because
        the page already owns the offset it displays them in.
        """
        when = request.get("when") or {}
        site = parse_observer(when)
        span = _f(request, "span_hours", 4.0)
        if not 0.25 <= span <= 24.0:
            raise moonlib.MoonlibError(
                f"天空图的时间跨度必须在 ±0.25–±24 小时之间（收到 ±{span:g}）。"
            )
        step = _f(request, "step_seconds", 120.0)
        if not 10.0 <= step <= 3600.0:
            raise moonlib.MoonlibError(
                f"天空图的采样间隔必须在 10–3600 秒之间（收到 {step:g}）。"
            )
        count = int(round(2.0 * span * 3600.0 / step)) + 1
        if count > 4000:
            raise moonlib.MoonlibError(
                "天空图的采样点太多（"
                f"{count} 个）；请加大采样间隔或缩短时间跨度。"
            )

        anchor = when.get("at") or None
        anchor_utc = moonlib.as_utc(anchor) if anchor else moonlib.utc_now()
        anchor_et = moonlib.to_et(anchor_utc)
        geometry = crescent_geometry(anchor_et, site)
        start_et = anchor_et - span * 3600.0
        samples = []
        for index in range(count):
            et = start_et + index * step
            moon = moonlib.az_el("MOON", et, site)
            sun = moonlib.az_el("SUN", et, site)
            samples.append({
                "when_utc": moonlib.to_utc(et).isoformat(),
                "moon_azimuth_deg": float(moon.azimuth_deg),
                "moon_elevation_deg": float(moon.elevation_deg),
                "sun_azimuth_deg": float(sun.azimuth_deg),
                "sun_elevation_deg": float(sun.elevation_deg),
            })

        moon_at = moonlib.az_el("MOON", anchor_et, site)
        sun_at = moonlib.az_el("SUN", anchor_et, site)
        return {
            "when_utc": anchor_utc.isoformat(),
            "site": site.as_dict(),
            "tz_offset_hours": _tz_offset(request),
            "span_hours": float(span),
            "step_seconds": float(step),
            "samples": samples,
            "moon": {
                "azimuth_deg": float(moon_at.azimuth_deg),
                "elevation_deg": float(moon_at.elevation_deg),
                "range_km": float(moon_at.range_km),
            },
            "sun": {
                "azimuth_deg": float(sun_at.azimuth_deg),
                "elevation_deg": float(sun_at.elevation_deg),
                "range_km": float(sun_at.range_km),
            },
            "geometry": {
                "elongation_deg": float(geometry.elongation_deg),
                "illumination_percent_geo": float(
                    geometry.illumination_percent_geo
                ),
                "illumination_percent_topo": float(
                    geometry.illumination_percent_topo
                ),
                "phase_angle_geo_deg": float(geometry.phase_angle_geo_deg),
                "moon_age_days": float(geometry.moon_age_days),
                "angular_diameter_arcsec": float(geometry.angular_diameter_arcsec),
                "crescent_width_arcsec": float(geometry.crescent_width_arcsec),
                "bright_limb_angle_deg": float(geometry.bright_limb_angle_deg),
            },
            # The Moon's marker in the chart is a picture of the body, so it
            # has to know when the body is inside the Earth's shadow.  The
            # state is at the chart's own instant, not at the main page's.
            "eclipse": moonlib.eclipse_state(anchor_et).to_dict(),
        }

    # -- experimental: the Sun from a lunar surface point ---------------
    def surface_sun(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """The Sun's local altitude and azimuth at one selenographic point."""
        when = request.get("when") or {}
        site = parse_observer(when)
        lon = _f(request, "moon_lon", 0.0)
        lat = _f(request, "moon_lat", 0.0)
        start = str(request.get("start") or "").strip()
        end = str(request.get("end") or "").strip()
        if not start or not end:
            raise moonlib.MoonlibError("月面太阳高度需要开始与结束时间。")
        step = max(1.0, min(1440.0, _f(request, "step_minutes", 10.0)))
        rows = moonlib.lunar_surface_solar_series(
            lon, lat, start, end, step_minutes=step, observer=site
        )
        return {
            "kind": "surface_sun",
            "site": site.as_dict(),
            "tz_offset_hours": _tz_offset(request),
            "moon_lon": lon,
            "moon_lat": lat,
            "start": start,
            "end": end,
            "step_minutes": step,
            "samples": [row.to_dict() for row in rows],
        }

    # -- curve window ---------------------------------------------------
    def curve(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Samples for the curve window's two charts.

        ``moon8`` returns a synodic month of the Moon's right ascension and
        declination at one fixed *local clock reading*: the lunar counterpart of
        the solar analemma.  ``earthshine`` returns the night-side magnitude as
        a function of phase angle, computed on a dense grid so the page can plot
        it against either phase angle or illuminated fraction without another
        round trip.
        """
        kind = str(request.get("kind") or "moon8").strip().lower()
        when = request.get("when") or {}
        site = parse_observer(when)
        offset = _tz_offset(request)
        zone = timezone(timedelta(hours=offset))

        if kind in ("moon8", "moon_analemma", "lunar_analemma"):
            raw_start = str(request.get("start") or "").strip()
            if raw_start:
                try:
                    start_day = datetime.fromisoformat(raw_start).date()
                except ValueError as exc:
                    raise moonlib.MoonlibError(
                        f"曲线图的起始日期无法解析：{raw_start!r}"
                    ) from exc
            else:
                start_day = moonlib.as_utc(
                    when.get("at") or moonlib.utc_now()
                ).astimezone(zone).date()
            hour = int(max(0, min(23, _f(request, "hour", 21))))
            minute = int(max(0, min(59, _f(request, "minute", 0))))
            days = int(round(max(1.0, min(366.0, _f(request, "days", 30.0)))))
            drift = max(0.0, min(120.0, _f(request, "drift_minutes", 50.47)))
            start_local = datetime(
                start_day.year, start_day.month, start_day.day, tzinfo=zone
            )
            end_local = start_local + timedelta(days=days)
            local_site = site.with_timezone(zone)
            from moonlib.analemma import lunar_analemma

            points = lunar_analemma(
                local_site, start_local, end_local,
                local_hour=hour, local_minute=minute,
                drift_minutes=drift,
            )
            rows = [point.to_dict() for point in points]
            # Extremes are a caption, not a second computation: every point is
            # already in hand.
            dec_values = [row["declination_deg"] for row in rows]
            ra_values = [row["ra_hours"] for row in rows]
            az_values = [row["azimuth_deg"] for row in rows]
            el_values = [row["elevation_deg"] for row in rows]
            return {
                "kind": "moon8",
                "site": local_site.as_dict(),
                "tz_offset_hours": offset,
                "start_utc": start_local.astimezone(timezone.utc).isoformat(),
                "end_utc": end_local.astimezone(timezone.utc).isoformat(),
                "local_hour": hour,
                "local_minute": minute,
                "days": days,
                "drift_minutes": drift,
                "samples": rows,
                "extremes": {
                    "ra_min_hours": min(ra_values) if ra_values else None,
                    "ra_max_hours": max(ra_values) if ra_values else None,
                    "declination_min_deg": min(dec_values) if dec_values else None,
                    "declination_max_deg": max(dec_values) if dec_values else None,
                    "azimuth_min_deg": min(az_values) if az_values else None,
                    "azimuth_max_deg": max(az_values) if az_values else None,
                    "elevation_min_deg": min(el_values) if el_values else None,
                    "elevation_max_deg": max(el_values) if el_values else None,
                },
            }

        if kind in ("sun8", "solar_analemma", "sun_analemma"):
            raw_start = str(request.get("start") or "").strip()
            if raw_start:
                try:
                    start_day = datetime.fromisoformat(raw_start).date()
                except ValueError as exc:
                    raise moonlib.MoonlibError(
                        f"曲线图的起始日期无法解析：{raw_start!r}"
                    ) from exc
            else:
                start_day = moonlib.as_utc(
                    when.get("at") or moonlib.utc_now()
                ).astimezone(zone).date()
            hour = int(max(0, min(23, _f(request, "hour", 12))))
            minute = int(max(0, min(59, _f(request, "minute", 0))))
            days = int(round(max(1.0, min(366.0, _f(request, "days", 365.0)))))
            start_local = datetime(
                start_day.year, start_day.month, start_day.day, tzinfo=zone
            )
            end_local = start_local + timedelta(days=days)
            local_site = site.with_timezone(zone)
            from moonlib.analemma import solar_analemma

            points = solar_analemma(
                local_site, start_local, end_local,
                local_hour=hour, local_minute=minute,
            )
            rows = [point.to_dict() for point in points]
            az_values = [row["azimuth_deg"] for row in rows]
            el_values = [row["elevation_deg"] for row in rows]
            dec_values = [row["declination_deg"] for row in rows]
            eot_values = [row["equation_of_time_minutes"] for row in rows]
            return {
                "kind": "sun8",
                "site": local_site.as_dict(),
                "tz_offset_hours": offset,
                "start_utc": start_local.astimezone(timezone.utc).isoformat(),
                "end_utc": end_local.astimezone(timezone.utc).isoformat(),
                "local_hour": hour,
                "local_minute": minute,
                "days": days,
                "drift_minutes": 0.0,
                "samples": rows,
                "extremes": {
                    "azimuth_min_deg": min(az_values) if az_values else None,
                    "azimuth_max_deg": max(az_values) if az_values else None,
                    "elevation_min_deg": min(el_values) if el_values else None,
                    "elevation_max_deg": max(el_values) if el_values else None,
                    "declination_min_deg": min(dec_values) if dec_values else None,
                    "declination_max_deg": max(dec_values) if dec_values else None,
                    "equation_of_time_min_minutes": min(eot_values) if eot_values else None,
                    "equation_of_time_max_minutes": max(eot_values) if eot_values else None,
                },
            }

        if kind in ("altitude", "diameter", "libration", "geometry",
                    "moon_altitude", "apparent_diameter", "libration_curve",
                    "moon_geometry"):
            raw_start = str(request.get("start") or "").strip()
            if raw_start:
                try:
                    start_day = datetime.fromisoformat(raw_start).date()
                except ValueError as exc:
                    raise moonlib.MoonlibError(
                        f"曲线图的起始日期无法解析：{raw_start!r}"
                    ) from exc
            else:
                start_day = moonlib.as_utc(
                    when.get("at") or moonlib.utc_now()
                ).astimezone(zone).date()
            hour = int(max(0, min(23, _f(request, "hour", 0))))
            minute = int(max(0, min(59, _f(request, "minute", 0))))
            days = int(round(max(1.0, min(366.0, _f(request, "days", 30.0)))))
            step_minutes = int(round(max(
                1.0, min(1440.0, _f(request, "step_minutes", 120.0))
            )))
            # A year of hourly samples is a request for a year, not for a
            # million points: widen the stride rather than refuse the range.
            max_samples = 2400
            wanted = int(math.ceil(days * 1440.0 / step_minutes))
            if wanted > max_samples:
                step_minutes = int(math.ceil(days * 1440.0 / max_samples))
            start_local = datetime(
                start_day.year, start_day.month, start_day.day,
                hour, minute, tzinfo=zone,
            )
            end_local = start_local + timedelta(days=days)
            start_utc = start_local.astimezone(timezone.utc)
            local_site = site.with_timezone(zone)
            from moonlib.moon import moon_state

            rows = []
            cursor = start_local
            while cursor < end_local:
                when_utc = cursor.astimezone(timezone.utc)
                state = moon_state(local_site, when_utc)
                rows.append({
                    "when_utc": when_utc.isoformat(),
                    "local_time": when_utc.astimezone(zone).strftime(
                        "%Y-%m-%d %H:%M:%S %Z"
                    ),
                    "elapsed_hours": (when_utc - start_utc).total_seconds() / 3600.0,
                    "day_of_year": cursor.timetuple().tm_yday,
                    "month": cursor.month,
                    "phase_angle_deg": float(state.phase.phase_angle_geo_deg),
                    "illumination_percent": float(
                        state.phase.illuminated_percent_geo
                    ),
                    "waxing": bool(state.phase.waxing),
                    "azimuth_deg": float(state.azimuth_deg),
                    "elevation_deg": float(state.elevation_deg),
                    "angular_diameter_arcsec": float(
                        state.angular_diameter_arcsec
                    ),
                    "angular_diameter_deg": float(state.angular_diameter_deg),
                    "distance_km": float(state.topo_distance_km),
                    "libration_lon_deg": float(
                        state.libration.libration_lon_deg
                    ),
                    "libration_lat_deg": float(
                        state.libration.libration_lat_deg
                    ),
                })
                cursor += timedelta(minutes=step_minutes)

            def _range(key: str):
                values = [row[key] for row in rows]
                return (min(values), max(values)) if values else (None, None)

            elevations = _range("elevation_deg")
            azimuths = _range("azimuth_deg")
            diameters = _range("angular_diameter_arcsec")
            lon = _range("libration_lon_deg")
            lat = _range("libration_lat_deg")
            phases = _range("phase_angle_deg")
            return {
                "kind": (
                    "geometry" if kind in ("geometry", "moon_geometry")
                    else "altitude" if kind in ("altitude", "moon_altitude")
                    else "diameter" if kind in (
                        "diameter", "apparent_diameter"
                    ) else "libration"
                ),
                "site": local_site.as_dict(),
                "tz_offset_hours": offset,
                "start_utc": start_utc.isoformat(),
                "end_utc": end_local.astimezone(timezone.utc).isoformat(),
                "local_hour": hour,
                "local_minute": minute,
                "days": days,
                "step_minutes": step_minutes,
                "drift_minutes": 0.0,
                "samples": rows,
                "extremes": {
                    "elevation_min_deg": elevations[0],
                    "elevation_max_deg": elevations[1],
                    "azimuth_min_deg": azimuths[0],
                    "azimuth_max_deg": azimuths[1],
                    "angular_diameter_min_arcsec": diameters[0],
                    "angular_diameter_max_arcsec": diameters[1],
                    "libration_lon_min_deg": lon[0],
                    "libration_lon_max_deg": lon[1],
                    "libration_lat_min_deg": lat[0],
                    "libration_lat_max_deg": lat[1],
                    "phase_angle_min_deg": phases[0],
                    "phase_angle_max_deg": phases[1],
                },
            }

        if kind in ("earthshine", "ashen", "earthshine_relation"):
            earth = _earthshine(request.get("earthshine"))
            model = _curve_earth_model(request, earth["earth_geometric_albedo"])
            anchor = moonlib.as_utc(when.get("at") or moonlib.utc_now())
            view = self.view(anchor.isoformat(), site, model["albedo"])
            phase_angles = np.linspace(180.0, 0.0, 361)
            rows = moonlib.moonview.earthshine_relation(
                phase_angles,
                distance_km=view.distance_km,
                earth_distance_km=view.earth_distance_km,
                earth_albedo=model["albedo"],
                earth_phase_model=model["phase"],
                earth_phase_g=model["g"],
            )
            current = moonlib.moonview.earthshine_relation(
                [view.phase_angle_deg],
                distance_km=view.distance_km,
                earth_distance_km=view.earth_distance_km,
                earth_albedo=model["albedo"],
                earth_phase_model=model["phase"],
                earth_phase_g=model["g"],
            )[0]
            return {
                "kind": "earthshine",
                "site": site.as_dict(),
                "tz_offset_hours": offset,
                "when_utc": anchor.isoformat(),
                "earth_model": model["key"],
                "earth_model_label": model["label"],
                "earth_phase_model": model["phase"],
                "earth_phase_g": model["g"],
                "earth_geometric_albedo": model["albedo"],
                "distance_km": float(view.distance_km),
                "earth_distance_km": float(view.earth_distance_km),
                "samples": rows,
                "current": current,
            }

        raise moonlib.MoonlibError(
            f"未知的曲线图类型 {kind!r}；可用 moon8（月亮8字）、sun8（太阳8字）、"
            "earthshine（地照）、geometry（月球几何量）、altitude（高度）、"
            "diameter（视直径）或 libration（天平动）。"
        )

    # -- full disk ------------------------------------------------------
    def cancel(self) -> Dict[str, Any]:
        """Stop the frame that is being marched, if there is one.

        The page calls this when the reader presses 终止.  Without it the abort
        only closed the request: the server marched the whole frame anyway, and
        two presses left two abandoned renders holding memory and pinning the
        CPU until somebody killed the process.
        """
        running = self._render_lock.locked()
        self._cancel_event.set()
        return {"cancelled": bool(running), "rendering": bool(running)}

    @contextmanager
    def _rendering(self):
        """A fresh cancel flag for the frame about to start.

        Replaced rather than cleared: a cancel that arrives just after one frame
        ends belongs to that frame, and must not stop the next one.

        Yields the new event so a long job -- the animation, which renders many
        frames -- can hold on to *its own* flag instead of reading the attribute
        again later, by which time a second render may have replaced it.
        """
        self._cancel_event = threading.Event()
        yield self._cancel_event

    def _take_render_slot(self, waited: Optional[float] = None) -> None:
        """Take the one ray-march slot, waiting a little for a cancelled frame.

        The page asks for a frame, the reader presses 终止 because it is slow,
        and presses again -- without one slot at a time the abandoned marches
        keep running beside the new one and the process ends up holding several
        frames' worth of memory with nothing to show for it.

        But 终止 cannot be instant: a band is traced as one vectorised block, so
        the frame stops at the end of the band it is *in*, which at 3000 px is a
        few seconds.  Refusing the next request in that window is what made
        "stop, then start again" two gestures with an error between them, so
        this waits for the handover instead.  The wait is bounded: a render that
        is genuinely stuck still has to report itself rather than hang the page.
        """
        if not self._cancel_event.is_set():
            # Nothing has been stopped, so this is a second frame being asked
            # for while the first one is still going: say so rather than queue
            # it behind a render nobody has given up on.
            if self._render_lock.acquire(blocking=False):
                return
            raise moonlib.MoonlibError(
                "上一张还在渲染。等它结束（或稍等几秒）再试；要停它就按「终止」。"
            )
        seconds = _RENDER_HANDOVER_SECONDS if waited is None else float(waited)
        # A short poll rather than `acquire(timeout=...)`, so the deadline and
        # the message about it are one thing in one place.
        deadline = time.monotonic() + seconds
        while True:
            if self._render_lock.acquire(blocking=False):
                return
            if time.monotonic() >= deadline:
                raise moonlib.MoonlibError(
                    "上一张还在渲染，而且没有响应「终止」。等它结束再试，"
                    "或者重启一下服务。"
                )
            time.sleep(0.05)

    @contextmanager
    def _render_slot(self):
        """One ray march at a time; see :meth:`_take_render_slot`."""
        self._take_render_slot()
        try:
            yield
        finally:
            self._render_lock.release()

    def globe_texture(self, request: Dict[str, Any], width: int) -> bytes:
        """A low-resolution equirectangular Moon for the 3D preview.

        This is deliberately not a ray march: the client only uses it at low
        zoom, and a 1024x512 shaded texture is a few hundred kilobytes.  The
        texture is rebuilt when the Sun direction changes and cached by width
        and subsolar vector, so rotating the globe costs no server work at all.
        """
        width = max(512, min(2048, int(width)))
        height = width // 2
        when = request.get("when") or {}
        site = parse_observer(when)
        view = self.view(
            when.get("at") or None, site,
            _earthshine(request.get("earthshine"))["earth_geometric_albedo"],
            parse_viewpoint(request),
        )
        key = (
            width,
            round(float(view.sun_direction[0]), 4),
            round(float(view.sun_direction[1]), 4),
            round(float(view.sun_direction[2]), 4),
        )
        with self._lock:
            cached = self._texture_cache.get(key)
        if cached is not None:
            return cached

        terrain = self.terrain()
        lon = np.linspace(-180.0, 180.0, width, endpoint=False) + 180.0 / width
        lat = 90.0 - (np.arange(height, dtype=np.float64) + 0.5) / height * 180.0
        lon_grid, lat_grid = np.meshgrid(lon, lat)
        radius_km = float(terrain.reference_radius_km)
        km_per_px = math.radians(360.0 / width) * radius_km
        try:
            level = int(terrain.dem.level_for_ground_scale(km_per_px))
        except Exception:
            level = 0
        level = max(0, min(level, int(getattr(terrain.dem, "n_levels", level + 1)) - 1))
        heights = np.asarray(
            terrain.dem.sample(lon_grid, lat_grid, level), dtype=np.float64
        )
        if heights.shape != lon_grid.shape:  # pragma: no cover - defensive
            heights = np.broadcast_to(heights, lon_grid.shape).astype(np.float64)
        heights = np.where(np.isfinite(heights), heights, np.nanmin(heights))

        d_lon = 360.0 / width
        d_lat = 180.0 / height
        east = np.asarray(terrain.dem.sample(lon_grid + d_lon, lat_grid, level),
                          dtype=np.float64)
        west = np.asarray(terrain.dem.sample(lon_grid - d_lon, lat_grid, level),
                          dtype=np.float64)
        north = np.asarray(terrain.dem.sample(lon_grid, lat_grid + d_lat, level),
                           dtype=np.float64)
        south = np.asarray(terrain.dem.sample(lon_grid, lat_grid - d_lat, level),
                           dtype=np.float64)
        cos_lat = np.maximum(np.cos(np.radians(lat_grid)), 0.02)
        dx_km = math.radians(d_lon) * radius_km * cos_lat
        dy_km = math.radians(d_lat) * radius_km
        slope_east = (east - west) / (2.0 * dx_km)
        slope_north = (north - south) / (2.0 * dy_km)
        norm = np.sqrt(slope_east * slope_east + slope_north * slope_north + 1.0)
        normal_east = -slope_east / norm
        normal_north = -slope_north / norm
        normal_up = 1.0 / norm

        sx, sy, sz = (float(value) for value in view.sun_direction)
        lon_r = np.radians(lon_grid)
        lat_r = np.radians(lat_grid)
        light_east = -np.sin(lon_r) * sx + np.cos(lon_r) * sy
        light_north = (
            -np.sin(lat_r) * np.cos(lon_r) * sx
            - np.sin(lat_r) * np.sin(lon_r) * sy
            + np.cos(lat_r) * sz
        )
        light_up = (
            np.cos(lat_r) * np.cos(lon_r) * sx
            + np.cos(lat_r) * np.sin(lon_r) * sy
            + np.sin(lat_r) * sz
        )
        direct = (
            normal_east * light_east
            + normal_north * light_north
            + normal_up * light_up
        )
        shade = np.clip(0.10 + 1.04 * np.maximum(direct, 0.0), 0.0, 1.15)

        colors = None
        if terrain.albedo is not None:
            try:
                colors = np.asarray(
                    terrain.albedo_rgb(lon_grid, lat_grid, level), dtype=np.float64
                )
            except Exception:
                colors = None
        if colors is not None and colors.shape[-1] >= 3:
            base = np.clip(colors[..., :3], 0.0, 1.0) ** 0.45
            base = np.clip(base * 1.22, 0.0, 1.0)
        else:
            grey = np.clip(
                0.55 + 0.55 * (heights - float(np.mean(heights))) / 8.0,
                0.15, 1.0,
            )
            base = np.repeat(grey[..., None], 3, axis=-1)
        rgb = np.clip(base * shade[..., None], 0.0, 1.0)
        rgb = rgb * 0.92 / (1.0 + 0.18 * rgb)
        png = encode_png(rgb)
        with self._lock:
            self._texture_cache[key] = png
            while len(self._texture_cache) > 6:
                self._texture_cache.pop(next(iter(self._texture_cache)))
        return png

    def disk(self, request: Dict[str, Any], width: int, samples: int,
             shadow_steps: int,
             cancel: Optional[Callable[[], bool]] = None,
             ) -> Tuple[np.ndarray, Dict[str, Any]]:
        if int(width) > MAX_DISK_PX:
            raise moonlib.MoonlibError(
                f"整月面最大 {MAX_DISK_PX} px（收到 {int(width)}）——"
                "再往上地形数据已经没有更多细节了。"
            )
        if int(width) < MIN_PX:
            raise moonlib.MoonlibError(
                f"整月面最小 {MIN_PX} px（收到 {int(width)}）。"
            )
        when = request.get("when") or {}
        site = parse_observer(when)
        viewpoint = parse_viewpoint(request)
        earth = _earthshine(request.get("earthshine"))
        # The albedo describes the Earth, not the switch: a frame drawn with
        # earthshine off still reports the truth about the sky it was taken in.
        view = self.view(
            when.get("at") or None, site, earth["earth_geometric_albedo"],
            viewpoint,
        )
        settings = MoonRenderSettings(
            width=int(width),
            height=int(width),
            samples_per_pixel=int(samples),
            shadow_steps=int(shadow_steps),
            band_workers=_parallel(request.get("parallel")),
            photometric_model=_optical_model(
                _light_option(request, "preview", "photometric_model")
            ),
            sun_samples=_sun_samples(_light_option(request, "preview", "sun_samples")),
            tone="srgb",
            keep_components=True,
            sky_ambient=_sky_ambient(_light_option(request, "preview", "sky_ambient")),
            roughness_deg=_roughness(_light_option(request, "preview", "roughness_deg")),
            surface_texture=_surface_texture(
                _light_option(request, "preview", "surface_texture")
            ),
            terrain_denoise=_terrain_denoise(
                _light_option(request, "preview", "terrain_denoise")
            ),
            **earth,
        )
        started = time.time()
        # A preview is a request about *this* instant.  If an older render is
        # still marching -- the usual case after a page reload or a time switch
        # during a long close-up -- that frame is now stale, so stop it at its
        # next band instead of refusing the preview for the next ten minutes.
        if request.get("preempt") and self._animation_cancel is None:
            self._cancel_event.set()
        # The slot first, then a fresh cancel flag: whether the frame holding
        # the slot was cancelled is only readable until `_rendering()` replaces
        # the flag with this frame's own, and that is what decides whether to
        # wait for the handover or refuse at once.
        with self._render_slot():
            with self._rendering() as frame_cancel:
                render = moonlib.render_moon(
                    view=view, settings=settings, terrain=self.terrain(),
                    cancel=_combined_cancel(frame_cancel, cancel),
                )
        geometry = moonlib.view_summary(render.view)
        # The left-hand 月球信息 panel is about the real Moon in the observer's
        # sky even when the picture is the far side: rise/set/altitude have no
        # meaning for a virtual geocentric observer.  The picture's own
        # viewpoint travels in `geometry["viewpoint"]`.
        geometry["report"] = self.report(
            when.get("at") or None, site, earth["earth_geometric_albedo"],
            "earth",
        )["report"]
        if not geometry["report"]:  # pragma: no cover - fallback
            geometry["moon_altitude_deg"] = moonlib.az_el(
                "MOON", moonlib.to_et(geometry["when_utc"]), site
            ).elevation_deg
        geometry["utc_offset_hours"] = _tz_offset(request)
        # 农历: the real Chinese calendar, not the age-based approximation the
        # page used to derive.  Computed here because it needs the ephemeris,
        # and sent as a finished label so the two can never disagree.
        try:
            geometry["lunar"] = moonlib.chinese_lunar_date(
                geometry["when_utc"]).as_dict()
        except Exception:  # pragma: no cover - a display nicety
            geometry["lunar"] = None
        geometry["render_seconds"] = time.time() - started
        geometry["disk_radius_fraction"] = 0.5 * settings.disk_fraction
        geometry["image_width"] = int(width)
        geometry["bands"] = int(render.stats.get("bands", 1))
        geometry["band_workers"] = int(render.stats.get("band_workers", 1))
        # Whether skylight was on, and how much of the sky the shadowed pixels
        # could see: the page reports the term rather than assuming it is dark.
        for key in ("sky_ambient", "sky_samples", "sky_visibility_mean",
                    "sky_visibility_min", "sky_ambient_pixels", "roughness_deg",
                    "mean_sunlit_radiance", "peak_sunlit_radiance"):
            if key in render.stats:
                geometry[key] = render.stats[key]
        # What the frame was actually drawn with: the earthshine ratio moves by
        # two orders of magnitude over a lunation, and the page captions the
        # picture with these numbers rather than re-deriving them.
        geometry["earthshine"] = _earthshine_stats(render.stats)
        # The HDR gain map, when the page is going to save one.  It is computed
        # here, at the size the picture was rendered, because it is a statement
        # about the *scene*: the display image is a tone map of the same frame,
        # and only the renderer has both.
        hdr_request = request.get("hdr") or {}
        if hdr_request.get("on"):
            stops = float(_f(hdr_request, "stops", 3.0))
            gamma = float(_f(hdr_request, "gamma", 1.0))
            component = _component(request)
            # The pair must be built from the *same* picture the caller gets
            # back, exposure included: a component save is metered on its own
            # range, and pairing it with the whole frame's exposure would put
            # the gain map on a different white than the base under it.
            source = render if component == "image" else _ExposedRender(
                _component_image(render, component),
                _component_linear(render, component),
            )
            sdr, high = moonlib.sdr_hdr_pair(source, stops=stops)
            gains = moonlib.gain_map(sdr, high, stops=stops, gamma=gamma)
            geometry["hdr"] = {
                "gain": _data_url(encode_png(gains.data)),
                "metadata": moonlib.gain_map_metadata(gains),
                "width": int(width),
                "height": int(width),
                "stops": stops,
            }
        return _component_image(render, _component(request)), geometry

    # -- close-up -------------------------------------------------------
    def region(
        self, request: Dict[str, Any],
        cancel: Optional[Callable[[], bool]] = None,
    ) -> Tuple[np.ndarray, Dict[str, Any], str, np.ndarray]:
        """Ray-march the selected area, cache its linear radiance, then process it.

        Returns ``(image, info, render_id, raw)``, where *raw* is the same frame
        with no post-processing at all.  The expensive part is the march;
        everything a slider touches happens in :meth:`process`, against the
        cached array.
        """
        when = request.get("when") or {}
        selection = request.get("selection") or {}
        if not selection:
            raise moonlib.MoonlibError(
                "No selection: drag a box on the Moon image first."
            )
        site = parse_observer(when)
        viewpoint = parse_viewpoint(request)
        earth = _earthshine(request.get("earthshine"))
        # The albedo describes the Earth, not the switch: a frame drawn with
        # earthshine off still reports the truth about the sky it was taken in.
        view = self.view(
            when.get("at") or None, site, earth["earth_geometric_albedo"],
            viewpoint,
        )
        train = parse_train(request)
        output = request.get("output") or {}

        width = int(_f(output, "width", 1200))
        height = int(_f(output, "height", width))
        for label, value in (("宽", width), ("高", height)):
            if not MIN_PX <= value <= MAX_OUTPUT_PX:
                raise moonlib.MoonlibError(
                    f"输出{label}必须在 64–{MAX_OUTPUT_PX} px 之间（收到 {value}）。"
                )
        settings = MoonRenderSettings(
            width=width,
            height=height,
            samples_per_pixel=int(_f(output, "samples", 2)),
            shadow_steps=int(_f(output, "shadow_steps", REGION_SHADOW_STEPS)),
            band_workers=_parallel(request.get("parallel")),
            photometric_model=_optical_model(
                _light_option(request, "output", "photometric_model")
            ),
            sun_samples=_sun_samples(_light_option(request, "output", "sun_samples")),
            tone="linear",
            keep_components=True,
            sky_ambient=_sky_ambient(_light_option(request, "preview", "sky_ambient")),
            roughness_deg=_roughness(_light_option(request, "output", "roughness_deg")),
            surface_texture=_surface_texture(
                _light_option(request, "output", "surface_texture")
            ),
            terrain_denoise=_terrain_denoise(
                _light_option(request, "output", "terrain_denoise")
            ),
            **earth,
        )
        started = time.time()
        lon = _f(selection, "lon", 0.0)
        lat = _f(selection, "lat", 0.0)
        rotation = _f(selection, "rotation_deg", 0.0)
        fov = _f(selection, "fov_radius_arcsec", 200.0)
        base_aspect = _f(selection, "aspect", 1.0)
        ellipse = bool(selection.get("ellipse"))
        corrected = bool(selection.get("corrected"))
        ellipse_geometry = None
        correction = None
        # An ellipse is a *ground patch*: radius_on_the_Moon, not a sky angle.
        # Its screen shape foreshortens near the limb, and its centre is kept
        # on the cap where the whole patch is visible -- so it can touch the
        # limb but never be cut by it.
        if ellipse and corrected:
            radius_fraction = _opt_f(selection, "radius_fraction")
            if radius_fraction is None:
                radius_fraction = fov / max(
                    1e-9, 0.5 * float(view.angular_diameter_deg) * 3600.0
                )
            ellipse_geometry = moonlib.selection_ellipse_geometry(
                view, lon, lat,
                radius_fraction=radius_fraction,
                aspect=base_aspect,
                rotation_deg=rotation,
                terrain=self.terrain(),
            )
            lon = ellipse_geometry.lon_deg
            lat = ellipse_geometry.lat_deg
            # The corrected frame is the ground patch itself; the renderer's
            # sky frame has to be large enough to contain its long axis, not
            # only the short one.  Otherwise the orthorectification has no
            # source pixels beyond the sky footprint and clips the patch.
            ground_half = max(
                ellipse_geometry.ground_half_width_km,
                ellipse_geometry.ground_half_height_km,
            )
            fov = max(
                fov,
                math.degrees(math.atan(ground_half / max(view.distance_km, 1.0)))
                * 3600.0 * 1.15,
            )
        # A corrected rectangle describes a different selection from an
        # ellipse; the 3D endpoint still builds a round corrected tile from the
        # same flags.
        if corrected and not ellipse:
            # The page's box is still the ordinary sky rectangle; the corrected
            # frame is the smallest rectangle around its irregular preimage.
            # Both the render and the mask are built from this one answer.
            correction = moonlib.selection_correction(
                view, lon, lat,
                fov_radius_arcsec=fov, aspect=base_aspect,
                rotation_deg=rotation, terrain=self.terrain(),
            )
            fov = correction.fov_radius_arcsec
        with self._render_slot():
            with self._rendering() as frame_cancel:
                render = moonlib.render_region(
                    view,
                    lon,
                    lat,
                    fov_radius_arcsec=fov,
                    rotation_deg=rotation,
                    arcsec_per_pixel=_opt_f(output, "arcsec_per_pixel"),
                    settings=settings,
                    terrain=self.terrain(),
                    cancel=_combined_cancel(frame_cancel, cancel),
                )
        exposure = float(render.stats["exposure_scale"])
        # The total is the sum of the two halves, and the halves are kept as
        # well: 分量出图 exports one of them on its own, and the sum -- which is
        # what every request that does not name a component still gets, bit for
        # bit -- is derived from them here so the two can never drift apart.
        sunlit = np.asarray(render.sunlit, dtype=np.float32) * exposure
        earthshine = (
            np.asarray(render.earthshine, dtype=np.float32) * exposure
            if render.earthshine is not None
            else None
        )
        linear = sunlit if earthshine is None else sunlit + earthshine
        mask_fraction = None
        ellipse_preview = ellipse
        mask = None
        if ellipse_geometry is not None:
            # The corrected ellipse is a patch of ground projected onto the
            # sky; mask that curve, the same one the page draws on the full
            # disk and in the live close-up.
            mask = _projected_ellipse_region_mask(
                view, ellipse_geometry,
                int(render.region.width), int(render.region.height),
                float(render.region.target_elevation_km), self.terrain(),
            )
        elif ellipse_preview:
            # Classic ellipse: the sky-plane shape, allowed to run over the
            # limb exactly like an uncorrected square selection.
            mask = _ellipse_region_mask(
                int(render.region.width), int(render.region.height)
            )
        elif correction is not None:
            # The camera covers the bounding sky box; the irregular selection
            # is the only part that is processed.  Masking the *linear* frame,
            # before it is cached, is what makes every later slider pass and
            # every exported component agree about the black surround.
            mask = _corrected_region_mask(
                view, correction, lon, lat, rotation,
                int(render.region.width), int(render.region.height),
                float(render.region.target_elevation_km), self.terrain(),
            )
        if mask is not None:
            mask_fraction = float(np.mean(mask))
            sunlit = _apply_region_mask(sunlit, mask)
            if earthshine is not None:
                earthshine = _apply_region_mask(earthshine, mask)
            linear = _apply_region_mask(linear, mask)
        orthorectified = (
            ellipse_geometry is not None
            and bool(selection.get("ellipse"))
            and bool(selection.get("corrected"))
        )
        if orthorectified:
            sunlit = _orthorectify_ellipse(
                sunlit, view, render.region, ellipse_geometry, rotation,
                self.terrain(),
            )
            if earthshine is not None:
                earthshine = _orthorectify_ellipse(
                    earthshine, view, render.region, ellipse_geometry, rotation,
                    self.terrain(),
                )
            linear = sunlit if earthshine is None else sunlit + earthshine

        # The counter and the frame it names are written together: two requests
        # arriving at once must not be handed the same render_id, or the page
        # would pass the stale-frame check with somebody else's picture.
        with self._lock:
            self._counter += 1
            render_id = str(self._counter)
            self._linear = linear
            self._linear_sunlit = sunlit
            self._linear_earthshine = earthshine
            self._linear_id = render_id
            self._linear_view = view
            self._linear_terrain = getattr(render, "terrain", None)
            self._linear_meta = {
                "arcsec_per_pixel": float(render.stats["arcsec_per_pixel"]),
                "render_seconds": time.time() - started,
                "exposure_scale": exposure,
                "rotation_deg": float(render.stats.get("rotation_deg") or 0.0),
            }
            # Kept so that /api/process can describe the frame as fully as
            # /api/region does; the page reads the same fields from both.
            region_context = render.region.to_dict()
            # The camera's roll and the observer's direction, for the page's
            # pointer readout.  The disk preview may already be showing a
            # different instant by the time a close-up is on screen, so the
            # close-up has to carry its own geometry rather than borrow it.
            region_context.update({
                "rotation_deg": float(render.stats.get("rotation_deg") or 0.0),
                "sub_observer_lon_deg": float(view.libration_lon_deg),
                "sub_observer_lat_deg": float(view.libration_lat_deg),
                "observer_distance_km": float(view.distance_km),
                "angular_diameter_deg": float(view.angular_diameter_deg),
                "viewpoint": view.viewpoint.value,
                "corrected": bool(corrected),
                "ellipse": bool(selection.get("ellipse")),
                "ellipse_masked": bool(ellipse_preview),
                "orthorectified": bool(orthorectified),
            })
            if correction is not None:
                region_context.update({
                    "corrected_fov_radius_arcsec": float(correction.fov_radius_arcsec),
                    "corrected_aspect": float(correction.aspect),
                    "ground_width_km": float(correction.ground_width_km),
                    "ground_height_km": float(correction.ground_height_km),
                })
            if mask_fraction is not None:
                region_context["mask_fraction"] = float(mask_fraction)
            self._linear_context = {
                "region": region_context,
                "train": train.to_dict(),
            }
        component = _component(request)
        body = {
            "region": render.region.to_dict(),
            "train": train.to_dict(),
            "render_seconds": time.time() - started,
            "earthshine": _earthshine_stats(render.stats),
            "component": component,
        }
        body["region"]["render_seconds"] = time.time() - started
        body["region"]["corrected"] = bool(corrected)
        body["region"]["ellipse"] = bool(selection.get("ellipse"))
        body["region"]["ellipse_masked"] = bool(ellipse_preview)
        body["region"]["orthorectified"] = bool(orthorectified)
        if mask_fraction is not None:
            body["region"]["mask_fraction"] = float(mask_fraction)
        if correction is not None:
            body["region"].update({
                "corrected_fov_radius_arcsec": float(correction.fov_radius_arcsec),
                "corrected_aspect": float(correction.aspect),
                "ground_width_km": float(correction.ground_width_km),
                "ground_height_km": float(correction.ground_height_km),
                "ground_box_km": list(correction.ground_box),
            })
        # Reported the way the disk render reports it, so the pane that says how
        # the frame was produced reads the same fields from both endpoints.
        body["region"]["bands"] = int(render.stats.get("bands", 1))
        body["region"]["band_workers"] = int(render.stats.get("band_workers", 1))
        # The camera's roll: the page needs it to turn a pointer position back
        # into the selenographic coordinates the frame was aimed at.
        body["region"]["rotation_deg"] = float(
            render.stats.get("rotation_deg") or 0.0
        )
        body["region"]["sub_observer_lon_deg"] = float(view.libration_lon_deg)
        body["region"]["sub_observer_lat_deg"] = float(view.libration_lat_deg)
        body["region"]["observer_distance_km"] = float(view.distance_km)
        body["region"]["angular_diameter_deg"] = float(view.angular_diameter_deg)
        body["region"]["viewpoint"] = view.viewpoint.value
        # The page shows two pictures of the same frame: what the geometry
        # produced, and what the processing made of it.  Both come from the one
        # cached array, so producing them together costs one extra tone map --
        # The raw half carries no orientation: a rotation is something done to
        # the photograph, and the geometry pane is the frame the camera saw.
        # Both carry the chosen component: the request picked which radiance
        # this frame is being looked at in.
        raw, _raw_info = self.process(
            {"render_id": render_id, "component": component,
             "processing": self.identity_processing(request)},
            render_id=render_id, train=train,
            arcsec_per_pixel=self._linear_meta["arcsec_per_pixel"],
        )
        image, processing_info = self.process(
            {"render_id": render_id, "component": component, "when": when,
             "telescope": request.get("telescope"),
             "camera": request.get("camera"), "atmosphere": request.get("atmosphere"),
             "processing": request.get("processing")},
            render_id=render_id, train=train,
            arcsec_per_pixel=self._linear_meta["arcsec_per_pixel"],
        )
        body.setdefault("instrument", processing_info["instrument"])
        body.setdefault("image", processing_info["image"])
        return image, body, render_id, raw

    # -- post-processing against the cached render ----------------------
    @staticmethod
    def identity_processing(request: Dict[str, Any]) -> Dict[str, Any]:
        """Processing that changes nothing -- including the orientation.

        The geometry pane is the frame exactly as it was marched.  Rotation and
        mirroring are treatments of the *photograph*: the processed pane gets
        them, the geometry pane keeps the camera's own frame, which is also the
        frame the labels were laid out in.
        """
        return dict(IDENTITY_PROCESSING)

    def _mesh_tile(self, request: Dict[str, Any]):
        """Build the TerrainTile a mesh request describes.

        Kept separate from the JSON and file responses so both export paths
        share exactly the same selection, projection and DEM-window decisions.
        """
        when = request.get("when") or {}
        selection = request.get("selection") or {}
        if not selection:
            raise moonlib.MoonlibError("先在月面上选择一块区域。")
        site = parse_observer(when)
        viewpoint = parse_viewpoint(request)
        view = self.view(
            when.get("at") or None, site, viewpoint=viewpoint
        )
        options = request.get("mesh") or {}
        corrected = bool(selection.get("corrected"))
        round_base = bool(selection.get("ellipse"))
        projection = bool(options.get("projection", True))
        # The page sends a valid fallback number in `resolution` as well as
        # `resolution_mode`: an older server that does not know the auto mode
        # then still exports at that number instead of rejecting the 0 sentinel.
        # This server recomputes the exact DEM-matched count from the mode.
        resolution_mode = str(options.get("resolution_mode") or "").strip().lower()
        resolution = (
            0 if resolution_mode == "auto"
            else int(_f(options, "resolution", 0))
        )
        arguments = dict(
            fov_radius_arcsec=_f(selection, "fov_radius_arcsec", 0.0),
            aspect=_f(selection, "aspect", 1.0),
            rotation_deg=_f(selection, "rotation_deg", 0.0),
            radius_fraction=_opt_f(selection, "radius_fraction"),
            long_side_mm=_f(options, "long_side_mm", 100.0),
            base_depth_km=_f(options, "base_depth_km", 2.0),
            vertical_exaggeration=_f(options, "vertical_exaggeration", 1.0),
            resolution=resolution,
            terrain=self.terrain(),
            projection=projection,
            corrected=corrected,
            round_base=round_base,
        )
        lon = _f(selection, "lon", 0.0)
        lat = _f(selection, "lat", 0.0)
        try:
            tile = moonlib.selection_tile(view, lon, lat, **arguments)
        except moonlib.MoonlibError as exc:
            # A box drawn against the limb can still describe printable ground
            # even when some rays miss the sphere.  The corrected path keeps the
            # valid ground rectangle; retrying that way is better than refusing
            # the export.  A truly far-side target is still rejected.
            if corrected or "past the lunar limb" not in str(exc):
                raise
            arguments["corrected"] = True
            tile = moonlib.selection_tile(view, lon, lat, **arguments)
            options = dict(options)
            options["limb_fallback"] = True
        return tile, options

    def mesh(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Cut the current selection into a printable STL and return it.

        Kept as the JSON/base64 endpoint for API clients and for pages that
        predate the binary file endpoint.  New pages use :meth:`mesh_file`.
        """
        tile, _options = self._mesh_tile(request)
        encoded = base64.b64encode(tile.stl_bytes()).decode("ascii")
        info = tile.as_dict()
        info.update({"format": "stl", "units": "mm"})
        return {
            "info": info,
            "stl": "data:model/stl;base64," + encoded,
        }

    def mesh_file(self, request: Dict[str, Any]):
        """The finished model as binary: STL, or a textured GLB for Blender."""
        tile, options = self._mesh_tile(request)
        model_format = str(options.get("format") or "stl").strip().lower()
        if model_format in ("glb", "gltf", "gltf-binary", "model/gltf-binary"):
            data = tile.glb_bytes()
            info = tile.as_dict()
            info.update({"format": "glb", "units": "m"})
            return data, info, "model/gltf-binary"
        if model_format not in ("stl", "model/stl"):
            raise moonlib.MoonlibError(
                "mesh.format must be 'stl' or 'glb' "
                f"(got {model_format!r})."
            )
        data = tile.stl_bytes()
        info = tile.as_dict()
        info.update({"format": "stl", "units": "mm"})
        return data, info, "model/stl"

    def blender_preview(self, request: Dict[str, Any]):
        """Build the same GLB, import it with Blender, and return a .blend.

        The .blend is self-contained: the mesh is imported (not linked), the
        albedo texture is packed, and the scene carries a studio rig plus the
        infinite-sun lunar views.  This is why the page needs a local Blender
        for this one button; STL and GLB export do not.
        """
        with self._blender_lock:
            blender = _find_blender()
            if blender is None:
                raise moonlib.MoonlibError(
                    "未找到 Blender。安装 Blender，或用 MOONLIB_BLENDER "
                    "环境变量指定可执行文件。"
                )
            tile, options = self._mesh_tile(request)
            info = tile.as_dict()
            blend_light = str(options.get("blend_light") or "phase").strip().lower()
            if blend_light not in ("low", "phase"):
                blend_light = "phase"
            default_scene = "Lunar Terminator" if blend_light == "low" else "Lunar Sun"
            when = request.get("when") or {}
            at = when.get("at") or None
            lon = float(info.get("lon_deg") or 0.0)
            lat = float(info.get("lat_deg") or 0.0)
            has_solar = False
            solar_alt = 25.0
            solar_az = 250.0
            solar_illuminated = False
            solar_when = str(at or "当前")
            try:
                row = moonlib.lunar_surface_solar(lon, lat, at)
                has_solar = True
                solar_alt = float(row.altitude_deg)
                solar_az = float(row.azimuth_deg)
                solar_illuminated = bool(row.illuminated)
                solar_when = row.when_utc.isoformat()
            except Exception:
                # A preview is still useful if the ephemeris is unavailable;
                # the Sun then falls back to the fixed studio direction.
                has_solar = False
            solar_line = (
                f"高度 {solar_alt:.2f}°，方位 {solar_az:.2f}°"
                if has_solar else "未计算（使用默认方向）"
            )
            long_mm = float(info.get("long_side_mm") or 0.0)
            short_mm = float(info.get("short_side_mm") or 0.0)
            note = (
                f"模型：{long_mm:.1f} × {short_mm:.1f} mm\n"
                f"位置：{lat:.2f}°，{lon:.2f}°\n"
                f"时刻：{solar_when}\n"
                f"太阳：{solar_line}\n"
                f"纹理：{'已打包' if info.get('has_texture') else '无'}\n"
                f"数据源：{info.get('terrain_source') or '未知'}\n"
                f"默认场景：{default_scene}\n"
                "场景：Studio / Lunar Sun / Lunar Terminator\n"
            )
            with tempfile.TemporaryDirectory(prefix="moonlib-blender-") as directory:
                root = Path(directory)
                glb_path = root / "model.glb"
                blend_path = root / "preview.blend"
                script_path = root / "build_preview.py"
                glb_path.write_bytes(tile.glb_bytes())
                script_path.write_text(
                    _blender_preview_script(
                        glb_path, blend_path, note,
                        solar_alt, solar_az, has_solar, default_scene,
                    ),
                    encoding="utf-8",
                )
                _run_blender(blender, script_path, blend_path)
                data = blend_path.read_bytes()
            info.update({
                "format": "blend",
                "units": "m",
                "blender": True,
                "blend_light": blend_light,
                "default_scene": default_scene,
                "scenes": ["Studio", "Lunar Sun", "Lunar Terminator"],
                "solar_altitude_deg": solar_alt,
                "solar_azimuth_deg": solar_az,
                "solar_illuminated": solar_illuminated,
                "solar_has_ephemeris": has_solar,
                "solar_when_utc": solar_when,
            })
            return data, info, "application/x-blender"

    def _whole_moon_texture(self, terrain: Any, width: int) -> Optional[np.ndarray]:
        """The albedo mosaic as an equirectangular RGB texture, north at row 0."""
        if getattr(terrain, "albedo", None) is None:
            return None
        width = max(512, min(2048, int(width)))
        height = max(256, width // 2)
        lon = -180.0 + (np.arange(width) + 0.5) / width * 360.0
        lat = 90.0 - (np.arange(height) + 0.5) / height * 180.0
        lon_grid, lat_grid = np.meshgrid(lon, lat)
        colors = terrain.albedo_rgb(lon_grid, lat_grid, 0)
        if colors is None:
            return None
        return np.asarray(colors[..., :3], dtype=np.float32)

    def _moon_globe(self, size_mm: float = 200.0, resolution: int = 1024):
        """Build (and cache) the whole-Moon textured GLB.

        ``resolution`` is the number of mesh samples around the equator; the
        texture width is twice that (capped at 2048).  The globe is a display
        model: its radius is ``size_mm / 2000`` metres.
        """
        size_mm = max(5.0, min(2000.0, float(size_mm)))
        resolution = max(256, min(2048, int(resolution)))
        with self._globe_lock:
            terrain = self.terrain()
            key = (id(terrain), round(size_mm, 3), resolution)
            if self._globe_cache and self._globe_cache[0] == key:
                data, info = self._globe_cache[1], self._globe_cache[2]
                return data, dict(info)
            try:
                width = int(terrain.dem.grid_shape(2)[1])
            except Exception:
                width = 11520
            step = max(1, int(round(width / float(resolution))))
            mesh = terrain.build_mesh(level=2, step=step, with_albedo=False)
            texture = self._whole_moon_texture(terrain, resolution * 2)
            radius_m = size_mm / 2000.0
            data = mesh.glb_bytes(texture, radius_m=radius_m, name="Moon")
            sources = getattr(terrain, "sources", None)
            source = ""
            if sources is not None and getattr(sources, "dem_path", None):
                source = os.path.basename(str(sources.dem_path))
            info = {
                "format": "glb",
                "units": "m",
                "scope": "moon",
                "diameter_m": size_mm / 1000.0,
                "diameter_mm": size_mm,
                "diameter_km": 3474.8,
                "radius_m": radius_m,
                "resolution": resolution,
                "n_vertices": int(mesh.n_vertices),
                "n_faces": int(mesh.n_faces),
                "has_texture": texture is not None,
                "texture_size": (
                    [int(texture.shape[1]), int(texture.shape[0])]
                    if texture is not None else [2, 2]
                ),
                "glb_bytes": len(data),
                "terrain_source": source,
            }
            self._globe_cache = (key, data, info)
            return data, dict(info)

    def whole_moon_glb(self, request: Dict[str, Any]):
        moon = request.get("moon") or {}
        data, info = self._moon_globe(
            _f(moon, "size_mm", 200.0),
            int(_f(moon, "resolution", 1024.0)),
        )
        return data, info, "model/gltf-binary"

    def whole_moon_blender(self, request: Dict[str, Any]):
        """A textured globe .blend with the current-phase scene."""
        with self._blender_lock:
            blender = _find_blender()
            if blender is None:
                raise moonlib.MoonlibError(
                    "未找到 Blender。安装 Blender，或用 MOONLIB_BLENDER "
                    "环境变量指定可执行文件。"
                )
            moon_options = request.get("moon") or {}
            size_mm = _f(moon_options, "size_mm", 200.0)
            resolution = int(_f(moon_options, "resolution", 1024.0))
            data, base_info = self._moon_globe(size_mm, resolution)
            when = request.get("when") or {}
            at = when.get("at") or None
            site = parse_observer(when)
            viewpoint = parse_viewpoint(request)
            view = self.view(at, site, viewpoint=viewpoint)
            sub_observer_lon = float(view.sub_observer_lon_deg)
            sub_observer_lat = float(view.sub_observer_lat_deg)
            solar_ok = False
            subsolar_lon = 0.0
            subsolar_lat = 0.0
            try:
                solar = moonlib.lunar_surface_solar(0.0, 0.0, at)
                subsolar_lon = float(solar.subsolar_lon_deg)
                subsolar_lat = float(solar.subsolar_lat_deg)
                solar_ok = True
            except Exception:
                solar_ok = False
            note = (
                f"全月面：直径 {size_mm:.1f} mm（显示比例，实际 3,474.8 km）\n"
                f"分辨率：{resolution}（赤道采样数）\n"
                f"太阳下点：{subsolar_lon:.2f}°，{subsolar_lat:.2f}°\n"
                f"相机下点：{sub_observer_lon:.2f}°，{sub_observer_lat:.2f}°\n"
                "场景：Current phase\n"
                f"纹理：{'已打包' if base_info.get('has_texture') else '无'}\n"
            )
            with tempfile.TemporaryDirectory(prefix="moonlib-globe-") as directory:
                root = Path(directory)
                glb_path = root / "moon.glb"
                blend_path = root / "moon.blend"
                script_path = root / "build_globe_preview.py"
                glb_path.write_bytes(data)
                script_path.write_text(
                    _blender_globe_script(
                        glb_path, blend_path, note,
                        sub_observer_lon, sub_observer_lat,
                        subsolar_lon, subsolar_lat,
                    ),
                    encoding="utf-8",
                )
                _run_blender(blender, script_path, blend_path)
                blend = blend_path.read_bytes()
            info = dict(base_info)
            info.update({
                "format": "blend",
                "units": "m",
                "scope": "moon",
                "blender": True,
                "scenes": ["Current phase"],
                "subsolar_lon_deg": subsolar_lon,
                "subsolar_lat_deg": subsolar_lat,
                "sub_observer_lon_deg": sub_observer_lon,
                "sub_observer_lat_deg": sub_observer_lat,
                "solar_has_ephemeris": solar_ok,
            })
            return blend, info, "application/x-blender"

    def process(
        self,
        request: Dict[str, Any],
        *,
        render_id: Optional[str] = None,
        train: Any = None,
        arcsec_per_pixel: Optional[float] = None,
    ) -> Tuple[np.ndarray, Dict[str, Any]]:
        """Apply blur, sharpening and the level controls to the cached frame.

        Everything here is an operation on an array that already exists, so it
        costs milliseconds and can run on every slider move.  A request may name
        one *component* instead of the whole picture; that is the same frame and
        the same treatment, metered on the chosen half's own range (see
        :func:`_component_exposure`), so it is still a slider-speed operation.
        """
        with self._lock:
            if self._linear is None:
                raise moonlib.MoonlibError(
                    "No rendered frame to process yet; press 生成模拟 first."
                )
            requested = render_id or request.get("render_id")
            if requested and self._linear_id and str(requested) != self._linear_id:
                raise StaleRender(
                    f"That frame is no longer the cached one (have "
                    f"{self._linear_id}, asked for {requested}); render again."
                )
            component = _component(request)
            linear = self._linear
            # The cached arrays are already exposed for the scene, which is what
            # the sunlit surface is metered on; a component is exposed for
            # *itself* on top of that.  ``image`` is the case the page has always
            # posted, and it goes through this call with the arithmetic it
            # always did -- one multiply by exactly 1.0.
            component_exposure = 1.0
            if component != "image":
                linear = (self._linear_sunlit if component == "sunlit"
                          else self._linear_earthshine)
                if linear is None:  # pragma: no cover - both are kept together
                    raise moonlib.MoonlibError(
                        f"这一帧没有保留 {component} 分量，请重新生成模拟。"
                    )
                component_exposure = _component_exposure(linear)
            linear_id = self._linear_id
            meta = dict(self._linear_meta)
            context = dict(self._linear_context)
        if train is None:
            train = parse_train(request)
        if arcsec_per_pixel is None:
            arcsec_per_pixel = float(meta.get("arcsec_per_pixel") or 1.0)

        processing = request.get("processing") or {}
        atmosphere = request.get("atmosphere") or {}
        started = time.time()
        rotation = _f(processing, "rotation_deg", 0.0)
        mirror = str(processing.get("mirror") or "none")
        photons = _opt_f(processing, "noise_photons")
        sigma = _f(processing, "noise_sigma", 0.0)
        sharpen = _sharpen_radius(processing, arcsec_per_pixel)
        image = moonlib.process_photograph(
            linear,
            None,
            train,
            exposure_scale=component_exposure,
            arcsec_per_pixel=arcsec_per_pixel,
            seeing_fwhm_arcsec=_f(atmosphere, "seeing", 0.8),
            frame_selection_gain=_f(atmosphere, "frame_selection_gain", 1.0),
            blur_strength=_f(processing, "blur_strength", 1.0),
            diffraction=_f(processing, "diffraction", 0.0),
            sharpen_strength=_f(processing, "sharpen_strength", 1.0),
            saturation=_f(processing, "saturation", 1.0),
            gain=_f(processing, "gain", 1.0),
            offset=_f(processing, "offset", 0.0),
            gamma=_f(processing, "gamma", 1.0),
            rotation_deg=rotation,
            mirror=mirror,
            **sharpen,
            **(_processing_extras(processing)),
            noise_photons=photons,
            noise_sigma=sigma,
            noise_seed=noise_seed(linear_id, photons, sigma),
        )
        # The report has to describe the frame that was just made, so it is
        # handed the same radius the mask was built with rather than the
        # multiple it would otherwise re-derive.
        report = moonlib.instrument_report(
            train.telescope,
            arcsec_per_pixel=arcsec_per_pixel,
            seeing_fwhm_arcsec=_f(atmosphere, "seeing", 0.8),
            frame_selection_gain=_f(atmosphere, "frame_selection_gain", 1.0),
            sharpen_radius_factor=sharpen["sharpen_radius_factor"],
            mask_radius_arcsec=sharpen["sharpen_radius_arcsec"],
        )
        info = {
            # The region is a property of the *frame* (from the cache); the train
            # is a property of this call, and reporting the cached one made the
            # readout disagree with the pixels as soon as the telescope changed.
            "region": context.get("region"),
            "train": train.to_dict(),
            "component": component,
            "instrument": report,
            "image": {
                "shape": [int(image.shape[0]), int(image.shape[1])],
                "min": float(image.min()),
                "max": float(image.max()),
                "mean": float(image.mean()),
            },
            "process_seconds": time.time() - started,
            "render_id": linear_id,
        }
        return image, info

    def events(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Forecast the events of one kind inside one time range.

        One kind per request: the page shows them one at a time (the switch is
        single-choice), and the highest-moon searches are the expensive part.
        """
        when = request.get("when") or {}
        site = parse_observer(when)
        kind = str(request.get("kind") or "full_moon")
        start = str(request.get("start") or "")
        end = str(request.get("end") or "")
        if not start or not end:
            raise moonlib.MoonlibError("事件预报需要开始与结束时间。")
        # The two old highest-moon names are accepted as aliases: a saved CSV
        # or a settings file from 1.4.1 should still work after the merge.
        known = set(moonlib.EVENT_KINDS) | {"highest_full", "highest_quarter"}
        if kind not in known:
            raise moonlib.MoonlibError(
                f"未知的事件类型 {kind!r}；可选：{', '.join(moonlib.EVENT_KINDS)}。"
            )
        found = moonlib.moon_events(site, start, end, kinds=[kind])
        return {
            "kind": kind,
            "start": start,
            "end": end,
            "observer": {"lat": site.lat, "lon": site.lon},
            "events": [event.to_dict() for event in found],
        }

    def crescents(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """"极低照明度": the thinnest crescent sessions of a period.

        The library already knows when a crescent is observable; this reports
        the *extreme* ones -- within two nights of the new moon, thinnest
        first -- with everything a reader needs to plan the shot: which night,
        how much of the disc is lit, the elongation and the Moon's altitude at
        the best moment, and the range of the session it belongs to.
        """
        when = request.get("when") or {}
        site = parse_observer(when)
        start = str(request.get("start") or "")
        end = str(request.get("end") or "")
        if not start or not end:
            raise moonlib.MoonlibError("极低照明度需要开始与结束时间。")
        limit = int(_f(request, "limit", 15))
        if not 1 <= limit <= 15:
            raise moonlib.MoonlibError(f"最多显示 15 条（收到 {limit}）。")
        nights = str(request.get("nights") or "both")
        rows = moonlib.extreme_crescents(
            site, (start, end), nights=nights, days_around_new_moon=1, limit=limit
        )
        items = [
            {
                "local_date": row["local_date"],
                "night": row["night"],
                "session": row["session"],
                "best_utc": row["best_utc"],
                "best_local": row["best_local"],
                "window_start_utc": row["window_start_utc"],
                "window_end_utc": row["window_end_utc"],
                "illumination_percent": round(float(row["illumination_percent"]), 3),
                "elongation_deg": round(float(row["elongation_deg"]), 2),
                "moon_elevation_deg": round(float(row["moon_elevation_deg"]), 2),
                "sun_elevation_deg": round(float(row["sun_elevation_deg"]), 2),
                "age_days": round(float(row["age_days"]), 3),
                "nights_from_new_moon": int(row["nights_from_new_moon"]),
                "photo_score": round(float(row["photo_score"]), 3),
                "yallop": (row.get("visibility") or {}).get("yallop", {}).get("verdict"),
                # The table's later columns are measured at the best moment,
                # exactly like an event row; the geometry above is already
                # that moment, so this only fills in the missing quantities.
                "metrics": event_metrics(site, row["best_utc"]),
            }
            for row in rows
        ]
        return {
            "start": start,
            "end": end,
            "observer": {"lat": site.lat, "lon": site.lon},
            "nights": nights,
            "limit": limit,
            "items": items,
        }

    # -- 区域补片 ---------------------------------------------------------
    def patch_download(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch a high-resolution patch covering the current selection.

        This is the answer to "the global product is 59 m at best, and I am
        looking at one crater": a few hundred megabytes of the finer product,
        clipped to the frame the reader is actually looking at, instead of tens
        of gigabytes of the whole Moon.  The download runs in the background --
        a two-degree patch of SLDEM2015 is a minute of HTTP ranges -- and the
        page polls :meth:`patch_status`.
        """
        from moonlib import online

        product = str(request.get("product") or "")
        if product not in {item["id"] for item in online.catalogue()}:
            raise moonlib.MoonlibError(
                f"未知的在线数据源：{product!r}；可选 "
                f"{', '.join(item['id'] for item in online.catalogue())}"
            )
        selection = request.get("selection") or {}
        lon = _f(selection, "lon", 0.0)
        lat = _f(selection, "lat", 51.0)
        fov = _f(selection, "fov_radius_arcsec", 30.0)
        if not (0.5 <= fov <= 3600.0):
            raise moonlib.MoonlibError(
                f"选框视场半径要在 0.5–3600″ 之间（收到 {fov}）。"
            )
        output = request.get("output") or {}
        width = int(_f(output, "width", 600))
        height = int(_f(output, "height", width))
        when = request.get("when") or {}
        site = parse_observer(when)
        view = self.view(
            when.get("at") or None, site, viewpoint=parse_viewpoint(request)
        )

        footprint = moonlib.selection_footprint(
            view, lon, lat, fov_radius_arcsec=fov, width=width, height=height,
            rotation_deg=_f(selection, "rotation_deg", 0.0),
            terrain=self.terrain(),
        )
        if footprint is None:  # pragma: no cover - the frame misses the disk
            raise moonlib.MoonlibError("选框没有落在这张月面上。")
        bounds = online.patch_bounds_for(
            footprint["lon_min"], footprint["lon_max"],
            footprint["lat_min"], footprint["lat_max"],
            margin_deg=moonlib.moonview.WINDOW_MARGIN_DEG,
        )
        product_info = online.CATALOGUE[product]
        ppd = product_info["pixels_per_degree"]
        itemsize = int(np.dtype(product_info["dtype"]).itemsize)
        megabytes = (ppd ** 2 * (bounds[1] - bounds[0]) * (bounds[3] - bounds[2])
                     * itemsize / 1e6)
        lon_tag = f"{'e' if lon >= 0 else 'w'}{abs(lon):.2f}".replace(".", "p")
        lat_tag = f"{'n' if lat >= 0 else 's'}{abs(lat):.2f}".replace(".", "p")
        name = f"{product}_{lon_tag}_{lat_tag}"
        destination = moonlib.terrain.resolve_cache_dir() / "regions" / (name + ".npy")

        with self._patch_lock:
            self._patch_seq += 1
            job_id = str(self._patch_seq)
            self._patches[job_id] = {
                "id": job_id,
                "state": "running",
                "product": product,
                "label": product_info["label"],
                "name": name,
                "km_per_px": float(product_info["metres_per_px"]) / 1000.0,
                "progress": 0.0,
                "message": "准备中",
                "bounds": [round(v, 4) for v in bounds],
                "bytes": None,
                "error": None,
            }
            job = self._patches[job_id]
        thread = threading.Thread(
            target=self._run_patch, args=(job, product, bounds, destination, name),
            daemon=True,
        )
        thread.start()
        return {"id": job_id, "product": product, "name": name,
                "label": product_info["label"],
                "km_per_px": float(product_info["metres_per_px"]) / 1000.0,
                "bounds": job["bounds"], "megabytes": round(megabytes, 1)}

    def _run_patch(self, job: Dict[str, Any], product: str, bounds: tuple,
                   destination: Path, name: str) -> None:
        from moonlib import online

        def progress(fraction: float, message: str) -> None:
            with self._patch_lock:
                job["progress"] = float(fraction)
                job["message"] = message

        try:
            sidecar = online.fetch_patch(
                product, bounds[0], bounds[1], bounds[2], bounds[3],
                destination, progress=progress, name=name,
            )
            with self._patch_lock:
                job.update(state="done", progress=1.0,
                           message=f"{sidecar['shape'][1]}×{sidecar['shape'][0]} px",
                           bytes=int(destination.stat().st_size),
                           path=str(destination), bounds=sidecar["bounds"])
        except Exception as error:  # pragma: no cover - surfaced to the page
            with self._patch_lock:
                job.update(state="failed", error=str(error),
                           message=f"下载失败：{error}")

    def patch_status(self, job_id: str) -> Dict[str, Any]:
        with self._patch_lock:
            job = self._patches.get(job_id)
            if job is None:
                raise moonlib.MoonlibError(f"没有这个下载任务：{job_id}")
            return dict(job)

    # -- animation --------------------------------------------------------
    def animation(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Start an MP4 animation in the background and hand back its id.

        A sequence of renders is minutes of work, so the request only *starts*
        it; the page then polls :meth:`animation_status` and fetches the file
        when it is done.  The job holds the render slot for its whole life, so
        it is one ray march at a time like everything else -- and one cancel
        flag, so 终止 stops the frame being traced as well as the ones after it.
        """
        when = request.get("when") or {}
        site = parse_observer(when)
        viewpoint = parse_viewpoint(request)
        start = str(request.get("start") or "")
        end = str(request.get("end") or "")
        if not start or not end:
            raise moonlib.MoonlibError("动画需要开始与结束时间。")
        step = _f(request, "step_seconds", 60.0)
        if not step > 0.0:
            raise moonlib.MoonlibError("时间步长必须大于 0 秒。")
        span = moonlib.to_et(end) - moonlib.to_et(start)
        if span < 0.0:
            raise moonlib.MoonlibError("动画的结束时间早于开始时间。")
        total = int(math.floor(span / step + 1e-9)) + 1
        if total > ANIMATION_MAX_FRAMES:
            raise moonlib.MoonlibError(
                f"这一动画有 {total} 帧，超过上限 {ANIMATION_MAX_FRAMES} 帧；"
                "把时间步长调大，或把起止范围缩短。"
            )

        output = request.get("output") or {}
        width = int(_f(output, "width", 1000))
        height = int(_f(output, "height", width))
        for side, value in (("宽", width), ("高", height)):
            if not MIN_PX <= value <= MAX_DISK_PX:
                raise moonlib.MoonlibError(
                    f"动画分辨率{side}必须在 {MIN_PX}–{MAX_DISK_PX} px 之间"
                    f"（收到 {value}）。"
                )
        earth = _earthshine(request.get("earthshine"))
        # Read before the settings: which kind of frame this is decides the
        # shadow step budget (see below).
        region = request.get("region")
        if region is not None and not isinstance(region, dict):
            raise moonlib.MoonlibError("region must be an object when it is given.")
        settings = MoonRenderSettings(
            width=width,
            height=height,
            samples_per_pixel=int(_f(output, "samples", 1)),
            shadow_steps=_animation_shadow_steps(output, region),
            band_workers=_parallel(request.get("parallel")),
            photometric_model=_optical_model(
                _light_option(request, "output", "photometric_model")
            ),
            sun_samples=_sun_samples(_light_option(request, "output", "sun_samples")),
            tone="srgb",
            keep_components=True,
            sky_ambient=_sky_ambient(_light_option(request, "preview", "sky_ambient")),
            roughness_deg=_roughness(_light_option(request, "output", "roughness_deg")),
            surface_texture=_surface_texture(
                _light_option(request, "output", "surface_texture")
            ),
            terrain_denoise=_terrain_denoise(
                _light_option(request, "output", "terrain_denoise")
            ),
            **earth,
        )
        # The file's size is a separate question from the render's: frames can
        # be rendered at the scale the physics resolves and written at the size
        # the reader asked for.  One number for a square frame, a pair for one
        # whose shape is the selection's own; nothing is resampled when absent.
        scale_to = _animation_size(request.get("scale_to"))
        if scale_to is not None:
            sides = scale_to if isinstance(scale_to, list) else [scale_to]
            if not all(MIN_PX <= side <= MAX_DISK_PX for side in sides):
                raise moonlib.MoonlibError(
                    "动画输出分辨率必须在 "
                    f"{MIN_PX}–{MAX_DISK_PX} px 之间（收到 {scale_to}）。"
                )
        smooth_px = _f(request, "smooth_px", 0.0)
        if smooth_px < 0.0:
            raise moonlib.MoonlibError("动画的平滑半径不能是负数。")
        timestamp = _animation_timestamp(request.get("timestamp"))
        hdr = request.get("hdr") or {}
        fps = _f(request, "fps", 12.0)
        if not fps > 0.0:
            raise moonlib.MoonlibError("帧率必须大于 0。")
        apply_processing = bool(request.get("apply_processing"))
        if apply_processing and bool(hdr.get("on")):
            raise moonlib.MoonlibError(
                "「应用后期处理」与 HDR 不能同时开：后期处理链的终点是显示图，"
                "没有线性帧可以写成 HDR。"
            )
        annotations = _animation_annotations(request.get("annotations"))
        if annotations.get("on") and bool(hdr.get("on")):
            raise moonlib.MoonlibError(
                "「地貌」与 HDR 不能同时开：标注是 sRGB 的图形，不是线性光，"
                "写不进 HDR 视频。"
            )
        processing = dict(request.get("processing") or {})
        atmosphere = dict(request.get("atmosphere") or {})
        train = parse_train(request) if apply_processing else None

        # 单帧预览: the same request, one frame out of it instead of a job.  It
        # is answered here rather than by a second endpoint so the preview and
        # the file go through one parse -- a preview that accepted parameters
        # the run would reject (or the other way round) is worse than none.
        if bool(request.get("preview")):
            return self._animation_preview(
                site, start, end, step, settings, region, train, atmosphere,
                processing, smooth_px, timestamp, annotations, scale_to,
                viewpoint,
            )

        # The cancel flag is made *here*, before the thread exists: 终止 pressed
        # the instant after 开始动画 has to reach this job, and a flag created
        # inside the worker would leave that press pointing at the frame before
        # it.  The render slot is taken here too, so "one ray march at a time"
        # is decided by whoever asks, not by the order two threads wake up in.
        self._take_render_slot()
        cancel = threading.Event()
        finish = threading.Event()
        try:
            with self._animation_lock:
                self._cancel_event = cancel
                self._animation_cancel = cancel
                self._animation_seq += 1
                job_id = str(self._animation_seq)
                self._animations[job_id] = {
                    "id": job_id,
                    "state": "running",
                    "frames": 0,
                    "total": int(total),
                    "progress": 0.0,
                    "message": "准备中",
                    "path": None,
                    "error": None,
                    "seconds": 0.0,
                    "fps": float(fps),
                    "hdr": bool(hdr.get("on")),
                    "finish": finish,
                    "partial": False,
                }
                self._purge_animations()
            thread = threading.Thread(
                target=self._run_animation,
                args=(job_id, site, start, end, step, settings, fps,
                      bool(hdr.get("on")), float(_f(hdr, "stops", 3.0)),
                      region, cancel, finish, scale_to, train, atmosphere,
                      processing, smooth_px, timestamp, annotations, viewpoint),
                daemon=True,
            )
            thread.start()
        except Exception:
            with self._animation_lock:
                if self._animation_cancel is cancel:
                    self._animation_cancel = None
            self._render_lock.release()
            raise
        return {"id": job_id, "state": "running", "total": int(total)}

    def _animation_preview(
        self, site: Any, start: str, end: str, step: float, settings: Any,
        region: Optional[Dict[str, Any]], train: Any, atmosphere: Dict[str, Any],
        processing: Dict[str, Any], smooth_px: float,
        timestamp: Optional[Dict[str, Any]], annotations: Dict[str, Any],
        scale_to: Optional[Any], viewpoint: str = "earth",
    ) -> Dict[str, Any]:
        """The run's first frame, finished exactly the way the file's would be.

        The whole range is handed to the generator and only the first frame is
        taken, because that is what makes the preview the *video's* first frame:
        with 当前选框 the run cuts one window covering every frame, so a preview
        that asked for "just this instant" could sample a different window than
        the file will.

        HDR is not previewed as HDR -- a PNG cannot carry PQ -- so the reader
        sees the SDR tone map the still viewer would show.
        """
        started = time.time()
        frames = moonlib.animation_frames(
            site, start, end, step, settings=settings, region=region,
            terrain=self.terrain(), graceful=True, viewpoint=viewpoint,
        )
        with self._render_slot():
            frame = next(iter(frames))
        processor = (
            _frame_processor(train, atmosphere or {}, processing or {}, "preview")
            if train is not None else None
        )
        painter = (
            _AnimationLabelPainter(self, annotations or {}, settings, region,
                                   processing if train is not None else None)
            if annotations and annotations.get("on") else None
        )
        pixels = processor(frame) if processor is not None else None
        image = pixels if pixels is not None else frame.render.image
        from moonlib.anim import finish_frame

        finished = finish_frame(
            _frame_carrier(frame, image),
            scale_to=scale_to, smooth_px=smooth_px, timestamp=timestamp,
            annotate=painter,
        )
        array = np.asarray(finished)
        if array.dtype == np.uint8:
            array = array.astype(np.float32) / 255.0
        return {
            "preview": {
                "image": _data_url(encode_png(array)),
                "width": int(array.shape[1]),
                "height": int(array.shape[0]),
                "when_utc": str(frame.when_utc),
                "seconds": time.time() - started,
            }
        }

    def _animation_dir(self) -> Path:
        directory = moonlib.cache_dir() / "animations"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _purge_animations(self) -> None:
        """Keep the last few jobs, and delete the files of the ones dropped.

        Called with the lock held, from the request that starts a new job: the
        only moment at which the table is allowed to grow.
        """
        finished = [
            job for job in self._animations.values()
            if job["state"] in ("done", "failed", "cancelled")
        ]
        finished.sort(key=lambda job: int(job["id"]))
        dropped = finished[:-ANIMATION_KEEP] if len(finished) > ANIMATION_KEEP else []
        for job in dropped:
            path = job.get("path")
            if path:
                try:
                    Path(path).unlink()
                except OSError:  # pragma: no cover - already gone
                    pass
            self._animations.pop(job["id"], None)

    def _run_animation(self, job_id: str, site: Any, start: str, end: str,
                       step: float, settings: Any, fps: float, hdr: bool,
                       hdr_stops: float, region: Optional[Dict[str, Any]],
                       cancel: threading.Event, finish: threading.Event,
                       scale_to: Optional[Any] = None,
                       train: Any = None, atmosphere: Optional[Dict[str, Any]] = None,
                       processing: Optional[Dict[str, Any]] = None,
                       smooth_px: float = 0.0,
                       timestamp: Optional[Dict[str, Any]] = None,
                       annotations: Optional[Dict[str, Any]] = None,
                       viewpoint: str = "earth") -> None:
        """Render every frame and encode them, off the request thread.

        Owns the render slot and the cancel flag that :meth:`animation` took
        before it started this thread, and gives both back when it is done.
        """
        path = self._animation_dir() / f"moon-{job_id}.mp4"
        try:
            frames = moonlib.animation_frames(
                site, start, end, step, settings=settings, region=region,
                terrain=self.terrain(),
                # 立即保存 and 终止 share the same generator, but only the hard
                # flag may interrupt a frame: a partial file full of half-traced
                # frames is worse than waiting one frame for the encoder.
                cancel=lambda: bool(cancel.is_set() or finish.is_set()),
                graceful=True,
                render_cancel=cancel.is_set,
                viewpoint=viewpoint,
            )

            processor = (
                _frame_processor(train, atmosphere or {}, processing or {}, job_id)
                if train is not None else None
            )
            painter = (
                _AnimationLabelPainter(self, annotations or {}, settings, region,
                                       processing if train is not None else None)
                if annotations and annotations.get("on") else None
            )

            def counted():
                for frame in frames:
                    with self._animation_lock:
                        job = self._animations.get(job_id)
                        if job is not None:
                            job["frames"] += 1
                            job["message"] = frame.when_utc
                            job["progress"] = job["frames"] / max(1, job["total"])
                    pixels = processor(frame) if processor is not None else None
                    if pixels is None and painter is None:
                        # Untouched frame: hand the encoder the real thing, so
                        # HDR keeps its linear components and everything else
                        # keeps the instant.
                        yield frame
                        continue
                    # Anything that replaced or drew into the pixels still has to
                    # carry the frame's camera and instant: 后期处理 replaces the
                    # image, the painter needs the view, and a timestamp needs
                    # `when_utc` -- yielding a bare array here is what made
                    # 「含时间戳」 fail whenever it was the only per-frame option
                    # on.  The carrier is the shape `anim._display_frame` reads.
                    image = pixels if pixels is not None else frame.render.image
                    yield _frame_carrier(frame, image)

            info = moonlib.write_animation(path, counted(), fps=fps, hdr=hdr,
                                           stops=hdr_stops, scale_to=scale_to,
                                           smooth_px=smooth_px, timestamp=timestamp,
                                           annotate=painter)
            if cancel.is_set():
                # 终止 arrived while the encoder was finishing: the file it just
                # wrote is not what the reader asked for.
                try:
                    path.unlink()
                except OSError:  # pragma: no cover - already gone
                    pass
                with self._animation_lock:
                    job = self._animations.get(job_id)
                    if job is not None:
                        job.update(state="cancelled", message="已终止")
                return
            with self._animation_lock:
                job = self._animations.get(job_id)
                if job is not None:
                    partial = (bool(finish.is_set())
                               and int(job.get("frames", 0)) < int(job.get("total", 0)))
                    job.update(
                        state="done", progress=1.0,
                        message="已保存已生成的帧" if partial else "完成",
                        partial=partial,
                        path=str(path), seconds=float(info.get("seconds", 0.0)),
                        bytes=int(path.stat().st_size) if path.is_file() else 0,
                    )
        except moonlib.MoonlibError as error:
            cancelled = "cancel" in str(error).lower()
            try:
                path.unlink()
            except OSError:  # pragma: no cover - nothing was written
                pass
            with self._animation_lock:
                job = self._animations.get(job_id)
                if job is not None:
                    job.update(
                        state="cancelled" if cancelled else "failed",
                        message=str(error), error=None if cancelled else str(error),
                    )
        except Exception as error:  # pragma: no cover - surfaced to the page
            traceback.print_exc()
            with self._animation_lock:
                job = self._animations.get(job_id)
                if job is not None:
                    job.update(state="failed", message=str(error), error=str(error))
        finally:
            with self._animation_lock:
                if self._animation_cancel is cancel:
                    self._animation_cancel = None
            self._render_lock.release()

    def animation_status(self, job_id: str) -> Dict[str, Any]:
        with self._animation_lock:
            job = self._animations.get(job_id)
            if job is None:
                raise moonlib.MoonlibError(f"没有这个动画任务：{job_id}")
            payload = dict(job)
        payload["url"] = f"/api/animation/{job_id}/file" if payload["state"] == "done" else None
        payload.pop("path", None)
        # A threading.Event is not JSON, and the page has no use for it.
        payload.pop("finish", None)
        return payload

    def finish_animation(self, job_id: str) -> Dict[str, Any]:
        """Stop after the frame being traced and encode what is already there.

        This is 立即保存, not 终止: the cancel flag interrupts a frame and the
        file is deleted, while this flag is read between frames and the encoder
        keeps everything it was given.  It is safe to press before the first
        frame lands -- the generator always lets at least one frame through.
        """
        with self._animation_lock:
            job = self._animations.get(job_id)
            if job is None:
                raise moonlib.MoonlibError(f"没有这个动画任务：{job_id}")
            if job["state"] != "running":
                raise moonlib.MoonlibError(
                    f"动画 {job_id} 已经结束（{job['state']}），不能立即保存。"
                )
            event = job.get("finish")
            if event is None:  # pragma: no cover - older jobs before this field
                raise moonlib.MoonlibError("这个动画任务不支持立即保存。")
            event.set()
            job["message"] = "正在保存已生成的帧"
        return {"id": job_id, "state": "running", "saving": True}

    def animation_file(self, job_id: str) -> Path:
        with self._animation_lock:
            job = self._animations.get(job_id)
            if job is None or job["state"] != "done" or not job.get("path"):
                raise moonlib.MoonlibError(f"动画 {job_id} 还没有准备好。")
            path = Path(job["path"])
        if not path.is_file():  # pragma: no cover - purged under our feet
            raise moonlib.MoonlibError(f"动画 {job_id} 的文件已经不在了。")
        return path

    # -- HDR gain-map JPEG ------------------------------------------------
    def hdr_jpeg(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Wrap a finished SDR picture in a gain-map JPEG (ISO 21496-1).

        The page composes the base -- its landform names, the eyepiece, the
        centring -- and the render supplied the gain map.  This is the
        *packaging* step, and it belongs on this side because a gain map is a
        second JPEG spliced inside the first, with an MPF index and an XMP
        block, and no canvas can write one.
        """
        base = _decode_data_url(request.get("base"), "RGB", "the HDR base image")
        gains = _decode_data_url(request.get("gain"), "L", "the HDR gain map")
        metadata = request.get("metadata") or {}
        shift = request.get("shift") or [0, 0]
        sdr = np.asarray(base, dtype=np.float32) / 255.0
        data = np.asarray(gains, dtype=np.float32) / 255.0
        data = _shift_gain(data, int(shift[0]), int(shift[1]))
        if data.shape[:2] != sdr.shape[:2]:
            data = _resize_gain(data, sdr.shape[:2])
        gain = _gain_map_from_metadata(data, metadata)
        quality = int(_f(request, "quality", 95))
        with tempfile.TemporaryDirectory(prefix="moonlib-hdr-") as directory:
            path = Path(directory) / "hdr.jpg"
            info = moonlib.write_gain_map_jpeg(
                path, sdr, gain, quality=quality, metadata=metadata
            )
            body = path.read_bytes()
        encoded = base64.b64encode(body).decode("ascii")
        return {"image": "data:image/jpeg;base64," + encoded, "hdr": info}

    # -- settings files --------------------------------------------------
    def settings_save(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Write one page of parameters under a user-chosen name."""
        from datetime import datetime, timezone

        payload = {
            "schema": "moonlib-web-settings/1",
            "saved_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "settings": request.get("settings"),
        }
        written = self.settings.write(request.get("name"), payload)
        return {"saved": written, "files": self.settings.list()["files"]}

    # -- names on the frame the page is looking at ----------------------
    def annotations(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Project the named-landform catalogue into one frame.

        There is no recognition anywhere in this path: the frame's own camera
        says where each name belongs, which is the whole advantage of labelling
        a simulation rather than a photograph.
        """
        frame_name = str(request.get("frame") or "disk")
        language = str(request.get("language") or DEFAULT_LANGUAGE)
        if language not in moonlib.LANGUAGES:
            raise moonlib.MoonlibError(
                f"language must be one of {', '.join(moonlib.LANGUAGES)}; "
                f"got {language!r}."
            )
        classes = request.get("classes")
        if classes is not None:
            if not isinstance(classes, (list, tuple)):
                raise moonlib.MoonlibError("classes must be a list of class keys.")
            classes = [str(key) for key in classes]
        satellite = str(request.get("satellite") or "full")
        if satellite not in ("full", "letter"):
            satellite = "full"
        related = request.get("related")
        related = str(related).strip() if related not in (None, "") else None
        # A region caption is set across the formation it names (a mare's name
        # on the mare) instead of beside it with a leader; obstacles are the
        # rectangles the page has already drawn -- the movable scale bar -- that
        # no name may be set on top of.
        region = bool(request.get("region", False))
        obstacles = _obstacle_rects(request.get("obstacles"))
        focus = request.get("focus")
        if isinstance(focus, dict):
            focus = {
                "lon_deg": _f(focus, "lon", 0.0),
                "lat_deg": _f(focus, "lat", 0.0),
                "radius_deg": max(0.0, _f(focus, "radius_deg", 0.0)),
                "aspect": max(0.05, _f(focus, "aspect", 1.0)),
                "rotation_deg": _f(focus, "rotation_deg", 0.0),
            }
            if not focus["radius_deg"] > 0.0:
                focus = None
        else:
            focus = None
        options = {
            "max_labels": max(0, int(_f(request, "labels", DEFAULT_LABELS))),
            "min_radius_px": max(0.0, _f(request, "min_radius_px", DEFAULT_MIN_RADIUS_PX)),
            "font_px": max(6.0, _f(request, "font_px", DEFAULT_LABEL_FONT_PX)),
            "only_illuminated": bool(request.get("only_illuminated", True)),
            "language": language,
            "classes": classes,
            "outline_min_radius_px": max(
                0.0, _f(request, "outline_min_radius_px", DEFAULT_OUTLINE_RADIUS_PX)
            ),
            "satellite": satellite,
            "satellites": bool(request.get("satellites", True)),
            "avoid_overlap": bool(request.get("avoid_overlap", True)),
            "focus": focus,
            "related": related or None,
            "lunar100": bool(request.get("lunar100", False)),
            "region": region,
            "obstacles": obstacles,
        }
        if frame_name == "disk":
            when = request.get("when") or {}
            view = self.view(
                when.get("at") or None,
                parse_observer(when),
                viewpoint=parse_viewpoint(request),
            )
            preview = request.get("preview") or {}
            width = int(_f(preview, "width", PREVIEW_WIDTH))
            frame = moonlib.disk_frame(view, width, width)
        elif frame_name == "region":
            frame = self.cached_region_frame(request.get("render_id"))
        else:
            raise moonlib.MoonlibError(
                f"unknown frame {frame_name!r}; expected 'disk' or 'region'."
            )
        started = time.time()
        # Names go where the picture has them, not where the reference sphere
        # under it would: the renderer draws the height field, so a label placed
        # on the sphere drifts off its own feature by the relief there -- a
        # kilometre of it is 0.65 px on a 2400 px frame.  A close-up may have
        # been traced against a finer regional window than the global pyramid,
        # so it must annotate against that same window.
        terrain = self.terrain()
        if frame_name == "region":
            with self._lock:
                terrain = self._linear_terrain or terrain
        options["terrain"] = terrain
        # The planner only needs the classes it may draw.  Filtering the 10 581
        # feature catalogue here, where the class is already known, keeps a
        # single-family refresh from projecting the other 10 000 names.
        catalogue = self.catalogue()
        if classes is not None:
            wanted = set(classes)
            include_lunar100 = bool(options["lunar100"])
            catalogue = tuple(
                feature for feature in catalogue
                if feature_class(feature.kind, feature.diameter_km) in wanted
                or (include_lunar100 and bool(getattr(feature, "lunar100", 0)))
            )
        # A crater near the limb is a circle standing on the height field; its
        # catalogue centre and the middle of the shape the reader sees are not
        # the same pixel.  The page wants the marker in the middle of the
        # crater, so it takes the projected rim's centroid.
        options["center_on_rim"] = True
        payload = moonlib.annotate_frame(frame, catalogue, **options).to_dict()
        payload["seconds"] = time.time() - started
        # Echo the request in the words the wire uses, so the page can quote the
        # density it actually got rather than the one it meant to ask for.
        payload["request"] = {
            "frame": frame_name,
            "labels": options["max_labels"],
            "min_radius_px": options["min_radius_px"],
            "font_px": options["font_px"],
            "only_illuminated": options["only_illuminated"],
            "language": options["language"],
            "classes": options["classes"],
            "outline_min_radius_px": options["outline_min_radius_px"],
            "center_on_rim": True,
            "satellite": options["satellite"],
            "satellites": options["satellites"],
            "avoid_overlap": options["avoid_overlap"],
            "focus": options["focus"],
            "related": options["related"],
            "lunar100": options["lunar100"],
            "region": options["region"],
            "obstacles": len(options["obstacles"]),
        }
        return payload

    def features(self) -> Dict[str, Any]:
        """The searchable catalogue, built once and then only serialised.

        Built outside the lock: :meth:`catalogue` takes it, and the two together
        would deadlock the request that first asks for the index.
        """
        index = self._feature_index
        if index is None:
            index = feature_index(self.catalogue())
            with self._lock:
                self._feature_index = index
        return index

    def cached_region_frame(self, render_id: Optional[Any] = None):
        """Camera of the close-up on screen, or an error saying to render first."""
        wanted = "先生成模拟图像，再添加月面地貌。"
        with self._lock:
            if self._linear is None or self._linear_view is None:
                raise moonlib.MoonlibError(wanted)
            requested = render_id or self._linear_id
            if str(requested) != str(self._linear_id):
                raise StaleRender(
                    f"That frame is no longer the cached one (have "
                    f"{self._linear_id}, asked for {requested}); render again."
                )
            region = dict(self._linear_context.get("region") or {})
            rotation = float(self._linear_meta.get("rotation_deg") or 0.0)
            view = self._linear_view
        if not region:
            raise moonlib.MoonlibError(wanted)
        with self._lock:
            terrain = self._linear_terrain
        return moonlib.region_frame(
            view,
            float(region["lon_deg"]),
            float(region["lat_deg"]),
            float(region["span_km"]),
            int(region["width"]),
            int(region["height"]),
            rotation,
            terrain=terrain or self.terrain(),
            target_elevation_km=region.get("target_elevation_km"),
        )


class StaleRender(moonlib.MoonlibError):
    """The client asked to process a frame the server no longer holds."""


# ---------------------------------------------------------------------------
# settings files
# ---------------------------------------------------------------------------
class SettingsError(moonlib.MoonlibError):
    """A settings file cannot be read or written as asked."""


def settings_name(raw: Any) -> str:
    """Turn a user-typed name into a safe file stem.

    Everything that is not alphanumeric, CJK punctuation or a space is dropped,
    and the directory part of any path is thrown away, so a name can never walk
    out of the settings directory.  Returns an empty string when nothing usable
    is left, and callers turn that into an error.
    """
    text = Path(str(raw or "")).name.strip()
    if text.lower().endswith(SETTINGS_SUFFIX):
        text = text[: -len(SETTINGS_SUFFIX)]
    allowed = " ._-()[]（）【】"
    kept = "".join(ch for ch in text if ch.isalnum() or ch in allowed).strip(" .")
    return kept[:SETTINGS_NAME_MAX]


class SettingsStore:
    """The directory of ``.conf`` files behind the settings panel."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def path_for(self, name: Any) -> Path:
        stem = settings_name(name)
        if not stem:
            raise SettingsError("请给设置文件起个名字（只允许字母、数字、中文与空格）。")
        return self.directory / (stem + SETTINGS_SUFFIX)

    def list(self) -> Dict[str, Any]:
        files = []
        if self.directory.is_dir():
            for path in sorted(self.directory.glob("*" + SETTINGS_SUFFIX)):
                try:
                    stat = path.stat()
                except OSError:  # pragma: no cover - raced deletion
                    continue
                files.append(
                    {
                        "name": path.stem,
                        "bytes": int(stat.st_size),
                        "saved_utc": _utc_iso(stat.st_mtime),
                    }
                )
        return {"dir": str(self.directory), "files": files}

    def read(self, name: Any) -> Dict[str, Any]:
        path = self.path_for(name)
        if not path.is_file():
            raise SettingsError(f"没有这个设置文件：{path.stem}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SettingsError(f"{path.stem} 读不出来：{error}") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("settings"), dict):
            raise SettingsError(f"{path.stem} 不是本页导出的设置文件。")
        payload["name"] = path.stem
        return payload

    def write(self, name: Any, payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise SettingsError("设置内容必须是一个 JSON 对象。")
        path = self.path_for(name)
        body = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
        if len(body.encode("utf-8")) > SETTINGS_MAX_BYTES:
            raise SettingsError("设置文件过大（上限 256 KB）。")
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".part")
        try:
            temporary.write_text(body + "\n", encoding="utf-8")
            temporary.replace(path)
        except OSError as error:
            raise SettingsError(f"写不进 {path.stem}：{error}") from error
        finally:
            temporary.unlink(missing_ok=True)
        return {"name": path.stem, "bytes": path.stat().st_size,
                "saved_utc": _utc_iso(path.stat().st_mtime)}


def _utc_iso(timestamp: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


def _animation_shadow_steps(output: Dict[str, Any], region: Any) -> int:
    """The shadow budget for an animation frame.

    The same policy a still of the same kind uses, in one place: a close-up gets
    what 1.3.4 settled on for close-ups, the whole disk gets the disk budget.
    The animation used to hard-code 48, which is the *old* quality -- its frames
    kept the stair-stepped shadow edges the stills had already lost.
    """
    return int(_f(output, "shadow_steps",
                  REGION_SHADOW_STEPS if region is not None else DISK_SHADOW_STEPS))


def _frame_carrier(frame: Any, image: Any) -> Any:
    """Wrap post-processed pixels back into a frame-shaped object.

    The encoder reads the instant, the camera and the pixels off the item; a
    bare array has only the third, which is why 含时间戳 used to fail the moment
    后期处理 replaced the image.  One constructor for both the run and the
    preview, so the two cannot drift.
    """
    render = SimpleNamespace(image=image)
    view = getattr(getattr(frame, "render", None), "view", None)
    region = getattr(getattr(frame, "render", None), "region", None)
    render.view = view
    render.region = region
    return SimpleNamespace(et=getattr(frame, "et", None),
                           when_utc=getattr(frame, "when_utc", None),
                           render=render)


def _frame_processor(
    train: Any,
    atmosphere: Dict[str, Any],
    processing: Dict[str, Any],
    job_id: str,
):
    """The 后期处理 chain, as a function from an animation frame to pixels.

    The same :func:`moonlib.process_photograph` call :meth:`MoonService.process`
    makes for a single frame, with one difference that matters for a sequence:
    the noise seed includes the frame number.  A frozen seed would stamp the
    *same* grain pattern on every frame, which is not what noise looks like --
    it would read as a dirty lens rather than as a sensor.

    The frames are processed where they were rendered.  Seeing, diffraction and
    sharpening are all angular quantities, so applying them before any
    resampling is the only order that means anything.
    """
    index = itertools.count()

    def apply(frame: Any) -> np.ndarray:
        render = getattr(frame, "render", None)
        if render is None:
            raise moonlib.MoonlibError(
                "「应用后期处理」需要渲染出的帧，而不是已经处理过的像素。"
            )
        sunlit = getattr(render, "sunlit", None)
        earthshine = getattr(render, "earthshine", None)
        if sunlit is None or earthshine is None:
            raise moonlib.MoonlibError(
                "这一帧没有保留 sunlit/earthshine 分量，无法应用后期处理。"
            )
        stats = getattr(render, "stats", None) or {}
        scale = float(stats.get("exposure_scale", 1.0))
        linear = (np.asarray(sunlit, dtype=np.float64)
                  + np.asarray(earthshine, dtype=np.float64)) * scale
        photons = _opt_f(processing, "noise_photons")
        sigma = _f(processing, "noise_sigma", 0.0)
        plate_scale = float(stats.get("arcsec_per_pixel") or 1.0)
        return moonlib.process_photograph(
            linear,
            None,
            train,
            exposure_scale=1.0,
            arcsec_per_pixel=plate_scale,
            seeing_fwhm_arcsec=_f(atmosphere, "seeing", 0.8),
            frame_selection_gain=_f(atmosphere, "frame_selection_gain", 1.0),
            blur_strength=_f(processing, "blur_strength", 1.0),
            diffraction=_f(processing, "diffraction", 0.0),
            sharpen_strength=_f(processing, "sharpen_strength", 1.0),
            **_sharpen_radius(processing, plate_scale),
            **(_processing_extras(processing)),
            saturation=_f(processing, "saturation", 1.0),
            gain=_f(processing, "gain", 1.0),
            offset=_f(processing, "offset", 0.0),
            gamma=_f(processing, "gamma", 1.0),
            rotation_deg=_f(processing, "rotation_deg", 0.0),
            mirror=str(processing.get("mirror") or "none"),
            noise_photons=photons,
            noise_sigma=sigma,
            noise_seed=noise_seed(f"{job_id}#{next(index)}", photons, sigma),
        )

    return apply


def _animation_size(value: Any) -> Optional[Any]:
    """The file's frame size: ``None``, one number, or a ``(width, height)`` pair.

    A whole-disk animation is square and says so with one number; a close-up's
    shape is the selection's own, so it says so with two.  Anything else is a
    caller's mistake and is reported rather than guessed at.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return [int(value[0]), int(value[1])]
        except (TypeError, ValueError) as exc:
            raise moonlib.MoonlibError(
                f"动画输出分辨率必须是两个整数（收到 {value!r}）。"
            ) from exc
    raise moonlib.MoonlibError(
        f"动画输出分辨率必须是一个数或一对数（收到 {value!r}）。"
    )


#: Where a time stamp may be burned in, as the page names the corners.
_TIMESTAMP_CORNERS = ("left-top", "right-top", "left-bottom", "right-bottom")


def _animation_timestamp(value: Any) -> Optional[Dict[str, Any]]:
    """The 含时间戳 block, or ``None`` when the reader did not ask for one."""
    if not value:
        return None
    if not isinstance(value, dict):
        raise moonlib.MoonlibError("时间戳设置必须是一个对象。")
    corner = str(value.get("corner") or "left-top")
    if corner not in _TIMESTAMP_CORNERS:
        raise moonlib.MoonlibError(
            f"时间戳位置只能是 {'/'.join(_TIMESTAMP_CORNERS)}（收到 {corner!r}）。"
        )
    margin = _f(value, "margin_px", 16.0)
    if margin < 0.0:
        raise moonlib.MoonlibError("时间戳边距不能是负数。")
    offset = _f(value, "utc_offset_hours", 0.0)
    return {"corner": corner, "margin_px": int(round(margin)),
            "utc_offset_hours": float(offset)}


#: The style keys the page sends for animation labels, and what a request that
#: predates one of them gets.  The numbers are screen pixels at the page's 13 px
#: reference font; :mod:`moonlib.labelpaint` scales them to each frame.
_ANIMATION_LABEL_STYLE = {
    "text_color": "#eef2ff",
    "text_stroke": "#0c0e14",
    "text_stroke_width": 2.0,
    "marker_stroke": "#0c0e14",
    "marker_stroke_width": 1.0,
    "outline_color": "#b4d6ff",
    "outline_width": 1.0,
    "outline_style": "solid",
}


def _animation_annotations(value: Any) -> Dict[str, Any]:
    """The 地貌 block of an animation request, defaults filled in.

    The catalogue projection stays on the server -- it needs every frame's camera
    and the gazetteer -- while the *look* is the page's, so this is the hand-off
    point between the two.  An unknown class or an unsupported language is
    refused here rather than halfway through a run, and a missing Pillow or font
    fails the request before the render slot is taken.
    """
    if not value or not isinstance(value, dict) or not value.get("on"):
        return {"on": False}
    language = str(value.get("language") or DEFAULT_LANGUAGE)
    if language not in moonlib.LANGUAGES:
        raise moonlib.MoonlibError(
            f"动画标注的语言只能是 {'/'.join(moonlib.LANGUAGES)}（收到 {language!r}）。"
        )
    classes = value.get("classes")
    if classes is not None:
        if not isinstance(classes, (list, tuple)):
            raise moonlib.MoonlibError("动画标注的 classes 必须是分类 key 的列表。")
        classes = [str(key) for key in classes]
        unknown = sorted(set(classes) - set(FEATURE_CLASSES))
        if unknown:
            raise moonlib.MoonlibError(
                f"动画标注收到未知分类 {unknown[0]!r}；可选：{', '.join(FEATURE_CLASSES)}。"
            )
    style = {}
    for key, default in _ANIMATION_LABEL_STYLE.items():
        raw = value.get(key, default)
        style[key] = str(raw) if isinstance(default, str) else _f(value, key, float(default))
    style["text_stroke_width"] = max(0.0, min(8.0, style["text_stroke_width"]))
    style["marker_stroke_width"] = max(0.0, min(4.0, style["marker_stroke_width"]))
    style["outline_width"] = max(0.0, min(6.0, style["outline_width"]))
    style["outline_style"] = (
        style["outline_style"] if style["outline_style"] in ("solid", "dash", "dot", "none")
        else "solid"
    )
    palette = value.get("class_colors")
    if not isinstance(palette, dict):
        palette = {}
    style["class_colors"] = {
        str(key): str(colour) for key, colour in palette.items()
        if str(key) in FEATURE_CLASSES and isinstance(colour, str)
    }
    style["uniform"] = bool(value.get("uniform"))
    style["bold"] = bool(value.get("bold"))
    satellite = str(value.get("satellite") or "full")
    if satellite not in ("full", "letter"):
        satellite = "full"
    try:
        import PIL  # noqa: F401
    except ImportError as error:  # pragma: no cover - install dependent
        raise moonlib.MoonlibError(
            "动画的「地貌」需要 Pillow；安装 `pip install -e '.[video]'` 后重试。"
        ) from error
    find_label_font(bool(style["bold"]))
    return {
        "on": True,
        "lock": bool(value.get("lock")),
        "language": language,
        "classes": classes,
        "only_illuminated": bool(value.get("only_illuminated", True)),
        "labels": max(0, int(_f(value, "labels", DEFAULT_LABELS))),
        "min_radius_px": max(0.0, _f(value, "min_radius_px", DEFAULT_MIN_RADIUS_PX)),
        "outline_min_radius_px": max(
            0.0, _f(value, "outline_min_radius_px", DEFAULT_OUTLINE_RADIUS_PX)
        ),
        "font_px": max(6.0, min(400.0, _f(value, "font_px", DEFAULT_LABEL_FONT_PX))),
        "satellite": satellite,
        "satellites": bool(value.get("satellites", True)),
        "avoid_overlap": bool(value.get("avoid_overlap", True)),
        "lunar100": bool(value.get("lunar100", False)),
        "region": bool(value.get("region", False)),
        "style": style,
    }


class _AnimationLabelPainter:
    """Draw one animation frame's landform names, using that frame's camera.

    An unlocked run lays every frame out for its own instant: the terminator
    moves, features cross the limb, and the names have to follow.  A locked run
    keeps the first layout for the whole file -- the right answer for a short
    clip where names that jump between frames read as a defect.
    """

    def __init__(self, service: Any, spec: Dict[str, Any], settings: Any,
                 region: Optional[Dict[str, Any]],
                 processing: Optional[Dict[str, Any]]) -> None:
        self._service = service
        self._spec = spec
        self._settings = settings
        self._region = region
        self._rotation = float((processing or {}).get("rotation_deg") or 0.0)
        self._mirror = str((processing or {}).get("mirror") or "none")
        # The layout is always computed from *every* feature, lit or not.  If
        # the candidate set itself followed the terminator, a name would enter
        # and leave the arrangement as the light moved, and the avoidance
        # algorithm would find a different set of slots every frame -- the
        # labels visibly jumped back and forth.  Layout is now a function of
        # the camera only; the lit/dark switch is applied to the finished plan.
        self._only_illuminated = bool(spec["only_illuminated"])
        self._options = {
            "max_labels": spec["labels"],
            "min_radius_px": spec["min_radius_px"],
            "outline_min_radius_px": spec["outline_min_radius_px"],
            "font_px": spec["font_px"],
            "language": spec["language"],
            "classes": spec["classes"],
            "only_illuminated": False,
            "satellite": spec.get("satellite", "full"),
            "satellites": bool(spec.get("satellites", True)),
            "avoid_overlap": spec.get("avoid_overlap", True),
            "lunar100": bool(spec.get("lunar100", False)),
            "region": bool(spec.get("region", False)),
        }
        self._frozen = None

    def _projection(self, item: Any, width: int, height: int) -> Any:
        view = getattr(getattr(item, "render", None), "view", None)
        if view is None:
            raise moonlib.MoonlibError("动画的「地貌」需要每一帧的相机，但这一帧没有。")
        if self._region is not None:
            rendered = getattr(item.render, "region", None)
            if rendered is None:
                raise moonlib.MoonlibError("动画的「地貌」在近景帧上找不到选框信息。")
            # The frame may have been traced against a finer regional window
            # than the global pyramid; the labels have to stand on that same
            # ground and aim at the height the renderer actually used.
            terrain = getattr(item.render, "terrain", None) or self._service.terrain()
            return region_frame(
                view,
                rendered.lon_deg,
                rendered.lat_deg,
                rendered.span_km,
                int(width),
                int(height),
                rotation_deg=float(self._region.get("rotation_deg") or 0.0),
                terrain=terrain,
                target_elevation_km=getattr(rendered, "target_elevation_km", None),
            )
        return disk_frame(
            view, int(width), int(height),
            disk_fraction=float(self._settings.disk_fraction),
        )

    def _plan(self, item: Any, width: int, height: int,
              projection: Optional[Any] = None) -> Any:
        projection = projection or self._projection(item, width, height)
        terrain = self._service.terrain()
        return plan_annotations(
            projection, self._service.catalogue(), terrain=terrain,
            **self._options,
        )

    def _apply_light(self, plan: Any, projection: Any) -> Any:
        """Show only the labels the current frame actually has lit.

        The positions came from the all-features layout, so removing a dark
        name leaves its slot empty instead of moving every other name.  With
        暗面 on, the names stay and only their faded/opaque state changes.
        """
        items = tuple(plan.items)
        if not items:
            return plan
        projected = projection.project(
            [float(item.lat_deg) for item in items],
            [float(item.lon_deg) for item in items],
        )
        facing = np.asarray(projected["facing"], dtype=bool)
        x = np.asarray(projected["x"], dtype=np.float64)
        y = np.asarray(projected["y"], dtype=np.float64)
        incidence = np.asarray(projected["incidence_deg"], dtype=np.float64)
        inside = (
            facing
            & (x >= 0.0) & (x <= float(projection.width) - 1.0)
            & (y >= 0.0) & (y <= float(projection.height) - 1.0)
        )
        lit = inside & (incidence < 90.0)
        if self._only_illuminated:
            kept = tuple(item for index, item in enumerate(items) if lit[index])
            return plan if len(kept) == len(items) else replace(plan, items=kept)
        # 暗面 on: keep every name, but let the painter fade the ones that are
        # currently in shadow rather than using the first frame's answer.
        updated = tuple(
            replace(item, illuminated=bool(lit[index]))
            for index, item in enumerate(items)
        )
        return replace(plan, items=updated)

    def __call__(self, item: Any, frame: Any) -> Any:
        shape = np.asarray(frame).shape
        if len(shape) < 2:
            return frame
        height, width = int(shape[0]), int(shape[1])
        projection = self._projection(item, width, height)
        plan = self._frozen
        if plan is None or int(plan.frame.width) != width or int(plan.frame.height) != height:
            plan = self._plan(item, width, height, projection)
            if self._spec.get("lock"):
                self._frozen = plan
        plan = self._apply_light(plan, projection)
        return paint_annotation_plan(
            frame, plan,
            style=self._spec["style"],
            rotation_deg=self._rotation,
            mirror=self._mirror,
        )



def _sharpen_radius(processing: Dict[str, Any],
                    arcsec_per_pixel: Optional[float]) -> Dict[str, Any]:
    """锐化半径, as the unsharp mask's width.

    The page states it the way a reader judges it -- how many *pixels* wide the
    halo is -- and the optics work in arcseconds.  Those are the same statement
    once the plate scale is known, and the plate scale belongs to the frame, so
    the conversion happens here rather than in the page (which would have to
    guess it before the first render).  ``sharpen_radius_px`` is the page's key;
    ``sharpen_radius_arcsec`` is still accepted directly, and ``sharpen_radius``
    (a multiple of the seeing) is kept as the fallback for callers that predate
    both -- a hand-written .conf, or the CLI's own payload.
    """
    direct = _opt_f(processing, "sharpen_radius_arcsec")
    pixels = _opt_f(processing, "sharpen_radius_px")
    if direct is None and pixels is not None and arcsec_per_pixel:
        direct = max(0.02, float(pixels) * float(arcsec_per_pixel))
    return {
        "sharpen_radius_factor": _f(processing, "sharpen_radius", 1.2),
        "sharpen_radius_arcsec": direct,
    }


def _processing_extras(processing: Dict[str, Any]) -> Dict[str, Any]:
    """The processing controls that are not the mask radius.

    Kept in one place because a still and an animation frame must read the same
    page payload the same way; the two used to duplicate this list, and a new
    control could reach one path and silently miss the other.
    """
    method = str(processing.get("sharpen_method") or "gaussian").strip().lower()
    if method not in ("gaussian", "wavelet", "wavelets"):
        method = "gaussian"
    levels = int(round(max(1.0, min(6.0, _f(processing, "sharpen_levels", 4.0)))))
    share = _opt_f(processing, "diffraction_share")
    if share is not None:
        share = max(0.0, min(1.0, float(share)))
    return {
        "diffraction_radius_px": _opt_f(processing, "diffraction_radius_px"),
        "diffraction_width_px": _opt_f(processing, "diffraction_width_px"),
        "diffraction_asymmetry": max(
            0.0, min(1.0, _f(processing, "diffraction_asymmetry", 0.75))
        ),
        "diffraction_share": share,
        "sharpen_method": method,
        "sharpen_levels": levels,
        "contrast": _f(processing, "contrast", 1.0),
        "brightness": _f(processing, "brightness", 0.0),
    }

def noise_seed(render_id: Optional[str], photons: Optional[float],
               sigma: float) -> Optional[int]:
    """A seed that changes with the noise settings and with nothing else.

    The page re-processes the cached frame on every slider move.  Drawing fresh
    grain each time would make the picture boil while the *sharpening* slider is
    dragged, which reads as a bug; keying the seed on the frame and the two
    noise parameters means the grain is redrawn exactly when the user asks for
    different noise.
    """
    if not photons and not sigma:
        return None
    key = f"{render_id}|{photons}|{sigma}".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(key, digest_size=4).digest(), "big")


def feature_index(catalogue: Sequence[Any]) -> Dict[str, Any]:
    """The catalogue, flattened for the page's search box.

    One row per named feature with everything a search result has to say --
    both names, where it is, how big it is, which class it belongs to -- so the
    page can filter 9 000 names without a round trip per keystroke.  Classes are
    listed with their counts and labels in the same payload, which is also how
    the page learns the taxonomy instead of hard-coding it.
    """
    rows = []
    for feature in catalogue:
        kind_class = feature_class(feature.kind, feature.diameter_km)
        rows.append({
            "en": feature.name_en,
            # The Lunar 100 search row is English-only for the same reason the
            # annotation caption is: the list's own names are the English ones.
            "zh": "" if kind_class == "lunar100" else (feature.name_zh or ""),
            "lat": round(float(feature.lat_deg), 4),
            "lon": round(float(feature.lon_deg), 4),
            "km": round(float(feature.diameter_km), 2),
            "class": kind_class,
        })
    counts: Dict[str, int] = {}
    for row in rows:
        counts[row["class"]] = counts.get(row["class"], 0) + 1
    return {
        "features": rows,
        "classes": [
            {"key": key, "zh": CLASS_LABELS[key][0], "en": CLASS_LABELS[key][1],
             "count": counts.get(key, 0)}
            for key in FEATURE_CLASSES
            if counts.get(key, 0)
        ],
        "count": len(rows),
    }

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def encode_png(image: np.ndarray) -> bytes:
    from moonlib.plotting import encode_png as _encode

    return _encode(image)


def _data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def disk_payload(image: np.ndarray, geometry: Dict[str, Any]) -> Dict[str, Any]:
    """The body of ``/api/disk``: the picture, the geometry, and the gain map.

    The gain map travels at the **top level**, not inside ``geometry``.  It is a
    second picture rather than a measurement, and leaving it in the geometry
    block is exactly how the page ended up sending a null one: the client read
    ``payload.hdr``, the server had written ``payload.geometry.hdr``, and the
    failure surfaced as "the base image is not a data URL".
    """
    body: Dict[str, Any] = {
        "geometry": geometry,
        "image": _data_url(encode_png(image)),
    }
    gain = geometry.pop("hdr", None)
    if gain is not None:
        body["hdr"] = gain
    return body


def _decode_data_url(url: Any, mode: str = "RGB", what: str = "the image") -> Any:
    """Pillow image from a ``data:`` URL, whatever the browser encoded it with.

    *what* names the field in the error: "the base image is not a data URL" and
    "the gain map is not a data URL" are different bugs, and a message that
    always blames the base sends the reader to the wrong end of the request.
    """
    text = str(url or "")
    if not text.startswith("data:") or "," not in text:
        raise moonlib.MoonlibError(f"expected a data: URL for {what}.")
    from PIL import Image

    head, body = text.split(",", 1)
    raw = base64.b64decode(body) if ";base64" in head else body.encode("utf-8")
    try:
        return Image.open(io.BytesIO(raw)).convert(mode)
    except Exception as error:
        raise moonlib.MoonlibError(f"{what} could not be decoded: {error}") from error


def _resize_gain(data: np.ndarray, shape: Tuple[int, int]) -> np.ndarray:
    """Resize an encoded gain map, in the log2 domain it is stored in.

    The encoded value is affine in log2(gain), so interpolating the encoded
    numbers *is* interpolating the gain; resampling the ratio instead would
    darken every highlight it touched.
    """
    from PIL import Image

    image = Image.fromarray(np.clip(data, 0.0, 1.0).astype(np.float32), mode="F")
    image = image.resize((int(shape[1]), int(shape[0])), Image.BILINEAR)
    return np.asarray(image, dtype=np.float32)


def _gain_map_from_metadata(data: np.ndarray, metadata: Dict[str, Any]):
    """Rebuild the library's GainMap from the block the render reported.

    Both spellings are accepted: the XMP names the file itself carries, and the
    library's own field names.
    """
    missing = object()

    def field(*names: str, default: Any = missing) -> Any:
        for name in names:
            if name in metadata and metadata[name] is not None:
                return float(metadata[name])
        if default is missing:
            raise moonlib.MoonlibError(f"the HDR metadata is missing {names[0]!r}.")
        return default

    return moonlib.GainMap(
        data=np.asarray(data, dtype=np.float32),
        min_log2=field("GainMapMin", "min_log2"),
        max_log2=field("GainMapMax", "max_log2"),
        gamma=field("Gamma", "gamma", default=1.0),
        offset_sdr=field("OffsetSDR", "offset_sdr", default=1.0 / 64.0),
        offset_hdr=field("OffsetHDR", "offset_hdr", default=1.0 / 64.0),
        capacity_min=field("HDRCapacityMin", "capacity_min", default=0.0),
        capacity_max=field("HDRCapacityMax", "capacity_max", default=None),
    )


def _shift_gain(data: np.ndarray, dx: int, dy: int) -> np.ndarray:
    """Translate the gain map by whole pixels: ``out[y, x] = data[y+dy, x+dx]``.

    The page may centre the picture on the lit part before saving, which draws
    the base at ``(-dx, -dy)``; the gain map describes the *same* pixels, so it
    has to move with it or every highlight would land beside its own shadow.
    What moves in is empty background, and the gain there is the lowest one --
    no boost -- rather than whatever was left in the buffer.
    """
    if not dx and not dy:
        return data
    out = np.zeros_like(data)
    height, width = data.shape[0], data.shape[1]
    y0, y1 = max(0, -dy), min(height, height - dy)
    x0, x1 = max(0, -dx), min(width, width - dx)
    if y1 > y0 and x1 > x0:
        out[y0:y1, x0:x1] = data[y0 + dy:y1 + dy, x0 + dx:x1 + dx]
    return out


def _globe_filename(
    request: Dict[str, Any], info: Dict[str, Any], extension: str
) -> str:
    when = (request.get("when") or {}).get("at") or "now"
    stamp = re.sub(r"[^0-9]", "", str(when))[:12] or "now"
    return f"moon-globe-{stamp}.{extension.lstrip('.')}"


def _model_filename(
    request: Dict[str, Any], info: Dict[str, Any], extension: str
) -> str:
    when = (request.get("when") or {}).get("at") or "now"
    stamp = re.sub(r"[^0-9]", "", str(when))[:12] or "now"
    lat = float(info.get("lat_deg") or 0.0)
    lon = float(info.get("lon_deg") or 0.0)
    return (
        f"moon-3d-{stamp}-{lat:.1f}_{lon:.1f}."
        f"{extension.lstrip('.')}"
    )


class Handler(BaseHTTPRequestHandler):
    service: MoonService = None  # type: ignore[assignment]
    server_version = "moonlib-web"

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter
        sys.stderr.write("  %s\n" % (fmt % args))

    # -- helpers --------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str,
              filename: Optional[str] = None,
              extra_headers: Optional[Dict[str, str]] = None) -> None:
        # The whole response is inside the guard, not only the body write: a
        # page refresh between `send_response` and `end_headers` raises
        # BrokenPipeError from `flush_headers`, which used to fall through to
        # the generic handler and try to send a 500 to a dead socket.
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            if filename:
                # The animation is a file the reader asked for, so the browser
                # must save it under a name rather than render it in a tab.
                self.send_header(
                    "Content-Disposition", f'attachment; filename="{filename}"'
                )
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:  # pragma: no cover - browser went away
            # A page refresh while a large JSON or PNG is on the wire is not a
            # server error: the peer closed the socket and there is nobody left
            # to read a 500.  Treat it as the end of this request.
            pass

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        # ``allow_nan=False`` is the point, not a detail: without it Python
        # writes the bare token ``NaN``, which is not JSON, and ``JSON.parse``
        # in the browser then rejects the whole response.  The failure appears
        # as a page that silently stops updating, with nothing in the terminal
        # to explain it.  The library already translates its "not applicable"
        # NaN sentinels at the model boundary, so reaching this guard means a
        # payload was built outside that boundary -- and it should be loud.
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as error:
            raise moonlib.MoonlibError(f"Malformed JSON body: {error}") from error

    def _client_gone(self, *, force: bool = False) -> bool:
        """Whether the peer has closed the connection.

        Polled by long renders through their cancel predicate.  A socket that
        is readable with ``recv(MSG_PEEK) == b""`` has reached EOF; a readable
        socket with data is a pipelined request and is not a disconnect.  The
        check is throttled because it is a syscall on the render's hot path.
        """
        now = time.monotonic()
        checked = getattr(self, "_client_gone_at", 0.0)
        if not force and now - checked < 0.25:
            return bool(getattr(self, "_client_gone_value", False))
        self._client_gone_at = now
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                gone = False
            else:
                gone = self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            gone = True
        self._client_gone_value = gone
        return gone

    # -- routes ---------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._file(STATIC / "index.html", "text/html; charset=utf-8")
        if path.startswith("/static/"):
            name = Path(path).name
            suffix = Path(name).suffix.lower()
            kind = {
                ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".html": "text/html; charset=utf-8",
                ".png": "image/png",
                ".svg": "image/svg+xml",
            }.get(suffix, "application/octet-stream")
            return self._file(STATIC / name, kind)
        if path == "/api/health":
            return self._json(200, {
                "ok": True,
                "limits": self.service.limits(),
                "data_sources": self.service.data_sources(),
            })
        if path == "/api/features":
            return self._json(200, self.service.features())
        if path == "/api/globe-texture":
            query = parse_qs(urlparse(self.path).query)
            def first(name, default=""):
                return query.get(name, [default])[0]
            try:
                width = int(float(first("width", "1024")))
            except ValueError:
                width = 1024
            when = {
                "at": first("at", ""),
                "tz_offset_hours": float(first("tz", "8") or 8),
                "observer": {
                    "lat": float(first("lat", "30") or 30),
                    "lon": float(first("lon", "120") or 120),
                },
            }
            png = self.service.globe_texture({"when": when}, width)
            return self._send(200, png, "image/png")
        if path == "/api/settings":
            return self._json(200, self.service.settings.list())
        match = re.fullmatch(r"/api/patch/(\d+)", path)
        if match:
            return self._json(200, self.service.patch_status(match.group(1)))
        match = re.fullmatch(r"/api/animation/(\d+)(/file)?", path)
        if match:
            job_id = match.group(1)
            if match.group(2):
                # The MP4 itself: a real file, not a data URL, because an
                # animation is tens of megabytes and base64 would inflate it.
                try:
                    file_path = self.service.animation_file(job_id)
                except moonlib.MoonlibError as error:
                    return self._json(404, {"error": str(error)})
                return self._send(
                    200, file_path.read_bytes(), "video/mp4",
                    filename=f"moon-animation-{job_id}.mp4",
                )
            return self._json(200, self.service.animation_status(job_id))
        return self._json(404, {"error": f"no route {path}"})

    def _file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            return self._json(404, {"error": f"missing {path.name}"})
        self._send(200, path.read_bytes(), content_type)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        self._client_gone_at = 0.0
        self._client_gone_value = False
        try:
            request = self._body()
            if path == "/api/disk":
                preview = request.get("preview") or {}
                image, geometry = self.service.disk(
                    request,
                    int(_f(preview, "width", PREVIEW_WIDTH)),
                    int(_f(preview, "samples", PREVIEW_SAMPLES)),
                    int(_f(preview, "shadow_steps", PREVIEW_SHADOW_STEPS)),
                    cancel=self._client_gone,
                )
                return self._json(200, disk_payload(image, geometry))
            if path == "/api/region":
                image, info, render_id, raw = self.service.region(
                    request, cancel=self._client_gone
                )
                return self._json(
                    200,
                    {"info": info, "render_id": render_id,
                     "image": _data_url(encode_png(image)),
                     "image_raw": _data_url(encode_png(raw))},
                )
            if path == "/api/moon/glb":
                data, info, content_type = self.service.whole_moon_glb(request)
                filename = _globe_filename(request, info, "glb")
                model_info = quote(
                    json.dumps(info, ensure_ascii=False, separators=(",", ":")),
                    safe="",
                )
                return self._send(
                    200, data, content_type, filename=filename,
                    extra_headers={"X-Moonlib-Mesh-Info": model_info},
                )
            if path == "/api/moon/blender":
                data, info, content_type = self.service.whole_moon_blender(request)
                filename = _globe_filename(request, info, "blend")
                model_info = quote(
                    json.dumps(info, ensure_ascii=False, separators=(",", ":")),
                    safe="",
                )
                return self._send(
                    200, data, content_type, filename=filename,
                    extra_headers={"X-Moonlib-Mesh-Info": model_info},
                )
            if path == "/api/mesh/blender":
                data, info, content_type = self.service.blender_preview(request)
                filename = _model_filename(request, info, "blend")
                model_info = quote(
                    json.dumps(info, ensure_ascii=False, separators=(",", ":")),
                    safe="",
                )
                return self._send(
                    200, data, content_type, filename=filename,
                    extra_headers={"X-Moonlib-Mesh-Info": model_info},
                )
            if path == "/api/mesh/file":
                data, info, content_type = self.service.mesh_file(request)
                extension = "glb" if content_type == "model/gltf-binary" else "stl"
                filename = _model_filename(request, info, extension)
                model_info = quote(
                    json.dumps(info, ensure_ascii=False, separators=(",", ":")),
                    safe="",
                )
                return self._send(
                    200, data, content_type, filename=filename,
                    extra_headers={"X-Moonlib-Mesh-Info": model_info},
                )
            if path == "/api/mesh":
                return self._json(200, self.service.mesh(request))
            if path == "/api/sky":
                # Samples, not a picture: the window draws, re-ranges and
                # labels the same tracks without another round trip.
                return self._json(200, self.service.sky(request))
            if path == "/api/curve":
                return self._json(200, self.service.curve(request))
            if path == "/api/surface-sun":
                return self._json(200, self.service.surface_sun(request))
            if path == "/api/cancel":
                # 终止: the page stops waiting *and* says so.  The render polls
                # this between bands, which is the difference between a button
                # that stops the work and one that only stops watching it.
                return self._json(200, self.service.cancel())
            finished = re.fullmatch(r"/api/animation/(\d+)/finish", path)
            if finished:
                # 立即保存: stop after the current frame and encode the rest.
                return self._json(200, self.service.finish_animation(finished.group(1)))
            if path == "/api/process":
                image, info = self.service.process(request)
                body = {"info": info, "image": _data_url(encode_png(image))}
                if request.get("raw"):
                    # The geometry pane is the same frame without the treatment,
                    # so a change of orientation has to update both.  Asking for
                    # both here means one round trip instead of two, and the page
                    # never has to reproduce the identity settings.
                    raw, _ = self.service.process(
                        {**request,
                         "processing": self.service.identity_processing(request)}
                    )
                    body["image_raw"] = _data_url(encode_png(raw))
                return self._json(200, body)
            if path == "/api/annotations":
                return self._json(200, self.service.annotations(request))
            if path == "/api/features":
                return self._json(200, self.service.features())
            if path == "/api/crescents":
                return self._json(200, self.service.crescents(request))
            if path == "/api/events":
                return self._json(200, self.service.events(request))
            if path == "/api/animation":
                return self._json(200, self.service.animation(request))
            if path == "/api/source":
                return self._json(200, self.service.set_data_source(request))
            if path == "/api/patch":
                return self._json(200, self.service.patch_download(request))
            if path == "/api/hdr/jpeg":
                return self._json(200, self.service.hdr_jpeg(request))
            if path == "/api/settings/save":
                return self._json(200, self.service.settings_save(request))
            if path == "/api/settings/load":
                return self._json(200, self.service.settings.read(request.get("name")))
            if path == "/api/settings/list":
                return self._json(200, self.service.settings.list())
            return self._json(404, {"error": f"no route {path}"})
        except StaleRender as error:
            if self._client_gone(force=True):
                return
            return self._json(409, {"error": str(error), "stale": True})
        except moonlib.MoonlibError as error:
            # A cancellation caused by the peer closing is not a 400 to send to
            # that peer; there is no peer left.
            if self._client_gone(force=True):
                return
            return self._json(400, {"error": str(error)})
        except ConnectionError:
            # The response could not be written because the browser went away.
            return
        except Exception as error:  # pragma: no cover - surfaced to the page
            if self._client_gone(force=True):
                return
            traceback.print_exc()
            return self._json(500, {"error": f"{type(error).__name__}: {error}"})


def build_server(host: str, port: int, settings_dir: Optional[Path] = None) -> ThreadingHTTPServer:
    service = MoonService(settings_dir)
    print("opening terrain ...", flush=True)
    started = time.time()
    service.terrain()
    print(
        "terrain ready in %.1f s (%s)"
        % (
            time.time() - started,
            ", ".join(
                "%s %.0f m/px" % (p.name, km * 1000)
                for p, km in moonlib.global_dem_sources()
            )
            or "default pyramid",
        ),
        flush=True,
    )

    # Loaded here, in the one thread that exists before a single request is
    # served.  CSPICE keeps its pool in process-global state, so a load that
    # happened lazily inside the first request thread would race with the
    # others; doing it up front also turns a missing kernel into a line in the
    # banner instead of a 500 on the first render.
    started = time.time()
    session = moonlib.init_kernels()
    span = session.coverage_text()
    print(
        "kernels ready in %.1f s (%d files, %s%s)"
        % (
            time.time() - started,
            len(session.loaded),
            session.mode,
            "" if span is None else ", %s .. %s" % span,
        ),
        flush=True,
    )

    class Bound(Handler):
        pass

    Bound.service = service
    return ThreadingHTTPServer((host, port), Bound)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--settings-dir", type=Path, default=DEFAULT_SETTINGS_DIR,
                        help="directory of .conf files the settings panel lists")
    args = parser.parse_args(argv)

    httpd = build_server(args.host, args.port, args.settings_dir)
    host, port = httpd.server_address[:2]
    print(f"\n  moon web  ->  http://{host}:{port}/", flush=True)
    print(f"  settings  ->  {args.settings_dir}", flush=True)
    print("  Ctrl-C to stop\n", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
