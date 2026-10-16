"""Illumination and phase geometry — the single implementation.

The legacy code computed the phase angle, illuminated fraction, topocentric
phase angle, elongation and angular diameter in four different places
(``compute_lunar_data``, ``quick_moon_data``, ``find_full_moons`` and several
notebooks) with subtly different conventions.  Everything now goes through this
module, which offers

* a **scalar API** for single-instant, high-accuracy work (used by
  :func:`moonlib_s.moon.moon_state`), and
* an **array API** for scanning (used by the phase finders, the crescent window
  search and the libration sweep), which evaluates the same physics through
  vectorised SPICE calls — roughly two orders of magnitude faster than calling
  the scalar path in a Python loop.
"""

from __future__ import annotations

import math
from typing import Tuple

import numpy as np

from .frames import (
    angular_separation,
    observer_bf,
    observer_j2000,
    surface_intercept,
    topocentric_vector,
)
from .kernels import kernel_session, spice
from .models import PhaseMode
from .observer import Observer

__all__ = [
    "PhaseMode",
    "AU_KM",
    "illuminated_fraction",
    "phase_angle_from_fraction",
    "angular_diameter_deg",
    "angular_diameter_arcsec",
    "geocentric_phase_angle_deg",
    "topocentric_phase_angle_deg",
    "illumination",
    "elongation_deg",
    "crescent_width_arcsec",
    "scan_geometry",
    "ScanGeometry",
]

#: Astronomical unit in kilometres (DE440 value).
AU_KM = 149597870.7


#: Re-exported from :mod:`moonlib_s.models` so that callers can keep using
#: ``moonlib_s.illumination.PhaseMode``.
__all__.append("PhaseMode")


# ---------------------------------------------------------------------------
# scalar relations
# ---------------------------------------------------------------------------
def illuminated_fraction(phase_angle_deg: float) -> float:
    """Illuminated fraction of the apparent disk from the phase angle.

    Uses the standard spherical-illumination relation
    ``f = (1 + cos i) / 2``.
    """
    return (1.0 + math.cos(math.radians(phase_angle_deg))) * 0.5


def phase_angle_from_fraction(fraction: float) -> float:
    """Inverse of :func:`illuminated_fraction`; returns degrees in ``[0, 180]``."""
    clamped = max(-1.0, min(1.0, 2.0 * fraction - 1.0))
    return math.degrees(math.acos(clamped))


def angular_diameter_deg(distance_km: float) -> float:
    """Angular diameter (degrees) of the Moon at a given distance."""
    radius = kernel_session().constants.moon_radius_km
    return math.degrees(2.0 * math.atan2(radius, distance_km))


def angular_diameter_arcsec(distance_km: float) -> float:
    return angular_diameter_deg(distance_km) * 3600.0


def crescent_width_arcsec(illumination_fraction: float, diameter_arcsec: float) -> float:
    """Width of the illuminated sliver at the cusps' midpoint (arcseconds).

    This is the quantity an observer actually tries to resolve; it is the
    physically meaningful "thinness" of a crescent and is reported alongside
    the illumination percentage, which saturates near new moon.
    """
    return float(illumination_fraction) * float(diameter_arcsec)


# ---------------------------------------------------------------------------
# scalar geometry
# ---------------------------------------------------------------------------
def geocentric_phase_angle_deg(et: float) -> float:
    """Geocentric phase angle in degrees.

    Evaluated with SPICE ``illumf`` at the sub-Earth surface point, so the
    local ellipsoid normal at the centre of the visible disk is used rather
    than the centre-of-mass direction.  This matches long-standing almanac
    practice and the legacy implementation.
    """
    sub_earth = surface_intercept("EARTH", et, frame="IAU_MOON", abcorr="NONE")
    _, _, phase, _, _, _, _ = spice.illumf(
        "ELLIPSOID", "MOON", "SUN", et, "IAU_MOON", "NONE", "EARTH", sub_earth
    )
    return math.degrees(float(phase))


def topocentric_phase_angle_deg(et: float, observer) -> float:
    """Topocentric phase angle in degrees: the Sun–Moon–observer angle.

    Differs from the geocentric value by up to about a degree when the Moon is
    near the horizon, which is exactly when crescent visibility is decided.
    """
    observer = Observer.from_value(observer)
    moon = np.asarray(
        spice.spkpos("MOON", et, "J2000", "NONE", "EARTH")[0], dtype=float
    )
    sun = np.asarray(spice.spkpos("SUN", et, "J2000", "NONE", "EARTH")[0], dtype=float)
    site = observer_j2000(et, observer)
    return math.degrees(
        angular_separation(sun - moon, site - moon)
    )


def illumination(
    et: float, observer, mode: PhaseMode = PhaseMode.GEOCENTRIC
) -> Tuple[float, float]:
    """``(illuminated_fraction, phase_angle_deg)`` for the requested mode."""
    observer = Observer.from_value(observer)
    geo = geocentric_phase_angle_deg(et)
    if mode is PhaseMode.GEOCENTRIC:
        return illuminated_fraction(geo), geo
    topo = topocentric_phase_angle_deg(et, observer)
    return illuminated_fraction(topo), topo


def elongation_deg(et: float, observer) -> float:
    """Apparent topocentric Sun–Moon elongation in degrees."""
    observer = Observer.from_value(observer)
    moon_vector, _ = topocentric_vector("MOON", et, observer)
    sun_vector, _ = topocentric_vector("SUN", et, observer)
    return math.degrees(angular_separation(moon_vector, sun_vector))


# ---------------------------------------------------------------------------
# array geometry (scanning)
# ---------------------------------------------------------------------------
class ScanGeometry:
    """Vectorised geometry over a grid of ephemeris times.

    Attributes are NumPy arrays of length ``n`` (or ``(n, 3)`` for vectors).
    """

    __slots__ = (
        "ets",
        "phase_angle_geo_deg",
        "phase_angle_topo_deg",
        "illumination_geo",
        "illumination_topo",
        "elongation_deg",
        "distance_km",
        "angular_diameter_arcsec",
        "elevation_deg",
        "sun_elevation_deg",
        "moon_vector",
        "sun_vector",
    )

    def __init__(self, **kwargs) -> None:
        for name in self.__slots__:
            setattr(self, name, kwargs.get(name))

    def illumination(self, mode: PhaseMode) -> np.ndarray:
        return (
            self.illumination_geo
            if mode is PhaseMode.GEOCENTRIC
            else self.illumination_topo
        )

    def phase_angle(self, mode: PhaseMode) -> np.ndarray:
        return (
            self.phase_angle_geo_deg
            if mode is PhaseMode.GEOCENTRIC
            else self.phase_angle_topo_deg
        )

    def crescent_width_arcsec(self) -> np.ndarray:
        return self.illumination_geo * self.angular_diameter_arcsec


def scan_geometry(
    ets: np.ndarray,
    observer=None,
    *,
    with_horizontal: bool = False,
) -> ScanGeometry:
    """Evaluate phase/illumination geometry over a grid of ETs.

    Parameters
    ----------
    ets
        Ephemeris times (1-D array).
    observer
        Observing site.  Required when ``with_horizontal`` is true or when the
        topocentric quantities are needed; when ``None`` the topocentric arrays
        mirror the geocentric ones.
    with_horizontal
        Also evaluate topocentric azimuth/elevation for the Moon and the Sun.
        This is the expensive part (one ``azlcpo`` call per sample) and is
        skipped by default — the phase finders do not need it.
    """
    times = np.asarray(ets, dtype=float)
    moon = np.asarray(
        spice.spkpos("MOON", times, "J2000", "NONE", "EARTH")[0], dtype=float
    )
    sun = np.asarray(spice.spkpos("SUN", times, "J2000", "NONE", "EARTH")[0], dtype=float)

    # Geocentric phase angle: Sun-Moon-Earth-centre.
    geo_phase = _angle_between(sun - moon, -moon)
    geo_illum = 0.5 * (1.0 + np.cos(geo_phase))

    distance = np.linalg.norm(moon, axis=1)
    diameter = np.degrees(
        2.0 * np.arctan2(kernel_session().constants.moon_radius_km, distance)
    ) * 3600.0

    if observer is None:
        topo_phase = geo_phase.copy()
        site = None
    else:
        observer = Observer.from_value(observer)
        site = _observer_j2000_array(times, observer)
        topo_phase = _angle_between(sun - moon, site - moon)
    topo_illum = 0.5 * (1.0 + np.cos(topo_phase))

    # Apparent elongation (topocentric, light-time and aberration corrected).
    moon_app = np.asarray(
        spice.spkpos("MOON", times, "J2000", "LT+S", "EARTH")[0], dtype=float
    )
    sun_app = np.asarray(
        spice.spkpos("SUN", times, "J2000", "LT+S", "EARTH")[0], dtype=float
    )
    if site is not None:
        moon_app = moon_app - site
        sun_app = sun_app - site
    elong = _angle_between(moon_app, sun_app)

    elevation = sun_elevation = None
    if with_horizontal:
        if observer is None:
            raise ValueError("with_horizontal=True requires an observer")
        moon_az_el = np.empty((times.size, 2), dtype=float)
        sun_az_el = np.empty((times.size, 2), dtype=float)
        obs_bf = observer_bf(observer)
        for index, et in enumerate(times):
            state, _ = spice.azlcpo(
                "ELLIPSOID", "MOON", float(et), "LT+S", False, True, obs_bf,
                "EARTH", "IAU_EARTH",
            )
            moon_az_el[index] = (math.degrees(state[1]) % 360.0, math.degrees(state[2]))
            state, _ = spice.azlcpo(
                "ELLIPSOID", "SUN", float(et), "LT+S", False, True, obs_bf,
                "EARTH", "IAU_EARTH",
            )
            sun_az_el[index] = (math.degrees(state[1]) % 360.0, math.degrees(state[2]))
        elevation = moon_az_el[:, 1]
        sun_elevation = sun_az_el[:, 1]

    return ScanGeometry(
        ets=times,
        phase_angle_geo_deg=np.degrees(geo_phase),
        phase_angle_topo_deg=np.degrees(topo_phase),
        illumination_geo=geo_illum,
        illumination_topo=topo_illum,
        elongation_deg=np.degrees(elong),
        distance_km=distance,
        angular_diameter_arcsec=diameter,
        elevation_deg=elevation,
        sun_elevation_deg=sun_elevation,
        moon_vector=(moon_app if site is not None else moon),
        sun_vector=(sun_app if site is not None else sun),
    )


def _angle_between(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Row-wise angle between two ``(n, 3)`` arrays, in radians."""
    a = np.atleast_2d(a)
    b = np.atleast_2d(b)
    num = np.einsum("ij,ij->i", a, b)
    den = np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1)
    return np.arccos(np.clip(num / den, -1.0, 1.0))


def _observer_j2000_array(ets: np.ndarray, observer) -> np.ndarray:
    """Observer ``J2000`` positions over a grid (km), shape ``(n, 3)``."""
    obs_bf = observer_bf(observer)
    out = np.empty((ets.size, 3), dtype=float)
    for index, et in enumerate(ets):
        out[index] = spice.mxv(spice.pxform("IAU_EARTH", "J2000", float(et)), obs_bf)
    return out
