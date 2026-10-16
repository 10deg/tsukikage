"""Limb-target libration visibility prediction.

Geometry
--------
A limb feature is modelled as a **spherical cap** on the Moon: a centre
direction ``p`` (its selenographic latitude/longitude expressed as a unit vector
in the ``MOON_ME`` mean-Earth body-fixed frame) plus an angular radius
``theta_r = asin(radius_km / moon_radius_km)``, widened by the catalogue's
terrain margin ``C`` into the effective radius ``r = theta_r + C``.

Whether the cap can be seen from Earth is decided by two directions, both unit
vectors from the Moon's centre in the same frame:

**Sub-Earth direction** — towards the Earth.  Its selenographic coordinates are
exactly the optical libration angles an observer sees, because the point where
the Earth stands at the zenith is the centre of the Earth-facing hemisphere.
The angular distance ``gamma`` between ``p`` and this direction therefore says
how far the feature is from the centre of the visible disk, with ``gamma = 90``
the limb.  A cap of angular radius ``r`` is

* ``gamma < 90 - r`` — entirely inside the disk
  (:attr:`~moonlib.models.VisibilityGrade.FULL`);
* ``90 - r <= gamma < 90`` — centre inside, cap straddling the limb
  (``CENTER``);
* ``90 <= gamma < 90 + r`` — centre already hidden, but the cap still reaches
  past the limb (``MARGINAL``, the grazing case a limb observer waits for);
* otherwise hidden.

**Sub-solar direction** — towards the Sun.  The angle between ``p`` and this
direction is the solar *incidence* at the feature: below 90° the ground is
sunlit, above it the feature is in lunar night.  A feature can be geometrically
tipped into view and still useless because it is unlit, so every result carries
:attr:`~moonlib.models.TargetVisibility.illuminated` (and
:attr:`~moonlib.models.TargetVisibility.is_observable`, which also requires the
Moon to be above the horizon) separately from the grade.

``margin_deg = 90 - (gamma - r)`` is the single signed number behind the grades:
it is positive exactly when the cap reaches past the limb.  For one fixed target
``margin > 2r``, ``margin > r`` and ``margin > 0`` reproduce ``FULL``,
``CENTER`` and ``MARGINAL`` exactly, so — because ``r`` is constant per target —
a larger margin is always the better observation.

What changed relative to the legacy notebook
--------------------------------------------
``legacy/libration_targets_prediction.ipynb`` (moonlib's own output, pre-refactor
and not part of this repository) recomputed the whole lunar state
with the heavyweight ``compute_lunar_data`` (rise/set/transit, moon age, phase
naming, …) at every single sweep step, and its "visible now" query looked only
at the grade, so a target in lunar night counted as visible.  Here

* :func:`sweep` evaluates the sub-Earth and sub-solar direction arrays **once**
  for the whole time grid with vectorised ``spkpos`` calls, one horizontal
  geometry scan for the grid, and then a pure-Python assembly loop, so a 90-day
  sweep at 30-minute steps over the full 53-target catalogue costs well under a
  second of SPICE work;
* the grade/margin/incidence rules are shared between the scalar
  :func:`target_visibility` and the sweep, so a one-off evaluation and a sweep
  step can never disagree;
* visibility, illumination and practical observability are three separate,
  documented predicates instead of one overloaded "visible" flag;
* :func:`best_windows` reports one *window* per limb passage rather than five
  adjacent half-hour samples belonging to the same night.

Nothing in this module prints, and every failure raised here is a
:class:`~moonlib.errors.MoonlibError` subclass.
"""

from __future__ import annotations

import csv
import functools
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

import numpy as np
import spiceypy as spice

from .config import package_data_dir
from .errors import MoonlibError, TargetDataError
from .frames import (
    angular_separation,
    az_el,
    direction_in_moon_frame,
    moon_fixed_unit_vector,
)
from .illumination import (
    geocentric_phase_angle_deg,
    illuminated_fraction,
    scan_geometry,
)
from .kernels import kernel_session
from .models import (
    LimbTarget,
    LibrationSweepSample,
    TargetVisibility,
    VisibilityGrade,
)
from .observer import Observer
from .timeutil import TimeLike, TimeRange, as_time_range, to_et, to_utc

__all__ = [
    "TARGET_CSV_NAME",
    "ProgressCallback",
    "load_targets",
    "sub_earth_direction",
    "subsolar_direction",
    "target_visibility",
    "sweep",
    "visible_targets",
    "window_groups",
    "best_windows",
    "summarize",
]

#: File name of the packaged limb-target catalogue.
TARGET_CSV_NAME = "libration_targets.csv"

#: Signature of the optional :func:`sweep` progress callback.
ProgressCallback = Callable[[int, int], None]

#: Grade lookup by :attr:`~moonlib.models.VisibilityGrade.rank`, so that the
#: vectorised threshold ladder can stay in integers while still returning the
#: enum members themselves.
_GRADE_BY_RANK: Tuple[VisibilityGrade, ...] = tuple(
    sorted(VisibilityGrade, key=lambda grade: grade.rank)
)

# ---------------------------------------------------------------------------
# catalogue
# ---------------------------------------------------------------------------
_COL_ENGLISH_NAME = "English Name"
_COL_NAME_ZH = "名称"
_COL_LAT = "Lat"
_COL_LON = "Lon"
_COL_RADIUS = "Radius"
_COL_MARGIN = "C"
_COL_REGION = "Region"
_COL_TYPE = "Type"

#: Columns that must exist in a target catalogue (the names are the legacy
#: ones, including the Chinese header, so the shipped CSV keeps working).
_REQUIRED_COLUMNS: Tuple[str, ...] = (
    _COL_ENGLISH_NAME,
    _COL_NAME_ZH,
    _COL_LAT,
    _COL_LON,
    _COL_RADIUS,
    _COL_MARGIN,
    _COL_REGION,
    _COL_TYPE,
)


def _default_csv_path() -> Path:
    """Path of the packaged limb-target catalogue."""
    return package_data_dir() / TARGET_CSV_NAME


def _parse_float(
    row: Dict[str, Any],
    column: str,
    line: int,
    path: Path,
    *,
    default: Optional[float] = None,
) -> float:
    """Read one numeric CSV field, or raise :class:`TargetDataError`.

    ``Radius`` and ``C`` are allowed to be empty (they mean "unknown radius" and
    "no terrain margin"); the caller passes ``default=0.0`` for those.  Latitude,
    longitude and any other required field pass ``default=None`` so that an
    empty cell is reported like any other malformed value.
    """
    raw = row.get(column)
    text = "" if raw is None else str(raw).strip()
    if not text:
        if default is None:
            raise TargetDataError(
                f"{path}: row {line}: column {column!r} is empty"
            )
        return float(default)
    try:
        value = float(text)
    except ValueError as exc:
        raise TargetDataError(
            f"{path}: row {line}: column {column!r} is not a number: {text!r}"
        ) from exc
    if not math.isfinite(value):
        # float('nan') / float('inf') parse happily but poison every later
        # comparison in the grade ladder, so they are malformed data too.
        raise TargetDataError(
            f"{path}: row {line}: column {column!r} is not finite: {text!r}"
        )
    return value


def _is_blank_row(row: Dict[str, Any]) -> bool:
    """True for a row whose every known column is empty (trailing newline)."""
    return not any(str(row.get(column) or "").strip() for column in _REQUIRED_COLUMNS)


def _target_from_row(row: Dict[str, Any], line: int, path: Path) -> LimbTarget:
    """Build one :class:`~moonlib.models.LimbTarget` from a CSV row.

    *line* is the 1-based physical line number in *path* (the header is line 1),
    which is the number a user needs to jump to the offending record.
    """
    lat_deg = _parse_float(row, _COL_LAT, line, path)
    lon_deg = _parse_float(row, _COL_LON, line, path)
    radius_km = _parse_float(row, _COL_RADIUS, line, path, default=0.0)
    margin_deg = _parse_float(row, _COL_MARGIN, line, path, default=0.0)

    if not -90.0 <= lat_deg <= 90.0:
        raise TargetDataError(
            f"{path}: row {line}: column {_COL_LAT!r} is out of range "
            f"[-90, 90]: {lat_deg}"
        )
    if not -180.0 <= lon_deg <= 180.0:
        raise TargetDataError(
            f"{path}: row {line}: column {_COL_LON!r} is out of range "
            f"[-180, 180]: {lon_deg}"
        )
    if radius_km < 0.0:
        raise TargetDataError(
            f"{path}: row {line}: column {_COL_RADIUS!r} must not be negative: "
            f"{radius_km}"
        )

    return LimbTarget(
        english_name=str(row.get(_COL_ENGLISH_NAME) or "").strip(),
        name_zh=str(row.get(_COL_NAME_ZH) or "").strip(),
        lat_deg=lat_deg,
        lon_deg=lon_deg,
        radius_km=radius_km,
        region=str(row.get(_COL_REGION) or "").strip(),
        feature_type=str(row.get(_COL_TYPE) or "").strip(),
        margin_deg=margin_deg,
    )


def _read_targets(path: Path) -> List[LimbTarget]:
    """Parse a target catalogue, skipping blank rows."""
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise TargetDataError(
            f"Cannot read the limb-target catalogue {path}: {exc}"
        ) from exc

    with handle:
        reader = csv.DictReader(handle)
        header = tuple(reader.fieldnames or ())
        missing = [column for column in _REQUIRED_COLUMNS if column not in header]
        if missing:
            raise TargetDataError(
                f"{path}: missing column(s) {', '.join(map(repr, missing))}; "
                f"expected the header {', '.join(_REQUIRED_COLUMNS)}."
            )
        targets: List[LimbTarget] = []
        for row in reader:
            if _is_blank_row(row):
                continue
            targets.append(_target_from_row(row, reader.line_num, path))
    return targets


@functools.lru_cache(maxsize=1)
def _cached_default_targets() -> Tuple[LimbTarget, ...]:
    """Parsed packaged catalogue, cached because it is immutable.

    Only the *default* file is cached: an explicit ``path`` may point at a file
    the caller is still editing, so it is always re-read.  Returning a tuple
    keeps the cache safe from mutation; :func:`load_targets` hands out a list
    copy and ``LimbTarget`` is frozen anyway.
    """
    return tuple(_read_targets(_default_csv_path()))


def load_targets(path: Optional[Union[str, Path]] = None) -> List[LimbTarget]:
    """Load limb targets from a catalogue CSV.

    Parameters
    ----------
    path
        CSV file with the columns ``English Name, 名称, Lat, Lon, Radius, C,
        Region, Type``.  ``None`` (default) loads the packaged
        ``data/libration_targets.csv``, whose parsed result is cached.

    Returns
    -------
    list of LimbTarget
        In file order, blank rows skipped.  ``Radius`` and ``C`` may be empty
        and then default to ``0`` (unknown radius, no terrain margin).

    Raises
    ------
    TargetDataError
        The file cannot be read, a required column is missing, a numeric field
        is malformed or non-finite, a latitude/longitude is out of range, or a
        radius is negative.  The message always names the file, the 1-based row
        number and the offending column.
    """
    if path is None:
        return list(_cached_default_targets())
    return _read_targets(Path(path))


# ---------------------------------------------------------------------------
# direction vectors in the MOON_ME frame
# ---------------------------------------------------------------------------
def sub_earth_direction(et: float) -> np.ndarray:
    """Unit vector from the Moon's centre towards the Earth in ``MOON_ME``.

    The selenographic coordinates of this direction are the optical libration
    angles (the sub-Earth point); the angle between it and a surface direction
    is that feature's distance from the centre of the visible disk.

    Light time and stellar aberration are applied (``LT+S``), i.e. this is the
    direction the observer *sees*, not the geometric Moon–Earth line.  For the
    Moon the difference from the geometric direction is under one arcsecond.
    """
    return direction_in_moon_frame("EARTH", float(et))


def subsolar_direction(et: float) -> np.ndarray:
    """Unit vector from the Moon's centre towards the Sun in ``MOON_ME``.

    The angle between this direction and a surface direction is the solar
    incidence there: ``< 90`` sunlit, ``> 90`` in lunar night.  As for
    :func:`sub_earth_direction` the apparent (``LT+S``) direction is used.
    """
    return direction_in_moon_frame("SUN", float(et))


def _direction_array(body: str, ets: np.ndarray) -> np.ndarray:
    """Row-wise unit vectors towards *body* for a whole grid of ETs.

    ``spice.spkpos`` vectorises over an ET array, so this is one kernel call for
    the entire grid instead of one per step — the performance-critical path of
    :func:`sweep`.
    """
    times = np.asarray(ets, dtype=float)
    position = np.atleast_2d(
        np.asarray(spice.spkpos(body, times, "MOON_ME", "LT+S", "MOON")[0], dtype=float)
    )
    norms = np.linalg.norm(position, axis=1)
    if not np.all(norms > 0.0):  # pragma: no cover - cannot happen for a real body
        raise MoonlibError(f"Degenerate {body} direction relative to the Moon")
    return position / norms[:, None]


def _lat_lon_deg(vectors: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Selenographic latitude/longitude (degrees) of ``(n, 3)`` unit vectors.

    Latitude is ``asin(z)``; longitude is ``atan2(y, x)`` in ``(-180, 180]``,
    the convention used by :func:`moonlib.frames.vector_to_spherical` and hence
    directly comparable with :class:`moonlib.models.Libration`.
    """
    unit = np.asarray(vectors, dtype=float)
    latitude = np.degrees(np.arcsin(np.clip(unit[:, 2], -1.0, 1.0)))
    longitude = np.degrees(np.arctan2(unit[:, 1], unit[:, 0]))
    return latitude, longitude


def _separation_deg_array(vector: np.ndarray, others: np.ndarray) -> np.ndarray:
    """Angular separation (degrees) between one vector and ``(n, 3)`` vectors.

    Uses ``2 * asin(|a - b| / 2)``, the numerically stable formula SPICE's
    ``vsep`` (and therefore :func:`moonlib.frames.angular_separation`) applies;
    the two agree to about ``1e-12`` degrees.  Evaluating it with NumPy for the
    whole grid at once is what keeps :func:`sweep` fast — one ``spice.vsep``
    call per (step, target) pair would cost seconds on a 90-day sweep.
    """
    difference = np.asarray(others, dtype=float) - np.asarray(vector, dtype=float)
    half_chord = np.clip(np.linalg.norm(difference, axis=1) * 0.5, 0.0, 1.0)
    return np.degrees(2.0 * np.arcsin(half_chord))


# ---------------------------------------------------------------------------
# grading
# ---------------------------------------------------------------------------
def _grade_from_gamma(gamma_deg: float, r_eff_deg: float) -> VisibilityGrade:
    """Scalar grade, exactly the legacy notebook's threshold chain.

    With ``r == 0`` the ``CENTER`` and ``MARGINAL`` bands have zero width, so
    only ``FULL`` and ``HIDDEN`` are reachable — intended for point-like
    features (a star grazing the limb), not a data error.
    """
    if gamma_deg < 90.0 - r_eff_deg:
        return VisibilityGrade.FULL
    if gamma_deg < 90.0:
        return VisibilityGrade.CENTER
    if gamma_deg < 90.0 + r_eff_deg:
        return VisibilityGrade.MARGINAL
    return VisibilityGrade.HIDDEN


def _grade_codes(gamma_deg: np.ndarray, r_eff_deg: float) -> np.ndarray:
    """Vectorised twin of :func:`_grade_from_gamma`, as rank codes.

    The masks are applied from the loosest threshold to the strictest, so the
    last match wins — the same nesting as the scalar ``if``/``elif`` chain.
    """
    codes = np.zeros(gamma_deg.shape, dtype=np.int8)
    codes[gamma_deg < 90.0 + r_eff_deg] = VisibilityGrade.MARGINAL.rank
    codes[gamma_deg < 90.0] = VisibilityGrade.CENTER.rank
    codes[gamma_deg < 90.0 - r_eff_deg] = VisibilityGrade.FULL.rank
    return codes


def _coerce_time_range(
    value: Union[TimeRange, TimeLike, int, Tuple[TimeLike, TimeLike], List[TimeLike]]
) -> TimeRange:
    """Time span for a sweep.

    Everything goes through :func:`moonlib.timeutil.as_time_range`; a
    ``(start, end)`` pair is additionally accepted in one argument (that helper
    spells it as two arguments), because "a pair of instants" is how the rest of
    the library documents a period and it is what a caller naturally writes.
    """
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return as_time_range(value[0], value[1])
    return as_time_range(value)


def _as_target_list(targets: Iterable[LimbTarget]) -> List[LimbTarget]:
    """Validate an iterable of targets (strings are a common mistake)."""
    if isinstance(targets, LimbTarget):
        raise TargetDataError(
            "targets must be an iterable of LimbTarget objects, not a single one"
        )
    resolved: List[LimbTarget] = []
    for item in targets:
        if not isinstance(item, LimbTarget):
            raise TargetDataError(
                "targets must contain LimbTarget objects, got "
                f"{type(item).__name__}"
            )
        resolved.append(item)
    return resolved


# ---------------------------------------------------------------------------
# single instant
# ---------------------------------------------------------------------------
def target_visibility(
    target: LimbTarget,
    et: TimeLike,
    observer: Optional[Union[Observer, Sequence[float]]] = None,
    *,
    moon_elevation_deg: Optional[float] = None,
    illumination_percent: Optional[float] = None,
) -> TargetVisibility:
    """Visibility of one limb target at one instant.

    Parameters
    ----------
    target
        Feature to evaluate.
    et
        Ephemeris time (a float is taken as ET, anything else is converted by
        :func:`moonlib.timeutil.to_et`).
    observer
        Observing site.  When given, the Moon's elevation and the illuminated
        percentage are evaluated for it *unless* the corresponding keyword below
        supplies them — :func:`sweep` always supplies both, having already
        computed them for the whole grid, so this fallback costs the sweep
        nothing.
    moon_elevation_deg
        Moon elevation above the observer's horizon, stored on the result.
    illumination_percent
        Illuminated percentage of the lunar disk, stored on the result.

    Returns
    -------
    TargetVisibility

    Notes
    -----
    ``gamma_deg`` is the angle between the target and the sub-Earth point and
    ``r`` the target's effective (cap) radius, and the grade is

    ==========================  ====================
    condition                   grade
    ==========================  ====================
    ``gamma < 90 - r``          ``FULL``
    ``gamma < 90``              ``CENTER``
    ``gamma < 90 + r``          ``MARGINAL``
    otherwise                   ``HIDDEN``
    ==========================  ====================

    When ``r == 0`` (a radius-less catalogue entry, or a point target such as a
    star at the limb) the ``CENTER`` and ``MARGINAL`` bands collapse to zero
    width and only ``FULL`` and ``HIDDEN`` can be returned.
    """
    if not isinstance(target, LimbTarget):
        raise TargetDataError(
            f"target must be a LimbTarget, got {type(target).__name__}"
        )

    et_value = to_et(et)
    kernel_session().require_epoch(et_value, what="observation time")

    target_vector = moon_fixed_unit_vector(target.lat_deg, target.lon_deg)
    gamma_deg = math.degrees(
        angular_separation(target_vector, sub_earth_direction(et_value))
    )
    solar_incidence_deg = math.degrees(
        angular_separation(target_vector, subsolar_direction(et_value))
    )

    if observer is not None:
        site = Observer.from_value(observer)
        if moon_elevation_deg is None:
            moon_elevation_deg = az_el("MOON", et_value, site).elevation_deg
        if illumination_percent is None:
            # The illuminated fraction that matters here is the geocentric one
            # (what the disk looks like), and it does not depend on the site.
            illumination_percent = (
                illuminated_fraction(geocentric_phase_angle_deg(et_value)) * 100.0
            )

    r_eff_deg = target.effective_radius_deg
    return TargetVisibility(
        target=target,
        when_utc=to_utc(et_value),
        et=et_value,
        gamma_deg=gamma_deg,
        effective_radius_deg=r_eff_deg,
        margin_deg=90.0 - (gamma_deg - r_eff_deg),
        grade=_grade_from_gamma(gamma_deg, r_eff_deg),
        solar_incidence_deg=solar_incidence_deg,
        illuminated=solar_incidence_deg < 90.0,
        moon_elevation_deg=moon_elevation_deg,
        moon_illumination_percent=illumination_percent,
    )


# ---------------------------------------------------------------------------
# sweeping
# ---------------------------------------------------------------------------
def sweep(
    targets: Iterable[LimbTarget],
    time_range: Union[
        TimeRange, TimeLike, int, Tuple[TimeLike, TimeLike], List[TimeLike]
    ],
    observer: Optional[Union[Observer, Sequence[float]]],
    *,
    step_minutes: float = 30.0,
    progress: Optional[ProgressCallback] = None,
) -> List[LibrationSweepSample]:
    """Evaluate every target over a uniform time grid.

    Parameters
    ----------
    targets
        Limb targets (typically :func:`load_targets`).
    time_range
        Period to scan; anything accepted by
        :func:`moonlib.timeutil.as_time_range` (a :class:`TimeRange`, a single
        start instant, a calendar year, a ``(start, end)`` pair of instants or a
        ``(first_year, last_year)`` pair).
    observer
        Observing site, used for the Moon's elevation, the Sun's elevation and
        the apparent elongation.  ``None`` means the library default site.
    step_minutes
        Step length in minutes.  The grid includes both endpoints, with the
        last sample snapped onto ``end_et``.
    progress
        Optional ``callable(done, total)`` invoked after every completed step
        (``done`` counts from 1).  Never called with anything else, never
        called when ``None``.

    Returns
    -------
    list of LibrationSweepSample
        One sample per grid instant, each carrying a tuple with one
        :class:`~moonlib.models.TargetVisibility` per target, in the order the
        targets were given.

    Notes
    -----
    This is the cheap path: no ``moon_state``/``compute_lunar_data`` call is
    made at any step (the legacy notebook made one per step and paid for
    rise/set/transit and moon age it never used).  The sub-Earth and sub-solar
    direction arrays are computed once for the whole grid with vectorised
    ``spkpos``, and ``scan_geometry`` supplies the per-step elevation,
    illumination and elongation.
    """
    if progress is not None and not callable(progress):
        raise MoonlibError("progress must be callable(done, total) or None")

    window = _coerce_time_range(time_range)
    site = Observer.from_value(observer)
    target_list = _as_target_list(targets)

    session = kernel_session()
    grid = np.asarray(window.grid(float(step_minutes) * 60.0), dtype=float)
    total = int(grid.size)
    if total:
        session.require_epoch(float(grid[0]), what="sweep start")
        session.require_epoch(float(grid[-1]), what="sweep end")

    # Per-step site geometry: one azlcpo pair per step, evaluated in one call.
    scan = scan_geometry(grid, site, with_horizontal=True)
    moon_elevation = np.asarray(scan.elevation_deg, dtype=float)
    sun_elevation = np.asarray(scan.sun_elevation_deg, dtype=float)
    illumination_percent = np.asarray(scan.illumination_geo, dtype=float) * 100.0
    elongation = np.asarray(scan.elongation_deg, dtype=float)

    # Sub-Earth / sub-solar directions for the whole grid: two kernel calls.
    sub_earth = _direction_array("EARTH", grid)
    subsolar = _direction_array("SUN", grid)
    sub_earth_lat, sub_earth_lon = _lat_lon_deg(sub_earth)
    subsolar_lat, subsolar_lon = _lat_lon_deg(subsolar)
    colongitude = (90.0 - subsolar_lon) % 360.0

    # One vectorised pass per target, then a cheap per-step assembly loop.
    prepared: List[
        Tuple[LimbTarget, np.ndarray, np.ndarray, np.ndarray, np.ndarray, float]
    ] = []
    for target in target_list:
        target_vector = moon_fixed_unit_vector(target.lat_deg, target.lon_deg)
        r_eff_deg = target.effective_radius_deg
        gamma_deg = _separation_deg_array(target_vector, sub_earth)
        solar_incidence_deg = _separation_deg_array(target_vector, subsolar)
        prepared.append(
            (
                target,
                gamma_deg,
                solar_incidence_deg,
                # margin_deg = 90 - (gamma - r_eff); positive when the cap
                # reaches past the limb.
                90.0 - (gamma_deg - r_eff_deg),
                _grade_codes(gamma_deg, r_eff_deg),
                r_eff_deg,
            )
        )

    samples: List[LibrationSweepSample] = []
    for index in range(total):
        et = float(grid[index])
        when_utc = to_utc(et)
        elevation = float(moon_elevation[index])
        percent = float(illumination_percent[index])
        visibilities = tuple(
            TargetVisibility(
                target=target,
                when_utc=when_utc,
                et=et,
                gamma_deg=float(gamma_deg[index]),
                effective_radius_deg=r_eff_deg,
                margin_deg=float(margins[index]),
                grade=_GRADE_BY_RANK[int(codes[index])],
                solar_incidence_deg=float(solar_incidence_deg[index]),
                illuminated=bool(solar_incidence_deg[index] < 90.0),
                moon_elevation_deg=elevation,
                moon_illumination_percent=percent,
            )
            for (
                target,
                gamma_deg,
                solar_incidence_deg,
                margins,
                codes,
                r_eff_deg,
            ) in prepared
        )
        samples.append(
            LibrationSweepSample(
                when_utc=when_utc,
                et=et,
                sub_earth_lat_deg=float(sub_earth_lat[index]),
                sub_earth_lon_deg=float(sub_earth_lon[index]),
                subsolar_lat_deg=float(subsolar_lat[index]),
                subsolar_lon_deg=float(subsolar_lon[index]),
                colongitude_deg=float(colongitude[index]),
                moon_elevation_deg=elevation,
                sun_elevation_deg=float(sun_elevation[index]),
                illumination_percent=percent,
                elongation_deg=float(elongation[index]),
                visibilities=visibilities,
            )
        )
        if progress is not None:
            progress(index + 1, total)

    return samples


# ---------------------------------------------------------------------------
# querying a sweep
# ---------------------------------------------------------------------------
def visible_targets(
    sample: LibrationSweepSample,
    min_grade: "VisibilityGrade | str" = VisibilityGrade.MARGINAL,
    *,
    require_illuminated: bool = True,
    min_solar_incidence_deg: Optional[float] = None,
    max_solar_incidence_deg: Optional[float] = None,
    min_moon_elevation_deg: Optional[float] = None,
    max_illumination_percent: Optional[float] = None,
) -> Tuple[TargetVisibility, ...]:
    """Targets visible at one sweep step and meeting the observing conditions.

    A thin, discoverable wrapper over
    :meth:`moonlib.models.LibrationSweepSample.visible` — the filtering rules
    live there and nowhere else.  In particular ``max_solar_incidence_deg`` is
    *not* defaulted to 90° when ``require_illuminated`` is true: that flag
    already rejects any feature whose solar incidence is at or beyond 90°, and a
    second numeric default would only restate the same condition.
    """
    return sample.visible(
        min_grade,
        require_illuminated=require_illuminated,
        min_solar_incidence_deg=min_solar_incidence_deg,
        max_solar_incidence_deg=max_solar_incidence_deg,
        min_moon_elevation_deg=min_moon_elevation_deg,
        max_illumination_percent=max_illumination_percent,
    )


def _target_name_key(target: Union[LimbTarget, str]) -> str:
    """Case-insensitive lookup key for a target name."""
    if isinstance(target, LimbTarget):
        name = target.english_name
    elif isinstance(target, str):
        name = target
    else:
        raise TargetDataError(
            "target must be a LimbTarget or an English name string, got "
            f"{type(target).__name__}"
        )
    key = name.strip().casefold()
    if not key:
        raise TargetDataError("target name must not be empty")
    return key


def _target_map(samples: Sequence[LibrationSweepSample]) -> Dict[str, LimbTarget]:
    """Distinct targets of a sweep, keyed by case-folded English name."""
    found: Dict[str, LimbTarget] = {}
    for sample in samples:
        for visibility in sample.visibilities:
            key = visibility.target.english_name.strip().casefold()
            found.setdefault(key, visibility.target)
    return found


def _unknown_target(
    target: Union[LimbTarget, str], available: Dict[str, LimbTarget]
) -> TargetDataError:
    """Build the error for a name that no sample of this sweep contains."""
    names = sorted(item.english_name for item in available.values())
    listing = ", ".join(names[:8]) + (", ..." if len(names) > 8 else "")
    return TargetDataError(
        f"No sample contains a target named {target!r}; available targets: "
        f"{listing or '(none)'}"
    )


def window_groups(
    samples: Sequence[LibrationSweepSample],
    target: Union[LimbTarget, str],
    *,
    min_grade: "VisibilityGrade | str" = VisibilityGrade.MARGINAL,
    require_illuminated: bool = True,
    min_solar_incidence_deg: Optional[float] = 60.0,
    max_solar_incidence_deg: Optional[float] = 89.0,
    min_moon_elevation_deg: Optional[float] = 5.0,
    max_illumination_percent: Optional[float] = 98.0,
) -> List[List[TargetVisibility]]:
    """Split the qualifying samples of one target into observing windows.

    Consecutive sweep steps that satisfy the conditions describe **one** limb
    passage — the same window seen from five adjacent half-hour samples is not
    five opportunities.  Groups are therefore runs of qualifying steps that are
    adjacent *in the sample sequence*; any step that fails a condition, or a
    missing target at a step, ends the run.  Adjacency is measured in sample
    indices rather than seconds so that the final grid point (which
    :meth:`~moonlib.timeutil.TimeRange.grid` snaps onto the range end) cannot
    split a window artificially.

    Parameters
    ----------
    samples
        A whole sweep (or any contiguous subsequence of one).
    target
        :class:`~moonlib.models.LimbTarget` or its English name, matched
        case-insensitively.
    min_grade, require_illuminated, min_solar_incidence_deg, \
max_solar_incidence_deg, min_moon_elevation_deg, max_illumination_percent
        Observing conditions, applied through
        :meth:`moonlib.models.LibrationSweepSample.visible`; the defaults are
        :func:`best_windows`' defaults (grazing sunlight, Moon at least 5° up,
        no full-Moon washout).  Pass ``None`` to switch a numeric condition off.

    Returns
    -------
    list of list of TargetVisibility
        Chronological groups; each inner list holds one visibility per
        qualifying step, in time order.

    Raises
    ------
    TargetDataError
        *target* is neither a :class:`~moonlib.models.LimbTarget` nor a name,
        or no sample of *samples* contains that name.
    """
    key = _target_name_key(target)
    groups: List[List[TargetVisibility]] = []
    current: List[TargetVisibility] = []
    previous_index: Optional[int] = None
    present = False

    for index, sample in enumerate(samples):
        if not present:
            # Presence of the target in the sweep is a different question from
            # whether it ever satisfies the conditions: a target that is simply
            # never observable in this period must yield no windows, not an
            # error.  The scan stops as soon as the name is found.
            present = any(
                visibility.target.english_name.strip().casefold() == key
                for visibility in sample.visibilities
            )
        for visibility in sample.visible(
            min_grade,
            require_illuminated=require_illuminated,
            min_solar_incidence_deg=min_solar_incidence_deg,
            max_solar_incidence_deg=max_solar_incidence_deg,
            min_moon_elevation_deg=min_moon_elevation_deg,
            max_illumination_percent=max_illumination_percent,
        ):
            if visibility.target.english_name.strip().casefold() != key:
                continue
            if previous_index is not None and index == previous_index + 1:
                current.append(visibility)
            else:
                if current:
                    groups.append(current)
                current = [visibility]
            previous_index = index

    if current:
        groups.append(current)
    if not present:
        raise _unknown_target(target, _target_map(samples))
    return groups


def _best_key(visibility: TargetVisibility) -> Tuple[int, float]:
    """Ranking key for a visibility: grade first, then margin.

    ``margin_deg`` alone already implies the grade for a fixed target
    (``margin > 2r`` ⇔ ``FULL``, ``> r`` ⇔ ``CENTER``, ``> 0`` ⇔ ``MARGINAL``),
    so the two orderings coincide today; the explicit grade rank makes the
    intent obvious and keeps the ordering correct if that relation changes.
    """
    return (visibility.grade.rank, visibility.margin_deg)


def best_windows(
    samples: Sequence[LibrationSweepSample],
    target: Union[LimbTarget, str],
    *,
    top_n: int = 5,
    min_grade: "VisibilityGrade | str" = VisibilityGrade.MARGINAL,
    min_moon_elevation_deg: Optional[float] = 5.0,
    min_solar_incidence_deg: Optional[float] = 60.0,
    max_solar_incidence_deg: Optional[float] = 89.0,
    max_illumination_percent: Optional[float] = 98.0,
) -> List[TargetVisibility]:
    """The best observing window for each limb passage of one target.

    Qualifying steps are grouped into windows by :func:`window_groups`; the best
    step of each window (highest grade, then largest margin) is returned, and
    those window champions are sorted best-first.  That is deliberately *not*
    "the top ``top_n`` samples": five adjacent half-hour steps are one
    opportunity, so returning five of them would hide four other passages.
    Adjacent samples never appear twice in the result; ask for a bigger
    ``top_n`` to see more distinct windows.

    Parameters
    ----------
    samples
        A whole sweep (or any contiguous subsequence of one).
    target
        :class:`~moonlib.models.LimbTarget` or its English name, matched
        case-insensitively.
    top_n
        Maximum number of windows to return; ``0`` or less returns ``[]``.
    min_grade, min_moon_elevation_deg, min_solar_incidence_deg, \
max_solar_incidence_deg, max_illumination_percent
        Observing conditions (see :func:`window_groups`).  The defaults target a
        useful limb observation: grazing sunlight (60°–89° incidence), the Moon
        at least 5° above the horizon, and no full-Moon washout (≤ 98 %).

    Returns
    -------
    list of TargetVisibility
        At most ``top_n`` entries, one per window, best first.

    Raises
    ------
    TargetDataError
        *target* cannot be resolved against *samples*.
    """
    groups = window_groups(
        samples,
        target,
        min_grade=min_grade,
        min_solar_incidence_deg=min_solar_incidence_deg,
        max_solar_incidence_deg=max_solar_incidence_deg,
        min_moon_elevation_deg=min_moon_elevation_deg,
        max_illumination_percent=max_illumination_percent,
    )
    champions = [max(group, key=_best_key) for group in groups]
    champions.sort(key=_best_key, reverse=True)
    if top_n <= 0:
        return []
    return champions[:top_n]


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def _iso_utc(when: datetime) -> str:
    """ISO-8601 UTC string, matching :mod:`moonlib.models`' JSON convention.

    Millisecond precision with a ``Z`` suffix, so :func:`summarize` payloads can
    be mixed with ``to_dict()`` payloads without a second time format.
    """
    return (
        when.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _resolve_targets(
    samples: Sequence[LibrationSweepSample],
    targets: Optional[Iterable[Union[LimbTarget, str]]],
) -> List[LimbTarget]:
    """Targets to summarise: the sweep's own targets, or the requested ones."""
    available = _target_map(samples)
    if targets is None:
        return list(available.values())
    resolved: List[LimbTarget] = []
    for item in targets:
        if isinstance(item, LimbTarget):
            resolved.append(item)
            continue
        key = _target_name_key(item)
        found = available.get(key)
        if found is None:
            raise _unknown_target(item, available)
        resolved.append(found)
    return resolved


def summarize(
    samples: Sequence[LibrationSweepSample],
    targets: Optional[Iterable[Union[LimbTarget, str]]] = None,
    *,
    min_grade: "VisibilityGrade | str" = VisibilityGrade.MARGINAL,
) -> List[Dict[str, Any]]:
    """Per-target visibility statistics for a sweep.

    Parameters
    ----------
    samples
        A whole sweep (or any subsequence of one).
    targets
        Targets to report, as :class:`~moonlib.models.LimbTarget` objects or
        English names (case-insensitive).  ``None`` reports every target found
        in *samples*, in first-appearance order.
    min_grade
        Grade a sample must reach to count as visible.  The default
        (``MARGINAL``) matches
        :attr:`~moonlib.models.TargetVisibility.is_visible`.

    Returns
    -------
    list of dict
        One JSON-safe row per target with ``name``, ``name_zh``, ``region``,
        ``feature_type``, ``theta_r_deg``, ``effective_radius_deg``,
        ``n_samples``, ``n_visible``, ``n_observable``, ``percent_visible``,
        ``percent_observable``, ``max_margin_deg``, ``best_when_utc`` (ISO-8601
        UTC string) and ``best_grade``.

    Notes
    -----
    ``n_visible`` counts samples whose grade reaches *min_grade*;
    ``n_observable`` narrows that to features that are also sunlit and have the
    Moon above the horizon (exactly
    :meth:`~moonlib.models.TargetVisibility.is_observable`), which is the number
    the legacy notebook's summary was missing.  Both percentages are relative to
    the number of sweep steps, so a target absent from some samples simply
    cannot score there.  ``max_margin_deg``, ``best_when_utc`` and
    ``best_grade`` describe the target's best instant over the whole sweep and
    ignore *min_grade* — a target that never becomes visible still reports how
    close it came.
    """
    threshold = (
        VisibilityGrade(min_grade) if isinstance(min_grade, str) else min_grade
    )

    resolved = _resolve_targets(samples, targets)
    total = len(samples)

    # One pass over the sweep buckets every visibility we were asked about,
    # instead of one full scan per target.
    wanted = {item.english_name.strip().casefold() for item in resolved}
    buckets: Dict[str, List[TargetVisibility]] = {key: [] for key in wanted}
    for sample in samples:
        for visibility in sample.visibilities:
            key = visibility.target.english_name.strip().casefold()
            bucket = buckets.get(key)
            if bucket is not None:
                bucket.append(visibility)

    rows: List[Dict[str, Any]] = []
    for target in resolved:
        visibilities = buckets.get(target.english_name.strip().casefold(), ())
        n_visible = 0
        n_observable = 0
        best: Optional[TargetVisibility] = None
        for visibility in visibilities:
            if visibility.grade.rank >= threshold.rank:
                n_visible += 1
                if visibility.is_observable:
                    n_observable += 1
            if best is None or _best_key(visibility) > _best_key(best):
                best = visibility
        rows.append(
            {
                "name": target.english_name,
                "name_zh": target.name_zh,
                "region": target.region,
                "feature_type": target.feature_type,
                "theta_r_deg": target.theta_r_deg,
                "effective_radius_deg": target.effective_radius_deg,
                "n_samples": total,
                "n_visible": n_visible,
                "n_observable": n_observable,
                "percent_visible": (100.0 * n_visible / total) if total else 0.0,
                "percent_observable": (
                    (100.0 * n_observable / total) if total else 0.0
                ),
                "max_margin_deg": 0.0 if best is None else best.margin_deg,
                "best_when_utc": None if best is None else _iso_utc(best.when_utc),
                "best_grade": None if best is None else best.grade.value,
            }
        )
    return rows
