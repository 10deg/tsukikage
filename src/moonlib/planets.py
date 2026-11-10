"""The planets in the observer's sky, and their passages past the Moon.

Everything here reads the ephemeris the rest of :mod:`moonlib` already loads:
``de440s.bsp`` carries the inner planets and the outer *barycentres*, which is
all a geocentric observer needs -- the offset between a gas giant's centre and
its barycentre is below an arcsecond at the Earth.  The radii come from
``pck00011.tpc`` through :func:`moonlib.kernels.body_radii_km`.

Two products live here:

* :func:`planet_sky_positions` -- one azimuth/elevation per planet, for the sky
  position diagram's planet layer;
* :func:`find_planet_conjunctions` -- every time the Moon passes a planet
  within the search range, refined to the instant of least separation and
  classified as a conjunction or an occultation.  The occultations carry the
  first/last contact window, which is what the page's 显示掩星 layer draws.

The Moon-planet separation is always *topocentric*: an occultation happens for
a place, not for the Earth's centre, and the Moon's parallax is large enough
(up to a degree) that the geocentric answer would be wrong about which side of
the limb the planet passed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

from .errors import MoonlibError
from .frames import (
    apparent_position,
    az_el,
    body_fixed_from_j2000,
    observer_j2000,
    topocentric_vector,
)
from .kernels import body_radii_km
from .observer import Observer
from .timeutil import to_et, to_utc

__all__ = [
    "PLANETS",
    "PLANET_BY_KEY",
    "Planet",
    "PlanetConjunction",
    "find_planet_conjunctions",
    "planet",
    "planet_angular_diameter_arcsec",
    "planet_moon_separation_deg",
    "planet_radius_km",
    "planet_sky_position",
    "planet_sky_positions",
    "planet_track",
]

#: The reference radius the rest of the package uses for the Moon.
MOON_REFERENCE_RADIUS_KM = 1737.4

#: One entry per planet the page offers.  ``position_body`` is the SPICE target
#: the ephemeris actually contains; ``radius_body`` is the name the planetary
#: constants kernel keys its radii by.  They differ for the outer planets
#: because ``de440s.bsp`` ships their barycentres, not their centres.
@dataclass(frozen=True)
class Planet:
    key: str
    name_zh: str
    name_en: str
    position_body: str
    radius_body: str
    colour: str


PLANETS: Tuple[Planet, ...] = (
    Planet("mercury", "水星", "Mercury", "MERCURY", "MERCURY", "#c7b8a5"),
    Planet("venus", "金星", "Venus", "VENUS", "VENUS", "#f3e2b6"),
    Planet("mars", "火星", "Mars", "MARS BARYCENTER", "MARS", "#e08a63"),
    Planet("jupiter", "木星", "Jupiter", "JUPITER BARYCENTER", "JUPITER", "#e5c99a"),
    Planet("saturn", "土星", "Saturn", "SATURN BARYCENTER", "SATURN", "#e8d9a6"),
    Planet("uranus", "天王星", "Uranus", "URANUS BARYCENTER", "URANUS", "#a9dbe0"),
    Planet("neptune", "海王星", "Neptune", "NEPTUNE BARYCENTER", "NEPTUNE", "#9db4ed"),
)

PLANET_BY_KEY: Dict[str, Planet] = {item.key: item for item in PLANETS}

#: A 6 h coarse step finds every lunar conjunction: the Moon laps a planet in
#: about a month, and its separation curve near the passage is a clean single
#: well.  Short ranges get a finer step because they may contain one crossing
#: and nothing else.
_COARSE_STEP_S = 6.0 * 3600.0
_MIN_COARSE_STEP_S = 60.0
_MAX_SCAN_SAMPLES = 20000


def planet(key: str) -> Planet:
    """Look one planet up by its page key, case-insensitively."""
    text = str(key or "").strip().lower()
    found = PLANET_BY_KEY.get(text)
    if found is None:
        raise MoonlibError(
            f"未知的行星 {key!r}；可选：{', '.join(PLANET_BY_KEY)}。"
        )
    return found


def planet_radius_km(key: str) -> float:
    """Mean radius of the planet, from the planetary constants kernel."""
    item = planet(key)
    values = [value for value in body_radii_km(item.radius_body)
              if value > 0.0]
    if not values:
        raise MoonlibError(f"行星半径不可用：{item.name_en}")
    return sum(values) / float(len(values))


def planet_sky_position(key: str, et: float, observer: Observer) -> Dict[str, object]:
    """One planet's horizontal position and apparent size at *et*."""
    item = planet(key)
    horizontal = az_el(item.position_body, float(et), observer)
    distance = float(horizontal.range_km)
    radius = planet_radius_km(item.key)
    diameter = 2.0 * math.degrees(
        math.asin(min(1.0, radius / max(distance, radius + 1e-9)))
    ) * 3600.0
    return {
        "key": item.key,
        "name": item.name_en,
        "name_zh": item.name_zh,
        "azimuth_deg": float(horizontal.azimuth_deg),
        "elevation_deg": float(horizontal.elevation_deg),
        "range_km": distance,
        "angular_diameter_arcsec": diameter,
        "colour": item.colour,
    }


def planet_sky_positions(et: float, observer: Observer) -> List[Dict[str, object]]:
    """Every planet the page draws, in the catalogue's order."""
    return [planet_sky_position(item.key, et, observer) for item in PLANETS]


def planet_angular_diameter_arcsec(
    key: str, et: float, observer: Observer
) -> float:
    """Apparent diameter in arcseconds, from the topocentric distance."""
    item = planet(key)
    vector, _ = topocentric_vector(item.position_body, float(et), observer)
    distance = float(np.linalg.norm(vector))
    radius = planet_radius_km(item.key)
    return 2.0 * math.degrees(
        math.asin(min(1.0, radius / max(distance, radius + 1e-9)))
    ) * 3600.0


# ---------------------------------------------------------------------------
# Moon-planet geometry
# ---------------------------------------------------------------------------
def _topocentric(body: str, et: float, observer_pos: np.ndarray) -> np.ndarray:
    """J2000 topocentric vector to *body*, given the observer's J2000 position."""
    return apparent_position(body, float(et)) - observer_pos


def _separation_deg(
    planet_body: str,
    et: float,
    observer: Observer,
    observer_pos: np.ndarray,
) -> Tuple[float, float, float]:
    """Moon-planet centre separation, moon distance, planet distance (deg, km)."""
    moon = _topocentric("MOON", et, observer_pos)
    other = _topocentric(planet_body, et, observer_pos)
    cos = float(np.dot(moon, other)) / max(
        float(np.linalg.norm(moon) * np.linalg.norm(other)), 1e-30
    )
    angle = math.degrees(math.acos(max(-1.0, min(1.0, cos))))
    return angle, float(np.linalg.norm(moon)), float(np.linalg.norm(other))


def planet_moon_separation_deg(key: str, et: float, observer: Observer) -> float:
    """Apparent centre-to-centre Moon-planet separation, in degrees."""
    item = planet(key)
    return _separation_deg(
        item.position_body, et, observer, _observer_position(et, observer)
    )[0]


def _observer_position(et: float, observer: Observer) -> np.ndarray:
    return observer_j2000(float(et), observer)


@dataclass(frozen=True)
class PlanetConjunction:
    """One closest approach of the Moon and a planet."""

    planet: Planet
    when_utc: datetime
    separation_deg: float
    moon_radius_deg: float
    planet_radius_deg: float
    angular_diameter_arcsec: float
    moon_distance_km: float
    planet_distance_km: float
    #: First and last contact, only for an occultation.
    window_start_utc: Optional[datetime] = None
    window_end_utc: Optional[datetime] = None

    @property
    def occultation(self) -> bool:
        return self.window_start_utc is not None


def _moon_radius_deg(distance_km: float) -> float:
    return math.degrees(
        math.asin(min(1.0, MOON_REFERENCE_RADIUS_KM / max(distance_km, 1.0)))
    )


def _find_contacts(
    item: Planet,
    observer: Observer,
    minimum_et: float,
    planet_radius_deg: float,
    half_width: float,
) -> Tuple[float, float]:
    """First and last contact around *minimum_et*, by bisection on the excess."""
    def excess(et: float) -> float:
        separation, moon_distance, _ = _separation_deg(
            item.position_body, et, observer, _observer_position(et, observer)
        )
        return separation - (
            _moon_radius_deg(moon_distance) + planet_radius_deg
        )

    def root(direction: int) -> float:
        inside = minimum_et
        outside = minimum_et + direction * half_width
        for _ in range(12):
            if excess(outside) > 0.0:
                break
            outside += direction * half_width
        else:  # pragma: no cover - the minimum itself is inside the Moon
            return minimum_et
        for _ in range(60):
            middle = 0.5 * (inside + outside)
            if excess(middle) > 0.0:
                outside = middle
            else:
                inside = middle
        return 0.5 * (inside + outside)

    return root(-1), root(+1)


def find_planet_conjunctions(
    observer: Observer,
    start: Any,
    end: Any,
    *,
    planets: Optional[Iterable[str]] = None,
) -> List[PlanetConjunction]:
    """Every Moon-planet closest approach in ``[start, end]``.

    The range is scanned coarsely and each well is refined by golden-section
    minimisation; a passage whose least separation is inside the Moon's disc
    plus the planet's own radius is an occultation, and its contact window is
    solved by bisection.
    """
    observer = Observer.from_value(observer)
    start_et = to_et(start)
    end_et = to_et(end)
    span = end_et - start_et
    if span <= 0.0:
        return []
    selected = (
        [planet(key) for key in planets]
        if planets is not None
        else list(PLANETS)
    )
    step = min(_COARSE_STEP_S, max(_MIN_COARSE_STEP_S, span / 48.0))
    step = max(step, span / _MAX_SCAN_SAMPLES)
    count = int(math.ceil(span / step)) + 1
    times = [start_et + index * step for index in range(count)]
    if times[-1] < end_et:
        times.append(end_et)
    if len(times) < 3:
        return []

    observer_positions = [
        _observer_position(et, observer) for et in times
    ]
    moon_vectors = [
        _topocentric("MOON", et, position)
        for et, position in zip(times, observer_positions)
    ]
    found: List[PlanetConjunction] = []
    for item in selected:
        separations: List[float] = []
        for et, position, moon in zip(times, observer_positions, moon_vectors):
            other = _topocentric(item.position_body, et, position)
            cos = float(np.dot(moon, other)) / max(
                float(np.linalg.norm(moon) * np.linalg.norm(other)), 1e-30
            )
            separations.append(
                math.degrees(math.acos(max(-1.0, min(1.0, cos))))
            )
        for index in range(1, len(times) - 1):
            if not (separations[index] <= separations[index - 1]
                    and separations[index] <= separations[index + 1]):
                continue
            low, high = times[index - 1], times[index + 1]

            def objective(et: float, body: str = item.position_body) -> float:
                return _separation_deg(
                    body, et, observer, _observer_position(et, observer)
                )[0]

            # Golden section on the separation over the bracketing interval.
            ratio = (math.sqrt(5.0) - 1.0) / 2.0
            a, b = low, high
            c = b - ratio * (b - a)
            d = a + ratio * (b - a)
            f_c, f_d = objective(c), objective(d)
            for _ in range(70):
                if abs(b - a) <= 0.25:
                    break
                if f_c < f_d:
                    b, d, f_d = d, c, f_c
                    c = b - ratio * (b - a)
                    f_c = objective(c)
                else:
                    a, c, f_c = c, d, f_d
                    d = a + ratio * (b - a)
                    f_d = objective(d)
            minimum_et = 0.5 * (a + b)
            separation, moon_distance, planet_distance = _separation_deg(
                item.position_body, minimum_et, observer,
                _observer_position(minimum_et, observer),
            )
            planet_radius = math.degrees(
                math.asin(min(
                    1.0,
                    planet_radius_km(item.key)
                    / max(planet_distance, planet_radius_km(item.key) + 1e-9),
                ))
            )
            moon_radius = _moon_radius_deg(moon_distance)
            if separation >= moon_radius + planet_radius:
                found.append(PlanetConjunction(
                    planet=item,
                    when_utc=to_utc(minimum_et),
                    separation_deg=separation,
                    moon_radius_deg=moon_radius,
                    planet_radius_deg=planet_radius,
                    angular_diameter_arcsec=planet_radius * 7200.0,
                    moon_distance_km=moon_distance,
                    planet_distance_km=planet_distance,
                ))
                continue
            half_width = max(900.0, (high - low) * 0.5)
            first, last = _find_contacts(
                item, observer, minimum_et, planet_radius, half_width
            )
            found.append(PlanetConjunction(
                planet=item,
                when_utc=to_utc(minimum_et),
                separation_deg=separation,
                moon_radius_deg=moon_radius,
                planet_radius_deg=planet_radius,
                angular_diameter_arcsec=planet_radius * 7200.0,
                moon_distance_km=moon_distance,
                planet_distance_km=planet_distance,
                window_start_utc=to_utc(first),
                window_end_utc=to_utc(last),
            ))
    found.sort(key=lambda hit: hit.when_utc)
    return found


def _unit(vector: np.ndarray) -> np.ndarray:
    """The direction of *vector*, or a safe fallback for a zero vector."""
    norm = float(np.linalg.norm(vector))
    if not (norm > 0.0):
        return np.array([1.0, 0.0, 0.0], dtype=float)
    return np.asarray(vector, dtype=float) / norm


def _track_direction(
    item: Planet,
    et: float,
    observer: Observer,
    display_moon: np.ndarray,
    rotation: np.ndarray,
) -> np.ndarray:
    """Where the planet sits on the display Moon, as an observer direction.

    ``display_moon`` is the unit observer-to-Moon direction at the display
    instant and ``rotation`` the J2000 -> MOON_ME transform at that same
    instant.  The planet's offset from the Moon's centre *at its own time* is
    added to the display centre, which is the path a reader is following: the
    Moon is held still and the planet's passage is laid over it.

    The result is a direction, not a point on the sphere, so a sample outside
    the limb simply projects outside the disc.  Folding missed rays onto the
    limb -- what the previous sphere-intersection version did -- put a bend at
    each contact; a direction has no limb to fold at.
    """
    moon_vector, _ = topocentric_vector("MOON", float(et), observer)
    planet_vector, _ = topocentric_vector(item.position_body, float(et), observer)
    moon = rotation @ _unit(np.asarray(moon_vector, dtype=float))
    planet = rotation @ _unit(np.asarray(planet_vector, dtype=float))
    return _unit(display_moon + (planet - moon))


def planet_track(
    key: str,
    observer: Observer,
    start: Any,
    end: Any,
    when: Any,
    *,
    samples: int = 181,
) -> Dict[str, object]:
    """The planet's apparent path across the Moon's disc, as sky directions.

    Each sample is where the planet appears *relative to the Moon* at that
    instant, expressed as a direction in the Moon body-fixed frame at the
    display instant.  The page projects these directions through whichever
    camera is drawing (the full disc or the close-up), so the track can run
    past the limb without being folded back onto it.

    The sampling range is the caller's: 显示掩星 asks for the contact window
    extended by ten minutes on each side, so the time scale along the track has
    a little room before immersion and after emersion.
    """
    item = planet(key)
    observer = Observer.from_value(observer)
    display = to_et(when)
    rotation = body_fixed_from_j2000(display, "MOON_ME")
    display_moon_vector, _ = topocentric_vector("MOON", display, observer)
    display_moon = rotation @ _unit(
        np.asarray(display_moon_vector, dtype=float)
    )

    count = max(2, int(samples))
    start_et = to_et(start)
    end_et = to_et(end)
    if end_et <= start_et:
        end_et = start_et + 1.0
    times = np.linspace(start_et, end_et, count)
    rows: List[Dict[str, object]] = []
    for et in times:
        direction = _track_direction(
            item, float(et), observer, display_moon, rotation
        )
        rows.append({
            "when_utc": to_utc(float(et)).isoformat(),
            "direction": [float(direction[0]), float(direction[1]),
                          float(direction[2])],
        })
    center = _track_direction(item, display, observer, display_moon, rotation)
    planet_vector, _ = topocentric_vector(item.position_body, display, observer)
    distance = float(np.linalg.norm(planet_vector))
    radius = planet_radius_km(item.key)
    diameter = 2.0 * math.degrees(
        math.asin(min(1.0, radius / max(distance, radius + 1e-9)))
    ) * 3600.0
    # The Moon's angular radius at the display instant, so the page can size the
    # planet circle against the limb as well as draw it.
    moon_vector, _ = topocentric_vector("MOON", display, observer)
    moon_distance = float(np.linalg.norm(moon_vector))
    return {
        "planet": item.key,
        "name": item.name_en,
        "name_zh": item.name_zh,
        "colour": item.colour,
        "when_utc": to_utc(display).isoformat(),
        "track_start_utc": to_utc(start_et).isoformat(),
        "track_end_utc": to_utc(end_et).isoformat(),
        "moon_radius_deg": _moon_radius_deg(moon_distance),
        "angular_diameter_arcsec": diameter,
        "center": {
            "direction": [float(center[0]), float(center[1]),
                          float(center[2])],
        },
        "samples": rows,
    }
