"""The solar analemma: the Sun's position at a fixed civil clock time.

What an analemma is
-------------------
Photograph the Sun from the same spot at the same *clock* time every day for a
year and superimpose the frames: the Sun is not in the same place.  The locus
it traces is a slender figure-eight, the **analemma**.  Two independent
mechanisms build it:

* **axial tilt** (obliquity, ±23.44°) — moves the Sun north and south, giving
  the long axis of the figure eight;
* **orbital eccentricity** plus the obliquity's projection onto the equator —
  makes the Sun run fast or slow against the clock, which displaces the figure
  east and west.  This is the equation of time, bounded by roughly ±17 min.

Because the Earth passes perihelion in early January, the two lobes are
unequal: the northern lobe is small and the southern one long — a detail that
any correct implementation reproduces and that a threshold-based hack does not.

Two representations
-------------------
Both are offered, and both are computed from the *same* samples (the sampling
scheme is what defines an analemma, not the projection):

``solar_analemma`` — sky representation
    Azimuth/elevation as actually seen from the observer's site at the fixed
    local clock time.  This is the picture a camera would record, so the curve
    is skewed by the observer's latitude and, at fixed clock time, also
    deformed by the equation of time.

``classical_analemma`` — classical representation
    Equation of time (x) against solar declination (y) at local noon.  This is
    the *site-independent* shape printed on globes and in almanacs.  The
    azimuth/elevation columns are filled in as well because they cost one extra
    SPICE call per sample, but the intended plot uses
    ``equation_of_time_minutes`` and ``declination_deg``.

Why sample by local civil day, not by fixed ET steps
----------------------------------------------------
An analemma is defined by *"the same clock reading every day"*.  Walking the
range with a uniform ephemeris-time step (``TimeRange.grid``) would drift
against the local clock for two reasons: a solar day is not exactly 86 400 SI
seconds, and the local UTC offset changes at DST transitions.  Sampling by
local calendar date — building ``datetime(y, m, d, hour, minute, tzinfo=tz)``
and converting *that* to UTC — is the only construction that keeps the clock
reading fixed, which is precisely the physical definition of the curve.  This
module therefore reuses :func:`moonlib_s.timeutil.as_time_range` to normalise the
requested span, but enumerates the days from the *local* dates inside it.

Reference frames
----------------
Declination comes from SPICE's apparent position referenced to the **J2000
equator**, while the equation of time is referenced to the **equinox of date**
(the fictitious mean Sun is defined that way).  The two are therefore not
formally in the same frame.  The resulting inconsistency — the accumulated
precession/nutation of the equator since J2000 — is below 0.01° in declination
and below 0.05 min in the equation of time for the present century, i.e. far
inside the line width of any plot.  Hand-rolling a precession-nutation chain to
"fix" it would add a large amount of error-prone code for an invisible gain, so
it is deliberately not done.

What the legacy ``solar_analemma.ipynb`` got wrong
--------------------------------------------------
The pre-refactor notebook -- moonlib_s's own output, not part of this repository --
produced recognisable pictures, but three defects made its output unreliable and
non-reproducible:

1. *It ignored its own parameters.*  ``compute_analemma`` accepted
   ``year_start`` / ``year_end`` and then looped ``d`` over them while the inner
   loop always sampled the same fixed days of the same 12 months, so every
   extra year merely re-appended an identical year of points, all labelled with
   the *current* year's calendar date.  The classical routine never used its
   ``year`` argument at all: it read the module global ``YEAR_START`` (and, for
   its ephemeris calls, the globals ``LAT`` / ``LON``) directly.  This module
   takes an explicit observer and an explicit span, and derives everything from
   them.
2. *It mixed incompatible frames.*  The "equation of time" combined a crude
   geocentric mean longitude (``280.466 + 0.9856474 * n``) with a *topocentric
   apparent* right ascension (the Sun as seen from the site, with light-time
   and aberration applied), so it silently contained the parallax and the
   neglected terms of the mean-longitude polynomial.  There is no version of
   that difference that is a proper equation of time.
3. *It normalised with a hard-coded threshold.*  After forming
   ``4 * (L - RA)`` it looped ``while eot > 20: eot -= 1440``, which forces the
   answer into ±20 min by construction: a value that was a whole day (or two)
   wrong would be folded back into the plausible range and could never be
   detected.  Here the equation of time is the bounded closed-form Meeus series
   (:func:`moonlib_s.sun.equation_of_time_minutes`), every term of which is a
   trigonometric function of an angle, so no wrapping decision — and hence no
   whole-day error — is possible.

Module rules
------------
Pure computation: no printing, no matplotlib, results are immutable
:class:`~moonlib_s.models.AnalemmaPoint` models ready for
:func:`moonlib_s.models.jsonable`.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .errors import MoonlibError, TimeError
from .frames import az_el
from .models import AnalemmaPoint, LunarAnalemmaPoint, _iso
from .moon import moon_state
from .observer import Observer
from .sun import apparent_position, equation_of_time_minutes
from .timeutil import TimeLike, TimeRange, as_time_range, to_et, to_local

__all__ = [
    "AnalemmaError",
    "solar_analemma",
    "classical_analemma",
    "lunar_analemma",
    "analemma_extremes",
    "to_curve",
]

#: Wall-clock format of :attr:`moonlib_s.models.AnalemmaPoint.local_time`.
LOCAL_TIME_FORMAT = "%Y-%m-%d %H:%M:%S %Z"

#: Sample days of the month used by the legacy notebook, kept as a convenience.
LEGACY_SAMPLE_DAYS: Tuple[int, ...] = (1, 6, 11, 16, 21, 26)

#: Hard ceiling on the number of samples, to fail loudly instead of hanging.
MAX_SAMPLES = 20000

#: Span item: a calendar year, a :class:`~moonlib_s.timeutil.TimeRange`, or any
#: instant accepted by :func:`moonlib_s.timeutil.to_et`.
SpanLike = Union[int, TimeRange, TimeLike]


class AnalemmaError(MoonlibError):
    """Invalid analemma request (bad field name, empty sample set, ...)."""


# ---------------------------------------------------------------------------
# sampling helpers
# ---------------------------------------------------------------------------
def _coerce_year(value: Any) -> Optional[int]:
    """Calendar year of a span endpoint, or ``None`` when it is an instant."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if len(text) == 4 and text.isdigit():
            return int(text)
    return None


def _span_range(start: SpanLike, end: Optional[SpanLike]) -> TimeRange:
    """Normalise the accepted span forms into a :class:`TimeRange`.

    ``int`` and 4-digit-string years are calendar years (a single year when
    *end* is ``None``, an inclusive year span when *end* is a year as well);
    everything else is handed to :func:`moonlib_s.timeutil.as_time_range`.
    """
    start_year = _coerce_year(start)
    end_year = _coerce_year(end) if end is not None else None

    if start_year is None:
        # An instant start; `as_time_range` supplies the 365-day default span.
        if end is None:
            return as_time_range(start)
        return as_time_range(start, end_year if end_year is not None else end)

    if end_year is None and end is None:
        end_year = start_year
    if end_year is None:
        # A year start with a non-year end: an explicit instant closes the span.
        return as_time_range(start, end)  # type: ignore[arg-type]
    if end_year < start_year:
        raise TimeError(f"End year {end_year} precedes start year {start_year}")
    try:
        single_year = end_year == start_year
        return as_time_range(start_year, None if single_year else end_year)
    except (ValueError, OverflowError) as exc:  # pragma: no cover - year 9999
        raise TimeError(f"Unsupported year span {start_year}..{end_year}: {exc}") from exc


def _step_for(step_days: float) -> int:
    """Convert a fractional day step into a whole number of calendar days."""
    try:
        step = float(step_days)
    except (TypeError, ValueError) as exc:
        raise AnalemmaError(f"step_days must be a number, got {step_days!r}") from exc
    if not math.isfinite(step) or step <= 0.0:
        raise AnalemmaError(f"step_days must be positive and finite, got {step_days!r}")
    return max(1, int(math.ceil(step)))


def _iter_local_dates(
    first: date,
    last: date,
    *,
    step_days: int,
    sample_days: Optional[Sequence[int]],
    max_points: int,
) -> List[date]:
    """Local calendar dates of the sampling grid, inclusive of both ends.

    ``sample_days`` (day-of-month numbers) selects days *within each month*,
    which is the legacy notebook's scheme and keeps the sampled civil dates
    identical from month to month; otherwise every ``step_days``-th day is
    taken starting at *first*.
    """
    picked: List[date] = []
    cursor = first
    guard = (max_points * 4) + 1000
    while cursor <= last and len(picked) < max_points and guard > 0:
        guard -= 1
        if sample_days is None:
            picked.append(cursor)
            cursor += timedelta(days=step_days)
            continue
        if cursor.day in sample_days:
            picked.append(cursor)
        cursor += timedelta(days=1)
    return picked


def _end_boundary(span: TimeRange, tz: tzinfo, *, tolerance: float = 1e-3) -> datetime:
    """The span end, nudged off a local midnight so that day is not sampled.

    The span is half-open: its end instant *starts* the next local day, so a
    full UTC year yields 365 (or 366) samples for that year and never a stray
    1 January of the following one.  The nudge is applied only when the end
    really sits on a local midnight (within *tolerance*, because a datetime
    routed through ET and back can jitter by under a microsecond), and it is a
    single microsecond — far below the sampling clock, so no legitimate sample
    can be affected.
    """
    end_local = to_local(span.end_utc, tz)
    at_midnight = (
        end_local.hour == 0
        and end_local.minute == 0
        and end_local.second == 0
        and end_local.microsecond <= tolerance
    )
    if at_midnight:
        return span.end_utc - timedelta(microseconds=1)
    return span.end_utc


def _dedupe_and_sort(by_stamp: Dict[datetime, AnalemmaPoint]) -> List[AnalemmaPoint]:
    """Chronological list; a civil time repeated by a DST jump keeps one point."""
    return [by_stamp[stamp] for stamp in sorted(by_stamp)]


# ---------------------------------------------------------------------------
# the two public samplers
# ---------------------------------------------------------------------------
def solar_analemma(
    observer: Observer,
    start: Optional[SpanLike] = None,
    end: Optional[SpanLike] = None,
    *,
    local_hour: int = 12,
    local_minute: int = 0,
    step_days: float = 1.0,
    sample_days: Optional[Sequence[int]] = None,
) -> List[AnalemmaPoint]:
    """Sky analemma: the Sun's apparent position at a fixed local clock time.

    Parameters
    ----------
    observer
        Observing site; anything :meth:`moonlib_s.observer.Observer.from_value`
        accepts (``None``, ``(lat, lon[, height_km])`` or an ``Observer``).
    start, end
        The span to sample.  Each may be a calendar year (``2026``), a
        :class:`~moonlib_s.timeutil.TimeRange`, or an instant accepted by
        :func:`moonlib_s.timeutil.to_et`.  Defaults to the current UTC year.  The
        span is treated as half-open ``[start, end)`` over local calendar days:
        a sample belongs to the days between the local day containing ``start``
        and the local day containing ``end``, and a sample whose clock time
        falls before ``start`` or at/after ``end`` is dropped.  Because an
        ``end`` exactly on a local midnight *starts* the next day, a single UTC
        year yields 365 (or 366) samples for that year and never a stray
        1 January of the following one.
    local_hour, local_minute
        The civil clock reading held fixed across the year.  Default local
        noon.
    step_days
        Sampling interval in days when ``sample_days`` is ``None``.  Fractional
        values are rounded up to whole days, because the grid is made of
        calendar dates.
    sample_days
        Day-of-month numbers to sample in *every* month, e.g.
        ``(1, 6, 11, 16, 21, 26)`` — the legacy notebook's scheme, which yields
        six evenly spaced points per month.  When given it takes precedence over
        ``step_days``.

    Returns
    -------
    list of :class:`~moonlib_s.models.AnalemmaPoint`
        Sorted by UTC instant, at most one point per sampled local day.  Each
        point carries the UTC instant, the local civil time as
        ``'%Y-%m-%d %H:%M:%S %Z'``, the local day of year and month, the
        topocentric azimuth/elevation from :func:`moonlib_s.frames.az_el`, the
        apparent declination, and the equation of time.

    Notes
    -----
    **Daylight-saving gaps are resolved silently, not rejected.**  A wall-clock
    reading inside a spring-forward gap does not exist, and Python's PEP 495
    machinery does not raise for it: ``datetime(y, m, d, h, mi, tzinfo=tz)``
    (equivalently ``naive.replace(tzinfo=tz)``) attaches the *pre-transition*
    UTC offset to a ``fold=0`` value and the conversion to UTC therefore lands
    one hour after the requested reading.  For example, with
    ``tz=America/New_York`` the requested 2026-03-08 02:30 becomes 07:30 UTC,
    which is 03:30 EDT locally — the returned ``local_time`` string shows
    03:30, i.e. the reading actually used.  A ``try``/``except`` cannot trap
    this, because nothing is raised; the legacy notebook's guard was dead code
    and its comment claimed protection it never had.  The mirror case (an
    ambiguous fall-back reading) is pinned to ``fold=0``, i.e. the first of the
    two possible instants.  Both effects are one-off hour shifts in the
    time-of-day — never an error in the day — and are why the returned
    ``local_time`` is the authority on which instant was sampled.
    """
    site = Observer.from_value(observer)  # type: ignore[arg-type]
    if start is None and end is None:
        span = TimeRange.utc_year(datetime.now(timezone.utc).year)
    else:
        span = _span_range(start, end)
    step = _step_for(step_days)
    days = _resolve_sample_days(sample_days)

    tz = site.timezone
    start_utc = span.start_utc
    first_local = to_local(start_utc, tz)
    last_local = to_local(span.end_utc, tz)
    first_day = first_local.date()
    # One extra day of slack: the strict span test below removes it again when
    # the requested clock time on the last day falls outside the span.
    last_day = last_local.date()

    end_utc = _end_boundary(span, tz)
    points: Dict[datetime, AnalemmaPoint] = {}
    for day in _iter_local_dates(
        first_day, last_day, step_days=step, sample_days=days, max_points=MAX_SAMPLES
    ):
        when_local = datetime(
            day.year, day.month, day.day, int(local_hour), int(local_minute), tzinfo=tz
        )
        when_utc = when_local.astimezone(timezone.utc)
        if when_utc < start_utc or when_utc >= end_utc:
            continue
        points[when_utc] = _sample_point(site, when_utc, day)
    return _dedupe_and_sort(points)


def lunar_analemma(
    observer: Observer,
    start: Optional[SpanLike] = None,
    end: Optional[SpanLike] = None,
    *,
    local_hour: int = 21,
    local_minute: int = 0,
    step_days: float = 1.0,
    drift_minutes: float = 50.47,
) -> List[LunarAnalemmaPoint]:
    """The Moon's figure-eight: the sky position at the same lunar time.

    The Sun returns to the same place in the sky at the same civil clock time
    every day, so the solar analemma holds the clock fixed.  The Moon does not:
    it returns to the same place about **51 minutes later** each day.  Holding
    the civil clock fixed therefore gives a month-long orbit, not an analemma.
    This sampler advances the clock by *drift_minutes* per sampled day (the mean
    lunar day is 24 h 50.47 min) and plots the resulting small residual against
    the Moon's orbital tilt and eccentricity.

    The natural span is one synodic month.  A year is allowed but is not useful:
    it overlays twelve of these loops.  The curve window draws the returned
    azimuth/elevation pair, which is the sky representation the solar analemma
    uses as well; right ascension and declination are returned too for other
    projections and for the phase-colouring metadata.

    Parameters
    ----------
    observer
        Observing site.  Its timezone defines the civil clock reading; pass
        ``observer.with_timezone(...)`` to pin a specific offset.
    start, end
        Span accepted by :func:`_span_range`.  When both are omitted, the
        month starting now is sampled.
    local_hour, local_minute
        The wall-clock reading of the first sample (default 21:00).
    step_days
        Sampling interval in days, rounded up to whole calendar days.
    drift_minutes
        How much later the clock is each day.  The default, 50.47 min, is the
        mean lunar day (24 h 50.47 min) and makes the Moon return to nearly the
        same sky position; ``0`` gives the fixed-civil-clock month path instead.

    Returns
    -------
    list of :class:`~moonlib_s.models.LunarAnalemmaPoint`
        Sorted by UTC instant.  Each point carries right ascension, declination,
        the horizontal position, the phase angle and the illuminated percent,
        so the curve can be coloured by phase without another ephemeris call.
    """
    site = Observer.from_value(observer)  # type: ignore[arg-type]
    if start is None and end is None:
        anchor = to_utc(to_et(datetime.now(timezone.utc)))
        span = as_time_range(anchor, anchor + timedelta(days=30))
    else:
        span = _span_range(start, end)
    step = _step_for(step_days)

    tz = site.timezone
    start_utc = span.start_utc
    first_day = to_local(start_utc, tz).date()
    last_day = to_local(span.end_utc, tz).date()
    days = _iter_local_dates(
        first_day, last_day, step_days=step, sample_days=None, max_points=MAX_SAMPLES
    )
    drift = float(drift_minutes)
    if not math.isfinite(drift):
        raise AnalemmaError("drift_minutes must be finite.")
    # The clock drifts forward across the month, so the last sample can land
    # after the nominal end instant.  It is still one of the month's samples;
    # widen the acceptance window by the whole drift rather than dropping it.
    first_date = days[0] if days else first_day
    last_date = days[-1] if days else last_day
    slack = timedelta(minutes=max(0.0, drift) * max(0, (last_date - first_date).days))
    end_utc = _end_boundary(span, tz) + slack

    points: Dict[datetime, LunarAnalemmaPoint] = {}
    for day in days:
        # The lunar clock advances with the calendar, not with the sampling
        # stride: a two-day step is two lunar days later, not one.
        offset_days = (day - first_date).days
        when_local = (
            datetime(
                day.year, day.month, day.day,
                int(local_hour), int(local_minute), tzinfo=tz,
            )
            + timedelta(minutes=drift * offset_days)
        )
        when_utc = when_local.astimezone(timezone.utc)
        if when_utc < start_utc or when_utc >= end_utc:
            continue
        state = moon_state(site, when_utc)
        shown_local = when_utc.astimezone(tz)
        points[when_utc] = LunarAnalemmaPoint(
            when_utc=when_utc,
            local_time=shown_local.strftime(LOCAL_TIME_FORMAT),
            day_of_year=shown_local.timetuple().tm_yday,
            month=shown_local.month,
            ra_hours=float(state.ra_hours),
            declination_deg=float(state.dec_deg),
            azimuth_deg=float(state.azimuth_deg),
            elevation_deg=float(state.elevation_deg),
            phase_angle_deg=float(state.phase.phase_angle_geo_deg),
            illumination_percent=float(state.phase.illuminated_percent_geo),
            waxing=bool(state.phase.waxing),
        )
    return [points[stamp] for stamp in sorted(points)]


def classical_analemma(
    observer: Observer,
    start: Optional[SpanLike] = None,
    end: Optional[SpanLike] = None,
    *,
    local_hour: int = 12,
    local_minute: int = 0,
    step_days: float = 1.0,
) -> List[AnalemmaPoint]:
    """Classical analemma: equation of time against declination.

    Sampling is identical to :func:`solar_analemma` (same observer, same span
    handling, same local-clock definition, same DST caveat — see that docstring
    for the full discussion), and every returned field is populated the same
    way.  The difference is which pair of columns is *meant* to be plotted: the
    classical figure puts ``equation_of_time_minutes`` on the x axis and
    ``declination_deg`` on the y axis, which is the site-independent
    figure-eight engraved on globes.  ``azimuth_deg`` / ``elevation_deg`` are
    still filled in — they cost one extra SPICE call per sample — so a caller
    can switch between the sky and classical views without recomputing.

    Reference-frame caveat
    ----------------------
    ``declination_deg`` comes from SPICE's **apparent** position referenced to
    the **J2000 equator** (:func:`moonlib_s.sun.apparent_position`), whereas
    ``equation_of_time_minutes`` is a frame-of-date quantity: the fictitious
    mean Sun it compares against is defined from the **equinox of date**
    (Meeus ch. 28, as used by NOAA).  The two columns are therefore not
    formally in the same frame.  The inconsistency — precession and nutation of
    the equator accumulated since J2000 — stays below **0.01°** in declination
    and below **0.05 min** in the equation of time for the present century, so
    it is invisible at any plot resolution, and no attempt is made to hand-roll
    a precession-nutation chain to remove it.
    """
    return solar_analemma(
        observer,
        start,
        end,
        local_hour=local_hour,
        local_minute=local_minute,
        step_days=step_days,
        sample_days=None,
    )


# ---------------------------------------------------------------------------
# shared per-sample evaluation
# ---------------------------------------------------------------------------
def _resolve_sample_days(
    sample_days: Optional[Sequence[int]],
) -> Optional[Tuple[int, ...]]:
    """Validate an explicit day-of-month selection."""
    if sample_days is None:
        return None
    values = tuple(int(day) for day in sample_days)
    if not values:
        raise AnalemmaError("sample_days must not be empty; omit it to sample daily")
    invalid = [day for day in values if not 1 <= day <= 31]
    if invalid:
        raise AnalemmaError(f"sample_days entries must be days of month, got {invalid}")
    return values


def _sample_point(observer: Observer, when_utc: datetime, day: date) -> AnalemmaPoint:
    """Evaluate every analemma quantity for one sampled local day."""
    et = to_et(when_utc)
    horizontal = az_el("SUN", et, observer)
    apparent = apparent_position(et, observer)
    return AnalemmaPoint(
        when_utc=when_utc,
        local_time=when_utc.astimezone(observer.timezone).strftime(LOCAL_TIME_FORMAT),
        day_of_year=day.timetuple().tm_yday,
        month=day.month,
        azimuth_deg=horizontal.azimuth_deg,
        elevation_deg=horizontal.elevation_deg,
        declination_deg=apparent.dec_deg,
        equation_of_time_minutes=equation_of_time_minutes(et),
    )


# ---------------------------------------------------------------------------
# summaries
# ---------------------------------------------------------------------------
def analemma_extremes(points: Sequence[AnalemmaPoint]) -> Dict[str, Any]:
    """JSON-safe summary of an analemma's extremes and span.

    The dictionary contains only primitives, ISO-8601 UTC strings and ``None``,
    so ``json.dumps(analemma_extremes(points))`` always succeeds.

    Keys
    ----
    ``n_points``
        Number of samples supplied.
    ``span_days``
        ``latest`` minus ``earliest`` in days (0 for a single sample).
    ``earliest``, ``latest``
        First and last sampled instant, ISO-8601 UTC.
    ``declination_max`` / ``declination_min``
        ``{"declination_deg", "when_utc"}`` — the solstice ends of the figure
        eight, so their instants fall near late June and late December.
    ``equation_of_time_max`` / ``equation_of_time_min``
        ``{"equation_of_time_minutes", "when_utc"}`` — the instants when the
        Sun crosses the local meridian earliest and latest by the clock
        (around 3 November and 11 February), i.e. the extremes of apparent
        versus mean local solar noon.
    ``earliest_local_solar_noon`` / ``latest_local_solar_noon``
        ``{"when_utc", "local_time", "equation_of_time_minutes"}`` for those two
        meridional passages; they are the equation-of-time extremes restated in
        the form an observer experiences them.
    ``local_hours_sampled``
        Sorted distinct local clock readings present in the input.

    Raises
    ------
    AnalemmaError
        If *points* is empty, since no extreme exists to report.
    """
    samples = list(points)
    if not samples:
        raise AnalemmaError(
            "analemma_extremes needs at least one sample; the input is empty "
            "(check the requested span and sample selection)"
        )

    ordered = sorted(samples, key=lambda point: point.when_utc)
    dec_max = max(ordered, key=lambda point: point.declination_deg)
    dec_min = min(ordered, key=lambda point: point.declination_deg)
    eot_max = max(ordered, key=lambda point: point.equation_of_time_minutes)
    eot_min = min(ordered, key=lambda point: point.equation_of_time_minutes)
    earliest = ordered[0]
    latest = ordered[-1]

    return {
        "n_points": len(ordered),
        "span_days": (latest.when_utc - earliest.when_utc).total_seconds() / 86400.0,
        "earliest": _iso(earliest.when_utc),
        "latest": _iso(latest.when_utc),
        "declination_max": {
            "declination_deg": dec_max.declination_deg,
            "when_utc": _iso(dec_max.when_utc),
        },
        "declination_min": {
            "declination_deg": dec_min.declination_deg,
            "when_utc": _iso(dec_min.when_utc),
        },
        "equation_of_time_max": {
            "equation_of_time_minutes": eot_max.equation_of_time_minutes,
            "when_utc": _iso(eot_max.when_utc),
        },
        "equation_of_time_min": {
            "equation_of_time_minutes": eot_min.equation_of_time_minutes,
            "when_utc": _iso(eot_min.when_utc),
        },
        "earliest_local_solar_noon": _solar_noon_entry(eot_max),
        "latest_local_solar_noon": _solar_noon_entry(eot_min),
        "local_hours_sampled": sorted(
            {point.local_time.split()[1][:5] for point in ordered if point.local_time}
        ),
    }


def _solar_noon_entry(point: AnalemmaPoint) -> Dict[str, Any]:
    """Local-noon description of an equation-of-time extreme."""
    return {
        "when_utc": _iso(point.when_utc),
        "local_time": point.local_time,
        "equation_of_time_minutes": point.equation_of_time_minutes,
    }


# ---------------------------------------------------------------------------
# plotting helper
# ---------------------------------------------------------------------------
def to_curve(
    points: Sequence[AnalemmaPoint],
    x: str = "equation_of_time_minutes",
    y: str = "declination_deg",
) -> Tuple[List[float], List[float]]:
    """Two numeric columns of *points*, ready for a plotting layer.

    ``to_curve(points)`` returns ``(equation_of_time_minutes, declination_deg)``
    — the classical figure.  Pass ``x='azimuth_deg', y='elevation_deg'`` for the
    sky figure, or any other numeric
    :class:`~moonlib_s.models.AnalemmaPoint` field.

    Raises
    ------
    AnalemmaError
        If a requested name is not a numeric ``AnalemmaPoint`` field; the
        message lists the fields that are available.
    """
    numeric = _numeric_fields()
    for role, name in (("x", x), ("y", y)):
        if name not in numeric:
            raise AnalemmaError(
                f"Unknown or non-numeric {role} field {name!r} for AnalemmaPoint; "
                f"available numeric fields: {', '.join(sorted(numeric))}"
            )
    return (
        [float(getattr(point, x)) for point in points],
        [float(getattr(point, y)) for point in points],
    )


#: Fields of :class:`~moonlib_s.models.AnalemmaPoint` that hold plain numbers.
NUMERIC_FIELDS: Tuple[str, ...] = (
    "azimuth_deg",
    "elevation_deg",
    "declination_deg",
    "equation_of_time_minutes",
)


def _numeric_fields() -> Tuple[str, ...]:
    """Numeric :class:`~moonlib_s.models.AnalemmaPoint` field names."""
    return NUMERIC_FIELDS
