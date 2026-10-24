"""Frames and geometry primitives.

Every raw SPICE geometry call used by the higher layers lives here, so that
policy decisions (which aberration correction, which reference frame, whether
a quantity is geometric or apparent) are made once and documented.

Two families of quantities are deliberately kept apart:

* **geometric** — no light-time or aberration correction (``abcorr='NONE'``).
  Used for distances, angular diameters, illumination geometry and orbit
  elements, where a physically instantaneous configuration is wanted.
* **apparent** — light-time and stellar aberration applied (``abcorr='LT+S'``).
  Used for sky positions (RA/Dec, azimuth/elevation, elongation), i.e. where
  the observer actually sees the body.
"""

from __future__ import annotations

import math
from typing import NamedTuple, Optional, Sequence

import numpy as np

from .kernels import SpiceConstants, kernel_session, spice
from .observer import Observer

__all__ = [
    "AzEl",
    "RaDec",
    "Spherical",
    "geometric_position",
    "geometric_state",
    "observer_bf",
    "observer_j2000",
    "az_el",
    "moon_elevation_deg",
    "apparent_radec",
    "topocentric_vector",
    "body_fixed_from_j2000",
    "j2000_from_body_fixed",
    "spherical_to_vector",
    "vector_to_spherical",
    "angular_separation",
    "angular_separation_spherical",
    "position_angle",
    "parallactic_angle",
    "moon_fixed_unit_vector",
    "direction_in_moon_frame",
    "sub_observer_point",
    "surface_intercept",
    "observer_direction_moon_frame",
    "moon_observer_range_km",
    "body_position_moon_frame",
    "position_from_earth",
]


class AzEl(NamedTuple):
    """Horizontal coordinates of a body."""

    azimuth_deg: float
    """Azimuth, degrees clockwise from north (0=N, 90=E, 180=S, 270=W)."""

    elevation_deg: float
    """Elevation above the horizon, degrees (refraction-free geometric)."""

    range_km: float
    """Topocentric distance in kilometres."""

    light_time_s: float
    """One-way light time in seconds (0 for geometric evaluations)."""


class RaDec(NamedTuple):
    """Equatorial coordinates of a body as seen by an observer."""

    ra_hours: float
    """Right ascension in hours, ``[0, 24)``."""

    dec_deg: float
    """Declination in degrees."""

    distance_km: float
    """Topocentric distance in kilometres."""

    light_time_s: float
    """One-way light time in seconds."""


class Spherical(NamedTuple):
    """A planetocentric direction."""

    lat_deg: float
    lon_deg: float
    radius_km: float


# ---------------------------------------------------------------------------
# basic vector helpers
# ---------------------------------------------------------------------------
def spherical_to_vector(lat_deg: float, lon_deg: float) -> np.ndarray:
    """Unit vector for planetocentric latitude/longitude (degrees)."""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    cos_lat = math.cos(lat)
    return np.array(
        [cos_lat * math.cos(lon), cos_lat * math.sin(lon), math.sin(lat)],
        dtype=float,
    )


def vector_to_spherical(vector: Sequence[float]) -> Spherical:
    """Inverse of :func:`spherical_to_vector` (radius in the same units)."""
    radius, lon_rad, lat_rad = spice.reclat([float(c) for c in vector])
    return Spherical(
        lat_deg=math.degrees(lat_rad),
        lon_deg=math.degrees(lon_rad),
        radius_km=float(radius),
    )


def angular_separation(vector_a: Sequence[float], vector_b: Sequence[float]) -> float:
    """Angle between two vectors in radians (numerically stable)."""
    a = np.asarray(vector_a, dtype=float)
    b = np.asarray(vector_b, dtype=float)
    norm_a = float(np.linalg.norm(a))
    norm_b = float(np.linalg.norm(b))
    if norm_a == 0.0 or norm_b == 0.0:
        raise ValueError("angular_separation requires non-zero vectors")
    return float(spice.vsep(a / norm_a, b / norm_b))


def angular_separation_spherical(
    lat1_deg: float, lon1_deg: float, lat2_deg: float, lon2_deg: float
) -> float:
    """Great-circle angle in radians between two spherical directions."""
    return angular_separation(
        spherical_to_vector(lat1_deg, lon1_deg), spherical_to_vector(lat2_deg, lon2_deg)
    )


def position_angle(
    from_vector: Sequence[float],
    to_vector: Sequence[float],
    pole: Sequence[float] = (0.0, 0.0, 1.0),
) -> float:
    """Position angle of *to_vector* as seen from *from_vector*, in radians.

    Measured from the direction of the *pole* (north), increasing towards the
    east side, i.e. the standard astronomical position angle when *pole* is the
    celestial north pole ``+Z``.  Both vectors must be expressed in the same
    frame; the returned angle lies in ``(-pi, pi]``.

    This is the quantity that orients the bright limb of the Moon (and every
    crescent marker drawn on a sky chart): it replaces the legacy
    ``atan2(delta_elevation, delta_azimuth)`` approximation, which ignored the
    non-orthogonality of the azimuth/elevation plane away from the equator.
    """
    v = np.asarray(from_vector, dtype=float)
    v /= np.linalg.norm(v)
    p = np.asarray(pole, dtype=float)
    p /= np.linalg.norm(p)
    north = p - float(np.dot(p, v)) * v
    north_norm = float(np.linalg.norm(north))
    if north_norm < 1e-12:
        raise ValueError("position_angle undefined at the pole")
    north /= north_norm
    east = np.cross(p, v)
    east /= np.linalg.norm(east)
    t = np.asarray(to_vector, dtype=float)
    t = t - float(np.dot(t, v)) * v
    t_norm = float(np.linalg.norm(t))
    if t_norm < 1e-12:
        raise ValueError("position_angle undefined for coincident directions")
    t /= t_norm
    return math.atan2(float(np.dot(t, east)), float(np.dot(t, north)))


def parallactic_angle(
    dec_deg: float,
    hour_angle_deg: float,
    lat_deg: float,
) -> float:
    """Parallactic angle in radians.

    The angle between the celestial north direction and the zenith direction
    at the target, needed to rotate a sky-plane rendering into the observer's
    horizon orientation.
    """
    dec = math.radians(dec_deg)
    hour_angle = math.radians(hour_angle_deg)
    lat = math.radians(lat_deg)
    return math.atan2(
        math.sin(hour_angle),
        math.tan(lat) * math.cos(dec) - math.sin(dec) * math.cos(hour_angle),
    )


# ---------------------------------------------------------------------------
# observer position
# ---------------------------------------------------------------------------
def observer_bf(
    observer: Observer, constants: Optional[SpiceConstants] = None
) -> np.ndarray:
    """Observer position in the Earth body-fixed ``IAU_EARTH`` frame (km)."""
    if constants is None:
        constants = kernel_session().constants
    return np.asarray(
        spice.georec(
            math.radians(observer.lon),
            math.radians(observer.lat),
            observer.height_km,
            constants.earth_radius_km,
            constants.earth_flattening,
        ),
        dtype=float,
    )


def body_fixed_from_j2000(et: float, frame: str = "IAU_EARTH") -> np.ndarray:
    """Rotation matrix taking ``J2000`` vectors into a body-fixed frame."""
    return np.asarray(spice.pxform("J2000", frame, et), dtype=float)


def j2000_from_body_fixed(et: float, frame: str = "IAU_EARTH") -> np.ndarray:
    """Rotation matrix taking body-fixed vectors into ``J2000``."""
    return np.asarray(spice.pxform(frame, "J2000", et), dtype=float)


def observer_j2000(et: float, observer: Observer) -> np.ndarray:
    """Observer position in ``J2000`` (km), including Earth orientation."""
    return np.asarray(
        spice.mxv(j2000_from_body_fixed(et, "IAU_EARTH"), observer_bf(observer)),
        dtype=float,
    )


# ---------------------------------------------------------------------------
# geometric / apparent body positions
# ---------------------------------------------------------------------------
def geometric_position(
    body: str,
    et: float,
    center: str = "EARTH",
    frame: str = "J2000",
) -> np.ndarray:
    """Geometric (light-time free) position vector of *body* (km)."""
    position, _ = spice.spkpos(body, et, frame, "NONE", center)
    return np.asarray(position, dtype=float)


def geometric_state(
    body: str,
    et: float,
    center: str = "EARTH",
    frame: str = "J2000",
) -> np.ndarray:
    """Geometric state vector ``[x, y, z, vx, vy, vz]`` of *body* (km, km/s)."""
    state, _ = spice.spkezr(body, et, frame, "NONE", center)
    return np.asarray(state, dtype=float)


def topocentric_vector(
    body: str,
    et: float,
    observer: Observer,
    *,
    abcorr: str = "LT+S",
) -> tuple:
    """Topocentric vector to *body* and its light time.

    The apparent (``LT+S``) geocentric position is combined with the
    observer's ``J2000`` position.  SPICE cannot apply an aberration correction
    *to a topocentric observer* without an SPK for the site, so this is the
    standard geocentric-apparent-minus-observer construction; it is exact to
    well below the arcsecond for the Moon and the Sun.
    """
    position, light_time = spice.spkpos(body, et, "J2000", abcorr, "EARTH")
    return np.asarray(position, dtype=float) - observer_j2000(et, observer), float(
        light_time
    )


def az_el(
    body: str,
    et: float,
    observer: Observer,
    *,
    abcorr: str = "LT+S",
) -> AzEl:
    """Topocentric azimuth/elevation of *body*.

    Uses SPICE ``azlcpo`` with the ellipsoid surface normal as the local
    zenith, which folds in polar motion, precession and nutation through the
    high-precision Earth orientation kernel.  Geometry only: no atmospheric
    refraction is applied (the caller adds the rise/set convention).
    """
    state, _ = spice.azlcpo(
        "ELLIPSOID",
        body,
        et,
        abcorr,
        False,  # azimuth clockwise from north (navigation convention)
        True,  # elevation positive above the horizon
        observer_bf(observer),
        "EARTH",
        "IAU_EARTH",
    )
    return AzEl(
        azimuth_deg=math.degrees(float(state[1])) % 360.0,
        elevation_deg=math.degrees(float(state[2])),
        range_km=float(state[0]),
        light_time_s=0.0,
    )


def moon_elevation_deg(et: float, observer: Observer) -> float:
    """Convenience wrapper: apparent elevation of the Moon in degrees."""
    return az_el("MOON", et, observer).elevation_deg


def apparent_radec(
    body: str, et: float, observer: Observer, *, abcorr: str = "LT+S"
) -> RaDec:
    """Topocentric apparent right ascension / declination of *body*."""
    vector, light_time = topocentric_vector(body, et, observer, abcorr=abcorr)
    distance, lon_rad, lat_rad = spice.reclat([float(c) for c in vector])
    ra_hours = math.degrees(lon_rad) / 15.0 % 24.0
    return RaDec(
        ra_hours=ra_hours,
        dec_deg=math.degrees(lat_rad),
        distance_km=float(distance),
        light_time_s=light_time,
    )


# ---------------------------------------------------------------------------
# lunar surface geometry
# ---------------------------------------------------------------------------
def moon_fixed_unit_vector(lat_deg: float, lon_deg: float) -> np.ndarray:
    """Unit vector of a selenographic direction in the ``MOON_ME`` frame."""
    return spherical_to_vector(lat_deg, lon_deg)


def direction_in_moon_frame(
    body: str, et: float, *, abcorr: str = "LT+S"
) -> np.ndarray:
    """Unit vector from the Moon's centre towards *body* in ``MOON_ME``.

    ``MOON_ME`` is the mean-Earth lunar body-fixed frame defined by the
    physical-libration kernel, which is the frame selenographic coordinates are
    conventionally expressed in.  Using the direction (rather than an
    intercept point) avoids the ellipsoid iteration and is equivalent to the
    sub-Earth point for a sphere.
    """
    position, _ = spice.spkpos(body, et, "MOON_ME", abcorr, "MOON")
    vector = np.asarray(position, dtype=float)
    return vector / float(np.linalg.norm(vector))


def surface_intercept(
    body: str, et: float, frame: str = "IAU_MOON", abcorr: str = "NONE"
) -> np.ndarray:
    """Surface intercept point on the Moon of the ray from the Moon to *body*."""
    point, _, _ = spice.subpnt(
        "INTERCEPT/ELLIPSOID", "MOON", et, frame, abcorr, body
    )
    return np.asarray(point, dtype=float)


def sub_observer_point(
    et: float, body: str = "EARTH", frame: str = "IAU_MOON"
) -> Spherical:
    """Selenographic coordinates of the sub-*body* point (the libration angle)."""
    return vector_to_spherical(surface_intercept(body, et, frame=frame))


def observer_direction_moon_frame(
    et: float, observer: Observer, *, abcorr: str = "LT+S"
) -> np.ndarray:
    """Unit vector from the Moon's centre towards a topocentric *observer*.

    This is the ``MOON_ME`` counterpart of
    :func:`direction_in_moon_frame`, but for an observer *on* the Earth rather
    than for the Earth's centre.  The difference is the topocentric parallax,
    up to about 0.95 deg, which is exactly the libration a ground-based
    observer sees rather than the geocentric value.

    SPICE cannot apply an aberration correction to a topocentric observer
    without an SPK for the site, so the apparent (``LT+S``) geocentric Moon
    position is differenced with the observer's ``J2000`` position and the
    result rotated into ``MOON_ME``.  The residual is well below an arcsecond.
    """
    moon_position, _ = spice.spkpos("MOON", et, "J2000", abcorr, "EARTH")
    to_observer = observer_j2000(et, observer) - np.asarray(moon_position, dtype=float)
    body_fixed = np.asarray(
        spice.mxv(body_fixed_from_j2000(et, "MOON_ME"), to_observer), dtype=float
    )
    return body_fixed / float(np.linalg.norm(body_fixed))


def moon_observer_range_km(
    et: float, observer: Observer, *, abcorr: str = "LT+S"
) -> float:
    """Topocentric distance to the Moon's centre (km)."""
    moon_position, _ = spice.spkpos("MOON", et, "J2000", abcorr, "EARTH")
    to_observer = observer_j2000(et, observer) - np.asarray(moon_position, dtype=float)
    return float(np.linalg.norm(to_observer))


def body_position_moon_frame(
    body: str, et: float, *, abcorr: str = "LT+S"
) -> np.ndarray:
    """Position vector of *body* relative to the Moon in ``MOON_ME`` (km).

    Unlike :func:`direction_in_moon_frame` this keeps the magnitude, which the
    earthshine model needs in order to place the Earth's finite disk.
    """
    position, _ = spice.spkpos(body, et, "MOON_ME", abcorr, "MOON")
    return np.asarray(position, dtype=float)


def position_from_earth(body: str, et: float, *, abcorr: str = "LT+S") -> np.ndarray:
    """Position vector of *body* relative to the Earth in ``J2000`` (km)."""
    position, _ = spice.spkpos(body, et, "J2000", abcorr, "EARTH")
    return np.asarray(position, dtype=float)
