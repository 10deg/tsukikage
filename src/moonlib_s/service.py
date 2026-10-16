"""Service façade: the stable JSON contract for non-Python front ends.

This module is the **only** thing a future HTTP API, a mobile app or a desktop
shell needs to depend on.  It contains no astronomy of its own — every function
is a thin adapter that

1. normalises loose input (``"2026-05-25"``, ``"30,120"``, dicts from a web
   request) into library objects,
2. calls the domain layer,
3. returns a plain ``dict`` that ``json.dumps`` accepts without further help.

Payload shape
-------------
Every response carries the same envelope, so a client can parse it generically::

    {
      "schema": "1.0",
      "kind": "lunar_report",
      "generated_utc": "2026-05-25T15:30:00.000Z",
      "request": { ...echo of normalised inputs... },
      "data": { ...kind-specific payload... }
    }

Errors are **not** swallowed: the transport layer decides whether a
:class:`~moonlib_s.errors.MoonlibError` becomes HTTP 400 or 500.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Sequence

from ._version import __version__
from .crescent import CrescentCriteria, ScoreWeights, find_crescent_windows
from .errors import MoonlibError
from .kernels import describe as describe_kernels
from .models import jsonable
from .observer import Observer
from .timeutil import TimeRange, as_time_range, to_utc_iso, utc_now

__all__ = [
    "SCHEMA_VERSION",
    "parse_observer",
    "envelope",
    "lunar_state",
    "lunar_report",
    "phase_calendar",
    "crescent_windows",
    "illumination_matches",
    "limb_targets",
    "limb_target_sweep",
    "analemma",
    "moon_view",
    "moon_region",
    "moon_feature",
    "list_features",
    "terrain_info",
    "environment",
]

#: Version of the payload contract.  Bump on any breaking field change.
SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# input normalisation
# ---------------------------------------------------------------------------
def parse_observer(value: Any = None) -> Observer:
    """Coerce the many ways a client may express a site into an Observer.

    Accepts an :class:`~moonlib_s.observer.Observer`, ``None`` (the default
    site), a mapping with ``lat``/``lon``/``height_km``/``name`` keys, a
    ``(lat, lon[, height])`` sequence, or the string ``"lat,lon[,height]"``.
    """
    if value is None:
        return Observer()
    if isinstance(value, Observer):
        return value
    if isinstance(value, str):
        parts = [piece.strip() for piece in value.split(",") if piece.strip()]
        if len(parts) < 2:
            raise MoonlibError(
                f"Cannot parse observer string {value!r}; expected 'lat,lon[,height_km]'"
            )
        try:
            numbers = [float(piece) for piece in parts]
        except ValueError as exc:
            raise MoonlibError(
                f"Cannot parse observer string {value!r}; the parts must be "
                f"numbers: 'lat,lon[,height_km]'"
            ) from exc
        return Observer(
            lat=numbers[0],
            lon=numbers[1],
            height_km=numbers[2] if len(numbers) > 2 else 0.0,
        )
    if isinstance(value, dict):
        return Observer(
            lat=float(value.get("lat", value.get("latitude", 0.0))),
            lon=float(value.get("lon", value.get("longitude", 0.0))),
            height_km=float(value.get("height_km", value.get("height", 0.0))),
            name=str(value.get("name", "")),
        )
    return Observer.from_value(value)


def envelope(kind: str, request: Dict[str, Any], data: Any) -> Dict[str, Any]:
    """Wrap a payload in the standard service envelope."""
    return {
        "schema": SCHEMA_VERSION,
        "library_version": __version__,
        "kind": kind,
        "generated_utc": to_utc_iso(_now_et()),
        "request": jsonable(request),
        "data": jsonable(data),
    }


def _now_et() -> float:
    from .timeutil import to_et

    return to_et(utc_now())


def _range_payload(window: TimeRange) -> Dict[str, Any]:
    return window.as_dict()


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------
def lunar_state(
    observer: Any = None,
    when: Any = None,
) -> Dict[str, Any]:
    """Instantaneous lunar state (no rise/set search)."""
    from .moon import moon_state

    site = parse_observer(observer)
    state = moon_state(site, when)
    return envelope(
        "lunar_state",
        {"observer": site.as_dict(), "when_utc": to_utc_iso(state.et)},
        state.to_dict(),
    )


def lunar_report(
    observer: Any = None,
    when: Any = None,
    *,
    rise_set_mode: str = "observational",
    with_transit_details: bool = True,
) -> Dict[str, Any]:
    """Full report: state plus moonrise, transit and moonset."""
    from .moon import moon_report

    site = parse_observer(observer)
    report = moon_report(
        site,
        when,
        rise_set_mode=rise_set_mode,
        with_transit_details=with_transit_details,
    )
    return envelope(
        "lunar_report",
        {
            "observer": site.as_dict(),
            "when_utc": to_utc_iso(report.state.et),
            "rise_set_mode": rise_set_mode,
        },
        report.to_dict(),
    )


def phase_calendar(
    observer: Any = None,
    period: Any = None,
    *,
    kinds: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Catalogue of new/first-quarter/full/last-quarter events."""
    from .models import PhaseKind
    from .phases import phase_calendar as _phase_calendar

    site = parse_observer(observer)
    window = as_time_range(period if period is not None else utc_now().year)
    requested = None
    if kinds:
        requested = tuple(PhaseKind(name) for name in kinds)
    entries = _phase_calendar(window, site, kinds=requested)
    return envelope(
        "phase_calendar",
        {
            "observer": site.as_dict(),
            "range": _range_payload(window),
            "kinds": [kind.value for kind in (requested or tuple(PhaseKind))],
        },
        [entry.to_dict() for entry in entries],
    )


def crescent_windows(
    observer: Any = None,
    period: Any = None,
    *,
    criteria: Optional[Dict[str, Any]] = None,
    weights: Optional[Dict[str, Any]] = None,
    phase_mode: str = "geocentric",
) -> Dict[str, Any]:
    """Thin-crescent observing sessions with geometry and advisory verdicts."""
    site = parse_observer(observer)
    window = (
        as_time_range(period)
        if period is not None
        else TimeRange.utc_days(utc_now(), 365.0)
    )
    resolved_criteria = (
        CrescentCriteria(**criteria) if criteria else CrescentCriteria()
    )
    resolved_weights = ScoreWeights(**weights) if weights else ScoreWeights()
    sessions = find_crescent_windows(
        site,
        window,
        criteria=resolved_criteria,
        weights=resolved_weights,
        phase_mode=phase_mode,
    )
    return envelope(
        "crescent_windows",
        {
            "observer": site.as_dict(),
            "range": _range_payload(window),
            "criteria": jsonable(resolved_criteria),
            "weights": resolved_weights.as_dict(),
            "phase_mode": phase_mode,
        },
        [session.to_dict() for session in sessions],
    )


def illumination_matches(
    target_percent: float,
    observer: Any = None,
    period: Any = None,
    *,
    waxing: Optional[bool] = None,
    phase_mode: str = "topocentric",
    phase_tolerance_deg: float = 3.0,
) -> Dict[str, Any]:
    """Moons at a requested illumination, largest apparent diameter first."""
    from .phases import find_moons_by_illumination

    site = parse_observer(observer)
    window = as_time_range(period if period is not None else utc_now().year)
    matches = find_moons_by_illumination(
        target_percent,
        window,
        site,
        waxing=waxing,
        phase_mode=phase_mode,
        phase_tolerance_deg=phase_tolerance_deg,
    )
    return envelope(
        "illumination_matches",
        {
            "observer": site.as_dict(),
            "range": _range_payload(window),
            "target_percent": target_percent,
            "waxing": waxing,
            "phase_mode": phase_mode,
        },
        [match.to_dict() for match in matches],
    )


def limb_targets() -> Dict[str, Any]:
    """The packaged catalogue of lunar limb / polar targets."""
    from .libration import load_targets

    targets = load_targets()
    return envelope("limb_targets", {}, [target.to_dict() for target in targets])


def limb_target_sweep(
    observer: Any = None,
    period: Any = None,
    *,
    step_minutes: float = 30.0,
    min_grade: str = "marginal",
    require_illuminated: bool = True,
    min_moon_elevation_deg: Optional[float] = None,
    max_solar_incidence_deg: Optional[float] = None,
    summary: bool = True,
    include_samples: bool = False,
) -> Dict[str, Any]:
    """Sweep limb-target visibility over a period.

    Parameters
    ----------
    summary
        Include the per-target statistics table.
    include_samples
        Include the full per-step payload.  Off by default: a 90-day sweep at
        30-minute steps is over 4000 steps and would dominate the response.
    """
    from .libration import load_targets, summarize, sweep
    from .models import VisibilityGrade

    site = parse_observer(observer)
    window = as_time_range(period if period is not None else 30)
    targets = load_targets()
    grade = VisibilityGrade(min_grade)
    samples = sweep(targets, window, site, step_minutes=step_minutes)

    payload: Dict[str, Any] = {
        "n_steps": len(samples),
        "n_targets": len(targets),
        "step_minutes": step_minutes,
        "min_grade": grade.value,
        "counts": _grade_counts(samples, grade, require_illuminated,
                                min_moon_elevation_deg, max_solar_incidence_deg),
    }
    if summary:
        payload["summary"] = summarize(samples, targets, min_grade=grade)
    if include_samples:
        payload["samples"] = [sample.to_dict() for sample in samples]

    return envelope(
        "limb_target_sweep",
        {
            "observer": site.as_dict(),
            "range": _range_payload(window),
            "step_minutes": step_minutes,
            "min_grade": grade.value,
        },
        payload,
    )


def _grade_counts(
    samples: Sequence[Any],
    grade: Any,
    require_illuminated: bool,
    min_moon_elevation_deg: Optional[float],
    max_solar_incidence_deg: Optional[float],
) -> Dict[str, int]:
    """How many steps each target satisfied, without the full sample payload."""
    counts: Dict[str, int] = {}
    for sample in samples:
        for visibility in sample.visible(
            grade,
            require_illuminated=require_illuminated,
            min_moon_elevation_deg=min_moon_elevation_deg,
            max_solar_incidence_deg=max_solar_incidence_deg,
        ):
            name = visibility.target.english_name
            counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))


def analemma(
    observer: Any = None,
    period: Any = None,
    *,
    local_hour: int = 12,
    local_minute: int = 0,
    sample_days: Optional[Sequence[int]] = None,
) -> Dict[str, Any]:
    """Solar analemma samples plus their extremes."""
    from .analemma import analemma_extremes, solar_analemma

    site = parse_observer(observer)
    year = period if period is not None else utc_now().year
    window = as_time_range(year)
    points = solar_analemma(
        site,
        window,
        local_hour=local_hour,
        local_minute=local_minute,
        sample_days=sample_days,
    )
    return envelope(
        "analemma",
        {
            "observer": site.as_dict(),
            "range": _range_payload(window),
            "local_hour": local_hour,
            "local_minute": local_minute,
        },
        {
            "extremes": analemma_extremes(points),
            "points": [point.to_dict() for point in points],
        },
    )


def environment() -> Dict[str, Any]:
    """Library and kernel-pool description — useful as a health check."""
    info = describe_kernels()
    return envelope("environment", {}, info)


# ---------------------------------------------------------------------------
# binary payloads
# ---------------------------------------------------------------------------
def sky_chart_png(
    observer: Any = None,
    when: Any = None,
    *,
    at_local_sunset: bool = False,
    span_hours: float = 1.0,
    figsize: Sequence[float] = (12.0, 8.0),
    dpi: int = 110,
) -> bytes:
    """Render a sky chart and return PNG bytes.

    A future HTTP handler can stream the result directly; keeping the renderer
    here means the chart style is shared by the CLI, notebooks and the API.
    """
    from .plotting import close_figure, figure_to_png, plot_sky

    site = parse_observer(observer)
    figure = plot_sky(
        site,
        when,
        at_local_sunset=at_local_sunset,
        span_hours=span_hours,
        figsize=(float(figsize[0]), float(figsize[1])),
    )
    payload = figure_to_png(figure, dpi=dpi)
    close_figure(figure)
    return payload


def moon_view(
    observer: Any = None,
    when: Any = None,
    *,
    width: int = 1024,
    height: int = 1024,
    samples: int = 2,
    photometric_model: str = "lunar_lambert",
    tone: str = "srgb",
    exposure_ev: float = 0.0,
    auto_exposure: bool = True,
    earthshine: bool = True,
    earth_samples: int = 13,
    earthshine_gain: float = 1.0,
    earth_geometric_albedo: Optional[float] = None,
    earthshine_tint: Optional[Sequence[float]] = None,
    normal_albedo: Optional[float] = None,
    sun_samples: int = 1,
    shadow_steps: int = 48,
    ambient_fraction: float = 0.0,
    sky_ambient: float = 0.0,
    sky_samples: int = 26,
    roughness_deg: float = 0.0,
    pyramid_level: Optional[int] = None,
    asinh_softening: Optional[float] = None,
) -> Dict[str, Any]:
    """Geometry and photometry of a simulated whole-disk observation.

    This is the metadata half of :func:`moon_view_png`: everything a client
    needs in order to caption, plan or verify an image, including the earthshine
    irradiance ratio and how many magnitudes it sits below sunlight.  The pixel
    array itself is deliberately not part of the JSON contract.
    """
    from .models import MoonRenderSettings
    from .moonview import render_moon

    site = parse_observer(observer)
    settings = _render_settings(
        MoonRenderSettings,
        width=width,
        height=height,
        samples=samples,
        photometric_model=photometric_model,
        tone=tone,
        exposure_ev=exposure_ev,
        auto_exposure=auto_exposure,
        earthshine=earthshine,
        earth_samples=earth_samples,
        earthshine_gain=earthshine_gain,
        earth_geometric_albedo=earth_geometric_albedo,
        earthshine_tint=earthshine_tint,
        normal_albedo=normal_albedo,
        sun_samples=sun_samples,
        shadow_steps=shadow_steps,
        ambient_fraction=ambient_fraction,
        sky_ambient=sky_ambient,
        sky_samples=sky_samples,
        roughness_deg=roughness_deg,
        pyramid_level=pyramid_level,
        asinh_softening=asinh_softening,
        keep_components=False,
    )
    render = render_moon(site, when, settings=settings)
    request = {
        "observer": site.as_dict(),
        "when": to_utc_iso(render.view.et),
        "width": width,
        "height": height,
        "samples": samples,
        "photometric_model": photometric_model,
        "tone": tone,
    }
    return envelope("moon_view", request, render.to_dict())


def moon_view_png(
    observer: Any = None,
    when: Any = None,
    *,
    width: int = 1024,
    height: int = 1024,
    samples: int = 2,
    photometric_model: str = "lunar_lambert",
    tone: str = "srgb",
    exposure_ev: float = 0.0,
    auto_exposure: bool = True,
    earthshine: bool = True,
    earth_samples: int = 13,
    earthshine_gain: float = 1.0,
    earth_geometric_albedo: Optional[float] = None,
    earthshine_tint: Optional[Sequence[float]] = None,
    normal_albedo: Optional[float] = None,
    sun_samples: int = 1,
    shadow_steps: int = 48,
    ambient_fraction: float = 0.0,
    sky_ambient: float = 0.0,
    sky_samples: int = 26,
    roughness_deg: float = 0.0,
    pyramid_level: Optional[int] = None,
    asinh_softening: Optional[float] = None,
    limb_grid: bool = False,
) -> bytes:
    """Render the simulated lunar disk and return PNG bytes.

    Uses the dependency-free PNG encoder in :mod:`moonlib_s.plotting` unless
    *limb_grid* is set, which needs matplotlib to draw the graticule.
    """
    from .models import MoonRenderSettings
    from .moonview import render_moon
    from .plotting import encode_png

    site = parse_observer(observer)
    settings = _render_settings(
        MoonRenderSettings,
        width=width,
        height=height,
        samples=samples,
        photometric_model=photometric_model,
        tone=tone,
        exposure_ev=exposure_ev,
        auto_exposure=auto_exposure,
        earthshine=earthshine,
        earth_samples=earth_samples,
        earthshine_gain=earthshine_gain,
        earth_geometric_albedo=earth_geometric_albedo,
        earthshine_tint=earthshine_tint,
        normal_albedo=normal_albedo,
        sun_samples=sun_samples,
        shadow_steps=shadow_steps,
        ambient_fraction=ambient_fraction,
        sky_ambient=sky_ambient,
        sky_samples=sky_samples,
        roughness_deg=roughness_deg,
        pyramid_level=pyramid_level,
        asinh_softening=asinh_softening,
        keep_components=False,
    )
    render = render_moon(site, when, settings=settings)
    if not limb_grid:
        return encode_png(render.image)

    from .plotting import close_figure, figure_to_png, plot_moon_view

    figure = plot_moon_view(render, show_limb_grid=True)
    payload = figure_to_png(figure)
    close_figure(figure)
    return payload


def _render_settings(factory: Any, **kwargs: Any) -> Any:
    """Drop ``None`` overrides so the model defaults keep applying."""
    if "samples" in kwargs:
        kwargs["samples_per_pixel"] = kwargs.pop("samples")
    return factory(**{key: value for key, value in kwargs.items() if value is not None})


def list_features() -> Dict[str, Any]:
    """The catalogue of named landforms a close-up can be sized from."""
    from .features import load_features

    features = load_features()
    return envelope(
        "features",
        {"count": len(features)},
        {"features": [feature.to_dict() for feature in features]},
    )


def moon_feature(
    observer: Any = None,
    when: Any = None,
    *,
    feature: str,
    margin: float = 1.25,
    width: int = 1024,
    height: Optional[int] = None,
    samples: int = 2,
    photometric_model: str = "lunar_lambert",
    tone: str = "srgb",
    exposure_ev: float = 0.0,
    auto_exposure: bool = True,
    earthshine: bool = True,
    earth_geometric_albedo: Optional[float] = None,
    normal_albedo: Optional[float] = None,
    sun_samples: int = 1,
    shadow_steps: int = 48,
    terrain: Any = None,
    pyramid_level: Optional[int] = None,
) -> Dict[str, Any]:
    """Geometry of a close-up sized automatically to fit a named landform.

    The field of view is derived from the feature's published diameter and the
    measured geometry, so the frame neither crops the feature nor buries it.
    ``data.region.coverage_ratio`` reports how many times over it fits.
    """
    from .features import find_feature
    from .moonview import render_feature, view_geometry

    resolved = find_feature(feature)
    site = parse_observer(observer)
    settings = _region_settings(
        {
            "photometric_model": photometric_model,
            "tone": tone,
            "exposure_ev": exposure_ev,
            "auto_exposure": auto_exposure,
            "earthshine": earthshine,
            "earth_geometric_albedo": earth_geometric_albedo,
            "normal_albedo": normal_albedo,
            "sun_samples": sun_samples,
            "shadow_steps": shadow_steps,
            "pyramid_level": pyramid_level,
        },
        width,
        height if height is not None else width,
        samples,
    )
    view = view_geometry(site, when, earth_albedo=earth_geometric_albedo)
    render = render_feature(
        view, resolved, margin=margin, settings=settings, terrain=terrain
    )
    request = {
        "observer": site.as_dict(),
        "when": to_utc_iso(render.view.et),
        "feature": resolved.name_en,
        "margin": float(margin),
    }
    return envelope("moon_feature", request, render.to_dict())


def _region_settings(
    options: Dict[str, Any], width: int, height: int, samples: int
) -> Any:
    from .models import MoonRenderSettings

    return _render_settings(
        MoonRenderSettings,
        width=width,
        height=height,
        samples=samples,
        keep_components=False,
        **options,
    )


def _region_terrain(region_dem: Optional[str]) -> Any:
    if not region_dem:
        return None
    from .terrain import open_region

    return open_region(region_dem)


def moon_region(
    observer: Any = None,
    when: Any = None,
    *,
    lon: float = 0.0,
    lat: float = 0.0,
    span_km: float = 200.0,
    width: int = 1024,
    height: Optional[int] = None,
    samples: int = 2,
    photometric_model: str = "lunar_lambert",
    tone: str = "srgb",
    exposure_ev: float = 0.0,
    auto_exposure: bool = True,
    earthshine: bool = True,
    earth_geometric_albedo: Optional[float] = None,
    normal_albedo: Optional[float] = None,
    sun_samples: int = 1,
    shadow_steps: int = 48,
    region_dem: Optional[str] = None,
    pyramid_level: Optional[int] = None,
) -> Dict[str, Any]:
    """Geometry of a simulated close-up of one selenographic area.

    The metadata half of :func:`moon_region_png`, and the cheapest way to ask
    whether a feature is observable now, at what sun angle, and whether the
    terrain data actually supports the magnification.
    ``data.region.terrain_limited`` and ``data.region.resolution_ratio`` answer
    the last part; ``region_dem`` picks a high-resolution window fetched with
    ``tools/fetch_region_dem.py``.
    """
    from .moonview import render_region, view_geometry

    site = parse_observer(observer)
    settings = _region_settings(
        {
            "photometric_model": photometric_model,
            "tone": tone,
            "exposure_ev": exposure_ev,
            "auto_exposure": auto_exposure,
            "earthshine": earthshine,
            "earth_geometric_albedo": earth_geometric_albedo,
            "normal_albedo": normal_albedo,
            "sun_samples": sun_samples,
            "shadow_steps": shadow_steps,
            "pyramid_level": pyramid_level,
        },
        width,
        height if height is not None else width,
        samples,
    )
    view = view_geometry(site, when, earth_albedo=earth_geometric_albedo)
    render = render_region(
        view,
        float(lon),
        float(lat),
        span_km=float(span_km),
        settings=settings,
        terrain=_region_terrain(region_dem),
    )
    request = {
        "observer": site.as_dict(),
        "when": to_utc_iso(render.view.et),
        "lon": float(lon),
        "lat": float(lat),
        "span_km": float(span_km),
        "region_dem": region_dem,
    }
    return envelope("moon_region", request, render.to_dict())


def moon_region_png(
    observer: Any = None,
    when: Any = None,
    *,
    lon: float = 0.0,
    lat: float = 0.0,
    span_km: float = 200.0,
    width: int = 1024,
    height: Optional[int] = None,
    samples: int = 2,
    photometric_model: str = "lunar_lambert",
    exposure_ev: float = 0.0,
    auto_exposure: bool = True,
    earthshine: bool = True,
    earth_geometric_albedo: Optional[float] = None,
    normal_albedo: Optional[float] = None,
    sun_samples: int = 1,
    shadow_steps: int = 48,
    region_dem: Optional[str] = None,
    pyramid_level: Optional[int] = None,
) -> bytes:
    """Render a close-up and return PNG bytes."""
    from .moonview import render_region, view_geometry
    from .plotting import encode_png

    site = parse_observer(observer)
    settings = _region_settings(
        {
            "photometric_model": photometric_model,
            "exposure_ev": exposure_ev,
            "auto_exposure": auto_exposure,
            "earthshine": earthshine,
            "earth_geometric_albedo": earth_geometric_albedo,
            "normal_albedo": normal_albedo,
            "sun_samples": sun_samples,
            "shadow_steps": shadow_steps,
            "pyramid_level": pyramid_level,
        },
        width,
        height if height is not None else width,
        samples,
    )
    view = view_geometry(site, when, earth_albedo=earth_geometric_albedo)
    render = render_region(
        view,
        float(lon),
        float(lat),
        span_km=float(span_km),
        settings=settings,
        terrain=_region_terrain(region_dem),
    )
    return encode_png(render.image)


def terrain_info(cache: Optional[Any] = None) -> Dict[str, Any]:
    """Describe the global terrain products and their pyramid cache."""
    from pathlib import Path

    from .terrain import found_sources, terrain_cache_root

    sources = found_sources()
    resolved = Path(cache) if cache is not None else terrain_cache_root()
    payload: Dict[str, Any] = {
        "available": sources is not None,
        "cache_root": str(resolved),
    }
    if sources is None:
        return envelope("terrain_info", {}, payload)
    from .terrain import open_terrain

    terrain = open_terrain(bake=False)
    payload.update(terrain.as_dict())
    return envelope("terrain_info", {}, payload)


def dumps(payload: Dict[str, Any], *, indent: Optional[int] = None) -> str:
    """Serialise a service payload, guaranteeing it is JSON-safe."""
    return json.dumps(payload, ensure_ascii=False, indent=indent, allow_nan=False)
