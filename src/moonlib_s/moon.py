"""Instantaneous lunar state and full reports.

Two entry points, matching two very different costs:

``moon_state``
    Everything that can be derived from a handful of SPICE calls: position,
    distance, apparent size, phase, libration, sub-solar point, orbit
    elements and horizontal coordinates.  Sub-millisecond, safe to call in a
    scanning loop.

``moon_report``
    ``moon_state`` plus rise/transit/set and transit details.  Tens of
    milliseconds — use it for final output, not for scanning.

The legacy library had the same split (``quick_moon_data`` vs
``compute_lunar_data``) but implemented the shared physics twice, with
different key names and different aberration conventions.  Here both entry
points are thin wrappers over one builder.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np

from .frames import (
    angular_separation,
    apparent_radec,
    az_el,
    observer_j2000,
    surface_intercept,
    vector_to_spherical,
)
from .illumination import (
    angular_diameter_arcsec,
    angular_diameter_deg,
    elongation_deg,
    geocentric_phase_angle_deg,
    illuminated_fraction,
    topocentric_phase_angle_deg,
)
from .kernels import kernel_session, spice
from .models import (
    Libration,
    MoonPhase,
    MoonReport,
    MoonState,
    OrbitElements,
    SurfaceIllumination,
)
from .observer import Observer
from .phases import ecliptic_longitude_difference_deg, moon_age, phase_name
from .riseset import RiseSetMode, rise_set_transit, transit_details
from .timeutil import SYNODIC_MONTH_DAYS, to_et, to_utc, utc_now

__all__ = [
    "moon_state",
    "moon_report",
    "libration_angles",
    "orbit_elements",
    "angular_separation_to_sun_deg",
]

_ECLIPTIC = "ECLIPJ2000"
_INERTIAL = "J2000"
_MOON_FRAME = "IAU_MOON"


# ---------------------------------------------------------------------------
# building blocks
# ---------------------------------------------------------------------------
def libration_angles(et: float, *, apparent: bool = False) -> Libration:
    """Selenographic geometry of the sub-Earth and sub-solar points.

    Parameters
    ----------
    et
        Ephemeris time.
    apparent
        When True the sub-Earth direction is light-time and aberration
        corrected.  Defaults to False, i.e. the *geometric* sub-Earth point —
        the conventional definition (the intersection of the Moon–Earth line
        with the surface).  The two differ by at most ~11″.
    """
    abcorr = "LT+S" if apparent else "NONE"
    sub_earth = vector_to_spherical(
        surface_intercept("EARTH", et, frame=_MOON_FRAME, abcorr=abcorr)
    )
    sub_solar = vector_to_spherical(
        surface_intercept("SUN", et, frame=_MOON_FRAME, abcorr="NONE")
    )
    return Libration(
        sub_earth_lat_deg=sub_earth.lat_deg,
        sub_earth_lon_deg=sub_earth.lon_deg,
        subsolar_lat_deg=sub_solar.lat_deg,
        subsolar_lon_deg=sub_solar.lon_deg,
        colongitude_deg=(90.0 - sub_solar.lon_deg) % 360.0,
    )


def _true_anomaly_deg(
    position: np.ndarray, velocity: np.ndarray, mu: float
) -> float:
    """True anomaly (degrees) from an osculating state vector."""
    r = float(np.linalg.norm(position))
    v2 = float(np.dot(velocity, velocity))
    rv = float(np.dot(position, velocity))
    e_vector = ((v2 - mu / r) * position - rv * velocity) / mu
    e_norm = float(np.linalg.norm(e_vector))
    if e_norm < 1e-12:  # pragma: no cover - the lunar orbit is never circular
        return 0.0
    angle = math.degrees(
        angular_separation(e_vector, position)
    )
    return angle if rv >= 0.0 else 360.0 - angle


def orbit_elements(et: float) -> OrbitElements:
    """Osculating geocentric orbit elements of the Moon."""
    constants = kernel_session().constants
    mu = constants.moon_earth_mu
    state = np.asarray(
        spice.spkezr("MOON", et, _ECLIPTIC, "NONE", "EARTH")[0], dtype=float
    )
    position, velocity = state[:3], state[3:]
    elements = spice.oscelt(state, et, mu)
    periapsis = float(elements[0])
    eccentricity = float(elements[1])
    semi_major = periapsis / (1.0 - eccentricity)
    period_days = (
        2.0 * math.pi * math.sqrt(semi_major ** 3 / mu) / 86400.0
    )
    return OrbitElements(
        semi_major_km=semi_major,
        periapsis_km=periapsis,
        apoapsis_km=semi_major * (1.0 + eccentricity),
        eccentricity=eccentricity,
        inclination_deg=math.degrees(float(elements[2])),
        ascending_node_deg=math.degrees(float(elements[3])) % 360.0,
        arg_periapsis_deg=math.degrees(float(elements[4])) % 360.0,
        mean_anomaly_deg=math.degrees(float(elements[5])) % 360.0,
        true_anomaly_deg=_true_anomaly_deg(position, velocity, mu),
        period_days=period_days,
        epoch_et=float(elements[6]),
    )


def angular_separation_to_sun_deg(et: float, observer: Observer) -> float:
    """Apparent topocentric Sun–Moon elongation in degrees."""
    return elongation_deg(et, observer)


def _build_phase(et: float, observer: Observer) -> MoonPhase:
    geo_angle = geocentric_phase_angle_deg(et)
    topo_angle = topocentric_phase_angle_deg(et, observer)
    age = moon_age(et)

    elong = elongation_deg(et, observer)
    # Visual age: the illuminated fraction grows from new to full while the
    # Moon is east of the Sun, and shrinks from full back to new while it is
    # west of it.  Mapping elongation onto [0, 29.53) therefore tracks what an
    # observer sees, independently of the ecliptic-longitude age above.
    waxing = ecliptic_longitude_difference_deg(et) >= 0.0
    fraction = elong / 360.0
    visual_age = (
        fraction * SYNODIC_MONTH_DAYS
        if waxing
        else SYNODIC_MONTH_DAYS * (1.0 - fraction)
    )

    name_en, name_zh = phase_name(age.age_days)
    return MoonPhase(
        phase_angle_geo_deg=geo_angle,
        phase_angle_topo_deg=topo_angle,
        illuminated_fraction_geo=illuminated_fraction(geo_angle),
        illuminated_fraction_topo=illuminated_fraction(topo_angle),
        age_days=age.age_days,
        visual_age_days=visual_age,
        name_en=name_en,
        name_zh=name_zh,
        last_new_moon_utc=age.new_moon_utc,
    )


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def moon_state(observer: Optional[Observer] = None, when=None) -> MoonState:
    """Complete instantaneous lunar state for an observer.

    Parameters
    ----------
    observer
        Observing site (:class:`~moonlib_s.observer.Observer`, a
        ``(lat, lon[, height_km])`` sequence, or ``None`` for the default).
    when
        Instant; anything accepted by :func:`moonlib_s.timeutil.to_et`.
        ``None`` means now.

    Returns
    -------
    MoonState
        Frozen, JSON-serialisable via :meth:`MoonState.to_dict`.
    """
    session = kernel_session()
    constants = session.constants
    observer = Observer.from_value(observer)
    et = to_et(utc_now() if when is None else when)
    session.require_epoch(et, what="observation time")

    site = observer_j2000(et, observer)

    # -- geometric state ---------------------------------------------------
    state = np.asarray(
        spice.spkezr("MOON", et, _INERTIAL, "NONE", "EARTH")[0], dtype=float
    )
    moon_position = state[:3]
    velocity_kms = float(np.linalg.norm(state[3:]))

    geo_distance = float(np.linalg.norm(moon_position))
    topo_distance = float(np.linalg.norm(moon_position - site))

    diameter_deg = angular_diameter_deg(topo_distance)

    # -- apparent sky position --------------------------------------------
    radec = apparent_radec("MOON", et, observer)
    _, light_time = spice.spkpos("MOON", et, _INERTIAL, "LT+S", "EARTH")

    moon_horizontal = az_el("MOON", et, observer)
    sun_horizontal = az_el("SUN", et, observer)

    # -- surface geometry --------------------------------------------------
    sub_earth_point = surface_intercept(
        "EARTH", et, frame=_MOON_FRAME, abcorr="NONE"
    )
    _, _, surface_phase, incidence, emission = spice.ilumin(
        "ELLIPSOID", "MOON", et, _MOON_FRAME, "NONE", "EARTH", sub_earth_point
    )

    return MoonState(
        when_utc=to_utc(et),
        et=float(et),
        observer=observer,
        phase=_build_phase(et, observer),
        libration=libration_angles(et),
        orbit=orbit_elements(et),
        surface=SurfaceIllumination(
            incidence_deg=math.degrees(float(incidence)),
            emission_deg=math.degrees(float(emission)),
            phase_deg=math.degrees(float(surface_phase)),
        ),
        geo_distance_km=geo_distance,
        topo_distance_km=topo_distance,
        angular_diameter_deg=diameter_deg,
        angular_diameter_arcsec=angular_diameter_arcsec(topo_distance),
        ra_hours=radec.ra_hours,
        dec_deg=radec.dec_deg,
        azimuth_deg=moon_horizontal.azimuth_deg,
        elevation_deg=moon_horizontal.elevation_deg,
        elongation_deg=elongation_deg(et, observer),
        sun_azimuth_deg=sun_horizontal.azimuth_deg,
        sun_elevation_deg=sun_horizontal.elevation_deg,
        velocity_kms=velocity_kms,
        light_time_s=float(light_time),
        moon_radius_km=constants.moon_radius_km,
    )


def moon_report(
    observer: Optional[Observer] = None,
    when=None,
    *,
    rise_set_mode: "RiseSetMode | str" = RiseSetMode.OBSERVATIONAL,
    with_transit_details: bool = True,
) -> MoonReport:
    """Instantaneous state plus rise, transit and set for the same cycle."""
    state = moon_state(observer, when)
    events = rise_set_transit(state.observer, state.et, mode=rise_set_mode)
    details = (
        transit_details(state.observer, to_et(events.transit_utc))
        if with_transit_details and events.transit_utc is not None
        else None
    )
    return MoonReport(state=state, rise_set=events, transit=details)
