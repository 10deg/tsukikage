"""Event forecasts: what is worth putting in a diary.

Eleven things, all of them answers to "when should I look?":

``new_moon`` / ``first_quarter`` / ``full_moon`` / ``last_quarter``
    The four principal phase instants, from :mod:`moonlib.phases`.
``supermoon`` / ``micromoon``
    A full moon that happens in the near or far band.  There is no official
    threshold -- the word was invented by an astrologer in 1979 -- so the two
    used here are stated rather than implied: within 363 000 km and beyond
    405 000 km of the Earth's centre at the instant of full moon.  The near
    band is deliberately wide enough to include several moons a year (2026 has
    three), because "supermoon" is a range, not a ranking.
``highest_moon``
    One row for each principal phase -- 最高朔月, 最高上弦月, 最高满月,
    最高下弦月 -- at the transit altitude that makes it highest for *this*
    observer.  Altitude at transit is a property of the observer's latitude and
    the Moon's declination, so this one genuinely needs a site.
``lowest_moon``
    The same four phases, but each at the transit that is *lowest* in the
    range: the mirror question to the one above, and the same dependence on the
    site.
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

``xvl``
    The Moon's "X", "V" and "L": sunlight grazing the rims near Blanchinus and
    Ukert, a few hours after first quarter, and the bright L-shaped ridge west
    of Zach G.  The X/V anchor is the eastern arm of the X near Blanchinus B
    (lon 1.8°, lat -25°); the L anchor is the stated Zach G west point
    (lon -0.5°, lat -58.54°).  X/V uses the Sun's -0.5°–1.5° band with 0.5°
    as the best moment and keeps the colongitude gate at 358°.  L has its own
    band: -0.75°–0.20°, best -0.30°, fitted to the reported one-hour/three-hour/
    five-hour offsets.  L is paired with the X/V of the same sunrise train: one
    row per X/V is named 月面X/V/L when the two windows overlap, and 月面X/V
    with the note "L不同时出现" when they do not.
``jewelled_handle``
    The "Jewelled Handle" (also called the Golden Handle): the bright arc where
    the rising Sun first clears the Montes Jura, near Promontorium Laplace.
    The anchor is 36.60°W, 43.39°N and the solar altitude band is -2.5°–2.0°
    with -2.0° as the best moment.  First described by Patrick Moore.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .errors import MoonlibError
from .frames import az_el, direction_in_moon_frame
from .kernels import kernel_session
from .lunardate import chinese_lunar_date
from .moon import geocentric_phase_angle_deg, moon_state
from .moonview import moon_magnitude, view_geometry
from .numerics import bisect_root, wrap_angle
from .observer import Observer
from .phases import PhaseKind, find_phase_events
from .timeutil import as_time_range, to_et, to_utc
from . import spice

__all__ = [
    "EVENT_KINDS",
    "MoonEvent",
    "moon_events",
    "event_metrics",
]

#: Distance at which a full moon counts as a "supermoon", km.
#:
#: The word has no official threshold, so the number is chosen to make the
#: range *useful* rather than to crown one moon a year: a 363 000 km cut-off
#: includes both 2026-01-03 (362 313 km) and 2026-12-24 (356 738 km), and the
#: 2026-11-24 full moon between them -- which is what "several full moons can
#: all be supermoons" means.  The old 360 000 km line left January out and made
#: the label look like a once-a-year title.
SUPERMOON_KM = 363_000.0
#: ...and as a micromoon ("minimoon"), km.  The mirror of the same choice: the
#: far tenth of the orbit, not only the single farthest full moon.
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

#: The X/V anchor: the eastern arm of the X, near Blanchinus B.  The published
#: observing notes put it at lon 1.8°, lat -25°, which is a few arcminutes east
#: of the catalogue position of Blanchinus B itself; the X is the intersection
#: of two rim shadows, not the crater's centre.
XVL_ANCHOR_LON_DEG = 1.8
XVL_ANCHOR_LAT_DEG = -25.0

#: The X and V live near first quarter, when the colongitude is about 358°.
#: Two degrees is a coarse gate, not the prediction itself -- the prediction is
#: the solar altitude at the anchor.
XVL_COLONGITUDE_DEG = 358.0
XVL_COLONGITUDE_TOLERANCE_DEG = 2.0

#: The Sun's altitude at the X/V anchor.  The old upper edge was 1.5° and the
#: window began at the horizon; the observing notes put the letters' best
#: contrast near 0.5°, so the window is the whole -0.5°…1.5° band and 0.5° is
#: reported as the best moment.  Below the horizon the terminator is still
#: close enough to catch the grazing light, which is why the start is negative.
XVL_SUN_ALTITUDE_MIN_DEG = -0.5
XVL_SUN_ALTITUDE_MAX_DEG = 1.5
XVL_SUN_ALTITUDE_BEST_DEG = 0.5

#: The Sun's altitude at the L anchor.  L is not the same grazing geometry as
#: X/V: comparing the first forecast with the observed event put our start
#: 1 h late, the best moment 3 h late and the end 5 h late.  The altitude rate
#: at the high-southern anchor is roughly 0.25°/h, so the band moves down by
#: 0.25°, 0.75° and 1.25° respectively: -0.75°…0.20°, best -0.30°.  These are
#: still provisional, but they are fitted to the reported offsets rather than
#: borrowed from X/V.
LXV_SUN_ALTITUDE_MIN_DEG = -0.75
LXV_SUN_ALTITUDE_MAX_DEG = 0.20
LXV_SUN_ALTITUDE_BEST_DEG = -0.30

#: The L of the same family: a bright L-shaped ridge just west of Zach G.
#: The published descriptions place it on the crater's western rampart; the
#: catalogue has Zach G at lon 0.46°, lat -58.54°, and the ridge sits about a
#: degree west of that.  The forecast only needs a point on the ground, so this
#: is the stated anchor -- provisional, as the request says.
LXV_ANCHOR_LON_DEG = -0.5
LXV_ANCHOR_LAT_DEG = -58.54

#: The Jewelled Handle (the Golden Handle), near Promontorium Laplace on the
#: Montes Jura.  The anchor and the band are the ones the observer supplied for
#: this release; -2.0° is the best moment, inside the wider band.
JEWELLED_HANDLE_ANCHOR_LON_DEG = -36.60
JEWELLED_HANDLE_ANCHOR_LAT_DEG = 43.39
JEWELLED_HANDLE_SUN_ALTITUDE_MIN_DEG = -2.5
JEWELLED_HANDLE_SUN_ALTITUDE_MAX_DEG = 2.0
JEWELLED_HANDLE_SUN_ALTITUDE_BEST_DEG = -2.0

KIND_LABELS = {
    "full_moon": "满月",
    "new_moon": "朔月",
    "first_quarter": "上弦月",
    "last_quarter": "下弦月",
    "supermoon": "超级月亮",
    "micromoon": "最小月亮",
    "highest_moon": "最高月亮",
    "lowest_moon": "最低月亮",
    "lunar_eclipse": "月食",
    "xvl": "月面 X/V/L",
    "jewelled_handle": "the jewelled handle（实验功能）",
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
    "new_moon", "first_quarter", "full_moon", "last_quarter",
    "supermoon", "micromoon", "highest_moon", "lowest_moon",
    "lunar_eclipse", "xvl", "jewelled_handle",
)

#: Old names kept as aliases so a bookmarked settings file or a saved request
#: does not turn into a 400 when the two highest-moon kinds are merged.
EVENT_KIND_ALIASES = {
    "highest_full": "highest_moon",
    "highest_quarter": "highest_moon",
}


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


def _moon_surface_brightness(
    magnitude: float, diameter_arcmin: float, illuminated_fraction: float
) -> Optional[float]:
    """Mean surface brightness of the illuminated part, mag/arcsec².

    The whole-disk magnitude is a flux.  Spreading it over the *lit* area of
    the apparent disc turns it into the number an observer compares between
    phases -- the brightness of the surface itself.  A new moon has no lit area
    to divide by, so that case is ``None`` rather than an infinity.
    """
    fraction = float(illuminated_fraction)
    if not math.isfinite(fraction) or fraction <= 1e-6:
        return None
    radius_arcsec = 0.5 * float(diameter_arcmin) * 60.0
    area = math.pi * radius_arcsec * radius_arcsec * fraction
    if not math.isfinite(area) or area <= 0.0:
        return None
    return float(magnitude) + 2.5 * math.log10(area)


def event_metrics(observer: Any, when: Any) -> Dict[str, Any]:
    """The table numbers for one event instant.

    One place for phase angle, age, the Chinese calendar date, apparent size,
    distance, altitude/azimuth and the three brightness numbers, so every event
    kind reports the same quantity under the same key.  ``observer`` is the
    site the altitude and azimuth are judged from; the phase geometry is
    geocentric, which is the convention an almanac uses.
    """
    site = Observer.from_value(observer)
    state = moon_state(site, when)
    view = view_geometry(site, state.when_utc)
    phase = state.phase
    diameter_arcmin = float(state.angular_diameter_arcsec) / 60.0
    magnitude = moon_magnitude(
        phase.phase_angle_geo_deg, state.geo_distance_km
    )
    surface = _moon_surface_brightness(
        magnitude, diameter_arcmin, phase.illuminated_fraction_geo
    )
    earthshine = float(view.earthshine_apparent_magnitude)
    if not math.isfinite(earthshine):
        earthshine = None
    lunar = chinese_lunar_date(state.when_utc)
    return {
        "age_days": float(phase.age_days),
        "lunar": lunar.label_zh,
        "lunar_latin": lunar.label_latin,
        "phase_angle_deg": float(phase.phase_angle_geo_deg),
        "illumination_percent": float(phase.illuminated_fraction_geo * 100.0),
        "diameter_arcmin": diameter_arcmin,
        "altitude_deg": float(state.elevation_deg),
        "azimuth_deg": float(state.azimuth_deg),
        "distance_km": float(state.geo_distance_km),
        "magnitude": float(magnitude),
        "surface_brightness": surface,
        "earthshine_magnitude": earthshine,
        "elongation_deg": float(state.elongation_deg),
        "colongitude_deg": float(state.libration.colongitude_deg),
    }


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
    #: Optional interval: the X/V window and a crescent session are ranges, and
    #: a single instant would throw away the thing the reader is planning around.
    window_start_utc: Optional[datetime] = None
    window_end_utc: Optional[datetime] = None
    #: The numbers the page's table shows beside the event, in the units the
    #: column names state.  Kept as a dictionary rather than fifteen fields so
    #: the six-key JSON contract of 1.4.4 keeps working unchanged.
    metrics: Dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.title or KIND_LABELS.get(self.kind, self.kind)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "kind": self.kind,
            "label": self.label,
            "when_utc": self.when_utc.isoformat(),
            "altitude_deg": round(self.altitude_deg, 2),
            "detail": self.detail,
            "observable": self.observable,
        }
        if self.window_start_utc is not None:
            payload["window_start_utc"] = self.window_start_utc.isoformat()
        if self.window_end_utc is not None:
            payload["window_end_utc"] = self.window_end_utc.isoformat()
        if self.metrics:
            payload["metrics"] = {
                key: (None if isinstance(value, float) and not math.isfinite(value)
                      else value)
                for key, value in self.metrics.items()
            }
        return payload


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


def _anchor_normal(lon_deg: float, lat_deg: float) -> np.ndarray:
    return np.array([
        math.cos(math.radians(lat_deg)) * math.cos(math.radians(lon_deg)),
        math.cos(math.radians(lat_deg)) * math.sin(math.radians(lon_deg)),
        math.sin(math.radians(lat_deg)),
    ])


#: Unit normals of the two anchors, in MOON_ME.
_XVL_ANCHOR_NORMAL = _anchor_normal(XVL_ANCHOR_LON_DEG, XVL_ANCHOR_LAT_DEG)
_LXV_ANCHOR_NORMAL = _anchor_normal(LXV_ANCHOR_LON_DEG, LXV_ANCHOR_LAT_DEG)
_JEWELLED_HANDLE_ANCHOR_NORMAL = _anchor_normal(
    JEWELLED_HANDLE_ANCHOR_LON_DEG, JEWELLED_HANDLE_ANCHOR_LAT_DEG
)


def _sun_altitude_at(normal: np.ndarray, et: float) -> float:
    sun = np.asarray(direction_in_moon_frame("SUN", float(et)), dtype=float)
    return math.degrees(math.asin(float(np.clip(
        np.dot(normal, sun), -1.0, 1.0
    ))))


def _xvl_sun_altitude_deg(et: float) -> float:
    """The Sun's altitude above the Blanchinus-B anchor, degrees."""
    return _sun_altitude_at(_XVL_ANCHOR_NORMAL, et)


def _lxv_sun_altitude_deg(et: float) -> float:
    """The Sun's altitude above the Zach G west anchor, degrees."""
    return _sun_altitude_at(_LXV_ANCHOR_NORMAL, et)


def _jewelled_handle_sun_altitude_deg(et: float) -> float:
    """The Sun's altitude above the Jewelled Handle anchor, degrees."""
    return _sun_altitude_at(_JEWELLED_HANDLE_ANCHOR_NORMAL, et)


def _xvl_colongitude_deg(et: float) -> float:
    """Selenographic colongitude of the Sun, in [0, 360)."""
    sun = np.asarray(direction_in_moon_frame("SUN", float(et)), dtype=float)
    subsolar_lon = math.degrees(math.atan2(float(sun[1]), float(sun[0])))
    return (90.0 - subsolar_lon) % 360.0


def _altitude_windows(altitude_fn, start: float, end: float,
                      minimum: float, maximum: float,
                      best_target: float,
                      limit_hours: float = 12.0) -> List[Tuple[float, float, float]]:
    """``(start, end, best)`` for every solar-altitude window in a band.

    *limit_hours* bounds the search for the upper crossing after the lower one.
    Twelve hours is enough for the half-degree X/V and L bands; a wider band at
    a slow surface-sun rate -- the Jewelled Handle's -2.5°…2° -- needs more, or
    its window would be cut off before the Sun reaches the upper edge.
    """
    if end <= start + 60.0:
        return []
    windows: List[Tuple[float, float, float]] = []
    step = 3600.0
    moment = start
    previous = altitude_fn(moment)
    while moment < end:
        following = min(moment + step, end)
        altitude = altitude_fn(following)
        if previous <= minimum < altitude:
            root = bisect_root(
                lambda et: altitude_fn(et) - minimum,
                moment, following, tol=1e-3,
            )
            limit = min(end, root + limit_hours * 3600.0)
            upper = altitude_fn(limit)
            if upper <= maximum:
                # The requested range ends inside the window; report the part
                # of it the reader asked for rather than inventing an end.
                finish = limit
            else:
                finish = bisect_root(
                    lambda et: altitude_fn(et) - maximum,
                    root, limit, tol=1e-3,
                )
            if finish > root + 60.0:
                # The best moment is the crossing of its own target, which lies
                # inside [minimum, maximum] by construction.
                best = bisect_root(
                    lambda et: altitude_fn(et) - best_target,
                    root, finish, tol=1e-3,
                )
                windows.append((root, finish, best))
        previous = altitude
        moment = following
    return windows


def _xvl_events(observer: Observer, window) -> List[MoonEvent]:
    """The X/V windows inside *window*, with L folded into their notes.

    One coarse scan for each anchor's sunrise, a bisection on the solar
    altitude, and a colongitude gate for X/V.  L uses its own altitude band
    (see :data:`LXV_SUN_ALTITUDE_MIN_DEG`) and is paired with the X/V of the
    same sunrise train.  There is **one row per X/V**: when the L window
    overlaps it the row is named 月面X/V/L and the detail carries L's best
    moment; when it does not, the detail says ``L不同时出现``.  Keeping L as a
    second row made the reader compare two rows to learn that they belong to
    one sunrise.
    """
    start = float(window.start_et)
    end = float(window.end_et)
    events: List[MoonEvent] = []

    xv_windows: List[Tuple[float, float, float]] = []
    for root, finish, best in _altitude_windows(
            _xvl_sun_altitude_deg, start, end,
            XVL_SUN_ALTITUDE_MIN_DEG, XVL_SUN_ALTITUDE_MAX_DEG,
            XVL_SUN_ALTITUDE_BEST_DEG):
        colongitude = _xvl_colongitude_deg(root)
        offset = wrap_angle(colongitude - XVL_COLONGITUDE_DEG, period=360.0)
        if abs(offset) <= XVL_COLONGITUDE_TOLERANCE_DEG:
            xv_windows.append((root, finish, best))

    l_windows = _altitude_windows(
        _lxv_sun_altitude_deg, start, end,
        LXV_SUN_ALTITUDE_MIN_DEG, LXV_SUN_ALTITUDE_MAX_DEG,
        LXV_SUN_ALTITUDE_BEST_DEG)
    paired_l: Dict[float, Tuple[float, float, float]] = {}
    used: set = set()
    for root, finish, best in xv_windows:
        for position, (l_root, l_finish, l_best) in enumerate(l_windows):
            if position in used:
                continue
            # L belongs to the same sunrise train, not a whole lunation away.
            # The corrected L band can put its onset or best moment slightly
            # before X/V's -- the two anchors are 33° apart in latitude -- so
            # the pairing is by proximity, not by a forced order.
            if not (root - 6.0 * 3600.0 <= l_root <= finish + 30.0 * 3600.0):
                continue
            used.add(position)
            paired_l[root] = (l_root, l_finish, l_best)
            break

    for xv_root, xv_finish, xv_best in xv_windows:
        l_pair = paired_l.get(xv_root)
        overlap = 0.0
        if l_pair is not None:
            l_root, l_finish, l_best = l_pair
            overlap = max(0.0, min(xv_finish, l_finish) - max(xv_root, l_root))
        metrics = event_metrics(observer, xv_best)
        metrics["colongitude_deg"] = _xvl_colongitude_deg(xv_best)
        if overlap > 0.0:
            title = "月面X/V/L"
            l_time = to_utc(l_best).strftime("%H:%M")
            note = f"L 出现时刻 {l_time} UTC"
            metrics["l_best_utc"] = to_utc(l_best).isoformat()
            metrics["l_start_utc"] = to_utc(l_root).isoformat()
            metrics["l_end_utc"] = to_utc(l_finish).isoformat()
            metrics["l_overlap"] = True
        else:
            title = "月面X/V"
            note = "L不同时出现"
            metrics["l_best_utc"] = None
            metrics["l_start_utc"] = None
            metrics["l_end_utc"] = None
            metrics["l_overlap"] = False
        moon_altitude = float(metrics["altitude_deg"])
        minutes = (xv_finish - xv_root) / 60.0
        events.append(MoonEvent(
            kind="xvl",
            when_utc=to_utc(xv_best),
            altitude_deg=moon_altitude,
            title=title,
            detail=(
                f"太阳高度 {XVL_SUN_ALTITUDE_MIN_DEG:g}–"
                f"{XVL_SUN_ALTITUDE_MAX_DEG:g}°，最佳 "
                f"{XVL_SUN_ALTITUDE_BEST_DEG:g}°　"
                f"持续 {minutes:.0f} min　"
                f"锚点 {XVL_ANCHOR_LON_DEG:g}° / {XVL_ANCHOR_LAT_DEG:g}°　"
                f"余经度 {metrics['colongitude_deg']:.1f}°　"
                f"{_altitude_text(moon_altitude)}　{note}"
            ),
            observable=moon_altitude > 0.0,
            window_start_utc=to_utc(xv_root),
            window_end_utc=to_utc(xv_finish),
            metrics=metrics,
        ))
    events.sort(key=lambda event: event.when_utc)
    return events


def _jewelled_handle_events(observer: Observer, window) -> List[MoonEvent]:
    """The Jewelled Handle (Golden Handle) windows inside *window*.

    A single solar-altitude scan at the provisional anchor.  ``_altitude_windows``
    finds only the rising crossing of the lower edge, so the sunset crossing of
    the same band later in the lunation is not reported: the handle is a sunrise
    phenomenon.
    """
    start = float(window.start_et)
    end = float(window.end_et)
    events: List[MoonEvent] = []
    for root, finish, best in _altitude_windows(
        _jewelled_handle_sun_altitude_deg, start, end,
        JEWELLED_HANDLE_SUN_ALTITUDE_MIN_DEG,
        JEWELLED_HANDLE_SUN_ALTITUDE_MAX_DEG,
        JEWELLED_HANDLE_SUN_ALTITUDE_BEST_DEG,
        limit_hours=24.0,
    ):
        metrics = event_metrics(observer, best)
        metrics["colongitude_deg"] = _xvl_colongitude_deg(best)
        moon_altitude = float(metrics["altitude_deg"])
        minutes = (finish - root) / 60.0
        events.append(MoonEvent(
            kind="jewelled_handle",
            when_utc=to_utc(best),
            altitude_deg=moon_altitude,
            title="the jewelled handle（实验功能）",
            detail=(
                f"太阳高度 {JEWELLED_HANDLE_SUN_ALTITUDE_MIN_DEG:g}–"
                f"{JEWELLED_HANDLE_SUN_ALTITUDE_MAX_DEG:g}°，最佳 "
                f"{JEWELLED_HANDLE_SUN_ALTITUDE_BEST_DEG:g}°　"
                f"持续 {minutes:.0f} min　"
                f"锚点 {abs(JEWELLED_HANDLE_ANCHOR_LON_DEG):g}°W / "
                f"{JEWELLED_HANDLE_ANCHOR_LAT_DEG:g}°N　"
                f"{_altitude_text(moon_altitude)}　"
                f"最早由 Patrick Moore 描述，又名“the golden handle”"
            ),
            observable=moon_altitude > 0.0,
            window_start_utc=to_utc(root),
            window_end_utc=to_utc(finish),
            metrics=metrics,
        ))
    events.sort(key=lambda event: event.when_utc)
    return events


def _altitude(et: float, observer: Observer) -> float:
    return float(az_el("MOON", et, observer).elevation_deg)


def _sun_altitude(et: float, observer: Observer) -> float:
    return float(az_el("SUN", et, observer).elevation_deg)


def _daylight_fraction(observer: Observer, start_et: float, end_et: float) -> float:
    """Fraction of an event's interval with the Sun above the horizon.

    A point event is one instant, so it is either daylight or not.  An interval
    (the X/V window, the Jewelled Handle) is sampled evenly, endpoints included;
    the question the forecast answers is "will the sky be bright for most of
    this?", not "is the Sun up at the best moment?" -- a window that starts in
    darkness and ends in daylight is still mostly a daylight event.
    """
    if end_et <= start_et:
        return 1.0 if _sun_altitude(start_et, observer) > 0.0 else 0.0
    moments = np.linspace(start_et, end_et, 21)
    above = sum(
        1 for moment in moments if _sun_altitude(float(moment), observer) > 0.0
    )
    return above / float(moments.size)


def _with_daylight(observer: Observer, event: MoonEvent) -> MoonEvent:
    """Append 白昼 when the Sun is up for most (or all) of the event.

    This is deliberately a note in ``detail`` rather than a new visibility
    flag: the event itself can still be seen in daylight (a lunar eclipse near
    the horizon, a full moon at noon), but the reader deserves to know that the
    sky, not the Moon, is the limiting factor.  The threshold is the one asked
    for: the whole event or more than half of it.
    """
    start = to_et(event.window_start_utc or event.when_utc)
    end = to_et(event.window_end_utc or event.when_utc)
    fraction = _daylight_fraction(observer, start, end)
    if fraction <= 0.5:
        return event
    metrics = dict(event.metrics)
    metrics["sun_altitude_deg"] = _sun_altitude(to_et(event.when_utc), observer)
    metrics["daylight_fraction"] = fraction
    return replace(event, detail=f"{event.detail}　白昼", metrics=metrics)


def _transit_altitude(et: float, observer: Observer) -> float:
    """The highest the Moon gets around *et*: a coarse scan of the day."""
    grid = np.linspace(et - 12 * 3600.0, et + 12 * 3600.0, 49)
    values = [_altitude(float(item), observer) for item in grid]
    return max(values)


def _principal_phase_events(observer: Observer, window) -> List[MoonEvent]:
    """All four principal phases, plus super/mini moons and eclipses.

    Every one of them carries the Moon's apparent diameter and the altitude it
    stands at: the diameter is what distinguishes a supermoon from a micromoon
    to the eye, and the altitude is what decides whether the reader can see it
    at all -- which the row's own greyed-out state only hints at.

    Full moons are *also* reported as 超级月亮 / 最小月亮 whenever they fall in
    the near or far band: several per year qualify, and the title is a range,
    not a ranking.
    """
    events: List[MoonEvent] = []
    for phase in find_phase_events(window):
        distance, radius_deg, _separation = _geometry(phase.et)
        altitude = _altitude(phase.et, observer)
        metrics = event_metrics(observer, phase.et)
        size = f"视直径 {_diameter_text(radius_deg)}"
        where = f"地心距 {distance:,.0f} km"
        height = _altitude_text(altitude)
        if phase.kind is PhaseKind.FULL_MOON:
            detail = f"{size}　{where}　{height}"
        elif phase.kind is PhaseKind.NEW_MOON:
            detail = f"{where}　{height}（新月不可见，这一行是日期的锚）"
        else:
            detail = f"{size}　{where}　{height}"
        events.append(MoonEvent(
            kind=phase.kind.value, when_utc=phase.when_utc,
            altitude_deg=altitude, detail=detail,
            observable=altitude > 0.0, metrics=dict(metrics),
        ))
        if phase.kind is not PhaseKind.FULL_MOON:
            continue
        if distance <= SUPERMOON_KM:
            events.append(MoonEvent(
                kind="supermoon", when_utc=phase.when_utc, altitude_deg=altitude,
                detail=f"{size}　{where}（≤ {SUPERMOON_KM:,.0f}）　{height}",
                observable=altitude > 0.0, metrics=dict(metrics),
            ))
        if distance >= MICROMOON_KM:
            events.append(MoonEvent(
                kind="micromoon", when_utc=phase.when_utc, altitude_deg=altitude,
                detail=f"{size}　{where}（≥ {MICROMOON_KM:,.0f}）　{height}",
                observable=altitude > 0.0, metrics=dict(metrics),
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
                metrics=event_metrics(observer, at),
            ))
    return events


def _extreme_phase_event(
    observer: Observer, window, kind: PhaseKind, title: str, *, lowest: bool
) -> Optional[MoonEvent]:
    """The highest (or lowest) transit of one phase in the range."""
    best: Optional[Tuple[float, float]] = None
    for phase in find_phase_events(window, (kind,)):
        height = _transit_altitude(phase.et, observer)
        if best is None or (height < best[0] if lowest else height > best[0]):
            best = (height, phase.et)
    if best is None:
        return None
    height, et = best
    # The event is the *transit*, not the phase instant: that is when the Moon
    # is highest (or lowest) in the sky, and for a reader that is the whole
    # point.  ``_transit_altitude`` found the extremum over the day; refine the
    # instant on the same grid, then report the state at that instant.
    grid = np.linspace(et - 12 * 3600.0, et + 12 * 3600.0, 49)
    # Both extremes are still judged at the *upper* transit: 最低月亮 asks which
    # phase culminates lowest, not where the Moon is at its lowest point of the
    # day (which for a circumpolar Moon is the opposite side of the sky).
    transit = max(grid, key=lambda item: _altitude(float(item), observer))
    return MoonEvent(
        kind="lowest_moon" if lowest else "highest_moon",
        when_utc=to_utc(float(transit)),
        altitude_deg=height, title=title,
        detail=f"{title}　中天高度 {height:.1f}°", observable=height > 0.0,
        metrics=event_metrics(observer, float(transit)),
    )


def _highest_moon_events(observer: Observer, window) -> List[MoonEvent]:
    """One highest-transit row for each principal phase.

    最高满月 and 最高弦月 used to be two switches with three answers between
    them; they are one category now, and 最高朔月 joins them, so the list is
    four rows -- 朔月, 上弦月, 满月, 下弦月 -- each at the transit altitude that
    makes it "highest" for this site.
    """
    events: List[MoonEvent] = []
    for kind, title in ((PhaseKind.NEW_MOON, "最高朔月"),
                        (PhaseKind.FIRST_QUARTER, "最高上弦月"),
                        (PhaseKind.FULL_MOON, "最高满月"),
                        (PhaseKind.LAST_QUARTER, "最高下弦月")):
        found = _extreme_phase_event(
            observer, window, kind, title, lowest=False
        )
        if found is not None:
            events.append(found)
    return events


def _lowest_moon_events(observer: Observer, window) -> List[MoonEvent]:
    """The mirror of :func:`_highest_moon_events`: the lowest transits.

    Transit altitude is set by the observer's latitude and the Moon's
    declination, so "最低满月" is as much a property of the site as "最高满月":
    the same phase can be low from one latitude and high from another.
    """
    events: List[MoonEvent] = []
    for kind, title in ((PhaseKind.NEW_MOON, "最低朔月"),
                        (PhaseKind.FIRST_QUARTER, "最低上弦月"),
                        (PhaseKind.FULL_MOON, "最低满月"),
                        (PhaseKind.LAST_QUARTER, "最低下弦月")):
        found = _extreme_phase_event(
            observer, window, kind, title, lowest=True
        )
        if found is not None:
            events.append(found)
    return events


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
        Anything :func:`moonlib.timeutil.as_time_range` accepts.
    kinds
        Any of :data:`EVENT_KINDS`; the default is all of them.

    Returns
    -------
    list of MoonEvent
        Chronological.
    """
    window = as_time_range((start, end))
    requested = tuple(kinds) if kinds is not None else EVENT_KINDS
    wanted = tuple(EVENT_KIND_ALIASES.get(kind, kind) for kind in requested)
    unknown = [kind for kind in wanted if kind not in EVENT_KINDS]
    if unknown:
        raise MoonlibError(
            f"unknown event kind(s) {', '.join(map(repr, unknown))}; "
            f"known: {', '.join(EVENT_KINDS)}."
        )
    observer = Observer.from_value(observer)
    wanted_set = set(wanted)

    events: List[MoonEvent] = []
    phase_kinds = {"new_moon", "first_quarter", "full_moon", "last_quarter",
                   "supermoon", "micromoon", "lunar_eclipse"}
    if phase_kinds & wanted_set:
        events.extend(_principal_phase_events(observer, window))
    if "highest_moon" in wanted_set:
        events.extend(_highest_moon_events(observer, window))
    if "lowest_moon" in wanted_set:
        events.extend(_lowest_moon_events(observer, window))
    if "xvl" in wanted_set:
        events.extend(_xvl_events(observer, window))
    if "jewelled_handle" in wanted_set:
        events.extend(_jewelled_handle_events(observer, window))
    keep = wanted_set
    events = [event for event in events if event.kind in keep]
    events.sort(key=lambda event: event.when_utc)
    return [_with_daylight(observer, event) for event in events]
