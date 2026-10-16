"""Turning a span of time into a rendered animation.

The point of this module is that an animation is *nothing but* a sequence of
ordinary renders.  Frame *i* is bit-for-bit what::

    render_moon(observer, t_i, settings=settings, terrain=terrain)

produces on its own, because that is literally the call that is made: the view
is resolved with :func:`moonlib_s.moonview.view_geometry` and handed to
:func:`moonlib_s.moonview.render_moon` exactly the way ``render_moon`` would have
resolved it itself.  Nothing is carried between frames -- no accumulator, no
temporal filter, no reused ray buffer.  The one thing that *is* shared is the
opened terrain, because every level is a memory map and opening it per frame
would re-read the manifest and rebuild the same objects for no numerical gain.
It is a memory and I/O choice, not a rendering one.

The other half is :func:`write_animation`, which feeds those frames to
:func:`moonlib_s.encode.write_mp4` -- lazily, so a long animation never has to
exist in memory all at once -- and picks the right pixels for the requested
dynamic range: the display image for SDR, and the *linear* radiance field for
HDR, where the render's own exposure sets the scale of diffuse white.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union

import numpy as np

from .caption import stamp_frame, stamp_text
from .encode import resize_frame, smooth_frame, write_mp4
from .errors import MoonlibError
from .models import MoonRenderSettings
from .moonview import region_terrain, render_moon, render_region, view_geometry
from .observer import Observer
from .terrain import open_terrain
from .timeutil import as_time_range, to_utc_iso

__all__ = ["AnimationFrame", "animation_frames", "write_animation"]


@dataclass(frozen=True)
class AnimationFrame:
    """One rendered instant of an animation.

    Attributes
    ----------
    et
        The instant as SPICE ephemeris time (TDB seconds past J2000), the value
        every astronomical computation in the library actually uses.
    when_utc
        The same instant as an ISO-8601 UTC string, for filenames, captions and
        JSON payloads.
    render
        A :class:`moonlib_s.models.MoonRender` -- ``.image`` is the display frame
        and ``.sunlit`` / ``.earthshine`` are the two physical components the
        HDR path combines.
    """

    et: float
    when_utc: str
    render: Any

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe summary: no pixels, but enough to caption or log a frame."""
        render = self.render
        stats = getattr(render, "stats", None) or {}
        view = getattr(render, "view", None)
        payload: Dict[str, Any] = {
            "et": float(self.et),
            "when_utc": self.when_utc,
            "width": int(getattr(render, "width", 0) or 0),
            "height": int(getattr(render, "height", 0) or 0),
        }
        for key in (
            "exposure_scale",
            "earthshine_ratio",
            "earthshine_magnitude",
            "earth_illuminated_percent",
            "illuminated_fraction",
        ):
            if key in stats:
                payload[key] = stats[key]
        if view is not None:
            payload["phase_angle_deg"] = float(view.phase_angle_deg)
            payload["illuminated_fraction"] = float(view.illuminated_fraction)
        region = getattr(render, "region", None)
        if region is not None:
            payload["region"] = region.to_dict()
        return payload


# ---------------------------------------------------------------------------
# timing
# ---------------------------------------------------------------------------
def _step_seconds(step: Union[float, int, timedelta]) -> float:
    """A positive step in seconds, from a number or a :class:`timedelta`."""
    if isinstance(step, timedelta):
        value = step.total_seconds()
    elif isinstance(step, bool):
        raise MoonlibError("step must be seconds or a datetime.timedelta, not a bool.")
    elif isinstance(step, (int, float)):
        value = float(step)
    else:
        raise MoonlibError(
            "step must be seconds (int or float) or a datetime.timedelta; got "
            f"{type(step).__name__}."
        )
    if not math.isfinite(value) or value <= 0.0:
        raise MoonlibError(f"step must be a positive number of seconds; got {value!r}.")
    return value


def _cancelled(cancel: Optional[Any]) -> bool:
    """Whether the caller has asked to stop."""
    if cancel is None:
        return False
    return bool(cancel())


# ---------------------------------------------------------------------------
# target regions
# ---------------------------------------------------------------------------
#: The four mutually exclusive ways :func:`moonlib_s.moonview.render_region`
#: accepts a field of view.
_FOV_KEYS: Tuple[str, ...] = (
    "fov_radius_arcsec",
    "span_km",
    "fov_radius_km",
    "cover_km",
)

#: Everything a region request may forward to ``render_region``.
_REGION_KEYS = _FOV_KEYS + ("rotation_deg", "arcsec_per_pixel", "width", "height", "margin")


@dataclass(frozen=True)
class _RegionRequest:
    """A resolved close-up target: where to point, and how wide."""

    lon: float
    lat: float
    options: Dict[str, Any] = field(default_factory=dict)


def _region_request(region: Any) -> Optional[_RegionRequest]:
    """Coerce the *region* argument of :func:`animation_frames`.

    Two spellings are accepted and both are documented in the public docstring:

    a mapping
        ``{"lon": ..., "lat": ..., "fov_radius_arcsec": ...}``.  ``lon`` and
        ``lat`` are required, and exactly one of ``fov_radius_arcsec``,
        ``span_km``, ``fov_radius_km`` or ``cover_km`` says how much sky the
        frame covers.  ``rotation_deg``, ``arcsec_per_pixel``, ``width``,
        ``height`` and ``margin`` are forwarded when present; keys with a
        ``None`` value are ignored.
    a sequence
        ``(lon, lat)``, ``(lon, lat, fov_radius_arcsec)`` or
        ``(lon, lat, fov_radius_arcsec, rotation_deg)`` -- the shorthand for the
        common case, in the order the arguments appear on ``render_region``.
    """
    if region is None:
        return None
    if isinstance(region, Mapping):
        values = dict(region)
        if "lon" not in values or "lat" not in values:
            raise MoonlibError("region needs both 'lon' and 'lat' (degrees).")
        lon = float(values.pop("lon"))
        lat = float(values.pop("lat"))
        unknown = sorted(set(values) - set(_REGION_KEYS))
        if unknown:
            raise MoonlibError(
                "region has unsupported key(s): "
                + ", ".join(unknown)
                + ". Supported: lon, lat, "
                + ", ".join(_REGION_KEYS)
                + "."
            )
        options = {key: value for key, value in values.items() if value is not None}
    elif isinstance(region, Sequence) and not isinstance(region, (str, bytes)):
        if not 2 <= len(region) <= 4:
            raise MoonlibError(
                "a region tuple is (lon, lat), (lon, lat, fov_radius_arcsec) or "
                f"(lon, lat, fov_radius_arcsec, rotation_deg); got {len(region)} "
                "elements."
            )
        lon = float(region[0])
        lat = float(region[1])
        options = {}
        if len(region) >= 3:
            options["fov_radius_arcsec"] = float(region[2])
        if len(region) >= 4:
            options["rotation_deg"] = float(region[3])
    else:
        raise MoonlibError(
            "region must be a mapping with 'lon'/'lat' or a "
            "(lon, lat[, fov_radius_arcsec[, rotation_deg]]) tuple; got "
            f"{type(region).__name__}."
        )
    if not (math.isfinite(lon) and math.isfinite(lat)):
        raise MoonlibError("region lon/lat must be finite degrees.")
    if not any(key in options for key in _FOV_KEYS):
        raise MoonlibError(
            "region needs one of fov_radius_arcsec, span_km, fov_radius_km or "
            "cover_km to say how much sky the close-up covers."
        )
    return _RegionRequest(lon=lon, lat=lat, options=options)


#: What an animation meters its frames on.  See
#: :attr:`moonlib_s.models.MoonRenderSettings.exposure_meter`.
_ANIMATION_EXPOSURE_METER = "top_mean"

#: The region keys that fix a *ground* size, and so cannot drift with distance.
_GROUND_FOV_KEYS = ("span_km", "fov_radius_km")

#: How many instants of a run are measured when its one terrain window is cut.
#: Two -- the ends -- is not enough: the libration turns over inside a span of a
#: few days, so a frame in the *middle* of a run can lean further than either
#: end, cover a larger lon/lat box and fall outside a window cut from the ends.
#: It is then re-cut for that frame alone, on a different grid, which is the
#: whole defect this function exists to prevent.  Sampling the span costs one
#: coverage box per instant and no raster reads.
_WINDOW_PROBE_VIEWS = 17


def _window_probe(items: Sequence[Any]) -> List[Any]:
    """At most :data:`_WINDOW_PROBE_VIEWS` of *items*, always including the ends."""
    if len(items) <= _WINDOW_PROBE_VIEWS:
        return list(items)
    step = (len(items) - 1) / float(_WINDOW_PROBE_VIEWS - 1)
    picked = {int(round(index * step)) for index in range(_WINDOW_PROBE_VIEWS)}
    picked.add(0)
    picked.add(len(items) - 1)
    return [items[index] for index in sorted(picked)]


def _region_window(
    target: "_RegionRequest", views: Sequence[Any],
    options: MoonRenderSettings, terrain: Any,
) -> Optional[Any]:
    """One high-resolution terrain window covering the whole run, or ``None``.

    ``None`` is the honest answer in two cases: no high-resolution raster is on
    disk, or the baked pyramid is already as good as what is there.  Every way of
    stating the field of view is resolved to **ground kilometres** here, with the
    first view's geometry, so that the window exists before the first frame is
    traced: ``cover_km`` (a feature diameter) is the one that needs the frame's
    own geometry to turn into a span, and a run that left it unresolved would cut
    a fresh window for every frame -- a window's grid is anchored to the source
    raster's pixels, so each of those frames samples the ground at a different
    offset and the shadows move with it.  A single render still resolves its own
    span frame by frame; only a *run* pins it.
    """
    kwargs = {
        key: target.options[key]
        for key in _GROUND_FOV_KEYS + ("fov_radius_arcsec",)
        if key in target.options
    }
    if not kwargs and "cover_km" in target.options:
        # Same measurement render_region makes for a one-off frame, made once.
        from .moonview import required_sky_span_km

        span = required_sky_span_km(
            views[0], target.lon, target.lat,
            float(target.options["cover_km"]),
            margin=float(target.options.get("margin") or 1.25),
            terrain=terrain,
        )
        kwargs = {"span_km": float(span)}
    if not kwargs:
        return None
    return region_terrain(
        _window_probe(views), target.lon, target.lat,
        rotation_deg=float(target.options.get("rotation_deg") or 0.0),
        settings=options, terrain=terrain, **kwargs,
    )


def _pinned_sampling(
    options: MoonRenderSettings, stats: Mapping
) -> MoonRenderSettings:
    """The first frame's sampling, as settings that later frames cannot change.

    ``pyramid_level`` fixes which terrain level every frame reads, and pinning
    both ends of the ray-step range makes :func:`_estimate_ray_steps` return one
    number for the whole run instead of one per frame.  The albedo level follows
    the same ground sample distance, which the pinned ground span already fixes.
    """
    updates: Dict[str, Any] = {}
    level = stats.get("pyramid_level")
    if isinstance(level, (int, float)):
        updates["pyramid_level"] = int(level)
    steps = stats.get("steps_budget")
    if isinstance(steps, (int, float)) and float(steps) > 0:
        updates.update(
            ray_steps=int(steps), ray_steps_min=int(steps), ray_steps_max=int(steps)
        )
    return replace(options, **updates) if updates else options


def _pinned_request(request: Dict[str, Any], stats: Mapping) -> Dict[str, Any]:
    """Replace an *angular* field of view with the ground span it worked out to.

    ``fov_radius_arcsec`` says how much *sky* the frame covers, so the ground a
    pixel covers grows and shrinks with the Moon's distance -- and that is what
    picks the terrain level, so a run of frames can step from one level to the
    next in the middle of the animation.  The first frame's own span, in
    kilometres, is a number that does not move.  ``cover_km`` is pinned the same
    way for the same reason: it is a diameter, and turning it into a span needs
    the frame's geometry, which drifts.
    """
    angular = "fov_radius_arcsec" in request
    if not angular and "cover_km" not in request:
        return request
    span = stats.get("span_km")
    if not isinstance(span, (int, float)) or not float(span) > 0.0:
        return request
    pinned = {
        key: value for key, value in request.items()
        if key not in ("fov_radius_arcsec", "cover_km")
    }
    pinned["span_km"] = float(span)
    return pinned


# ---------------------------------------------------------------------------
# frames
# ---------------------------------------------------------------------------
def animation_frames(
    observer: Any,
    start: Any,
    end: Any,
    step: Union[float, int, timedelta],
    *,
    settings: Optional[MoonRenderSettings] = None,
    region: Any = None,
    terrain: Optional[Any] = None,
    cancel: Optional[Any] = None,
) -> Iterator[AnimationFrame]:
    """Render the instants from *start* to *end* inclusive, one frame each.

    Parameters
    ----------
    observer
        Observing site; anything :meth:`moonlib_s.Observer.from_value` accepts.
    start, end
        Anything :func:`moonlib_s.timeutil.to_et` accepts, in either order of
        precision; they are interpreted as a closed interval
        (``moonlib_s.timeutil.as_time_range``), so a span that is an exact
        multiple of *step* ends on *end* rather than a step past it.
    step
        Sampling interval in seconds (``int``/``float``) or as a
        :class:`datetime.timedelta`.  Must be positive.
    settings
        :class:`moonlib_s.models.MoonRenderSettings`, shared by every frame.  The
        instant is **not** part of the settings: it is carried by the
        :class:`moonlib_s.models.MoonView` built for each frame, exactly as
        ``render_moon`` does.
    region
        When given, each frame is a telescope close-up instead of the whole
        disk, rendered with :func:`moonlib_s.moonview.render_region`.  Either a
        mapping ``{"lon", "lat", "fov_radius_arcsec", "rotation_deg", ...}`` or
        a tuple ``(lon, lat[, fov_radius_arcsec[, rotation_deg]])``; see
        :func:`_region_request` for the exact keys.
    terrain
        An already opened :class:`moonlib_s.terrain.MoonTerrain`.  ``None`` opens
        the global products once, on the first frame, and reuses them for the
        rest of the animation.  **This is a memory and I/O choice, not a
        numerical one**: every level is a memory map, so a fresh open per frame
        would read exactly the same bytes and produce exactly the same pixels.
    cancel
        Optional ``callable() -> bool``, polled before every frame and forwarded
        to the renderer so a long frame can be interrupted between bands.  When
        it reports true the generator stops with
        ``MoonlibError("animation cancelled")``.

    Yields
    ------
    AnimationFrame
        One per instant: ``start``, ``start + step``, ... up to ``end``.

    Raises
    ------
    MoonlibError
        For a non-positive *step*, an unusable *region*, or a cancellation.
    """
    seconds = _step_seconds(step)
    options = settings or MoonRenderSettings()
    # A run is metered with the steady statistic, not the rank one: the 99.9th
    # percentile steps whenever a pixel crosses the cut, and in a sequence that
    # shows up as the whole frame jumping back to a brightness it already had.
    # Measured over one waxing gibbous: four backwards steps of up to 1% with
    # the percentile, worst backwards step 0.02% with the mean of the top 1%.
    options = replace(options, exposure_meter=_ANIMATION_EXPOSURE_METER)
    window = as_time_range(start, end)
    instants = window.grid(seconds)
    target = _region_request(region)
    site = Observer.from_value(observer)

    def views_for(instants: Sequence[float]) -> list:
        return [
            view_geometry(site, float(instant),
                          earth_albedo=options.earth_geometric_albedo)
            for instant in instants
        ]

    def generator() -> Iterator[AnimationFrame]:
        # Opened once, on the first frame, and handed to every render after it.
        shared = terrain if terrain is not None else open_terrain()
        pinned = options
        request = dict(target.options) if target is not None else {}
        # One terrain window for the whole run.  Frames cannot each cut their
        # own: a window's grid is anchored to the source raster's pixels, so as
        # the footprint drifts the window slides by whole source pixels and the
        # terrain under the frame changes with it -- which at a grazing sun is
        # visible as shadows that jump between frames.
        if target is not None:
            run = _region_window(target, views_for(_window_probe(instants)),
                                 options, shared)
            if run is not None:
                shared = run
        # The anchor the last frame was exposed on, handed to the next one so
        # the sequence cannot jump: any per-frame statistic eventually steps --
        # near new Moon it is measured from a couple of pixels -- and the whole
        # picture's brightness steps with it.  See EXPOSURE_SLEW_MAG.
        hint: Optional[float] = None
        for index, instant in enumerate(instants):
            if _cancelled(cancel):
                raise MoonlibError("animation cancelled")
            et = float(instant)
            # The same call render_moon makes for itself when no view is given,
            # so a frame is bit-for-bit an independent render of this instant.
            view = view_geometry(site, et, earth_albedo=options.earth_geometric_albedo)
            try:
                if target is None:
                    render = render_moon(
                        view=view,
                        settings=pinned,
                        terrain=shared,
                        cancel=cancel,
                        exposure_hint=hint,
                    )
                else:
                    render = render_region(
                        view,
                        target.lon,
                        target.lat,
                        settings=pinned,
                        terrain=shared,
                        cancel=cancel,
                        exposure_hint=hint,
                        **request,
                    )
            except MoonlibError as error:
                # render_moon reports cancellation in the server's own wording;
                # this generator has one message for it, so the transport layer
                # can map a stopped animation onto a single response.
                if _cancelled(cancel):
                    raise MoonlibError("animation cancelled") from error
                raise
            if index == 0:
                # Pin the *sampling* to what the first frame chose, so no later
                # frame can silently re-sample.  The terrain level, the albedo
                # level and the shell-march budget are all derived from the
                # frame's own geometry; a frame that gets different ones is not
                # a slightly different picture of the same scene, it is a
                # different sampling of it.  This holds for the whole disk as
                # much as for a close-up: nothing about a run should be
                # re-decided halfway through it.
                pinned = _pinned_sampling(options, render.stats)
                if target is not None:
                    request = _pinned_request(request, render.stats)
            anchor = (render.stats or {}).get("exposure_anchor")
            if isinstance(anchor, (int, float)) and float(anchor) > 0.0:
                hint = float(anchor)
            yield AnimationFrame(et=et, when_utc=to_utc_iso(et), render=render)

    return generator()


# ---------------------------------------------------------------------------
# encoding
# ---------------------------------------------------------------------------
def _display_frame(item: Any) -> Any:
    """The SDR pixels of one item: a render's display image, or the item itself."""
    if isinstance(item, AnimationFrame):
        image = getattr(item.render, "image", None)
        if image is None:
            raise MoonlibError(
                "write_animation: this AnimationFrame carries no render.image."
            )
        return image
    return item


def _linear_frame(item: Any, stops: float = 3.0) -> Any:
    """Scene-linear pixels, with ``stops`` of headroom above the SDR white.

    For an :class:`AnimationFrame` the frame is the physical radiance of the
    surface, ``sunlit + earthshine``, multiplied by the exposure the render
    itself chose (``stats["exposure_scale"]``).  That is exactly the array the
    SDR tone curve was applied to, so 1.0 means "the brightest sunlit pixel"
    -- the SDR reference white -- and the PQ encoder maps it to 203 cd/m^2.

    The extra ``2 ** stops`` factor is what makes the HDR file different from
    the SDR one.  Each frame is already normalised by the renderer's own
    auto-exposure, so its bright end sits at 1.0; multiplying by one *constant*
    (rather than re-anchoring every frame) puts each frame's bright end at
    ``2 ** stops`` and keeps the sequence steady -- a per-frame anchor would
    make the whole animation pulse as the terminator sweeps.  It is the same
    rule :func:`moonlib_s.hdr.sdr_hdr_pair` applies to a single picture, and for
    the same reason: without it the gain map is empty and the "HDR" video is the
    SDR video with a different transfer function.

    The tone curve itself is *not* applied: PQ wants linear light.

    Two caveats are worth stating rather than hiding.  The components only
    exist when ``keep_components`` is on (the default), and with
    ``auto_exposure=False`` the scale is just ``2**exposure_ev``, so 1.0 is
    then one unit of radiance factor rather than a display white.

    A plain array is passed through unchanged; it is then the caller's job to
    hand over linear light *and* to put it at the exposure it wants.
    """
    if not isinstance(item, AnimationFrame):
        return item
    render = item.render
    sunlit = getattr(render, "sunlit", None)
    earthshine = getattr(render, "earthshine", None)
    if sunlit is None or earthshine is None:
        raise MoonlibError(
            "write_animation(hdr=True) needs the render's sunlit and earthshine "
            "components; render with MoonRenderSettings(keep_components=True), "
            "or pass plain linear-light arrays instead of AnimationFrames."
        )
    stats = getattr(render, "stats", None) or {}
    scale = float(stats.get("exposure_scale", 1.0))
    total = np.asarray(sunlit, dtype=np.float64) + np.asarray(earthshine, dtype=np.float64)
    return total * scale * (2.0 ** float(stops))


def write_animation(
    path: Any,
    frames: Any,
    *,
    fps: float = 24.0,
    hdr: bool = False,
    stops: float = 3.0,
    scale_to: Optional[Any] = None,
    smooth_px: float = 0.0,
    timestamp: Optional[Dict[str, Any]] = None,
    ffmpeg: Optional[str] = None,
) -> Dict[str, Any]:
    """Encode *frames* to an MP4 at *path*.

    Parameters
    ----------
    path
        Output file.
    frames
        An iterable of :class:`AnimationFrame` or of plain arrays.  It is
        consumed lazily: the frames are converted and piped to ffmpeg one at a
        time by :func:`moonlib_s.encode.write_mp4`, so the animation never has to
        be held in memory.
    fps
        Playback rate.
    hdr
        ``False`` writes the display image with 8-bit H.264.  ``True`` writes
        the *linear* frame (see :func:`_linear_frame`) as 10-bit HDR10 HEVC.
    stops
        How far above the SDR white an HDR frame is allowed to go, in stops --
        the same control, and the same meaning, as the one on a single-picture
        HDR save.  Ignored when *hdr* is false.
    scale_to
        Write the frames at this size -- one number for a square frame, or a
        ``(width, height)`` pair -- resampling them if they were rendered at
        another (see :func:`moonlib_s.encode.resize_frame`).  The renderer's size
        and the file's size are two different questions: the first is how much
        detail is resolved, the second how large the picture is.
    smooth_px
        Blur each frame by this many of its own pixels before writing it (see
        :func:`moonlib_s.encode.smooth_frame`).  Applied after the resize and
        before the time stamp, so the caption stays crisp.
    timestamp
        Draw the frame's own instant into one corner.  A mapping with
        ``corner`` (``left-top``/``right-top``/``left-bottom``/
        ``right-bottom``), ``margin_px`` and ``utc_offset_hours``; the text comes
        from :func:`moonlib_s.caption.stamp_text`, so the format is fixed.  It
        needs :class:`AnimationFrame`s -- a plain array carries no instant, and
        asking for a stamp on one is an error rather than a silent omission.
    ffmpeg
        Explicit ffmpeg executable, forwarded to :func:`moonlib_s.encode.write_mp4`.

    Returns
    -------
    dict
        The encoder's summary: ``{"path", "frames", "fps", "seconds", "width",
        "height", "codec", "hdr"}``.
    """
    def convert(item: Any) -> Any:
        """One item, all the way to the pixels that go into the file.

        The order is the order the operations mean: the frame as rendered, then
        the size the file wants (which is where area-averaging happens), then
        极小尺度平滑, then the caption.  A stamp drawn before the resize would be
        resized with the picture and land a different number of pixels from the
        edge on every output size -- and its margin is stated in the file's own
        pixels.
        """
        frame = _linear_frame(item, stops) if hdr else _display_frame(item)
        if scale_to is not None:
            frame = resize_frame(frame, scale_to)
        if smooth_px:
            frame = smooth_frame(frame, smooth_px)
        if timestamp:
            frame = stamp_frame(
                frame,
                stamp_text(_frame_instant(item), timestamp.get("utc_offset_hours", 0.0)),
                corner=str(timestamp.get("corner") or "left-top"),
                margin_px=int(timestamp.get("margin_px", 16)),
            )
        return frame

    stream: Any = (convert(item) for item in frames)
    return write_mp4(path, stream, fps=fps, hdr=hdr, ffmpeg=ffmpeg)


def _frame_instant(item: Any) -> str:
    """The instant of one item, for its caption."""
    when = getattr(item, "when_utc", None)
    if when is None:
        raise MoonlibError(
            "write_animation(timestamp=...) needs AnimationFrames: a plain array "
            "has no instant to write into the corner."
        )
    return str(when)
