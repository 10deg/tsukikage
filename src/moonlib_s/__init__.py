"""moonlib_s — an offline, self-contained lunar computation library.

Quick start
-----------
>>> import moonlib_s
>>> observer = moonlib_s.Observer(lat=30, lon=120)
>>> report = moonlib_s.moon_report(observer)
>>> print(moonlib_s.format_moon_report(report))

The SPICE kernel pool is loaded automatically on first use; call
:func:`init_kernels` explicitly only when you need a non-default kernel
directory or the long (DE441) ephemeris.

Layer map
---------
==========  ==========================================================
Layer       Modules
==========  ==========================================================
service     :mod:`service`, :mod:`cli`          (JSON contract, CLI)
domain      :mod:`phases`, :mod:`crescent`, :mod:`libration`, :mod:`analemma`,
            :mod:`moonview`                     (whole-disk observation simulation)
model       :mod:`moon`, :mod:`sun`, :mod:`riseset`, :mod:`illumination`, :mod:`frames`,
            :mod:`terrain`                      (global elevation and colour rasters)
foundation  :mod:`kernels`, :mod:`timeutil`, :mod:`observer`, :mod:`config`, :mod:`numerics`
presentation :mod:`reporting`, :mod:`plotting`, :mod:`anim`, :mod:`encode`,
            :mod:`hdr`                     (files: reports, charts, MP4, HDR JPEG)
==========  ==========================================================
"""

from __future__ import annotations

from typing import Any

from ._version import __version__
from .config import Settings, cache_dir, load_settings
from .errors import (
    ConfigurationError,
    KernelError,
    KernelNotFoundError,
    KernelNotLoadedError,
    MoonlibError,
    ObserverError,
    OutOfRangeError,
    PlottingUnavailableError,
    TargetDataError,
    TerrainDataError,
    TimeError,
)
from .kernels import (
    KernelSession,
    SpiceConstants,
    describe,
    init_kernels,
    is_initialized,
    kernel_session,
    loaded_kernels,
    shutdown_kernels,
    spice,
)
from .numerics import bisect_root, golden_minimum, wrap_angle
from .observer import Observer
from .timeutil import (
    SYNODIC_MONTH_DAYS,
    TimeRange,
    as_time_range,
    as_utc,
    format_local,
    format_offset,
    resolve_timezone,
    to_et,
    to_utc,
    to_utc_iso,
    utc_now,
)

from . import (  # noqa: F401
    features,
    frames,
    illumination,
    instrument,
    models,
    moon,
    phases,
    riseset,
    sun,
)
#   ^ imported for their side-effect-free presence as attributes; the public
#     names themselves are re-exported explicitly below.
from .events import EVENT_KINDS, MoonEvent, moon_events
from .frames import AzEl, RaDec, az_el, angular_separation, position_angle
from .instrument import (
    DEFAULT_CAMERA,
    DEFAULT_TELESCOPE,
    Camera,
    ImagingTrain,
    Telescope,
    add_sensor_noise,
    adjust_gain,
    arcsec_per_pixel,
    blur_budget,
    blur_image,
    diffraction_ring_kernel,
    diffraction_rings,
    desaturate,
    diffraction_core_fwhm_arcsec,
    effective_seeing_arcsec,
    finish_image,
    fried_parameter_cm,
    instrument_blur,
    instrument_report,
    instrument_sharpen,
    default_train,
    orient_image,
    process_photograph,
    psf_core_ratio,
    rotate_image,
    sharpen_image,
    sharpen_radius_arcsec,
)
from .illumination import (
    PhaseMode,
    angular_diameter_arcsec,
    angular_diameter_deg,
    crescent_width_arcsec,
    elongation_deg,
    illuminated_fraction,
    phase_angle_from_fraction,
    scan_geometry,
)
from .models import (
    AnalemmaPoint,
    CrescentGeometry,
    CrescentWindow,
    IlluminationMatch,
    Libration,
    LimbTarget,
    MoonPhase,
    MoonRegion,
    MoonReport,
    SurfaceFeature,
    MoonRender,
    MoonRenderSettings,
    MoonState,
    MoonView,
    OrbitElements,
    PhaseCatalogEntry,
    PhaseEvent,
    PhaseKind,
    PhotometricModel,
    RiseSetEvents,
    SunState,
    SurfaceIllumination,
    TargetVisibility,
    ToneMapping,
    TransitDetails,
    VisibilityGrade,
    as_photometric_model,
    as_tone_mapping,
    jsonable,
)
from .lunardate import chinese_lunar_date, lunar_date_label
from .moon import libration_angles, moon_report, moon_state, orbit_elements
from .phases import (
    PHASE_NAME_BANDS,
    PHASE_TARGET_DEG,
    MoonAge,
    ecliptic_longitude_difference_deg,
    find_full_moons,
    find_moons_by_illumination,
    find_new_moons,
    find_phase_events,
    find_quarter_moons,
    lunar_day_label,
    moon_age,
    phase_calendar,
    phase_name,
    previous_new_moon_et,
)
from .riseset import (
    RISE_SET_THRESHOLD_DEG,
    RiseSetMode,
    rise_set_transit,
    transit_details,
)
from .features import (
    estimate_diameter_km,
    feature_from_measurement,
    feature_names,
    find_feature,
    load_features,
)
from .annotations import (
    Annotation,
    AnnotationPlan,
    BASIN_MIN_DIAMETER_KM,
    CLASS_LABELS,
    FEATURE_CLASSES,
    FrameProjection,
    LANGUAGES,
    annotate_frame,
    annotation_catalogue,
    canonical_kind,
    disk_frame,
    feature_class,
    load_chinese_names,
    load_nomenclature,
    plan_annotations,
    region_frame,
    rim_outline,
)
from .moonview import (
    apply_tone_mapping,
    region_terrain,
    selection_footprint,
    disk_function,
    lunar_lambert_weight,
    region_geometry,
    render_feature,
    required_sky_span_km,
    render_moon,
    render_region,
    view_geometry,
    moon_magnitude,
    view_summary,
)
from .sun import equation_of_time_minutes, sun_state
from .terrain import (
    MoonTerrain,
    extract_region,
    discover_dem_sources,
    find_global_dem,
    global_dem_sources,
    open_region_at,
    preferred_dem,
    set_preferred_dem,
    SurfaceMesh,
    TerrainSources,
    bake_terrain_cache,
    clear_terrain_cache,
    find_region_files,
    find_sources,
    found_sources,
    load_region_dem,
    open_region,
    open_terrain,
)
from .anim import AnimationFrame, animation_frames, write_animation
from .caption import stamp_frame, stamp_text
from .encode import find_ffmpeg, have_ffmpeg, probe_mp4, write_mp4
from .hdr import (
    GainMap,
    GainMapFile,
    apply_gain_map,
    decode_gain_map,
    gain_map,
    gain_map_metadata,
    read_gainmap_jpeg,
    sdr_hdr_pair,
    write_gain_map_jpeg,
    write_gainmap_jpeg,
)

# Domain modules that pull in heavier dependencies are bound lazily below.
_LAZY = {
    "analemma": "moonlib_s.analemma",
    "crescent": "moonlib_s.crescent",
    "libration": "moonlib_s.libration",
    "plotting": "moonlib_s.plotting",
    "reporting": "moonlib_s.reporting",
    "service": "moonlib_s.service",
}


def __getattr__(name: str) -> Any:
    """Import domain/presentation modules on first attribute access."""
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target)
    globals()[name] = module
    return module


__all__ = [
    "__version__",
    # configuration / kernels
    "Settings",
    "load_settings",
    "cache_dir",
    "init_kernels",
    "kernel_session",
    "KernelSession",
    "SpiceConstants",
    "is_initialized",
    "loaded_kernels",
    "shutdown_kernels",
    "describe",
    "spice",
    # errors
    "MoonlibError",
    "ConfigurationError",
    "KernelError",
    "KernelNotFoundError",
    "KernelNotLoadedError",
    "OutOfRangeError",
    "TimeError",
    "ObserverError",
    "TargetDataError",
    "TerrainDataError",
    "PlottingUnavailableError",
    # time
    "TimeRange",
    "as_time_range",
    "to_et",
    "to_utc",
    "to_utc_iso",
    "as_utc",
    "utc_now",
    "resolve_timezone",
    "format_offset",
    "format_local",
    "SYNODIC_MONTH_DAYS",
    # observer
    "Observer",
    # numerics
    "bisect_root",
    "golden_minimum",
    "wrap_angle",
    # frames
    "AzEl",
    "RaDec",
    "az_el",
    "angular_separation",
    "position_angle",
    # illumination (the module stays reachable as `moonlib_s.illumination`;
    # its `illumination()` function is not re-exported at package level to
    # avoid shadowing the module)
    "PhaseMode",
    "illuminated_fraction",
    "phase_angle_from_fraction",
    "angular_diameter_deg",
    "angular_diameter_arcsec",
    "crescent_width_arcsec",
    "elongation_deg",
    "scan_geometry",
    # moon
    "moon_state",
    "moon_report",
    "libration_angles",
    "orbit_elements",
    # sun
    "sun_state",
    "equation_of_time_minutes",
    # rise / set
    "RiseSetMode",
    "RISE_SET_THRESHOLD_DEG",
    "rise_set_transit",
    "transit_details",
    # phases
    "PhaseKind",
    "PHASE_TARGET_DEG",
    "PHASE_NAME_BANDS",
    "MoonAge",
    "ecliptic_longitude_difference_deg",
    "find_phase_events",
    "find_new_moons",
    "find_full_moons",
    "EVENT_KINDS",
    "MoonEvent",
    "moon_events",
    "find_quarter_moons",
    "previous_new_moon_et",
    "moon_age",
    "phase_name",
    "chinese_lunar_date",
    "lunar_date_label",
    "lunar_day_label",
    "phase_calendar",
    "find_moons_by_illumination",
    # models
    "MoonState",
    "MoonPhase",
    "MoonReport",
    "Libration",
    "OrbitElements",
    "SurfaceIllumination",
    "RiseSetEvents",
    "TransitDetails",
    "PhaseEvent",
    "PhaseCatalogEntry",
    "IlluminationMatch",
    "CrescentGeometry",
    "CrescentWindow",
    "SunState",
    "AnalemmaPoint",
    "LimbTarget",
    "TargetVisibility",
    "VisibilityGrade",
    "PhotometricModel",
    "ToneMapping",
    "as_photometric_model",
    "as_tone_mapping",
    "MoonView",
    "MoonRegion",
    "MoonRenderSettings",
    "MoonRender",
    "jsonable",
    # lunar surface data and observation simulation
    "MoonTerrain",
    "TerrainSources",
    "SurfaceMesh",
    "open_terrain",
    "find_sources",
    "found_sources",
    "bake_terrain_cache",
    "clear_terrain_cache",
    "view_geometry",
    "moon_magnitude",
    "view_summary",
    "render_moon",
    "render_region",
    "render_feature",
    "region_geometry",
    "region_terrain",
    "selection_footprint",
    "required_sky_span_km",
    # output formats: an animation is a sequence of frames written to a file,
    # an HDR gain map is a second picture inside a JPEG
    "AnimationFrame",
    "stamp_frame",
    "stamp_text",
    "animation_frames",
    "write_animation",
    "find_ffmpeg",
    "have_ffmpeg",
    "write_mp4",
    "probe_mp4",
    "GainMap",
    "GainMapFile",
    "gain_map",
    "gain_map_metadata",
    "decode_gain_map",
    "apply_gain_map",
    "sdr_hdr_pair",
    "write_gain_map_jpeg",
    "write_gainmap_jpeg",
    "read_gainmap_jpeg",
    "SurfaceFeature",
    "Telescope",
    "Camera",
    "ImagingTrain",
    "arcsec_per_pixel",
    "diffraction_core_fwhm_arcsec",
    "effective_seeing_arcsec",
    "blur_budget",
    "blur_image",
    "diffraction_ring_kernel",
    "diffraction_rings",
    "instrument_blur",
    "sharpen_radius_arcsec",
    "sharpen_image",
    "instrument_sharpen",
    "psf_core_ratio",
    "desaturate",
    "adjust_gain",
    "DEFAULT_TELESCOPE",
    "DEFAULT_CAMERA",
    "default_train",
    "process_photograph",
    "finish_image",
    "fried_parameter_cm",
    "instrument_report",
    "orient_image",
    "rotate_image",
    "add_sensor_noise",
    "load_features",
    "find_feature",
    "feature_names",
    "estimate_diameter_km",
    "feature_from_measurement",
    "load_nomenclature",
    "load_chinese_names",
    "annotation_catalogue",
    "FrameProjection",
    "disk_frame",
    "region_frame",
    "rim_outline",
    "LANGUAGES",
    "Annotation",
    "AnnotationPlan",
    "plan_annotations",
    "annotate_frame",
    "FEATURE_CLASSES",
    "CLASS_LABELS",
    "BASIN_MIN_DIAMETER_KM",
    "canonical_kind",
    "feature_class",
    "load_region_dem",
    "open_region",
    "open_region_at",
    "extract_region",
    "discover_dem_sources",
    "find_global_dem",
    "set_preferred_dem",
    "preferred_dem",
    "global_dem_sources",
    "find_region_files",
    "disk_function",
    "lunar_lambert_weight",
    "apply_tone_mapping",
    # lazy submodules
    "crescent",
    "libration",
    "analemma",
    "plotting",
    "reporting",
    "service",
]
