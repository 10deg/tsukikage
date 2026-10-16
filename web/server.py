#!/usr/bin/env python3
"""Local web page for the lunar simulation.

Serves one page plus a small JSON API.  Everything the page does goes through
:func:`moonlib_s.render_moon` and :func:`moonlib_s.render_region`, so the browser is
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
import re
import sys
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

import moonlib_s  # noqa: E402
from moonlib_s.annotations import CLASS_LABELS, FEATURE_CLASSES, feature_class  # noqa: E402
from moonlib_s.models import MoonRenderSettings  # noqa: E402

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
    "gain": 1.0,
    "offset": 0.0,
    "gamma": 1.0,
}
PREVIEW_SAMPLES = 1
PREVIEW_SHADOW_STEPS = 24

#: Hard pixel caps.  A preview is re-rendered on every change of time or place,
#: so its cap is the one that decides how interactive the page feels -- but the
#: page offers up to 5000 as a deliberate choice (about half a minute of ray
#: marching), so the cap has to be at least that or the choice would be a lie.
#: An output render is the user's call -- the pane renders exactly what it is
#: asked for -- so its cap is only a memory guard against a typo, not a bound on
#: useful work: 8192x8192 is 67 M pixels and hours of ray marching if anyone
#: really means it.
MAX_PREVIEW_PX = 5000
MAX_OUTPUT_PX = 8192
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

#: Animations are a sequence of renders, so they run as a background job and the
#: page polls: a whole lunation at 1000 px is minutes, which no HTTP request can
#: wait for.  The finished MP4 is kept under the cache directory, and only the
#: How long a new render waits for a cancelled one to give the slot back.
#:
#: A frame stops at the end of the band it is tracing, and a band at 3000 px
#: with 2x2 sampling takes a few seconds; a reader who presses 终止 and then
#: 生成 should wait that out rather than be told to try again.
_RENDER_HANDOVER_SECONDS = 20.0

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
        import moonlib_s
        sources = moonlib_s.terrain.global_dem_sources()
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


def parse_train(payload: Dict[str, Any]) -> moonlib_s.ImagingTrain:
    """Build the imaging train, falling back to the documented defaults.

    A blank field means "use the default", which is how the page can start with
    an 8-inch f/10 SCT and an IMX533C without the user typing anything.
    """
    scope = payload.get("telescope") or {}
    camera = payload.get("camera") or {}
    aperture = _f(scope, "aperture_mm", moonlib_s.DEFAULT_TELESCOPE.aperture_mm)
    focal_length = _opt_f(scope, "focal_length_mm")
    focal_ratio = None if focal_length else _f(
        scope, "focal_ratio", moonlib_s.DEFAULT_TELESCOPE.native_focal_ratio
    )
    telescope = moonlib_s.Telescope(
        aperture_mm=aperture,
        focal_ratio=focal_ratio,
        focal_length_mm=focal_length,
        barlow=_f(scope, "barlow", 1.0),
        back_focus=_f(scope, "back_focus", 1.0),
        obstruction=_f(scope, "obstruction", 0.34),
    )
    imx = moonlib_s.DEFAULT_CAMERA
    cam = moonlib_s.Camera(
        width_px=int(_f(camera, "width_px", imx.width_px)),
        height_px=int(_f(camera, "height_px", imx.height_px)),
        pixel_um=_f(camera, "pixel_um", imx.pixel_um),
        color=bool(camera.get("color", True)),
    )
    return moonlib_s.ImagingTrain(telescope, cam)


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
    converter = getattr(moonlib_s, "as_photometric_model", None)
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
        albedo = moonlib_s.moonview.DEFAULT_EARTH_GEOMETRIC_ALBEDO
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


#: Tone curves for the single-component exports, mirroring
#: :func:`moonlib_s.plotting.write_radiance_png`: the CLI writes the earthshine
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
    from moonlib_s.moonview import apply_tone_mapping

    field = getattr(render, component, None)
    if field is None:  # pragma: no cover - both are kept for every web render
        raise moonlib_s.MoonlibError(f"这一帧没有保留 {component} 分量。")
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
    """Just enough of a ``MoonRender`` for :func:`moonlib_s.sdr_hdr_pair`.

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


def parse_observer(when: Dict[str, Any]) -> moonlib_s.Observer:
    site = when.get("observer") or {}
    return moonlib_s.Observer(
        lat=_f(site, "lat", 30),
        lon=_f(site, "lon", 120),
        name=str(site.get("name") or "observer"),
    )


# ---------------------------------------------------------------------------
# the two things the page can ask for
# ---------------------------------------------------------------------------
class MoonService:
    """Holds the terrain and the view cache, so requests stay cheap."""

    def __init__(self, settings_dir: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        # Held for the length of a ray march, so two of them cannot run at once.
        self._render_lock = threading.Lock()
        # The flag the running render polls.  Set by /api/cancel, replaced at the
        # start of every render: a cancel that arrives after the frame it was
        # meant for must not stop the next one.
        self._cancel_event = threading.Event()
        self._terrain = None
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
        self._counter = 0

    def terrain(self):
        with self._lock:
            if self._terrain is None:
                started = time.time()
                self._terrain = moonlib_s.open_terrain()
                self._terrain_seconds = time.time() - started
            return self._terrain

    def catalogue(self) -> Tuple[Any, ...]:
        """Named landforms, read once: the gazetteer is 9 000 rows of CSV."""
        with self._lock:
            if self._catalogue is None:
                self._catalogue = moonlib_s.annotation_catalogue()
            return self._catalogue

    def view(self, when_utc: str, site: moonlib_s.Observer, earth_albedo: float = 0.0):
        return self.report(when_utc, site, earth_albedo)["view"]

    def report(self, when_utc: str, site: moonlib_s.Observer,
               earth_albedo: float = 0.0) -> Dict[str, Any]:
        """Geometry plus the full moon_report, cached together.

        The report includes rise, transit and set, which are found by root
        solving and are far too slow to redo on every keystroke; the page asks
        for it on each change, so it is cached against the same key.

        A non-default Earth albedo is part of that key rather than a detail of
        the render: it is read once, while the view is built, so two albedos are
        two views -- and the earthshine the frame reports has to be the one it
        was drawn with.
        """
        key = (
            str(when_utc),
            round(site.lat, 6),
            round(site.lon, 6),
            round(float(earth_albedo or 0.0), 6),
        )
        with self._lock:
            cached = self._views.get(key)
        if cached is None:
            view = moonlib_s.view_geometry(
                site, when_utc, earth_albedo=earth_albedo or None
            )
            try:
                report = moonlib_s.moon_report(site, when_utc).to_dict()
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
            # The page prints this in its header, so "which build is this?" has
            # an answer without opening a terminal.
            "version": moonlib_s.__version__,
        }

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
            raise moonlib_s.MoonlibError(
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
                raise moonlib_s.MoonlibError(
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

    def disk(self, request: Dict[str, Any], width: int, samples: int,
             shadow_steps: int
             ) -> Tuple[np.ndarray, Dict[str, Any]]:
        if int(width) > MAX_DISK_PX:
            raise moonlib_s.MoonlibError(
                f"整月面最大 {MAX_DISK_PX} px（收到 {int(width)}）——"
                "再往上地形数据已经没有更多细节了。"
            )
        if int(width) < MIN_PX:
            raise moonlib_s.MoonlibError(
                f"整月面最小 {MIN_PX} px（收到 {int(width)}）。"
            )
        when = request.get("when") or {}
        site = parse_observer(when)
        earth = _earthshine(request.get("earthshine"))
        # The albedo describes the Earth, not the switch: a frame drawn with
        # earthshine off still reports the truth about the sky it was taken in.
        view = self.view(
            when.get("at") or None, site, earth["earth_geometric_albedo"]
        )
        settings = MoonRenderSettings(
            width=int(width),
            height=int(width),
            samples_per_pixel=int(samples),
            shadow_steps=int(shadow_steps),
            photometric_model=_optical_model(
                _light_option(request, "preview", "photometric_model")
            ),
            sun_samples=_sun_samples(_light_option(request, "preview", "sun_samples")),
            tone="srgb",
            keep_components=True,
            sky_ambient=_sky_ambient(_light_option(request, "preview", "sky_ambient")),
            roughness_deg=_roughness(_light_option(request, "preview", "roughness_deg")),
            **earth,
        )
        started = time.time()
        # The slot first, then a fresh cancel flag: whether the frame holding
        # the slot was cancelled is only readable until `_rendering()` replaces
        # the flag with this frame's own, and that is what decides whether to
        # wait for the handover or refuse at once.
        with self._render_slot():
            with self._rendering():
                render = moonlib_s.render_moon(
                    view=view, settings=settings, terrain=self.terrain(),
                    cancel=self._cancel_event.is_set,
                )
        geometry = moonlib_s.view_summary(render.view)
        geometry["report"] = self.report(
            when.get("at") or None, site, earth["earth_geometric_albedo"]
        )["report"]
        if not geometry["report"]:  # pragma: no cover - fallback
            geometry["moon_altitude_deg"] = moonlib_s.az_el(
                "MOON", moonlib_s.to_et(geometry["when_utc"]), site
            ).elevation_deg
        geometry["utc_offset_hours"] = _tz_offset(request)
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
            sdr, high = moonlib_s.sdr_hdr_pair(source, stops=stops)
            gains = moonlib_s.gain_map(sdr, high, stops=stops, gamma=gamma)
            geometry["hdr"] = {
                "gain": _data_url(encode_png(gains.data)),
                "metadata": moonlib_s.gain_map_metadata(gains),
                "width": int(width),
                "height": int(width),
                "stops": stops,
            }
        return _component_image(render, _component(request)), geometry

    # -- close-up -------------------------------------------------------
    def region(
        self, request: Dict[str, Any]
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
            raise moonlib_s.MoonlibError(
                "No selection: drag a box on the Moon image first."
            )
        site = parse_observer(when)
        earth = _earthshine(request.get("earthshine"))
        # The albedo describes the Earth, not the switch: a frame drawn with
        # earthshine off still reports the truth about the sky it was taken in.
        view = self.view(
            when.get("at") or None, site, earth["earth_geometric_albedo"]
        )
        train = parse_train(request)
        output = request.get("output") or {}

        width = int(_f(output, "width", 1200))
        height = int(_f(output, "height", width))
        for label, value in (("宽", width), ("高", height)):
            if not MIN_PX <= value <= MAX_OUTPUT_PX:
                raise moonlib_s.MoonlibError(
                    f"输出{label}必须在 64–{MAX_OUTPUT_PX} px 之间（收到 {value}）。"
                )
        settings = MoonRenderSettings(
            width=width,
            height=height,
            samples_per_pixel=int(_f(output, "samples", 2)),
            shadow_steps=int(_f(output, "shadow_steps", 64)),
            photometric_model=_optical_model(
                _light_option(request, "output", "photometric_model")
            ),
            sun_samples=_sun_samples(_light_option(request, "output", "sun_samples")),
            tone="linear",
            keep_components=True,
            sky_ambient=_sky_ambient(_light_option(request, "preview", "sky_ambient")),
            roughness_deg=_roughness(_light_option(request, "output", "roughness_deg")),
            **earth,
        )
        started = time.time()
        with self._render_slot():
            with self._rendering():
                render = moonlib_s.render_region(
                    view,
                    _f(selection, "lon", 0.0),
                    _f(selection, "lat", 0.0),
                    fov_radius_arcsec=_f(selection, "fov_radius_arcsec", 200.0),
                    rotation_deg=_f(selection, "rotation_deg", 0.0),
                    arcsec_per_pixel=_opt_f(output, "arcsec_per_pixel"),
                    settings=settings,
                    terrain=self.terrain(),
                    cancel=self._cancel_event.is_set,
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
            self._linear_meta = {
                "arcsec_per_pixel": float(render.stats["arcsec_per_pixel"]),
                "render_seconds": time.time() - started,
                "exposure_scale": exposure,
                "rotation_deg": float(render.stats.get("rotation_deg") or 0.0),
            }
            # Kept so that /api/process can describe the frame as fully as
            # /api/region does; the page reads the same fields from both.
            self._linear_context = {
                "region": render.region.to_dict(),
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
        # Reported the way the disk render reports it, so the pane that says how
        # the frame was produced reads the same fields from both endpoints.
        body["region"]["bands"] = int(render.stats.get("bands", 1))
        body["region"]["band_workers"] = int(render.stats.get("band_workers", 1))
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
                raise moonlib_s.MoonlibError(
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
                    raise moonlib_s.MoonlibError(
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
        image = moonlib_s.process_photograph(
            linear,
            None,
            train,
            exposure_scale=component_exposure,
            arcsec_per_pixel=arcsec_per_pixel,
            seeing_fwhm_arcsec=_f(atmosphere, "seeing", 1.0),
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
            noise_photons=photons,
            noise_sigma=sigma,
            noise_seed=noise_seed(linear_id, photons, sigma),
        )
        # The report has to describe the frame that was just made, so it is
        # handed the same radius the mask was built with rather than the
        # multiple it would otherwise re-derive.
        report = moonlib_s.instrument_report(
            train.telescope,
            arcsec_per_pixel=arcsec_per_pixel,
            seeing_fwhm_arcsec=_f(atmosphere, "seeing", 1.0),
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
            raise moonlib_s.MoonlibError("事件预报需要开始与结束时间。")
        if kind not in moonlib_s.EVENT_KINDS:
            raise moonlib_s.MoonlibError(
                f"未知的事件类型 {kind!r}；可选：{', '.join(moonlib_s.EVENT_KINDS)}。"
            )
        found = moonlib_s.moon_events(site, start, end, kinds=[kind])
        return {
            "kind": kind,
            "start": start,
            "end": end,
            "observer": {"lat": site.lat, "lon": site.lon},
            "events": [event.to_dict() for event in found],
        }

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
        start = str(request.get("start") or "")
        end = str(request.get("end") or "")
        if not start or not end:
            raise moonlib_s.MoonlibError("动画需要开始与结束时间。")
        step = _f(request, "step_seconds", 60.0)
        if not step > 0.0:
            raise moonlib_s.MoonlibError("时间步长必须大于 0 秒。")
        span = moonlib_s.to_et(end) - moonlib_s.to_et(start)
        if span < 0.0:
            raise moonlib_s.MoonlibError("动画的结束时间早于开始时间。")
        total = int(math.floor(span / step + 1e-9)) + 1
        if total > ANIMATION_MAX_FRAMES:
            raise moonlib_s.MoonlibError(
                f"这一动画有 {total} 帧，超过上限 {ANIMATION_MAX_FRAMES} 帧；"
                "把时间步长调大，或把起止范围缩短。"
            )

        output = request.get("output") or {}
        width = int(_f(output, "width", 1000))
        height = int(_f(output, "height", width))
        for side, value in (("宽", width), ("高", height)):
            if not MIN_PX <= value <= MAX_DISK_PX:
                raise moonlib_s.MoonlibError(
                    f"动画分辨率{side}必须在 {MIN_PX}–{MAX_DISK_PX} px 之间"
                    f"（收到 {value}）。"
                )
        earth = _earthshine(request.get("earthshine"))
        settings = MoonRenderSettings(
            width=width,
            height=height,
            samples_per_pixel=int(_f(output, "samples", 1)),
            shadow_steps=int(_f(output, "shadow_steps", 48)),
            photometric_model=_optical_model(
                _light_option(request, "output", "photometric_model")
            ),
            sun_samples=_sun_samples(_light_option(request, "output", "sun_samples")),
            tone="srgb",
            keep_components=True,
            sky_ambient=_sky_ambient(_light_option(request, "preview", "sky_ambient")),
            roughness_deg=_roughness(_light_option(request, "output", "roughness_deg")),
            **earth,
        )
        region = request.get("region")
        if region is not None and not isinstance(region, dict):
            raise moonlib_s.MoonlibError("region must be an object when it is given.")
        # The file's size is a separate question from the render's: frames can
        # be rendered at the scale the physics resolves and written at the size
        # the reader asked for.  One number for a square frame, a pair for one
        # whose shape is the selection's own; nothing is resampled when absent.
        scale_to = _animation_size(request.get("scale_to"))
        if scale_to is not None:
            sides = scale_to if isinstance(scale_to, list) else [scale_to]
            if not all(MIN_PX <= side <= MAX_DISK_PX for side in sides):
                raise moonlib_s.MoonlibError(
                    "动画输出分辨率必须在 "
                    f"{MIN_PX}–{MAX_DISK_PX} px 之间（收到 {scale_to}）。"
                )
        smooth_px = _f(request, "smooth_px", 0.0)
        if smooth_px < 0.0:
            raise moonlib_s.MoonlibError("动画的平滑半径不能是负数。")
        timestamp = _animation_timestamp(request.get("timestamp"))
        hdr = request.get("hdr") or {}
        fps = _f(request, "fps", 12.0)
        if not fps > 0.0:
            raise moonlib_s.MoonlibError("帧率必须大于 0。")
        apply_processing = bool(request.get("apply_processing"))
        if apply_processing and bool(hdr.get("on")):
            raise moonlib_s.MoonlibError(
                "「应用后期处理」与 HDR 不能同时开：后期处理链的终点是显示图，"
                "没有线性帧可以写成 HDR。"
            )
        processing = dict(request.get("processing") or {})
        atmosphere = dict(request.get("atmosphere") or {})
        train = parse_train(request) if apply_processing else None

        # The cancel flag is made *here*, before the thread exists: 终止 pressed
        # the instant after 开始动画 has to reach this job, and a flag created
        # inside the worker would leave that press pointing at the frame before
        # it.  The render slot is taken here too, so "one ray march at a time"
        # is decided by whoever asks, not by the order two threads wake up in.
        self._take_render_slot()
        cancel = threading.Event()
        try:
            with self._animation_lock:
                self._cancel_event = cancel
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
                }
                self._purge_animations()
            thread = threading.Thread(
                target=self._run_animation,
                args=(job_id, site, start, end, step, settings, fps,
                      bool(hdr.get("on")), float(_f(hdr, "stops", 3.0)),
                      region, cancel, scale_to, train, atmosphere, processing,
                      smooth_px, timestamp),
                daemon=True,
            )
            thread.start()
        except Exception:
            self._render_lock.release()
            raise
        return {"id": job_id, "state": "running", "total": int(total)}

    def _animation_dir(self) -> Path:
        directory = moonlib_s.cache_dir() / "animations"
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
                       cancel: threading.Event, scale_to: Optional[Any] = None,
                       train: Any = None, atmosphere: Optional[Dict[str, Any]] = None,
                       processing: Optional[Dict[str, Any]] = None,
                       smooth_px: float = 0.0,
                       timestamp: Optional[Dict[str, Any]] = None) -> None:
        """Render every frame and encode them, off the request thread.

        Owns the render slot and the cancel flag that :meth:`animation` took
        before it started this thread, and gives both back when it is done.
        """
        path = self._animation_dir() / f"moon-{job_id}.mp4"
        try:
            frames = moonlib_s.animation_frames(
                site, start, end, step, settings=settings, region=region,
                terrain=self.terrain(), cancel=cancel.is_set,
            )

            processor = (
                _frame_processor(train, atmosphere or {}, processing or {}, job_id)
                if train is not None else None
            )

            def counted():
                for frame in frames:
                    with self._animation_lock:
                        job = self._animations.get(job_id)
                        if job is not None:
                            job["frames"] += 1
                            job["message"] = frame.when_utc
                            job["progress"] = job["frames"] / max(1, job["total"])
                    yield processor(frame) if processor is not None else frame

            info = moonlib_s.write_animation(path, counted(), fps=fps, hdr=hdr,
                                           stops=hdr_stops, scale_to=scale_to,
                                           smooth_px=smooth_px, timestamp=timestamp)
            with self._animation_lock:
                job = self._animations.get(job_id)
                if job is not None:
                    job.update(
                        state="done", progress=1.0, message="完成",
                        path=str(path), seconds=float(info.get("seconds", 0.0)),
                        bytes=int(path.stat().st_size) if path.is_file() else 0,
                    )
        except moonlib_s.MoonlibError as error:
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
            self._render_lock.release()

    def animation_status(self, job_id: str) -> Dict[str, Any]:
        with self._animation_lock:
            job = self._animations.get(job_id)
            if job is None:
                raise moonlib_s.MoonlibError(f"没有这个动画任务：{job_id}")
            payload = dict(job)
        payload["url"] = f"/api/animation/{job_id}/file" if payload["state"] == "done" else None
        payload.pop("path", None)
        return payload

    def animation_file(self, job_id: str) -> Path:
        with self._animation_lock:
            job = self._animations.get(job_id)
            if job is None or job["state"] != "done" or not job.get("path"):
                raise moonlib_s.MoonlibError(f"动画 {job_id} 还没有准备好。")
            path = Path(job["path"])
        if not path.is_file():  # pragma: no cover - purged under our feet
            raise moonlib_s.MoonlibError(f"动画 {job_id} 的文件已经不在了。")
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
        with tempfile.TemporaryDirectory(prefix="moonlib_s-hdr-") as directory:
            path = Path(directory) / "hdr.jpg"
            info = moonlib_s.write_gain_map_jpeg(
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
            "schema": "moonlib_s-web-settings/1",
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
        if language not in moonlib_s.LANGUAGES:
            raise moonlib_s.MoonlibError(
                f"language must be one of {', '.join(moonlib_s.LANGUAGES)}; "
                f"got {language!r}."
            )
        classes = request.get("classes")
        if classes is not None:
            if not isinstance(classes, (list, tuple)):
                raise moonlib_s.MoonlibError("classes must be a list of class keys.")
            classes = [str(key) for key in classes]
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
        }
        if frame_name == "disk":
            when = request.get("when") or {}
            view = self.view(when.get("at") or None, parse_observer(when))
            preview = request.get("preview") or {}
            width = int(_f(preview, "width", PREVIEW_WIDTH))
            frame = moonlib_s.disk_frame(view, width, width)
        elif frame_name == "region":
            frame = self.cached_region_frame(request.get("render_id"))
        else:
            raise moonlib_s.MoonlibError(
                f"unknown frame {frame_name!r}; expected 'disk' or 'region'."
            )
        started = time.time()
        # Names go where the picture has them, not where the reference sphere
        # under it would: the renderer draws the height field, so a label placed
        # on the sphere drifts off its own feature by the relief there -- a
        # kilometre of it is 0.65 px on a 2400 px frame.
        options["terrain"] = self.terrain()
        payload = moonlib_s.annotate_frame(frame, self.catalogue(), **options).to_dict()
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
                raise moonlib_s.MoonlibError(wanted)
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
            raise moonlib_s.MoonlibError(wanted)
        return moonlib_s.region_frame(
            view,
            float(region["lon_deg"]),
            float(region["lat_deg"]),
            float(region["span_km"]),
            int(region["width"]),
            int(region["height"]),
            rotation,
            terrain=self.terrain(),
        )


class StaleRender(moonlib_s.MoonlibError):
    """The client asked to process a frame the server no longer holds."""


# ---------------------------------------------------------------------------
# settings files
# ---------------------------------------------------------------------------
class SettingsError(moonlib_s.MoonlibError):
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


def _frame_processor(
    train: Any,
    atmosphere: Dict[str, Any],
    processing: Dict[str, Any],
    job_id: str,
):
    """The 后期处理 chain, as a function from an animation frame to pixels.

    The same :func:`moonlib_s.process_photograph` call :meth:`MoonService.process`
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
            raise moonlib_s.MoonlibError(
                "「应用后期处理」需要渲染出的帧，而不是已经处理过的像素。"
            )
        sunlit = getattr(render, "sunlit", None)
        earthshine = getattr(render, "earthshine", None)
        if sunlit is None or earthshine is None:
            raise moonlib_s.MoonlibError(
                "这一帧没有保留 sunlit/earthshine 分量，无法应用后期处理。"
            )
        stats = getattr(render, "stats", None) or {}
        scale = float(stats.get("exposure_scale", 1.0))
        linear = (np.asarray(sunlit, dtype=np.float64)
                  + np.asarray(earthshine, dtype=np.float64)) * scale
        photons = _opt_f(processing, "noise_photons")
        sigma = _f(processing, "noise_sigma", 0.0)
        plate_scale = float(stats.get("arcsec_per_pixel") or 1.0)
        return moonlib_s.process_photograph(
            linear,
            None,
            train,
            exposure_scale=1.0,
            arcsec_per_pixel=plate_scale,
            seeing_fwhm_arcsec=_f(atmosphere, "seeing", 1.0),
            frame_selection_gain=_f(atmosphere, "frame_selection_gain", 1.0),
            blur_strength=_f(processing, "blur_strength", 1.0),
            diffraction=_f(processing, "diffraction", 0.0),
            sharpen_strength=_f(processing, "sharpen_strength", 1.0),
            **_sharpen_radius(processing, plate_scale),
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
            raise moonlib_s.MoonlibError(
                f"动画输出分辨率必须是两个整数（收到 {value!r}）。"
            ) from exc
    raise moonlib_s.MoonlibError(
        f"动画输出分辨率必须是一个数或一对数（收到 {value!r}）。"
    )


#: Where a time stamp may be burned in, as the page names the corners.
_TIMESTAMP_CORNERS = ("left-top", "right-top", "left-bottom", "right-bottom")


def _animation_timestamp(value: Any) -> Optional[Dict[str, Any]]:
    """The 含时间戳 block, or ``None`` when the reader did not ask for one."""
    if not value:
        return None
    if not isinstance(value, dict):
        raise moonlib_s.MoonlibError("时间戳设置必须是一个对象。")
    corner = str(value.get("corner") or "left-top")
    if corner not in _TIMESTAMP_CORNERS:
        raise moonlib_s.MoonlibError(
            f"时间戳位置只能是 {'/'.join(_TIMESTAMP_CORNERS)}（收到 {corner!r}）。"
        )
    margin = _f(value, "margin_px", 16.0)
    if margin < 0.0:
        raise moonlib_s.MoonlibError("时间戳边距不能是负数。")
    offset = _f(value, "utc_offset_hours", 0.0)
    return {"corner": corner, "margin_px": int(round(margin)),
            "utc_offset_hours": float(offset)}


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
    rows = [
        {
            "en": feature.name_en,
            "zh": feature.name_zh or "",
            "lat": round(float(feature.lat_deg), 4),
            "lon": round(float(feature.lon_deg), 4),
            "km": round(float(feature.diameter_km), 2),
            "class": feature_class(feature.kind, feature.diameter_km),
        }
        for feature in catalogue
    ]
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
    from moonlib_s.plotting import encode_png as _encode

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
        raise moonlib_s.MoonlibError(f"expected a data: URL for {what}.")
    from PIL import Image

    head, body = text.split(",", 1)
    raw = base64.b64decode(body) if ";base64" in head else body.encode("utf-8")
    try:
        return Image.open(io.BytesIO(raw)).convert(mode)
    except Exception as error:
        raise moonlib_s.MoonlibError(f"{what} could not be decoded: {error}") from error


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
            raise moonlib_s.MoonlibError(f"the HDR metadata is missing {names[0]!r}.")
        return default

    return moonlib_s.GainMap(
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


class Handler(BaseHTTPRequestHandler):
    service: MoonService = None  # type: ignore[assignment]
    server_version = "moonlib_s-web"

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter
        sys.stderr.write("  %s\n" % (fmt % args))

    # -- helpers --------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str,
              filename: Optional[str] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            # The animation is a file the reader asked for, so the browser must
            # save it under a name rather than render it in a tab.
            self.send_header(
                "Content-Disposition", f'attachment; filename="{filename}"'
            )
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:  # pragma: no cover - browser went away
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
            raise moonlib_s.MoonlibError(f"Malformed JSON body: {error}") from error

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
            return self._json(200, {"ok": True, "limits": self.service.limits()})
        if path == "/api/features":
            return self._json(200, self.service.features())
        if path == "/api/settings":
            return self._json(200, self.service.settings.list())
        match = re.fullmatch(r"/api/animation/(\d+)(/file)?", path)
        if match:
            job_id = match.group(1)
            if match.group(2):
                # The MP4 itself: a real file, not a data URL, because an
                # animation is tens of megabytes and base64 would inflate it.
                try:
                    file_path = self.service.animation_file(job_id)
                except moonlib_s.MoonlibError as error:
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

    def do_POST(self) -> None:  # noqa: N802    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            request = self._body()
            if path == "/api/disk":
                preview = request.get("preview") or {}
                image, geometry = self.service.disk(
                    request,
                    int(_f(preview, "width", PREVIEW_WIDTH)),
                    int(_f(preview, "samples", PREVIEW_SAMPLES)),
                    int(_f(preview, "shadow_steps", PREVIEW_SHADOW_STEPS)),
                )
                return self._json(200, disk_payload(image, geometry))
            if path == "/api/region":
                image, info, render_id, raw = self.service.region(request)
                return self._json(
                    200,
                    {"info": info, "render_id": render_id,
                     "image": _data_url(encode_png(image)),
                     "image_raw": _data_url(encode_png(raw))},
                )
            if path == "/api/cancel":
                # 终止: the page stops waiting *and* says so.  The render polls
                # this between bands, which is the difference between a button
                # that stops the work and one that only stops watching it.
                return self._json(200, self.service.cancel())
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
            if path == "/api/events":
                return self._json(200, self.service.events(request))
            if path == "/api/animation":
                return self._json(200, self.service.animation(request))
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
            return self._json(409, {"error": str(error), "stale": True})
        except moonlib_s.MoonlibError as error:
            return self._json(400, {"error": str(error)})
        except Exception as error:  # pragma: no cover - surfaced to the page
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
                for p, km in moonlib_s.global_dem_sources()
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
    session = moonlib_s.init_kernels()
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
