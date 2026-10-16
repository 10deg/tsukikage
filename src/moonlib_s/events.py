"""Event forecasts: what is worth putting in a diary.

Six things, all of them answers to "when should I look?":

``full_moon``
    The principal phase instant itself, from :mod:`moonlib_s.phases`.
``supermoon`` / ``micromoon``
    A full moon that happens near perigee or near apogee.  There is no official
    threshold -- the word was invented by an astrologer in 1979 -- so the two
    used here are stated rather than implied: within 360 000 km and beyond
    405 000 km of the Earth's centre at the instant of full moon, which is
    roughly the nearest and farthest tenth of the orbit.
``highest_full`` / ``highest_quarter``
    The full moon, and the quarter moons, that climb highest for *this*
    observer in the range.  Altitude at transit is a property of the observer's
    latitude and the Moon's declination, so this one genuinely needs a site.
``lunar_eclipse``
    Penumbral, partial or total, found from the geometry of the Earth's shadow
    rather than from a table of eclipses: the Moon is eclipsed when it passes
    close enough to the anti-solar direction, and the shadow's radii at the
    Moon's distance follow from the Sun's and the Earth's apparent sizes.

The eclipse model is the classical one and is honest about what it is: it finds
the instant of greatest eclipse and classifies it.  It does **not** compute
contact times to the second -- that needs the full Besselian treatment -- and it
does not correct for the atmosphere's enlargement of the Earth's shadow, which
is why the umbra radius carries the conventional 2% allowance.

``xvl`` is listed as a kind and returns nothing but a note: the Moon's "X" and
"V" are lighting effects on a handful of craters near the terminator, and
predicting them properly is a piece of work of its own.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .errors import MoonlibError
from .frames import az_el
from .kernels import kernel_session
from .moon import geocentric_phase_angle_deg
from .observer import Observer
from .phases import PhaseKind, find_phase_events
from .timeutil import as_time_range, to_et, to_utc
from . import spice

__all__ = [
    "EVENT_KINDS",
    "MoonEvent",
    "moon_events",
]

#: Distance at which a full moon counts as a "supermoon", km.
SUPERMOON_KM = 360_000.0
#: ...and as a micromoon ("minimoon"), km.
MICROMOON_KM = 405_000.0

#: The Earth's umbra and penumbra, as angular radii at the Moon's distance,
#: from the standard geometrical construction: the Sun's disc is 0.2666° in
#: radius at 1 AU, the Earth's is ``arcsin(R_earth / d)``.
#:
#: The umbra is enlarged by 2% for the atmosphere, which is the convention every
#: almanac uses -- refraction lifts the shadow's edge by about that much, and
#: without it predicted total eclipses come out slightly too short.
UMBRA_ENLARGEMENT = 1.02
SUN_ANGULAR_RADIUS_DEG = 0.2666

KIND_LABELS = {
    "full_moon": "满月",
    "supermoon": "超级月亮",
    "micromoon": "最小月亮",
    "highest_full": "最高满月",
    "highest_quarter": "最高弦月",
    "lunar_eclipse": "月食",
    "xvl": "月面X / V / L",
}

#: A lunar eclipse *is* 月全食 or 月偏食; "月食 偏食" says the same thing twice
#: and makes the reader join two halves to learn what happened.
ECLIPSE_TITLES = {
    "total": "月全食",
    "partial": "月偏食",
    "penumbral": "月半影食",
}

#: Every kind the page can ask for, in the order it shows them.
EVENT_KINDS: Tuple[str, ...] = (
    "full_moon", "supermoon", "micromoon", "highest_full", "highest_quarter",
    "lunar_eclipse", "xvl",
)


def _diameter_text(radius_deg: float) -> str:
    """The apparent diameter that goes with an angular radius, as ′ and ″.

    The Moon's own size is the thing a reader compares between a supermoon and
    a micromoon -- more than the distance that produced it -- so the events for
    full moons report the diameter rather than leaving them to convert.
    """
    total_arcsec = 2.0 * float(radius_deg) * 3600.0
    minutes = int(total_arcsec // 60.0)
    seconds = total_arcsec - 60.0 * minutes
    return f"{minutes}′ {seconds:04.1f}″"


def _altitude_text(altitude_deg: float) -> str:
    """Signed altitude, so a row below the horizon reads as negative."""
    return f"高度 {float(altitude_deg):+.1f}°"


@dataclass(frozen=True)
class MoonEvent:
    """One forecast instant, with the two things a reader needs to judge it."""

    kind: str
    when_utc: datetime
    #: Moon altitude at that instant for the observer, degrees.
    altitude_deg: float
    #: Free text: the diameter, the eclipse magnitude, the altitude at transit.
    detail: str
    #: False when the Moon is below the horizon where the reader is.
    observable: bool
    #: Display name when the *kind* is not the whole answer -- a lunar eclipse
    #: is 月偏食, not "月食" with the type tacked on after it.
    title: Optional[str] = None

    @property
    def label(self) -> str:
        return self.title or KIND_LABELS.get(self.kind, self.kind)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "when_utc": self.when_utc.isoformat(),
            "altitude_deg": round(self.altitude_deg, 2),
            "detail": self.detail,
            "observable": self.observable,
        }


def _geometry(et: float) -> Tuple[float, float, float]:
    """``(distance_km, moon_radius_deg, anti_solar_separation_deg)``.

    The separation is between the Moon's centre and the point exactly opposite
    the Sun as seen from the Earth's centre -- the axis of the Earth's shadow.
    """
    session = kernel_session()
    session.require_epoch(et, what="event search")
    state = np.asarray(spice.spkezr("MOON", et, "J2000", "NONE", "EARTH")[0], dtype=float)
    moon = state[:3]
    distance = float(np.linalg.norm(moon))
    radius_deg = math.degrees(math.asin(
        session.constants.moon_radius_km / distance))
    sun = np.asarray(spice.spkezr("SUN", et, "J2000", "NONE", "EARTH")[0][:3], dtype=float)
    # The anti-solar direction, and the angle between it and the Moon.
    anti = -sun / float(np.linalg.norm(sun))
    direction = moon / distance
    cosine = float(np.clip(np.dot(anti, direction), -1.0, 1.0))
    separation_deg = math.degrees(math.acos(cosine))
    return distance, radius_deg, separation_deg


def _shadow_radii_deg(distance_km: float) -> Tuple[float, float]:
    """The umbra's and the penumbra's angular radii at the Moon's distance."""
    earth_radius = kernel_session().constants.earth_radius_km
    earth_deg = math.degrees(math.asin(earth_radius / distance_km))
    # The umbra is what the Earth hides of the Sun: the Earth's disc minus the
    # Sun's, both as seen from the Moon's distance.  The penumbra is their sum.
    umbra = (earth_deg - SUN_ANGULAR_RADIUS_DEG) * UMBRA_ENLARGEMENT
    penumbra = earth_deg + SUN_ANGULAR_RADIUS_DEG
    return umbra, penumbra


def _eclipse_at(et: float) -> Optional[Tuple[str, float]]:
    """``(type, separation)`` when the Moon is in the shadow at *et*."""
    distance, radius_deg, separation = _geometry(et)
    umbra, penumbra = _shadow_radii_deg(distance)
    if separation <= umbra - radius_deg:
        return "total", separation
    if separation <= umbra + radius_deg:
        return "partial", separation
    if separation <= penumbra + radius_deg:
        return "penumbral", separation
    return None


def _greatest_eclipse(et_full: float) -> Optional[Tuple[str, float, datetime]]:
    """Find the eclipse around a full moon, if there is one.

    The minimum separation is bracketed by a coarse scan and then refined by
    golden-section search: the separation is smooth and unimodal over the few
    hours that matter, so a few dozen evaluations land within seconds.

    Returns ``(type, magnitude, instant)``; the *type* is one of ``total``,
    ``partial`` or ``penumbral``.
    """
    span = 6.0 * 3600.0
    grid = np.linspace(et_full - span, et_full + span, 25)
    values = [_geometry(float(et))[2] for et in grid]
    index = int(np.argmin(values))
    low = float(grid[max(0, index - 1)])
    high = float(grid[min(len(grid) - 1, index + 1)])
    golden = (math.sqrt(5.0) - 1.0) / 2.0
    for _ in range(60):
        left = high - golden * (high - low)
        right = low + golden * (high - low)
        if _geometry(left)[2] < _geometry(right)[2]:
            high = right
        else:
            low = left
    et = 0.5 * (low + high)
    found = _eclipse_at(et)
    if found is None:
        return None
    kind_name, separation = found
    distance, radius_deg, _ = _geometry(et)
    umbra, penumbra = _shadow_radii_deg(distance)
    # Magnitude: how much of the Moon's diameter is inside the umbra, the
    # number an almanac prints.
    if umbra > 0:
        magnitude = (umbra + radius_deg - separation) / (2.0 * radius_deg)
    else:  # pragma: no cover - the umbra is never negative at the Moon
        magnitude = 0.0
    return kind_name, max(0.0, magnitude), to_utc(et)


def _altitude(et: float, observer: Observer) -> float:
    return float(az_el("MOON", et, observer).elevation_deg)


def _transit_altitude(et: float, observer: Observer) -> float:
    """The highest the Moon gets around *et*: a coarse scan of the day."""
    grid = np.linspace(et - 12 * 3600.0, et + 12 * 3600.0, 49)
    values = [_altitude(float(item), observer) for item in grid]
    return max(values)


def _full_moon_events(observer: Observer, window) -> List[MoonEvent]:
    """Full moons, super/mini moons, and the eclipses among them.

    Every one of them carries the Moon's apparent diameter and the altitude it
    stands at: the diameter is what distinguishes a supermoon from a micromoon
    to the eye, and the altitude is what decides whether the reader can see it
    at all -- which the row's own greyed-out state only hints at.
    """
    events: List[MoonEvent] = []
    for phase in find_phase_events(window, (PhaseKind.FULL_MOON,)):
        distance, radius_deg, _separation = _geometry(phase.et)
        altitude = _altitude(phase.et, observer)
        size = f"视直径 {_diameter_text(radius_deg)}"
        where = f"地心距 {distance:,.0f} km"
        height = _altitude_text(altitude)
        events.append(MoonEvent(
            kind="full_moon", when_utc=phase.when_utc, altitude_deg=altitude,
            detail=f"{size}　{where}　{height}",
            observable=altitude > 0.0,
        ))
        if distance <= SUPERMOON_KM:
            events.append(MoonEvent(
                kind="supermoon", when_utc=phase.when_utc, altitude_deg=altitude,
                detail=f"{size}　{where}（≤ {SUPERMOON_KM:,.0f}）　{height}",
                observable=altitude > 0.0,
            ))
        if distance >= MICROMOON_KM:
            events.append(MoonEvent(
                kind="micromoon", when_utc=phase.when_utc, altitude_deg=altitude,
                detail=f"{size}　{where}（≥ {MICROMOON_KM:,.0f}）　{height}",
                observable=altitude > 0.0,
            ))
        eclipse = _greatest_eclipse(phase.et)
        if eclipse is not None:
            kind_name, magnitude, when_utc = eclipse
            at = to_et(when_utc)
            height = _altitude(at, observer)
            events.append(MoonEvent(
                kind="lunar_eclipse", when_utc=when_utc,
                altitude_deg=height,
                # The type is the event's name, not a suffix on the kind.
                title=ECLIPSE_TITLES[kind_name],
                detail=f"食分 {magnitude:.2f}　{_altitude_text(height)}",
                observable=height > 0.0,
            ))
    return events


def _highest_event(observer: Observer, window,
                   kind: PhaseKind, name: str, label: str) -> Optional[MoonEvent]:
    """The highest transit of one phase in the range."""
    best: Optional[Tuple[float, float]] = None
    for phase in find_phase_events(window, (kind,)):
        height = _transit_altitude(phase.et, observer)
        if best is None or height > best[0]:
            best = (height, phase.et)
    if best is None:
        return None
    height, et = best
    # The event is the *transit*, not the phase instant: that is when it is
    # highest, and for a reader "highest in the sky" is the whole point.
    grid = np.linspace(et - 12 * 3600.0, et + 12 * 3600.0, 49)
    transit = max(grid, key=lambda item: _altitude(float(item), observer))
    return MoonEvent(
        kind=name, when_utc=to_utc(float(transit)), altitude_deg=height,
        detail=f"{label}　中天高度 {height:.1f}°", observable=height > 0.0,
    )


def moon_events(
    observer: Observer,
    start: Any,
    end: Any,
    *,
    kinds: Optional[Iterable[str]] = None,
) -> List[MoonEvent]:
    """Forecast the events of ``kinds`` between *start* and *end*.

    Parameters
    ----------
    observer
        The site the altitude is judged from: "highest full moon" and the
        "observable" flag are both properties of a place, not of the Moon.
    start, end
        Anything :func:`moonlib_s.timeutil.as_time_range` accepts.
    kinds
        Any of :data:`EVENT_KINDS`; the default is all of them.

    Returns
    -------
    list of MoonEvent
        Chronological.
    """
    window = as_time_range((start, end))
    wanted = tuple(kinds) if kinds is not None else EVENT_KINDS
    unknown = [kind for kind in wanted if kind not in EVENT_KINDS]
    if unknown:
        raise MoonlibError(
            f"unknown event kind(s) {', '.join(map(repr, unknown))}; "
            f"known: {', '.join(EVENT_KINDS)}."
        )
    observer = Observer.from_value(observer)

    events: List[MoonEvent] = []
    if {"full_moon", "supermoon", "micromoon", "lunar_eclipse"} & set(wanted):
        events.extend(_full_moon_events(observer, window))
    if "highest_full" in wanted:
        found = _highest_event(observer, window, PhaseKind.FULL_MOON,
                               "highest_full", "满月")
        if found is not None:
            events.append(found)
    if "highest_quarter" in wanted:
        # Both quarters: "the highest quarter" is a question with two answers,
        # and a reader planning an evening wants to see whether it is the first
        # or the last one that is up.
        for phase_kind, label in ((PhaseKind.FIRST_QUARTER, "上弦"),
                                  (PhaseKind.LAST_QUARTER, "下弦")):
            found = _highest_event(observer, window, phase_kind,
                                   "highest_quarter", label)
            if found is not None:
                events.append(found)
    if "xvl" in wanted:
        events.append(MoonEvent(
            kind="xvl", when_utc=to_utc(window.start_et), altitude_deg=0.0,
            detail="月面 X / V / L：这一项还没有实现（只占位，不给出时间）",
            observable=False,
        ))
    keep = set(wanted)
    events = [event for event in events if event.kind in keep]
    events.sort(key=lambda event: event.when_utc)
    return events
