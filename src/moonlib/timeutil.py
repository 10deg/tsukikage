"""Time handling: SPICE ET ⇄ UTC ⇄ local time, and time ranges.

Conventions used throughout :mod:`moonlib`:

* SPICE ephemeris time (ET, TDB seconds past J2000) is the internal currency
  for every astronomical computation.
* UTC ``datetime`` objects are always timezone-aware.  Naive datetimes are
  interpreted as UTC by :func:`to_et` (documented, deliberate: it removes the
  ceremony that the legacy API required without introducing ambiguity).
* Local time is derived from the observer's coordinates through
  :func:`resolve_timezone`, which uses the offline ``timezonefinder`` database
  when available and falls back to a longitude-based offset otherwise.
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Iterator, Optional, Union

import numpy as np

from .errors import OutOfRangeError, TimeError
from .kernels import kernel_session, spice

__all__ = [
    "SECONDS_PER_DAY",
    "SECONDS_PER_HOUR",
    "SYNODIC_MONTH_DAYS",
    "ensure_utc",
    "to_et",
    "to_utc",
    "to_utc_iso",
    "as_utc",
    "utc_now",
    "resolve_timezone",
    "to_local",
    "format_offset",
    "format_local",
    "TimeRange",
    "as_time_range",
]

SECONDS_PER_DAY = 86400.0
SECONDS_PER_HOUR = 3600.0
SECONDS_PER_MINUTE = 60.0

#: Mean synodic month length (days), used for phase naming and age wrapping.
SYNODIC_MONTH_DAYS = 29.530588853

TimeLike = Union[float, int, datetime, str]


# ---------------------------------------------------------------------------
# conversions
# ---------------------------------------------------------------------------
def utc_now() -> datetime:
    """Current instant as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime, *, assume_utc: bool = True) -> datetime:
    """Return *value* as a timezone-aware UTC datetime.

    Parameters
    ----------
    value
        Datetime to normalise.
    assume_utc
        When True (default) a naive datetime is interpreted as UTC.  When
        False a :class:`~moonlib.errors.TimeError` is raised instead.
    """
    if not isinstance(value, datetime):
        raise TimeError(f"Expected a datetime, got {type(value).__name__}")
    if value.tzinfo is None:
        if not assume_utc:
            raise TimeError(
                "Naive datetime is ambiguous; pass a timezone-aware datetime."
            )
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_iso(text: str) -> datetime:
    candidate = text.strip()
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise TimeError(f"Cannot parse time string {text!r}: {exc}") from exc
    return ensure_utc(parsed)


def to_et(value: TimeLike) -> float:
    """Convert a datetime / ISO string / ET value to SPICE ET seconds.

    Floats and ints are passed through unchanged (they are already ET), which
    lets every public function accept either representation.
    """
    if isinstance(value, bool):
        raise TimeError("Booleans are not valid times")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        value = _parse_iso(value)
    if isinstance(value, datetime):
        kernel_session()  # the leap-second kernel is required for UTC -> ET
        utc = ensure_utc(value)
        stamp = utc.strftime("%Y-%m-%dT%H:%M:%S.%f")
        try:
            return float(spice.utc2et(stamp))
        except Exception as exc:
            raise TimeError(f"Cannot convert {utc.isoformat()} to ET: {exc}") from exc
    raise TimeError(f"Unsupported time value: {value!r}")


def to_utc(et: float, precision: int = 3) -> datetime:
    """Convert SPICE ET to a timezone-aware UTC datetime.

    Parameters
    ----------
    et
        Ephemeris time in TDB seconds past J2000.
    precision
        Number of fractional-second digits kept (default milliseconds, the
        library-wide standard event precision).
    """
    kernel_session()
    try:
        text = spice.et2utc(float(et), "ISOC", int(precision))
    except Exception as exc:
        raise OutOfRangeError(
            f"ET {et!r} cannot be expressed as a UTC calendar date: {exc}"
        ) from exc
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def to_utc_iso(et: float, precision: int = 3) -> str:
    """ISO-8601 UTC string for an ET value (JSON friendly)."""
    kernel_session()
    try:
        return spice.et2utc(float(et), "ISOC", int(precision))
    except Exception as exc:
        raise OutOfRangeError(
            f"ET {et!r} cannot be expressed as a UTC calendar date: {exc}"
        ) from exc


def as_utc(value: TimeLike) -> datetime:
    """Coerce a datetime / ISO string / ET value into an aware UTC datetime.

    Unlike :func:`to_utc`, which takes ephemeris time, this accepts anything
    and is the right entry point when a caller may hold either representation.
    """
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, str):
        return _parse_iso(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TimeError(f"Unsupported time value: {value!r}")
    return to_utc(float(value))


# ---------------------------------------------------------------------------
# time zones
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=1)
def _timezone_finder():
    """Lazily construct the (heavy) timezonefinder instance."""
    try:
        from timezonefinder import TimezoneFinder
    except ImportError:  # pragma: no cover - optional dependency
        return None
    return TimezoneFinder(in_memory=True)


@functools.lru_cache(maxsize=512)
def resolve_timezone(lat: float, lon: float) -> tzinfo:
    """Timezone for an observer at (*lat*, *lon*) degrees.

    Uses the bundled offline ``timezonefinder`` dataset when available and
    degrades to a fixed longitude/15° offset otherwise.  Results are cached;
    the finder is constructed lazily so importing :mod:`moonlib` stays cheap.
    """
    finder = _timezone_finder()
    if finder is not None:
        try:
            name = finder.timezone_at(lat=float(lat), lng=float(lon))
        except Exception:  # pragma: no cover - defensive
            name = None
        if name:
            try:
                from zoneinfo import ZoneInfo

                return ZoneInfo(name)
            except Exception:  # pragma: no cover - missing tz database
                pass
    offset_hours = int(round(float(lon) / 15.0))
    return timezone(timedelta(hours=offset_hours))


def to_local(dt_utc: datetime, tz: tzinfo) -> datetime:
    """Convert an aware datetime to *tz*."""
    return ensure_utc(dt_utc).astimezone(tz)


def format_offset(tz: tzinfo, reference: Optional[datetime] = None) -> str:
    """Format a timezone's UTC offset as ``UTC+08:00`` / ``UTC-03:30``."""
    if reference is None:
        reference = utc_now()
    offset = tz.utcoffset(reference)
    if offset is None:
        return "UTC+00:00"
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"UTC{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def format_local(
    dt_utc: datetime,
    tz: tzinfo,
    fmt: str = "%Y-%m-%d %H:%M:%S",
) -> str:
    """Format an instant in local time with an explicit UTC offset suffix."""
    local = to_local(dt_utc, tz)
    return f"{local.strftime(fmt)} ({format_offset(tz, local)})"


# ---------------------------------------------------------------------------
# ranges
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TimeRange:
    """A closed interval of ephemeris time."""

    start_et: float
    end_et: float

    def __post_init__(self) -> None:
        if self.end_et < self.start_et:
            raise TimeError("TimeRange end must not precede start")

    # -- constructors -------------------------------------------------------
    @classmethod
    def from_values(cls, start: TimeLike, end: TimeLike) -> "TimeRange":
        return cls(to_et(start), to_et(end))

    @classmethod
    def around(cls, center: TimeLike, days: float) -> "TimeRange":
        center_et = to_et(center)
        half = abs(float(days)) * SECONDS_PER_DAY * 0.5
        return cls(center_et - half, center_et + half)

    @classmethod
    def utc_year(cls, year: int) -> "TimeRange":
        """Full UTC calendar year ``[year-01-01, (year+1)-01-01)``."""
        start = datetime(int(year), 1, 1, tzinfo=timezone.utc)
        end = datetime(int(year) + 1, 1, 1, tzinfo=timezone.utc)
        return cls(to_et(start), to_et(end))

    @classmethod
    def utc_days(cls, start: TimeLike, days: float) -> "TimeRange":
        start_et = to_et(start)
        return cls(start_et, start_et + float(days) * SECONDS_PER_DAY)

    @classmethod
    def covering_years(cls, year_start: int, year_end: int) -> "TimeRange":
        """Inclusive calendar-year range, e.g. ``covering_years(2026, 2026)``."""
        if year_end < year_start:
            raise TimeError("year_end must not precede year_start")
        start = datetime(int(year_start), 1, 1, tzinfo=timezone.utc)
        end = datetime(int(year_end) + 1, 1, 1, tzinfo=timezone.utc)
        return cls(to_et(start), to_et(end))

    # -- properties ---------------------------------------------------------
    @property
    def start_utc(self) -> datetime:
        return to_utc(self.start_et)

    @property
    def end_utc(self) -> datetime:
        return to_utc(self.end_et)

    @property
    def duration_seconds(self) -> float:
        return self.end_et - self.start_et

    @property
    def duration_days(self) -> float:
        return self.duration_seconds / SECONDS_PER_DAY

    def __contains__(self, value: object) -> bool:
        et = to_et(value)  # type: ignore[arg-type]
        return self.start_et <= et <= self.end_et

    # -- sampling -----------------------------------------------------------
    def grid(self, step_seconds: float) -> np.ndarray:
        """Uniform grid of ET samples including both endpoints.

        When the range is an exact multiple of the step (to within the
        microsecond jitter introduced by ET <-> UTC round trips) the last node
        is snapped onto ``end_et`` instead of being appended, so a one-day step
        over a 366-day year yields 367 samples and never a duplicate pair.

        Without the snapping tolerance an exact-multiple range whose duration
        came back a few nanoseconds long produced one extra sample, and the
        caller saw two nodes separated by less than a microsecond.
        """
        if step_seconds <= 0:
            raise TimeError("step_seconds must be positive")

        exact_steps = self.duration_seconds / step_seconds
        nearest = round(exact_steps)
        if nearest >= 1 and abs(exact_steps - nearest) <= 1e-6 * nearest:
            grid = self.start_et + np.arange(int(nearest) + 1, dtype=float) * step_seconds
            grid[-1] = self.end_et
            return grid

        count = int(math.floor(exact_steps)) + 1
        grid = self.start_et + np.arange(count, dtype=float) * step_seconds
        if grid.size == 0 or grid[-1] < self.end_et:
            grid = np.append(grid, self.end_et)
        return grid

    def iter_steps(
        self, step_seconds: float
    ) -> Iterator[tuple]:
        """Yield ``(et, utc_datetime)`` pairs across the range."""
        for et in self.grid(step_seconds):
            yield float(et), to_utc(float(et))

    def clipped(self, other: "TimeRange") -> "TimeRange":
        """Intersection with *other*; raises when the ranges do not overlap."""
        start = max(self.start_et, other.start_et)
        end = min(self.end_et, other.end_et)
        if end < start:
            raise TimeError("TimeRanges do not overlap")
        return TimeRange(start, end)

    def as_dict(self) -> dict:
        return {
            "start_utc": to_utc_iso(self.start_et),
            "end_utc": to_utc_iso(self.end_et),
            "duration_days": self.duration_days,
        }


def as_time_range(
    value: "TimeLike | TimeRange | int",
    end: "TimeLike | int | None" = None,
) -> TimeRange:
    """Coerce the many ways of expressing a period into a :class:`TimeRange`.

    Accepted forms:

    ``as_time_range(TimeRange(...))``
        returned unchanged;
    ``as_time_range(2026)``
        the whole UTC calendar year 2026;
    ``as_time_range(2026, 2028)``
        inclusive calendar-year span;
    ``as_time_range(dt_or_iso, dt_or_iso)``
        explicit instants;
    ``as_time_range(dt_or_iso)``
        a 365-day window starting there;
    ``as_time_range((start, end))``
        the same as ``as_time_range(start, end)``.

    This single helper replaces the legacy mixture of
    ``find_new_moons(et_start, et_end)``, ``find_full_moons(year, year)`` and
    ``find_crescent_windows(date_start, date_end)``.
    """
    if value is None:
        raise TimeError(
            "No period given. Pass a TimeRange, a calendar year, a "
            "(start, end) pair, or a single instant; None is not a valid period."
        )

    if isinstance(value, TimeRange):
        if end is not None:
            raise TimeError("end must not be given when a TimeRange is passed")
        return value

    if isinstance(value, (tuple, list)):
        if end is not None:
            raise TimeError("end must not be given when a (start, end) pair is passed")
        if len(value) != 2:
            raise TimeError(
                f"A time-range pair must have exactly two elements, got {len(value)}"
            )
        return as_time_range(value[0], value[1])

    if isinstance(value, int) and not isinstance(value, bool):
        if end is None:
            return TimeRange.utc_year(value)
        if isinstance(end, int) and not isinstance(end, bool):
            return TimeRange.covering_years(value, end)
        return TimeRange(to_et(datetime(value, 1, 1, tzinfo=timezone.utc)), to_et(end))

    if end is None:
        return TimeRange.utc_days(value, 365.0)
    return TimeRange.from_values(value, end)
