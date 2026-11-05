"""Condition search behind the web calculator.

The page asks questions of the form "when was/will the Moon's age be the same
as it is at this instant?", "when is the Moon highest in the next month?" and
so on.  This module turns one of those sentences into a small set of instants.
It is deliberately separate from :mod:`moonlib.events`: the events module is a
catalogue of named phenomena, while these are arbitrary conditions on the
Moon's continuous state.

The search uses a coarse grid plus a local refinement.  For ``age`` the answer
is even simpler: the age is the time since the previous new moon, so a target
age is ``new_moon + target_days`` and no root finding is needed.  "Same" is
therefore exact for age, while illumination and colongitude are refined by
bisection.  "Similar" is used for libration, where the condition is that both
libration components are within the requested error of the reference.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .errors import MoonlibError
from .illumination import scan_geometry
from .moon import libration_angles, moon_state
from .observer import Observer
from .phases import moon_age
from .timeutil import to_et, to_utc, utc_now

__all__ = [
    "CALCULATOR_DIRECTIONS",
    "CALCULATOR_MODES",
    "CALCULATOR_TARGETS",
    "search_conditions",
]

CALCULATOR_DIRECTIONS = ("previous", "next", "past", "future")
CALCULATOR_TARGETS = ("age", "illumination", "libration", "shadow", "altitude")
CALCULATOR_MODES = ("same", "similar", "highest", "lowest")

#: The fixed "same" tolerances.  The page disables the error field for 相同,
#: so the search needs a number below the display resolution but wide enough
#: that a coarse grid can find the root.  Age is handled by new-moon arithmetic
#: and does not use this table; the others are refined to seconds.
_SAME_TOLERANCE = {
    "illumination": 0.05,   # percent
    "shadow": 0.05,         # degrees of colongitude
    "libration": 0.05,      # degrees per component (unused for 相同)
    "altitude": 0.05,       # degrees (unused for 相同)
}

_SYNODIC_DAYS = 29.530588853
_YEAR_DAYS = 365.25
_MAX_SPAN_DAYS = 10.0 * _YEAR_DAYS
_FALLBACK_DAYS = _YEAR_DAYS
# previous/next altitude answers stay inside the adjacent lunation; the
# same colongitude occurs once there, and the adjacent cycle is enough.
_ALTITUDE_DIRECTION_DAYS = 35.0


def _day_seconds(value: float) -> float:
    return float(value) * 86400.0


def _span_days(value: Any, unit: Any) -> float:
    number = float(value)
    unit = str(unit or "month").strip().lower()
    factor = {"week": 7.0, "month": 30.4375, "year": _YEAR_DAYS}.get(unit)
    if factor is None:
        raise MoonlibError("时间段单位必须是 周 / 月 / 年。")
    if not (number > 0):
        raise MoonlibError("时间段必须是正数。")
    days = number * factor
    if days > _MAX_SPAN_DAYS:
        raise MoonlibError("时间段最多 10 年。")
    return days


def _wrap360(value: float) -> float:
    return (float(value) + 180.0) % 360.0 - 180.0


def _bisect(fn, low: float, high: float, *, iterations: int = 70) -> float:
    """A root in ``[low, high]`` for a continuous sign-changing function."""
    f_low = fn(low)
    f_high = fn(high)
    if f_low == 0.0:
        return low
    if f_high == 0.0:
        return high
    for _ in range(int(iterations)):
        mid = 0.5 * (low + high)
        f_mid = fn(mid)
        if f_mid == 0.0:
            return mid
        if (f_low < 0.0) == (f_mid < 0.0):
            low, f_low = mid, f_mid
        else:
            high, f_high = mid, f_mid
    return 0.5 * (low + high)


def _ternary_min(fn, low: float, high: float, *, iterations: int = 60) -> float:
    """Minimum of a locally unimodal function in ``[low, high]``."""
    for _ in range(int(iterations)):
        left = low + (high - low) / 3.0
        right = high - (high - low) / 3.0
        if fn(left) <= fn(right):
            high = right
        else:
            low = left
    return 0.5 * (low + high)


def _step_seconds(start: float, end: float) -> float:
    span = max(0.0, float(end) - float(start))
    if span <= 2.0 * 86400.0:
        step = 600.0
    elif span <= 40.0 * 86400.0:
        step = 1800.0
    elif span <= 400.0 * 86400.0:
        step = 3600.0
    elif span <= 1500.0 * 86400.0:
        step = 10800.0
    else:
        step = 21600.0
    # A hard ceiling on the grid: the request can be a decade wide, and the
    # browser is waiting for one answer.
    return max(step, span / 60000.0, 60.0)


def _grid(start: float, end: float) -> np.ndarray:
    start = float(start)
    end = float(end)
    if end <= start:
        return np.asarray([start], dtype=np.float64)
    step = _step_seconds(start, end)
    count = int(math.floor((end - start) / step)) + 1
    times = start + np.arange(count, dtype=np.float64) * step
    if times.size == 0 or times[-1] < end - 1e-6:
        times = np.append(times, end)
    else:
        times[-1] = end
    return times


def _new_moons(start: float, end: float) -> List[float]:
    """New-moon instants visible in a window, via a daily probe.

    ``moon_age`` is cheap enough that sampling once a day is a few hundred
    calls for a year; the probe returns the *last* new moon, so each lunation
    is seen by at least one sample.
    """
    start = float(start)
    end = float(end)
    found: List[float] = []
    seen: set = set()
    day = 86400.0
    count = int(math.floor((end - start) / day)) + 2
    for index in range(max(1, count)):
        et = min(end, start + index * day)
        new_moon = float(moon_age(et).new_moon_et)
        key = round(new_moon, 3)
        if key not in seen:
            seen.add(key)
            found.append(new_moon)
    found.sort()
    return found


def _age_candidates(start: float, end: float, target_age: float) -> List[float]:
    """Instants where the Moon's age equals *target_age* in a window.

    Age is the elapsed time since the previous new moon, so the candidate is
    ``new_moon + target``.  A candidate is rejected when the actual lunation is
    shorter than the target and the next new moon has already arrived.
    """
    target_age = max(0.0, min(_SYNODIC_DAYS, float(target_age)))
    out: List[float] = []
    for new_moon in _new_moons(start - _day_seconds(_SYNODIC_DAYS), end):
        candidate = new_moon + _day_seconds(target_age)
        if candidate < start - 1e-6 or candidate > end + 1e-6:
            continue
        if abs(float(moon_age(candidate).age_days) - target_age) > 0.2:
            continue
        if out and abs(candidate - out[-1]) < 60.0:
            continue
        out.append(candidate)
    out.sort()
    return out


def _crossings(times: np.ndarray, values: np.ndarray, target: float,
               value_at, *, tolerance: float) -> List[float]:
    """Roots of ``value(t) - target`` in a sampled interval.

    Sign changes are bisected; local minima of ``|value - target|`` are also
    refined, so a value that only touches the target (0 % or 100 %
    illumination) is not lost.
    """
    times = np.asarray(times, dtype=np.float64)
    values = np.asarray(values, dtype=np.float64)
    if times.size < 2:
        return []
    target = float(target)
    tolerance = max(float(tolerance), 1e-9)
    found: List[float] = []

    def add(et: float) -> None:
        if not any(abs(et - other) < 1.0 for other in found):
            found.append(float(et))

    def f(et: float) -> float:
        return float(value_at(et)) - target

    for index in range(1, times.size):
        left = times[index - 1]
        right = times[index]
        a = values[index - 1] - target
        b = values[index] - target
        if a == 0.0:
            add(left)
        if b == 0.0:
            add(right)
        if (a < 0.0 < b) or (b < 0.0 < a):
            add(_bisect(f, left, right))

    # A tangent touch: the signed difference does not change sign, but its
    # magnitude reaches zero between two neighbours.
    for index in range(1, times.size - 1):
        before = abs(values[index - 1] - target)
        here = abs(values[index] - target)
        after = abs(values[index + 1] - target)
        if here <= before and here <= after and here <= tolerance * 8.0:
            refined = _ternary_min(
                lambda et: abs(f(et)), times[index - 1], times[index + 1]
            )
            if abs(f(refined)) <= tolerance:
                add(refined)
    found.sort()
    return found


def _libration_arrays(times: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    lon = np.empty(times.size, dtype=np.float64)
    lat = np.empty(times.size, dtype=np.float64)
    colong = np.empty(times.size, dtype=np.float64)
    for index, et in enumerate(times):
        libration = libration_angles(float(et))
        lon[index] = float(libration.libration_lon_deg)
        lat[index] = float(libration.libration_lat_deg)
        colong[index] = float(libration.colongitude_deg)
    return lon, lat, colong


def _similar_libration_candidates(
    times: np.ndarray, lon: np.ndarray, lat: np.ndarray,
    target_lon: float, target_lat: float, error: float,
) -> List[float]:
    """One representative time per contiguous interval that is close enough."""
    close = (
        (np.abs(lon - target_lon) <= error)
        & (np.abs(lat - target_lat) <= error)
    )
    found: List[float] = []
    index = 0
    while index < close.size:
        if not close[index]:
            index += 1
            continue
        end = index
        while end + 1 < close.size and close[end + 1]:
            end += 1
        # The best time inside the interval is the one whose larger component
        # is closest to the reference, not merely the first sample.
        span = slice(index, end + 1)
        score = np.maximum(
            np.abs(lon[span] - target_lon), np.abs(lat[span] - target_lat)
        )
        best = index + int(np.argmin(score))
        low = times[max(index, best - 1)]
        high = times[min(end, best + 1)]

        def distance(et: float) -> float:
            libration = libration_angles(float(et))
            return max(
                abs(float(libration.libration_lon_deg) - target_lon),
                abs(float(libration.libration_lat_deg) - target_lat),
            )

        refined = _ternary_min(distance, low, high)
        candidate = refined if distance(refined) <= error else times[best]
        if not any(abs(candidate - other) < 1.0 for other in found):
            found.append(float(candidate))
        index = end + 1
    found.sort()
    return found


def _row(site: Observer, et: float, *, outside: bool = False) -> Dict[str, Any]:
    state = moon_state(site, et)
    libration = state.libration
    return {
        "when_utc": to_utc(et).isoformat(),
        "age_days": float(state.phase.age_days),
        "illumination_percent": float(state.phase.illuminated_percent_topo),
        "azimuth_deg": float(state.azimuth_deg),
        "altitude_deg": float(state.elevation_deg),
        "libration_lon_deg": float(libration.libration_lon_deg),
        "libration_lat_deg": float(libration.libration_lat_deg),
        "angular_diameter_arcmin": float(state.angular_diameter_arcsec) / 60.0,
        "colongitude_deg": float(libration.colongitude_deg),
        "visible": bool(state.elevation_deg > 0.0),
        "outside_span": bool(outside),
    }


def _refine_extreme(times: np.ndarray, elevations: np.ndarray,
                    index: int, observer: Observer, highest: bool) -> float:
    low = times[max(0, index - 1)]
    high = times[min(times.size - 1, index + 1)]

    def value(et: float) -> float:
        state = moon_state(observer, et)
        # ``_ternary_min`` minimises, so the "highest" case minimises the
        # negative elevation.
        return -float(state.elevation_deg) if highest else float(state.elevation_deg)

    return _ternary_min(value, low, high)


def _altitude_candidates(times: np.ndarray, elevations: np.ndarray,
                         observer: Observer, mode: str,
                         *, local_extrema: bool) -> List[float]:
    """Extrema of the Moon's altitude.

    ``past/future`` asks for the one global highest/lowest instant in the
    period; ``previous/next`` asks for the nearest transit, so every local
    maximum (and minimum, for completeness) is refined and the caller picks
    the nearest side.
    """
    highest = mode == "highest"
    found: List[float] = []
    if local_extrema:
        for index in range(1, times.size - 1):
            value = elevations[index]
            before = elevations[index - 1]
            after = elevations[index + 1]
            if highest and value >= before and value >= after:
                found.append(_refine_extreme(
                    times, elevations, index, observer, True))
            elif not highest and value <= before and value <= after:
                found.append(_refine_extreme(
                    times, elevations, index, observer, False))
        return sorted(set(found))
    index = int(np.argmax(elevations) if highest else np.argmin(elevations))
    return [_refine_extreme(times, elevations, index, observer, highest)]


def _collect_candidates(
    *, observer: Observer, start: float, end: float, target: str, mode: str,
    reference: Dict[str, float], error: Optional[float],
    local_extrema: bool = False,
    nearest_to: Optional[float] = None,
    side: Optional[str] = None,
) -> List[float]:
    """Candidate instants for one window, before visibility is considered."""
    if target == "age":
        return _age_candidates(start, end, reference["age"])
    times = _grid(start, end)
    if times.size < 2:
        return []

    if target == "illumination":
        geometry = scan_geometry(times, observer)
        values = np.asarray(geometry.illumination_topo, dtype=np.float64) * 100.0

        def value_at(et: float) -> float:
            return float(moon_state(observer, et).phase.illuminated_percent_topo)

        return _crossings(
            times, values, reference["illumination"], value_at,
            tolerance=_SAME_TOLERANCE["illumination"],
        )

    if target in ("libration", "shadow"):
        lon, lat, colong = _libration_arrays(times)
        if target == "libration":
            assert error is not None
            return _similar_libration_candidates(
                times, lon, lat, reference["libration_lon"],
                reference["libration_lat"], float(error),
            )

        def value_at(et: float) -> float:
            return float(libration_angles(et).colongitude_deg)

        # The colongitude wraps at 360°; the sawtooth difference changes sign
        # at the target and at the opposite side, but the latter is the jump
        # from +180 to -180, not a root.  A bracket around a sign change is
        # narrow (one grid step, under a degree), so it never contains it.
        differences = np.asarray([_wrap360(float(v) - reference["shadow"])
                                  for v in colong], dtype=np.float64)
        found: List[float] = []
        for index in range(1, times.size):
            a = differences[index - 1]
            b = differences[index]
            # The sawtooth has a second sign change 180° away, where it jumps
            # from +180 to -180.  Both sides of the real crossing are close to
            # zero; both sides of that jump are close to ±180.
            if ((a < 0.0 < b) or (b < 0.0 < a)) and max(abs(a), abs(b)) < 90.0:
                root = _bisect(
                    lambda et: _wrap360(float(value_at(et)) - reference["shadow"]),
                    times[index - 1], times[index],
                )
                found.append(float(root))
        return found

    if target == "altitude":
        if mode not in ("highest", "lowest"):
            raise MoonlibError("高度角只支持最高/最低。")
        geometry = scan_geometry(times, observer, with_horizontal=True)
        elevations = np.asarray(geometry.elevation_deg, dtype=np.float64)
        if local_extrema and nearest_to is not None:
            highest = mode == "highest"
            indices = []
            for index in range(1, times.size - 1):
                value = elevations[index]
                before = elevations[index - 1]
                after = elevations[index + 1]
                if highest and value >= before and value >= after:
                    indices.append(index)
                elif not highest and value <= before and value <= after:
                    indices.append(index)
            if not indices:
                return []
            if side == "previous":
                usable = [index for index in indices
                          if times[index] < nearest_to]
                index = max(usable, key=lambda item: times[item]) if usable else None
            else:
                usable = [index for index in indices
                          if times[index] > nearest_to]
                index = min(usable, key=lambda item: times[item]) if usable else None
            if index is None:
                return []
            return [_refine_extreme(
                times, elevations, index, observer, highest)]
        return _altitude_candidates(
            times, elevations, observer, mode,
            local_extrema=local_extrema,
        )

    raise MoonlibError(f"未知的检索目标：{target}")


def _visible_altitude_extreme(observer: Observer, start: float, end: float,
                              mode: str) -> Optional[float]:
    """The first visible local extremum of altitude after *start*.

    Used for the "时间段外下一次可见事件" fallback.  Only extrema are refined,
    and the loop stops at the first one above the horizon, so the usual case
    costs one refinement rather than a year of them.
    """
    times = _grid(start, end)
    if times.size < 3:
        return None
    geometry = scan_geometry(times, observer, with_horizontal=True)
    elevations = np.asarray(geometry.elevation_deg, dtype=np.float64)
    highest = mode == "highest"
    for index in range(1, times.size - 1):
        value = elevations[index]
        before = elevations[index - 1]
        after = elevations[index + 1]
        if highest and not (value >= before and value >= after):
            continue
        if not highest and not (value <= before and value <= after):
            continue
        refined = _refine_extreme(times, elevations, index, observer, highest)
        if _row(observer, refined)["visible"]:
            return refined
    return None


def _reference_values(observer: Observer, et: float) -> Dict[str, float]:
    state = moon_state(observer, et)
    return {
        "age": float(state.phase.age_days),
        "illumination": float(state.phase.illuminated_percent_topo),
        "libration_lon": float(state.libration.libration_lon_deg),
        "libration_lat": float(state.libration.libration_lat_deg),
        "shadow": float(state.libration.colongitude_deg),
        "altitude": float(state.elevation_deg),
    }


def _query_allowed(direction: str, target: str, mode: str) -> None:
    if direction in ("previous", "next"):
        allowed = {
            "age": ("same",),
            "illumination": ("same",),
            "shadow": ("same",),
            "libration": ("similar",),
            "altitude": ("highest",),
        }[target]
    else:
        allowed = {
            "age": ("same",),
            "illumination": ("same",),
            "shadow": ("same",),
            "libration": ("similar",),
            "altitude": ("highest", "lowest"),
        }[target]
    if mode not in allowed:
        raise MoonlibError(
            f"{target} 在 {direction} 下只支持 {', '.join(allowed)}。"
        )


def search_conditions(
    observer: Observer,
    when: Any,
    *,
    direction: str,
    target: str,
    mode: str,
    span_value: Optional[float] = None,
    span_unit: str = "month",
    error: Optional[float] = None,
    max_results: int = 200,
) -> Dict[str, Any]:
    """Find instants satisfying one calculator sentence.

    Parameters mirror the page's five controls: *direction* (previous / next /
    past / future), *target* (age / illumination / libration / shadow /
    altitude), *mode* (same / similar / highest / lowest), the optional span for
    past/future, and the similarity *error* in the target's own unit.
    """
    direction = str(direction or "").strip().lower()
    target = str(target or "").strip().lower()
    mode = str(mode or "").strip().lower()
    if direction not in CALCULATOR_DIRECTIONS:
        raise MoonlibError("检索方向必须是 上一次 / 下一次 / 过去 / 未来。")
    if target not in CALCULATOR_TARGETS:
        raise MoonlibError("检索目标必须是 月龄 / 照明度 / 天平动 / 阴影带 / 高度角。")
    if mode not in CALCULATOR_MODES:
        raise MoonlibError("检索方式必须是 相同 / 相似 / 最高 / 最低。")
    _query_allowed(direction, target, mode)

    site = Observer.from_value(observer)
    reference_et = to_et(when if when is not None else utc_now())
    reference = _reference_values(site, reference_et)
    limit = int(max(1, min(500, max_results)))

    if mode == "similar":
        if error is None or not (float(error) > 0.0):
            raise MoonlibError("相似检索需要一个正误差。")
        error = float(error)
    else:
        error = None

    if direction in ("previous", "next"):
        window_days = (_ALTITUDE_DIRECTION_DAYS if target == "altitude"
                       else _FALLBACK_DAYS)
        if direction == "previous":
            search_start = reference_et - (window_days * 86400.0)
            search_end = reference_et
        else:
            search_start = reference_et
            search_end = reference_et + (window_days * 86400.0)
    else:
        days = _span_days(span_value, span_unit)
        if direction == "past":
            search_start = reference_et - days * 86400.0
            search_end = reference_et
        else:
            search_start = reference_et
            search_end = reference_et + days * 86400.0
    window_start, window_end = search_start, search_end

    # 高度角最高/最低 means "among the Moons with the same colongitude as
    # the reference, the one highest/lowest in the observer's sky".  It is a
    # terminator-constrained altitude query, not a free global altitude extreme.
    altitude_query = target == "altitude"
    candidates = _collect_candidates(
        observer=site, start=search_start, end=search_end,
        target="shadow" if altitude_query else target,
        mode="same" if altitude_query else mode,
        reference=reference,
        error=None if altitude_query else error,
    )
    # The reference instant itself is the question, not an answer.
    candidates = [et for et in candidates if abs(et - reference_et) > 60.0]
    candidates.sort()

    if altitude_query:
        if direction == "previous":
            candidates = [et for et in candidates if et < reference_et]
        elif direction == "next":
            candidates = [et for et in candidates if et > reference_et]
        if candidates:
            pick = max if mode == "highest" else min
            selected = [pick(candidates, key=lambda et: _row(site, et)["altitude_deg"])]
        else:
            selected = []
    elif direction == "previous":
        candidates = [et for et in candidates if et < reference_et]
        selected = candidates[-1:] if candidates else []
    elif direction == "next":
        candidates = [et for et in candidates if et > reference_et]
        selected = candidates[:1]
    else:
        selected = candidates

    fallback = False
    outside = False
    if not selected and direction in ("past", "future"):
        # "若时间段内没有，则显示时间段外的下一次可见事件": continue forward
        # for up to one year, and keep only an instant the Moon is above the
        # horizon at.
        fallback_start = search_end
        fallback_end = fallback_start + _FALLBACK_DAYS * 86400.0
        later = _collect_candidates(
            observer=site, start=fallback_start, end=fallback_end,
            target="shadow" if altitude_query else target,
            mode="same" if altitude_query else mode,
            reference=reference,
            error=None if altitude_query else error,
        )
        later = sorted(et for et in later if et > search_end + 60.0)
        for et in later:
            if _row(site, et)["visible"]:
                selected = [et]
                fallback = True
                outside = True
                break
        if not selected:
            fallback = True

    rows = [_row(site, et, outside=outside) for et in selected]
    rows.sort(key=lambda row: row["when_utc"])
    truncated = len(rows) > limit
    rows = rows[:limit]
    return {
        "reference_utc": to_utc(reference_et).isoformat(),
        "window": {
            "start_utc": to_utc(window_start).isoformat(),
            "end_utc": to_utc(window_end).isoformat(),
        },
        "query": {
            "direction": direction,
            "target": target,
            "mode": mode,
            "span_value": span_value,
            "span_unit": span_unit,
            "error": error,
        },
        "items": rows,
        "fallback": bool(fallback),
        "outside_span": bool(outside),
        "truncated": bool(truncated),
    }
