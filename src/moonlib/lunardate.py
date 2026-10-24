#!/usr/bin/env python3
"""The Chinese lunisolar calendar, computed from this project's own ephemeris.

The page used to print a lunar day guessed from the Moon's age: ``day =
floor(age) + 1``.  That is a display approximation, and it is wrong whenever the
civil new moon falls late in the day -- the real calendar's month length is 29 or
30 days decided by the *instant* of the conjunction in Beijing civil time, and a
leap month is inserted by a rule that no age can express.

This module computes the calendar the way GB/T 33661-2017 defines it, using the
same SPICE ephemeris the rest of the library uses:

* a month starts on the Beijing (UTC+8) civil date containing the new moon;
* the month containing the December solstice is month 11;
* between two such months there are 12 or 13 months; if 13, the first month that
  contains no *major solar term* (中气, solar longitude a multiple of 30°) is a
  leap month, numbered after the month it follows;
* day 1 is the first day of a month, so a month has 29 or 30 days.

Nothing here is a table of historical data, so the calendar is as good as the
ephemeris: it is correct for any date the kernels cover, not just for a range a
table happens to list.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from . import sun
from .phases import find_new_moons
from .timeutil import as_time_range, to_et, to_utc

__all__ = [
    "LunarDate",
    "chinese_lunar_date",
    "lunar_date_label",
]

#: The Chinese calendar is defined in Beijing civil time, which has been UTC+8
#: since 1929 and is UTC+8 today; the ephemeris work is all UTC.
BEIJING_OFFSET = timezone(timedelta(hours=8))

_MONTH_NAMES = ("正", "二", "三", "四", "五", "六",
                "七", "八", "九", "十", "冬", "腊")
_MONTH_NAMES_LATIN = ("Zheng", "Er", "San", "Si", "Wu", "Liu",
                      "Qi", "Ba", "Jiu", "Shi", "Dong", "La")
_DAY_NAMES = (
    "初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十",
    "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
    "廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十",
)

#: Terms are searched a day apart: the Sun moves about one degree a day, so a
#: crossing cannot fall between two samples.
_SCAN_STEP_DAYS = 1.0


@dataclass(frozen=True)
class LunarDate:
    """One day in the Chinese calendar."""

    year: int
    month: int
    day: int
    is_leap: bool
    #: The civil date (Beijing, UTC+8) this refers to.
    civil_date: date

    @property
    def label_zh(self) -> str:
        return f"{'闰' if self.is_leap else ''}{_MONTH_NAMES[self.month - 1]}月" \
               f"{_DAY_NAMES[self.day - 1]}"

    @property
    def label_latin(self) -> str:
        return (f"{'L' if self.is_leap else ''}M{self.month:02d}"
                f"D{self.day:02d}")

    def as_dict(self) -> Dict[str, object]:
        return {
            "year": int(self.year),
            "month": int(self.month),
            "day": int(self.day),
            "is_leap": bool(self.is_leap),
            "label_zh": self.label_zh,
            "label_latin": self.label_latin,
            "civil_date": self.civil_date.isoformat(),
        }

    def __str__(self) -> str:
        return self.label_zh


def _beijing_date(et: float) -> date:
    """The Beijing civil date of an ephemeris instant.

    Through :func:`moonlib.timeutil.to_utc`, which knows that J2000 is 12:00
    *TT* and not 12:00 UTC: doing the arithmetic here instead is a 64-second
    error, and 64 seconds is enough to put an evening conjunction in the wrong
    month.
    """
    return to_utc(float(et)).astimezone(BEIJING_OFFSET).date()


def _utc_from_date(day: date, hour: float = 0.0) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(hours=hour)


def _solar_longitude(et: float) -> float:
    """Apparent geocentric solar longitude, degrees.

    Apparent, not geometric: the calendar's terms are defined by the *apparent*
    Sun (the difference is about 20 arcseconds, i.e. tens of seconds of time,
    enough to move a term across midnight once in a while).
    """
    return sun.apparent_ecliptic_longitude_deg(et)


def _find_term(target_deg: float, start_et: float, end_et: float) -> float:
    """The instant in ``[start_et, end_et]`` at which the Sun crosses *target*."""
    def residual(et: float) -> float:
        delta = (_solar_longitude(et) - target_deg) % 360.0
        # The crossing is where this passes 0 from below: map to [-180, 180)
        # so the root finder sees a monotone function through the crossing.
        return delta if delta < 180.0 else delta - 360.0

    low, high = float(start_et), float(end_et)
    f_low = residual(low)
    if f_low > 0.0:  # pragma: no cover - callers bracket the crossing
        low -= 5.0
        f_low = residual(low)
    for _ in range(60):
        middle = 0.5 * (low + high)
        if high - low < 1.0:
            return middle
        if residual(middle) < 0.0:
            low = middle
        else:
            high = middle
    return 0.5 * (low + high)


def _winter_solstice_et(year: int) -> float:
    """December solstice of one Gregorian year (apparent longitude 270°)."""
    start = to_et(f"{year}-12-01T00:00:00")
    end = to_et(f"{year}-12-31T23:59:59")
    return _find_term(270.0, start, end)


def _new_moon_dates(start_et: float, end_et: float) -> List[Tuple[float, date]]:
    """New moons between two instants, as ``(et, Beijing civil date)``."""
    events = find_new_moons(as_time_range(start_et, end_et))
    return [(event.et, _beijing_date(event.et)) for event in events]


def _major_terms_between(start_et: float, end_et: float) -> List[float]:
    """The 中气 (solar longitude multiple of 30°) inside a span, as instants."""
    terms: List[float] = []
    step = _SCAN_STEP_DAYS * 86400.0
    previous = _solar_longitude(start_et)
    et = start_et
    while et < end_et:
        nxt = min(et + step, end_et)
        current = _solar_longitude(nxt)
        # Which multiples of 30 degrees the Sun passed in this interval.
        first = int(math.floor(previous / 30.0))
        last = int(math.floor(current / 30.0))
        if current < previous:          # wrapped through 360 -> 0
            last += 12
        for index in range(first + 1, last + 1):
            target = (index * 30.0) % 360.0
            terms.append(_find_term(target, et, nxt))
        previous = current
        et = nxt
    return terms


class _Sui:
    """One solstice-to-solstice year: its months, numbered the standard way."""

    def __init__(self, winter_year: int) -> None:
        self.winter_year = winter_year
        w0 = _winter_solstice_et(winter_year)
        w1 = _winter_solstice_et(winter_year + 1)
        # New moons from a month before the first solstice to one after the
        # second: enough to bracket both month boundaries comfortably.
        # The window has to reach past the *second* solstice far enough to
        # include the new moon that starts the next suì, or the last month of
        # this one is truncated and its days become unplaceable.
        new_moons = _new_moon_dates(w0 - 40 * 86400.0, w1 + 45 * 86400.0)

        start_index = 0
        for index, (_et, day) in enumerate(new_moons):
            if day > _beijing_date(w0):
                start_index = max(0, index - 1)
                break
        # The month containing the solstice is month 11; the *next* such month
        # ends this suì, so the months of interest run from start_index to the
        # new moon on or before the following solstice.
        # The suì ends where the *next* suì begins: at the month containing the
        # following solstice (that month is month 11 of the next suì and must
        # not be counted here, or a twelve-month year looks like a leap year).
        solstice_date = _beijing_date(w1)
        end_index = start_index + 1
        for index in range(start_index + 1, len(new_moons)):
            if new_moons[index][1] > solstice_date:
                break
            end_index = index
        months = new_moons[start_index:end_index]
        starts = [day for _et, day in months]
        spans_et = [et for et, _day in months]
        # The suì runs up to (not including) the month that holds the *next*
        # solstice: that month is month 11 of the following suì.
        self_end = new_moons[end_index][1] if end_index < len(new_moons) else (starts[-1] + timedelta(days=30))

        # Which months hold a 中气: the rule that decides the leap month.
        terms = _major_terms_between(spans_et[0] - 40 * 86400.0,
                                     spans_et[-1] + 40 * 86400.0)
        term_dates = [_beijing_date(et) for et in terms]
        has_term: List[bool] = []
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else self_end
            inside = [day for day in term_dates if start <= day < end]
            # A month can hold several 中气 when the span is long; the standard
            # only asks whether it holds one.
            has_term.append(bool(inside))

        leap_index: Optional[int] = None
        if len(starts) == 13:
            # Month 11 is index 0; the first month after it with no 中气 is the
            # leap month.  A leap month can never be month 11 or 12 by the rule.
            for index in range(1, len(starts) - 1):
                if not has_term[index]:
                    leap_index = index
                    break
            if leap_index is None:  # pragma: no cover - 13 months implies one
                leap_index = len(starts) - 2

        # Numbering: 11, 12, then 1, 2, ... A leap month repeats the number of
        # the month before it and does not advance the count.
        numbered: List[Tuple[int, bool]] = []
        number = 11
        last = 11
        for index in range(len(starts)):
            if index == leap_index:
                # A leap month repeats the number of the month before it and
                # does not advance the count: 四月, 闰四月, 五月.
                numbered.append((last, True))
                continue
            numbered.append((number, False))
            last = number
            number = number + 1 if number < 12 else 1

        self.starts = starts
        self.numbers = numbered
        self.end = self_end

    def contains(self, day: date) -> bool:
        return self.starts[0] <= day < self.end

    def month_for(self, day: date) -> Optional[Tuple[int, bool, int, date]]:
        if not self.contains(day):
            return None
        index = len(self.starts) - 1
        for position in range(len(self.starts)):
            end = (self.starts[position + 1]
                   if position + 1 < len(self.starts) else self.end)
            if self.starts[position] <= day < end:
                index = position
                break
        number, is_leap = self.numbers[index]
        return number, is_leap, (day - self.starts[index]).days + 1, self.starts[index]

    @property
    def new_year_date(self) -> Optional[date]:
        """The 正月初一 of this suì (its month 1), if it has one."""
        for index, (number, is_leap) in enumerate(self.numbers):
            if number == 1 and not is_leap:
                return self.starts[index]
        return None


_SUI_CACHE: Dict[int, _Sui] = {}


def _sui(winter_year: int) -> _Sui:
    cached = _SUI_CACHE.get(winter_year)
    if cached is None:
        cached = _Sui(winter_year)
        if len(_SUI_CACHE) > 8:
            _SUI_CACHE.pop(next(iter(_SUI_CACHE)))
        _SUI_CACHE[winter_year] = cached
    return cached


def chinese_lunar_date(when: object) -> LunarDate:
    """The Chinese calendar date for an instant.

    *when* is anything :func:`moonlib.timeutil.to_et` accepts (an ISO string,
    a :class:`datetime`, or an ET float).  The result is expressed in Beijing
    civil time, which is what "the Chinese calendar" means, whatever zone the
    caller is reading the sky from.
    """
    et = to_et(when) if not isinstance(when, (int, float)) else float(when)
    civil = _beijing_date(et)
    # The solstice year whose months contain this date: the suì starting in the
    # previous December does; the one starting this December covers dates after
    # mid-December.
    for winter_year in (civil.year, civil.year - 1, civil.year + 1):
        sui = _sui(winter_year)
        found = sui.month_for(civil)
        if found is None:
            continue
        number, is_leap, day, _start = found
        # Months 11 and 12 come before the suì's 正月初一, so their lunar year is
        # the Gregorian year in which this suì's new year falls minus one.
        new_year = sui.new_year_date
        year = civil.year if new_year is None else new_year.year
        if number >= 11:
            year -= 1
        return LunarDate(year=int(year), month=int(number), day=int(day),
                         is_leap=bool(is_leap), civil_date=civil)
    raise RuntimeError(f"cannot place the date {civil} in the Chinese calendar")


def lunar_date_label(when: object, style: str = "zh") -> str:
    """:func:`chinese_lunar_date` as a short label (``八月十五`` / ``M08D15``)."""
    lunar = chinese_lunar_date(when)
    return lunar.label_latin if style == "latin" else lunar.label_zh
