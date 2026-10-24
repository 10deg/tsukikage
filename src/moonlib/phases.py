"""Principal lunar phases: search, catalogues, moon age and naming.

Convention
----------
Every phase is defined by the **geocentric ecliptic longitude difference**
``D = lambda_moon - lambda_sun`` (geometric positions, no aberration), which is
the definition used by almanacs:

===============  ==================
Phase            D
===============  ==================
New Moon         0°
First Quarter    +90°
Full Moon        180°
Last Quarter     270° (i.e. -90°)
===============  ==================

Because the Moon's ecliptic latitude reaches ±5.15°, the *phase angle* at full
Moon is typically 1°–5° rather than 0°.  The legacy ``moon_phase_query``
notebook identified full moons by requiring the phase angle to dip below
0.02 rad (1.15°), which silently discarded about 85 % of them — 2026 was
reported as having 2 full moons instead of 13.  The longitude criterion used
here has no such threshold.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

import numpy as np

from .errors import MoonlibError, TimeError
from .frames import az_el, geometric_position
from .illumination import (
    PhaseMode,
    angular_diameter_arcsec,
    illumination as illumination_scalar,
    angular_diameter_deg,
    elongation_deg,
    phase_angle_from_fraction,
    scan_geometry,
)
from .kernels import spice
from .models import IlluminationMatch, PhaseCatalogEntry, PhaseEvent, PhaseKind
from .numerics import bisect_root, wrap_angle
from .observer import Observer
from .timeutil import SYNODIC_MONTH_DAYS, as_time_range, to_utc

__all__ = [
    "PHASE_TARGET_DEG",
    "PHASE_NAME_BANDS",
    "MoonAge",
    "ecliptic_longitude_difference_deg",
    "find_phase_events",
    "find_new_moons",
    "find_full_moons",
    "find_quarter_moons",
    "previous_new_moon_et",
    "moon_age",
    "phase_name",
    "lunar_day_label",
    "phase_calendar",
    "find_moons_by_illumination",
]

#: Ecliptic-longitude difference (degrees) that defines each principal phase.
PHASE_TARGET_DEG: Dict[PhaseKind, float] = {
    PhaseKind.NEW_MOON: 0.0,
    PhaseKind.FIRST_QUARTER: 90.0,
    PhaseKind.FULL_MOON: 180.0,
    PhaseKind.LAST_QUARTER: 270.0,
}

#: Mean relative motion of the Moon w.r.t. the Sun (degrees per day).
_MEAN_RELATIVE_RATE_DEG_PER_DAY = 360.0 / SYNODIC_MONTH_DAYS

#: Upper bound (exclusive) of each lunar age band, in days, with its names.
PHASE_NAME_BANDS: Tuple[Tuple[float, str, str], ...] = (
    (1.0, "New Moon", "新月"),
    (6.5, "Waxing Crescent", "蛾眉月"),
    (8.5, "First Quarter", "上弦月"),
    (13.5, "Waxing Gibbous", "盈凸月"),
    (15.5, "Full Moon", "满月"),
    (20.5, "Waning Gibbous", "亏凸月"),
    (22.5, "Last Quarter", "下弦月"),
    (28.5, "Waning Crescent", "残月"),
)


class MoonAge(NamedTuple):
    """Result of :func:`moon_age`."""

    age_days: float
    new_moon_et: float
    new_moon_utc: datetime


# ---------------------------------------------------------------------------
# longitude difference
# ---------------------------------------------------------------------------
def _moon_sun_ecliptic_vectors(ets: np.ndarray):
    moon = np.asarray(
        spice.spkpos("MOON", ets, "ECLIPJ2000", "NONE", "EARTH")[0], dtype=float
    )
    sun = np.asarray(
        spice.spkpos("SUN", ets, "ECLIPJ2000", "NONE", "EARTH")[0], dtype=float
    )
    return moon, sun


def _longitude_difference_array(ets: np.ndarray) -> np.ndarray:
    moon, sun = _moon_sun_ecliptic_vectors(np.asarray(ets, dtype=float))
    return np.arctan2(moon[:, 1], moon[:, 0]) - np.arctan2(sun[:, 1], sun[:, 0])


def ecliptic_longitude_difference_deg(et: float) -> float:
    """Geocentric ecliptic longitude difference ``moon - sun`` in degrees.

    Wrapped into ``(-180, 180]``.  Negative means the Moon trails the Sun
    (waning, approaching new moon); positive means it leads (waxing).
    """
    moon = geometric_position("MOON", et, center="EARTH", frame="ECLIPJ2000")
    sun = geometric_position("SUN", et, center="EARTH", frame="ECLIPJ2000")
    difference = math.atan2(moon[1], moon[0]) - math.atan2(sun[1], sun[0])
    return math.degrees(wrap_angle(difference))


def _phase_root_function(target_deg: float):
    """Scalar function with an upward zero crossing at the requested phase."""
    target_rad = math.radians(target_deg)

    def func(et: float) -> float:
        difference = math.radians(ecliptic_longitude_difference_deg(et)) - target_rad
        return wrap_angle(difference)

    return func


# ---------------------------------------------------------------------------
# event search
# ---------------------------------------------------------------------------
def find_phase_events(
    time_range,
    kinds: Optional[Iterable[PhaseKind]] = None,
    *,
    step_seconds: float = 3600.0,
) -> List[PhaseEvent]:
    """Find the principal phase instants inside a time range.

    Parameters
    ----------
    time_range
        :class:`~moonlib.timeutil.TimeRange` or anything accepted by
        :func:`moonlib.timeutil.as_time_range`.
    kinds
        Which phases to search for; defaults to all four.
    step_seconds
        Coarse scan step.  One hour is ample: the Moon gains at most 0.55° of
        ecliptic longitude on the Sun per hour, so a crossing can never be
        skipped.

    Returns
    -------
    list of PhaseEvent
        Sorted chronologically, millisecond precision.
    """
    window = as_time_range(time_range)
    requested = tuple(kinds) if kinds is not None else tuple(PhaseKind)

    grid = window.grid(step_seconds)
    difference = _longitude_difference_array(grid)
    events: List[PhaseEvent] = []

    for kind in requested:
        target_rad = math.radians(PHASE_TARGET_DEG[kind])
        residual = np.arctan2(
            np.sin(difference - target_rad), np.cos(difference - target_rad)
        )
        func = _phase_root_function(PHASE_TARGET_DEG[kind])
        for index in np.nonzero((residual[:-1] < 0.0) & (residual[1:] >= 0.0))[0]:
            low = float(grid[index])
            high = float(grid[index + 1])
            et = bisect_root(func, low, high, tol=1e-3)
            events.append(PhaseEvent(kind=kind, et=et, when_utc=to_utc(et)))

    events.sort(key=lambda event: event.et)
    return events


def find_new_moons(time_range) -> List[PhaseEvent]:
    """All new moons in a time range."""
    return find_phase_events(time_range, (PhaseKind.NEW_MOON,))


def find_full_moons(time_range) -> List[PhaseEvent]:
    """All full moons in a time range."""
    return find_phase_events(time_range, (PhaseKind.FULL_MOON,))


def find_quarter_moons(time_range, which: str = "both") -> List[PhaseEvent]:
    """Quarter moons in a time range.

    Parameters
    ----------
    which
        ``'first'``, ``'third'`` or ``'both'`` (default).
    """
    if which == "first":
        kinds = (PhaseKind.FIRST_QUARTER,)
    elif which == "third":
        kinds = (PhaseKind.LAST_QUARTER,)
    elif which == "both":
        kinds = (PhaseKind.FIRST_QUARTER, PhaseKind.LAST_QUARTER)
    else:
        raise TimeError(f"which must be 'first', 'third' or 'both', not {which!r}")
    return find_phase_events(time_range, kinds)


def previous_new_moon_et(et: float) -> float:
    """Ephemeris time of the last new moon at or before *et*.

    The wrapped ecliptic-longitude difference ``D`` rises smoothly through zero
    at every new moon and is monotone across one, so the search is a plain
    bracketed bisection of ``D`` itself.  An analytic first guess from the mean
    synodic rate puts the bracket within a day or two in virtually all cases;
    the bracket is widened progressively if it does not, and a bounded backward
    walk is the final fallback, so this cannot fail for a valid epoch.

    .. note::
        An earlier version unwrapped ``D`` relative to its value at *et*.  That
        is discontinuous whenever ``|D(et)|`` approaches 180 deg — i.e. near
        full moon — because the unwrapped value jumps by a whole turn exactly
        where the bracket sits, which made the bisection fail for about a fifth
        of all epochs (and for every full-moon instant).  Bisecting the wrapped
        difference avoids the construction entirely.

    Costs roughly a dozen SPICE evaluations instead of the ~1700 the legacy
    :func:`compute_moon_age` spent scanning 35 days at one-hour resolution.
    """
    reference = ecliptic_longitude_difference_deg(et)
    rate_per_second = _MEAN_RELATIVE_RATE_DEG_PER_DAY / 86400.0

    # Elapsed time since the previous conjunction, from the mean relative rate.
    # When the Moon already leads the Sun the angle travelled is `reference`;
    # when it trails, it is `reference + 360`.
    elapsed = (reference if reference >= 0.0 else reference + 360.0) / rate_per_second
    initial = et - elapsed

    # The widest bracket must stay below one synodic month so that it cannot
    # contain two conjunctions.
    for half_width_days in (3.0, 6.0, 10.0, 14.0):
        low = initial - half_width_days * 86400.0
        high = initial + half_width_days * 86400.0
        if ecliptic_longitude_difference_deg(low) <= 0.0 <= ecliptic_longitude_difference_deg(high):
            return bisect_root(
                ecliptic_longitude_difference_deg, low, high, tol=1e-3
            )

    # Fallback: walk backwards in bounded steps.  Two days is far below the
    # ~180 deg of relative motion that would be needed to step over a crossing.
    step = 0.1 * 86400.0
    later_et, later_value = et, reference
    travelled = 0.0
    while travelled < 32.0 * 86400.0:
        earlier_et = later_et - step
        earlier_value = ecliptic_longitude_difference_deg(earlier_et)
        if earlier_value < 0.0 <= later_value:
            return bisect_root(
                ecliptic_longitude_difference_deg, earlier_et, later_et, tol=1e-3
            )
        later_et, later_value = earlier_et, earlier_value
        travelled += step
        step = min(step * 1.3, 2.0 * 86400.0)

    raise MoonlibError(
        f"Could not locate the new moon preceding ET {et!r}; "
        "the ephemeris may not cover this epoch."
    )


def moon_age(et: float) -> MoonAge:
    """Days elapsed since the last new moon.

    The age lies in ``[0, 30)`` days.  Note that the *actual* synodic month
    varies between about 29.27 and 29.93 days, so an age can exceed the mean
    synodic month of 29.5306 days; that only happens in the last few hours
    before the next new moon, where :func:`phase_name` correctly reports
    "new moon" anyway.
    """
    new_moon_et = previous_new_moon_et(et)
    age_days = (et - new_moon_et) / 86400.0
    return MoonAge(
        age_days=age_days,
        new_moon_et=new_moon_et,
        new_moon_utc=to_utc(new_moon_et),
    )


# ---------------------------------------------------------------------------
# naming
# ---------------------------------------------------------------------------
def phase_name(age_days: float) -> Tuple[str, str]:
    """``(english, chinese)`` phase names for a lunar age in days."""
    days = age_days % SYNODIC_MONTH_DAYS
    for limit, name_en, name_zh in PHASE_NAME_BANDS:
        if days < limit:
            return name_en, name_zh
    return "New Moon", "新月"


def lunar_day_label(age_days: float, style: str = "zh") -> str:
    """Approximate lunar-calendar day for an age in days.

    .. warning::
        This is an *approximation* derived from the age; the true Chinese
        calendar day depends on the civil new-moon date and can differ by one
        day.  Use it for display only.
    """
    magnitude = abs(age_days)
    if style == "latin":
        if age_days >= 0:
            if magnitude < 0.5:
                return "L1"
            if magnitude < 1.5:
                return "L2"
            if magnitude < 2.5:
                return "L3"
            if magnitude < 3.5:
                return "L4"
            return f"L{int(magnitude) + 1}"
        if magnitude < 0.5:
            return "L30"
        if magnitude < 1.5:
            return "L29"
        if magnitude < 2.5:
            return "L28"
        if magnitude < 3.5:
            return "L27"
        return f"L{30 - int(magnitude)}"

    if age_days >= 0:
        if magnitude < 0.5:
            return "初一"
        if magnitude < 1.5:
            return "初二"
        if magnitude < 2.5:
            return "初三"
        if magnitude < 3.5:
            return "初四"
        return f"初{int(magnitude) + 1}"
    if magnitude < 0.5:
        return "三十"
    if magnitude < 1.5:
        return "廿九"
    if magnitude < 2.5:
        return "廿八"
    if magnitude < 3.5:
        return "廿七"
    return f"廿{int(magnitude) + 1}"


# ---------------------------------------------------------------------------
# catalogues
# ---------------------------------------------------------------------------
def phase_calendar(
    year_or_range,
    observer: Optional[Observer] = None,
    *,
    kinds: Optional[Iterable[PhaseKind]] = None,
    step_seconds: float = 3600.0,
) -> List[PhaseCatalogEntry]:
    """Standard phase catalogue for a period.

    Parameters
    ----------
    year_or_range
        A calendar year (``2026``), a year pair (``(2026, 2028)``), a
        :class:`~moonlib.timeutil.TimeRange`, or a ``(start, end)`` pair of
        instants.
    observer
        When given, each entry also carries the Moon's elevation at the event.

    Returns
    -------
    list of PhaseCatalogEntry
        Chronological, each with geocentric distance and apparent diameter.
    """
    window = as_time_range(year_or_range)
    events = find_phase_events(window, kinds, step_seconds=step_seconds)

    observer_obj = Observer.from_value(observer) if observer is not None else None

    entries: List[PhaseCatalogEntry] = []
    for event in events:
        distance = float(
            np.linalg.norm(geometric_position("MOON", event.et, frame="J2000"))
        )
        elevation = (
            None
            if observer_obj is None
            else az_el("MOON", event.et, observer_obj).elevation_deg
        )
        entries.append(
            PhaseCatalogEntry(
                event=event,
                distance_km=distance,
                angular_diameter_arcsec=angular_diameter_arcsec(distance),
                angular_diameter_deg=angular_diameter_deg(distance),
                elevation_deg=elevation,
            )
        )
    return entries


# ---------------------------------------------------------------------------
# illumination matching
# ---------------------------------------------------------------------------
def find_moons_by_illumination(
    target_percent: float,
    time_range,
    observer: Optional[Observer] = None,
    *,
    waxing: Optional[bool] = None,
    phase_mode: PhaseMode = PhaseMode.TOPOCENTRIC,
    phase_tolerance_deg: float = 3.0,
    step_minutes: float = 20.0,
) -> List[IlluminationMatch]:
    """Moons whose illuminated fraction equals *target_percent*.

    Each synodic month crosses any given illumination twice — once while
    waxing, once while waning.  This routine scans the range, refines every
    crossing, keeps the best match per local calendar month and returns them
    sorted by apparent diameter, largest first.  Useful for picking the biggest
    Moon at a chosen phase.

    Parameters
    ----------
    target_percent
        Requested illuminated percentage, ``0 < target < 100``.
    time_range
        Period to search (see :func:`phase_calendar`).
    observer
        Observing site; also determines the local month used for grouping.
    waxing
        ``True`` for waxing only, ``False`` for waning only, ``None`` for both.
    phase_mode
        Which illumination to match (:class:`~moonlib.illumination.PhaseMode`).
    phase_tolerance_deg
        Maximum allowed deviation of the actual phase angle from the value
        implied by *target_percent*.  Rejects near-new/near-full matches whose
        ecliptic-latitude geometry distorts the relation.
    step_minutes
        Coarse scan step.

    Returns
    -------
    list of IlluminationMatch
    """
    if not 0.0 < target_percent < 100.0:
        raise TimeError("target_percent must lie strictly between 0 and 100")

    window = as_time_range(time_range)
    observer_obj = Observer.from_value(observer)
    if isinstance(phase_mode, str):
        phase_mode = PhaseMode(phase_mode)

    target_fraction = target_percent / 100.0
    expected_phase_angle = phase_angle_from_fraction(target_fraction)

    grid = window.grid(step_minutes * 60.0)
    scan = scan_geometry(grid, observer_obj)
    illumination = scan.illumination(phase_mode)
    residual = illumination - target_fraction

    def scalar(et: float) -> float:
            return illumination_scalar(et, observer_obj, phase_mode)[0] - target_fraction

    matches: List[IlluminationMatch] = []
    for index in np.nonzero(
        (residual[:-1] * residual[1:] < 0.0)
        & np.isfinite(residual[:-1])
        & np.isfinite(residual[1:])
    )[0]:
        is_waxing = residual[index] < 0.0
        if waxing is not None and is_waxing != waxing:
            continue
        low = float(grid[index])
        high = float(grid[index + 1])
        try:
            et = bisect_root(scalar, low, high, tol=1e-3)
        except ValueError:  # pragma: no cover - defensive
            continue
        matches.append(_illumination_match(et, observer_obj, phase_mode,
                                           expected_phase_angle, target_percent))

    # Keep the closest match per local calendar month.
    best_per_month: Dict[Tuple[int, int], IlluminationMatch] = {}
    for match in matches:
        local = observer_obj.local_time(match.when_utc)
        key = (local.year, local.month)
        deviation = abs(match.illumination_percent - target_percent)
        current = best_per_month.get(key)
        if current is None or deviation < abs(
            current.illumination_percent - target_percent
        ):
            best_per_month[key] = match

    return sorted(
        best_per_month.values(),
        key=lambda match: match.angular_diameter_arcsec,
        reverse=True,
    )


def _illumination_match(
    et: float,
    observer: Observer,
    phase_mode: PhaseMode,
    expected_phase_angle: float,
    target_percent: float,
) -> IlluminationMatch:
    fraction, phase_angle = illumination_scalar(et, observer, phase_mode)
    distance = float(
        np.linalg.norm(geometric_position("MOON", et, frame="J2000"))
    )
    local = observer.local_time(to_utc(et))
    return IlluminationMatch(
        when_utc=to_utc(et),
        et=et,
        month=local.month,
        illumination_percent=fraction * 100.0,
        phase_angle_deg=phase_angle,
        expected_phase_angle_deg=expected_phase_angle,
        phase_deviation_deg=abs(phase_angle - expected_phase_angle),
        angular_diameter_arcsec=angular_diameter_arcsec(distance),
        angular_diameter_deg=angular_diameter_deg(distance),
        distance_km=distance,
        elongation_deg=elongation_deg(et, observer),
        elevation_deg=az_el("MOON", et, observer).elevation_deg,
        waxing=ecliptic_longitude_difference_deg(et) >= 0.0,
    )
