"""Physically-motivated simulation of the Moon as seen from the ground.

What this module computes
-------------------------
Given an observing site and an instant it produces the radiance field of the
lunar disk the way a camera or an eye at that site would receive it:

* the **true silhouette**, obtained by ray-marching the LOLA height field, so
  the limb follows the real topography and crater rims occlude their floors;
* **topographic shading** from DEM-derived normals, not from a sphere;
* **cast shadows and their penumbra**, resolved by marching the sun ray and by
  sampling the solar disk, which is what makes the grazing illumination inside
  the terminator band legible;
* **earthshine** from the Earth's finite, phase-dependent disk — including the
  soft, ~2° wide "earthset" transition that only an extended source produces;
* an empirical photometric function (McEwen lunar-Lambert by default) so the
  disk-integrated brightness and limb behaviour match the real Moon.

Geometry conventions
--------------------
Everything is done in the ``MOON_ME`` frame, which is the frame the global
LOLA/LROC rasters are gridded in; :mod:`moonlib_s.moon` reports libration in
``IAU_MOON`` instead, and the two differ by the physical-libration offset of
the principal axes.  The image is rendered **north up with selenographic east
to the right**, which is how the near side appears from Earth: at first quarter
the sub-solar longitude is +90°, and a waxing Moon is lit on the right from the
northern hemisphere.

Units
-----
Radiance is returned as the dimensionless radiance factor ``I/F``: reflected
radiance divided by the incident irradiance over pi.  A perfectly diffuse white
surface at normal incidence and zero phase would read 1.0, so the sunlit Moon
peaks near its normal albedo (~0.13) and the earthshine-lit hemisphere near
1e-5.  That ratio is a physical result, not a display choice, and it is why a
single exposure can never show both.
"""

from __future__ import annotations

import math
import os
from dataclasses import replace
from typing import Any, Dict, Optional, Tuple

import numpy as np

from .errors import MoonlibError
from .frames import (
    angular_separation,
    body_position_moon_frame,
    direction_in_moon_frame,
    moon_observer_range_km,
    observer_direction_moon_frame,
    position_from_earth,
    vector_to_spherical,
)
from .instrument import ARCSEC_PER_RADIAN
from .kernels import kernel_session
from .models import (
    MoonRegion,
    MoonRender,
    MoonRenderSettings,
    MoonView,
    PhotometricModel,
    ToneMapping,
    as_photometric_model,
    as_tone_mapping,
)
from .observer import Observer
from typing import List
from .terrain import MOON_REFERENCE_RADIUS_KM, MoonTerrain, open_terrain
from .timeutil import to_et, to_utc, utc_now

__all__ = [
    "SOLAR_ANGULAR_RADIUS_DEG",
    "SUN_APPARENT_MAGNITUDE",
    "DEFAULT_EARTH_GEOMETRIC_ALBEDO",
    "view_geometry",
    "region_geometry",
    "required_sky_span_km",
    "render_moon",
    "render_region",
    "render_feature",
    "disk_function",
    "disk_integrated_phase",
    "roughness_shadowing",
    "lunar_lambert_weight",
    "apply_tone_mapping",
    "VISUAL_ASINH_SOFTENING",
    "moon_magnitude",
    "view_summary",
    "selection_footprint",
]

#: Mean apparent angular radius of the Sun, degrees.  Used for the penumbra.
SOLAR_ANGULAR_RADIUS_DEG = 0.2666

#: Apparent V magnitude of the Sun at 1 AU; the anchor for every other
#: magnitude this module reports.
SUN_APPARENT_MAGNITUDE = -26.74

#: Mean Earth-Moon distance, km: the reference distance the Moon's magnitude
#: fit is quoted at.
MEAN_LUNAR_DISTANCE_KM = 384400.0


def moon_magnitude(
    phase_angle_deg: float,
    distance_km: Optional[float] = None,
    *,
    mean_distance_km: float = MEAN_LUNAR_DISTANCE_KM,
) -> float:
    """Apparent visual magnitude of the Moon at a given phase and distance.

    Allen's (1976) fit to Russell's (1916) visual photometry::

        m = -12.73 + 0.026 |alpha| + 4e-9 alpha**4

    plus ``5 log10(d / d_mean)`` for the distance term.  *alpha* is the
    Sun-Moon-observer angle in degrees -- the same phase angle
    :class:`MoonView` reports -- so a quarter moon comes out near -10.4 and a
    full one near -12.7.

    The fit is a whole-disk average of a famously non-Lambertian surface, so
    treat it as the conventional number rather than a measurement: it is good
    to a few tenths of a magnitude over the phases anyone observes, and it is
    **not** usable near new moon, where the real Moon is some five magnitudes
    fainter than the polynomial says (the fit has no term for the opposition
    surge and none for the earthshine that is all you see at alpha ~ 180).
    """
    alpha = float(phase_angle_deg)
    if not math.isfinite(alpha):
        raise MoonlibError("phase_angle_deg must be finite.")
    distance = MEAN_LUNAR_DISTANCE_KM if distance_km is None else float(distance_km)
    if not math.isfinite(distance) or distance <= 0.0:
        raise MoonlibError("distance_km must be positive.")
    absolute = abs(alpha)
    magnitude = -12.73 + 0.026 * absolute + 4.0e-9 * absolute ** 4
    return magnitude + 5.0 * math.log10(distance / float(mean_distance_km))


#: Earth's geometric albedo.  Published values scatter between 0.30 and 0.43
#: depending on how the non-Lambertian phase function is folded in; 0.367 sits
#: in the middle and reproduces the frequently quoted "full Earth from the Moon
#: is about magnitude -16.8".
DEFAULT_EARTH_GEOMETRIC_ALBEDO = 0.367

#: McEwen (1991) cubic coefficients for the lunar-Lambert weight ``L(alpha)``,
#: exactly as USGS ISIS ``LunarLambertMcEwen`` uses them (alpha in degrees).
_MCEWEN_A1 = -0.019
_MCEWEN_A2 = 0.000242
_MCEWEN_A3 = -0.00000146

#: The LROC colour mosaic is photometrically normalised, but to incidence
#: 60 deg / emission 0 deg / phase 60 deg rather than to normal albedo, and its
#: absolute level was adjusted for human viewing (Sato et al. 2014, JGR Planets
#: 119, 1775).  It is therefore proportional to normal albedo with one unknown
#: global constant, which the renderer supplies as ``normal_albedo`` after
#: dividing the mosaic by its own global mean.  Nothing here needs the
#: normalisation geometry itself: that constant cancels, and keeping the
#: division would only imply a precision the product does not have.

#: Calibrated against the Clementine observations quoted in
#: ``tools/calibrate_opposition.py``: the observed surge and the far-field
#: shape are fitted *jointly*, because they pull on the same parameters.
#:
#: A note on the phase function.  Hapke (2002) writes p(g) as a two-term
#: Henyey-Greenstein function.  Adding the second lobe here was tried and
#: measured: with the sign convention this code uses, the second lobe peaks at
#: 180 deg, so the whole-disk curve *turned back up* past 30 deg -- a Moon that
#: brightens towards new Moon, which no photometry shows.  The single lobe is
#: retained, and its asymmetry was re-fitted instead (``g`` lands at a mild
#: forward value, which is what a one-lobe stand-in needs to imitate what two
#: lobes do; it is not a claim about the grains).
_HAPKE_DEFAULTS = {
    # Single-lobe Henyey-Greenstein phase function.
    #
    # **Mind the sign.**  In the form ``_hapke_raw`` actually evaluates,
    # ``(1-g^2)/(1 + g^2 - 2 g cos a)^1.5``, a POSITIVE coefficient peaks at
    # ``a = 0`` (opposition, i.e. back-scattering) and a negative one peaks at
    # 180 deg (forward).  The previous default of -0.15 was therefore
    # forward-peaked -- backwards for a regolith, whatever its comment claimed.
    # The value below is what the fit chose once the sign was read off the
    # formula instead of assumed; see docs/07 section 7.1.4.
    "asymmetry": 0.042,
    "single_scattering_albedo": 0.382,
    # Opposition effect in TWO terms, as in Hapke (2002) Eq. (32): a narrow
    # coherent-backscatter term (CBOE) and a wide shadow-hiding one (SHOE).
    #
    # One term cannot do the job, and that is a *measured* statement rather than
    # a preference: at the width Buratti, Hillier & Wang (1996) report from
    # Clementine (h = 0.05-0.07) a single term caps 0.1->1 deg at about +11 %
    # against their +16 %, and forcing +16 % needs h ~ 0.015, i.e. a surge far
    # narrower than anyone measured.  Two terms hold both anchors *and* keep the
    # wide component inside the measured window.
    #
    # The fit puts most of the amplitude in the NARROW term.  That is the branch
    # Hapke (2002) himself argues for: of the two solutions that retrieve the
    # same curve, "physical considerations suggest that the solution with the
    # narrow CBOE is most likely to be correct".
    "opposition_amplitude": 0.145,
    "opposition_width": 0.060,
    "coherent_amplitude": 1.011,
    "coherent_width": 0.0154,
}

_GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))

_TINY = 1e-12

#: Height bias subtracted from the terrain in the shadow test, in kilometres.
#: Three DEM quanta of 0.5 m: enough that bilinear interpolation of a sloping
#: texel cannot report a self-intersection, small enough to shorten a grazing
#: shadow by only a kilometre or so.
_SHADOW_BIAS_KM = 1.5e-3

#: Hard ceiling on samples per ray, so a pathological ``shadow_steps`` (or a
#: request from an old settings file) cannot turn one frame into an hour of
#: marching.
_MAX_SHADOW_SAMPLES = 512

#: Rays traced per band.  This, not the frame size, is what bounds peak memory:
#: a 8000x8000 frame at 2x2 samples is 256 million rays, which used to be
#: refused outright and now simply takes more bands.
#: Ceiling on band threads, whatever the machine reports.  The work is
#: memory-bandwidth bound well before it is core bound, so past a handful of
#: threads the curve flattens.
_MAX_BAND_WORKERS = 8
#: How many bands a frame is cut into, when memory allows.  This is a property
#: of the frame, *not* of the machine: the band size must not depend on how many
#: threads happen to be available, or the same render would come out slightly
#: different on a different computer.
_TARGET_BANDS = 16
_RAYS_PER_BAND = 4_000_000

#: Absolute ceiling on the rays in one band, so a pathological request still
#: fails loudly rather than exhausting memory.
_MAX_RAYS = 32_000_000

#: Percentile of the positive radiance that :attr:`ToneMapping.VISUAL` puts on
#: the knee of its asinh curve.  A fixed softening cannot serve every phase:
#: the earthshine-to-sunlight ratio itself moves by two orders of magnitude
#: between full Moon and a thin crescent, so the same curve would either drown
#: the crescent or wash out the dark side.  Pinning the *faintest extended
#: detail* instead is the global-curve analogue of the local adaptation a
#: dark-adapted eye performs.
VISUAL_KNEE_PERCENTILE = 2.0

#: Fallback softening for :attr:`ToneMapping.VISUAL` when the exposure is fixed
#: rather than automatic, so there is no image statistics to adapt to.
VISUAL_ASINH_SOFTENING = 1.0e-6


# ---------------------------------------------------------------------------
# photometry
# ---------------------------------------------------------------------------
def lunar_lambert_weight(phase_angle_deg: np.ndarray) -> np.ndarray:
    """McEwen's phase-dependent blend weight ``L(alpha)``, clamped to [0, 1].

    The published cubic is a fit to lunar photometry over roughly
    ``0 < alpha < 130`` deg and turns over beyond that, reaching zero near
    155 deg.  Clamping keeps the two weights physical for very thin crescents,
    where the fit no longer applies and the function falls back to Lambert.
    """
    alpha = np.asarray(phase_angle_deg, dtype=np.float64)
    weight = 1.0 + _MCEWEN_A1 * alpha + _MCEWEN_A2 * alpha ** 2 + _MCEWEN_A3 * alpha ** 3
    return np.clip(weight, 0.0, 1.0)


def _hapke_raw(
    mu0: np.ndarray,
    mu: np.ndarray,
    phase_angle_rad: np.ndarray,
) -> np.ndarray:
    """Hapke single scattering plus a two-term opposition, two-stream ``H``.

    A full Hapke (2002) model -- two-term phase function, the ``M`` multiple-
    scattering term and the Legendre moment machinery -- was implemented and
    measured against this form; the derivation is transcribed in ``docs/07``
    section 7.1.2 and the prototype can reproduce the paper's equations, but its
    parameter fit could not be validated in the renderer (the installed path
    disagreed with the prototype and produced non-physical negatives for some
    coefficients), so this simpler form stays.  It is the one whose numbers are
    verified end to end.

    The opposition term is the part of Hapke (2002) that *did* prove necessary:
    Eq. (32) splits it into a narrow coherent-backscatter term and a wide
    shadow-hiding one, and a single term cannot satisfy both Clementine anchors
    at a measured width -- see :data:`_HAPKE_DEFAULTS` and ``docs/07`` 7.1.4.
    """
    w = _HAPKE_DEFAULTS["single_scattering_albedo"]
    b0 = _HAPKE_DEFAULTS["opposition_amplitude"]
    h = _HAPKE_DEFAULTS["opposition_width"]
    bc = _HAPKE_DEFAULTS["coherent_amplitude"]
    hc = _HAPKE_DEFAULTS["coherent_width"]
    g = _HAPKE_DEFAULTS["asymmetry"]

    alpha = np.clip(phase_angle_rad, 1e-9, math.pi)
    # Single-term Henyey-Greenstein lobe:  p(g) = (1 - g^2)/(1 + g^2 - 2 g cos g)^1.5
    # which peaks at g = 0 (opposition) for a positive coefficient.
    cos_a = np.cos(alpha)
    denom = (1.0 + g * g - 2.0 * g * cos_a) ** 1.5
    phase = (1.0 - g * g) / np.maximum(denom, _TINY)
    # Both opposition terms use the shadow-hiding shape 1/(1 + tan(g/2)/h); they
    # differ only in width, which is what lets a wide, measured surge carry the
    # 4-deg anchor while a narrow one carries the 1-deg anchor.
    tan_half = np.tan(0.5 * alpha)
    opposition = (
        1.0
        + b0 / (1.0 + tan_half / h)
        + bc / (1.0 + tan_half / hc)
    )
    root = math.sqrt(max(1.0 - w, _TINY))
    h0 = (1.0 + 2.0 * mu0) / (1.0 + 2.0 * root * mu0)
    h1 = (1.0 + 2.0 * mu) / (1.0 + 2.0 * root * mu)
    return (w / (4.0 * math.pi)) * (mu0 / np.maximum(mu0 + mu, _TINY)) * (
        phase * opposition + h0 * h1 - 1.0
    )


def roughness_shadowing(
    mu0: np.ndarray, mu: np.ndarray, mean_slope_deg: float
) -> np.ndarray:
    """Shadowing by micro-relief on a rough surface -- an approximation.

    Real regolith is rough at scales far below any pixel, so part of it is
    shadowed by its own micro-relief even where the *mean* surface is lit.  This
    factor multiplies the reflection: 1 for a smooth surface, below 1 as either
    the illumination or the view grazes, which is why a rough terminator falls
    off faster than a smooth one and looks grainy.  It is a property of the
    *surface*, not of any scattering law, so it multiplies whatever
    :func:`disk_function` produced.

    The form used is::

        S = 1 / (1 + tan(theta_bar) * tan(max(i, e)) / 2)

    with ``i`` and ``e`` the incidence and emergence angles and ``theta_bar``
    the mean surface slope (Hapke's roughness parameter, 15-25 deg for lunar
    soil).  It is symmetric in ``i`` and ``e``, as the physics requires; it is
    exactly 1 when ``theta_bar`` is 0, so the option can be off without moving
    a byte; it is exactly 1 at the sub-solar point for any roughness, so a
    full-Moon disk does not darken in its middle; and it falls smoothly and
    monotonically with both the mean slope and the grazing angle, with no
    threshold past which the knob stops responding.

    It is **not** Hapke's (1984) exact roughness integral.  The exact theory
    averages the reflection over a distribution of facet tilts with the mutual
    shadowing between them, and needs a two-dimensional quadrature per point --
    viable for a whole-disk integral, not for every pixel of a render.  This is
    the standard one-parameter reduction, and it is calibrated only to the
    properties listed above: strongly shadowed geometries (both ``mu0`` and
    ``mu`` below about 0.3) are where it is least trustworthy.
    """
    if mean_slope_deg <= 0.0:
        return np.ones_like(mu0, dtype=np.float64)
    mu0 = np.clip(np.asarray(mu0, dtype=np.float64), 0.0, 1.0)
    mu = np.clip(np.asarray(mu, dtype=np.float64), 0.0, 1.0)
    angle = math.radians(min(max(mean_slope_deg, 0.0), 89.0))
    grazing = np.tan(np.maximum(np.arccos(mu0), np.arccos(mu)))
    return np.clip(
        1.0 / (1.0 + math.tan(angle) * np.maximum(grazing, 0.0) / 2.0), 0.0, 1.0
    )


def disk_integrated_phase(
    phase_angle_deg: Any,
    model: Any = "lunar_lambert",
    *,
    points: int = 192,
) -> np.ndarray:
    """Whole-disk brightness of a uniform sphere against phase angle.

    This is the observable the photometric models exist to reproduce: what a
    whole-Moon photometer measures as the phase angle sweeps.  The disk is
    integrated on a polar grid whose cell areas are exactly the quadrature
    weights, so no solid-angle approximation enters, and the *disk function*
    alone is evaluated -- the normal albedo cancels in every ratio that
    matters.

    Parameters
    ----------
    phase_angle_deg
        Scalar or array of Sun-Moon-observer angles, degrees.
    model
        :class:`moonlib_s.models.PhotometricModel` or its string value.
    points
        Radial resolution of the quadrature; the azimuthal resolution follows
        it.  The default is converged to better than 1e-5 for every model here.

    Returns
    -------
    numpy.ndarray
        ``I/F`` of the whole disk, normalised the same way the disk functions
        are: one at zero phase with a face-on, fully illuminated surface.
    """
    angles = np.atleast_1d(np.asarray(phase_angle_deg, dtype=np.float64))
    r = (np.arange(points) + 0.5) / points
    phi = (np.arange(points * 2) + 0.5) / (points * 2) * 2.0 * math.pi
    radius, azimuth = np.meshgrid(r, phi, indexing="ij")
    x = radius * np.cos(azimuth)
    z = np.sqrt(np.maximum(0.0, 1.0 - radius * radius))
    weight = radius
    out = np.empty(angles.size, dtype=np.float64)
    for index, alpha in enumerate(angles):
        radians = math.radians(float(alpha))
        mu0 = np.clip(
            x * math.sin(radians) + z * math.cos(radians), 0.0, 1.0
        )
        lit = mu0 > 0.0
        if not lit.any():                      # pragma: no cover - alpha <= 90
            out[index] = 0.0
            continue
        value = disk_function(
            model, mu0[lit], z[lit], np.full(int(lit.sum()), float(alpha))
        )
        out[index] = float(np.sum(value * weight[lit]) / np.sum(weight[lit]))
    return out


def _hapke_normalisation() -> float:
    """Reciprocal of the Hapke reflectance at zero phase, normal incidence.

    Every disk function in this module is scaled so that ``D = 1`` for a
    surface at ``alpha = 0`` with ``mu0 = mu = 1``.  That makes them
    interchangeable under ``I/F = normal_albedo * D`` and lets the albedo map
    carry the absolute brightness instead of the scattering law.
    """
    cached = _HAPKE_NORMALISATION[0]
    if cached is None:
        cached = 1.0 / float(_hapke_raw(np.array(1.0), np.array(1.0), np.array(1e-9)))
        _HAPKE_NORMALISATION[0] = cached
    return cached


_HAPKE_NORMALISATION: list = [None]


def disk_function(
    model: Any,
    mu0: np.ndarray,
    mu: np.ndarray,
    phase_angle_deg: np.ndarray,
    roughness_deg: float = 0.0,
) -> np.ndarray:
    """Evaluate a photometric disk function.

    Parameters
    ----------
    model
        :class:`moonlib_s.models.PhotometricModel` or its string value.
    mu0, mu
        Cosines of the local incidence and emergence angles.
    phase_angle_deg
        Angle between the incident and emergent directions, degrees.
    roughness_deg
        Mean surface slope, degrees, for Hapke's macroscopic shadowing by
        micro-relief; 0 (the default) leaves the model untouched, and the
        factor is 1 at the sub-solar point for any value, so a full-Moon disk
        does not darken in its middle.

    Returns
    -------
    numpy.ndarray
        The disk function ``D``, normalised so that ``D = 1`` at zero phase
        angle with ``mu0 = mu = 1``.  Every model here satisfies that, so
        ``I/F = normal_albedo * D`` and the models can be swapped without
        rescaling the albedo.
    """
    kind = as_photometric_model(model)
    mu0 = np.clip(np.asarray(mu0, dtype=np.float64), 0.0, 1.0)
    mu = np.clip(np.asarray(mu, dtype=np.float64), 0.0, 1.0)
    alpha = np.asarray(phase_angle_deg, dtype=np.float64)

    if kind is PhotometricModel.LAMBERT:
        value = mu0
    elif kind is PhotometricModel.LOMMEL_SEELIGER:
        # Two times the textbook Lommel-Seeliger disk function, so that it
        # agrees with lunar-Lambert at zero phase and shares the normalisation.
        value = 2.0 * mu0 / np.maximum(mu0 + mu, _TINY)
    elif kind is PhotometricModel.LUNAR_LAMBERT:
        weight = lunar_lambert_weight(alpha)
        value = 2.0 * weight * mu0 / np.maximum(mu0 + mu, _TINY) + (1.0 - weight) * mu0
    else:
        value = _hapke_raw(mu0, mu, np.radians(alpha)) * _hapke_normalisation()
    if roughness_deg > 0.0:
        # Micro-relief shadowing, applied last so that it multiplies whatever
        # law the model produced: it is a property of the *surface*, not of the
        # scattering law, and it is 1 wherever the surface faces the Sun.
        value = value * roughness_shadowing(mu0, mu, roughness_deg)
    return value


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------
def view_geometry(
    observer: Any = None,
    when: Any = None,
    earth_albedo: Optional[float] = None,
) -> MoonView:
    """Resolve the full illumination geometry of one instant.

    Parameters
    ----------
    observer
        Observing site; anything :meth:`moonlib_s.Observer.from_value` accepts.
    when
        Anything :func:`moonlib_s.to_et` accepts; defaults to now.
    earth_albedo
        Earth's geometric albedo used for the earthshine irradiance ratio.
        ``None`` or a non-positive value falls back to
        :data:`DEFAULT_EARTH_GEOMETRIC_ALBEDO`, and the reported ratio is always
        strictly proportional to whatever is used here.
    """
    site = Observer.from_value(observer)
    if when is None:
        when = utc_now()
    et = to_et(when)
    constants = kernel_session().constants

    sun_direction = direction_in_moon_frame("SUN", et)
    earth_direction = direction_in_moon_frame("EARTH", et)
    observer_direction = observer_direction_moon_frame(et, site)
    distance_km = moon_observer_range_km(et, site)

    reference_radius = MOON_REFERENCE_RADIUS_KM
    angular_diameter = 2.0 * math.degrees(math.asin(reference_radius / distance_km))

    sub_observer = vector_to_spherical(observer_direction)
    sub_solar = vector_to_spherical(sun_direction)

    phase_angle = math.degrees(angular_separation(sun_direction, observer_direction))
    illuminated = 0.5 * (1.0 + math.cos(math.radians(phase_angle)))

    earth_position = body_position_moon_frame("EARTH", et)
    earth_distance = float(np.linalg.norm(earth_position))
    earth_diameter = 2.0 * math.degrees(
        math.asin(constants.earth_radius_km / earth_distance)
    )
    # The Earth's phase angle is the Sun-Earth-Moon angle, which is *not* the
    # Moon's phase angle: it is the same triangle read from the other vertex.
    earth_phase_angle = math.degrees(
        angular_separation(
            position_from_earth("SUN", et), position_from_earth("MOON", et)
        )
    )
    earth_illuminated = 0.5 * (1.0 + math.cos(math.radians(earth_phase_angle)))

    # Irradiance ratio at the Moon: a Lambert sphere of geometric albedo p at
    # distance d delivers p * (R/d)^2 * Phi(alpha) of the incident sunlight.
    # The albedo is a *setting*, not a constant of nature: a caller that wants
    # a brighter or darker Earth (or a different published value) changes it
    # here and every consumer -- ratio, magnitude, rendered earthshine -- follows.
    try:
        albedo_earth = float(earth_albedo) if earth_albedo is not None else 0.0
    except (TypeError, ValueError):
        albedo_earth = 0.0
    if not math.isfinite(albedo_earth) or albedo_earth <= 0.0:
        albedo_earth = DEFAULT_EARTH_GEOMETRIC_ALBEDO
    phi = _lambert_sphere_phase(earth_phase_angle)
    earthshine_ratio = (
        albedo_earth
        * phi
        * (constants.earth_radius_km / earth_distance) ** 2
    )
    earthshine_magnitude = SUN_APPARENT_MAGNITUDE - 2.5 * math.log10(
        max(earthshine_ratio, 1e-30)
    )
    # 地照亮度: how bright the *night* side of the Moon is in our sky.  Two
    # differences from the line above, and both matter.  The illuminant is the
    # Earth, not the Sun, so the ratio above applies instead of a solar
    # irradiance of one; and the light comes from *our* direction, so the
    # earthshine-lit surface is seen and lit from the same place -- a phase
    # angle of zero, which is exactly the full-moon geometry whose whole-disk
    # magnitude the fit is anchored at.  The night side is only the fraction
    # ``1 - illuminated`` of the visible disk, and it is that much less light.
    earthshine_apparent = float("inf")
    night = 1.0 - illuminated
    if earthshine_ratio > 0.0 and night > 0.0:
        earthshine_apparent = (
            moon_magnitude(0.0, distance_km)
            - 2.5 * math.log10(max(earthshine_ratio, 1e-30))
            - 2.5 * math.log10(night)
        )

    moon_elevation: Optional[float]
    try:
        from .frames import az_el

        moon_elevation = az_el("MOON", et, site).elevation_deg
    except MoonlibError:  # pragma: no cover - defensive
        moon_elevation = None

    return MoonView(
        when_utc=to_utc(et),
        observer=site,
        et=et,
        distance_km=distance_km,
        angular_diameter_deg=angular_diameter,
        sub_observer_lon_deg=sub_observer.lon_deg,
        sub_observer_lat_deg=sub_observer.lat_deg,
        sub_solar_lon_deg=sub_solar.lon_deg,
        sub_solar_lat_deg=sub_solar.lat_deg,
        sun_direction=tuple(float(v) for v in sun_direction),
        earth_direction=tuple(float(v) for v in earth_direction),
        observer_direction=tuple(float(v) for v in observer_direction),
        phase_angle_deg=phase_angle,
        illuminated_fraction=illuminated,
        colongitude_deg=(90.0 - sub_solar.lon_deg) % 360.0,
        earth_distance_km=earth_distance,
        earth_angular_diameter_deg=earth_diameter,
        earth_phase_angle_deg=earth_phase_angle,
        earth_illuminated_fraction=earth_illuminated,
        earthshine_ratio=earthshine_ratio,
        earthshine_magnitude=earthshine_magnitude,
        earthshine_apparent_magnitude=earthshine_apparent,
        bright_limb_position_angle_deg=_bright_limb_angle(
            sun_direction, observer_direction
        ),
        moon_elevation_deg=moon_elevation,
    )


def _lambert_sphere_phase(phase_angle_deg: float) -> float:
    """Lambert-sphere phase function, normalised to 1 at zero phase."""
    alpha = math.radians(min(max(phase_angle_deg, 0.0), 180.0))
    return (math.sin(alpha) + (math.pi - alpha) * math.cos(alpha)) / math.pi


def _camera_basis(view: MoonView) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return ``(forward, right, up)`` for the simulated camera.

    ``up`` is the projection of the Moon's rotation axis, so the image is north
    up for any observer; ``right`` then runs towards increasing selenographic
    longitude, which is what makes the rendered disk match the real appearance
    of the near side.
    """
    observer_direction = np.asarray(view.observer_direction, dtype=np.float64)
    forward = -observer_direction
    pole = np.array([0.0, 0.0, 1.0])
    up = pole - float(np.dot(pole, forward)) * forward
    norm = float(np.linalg.norm(up))
    if norm < 1e-9:  # pragma: no cover - observer exactly over the pole
        up = np.array([0.0, 1.0, 0.0])
    else:
        up = up / norm
    right = np.cross(forward, up)
    right /= float(np.linalg.norm(right))
    return forward, right, up


def _bright_limb_angle(
    sun_direction: np.ndarray, observer_direction: np.ndarray
) -> float:
    """Position angle of the bright limb, from north towards selenographic east."""
    forward = -np.asarray(observer_direction, dtype=np.float64)
    pole = np.array([0.0, 0.0, 1.0])
    up = pole - float(np.dot(pole, forward)) * forward
    up /= float(np.linalg.norm(up))
    right = np.cross(forward, up)
    right /= float(np.linalg.norm(right))
    sun = np.asarray(sun_direction, dtype=np.float64)
    sun = sun - float(np.dot(sun, forward)) * forward
    if float(np.linalg.norm(sun)) < 1e-12:  # pragma: no cover - Sun behind Moon
        return 0.0
    return math.degrees(math.atan2(float(np.dot(sun, right)), float(np.dot(sun, up)))) % 360.0


def _spherical_from_cartesian(
    points: np.ndarray, radius: Optional[np.ndarray] = None
) -> Tuple[np.ndarray, np.ndarray]:
    """``(lon_deg, lat_deg)`` of ``(n, 3)`` Cartesian points in km.

    *radius* is the Euclidean norm of each row, when the caller already has it.
    The marches need the radius for their own step schedule, and recomputing it
    here was the single largest cost in the inner loop: ``np.linalg.norm`` over
    an ``(n, 3)`` block walks the same memory a second time.  The caller's
    ``np.sqrt(np.einsum("ij,ij->i", p, p))`` produces the identical float64
    values, so passing it changes nothing but the clock.
    """
    if radius is None:
        radius = np.sqrt(np.einsum("ij,ij->i", points, points))
    safe = np.maximum(radius, _TINY)
    lon = np.degrees(np.arctan2(points[:, 1], points[:, 0]))
    lat = np.degrees(np.arcsin(np.clip(points[:, 2] / safe, -1.0, 1.0)))
    return lon, lat


def _cone_offsets(
    direction: np.ndarray, half_angle_rad: float, count: int
) -> np.ndarray:
    """``(count, 3)`` offsets filling a cone of the given half angle.

    The offsets are area-uniform (a Fibonacci disk), so averaging a visibility
    or a direction cosine over them estimates the integral over a source disk.
    That is what turns a hard shadow edge into a penumbra and a hard earthshine
    terminator into the soft band a real extended source produces.  Returning
    *offsets* rather than finished directions lets the caller add them to a
    per-point base direction, which is how the Earth's disk is sampled.
    """
    base = np.asarray(direction, dtype=np.float64)
    base = base / max(float(np.linalg.norm(base)), _TINY)
    if count <= 1:
        return np.zeros((1, 3), dtype=np.float64)
    helper = np.array([0.0, 0.0, 1.0])
    if abs(float(np.dot(helper, base))) > 0.9:
        helper = np.array([1.0, 0.0, 0.0])
    axis_a = np.cross(base, helper)
    axis_a /= float(np.linalg.norm(axis_a))
    axis_b = np.cross(base, axis_a)

    index = np.arange(count, dtype=np.float64)
    radius = half_angle_rad * np.sqrt((index + 0.5) / count)   # equal-area rings
    theta = index * _GOLDEN_ANGLE
    return (
        (radius * np.cos(theta))[:, None] * axis_a[None, :]
        + (radius * np.sin(theta))[:, None] * axis_b[None, :]
    )


def _disk_samples(direction: np.ndarray, half_angle_rad: float, count: int) -> np.ndarray:
    """``(count, 3)`` unit directions filling a cone of the given half angle."""
    base = np.asarray(direction, dtype=np.float64)
    base = base / max(float(np.linalg.norm(base)), _TINY)
    offsets = _cone_offsets(base, half_angle_rad, count)
    if count <= 1:
        return base[None, :]
    samples = base[None, :] + offsets
    return samples / np.linalg.norm(samples, axis=1, keepdims=True)


#: How far a sky ray searches for terrain before it is counted as open sky.
#: Long enough to cross a Tycho-sized crater and meet the wall on the far side
#: (10 km), because that wall is the whole reason a crater floor is dark; a ray
#: that found nothing by then is pointed at open sky, and the few kilometres
#: beyond would only add distant massifs.  Measured against a test bowl: 20 km
#: and 60 km give identical visibility, 6 km misses the wall entirely.
SKY_HORIZON_RANGE_KM = 20.0


def _flattening_basis(up: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Two unit vectors spanning the plane perpendicular to ``up``.

    ``up`` is the radial direction at each point, so the helper axis is chosen
    per point; when it is nearly parallel to ``up`` (the poles) the cross
    product would vanish, which is why the helper is switched rather than
    fixed.
    """
    helper = np.where(
        (np.abs(up[:, 2]) < 0.9)[:, None],
        np.array([0.0, 0.0, 1.0]),
        np.array([1.0, 0.0, 0.0]),
    )
    axis_a = np.cross(up, helper)
    axis_a /= np.maximum(np.linalg.norm(axis_a, axis=1, keepdims=True), _TINY)
    axis_b = np.cross(up, axis_a)
    return axis_a, axis_b


def _sky_up(normals: np.ndarray, outward: np.ndarray) -> np.ndarray:
    """The local up a horizon is measured from: the normal in the tangent plane.

    Taking the tangential part of the normal is the right thing physically --
    the local horizon is a tangent-plane idea -- but it is numerically delicate
    on the very flat ground the mare are made of.  There the normal is radial to
    within a degree, so the tangent part is one part in a hundred of it: on the
    float32 normals the terrain hands back, that is round-off, and normalising
    it yields a *random* direction that can point below the horizon.  So the
    tangent part is only trusted when it is a real feature of the terrain; below
    that the surface is flat by the data's own standard and radially outward is
    the honest answer.
    """
    tangent = normals - np.einsum("ij,ij->i", normals, outward)[:, None] * outward
    length = np.linalg.norm(tangent, axis=1, keepdims=True)
    scale = 1.0e-3 + 1.0e-2 * np.sqrt(np.maximum(0.0, 1.0 - np.einsum(
        "ij,ij->i", normals, outward
    ) ** 2))[:, None]
    return np.where(length > scale, tangent / np.maximum(length, _TINY), outward)


#: The horizon band: ``(low, high)`` in degrees of elevation.  Terrain occludes
#: from the ground up, so this is where the answer is decided -- a crater floor
#: 100 km across has its wall only 1.7 deg up -- and a spread that starts at the
#: zenith-sample elevations resolves nothing down there.  The rest of the dome is
#: nearly never blocked and gets few samples.
_SKY_HORIZON_BAND_DEG = (0.25, 18.0)
_SKY_NEAR_HORIZON_SAMPLES = 20
_SKY_UPPER_SAMPLES = 6

#: Hard cap on the points a single band will evaluate sky visibility for.  The
#: field is smooth, so a band in a large frame is coarsened rather than marched
#: ray by ray; this is what keeps a 1600 px terminator frame in the same cost
#: bracket as the shadowing it sits beside.
_SKY_POINTS_PER_BAND = 120_000


def _hemisphere_directions(
    axis: np.ndarray, count: int, tilt: Optional[np.ndarray] = None
) -> Tuple[np.ndarray, Optional[np.ndarray]]:
    """``(count, n, 3)`` sky directions about each ``axis``, and their weights.

    *axis* is the surface's own outward direction, which is a stable thing to
    build a tangent plane from at every point.  *tilt*, when given, is the same
    surface's normal: each direction is then weighted by its cosine against the
    facet, so a grazing sample counts for less and one the facet has turned
    away from counts for nothing.  The two are kept apart on purpose -- a normal
    is a small difference of large vectors, and on flat mare it is so nearly
    radial that it defines no plane at all, which is what once produced sky
    directions pointing downwards.

    The samples are spread evenly in ``sin(elevation)``, which is the cosine
    measure the visibility integral wants, and split into a dense band just
    above the local horizon plus a sparse cap overhead.  *count*, when given,
    overrides the band's density (the cap stays as it is).
    """
    axis = np.asarray(axis, dtype=np.float64)
    axis = axis / np.maximum(np.linalg.norm(axis, axis=1, keepdims=True), _TINY)
    axis_a, axis_b = _flattening_basis(axis)
    del count  # the density is a property of the physics, not of the caller
    low = math.sin(math.radians(_SKY_HORIZON_BAND_DEG[0]))
    high = math.sin(math.radians(_SKY_HORIZON_BAND_DEG[1]))
    band = (np.arange(_SKY_NEAR_HORIZON_SAMPLES, dtype=np.float64) + 0.5) / (
        _SKY_NEAR_HORIZON_SAMPLES
    )
    height = np.concatenate([
        low + (high - low) * band,
        high + (1.0 - high) * (np.arange(_SKY_UPPER_SAMPLES, dtype=np.float64) + 0.5)
        / _SKY_UPPER_SAMPLES,
    ])
    spread = np.sqrt(np.maximum(0.0, 1.0 - height ** 2))
    theta = np.arange(height.size, dtype=np.float64) * _GOLDEN_ANGLE
    cosine = (spread * np.cos(theta))[:, None, None]
    sine = (spread * np.sin(theta))[:, None, None]
    out = (
        cosine * axis_a[None, :, :]
        + sine * axis_b[None, :, :]
        + height[:, None, None] * axis[None, :, :]
    )
    out = out / np.maximum(np.linalg.norm(out, axis=2, keepdims=True), _TINY)
    if tilt is None:
        return out, None
    weights = np.clip(
        np.einsum("kij,ij->ki", out, np.asarray(tilt, dtype=np.float64)), 0.0, 1.0
    )
    return out, weights


def _sky_visibility(
    points: np.ndarray,
    normals: np.ndarray,
    terrain: MoonTerrain,
    options: MoonRenderSettings,
    *,
    pixel_km: float,
    sky_samples: int,
    shape: Optional[Tuple[int, int]] = None,
) -> np.ndarray:
    """Fraction of the sky each point can see, ``0`` (pit) to ``1`` (plain).

    This is the quantity a global-illumination solve would need: the share of
    the hemisphere above the facet that no terrain blocks.  It is what makes a
    crater floor and the terminator-side of a ridge darker than a plain, and it
    depends only on the terrain -- never on where the Sun is.

    *shape* is the ``(rows, columns)`` grid the points are laid out on, when
    the caller knows it.  The field is smooth over hundreds of metres -- far
    wider than a pixel -- so most of it is evaluated on a coarse sub-grid and
    read back with bilinear interpolation.  Without that the cost is one sky
    march per sub-sample, which is more than the sunlight shadowing this is
    meant to sit beside.
    """
    count = points.shape[0]
    if sky_samples <= 0 or count == 0:
        return np.ones(count, dtype=np.float64)
    points = np.asarray(points, dtype=np.float64)
    normals = np.asarray(normals, dtype=np.float64)

    if shape is not None and shape[0] > 1 and shape[1] > 1 and count == shape[0] * shape[1]:
        return _sky_visibility_on_grid(
            points, normals, terrain, options,
            pixel_km=pixel_km, sky_samples=sky_samples, shape=shape,
        )

    # No grid to exploit: sample a subset and interpolate along the run.
    stride = max(1, int(math.ceil(math.sqrt(count / _SKY_POINTS_PER_BAND))))
    index = np.arange(0, count, stride)
    fraction = _sky_fraction_at(
        points[index], normals[index], terrain, options,
        pixel_km=pixel_km, sky_samples=sky_samples,
    )
    if stride == 1:
        return fraction
    return np.interp(np.arange(count), index, fraction)


def _sky_fraction_at(
    points: np.ndarray,
    normals: np.ndarray,
    terrain: MoonTerrain,
    options: MoonRenderSettings,
    *,
    pixel_km: float,
    sky_samples: int,
) -> np.ndarray:
    """The sky fraction itself: one hemisphere march per point."""
    count = points.shape[0]
    outward = points / np.maximum(np.linalg.norm(points, axis=1, keepdims=True), _TINY)
    # The horizon plane comes from the radial direction; the normal only
    # weights the samples, for the reasons given in _hemisphere_directions.
    up = _sky_up(normals, outward)

    # The hit points sit on the *reference sphere* -- the crater floor of a
    # shadowed pixel is kilometres below them -- and a march from down there
    # starts underneath the terrain it is supposed to be looking over.  Lift
    # each origin onto the surface it belongs to first.
    lon, lat = _spherical_from_cartesian(points)
    surface = np.asarray(terrain.dem.sample(lon, lat, 0), dtype=np.float64)
    origin = outward * (MOON_REFERENCE_RADIUS_KM + surface)[:, None]

    directions, weights = _hemisphere_directions(outward, int(sky_samples), tilt=up)
    visible = np.zeros(count, dtype=np.float64)
    for slot, direction in enumerate(directions):
        seen = ~_occlusion(
            origin,
            direction,
            terrain,
            steps=options.sky_steps,
            max_range_km=SKY_HORIZON_RANGE_KM,
            pixel_km=pixel_km,
            sin_elevation=np.zeros(count),
            active=np.ones(count, dtype=bool),
        )
        if weights is None:
            visible += seen
        else:
            visible += weights[slot] * seen
    if weights is None:
        return visible / max(1, len(directions))
    # The weights are cosines against the facet, so they sum to the cosine
    # integral of the whole sky: dividing by that gives the *fraction* of it
    # that is visible, which is 1 for a plain whichever way the facet leans.
    return visible / np.maximum(weights.sum(axis=0), _TINY)


def _sky_visibility_on_grid(
    points: np.ndarray,
    normals: np.ndarray,
    terrain: MoonTerrain,
    options: MoonRenderSettings,
    *,
    pixel_km: float,
    sky_samples: int,
    shape: Tuple[int, int],
) -> np.ndarray:
    """``(h, w)``-shaped sky fraction, matched back to the flat point order."""
    rows, columns = shape
    # Four times the sub-pixel grid is the sweet spot: the field is smooth, and
    # a coarser map starts to round off the crater rims it exists to describe.
    stride = max(1, int(math.ceil(math.sqrt(rows * columns / _SKY_POINTS_PER_BAND))))
    coarse_rows = len(range(0, rows, stride))
    coarse_columns = len(range(0, columns, stride))
    coarse = np.empty(coarse_rows * coarse_columns, dtype=np.float64)
    for row_slot, row in enumerate(range(0, rows, stride)):
        span = slice(row * columns, min(row + stride, rows) * columns)
        block_points = points[span].reshape(-1, columns, 3)[::stride].reshape(-1, 3)
        block_normals = normals[span].reshape(-1, columns, 3)[::stride].reshape(-1, 3)
        coarse[row_slot * coarse_columns:(row_slot + 1) * coarse_columns] = (
            _sky_fraction_at(
                block_points, block_normals, terrain, options,
                pixel_km=pixel_km, sky_samples=sky_samples,
            )
        )
    coarse = coarse.reshape(coarse_rows, coarse_columns)
    return _upsample_bilinear(coarse, rows, columns, stride)


def _upsample_bilinear(
    coarse: np.ndarray, rows: int, columns: int, stride: int
) -> np.ndarray:
    """Nearest-and-linear resample of a coarse map onto the full grid.

    Bilinear, because a stepped field would show up as visible banding on the
    terminator; the edges are held rather than extrapolated.
    """
    if stride == 1 and coarse.shape == (rows, columns):
        return coarse.reshape(-1)
    row_at = np.minimum(np.arange(rows) / stride, coarse.shape[0] - 1)
    column_at = np.minimum(np.arange(columns) / stride, coarse.shape[1] - 1)
    row_lo = np.floor(row_at).astype(np.int64)
    column_lo = np.floor(column_at).astype(np.int64)
    row_hi = np.minimum(row_lo + 1, coarse.shape[0] - 1)
    column_hi = np.minimum(column_lo + 1, coarse.shape[1] - 1)
    row_w = (row_at - row_lo)[:, None]
    column_w = (column_at - column_lo)[None, :]
    top = (
        coarse[row_lo][:, column_lo] * (1.0 - column_w)
        + coarse[row_lo][:, column_hi] * column_w
    )
    bottom = (
        coarse[row_hi][:, column_lo] * (1.0 - column_w)
        + coarse[row_hi][:, column_hi] * column_w
    )
    return (top * (1.0 - row_w) + bottom * row_w).reshape(-1)


# ---------------------------------------------------------------------------
# ray marching
# ---------------------------------------------------------------------------
def _finest_value_range(dem: Any) -> Optional[Tuple[float, float]]:
    """``(min, max)`` of level 0 in *stored* units, or ``None``.

    Two ways to get it, and the difference is minutes.  A baked pyramid records
    the range in its manifest, which is a dictionary lookup; failing that the
    range has to be reduced out of the memory map -- and the reduction must not
    go through ``np.asarray(level, dtype=np.float64)``, which on the USGS
    256 px/deg product is an 8.5 GB allocation for a 2.1 GB raster.  ``np.min``
    and ``np.max`` read the map in place.
    """
    recorded = getattr(dem, "value_range", None)
    if recorded is not None:
        return (float(recorded[0]), float(recorded[1]))
    try:
        level = dem.level(0)
        return (float(np.min(level)), float(np.max(level)))
    except Exception:  # pragma: no cover - defensive
        return None


def _height_range(terrain: MoonTerrain) -> Tuple[float, float]:
    """Cached global ``(min, max)`` elevation of the finest pyramid level (km).

    The bake records level 0's range in the cache manifest, so this is normally
    a dictionary lookup.  Measuring it instead means reading every sample of
    level 0: 0.4 s for the SVS 64 px/deg raster, and about two minutes for the
    USGS 256 px/deg one, which is long enough to look like a hang and happens
    once per process because the cache below lives on the object.
    """
    cached = getattr(terrain, "_height_range_cache", None)
    if cached is not None:
        return cached
    span = _finest_value_range(terrain.dem)
    if span is None:  # pragma: no cover - defensive
        span = (0.0, 0.0)
    lower = span[0] * terrain.dem.scale + terrain.dem.offset
    upper = span[1] * terrain.dem.scale + terrain.dem.offset
    result = (lower - 0.1, upper + 0.1)
    try:
        terrain._height_range_cache = result  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - defensive
        pass
    return result


def _shell_window(
    origin: np.ndarray,
    directions: np.ndarray,
    r_outer: float,
    r_inner: float,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Entry/exit times of each ray through the terrain shell.

    The window deliberately stops where a ray reaches ``r_inner`` rather than
    running on to the far side of the Moon: only the *first* crossing matters,
    and using the full chord would stretch the step over thousands of
    kilometres and step straight through the surface.
    """
    distance = float(np.linalg.norm(origin))
    b = np.einsum("ij,j->i", directions, origin)
    outer_disc = b * b - (distance * distance - r_outer * r_outer)
    alive = outer_disc > 0.0
    root = np.sqrt(np.maximum(outer_disc, 0.0))
    t_enter = np.where(alive, -b - root, 0.0)
    t_exit = np.where(alive, -b + root, 0.0)
    t_enter = np.maximum(t_enter, 0.0)

    inner_disc = b * b - (distance * distance - r_inner * r_inner)
    reaches_inner = inner_disc > 0.0
    t_inner = np.where(
        reaches_inner, -b - np.sqrt(np.maximum(inner_disc, 0.0)), np.inf
    )
    t_end = np.where(reaches_inner, np.minimum(t_exit, t_inner), t_exit)
    return alive, t_enter, np.maximum(t_end, t_enter)


def _march_view_rays(
    origin: np.ndarray,
    directions: np.ndarray,
    terrain: MoonTerrain,
    level: int,
    settings: MoonRenderSettings,
    ground_km: float,
    *,
    steps: Optional[int] = None,
    cancel: Optional[Any] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """Find where each ray first meets the displaced surface.

    Returns ``(hit, points)``; rays that leave the shell without ever dipping
    below the height field stay marked as misses, which is what produces the
    true topographic limb silhouette.

    The working arrays are compacted whenever more than half of the surviving
    rays have finished, and everything that did hit is flushed into result
    buffers first.  Without that, a thin annulus of rays that graze the shell
    almost tangentially -- they need two orders of magnitude more steps than
    the rest of the disk -- would set the step budget for every ray.
    """
    reference = MOON_REFERENCE_RADIUS_KM
    low, high = _height_range(terrain)
    r_outer = reference + high
    r_inner = reference + low

    alive, t_enter, t_end = _shell_window(origin, directions, r_outer, r_inner)
    total = directions.shape[0]
    hit = np.zeros(total, dtype=bool)
    hit_t = np.zeros(total, dtype=np.float64)
    if not alive.any():
        return hit, origin[None, :] + hit_t[:, None] * directions

    index = np.flatnonzero(alive)
    # Advance every ray by exactly one terrain pixel per step.  Stepping in
    # equal *fractions* of the shell crossing instead would force every ray to
    # spend as many steps as the most grazing one needs, which is roughly eight
    # times the work for the same answer.
    step_target = max(terrain.dem.km_per_pixel(level), 1e-4)
    if steps is None:
        # Standalone use: derive the budget from this block alone.
        needed = np.ceil((t_end[index] - t_enter[index]) / step_target)
        steps = int(
            np.clip(
                max(float(np.max(needed)), settings.ray_steps),
                settings.ray_steps_min,
                settings.ray_steps_max,
            )
        )
    steps = int(steps)

    maps = index
    dirs_c = directions[index]
    base_c = t_enter[index]
    t_lo_c = base_c.copy()
    t_hi_c = np.zeros(index.size, dtype=np.float64)
    hit_c = np.zeros(index.size, dtype=bool)
    live = np.ones(index.size, dtype=bool)
    done_maps: List[np.ndarray] = []
    done_lo: List[np.ndarray] = []
    done_hi: List[np.ndarray] = []

    for step in range(steps):
        # 终止 lands on a step boundary, not on a band boundary: at the finest
        # scales one band is tens of seconds of marching, and a hook polled
        # only between bands let the server's handover window expire while the
        # frame was still pinning the CPU.  A bool read against a numpy block
        # over millions of rays costs nothing measurable.
        _check_cancelled(cancel)
        count = int(np.count_nonzero(live))
        if count == 0:
            break
        if count * 2 < live.size:
            if hit_c.any():
                done_maps.append(maps[hit_c])
                done_lo.append(t_lo_c[hit_c])
                done_hi.append(t_hi_c[hit_c])
            dirs_c = dirs_c[live]
            base_c = base_c[live]
            t_lo_c = t_lo_c[live]
            t_hi_c = t_hi_c[live]
            hit_c = hit_c[live]
            maps = maps[live]
            live = np.ones(maps.size, dtype=bool)
        t = base_c + (step + 0.5) * step_target
        points = origin[None, :] + t[:, None] * dirs_c
        radius = np.sqrt(np.einsum("ij,ij->i", points, points))
        lon, lat = _spherical_from_cartesian(points, radius)
        inside = radius < reference + terrain.dem.sample(lon, lat, level)
        if inside.any():
            hit_c |= inside
            t_hi_c = np.where(inside, t, t_hi_c)
            live &= ~inside
        t_lo_c = np.where(live, t, t_lo_c)
        live &= radius > r_inner

    if hit_c.any():
        done_maps.append(maps[hit_c])
        done_lo.append(t_lo_c[hit_c])
        done_hi.append(t_hi_c[hit_c])

    if done_maps:
        struck = np.concatenate(done_maps)
        lo = np.concatenate(done_lo)
        hi = np.concatenate(done_hi)
        sub_dirs = directions[struck]
        for _ in range(max(1, settings.bisection_steps)):
            mid = 0.5 * (lo + hi)
            points = origin[None, :] + mid[:, None] * sub_dirs
            radius = np.sqrt(np.einsum("ij,ij->i", points, points))
            lon, lat = _spherical_from_cartesian(points, radius)
            inside = radius < reference + terrain.dem.sample(lon, lat, level)
            lo = np.where(inside, lo, mid)
            hi = np.where(inside, mid, hi)
        hit[struck] = True
        hit_t[struck] = 0.5 * (lo + hi)

    points = origin[None, :] + hit_t[:, None] * directions
    return hit, points


#: Where the computed ceiling is kept on the raster itself.  A raster's levels
#: never change, so the answer is a property of the *object* -- and caching it
#: there is what makes it impossible for a freed raster's answer to be handed to
#: a different one.
_CEILING_ATTRIBUTE = "_ceiling_km"
_CEILING_UNSET = object()


def _dem_ceiling_km(dem: Any) -> Optional[float]:
    """Highest terrain height in the raster, plus a margin, cached per DEM.

    Computed from the finest level, which is the only one that bounds the rest:
    a coarser level is an average, so its maximum is *lower* and would allow the
    early-out to skip a step that could still be blocked.

    The cache lives on the raster rather than in a module-level dictionary
    keyed by ``id()``.  An address is unique only among *live* objects: CPython
    hands a freed object's block to the next object of the same size, so an
    address-keyed cache quietly answers for whatever raster now occupies it --
    a close-up window's ceiling standing in for the global pyramid's, or the
    reverse.  The early-out in :func:`_occlusion` then drops steps that would
    have found a shadow, the frame changes, and it changes again on the next
    run once the allocation order differs.  That is the whole reason the
    bit-exactness tests could fail under load and never on their own.
    """
    cached = getattr(dem, _CEILING_ATTRIBUTE, _CEILING_UNSET)
    if cached is not _CEILING_UNSET:
        return cached
    value: Optional[float] = None
    span = _finest_value_range(dem)
    if span is not None:
        tallest = span[1] * float(getattr(dem, "scale", 1.0)) \
            + float(getattr(dem, "offset", 0.0))
        if np.isfinite(tallest):
            value = MOON_REFERENCE_RADIUS_KM + tallest + 0.1
    try:
        setattr(dem, _CEILING_ATTRIBUTE, value)
    except (AttributeError, TypeError):  # pragma: no cover - a slotted stand-in
        pass
    return value


def _occlusion(
    points: np.ndarray,
    directions: np.ndarray,
    terrain: MoonTerrain,
    *,
    steps: int,
    max_range_km: float,
    pixel_km: float,
    sin_elevation: np.ndarray,
    active: np.ndarray,
    cancel: Optional[Any] = None,
) -> np.ndarray:
    """March from surface *points* along *directions*; True where blocked.

    *directions* may be a single direction broadcast over every point (the Sun)
    or one direction per point (the Earth).  Steps grow geometrically, so the
    near field keeps pixel-scale resolution while a 200 km graze is still
    covered in a few dozen samples, and each sample reads the pyramid level that
    matches the spacing between samples -- the scale the march actually
    resolves -- rather than its distance from the ray's origin.
    """
    count = points.shape[0]
    blocked = np.zeros(count, dtype=bool)
    if steps < 2:
        return blocked
    subset = np.flatnonzero(active)
    if subset.size == 0:
        return blocked

    here = points[subset]
    along = directions if directions.ndim == 1 else directions[subset]
    del sin_elevation  # kept in the signature for every existing caller
    # The geometric schedule of the versions before 1.3.0.  A rise-limited
    # schedule was tried in 1.3.0 and again in 1.3.2: it made the far field of
    # a grazing shadow smoother, but it also blunted the tips of long shadows
    # (a 4.5 deg Sun at Sinus Iridum was enough to see it), and real photographs
    # kept agreeing with the older march.  Keep the old spacing; the polygon
    # bug was in the *level* choice, not in the step schedule, and that is
    # fixed below.
    first = float(np.clip(pixel_km, 1e-3, 0.4 * max_range_km))
    sample_count = max(2, min(int(steps), _MAX_SHADOW_SAMPLES))
    growth = (max_range_km / first) ** (1.0 / float(sample_count - 1))
    distances = [first * growth ** step for step in range(sample_count)]
    reference = MOON_REFERENCE_RADIUS_KM
    # The pyramid level has to match the *distance between samples* -- the
    # scale at which the ray actually queries the terrain -- not the distance
    # from where the ray started.  Choosing by distance was the 1.3.0
    # regression: a sample 100 km out read a level tens of kilometres wide, so
    # the far half of a shadow was carved from a handful of bilinear facets and
    # long shadow boundaries became polygons (worst at samples_per_pixel=1,
    # where the level floor is already the coarser one).
    levels: List[int] = []
    for index, distance in enumerate(distances):
        if index + 1 < len(distances):
            spacing = distances[index + 1] - distance
        elif index:
            spacing = distance - distances[index - 1]
        else:
            spacing = first
        levels.append(
            terrain.dem.level_for_ground_scale(max(spacing, first))
        )
    ceiling = _dem_ceiling_km(terrain.dem)
    remaining = np.ones(subset.size, dtype=bool)
    for step, distance in enumerate(distances):
        _check_cancelled(cancel)
        sample_points = here + distance * along
        # One norm per step, shared by the ceiling test and the conversion:
        # np.linalg.norm walked the block a second time inside the conversion,
        # and at a few hundred steps per ray that was the shadow pass's largest
        # single cost after the arctangent that computes the angles themselves.
        radius = np.sqrt(np.einsum("ij,ij->i", sample_points, sample_points))
        if ceiling is not None:
            # A ray that is already above every terrain height and still moving
            # away from the centre cannot hit anything further out: the rest of
            # its steps are skipped.  Unknown terrain (NaN) never blocks, which
            # is the same rule the samples below apply by comparison.
            # along is one direction broadcast over every point (the Sun) or
            # one per point (the Earth), so the dot product has two shapes.
            outward = ((sample_points.dot(along) if along.ndim == 1 else
                        np.einsum("ij,ij->i", sample_points, along)) > 0.0)
            remaining &= ~((radius >= ceiling) & outward)
        lon, lat = _spherical_from_cartesian(sample_points, radius)
        surface = reference + terrain.dem.sample(lon, lat, levels[step]) - _SHADOW_BIAS_KM
        inside = remaining & (radius < surface)
        if inside.any():
            blocked[subset[inside]] = True
            remaining &= ~inside
            if not remaining.any():
                break
    return blocked


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def _estimate_ray_steps(
    origin: np.ndarray,
    forward: np.ndarray,
    right: np.ndarray,
    up: np.ndarray,
    columns: np.ndarray,
    rows: np.ndarray,
    sample_scale: float,
    terrain: MoonTerrain,
    level: int,
    settings: MoonRenderSettings,
) -> int:
    """Choose the shell-marching step budget for the whole frame at once.

    The budget comes from the 99.5th percentile of how many steps each ray
    would need, so it is driven by the grazing rays near the limb.  It has to
    be computed over the *entire* frame rather than per block, or bands would
    be sampled differently and faint horizontal seams would appear.  A strided
    sample of the same camera is representative and costs almost nothing.
    """
    stride = max(1, int(math.ceil(max(columns.size, rows.size) / 192.0)))
    sample_columns = columns[::stride]
    sample_rows = rows[::stride]
    if sample_columns.size < 2 or sample_rows.size < 2:
        return settings.ray_steps
    directions = (
        forward[None, None, :]
        + (sample_columns[None, :] * sample_scale)[..., None] * right[None, None, :]
        + (sample_rows[:, None] * sample_scale)[..., None] * up[None, None, :]
    )
    directions /= np.linalg.norm(directions, axis=-1, keepdims=True)
    flat = directions.reshape(-1, 3)

    reference = MOON_REFERENCE_RADIUS_KM
    low, high = _height_range(terrain)
    alive, t_enter, t_end = _shell_window(
        origin, flat, reference + high, reference + low
    )
    if not alive.any():
        return settings.ray_steps
    step_target = max(terrain.dem.km_per_pixel(level), 1e-4)
    needed = np.ceil((t_end[alive] - t_enter[alive]) / step_target)
    return int(
        np.clip(
            max(float(np.percentile(needed, 99.5)), settings.ray_steps),
            settings.ray_steps_min,
            settings.ray_steps_max,
        )
    )


def _band_workers(band_count: int, requested: int = 0) -> int:
    """How many bands to trace at once.

    Chosen from the machine, capped so that small renders do not pay thread
    startup for nothing, and overridable with ``MOONLIB_BAND_WORKERS`` (set it
    to 1 to force the serial path, which is also what the tests use).

    *requested* is the caller's own ceiling -- the page's 并行数 control.  A
    positive value wins over the environment, because it is a statement about
    this render rather than about the process; ``0`` keeps the old decision.
    """
    if band_count <= 1:
        return 1
    if requested and requested > 0:
        return max(1, min(int(requested), band_count))
    override = os.environ.get("MOONLIB_BAND_WORKERS")
    if override:
        try:
            return max(1, min(int(override), band_count))
        except ValueError:
            raise MoonlibError(
                f"MOONLIB_BAND_WORKERS={override!r} is not an integer."
            ) from None
    available = os.cpu_count() or 1
    if available <= 1:
        return 1
    return max(1, min(available, band_count, _MAX_BAND_WORKERS))


def _band_rows(
    fine_width: int, fine_height: int, samples: int, target_bands: int = _TARGET_BANDS
) -> int:
    """Rows per band: near :data:`_RAYS_PER_BAND`, and enough bands to share.

    Always a whole number of *output* rows, so that each band's supersampled
    hits can be reduced to output pixels without straddling a boundary.

    A band is purely a unit of memory and of scheduling: every constant the
    shading uses is computed frame-wide before the loop, so the boundaries do
    not affect the result beyond floating-point reduction order.  The count is
    fixed at :data:`_TARGET_BANDS` rather than tied to the thread count, so that
    a render is reproducible across machines.
    """
    if fine_width <= 0:  # pragma: no cover - defensive
        return max(1, fine_height)
    rows = int(_RAYS_PER_BAND // max(fine_width, 1))
    samples = max(1, int(samples))
    rows = max(1, rows // samples) * samples
    if target_bands > 1:
        share = int(math.ceil(fine_height / float(target_bands)))
        share = max(1, share // samples) * samples
        rows = min(rows, max(samples, share))
    rows = max(samples, min(rows, fine_height))
    if rows * fine_width > _MAX_RAYS:  # pragma: no cover - absurd width
        raise MoonlibError(
            f"A single row of {fine_width} rays exceeds the {_MAX_RAYS} ray "
            "band budget; reduce the image width or samples_per_pixel."
        )
    return rows


def _shade_rays(
    origin: np.ndarray,
    flat: np.ndarray,
    terrain: MoonTerrain,
    view: MoonView,
    options: MoonRenderSettings,
    *,
    level: int,
    albedo_level: int,
    ground_km: float,
    sample_km: float,
    steps: int,
    sky_shape: Optional[Tuple[int, int]] = None,
    cancel: Optional[Any] = None,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, Dict[str, Any]]:
    """Trace and shade one block of rays.

    Returns ``(sunlit, earthshine, hit, block_stats)`` where the two radiance
    arrays are ``(n, 3)`` float32 radiance factors in ``I/F``.  Kept separate
    from the renderers so that a large frame can be produced one band at a time
    without holding every ray in memory at once.

    *sky_shape* is the ``(rows, columns)`` layout of ``flat``, when the caller
    is a renderer that lays its band out on a grid; it is what lets the sky
    visibility be evaluated coarsely and read back bilinearly.
    """
    del ground_km  # shadows march at the sub-sample footprint, not the pixel
    hit, points = _march_view_rays(
        origin, flat, terrain, level, options, sample_km, steps=steps,
        cancel=cancel,
    )
    sunlit = np.zeros((flat.shape[0], 3), dtype=np.float32)
    earthlit = np.zeros((flat.shape[0], 3), dtype=np.float32)
    block: Dict[str, Any] = {
        "hits": int(hit.sum()),
        "sum_sunlit": 0.0,
        "peak_sunlit": 0.0,
        "sum_earthshine": 0.0,
        "peak_earthshine": 0.0,
        "sunlit_fraction": 0.0,
        "shadowed_fraction": 0.0,
        "terminator_pixels": 0,
    }
    if not hit.any():
        return sunlit, earthlit, hit, block

    sun = np.asarray(view.sun_direction, dtype=np.float64)
    hit_points = points[hit]
    lon, lat = _spherical_from_cartesian(hit_points)
    normals = np.asarray(terrain.normals(lon, lat, level), dtype=np.float64)
    albedo = terrain.albedo_rgb(lon, lat, albedo_level)
    if albedo is None:
        block["albedo_source"] = "flat 0.5 (no colour product)"
        albedo = np.full((hit_points.shape[0], 3), 0.5, dtype=np.float64)
    else:
        block["albedo_source"] = (
            terrain.sources.albedo_path.name if terrain.sources.albedo_path else "none"
        )
        # Relative mosaic -> normal albedo, using the mosaic's own global mean
        # so that normal_albedo means the disk average.
        reference_mean = float(getattr(terrain.albedo, "relative_mean", 0.0) or 0.0)
        albedo = np.asarray(albedo, dtype=np.float64)
        if reference_mean > 0.0:
            albedo = albedo / reference_mean
        else:  # pragma: no cover - a cache without the summary level
            albedo = albedo / max(float(np.mean(albedo)), _TINY)
        block["albedo_relative_mean"] = reference_mean
    albedo = albedo * options.normal_albedo

    view_direction = flat[hit]
    mu = np.clip(np.einsum("ij,ij->i", normals, -view_direction), 0.0, 1.0)

    # --- direct sunlight -----------------------------------------------
    mu0 = np.einsum("ij,j->i", normals, sun)
    local_up = hit_points / np.maximum(
        np.linalg.norm(hit_points, axis=1, keepdims=True), _TINY
    )
    sin_elevation = np.einsum("ij,j->i", local_up, sun)
    lit = mu0 > 0.0

    shadow = np.where(lit, 1.0, 0.0)
    threshold = math.sin(math.radians(options.shadow_skip_elevation_deg))
    needs_shadow = lit & (sin_elevation < threshold)
    if needs_shadow.any() and options.shadow_steps >= 2:
        sun_samples = _disk_samples(
            sun, math.radians(SOLAR_ANGULAR_RADIUS_DEG), options.sun_samples
        )
        visible = np.zeros(hit_points.shape[0], dtype=np.float64)
        for sample in sun_samples:
            blocked = _occlusion(
                hit_points,
                sample,
                terrain,
                steps=options.shadow_steps,
                max_range_km=options.shadow_range_km,
                pixel_km=sample_km,
                sin_elevation=sin_elevation,
                active=needs_shadow,
                cancel=cancel,
            )
            visible += np.where(blocked, 0.0, 1.0)
        shadow = np.where(needs_shadow, visible / len(sun_samples), shadow)
        shadow = np.where(lit, shadow, 0.0)

    # The local phase angle follows the actual ray, so it sweeps about a degree
    # across the disk instead of being one global number.
    cos_phase = np.clip(np.einsum("ij,j->i", -view_direction, sun), -1.0, 1.0)
    disk_sun = disk_function(
        options.photometric_model,
        mu0,
        mu,
        np.degrees(np.arccos(cos_phase)),
        roughness_deg=options.roughness_deg,
    )
    direct = albedo * (disk_sun * shadow)[:, None]
    if options.ambient_fraction > 0.0:
        # Empirical fill standing in for light scattered off neighbouring
        # illuminated terrain; not a global-illumination solve.  Superseded by
        # `sky_ambient` below, kept because a stored .conf may still ask for it.
        direct = direct + (
            options.ambient_fraction
            * albedo
            * disk_sun[:, None]
            * (1.0 - shadow)[:, None]
        )
    if options.sky_ambient > 0.0 and options.sky_samples > 0 and options.shadow_steps >= 2:
        # Sky light is *not* reflected sunlight: it arrives from the whole
        # hemisphere and never from the Sun's direction, so it is added where
        # the direct term was shadowed out rather than scaled by the geometry
        # of a Sun that is not there.  The visibility is the terrain's own, so
        # a crater floor and the far side of a ridge are darker than a plain.
        in_shadow = lit & (shadow <= 0.0)
        if in_shadow.any():
            sky = np.zeros(hit_points.shape[0], dtype=np.float64)
            sky[in_shadow] = _sky_visibility(
                hit_points[in_shadow],
                normals[in_shadow],
                terrain,
                options,
                pixel_km=sample_km,
                sky_samples=int(options.sky_samples),
                shape=sky_shape,
            )
            block["sky_visibility_mean"] = float(np.mean(sky[in_shadow]))
            block["sky_visibility_min"] = float(np.min(sky[in_shadow]))
            block["sky_ambient_pixels"] = int(np.count_nonzero(in_shadow))
            direct = direct + options.sky_ambient * albedo * sky[:, None]
    sunlit[hit] = direct.astype(np.float32)

    # --- earthshine -----------------------------------------------------
    if options.earthshine and view.earthshine_ratio > 0.0:
        earthlit[hit] = _earthshine_radiance(
            view,
            hit_points,
            normals,
            mu,
            view_direction,
            albedo,
            terrain,
            options,
            steps=options.shadow_steps,
            pixel_km=sample_km,
        ).astype(np.float32)

    block.update(
        sunlit_fraction=float(np.mean(shadow > 0.5)),
        shadowed_fraction=float(np.mean(shadow < 0.5)),
        terminator_pixels=int(np.count_nonzero((mu0 > 0.0) & (mu0 < 0.15))),
        sum_sunlit=float(np.sum(sunlit[hit])),
        peak_sunlit=float(np.max(sunlit[hit])),
        sum_earthshine=float(np.sum(earthlit[hit])),
        peak_earthshine=float(np.max(earthlit[hit])),
    )
    return sunlit, earthlit, hit, block


def _check_cancelled(cancel: Optional[Any]) -> None:
    """Raise if the caller has gone away.

    A frame can take a minute: without this, a reader who presses 终止 (or
    closes the tab) leaves the server marching a frame nobody will ever see,
    and the next press starts a second one beside it.

    Polled on every step of both marching loops -- the view-ray march and the
    shadow march -- so 终止 lands within a step.  Polling only *between bands*
    was not enough: at the finest scales a band is tens of seconds of marching,
    so the server's 20 s handover window expired first and told the reader to
    restart the service while the abandoned frame was still pinning the CPU.
    The check is a bool read against a numpy block over millions of rays.
    """
    if cancel is not None and cancel():
        raise MoonlibError("渲染已取消。")


def _render_bands(
    origin: np.ndarray,
    terrain: MoonTerrain,
    view: MoonView,
    options: MoonRenderSettings,
    *,
    fine_width: int,
    fine_height: int,
    samples: int,
    band_rows: int,
    band_directions: Any,
    ground_km: float,
    sample_km: float,
    level: int,
    albedo_level: int,
    steps: int,
    cancel: Optional[Any] = None,
) -> Tuple[np.ndarray, np.ndarray, Dict[str, Any]]:
    """Trace and shade a whole frame, one band of rows at a time.

    *band_directions* is called with ``(row_start, row_stop)`` and returns the
    unit view directions for that band, so the whole-disk camera and a targeted
    close-up camera share every line of the shading pipeline.

    Returns ``(sunlit, earthshine, stats)`` with the two radiance fields at
    *output* resolution, already box-averaged over the sub-samples.
    """
    sunlit = np.empty((fine_height * fine_width, 3), dtype=np.float32)
    earthlit = np.empty((fine_height * fine_width, 3), dtype=np.float32)
    disk_mask = np.zeros((fine_height // samples, fine_width // samples), dtype=bool)

    stats: Dict[str, Any] = {
        "pyramid_level": int(level),
        "albedo_level": int(albedo_level),
        "ground_sample_km": float(ground_km),
        "sample_km": float(sample_km),
        "samples_per_pixel": int(samples),
        "terrain_km_per_pixel": float(terrain.dem.km_per_pixel(level)),
        "steps_budget": int(steps),
        "bands": 0,
        "sun_samples": int(options.sun_samples),
        "earth_samples": int(options.earth_samples) if options.earthshine else 0,
        "photometric_model": as_photometric_model(options.photometric_model).value,
        "tone": as_tone_mapping(options.tone).value,
        "normal_albedo": float(options.normal_albedo),
        "roughness_deg": float(options.roughness_deg),
        "sky_ambient": float(options.sky_ambient),
        "sky_samples": int(options.sky_samples) if options.sky_ambient > 0.0 else 0,
    }

    hits = 0
    sums = {"sunlit": 0.0, "earthshine": 0.0}
    peaks = {"sunlit": 0.0, "earthshine": 0.0}
    lit_pixels = 0.0
    shadowed_pixels = 0.0
    terminator_pixels = 0
    sky_pixels = 0
    sky_sum = 0.0
    sky_low = None

    def trace(row_start: int, row_stop: int):
        """Trace one band and return its result; pure, so it can run anywhere."""
        band = band_directions(row_start, row_stop)
        block_sunlit, block_earthlit, hit, block = _shade_rays(
            origin,
            band.reshape(-1, 3),
            terrain,
            view,
            options,
            level=level,
            albedo_level=albedo_level,
            ground_km=ground_km,
            sample_km=sample_km,
            steps=steps,
            sky_shape=((row_stop - row_start), fine_width),
            cancel=cancel,
        )
        mask = None
        if block["hits"]:
            mask = hit.reshape(
                (row_stop - row_start) // samples,
                samples,
                fine_width // samples,
                samples,
            ).any(axis=(1, 3))
        return row_start, row_stop, block_sunlit, block_earthlit, block, mask

    bands = [
        (start, min(start + band_rows, fine_height))
        for start in range(0, fine_height, band_rows)
    ]
    workers = _band_workers(len(bands), int(options.band_workers or 0))
    if workers > 1:
        # Bands are independent by construction -- every constant the shading
        # uses is computed frame-wide before the loop -- so they can be traced
        # in any order.  Threads rather than processes: numpy drops the GIL for
        # the array arithmetic that dominates here, while a process pool would
        # have to copy the terrain to every worker.
        from concurrent.futures import ThreadPoolExecutor

        pool = ThreadPoolExecutor(max_workers=workers)
        results = []
        try:
            # Consumed as they finish rather than in one `list()` so that a
            # cancellation can stop the loop: `cancel_futures` below then drops
            # the bands that have not started yet, and at most the bands already
            # running have to finish.
            for result in pool.map(lambda span: trace(*span), bands):
                results.append(result)
                _check_cancelled(cancel)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
    else:
        results = []
        for span in bands:
            results.append(trace(*span))
            _check_cancelled(cancel)

    for row_start, row_stop, block_sunlit, block_earthlit, block, mask in results:
        span = slice(row_start * fine_width, row_stop * fine_width)
        sunlit[span] = block_sunlit
        earthlit[span] = block_earthlit
        if block["hits"]:
            if mask is not None:
                disk_mask[row_start // samples: row_stop // samples, :] |= mask
            hits += block["hits"]
            sums["sunlit"] += block["sum_sunlit"]
            sums["earthshine"] += block["sum_earthshine"]
            peaks["sunlit"] = max(peaks["sunlit"], block["peak_sunlit"])
            peaks["earthshine"] = max(peaks["earthshine"], block["peak_earthshine"])
            lit_pixels += block["sunlit_fraction"] * block["hits"]
            shadowed_pixels += block["shadowed_fraction"] * block["hits"]
            terminator_pixels += block["terminator_pixels"]
            if block.get("sky_ambient_pixels"):
                sky_pixels += int(block["sky_ambient_pixels"])
                sky_sum += block["sky_visibility_mean"] * block["sky_ambient_pixels"]
                lowest = block["sky_visibility_min"]
                sky_low = lowest if sky_low is None else min(sky_low, lowest)
            stats.setdefault("albedo_source", block.get("albedo_source", "none"))
            if "albedo_relative_mean" in block:
                stats["albedo_relative_mean"] = block["albedo_relative_mean"]
        stats["bands"] += 1
    stats["band_workers"] = int(workers)
    del results

    if samples > 1:
        sunlit = sunlit.reshape(
            fine_height // samples, samples, fine_width // samples, samples, 3
        ).mean(axis=(1, 3))
        earthlit = earthlit.reshape(
            fine_height // samples, samples, fine_width // samples, samples, 3
        ).mean(axis=(1, 3))
    shape = (fine_height // samples, fine_width // samples)
    sunlit_image = sunlit.reshape(shape + (3,))
    earthlit_image = earthlit.reshape(shape + (3,))

    stats["frames_of_rays"] = int(fine_width * fine_height)
    stats["hits"] = int(hits)
    stats["disk_pixels"] = int(disk_mask.sum())
    stats["disk_fraction_of_frame"] = (
        stats["disk_pixels"] / float(shape[0] * shape[1]) if shape[0] and shape[1] else 0.0
    )
    stats["albedo_relative_mean"] = stats.get("albedo_relative_mean", 0.0)
    if sky_pixels:
        stats["sky_visibility_mean"] = sky_sum / sky_pixels
        stats["sky_visibility_min"] = float(sky_low if sky_low is not None else 1.0)
        stats["sky_ambient_pixels"] = int(sky_pixels)
    if hits:
        stats["sunlit_fraction_of_disk"] = lit_pixels / hits
        stats["shadowed_fraction_of_disk"] = shadowed_pixels / hits
        stats["terminator_pixels"] = int(terminator_pixels)
        stats["mean_sunlit_radiance"] = sums["sunlit"] / (hits * 3)
        stats["peak_sunlit_radiance"] = peaks["sunlit"]
        stats["mean_earthshine_radiance"] = sums["earthshine"] / (hits * 3)
        stats["peak_earthshine_radiance"] = peaks["earthshine"]
    else:  # pragma: no cover - only if the target is off frame
        stats.update(
            sunlit_fraction_of_disk=0.0,
            shadowed_fraction_of_disk=0.0,
            terminator_pixels=0,
            mean_sunlit_radiance=0.0,
            peak_sunlit_radiance=0.0,
            mean_earthshine_radiance=0.0,
            peak_earthshine_radiance=0.0,
        )
    ratio = 0.0
    if stats["peak_sunlit_radiance"] > 0.0:
        ratio = stats["peak_earthshine_radiance"] / stats["peak_sunlit_radiance"]
    stats["earthshine_to_sunlit_ratio"] = float(ratio)
    if ratio > 0.0:
        stats["earthshine_magnitudes_below_sunlit"] = float(-2.5 * math.log10(ratio))
    return sunlit_image, earthlit_image, stats


#: The fraction of the lit pixels ``"top_mean"`` averages over.
_TOP_MEAN_FRACTION = 0.01

#: How far one frame of a *run* may move the exposure, in magnitudes.
#:
#: A sequence of frames is metered one frame at a time, and any per-frame
#: statistic -- the 99.9th percentile, the mean of the brightest 1%, the peak --
#: eventually steps: near new Moon the sunlit part of the frame is a couple of
#: pixels, so "the brightest pixel" is measured from almost nothing and swings
#: by a factor of two between two frames twelve hours apart.  A still may do
#: that; a film may not, because the whole picture brightens and darkens with
#: it and the shadow on the ground appears to jump *back*.
#:
#: So a caller that renders a run hands each frame the anchor the last one used
#: (``exposure_hint``) and gets back an anchor no further than this from it.
#: Measured on a 30-day whole-disk run at twelve-hour steps, the worst
#: single-frame move falls from +94% to this limit, and on a close-up that
#: crosses into lunar night from +16891% -- the frame where the sunlit peak
#: first loses to the earthshine -- to the same.
#:
#: The price is a lag: a scene that really does change faster than this is
#: followed at this speed, which is what a time-lapse photographer's own hand
#: does on the exposure ring.  It is a *slew* limit and not a lock, so a run
#: still tracks a lunation -- five days of gibbous needs 18.6%, or 0.9% a frame.
EXPOSURE_SLEW_MAG = 0.25


def _exposure_anchor(positive: np.ndarray, meter: str) -> float:
    """The radiance the auto-exposure is metered on.

    ``"percentile"`` (the default) is a *rank* statistic, and a rank statistic
    steps: on a waxing gibbous the 99.9th percentile walked backwards four times
    by up to 1% while the scene only brightened, and an animation metered that
    way flickers.  ``"top_mean"`` averages the brightest 1% of the lit pixels
    instead -- 2-12% off the percentile's level on every scene measured, and
    with a worst backwards step of 0.02% over the same run.  See
    :attr:`moonlib_s.models.MoonRenderSettings.exposure_meter`.
    """
    if str(meter) == "top_mean":
        ordered = np.sort(positive)[::-1]
        count = max(1, int(round(_TOP_MEAN_FRACTION * ordered.size)))
        return float(ordered[:count].mean())
    return float(np.percentile(positive, 99.9))


def _finish_render(
    view: MoonView,
    options: MoonRenderSettings,
    sunlit_image: np.ndarray,
    earthlit_image: np.ndarray,
    stats: Dict[str, Any],
    *,
    region: Optional["MoonRegion"] = None,
    exposure_hint: Optional[float] = None,
) -> MoonRender:
    """Expose, tone-map and package a traced frame.

    *exposure_hint* is the anchor the previous frame of a *run* used; the anchor
    this frame measured is pulled back to within :data:`EXPOSURE_SLEW_MAG` of it
    so that a sequence cannot jump.  ``None`` -- a still -- is unmetered by it.
    """
    total = sunlit_image + earthlit_image

    # Expose the sunlit surface, which is what a photographer meters for -- but
    # only while it actually dominates.  Within a day of new Moon the sunlit
    # sliver carries less light than the earthshine, and metering on it would
    # blow the whole frame out.
    sunlit_peak = float(np.max(sunlit_image)) if sunlit_image.any() else 0.0
    earthshine_peak = float(np.max(earthlit_image)) if earthlit_image.any() else 0.0
    if sunlit_peak >= earthshine_peak:
        reference = sunlit_image if sunlit_peak > 0.0 else None
    else:
        reference = None
    stats["exposure_reference"] = "sunlit" if reference is not None else "total"
    # The earthshine numbers the frame was actually drawn with.  They vary by
    # two orders of magnitude over a lunation, so a page that wants to caption
    # or verify a picture needs the values of *this* instant, not a constant
    # table -- and they come from the ephemeris, not from the image.
    #
    # The ratio stays the physical one even when earthshine is switched off:
    # that is still how much light the Earth is delivering.  Whether it was
    # *painted* is a separate, honest flag, because a frame drawn without it
    # says nothing about how faint the Earth was.
    stats["earthshine_ratio"] = float(view.earthshine_ratio)
    stats["earthshine_magnitude"] = float(view.earthshine_magnitude)
    stats["earthshine_apparent_magnitude"] = float(view.earthshine_apparent_magnitude)
    stats["earth_illuminated_percent"] = float(view.earth_illuminated_percent)
    stats["earth_phase_angle_deg"] = float(view.earth_phase_angle_deg)
    stats["earth_angular_diameter_deg"] = float(view.earth_angular_diameter_deg)
    stats["earth_distance_km"] = float(view.earth_distance_km)
    stats["earth_geometric_albedo"] = float(options.earth_geometric_albedo)
    stats["earthshine_gain"] = float(options.earthshine_gain)
    stats["earthshine_on"] = bool(options.earthshine)
    stats["earthshine_tint"] = [float(part) for part in options.earthshine_tint]
    probe = reference if reference is not None else total
    positive = probe[probe > 0.0]
    anchor = (
        _exposure_anchor(positive, options.exposure_meter) if positive.size else 0.0
    )
    stats["exposure_anchor_raw"] = float(anchor)
    limited = False
    if exposure_hint is not None and float(exposure_hint) > 0.0 and anchor > 0.0:
        # A magnitude is a factor of 10 ** 0.4, not 10.
        limit = 10.0 ** (0.4 * EXPOSURE_SLEW_MAG)
        clamped = min(float(exposure_hint) * limit,
                      max(float(exposure_hint) / limit, anchor))
        limited = clamped != anchor
        anchor = clamped
    stats["exposure_anchor"] = float(anchor)
    stats["exposure_limited"] = bool(limited)
    exposure_scale = 1.0
    if options.auto_exposure and anchor > 0.0:
        exposure_scale = 1.0 / anchor
    exposure_scale *= float(2.0 ** options.exposure_ev)
    stats["exposure_scale"] = exposure_scale
    stats["exposure_meter"] = str(options.exposure_meter)

    display = apply_tone_mapping(
        total,
        tone=options.tone,
        exposure_ev=options.exposure_ev,
        auto_exposure=options.auto_exposure,
        reference=reference,
        asinh_softening=options.asinh_softening,
        # The exposure computed just above, *handed over* rather than derived
        # again: metering twice meant the pixels were always exposed on the
        # 99.9th percentile whatever `exposure_meter` said, so an animation's
        # steady statistic reached `stats` and stopped there.
        scale=exposure_scale,
    )
    return MoonRender(
        view=view,
        settings=options,
        image=display,
        sunlit=sunlit_image if options.keep_components else None,
        earthshine=earthlit_image if options.keep_components else None,
        stats=stats,
        region=region,
    )


def _frame_grid(
    options: MoonRenderSettings,
    forward: np.ndarray,
    right: np.ndarray,
    up: np.ndarray,
    pixel_scale: float,
    samples: int,
) -> Tuple[np.ndarray, np.ndarray, int, int]:
    """Column and row offsets of the supersampled ray grid, and its size."""
    fine_width = options.width * samples
    fine_height = options.height * samples
    sample_scale = pixel_scale / samples
    columns = np.arange(fine_width, dtype=np.float64) - 0.5 * (fine_width - 1)
    rows = 0.5 * (fine_height - 1) - np.arange(fine_height, dtype=np.float64)
    return columns * sample_scale, rows * sample_scale, fine_width, fine_height


def render_moon(
    observer: Any = None,
    when: Any = None,
    *,
    settings: Optional[MoonRenderSettings] = None,
    terrain: Optional[MoonTerrain] = None,
    view: Optional[MoonView] = None,
    progress: Optional[Any] = None,
    cancel: Optional[Any] = None,
    exposure_hint: Optional[float] = None,
) -> MoonRender:
    """Render the whole lunar disk as it appears from *observer* at *when*.

    Parameters
    ----------
    observer, when
        Observing site and instant; ignored when *view* is supplied.
    settings
        :class:`moonlib_s.models.MoonRenderSettings`; the defaults give a
        1024x1024 disk with cast shadows and earthshine enabled.
    terrain
        Pre-opened :class:`moonlib_s.terrain.MoonTerrain`.  When omitted the
        global products are opened lazily, which bakes the pyramid cache on
        first use.
    view
        Reuse an already computed :class:`moonlib_s.models.MoonView`.
    progress
        Optional ``callable(fraction, message)`` forwarded to the cache bake.
    cancel
        Optional ``callable() -> bool``.  Polled between bands; when it returns
        true the render stops with :class:`MoonlibError` instead of tracing a
        frame the caller has stopped waiting for.
    exposure_hint
        The radiance anchor the previous frame of a run was exposed on, from
        its ``stats["exposure_anchor"]``.  The anchor measured here is pulled
        back to within :data:`EXPOSURE_SLEW_MAG` of it, which is what keeps a
        sequence from jumping between frames; see that constant.  ``None`` --
        a one-off picture -- leaves the exposure exactly where it was.

    Returns
    -------
    moonlib_s.models.MoonRender
        Display image plus the separate sunlit and earthshine radiance fields.

    Notes
    -----
    The frame is traced in horizontal bands.  Every ray is independent, so
    banding changes nothing about the result -- the step budget and the shadow
    sampling are both frame-wide constants -- but it keeps the peak memory
    proportional to ``width * band`` instead of to the whole supersampled
    frame, which is what lets a multi-thousand-pixel render fit in RAM at all.

    See Also
    --------
    render_region : the same physics aimed at one area instead of the disk.
    """
    options = settings or MoonRenderSettings()
    _validate_settings(options)
    if view is None:
        view = view_geometry(observer, when, earth_albedo=options.earth_geometric_albedo)
    if terrain is None:
        terrain = open_terrain(progress=progress)

    forward, right, up = _camera_basis(view)
    origin = float(view.distance_km) * np.asarray(
        view.observer_direction, dtype=np.float64
    )

    samples = max(1, int(options.samples_per_pixel))
    pixel_scale = 2.0 * math.tan(math.radians(0.5 * view.angular_diameter_deg)) / (
        options.disk_fraction * min(options.width, options.height)
    )
    columns, rows, fine_width, fine_height = _frame_grid(
        options, forward, right, up, pixel_scale, samples
    )
    ground_km = float(view.distance_km) * pixel_scale
    sample_km = max(ground_km / samples, 1e-4)
    level = (
        int(options.pyramid_level)
        if options.pyramid_level is not None
        else terrain.dem.level_for_ground_scale(sample_km)
    )
    albedo_level = (
        terrain.albedo.level_for_ground_scale(sample_km)
        if terrain.albedo is not None
        else 0
    )
    steps = _estimate_ray_steps(
        origin, forward, right, up, columns, rows, 1.0, terrain, level, options
    )

    build_band = _band_direction_builder(forward, right, up, columns, rows)
    sunlit, earthlit, stats = _render_bands(
        origin,
        terrain,
        view,
        options,
        fine_width=fine_width,
        fine_height=fine_height,
        samples=samples,
        band_rows=_band_rows(fine_width, fine_height, samples),
        band_directions=build_band,
        ground_km=ground_km,
        sample_km=sample_km,
        level=level,
        albedo_level=albedo_level,
        steps=steps,
        cancel=cancel,
    )
    stats["apparent_diameter_pixels"] = float(
        options.disk_fraction * min(options.width, options.height)
    )
    return _finish_render(view, options, sunlit, earthlit, stats,
                          exposure_hint=exposure_hint)


def _band_direction_builder(
    forward: np.ndarray,
    right: np.ndarray,
    up: np.ndarray,
    columns: np.ndarray,
    rows: np.ndarray,
) -> Any:
    """Closure returning unit directions for a band of the ray grid."""

    def build(row_start: int, row_stop: int) -> np.ndarray:
        band = (
            forward[None, None, :]
            + columns[None, :, None] * right[None, None, :]
            + rows[row_start:row_stop, None, None] * up[None, None, :]
        )
        return band / np.linalg.norm(band, axis=-1, keepdims=True)

    return build


def _validate_settings(options: MoonRenderSettings) -> None:
    if options.width < 8 or options.height < 8:
        raise MoonlibError("a render needs an image of at least 8x8 pixels.")
    if not 0.05 <= options.disk_fraction <= 1.0:
        raise MoonlibError("disk_fraction must lie in [0.05, 1.0].")
    if options.ray_steps_min > options.ray_steps_max:
        raise MoonlibError("ray_steps_min must not exceed ray_steps_max.")






def render_region(
    view: MoonView,
    lon_deg: float,
    lat_deg: float,
    *,
    span_km: Optional[float] = None,
    settings: Optional[MoonRenderSettings] = None,
    terrain: Optional[MoonTerrain] = None,
    progress: Optional[Any] = None,
    cover_km: Optional[float] = None,
    margin: float = 1.25,
    fov_radius_arcsec: Optional[float] = None,
    fov_radius_km: Optional[float] = None,
    rotation_deg: float = 0.0,
    width: Optional[int] = None,
    height: Optional[int] = None,
    arcsec_per_pixel: Optional[float] = None,
    cancel: Optional[Any] = None,
    exposure_hint: Optional[float] = None,
) -> MoonRender:
    """Simulate a telescope close-up of one selenographic area.

    The camera is aimed at a single point on the surface and its field of view
    is set by *span_km*, the surface distance across the frame.  Everything
    else is the same physics that draws the whole disk: the observer's real
    position, the true Sun direction, the same photometric function, the same
    cast shadows.  What changes is only where the camera points and how much it
    magnifies, so the result is what an observer at that site would see through
    a telescope of that scale at that instant.

    Two consequences of the geometry are worth expecting rather than being
    surprised by:

    * **Foreshortening.** A region away from the sub-observer point is seen at
      an angle, so the frame covers less surface along the direction of tilt
      than *span_km* suggests, and *span_km* is measured on the surface rather
      than in the sky.
    * **A resolution ceiling set by the data, not the request.** Beyond the
      terrain's own pixel spacing the renderer is interpolating.  ``stats``
      reports the ground sample distance actually achieved, and
      :attr:`MoonRegion.terrain_limited` says whether the request went past it.

    Parameters
    ----------
    view
        Geometry from :func:`view_geometry`; the target must be on the near
        side, which is checked.
    lon_deg, lat_deg
        Selenographic centre of the close-up.
    span_km
        Surface extent of *sky* across the shorter side of the frame, in
        kilometres.  Note that this is not the ground the frame covers: see
        :func:`required_sky_span_km`.
    cover_km
        Alternative to *span_km*: the diameter of a feature that must fit, in
        kilometres.  The span is then measured from the geometry so the feature
        is fully inside the frame with *margin* to spare.
    fov_radius_arcsec, fov_radius_km
        Further ways to say how much sky the frame covers, as a **radius** from
        the centre rather than a full width.  ``fov_radius_km`` is that radius
        expressed as ground distance at the Moon's distance; the two are the
        same statement in different units, and ``span_km = 2 * fov_radius_km``.
        Give exactly one of *span_km*, *cover_km*, *fov_radius_arcsec* and
        *fov_radius_km*.
    rotation_deg
        Roll of the camera about the line of sight, counter-clockwise in the
        image.  Zero puts selenographic north up, which is how an observer with
        an alt-azimuth mount and no derotator does *not* see it, but is the
        easiest frame to reason about.
    width, height
        Output size in pixels, overriding the settings.  *height* defaults to
        *width*.
    arcsec_per_pixel
        Output sampling, overriding *width*: the frame keeps the extent asked
        for and the pixel count follows.  This is the parameter to use when the
        point of the render is to match a particular camera, since it is the
        plate scale rather than the pixel count that decides what is resolved.
    settings, terrain, progress
        As for :func:`render_moon`; ``disk_fraction`` is ignored here.

    Raises
    ------
    MoonlibError
        When the target is beyond the limb, or the request is unusable.

    See Also
    --------
    render_moon : the whole disk, same shading pipeline.
    """
    options = settings or MoonRenderSettings()
    given = [
        name
        for name, value in (
            ("span_km", span_km),
            ("cover_km", cover_km),
            ("fov_radius_arcsec", fov_radius_arcsec),
            ("fov_radius_km", fov_radius_km),
        )
        if value is not None
    ]
    if len(given) != 1:
        raise MoonlibError(
            "Give exactly one way of saying how much sky the frame covers: "
            "span_km (full width), fov_radius_arcsec or fov_radius_km (radius), "
            "or cover_km (a feature diameter to fit). Got: "
            + (", ".join(given) if given else "none")
            + "."
        )
    if cover_km is not None:
        span_km = required_sky_span_km(
            view, lon_deg, lat_deg, cover_km, margin=margin, terrain=terrain
        )
    if fov_radius_arcsec is not None:
        if fov_radius_arcsec <= 0.0:
            raise MoonlibError("fov_radius_arcsec must be positive.")
        span_km = 2.0 * float(view.distance_km) * math.tan(
            math.radians(float(fov_radius_arcsec) / 3600.0)
        )
    elif fov_radius_km is not None:
        if fov_radius_km <= 0.0:
            raise MoonlibError("fov_radius_km must be positive.")
        span_km = 2.0 * float(fov_radius_km)
    if span_km is None or span_km <= 0.0:
        raise MoonlibError("span_km must be positive.")

    resolved_width = int(width if width is not None else options.width)
    resolved_height = int(
        height if height is not None else (width if width is not None else options.height)
    )
    if arcsec_per_pixel is not None:
        if arcsec_per_pixel <= 0.0:
            raise MoonlibError("arcsec_per_pixel must be positive.")
        # The frame's half-angle is fixed by span_km; the pixel count then
        # follows from the sampling asked for.
        half_angle = math.atan(0.5 * float(span_km) / float(view.distance_km))
        per_pixel = math.tan(math.radians(float(arcsec_per_pixel) / 3600.0))
        resolved_width = max(8, int(round(2.0 * math.tan(half_angle) / per_pixel)))
        resolved_height = resolved_width
    if (
        resolved_width != options.width
        or resolved_height != options.height
        or rotation_deg
    ):
        options = replace(
            options,
            width=resolved_width,
            height=resolved_height,
            rotation_deg=float(rotation_deg),
        )
    _validate_settings(options)
    # Upgrade whatever topography we were handed to the finest available: a
    # caller that passes the baked global pyramid is asking for *a* terrain, not
    # specifically the coarse one, and a close-up rendered against it is several
    # times softer than the same call with no terrain at all.
    #
    # The window is cut from the *measured* footprint rather than a radius
    # guess.  A guess sized from the frame's diagonal looks generous but can
    # still miss an elongated or steeply tilted frame, and the shortfall then
    # surfaces as a coverage error on a selection that is perfectly valid.
    base_terrain = terrain
    probe_camera = _region_camera(view, lon_deg, lat_deg, None)
    probe_pixel_scale = (
        2.0 * math.tan(math.atan(0.5 * span_km / probe_camera["distance"]))
        / max(1, min(options.width, options.height))
    )
    # Probe with the *rolled* basis.  A rotated rectangle covers a larger
    # lon/lat box than an upright one -- up to sqrt(2) for a square at 45
    # degrees, more where meridians converge -- so measuring the upright frame
    # and then rendering the rotated one cut the window too small.
    probe_right, probe_up = _rolled_basis(
        probe_camera["right"], probe_camera["up"], options.rotation_deg
    )
    footprint = _region_coverage(
        probe_camera["origin"], probe_camera["forward"], probe_right,
        probe_up, probe_pixel_scale, options.width, options.height,
        None,
    )
    upgraded = _terrain_for_footprint(footprint, base=terrain)
    if upgraded is not None:
        terrain = upgraded
    if terrain is None:
        terrain = open_terrain(progress=progress)

    observer_position = float(view.distance_km) * np.asarray(
        view.observer_direction, dtype=np.float64
    )
    region = region_geometry(view, lon_deg, lat_deg, span_km, options, terrain)
    if cover_km is not None:
        object.__setattr__(region, "covering_diameter_km", float(cover_km))

    to_target = _target_vector(lon_deg, lat_deg, terrain) - observer_position
    distance = float(np.linalg.norm(to_target))
    forward = to_target / distance
    pole = np.array([0.0, 0.0, 1.0])
    up = pole - float(np.dot(pole, forward)) * forward
    up /= float(np.linalg.norm(up))
    right = np.cross(forward, up)
    right /= float(np.linalg.norm(right))

    samples = max(1, int(options.samples_per_pixel))
    half_angle = math.atan(0.5 * span_km / distance)
    pixel_scale = 2.0 * math.tan(half_angle) / min(options.width, options.height)
    columns, rows, fine_width, fine_height = _frame_grid(
        options, forward, right, up, pixel_scale, samples
    )
    ground_km = distance * pixel_scale
    sample_km = max(ground_km / samples, 1e-4)
    level = (
        int(options.pyramid_level)
        if options.pyramid_level is not None
        else terrain.dem.level_for_ground_scale(sample_km)
    )
    albedo_level = (
        terrain.albedo.level_for_ground_scale(sample_km)
        if terrain.albedo is not None
        else 0
    )
    right, up = _rolled_basis(right, up, options.rotation_deg)

    # Decide the terrain against the frame we are actually about to trace, not
    # against an estimate made earlier: a rotated or steeply tilted frame covers
    # a larger lon/lat box than the probe suggests, and any shortfall would
    # otherwise surface as a failure on a perfectly valid selection.  The baked
    # pyramid has no bounds, so this fallback always succeeds.
    if not _footprint_fits(
        _region_coverage(
            observer_position, forward, right, up, pixel_scale,
            options.width, options.height, terrain,
        ),
        terrain.dem.bounds,
    ):
        # Fall back to what the caller handed us before opening anything new: a
        # regional window that turns out to be a degree short must not be
        # replaced by a fresh *global* open, which -- when the page's 数据源 is a
        # 34 GB product -- means an eleven-gigabyte bake in the middle of a
        # render.  Only a caller that passed nothing opens the default pyramid.
        terrain = terrain if not terrain.dem.is_regional else (
            open_terrain(progress=progress) if base_terrain is None else base_terrain
        )
    level = (
        int(options.pyramid_level)
        if options.pyramid_level is not None
        else terrain.dem.level_for_ground_scale(sample_km)
    )
    albedo_level = (
        terrain.albedo.level_for_ground_scale(sample_km)
        if terrain.albedo is not None
        else 0
    )
    steps = _estimate_ray_steps(
        observer_position, forward, right, up, columns, rows, 1.0,
        terrain, level, options,
    )
    terrain_source = (
        "high-resolution window"
        if terrain.dem.is_regional
        else ("global pyramid (no window covers this frame)"
              if footprint is not None else "global pyramid")
    )

    sunlit, earthlit, stats = _render_bands(
        observer_position,
        terrain,
        view,
        options,
        fine_width=fine_width,
        fine_height=fine_height,
        samples=samples,
        band_rows=_band_rows(fine_width, fine_height, samples),
        band_directions=_band_direction_builder(forward, right, up, columns, rows),
        ground_km=ground_km,
        sample_km=sample_km,
        level=level,
        albedo_level=albedo_level,
        steps=steps,
        cancel=cancel,
    )
    stats["terrain_source"] = terrain_source
    stats["span_km"] = float(span_km)
    stats["fov_radius_km"] = 0.5 * float(span_km)
    stats["rotation_deg"] = float(options.rotation_deg)
    stats["arcsec_per_pixel"] = pixel_scale * ARCSEC_PER_RADIAN
    stats["fov_arcsec"] = math.degrees(2.0 * half_angle) * 3600.0
    stats["distance_to_target_km"] = distance
    stats["emergence_deg"] = region.emergence_deg
    stats["incidence_deg"] = region.incidence_deg
    stats["terrain_limited"] = region.terrain_limited
    return _finish_render(view, options, sunlit, earthlit, stats, region=region,
                          exposure_hint=exposure_hint)


def _region_coverage(
    origin: np.ndarray,
    forward: np.ndarray,
    right: np.ndarray,
    up: np.ndarray,
    pixel_scale: float,
    width: int,
    height: int,
    terrain: MoonTerrain,
) -> Optional[Dict[str, float]]:
    """Lon/lat box the frame covers, or ``None`` for a global raster.

    Sampling a regional patch outside its bounds is clamped, which would
    quietly flatten the edge of a close-up, so the caller compares this box
    against the patch before rendering.
    """
    reference = MOON_REFERENCE_RADIUS_KM
    half_w = 0.5 * width * pixel_scale
    half_h = 0.5 * height * pixel_scale
    grid_u, grid_v = np.meshgrid(
        np.linspace(-half_w, half_w, 9), np.linspace(-half_h, half_h, 9)
    )
    directions = (
        forward[None, None, :]
        + grid_u[..., None] * right[None, None, :]
        + grid_v[..., None] * up[None, None, :]
    )
    directions /= np.linalg.norm(directions, axis=-1, keepdims=True)
    flat = directions.reshape(-1, 3)
    distance = float(np.linalg.norm(origin))
    b = np.einsum("ij,j->i", flat, origin)
    disc = b * b - (distance * distance - reference * reference)
    hits = disc > 0.0
    if not np.any(hits):
        return None
    root = np.sqrt(np.maximum(disc, 0.0))
    t_near = np.maximum(-b - root, 0.0)
    # Only the rays that actually land on the globe describe the frame.
    points = origin[None, :] + t_near[hits][:, None] * flat[hits]
    lon, lat = _spherical_from_cartesian(points)
    return {
        "lon_min": float(np.min(lon)),
        "lon_max": float(np.max(lon)),
        "lat_min": float(np.min(lat)),
        "lat_max": float(np.max(lat)),
    }


def _check_region_covers(
    footprint: Optional[Dict[str, float]], bounds: Any, label: str
) -> None:
    if footprint is None or bounds is None:
        return
    lon_min, lon_max, lat_min, lat_max = bounds
    margin = 0.0
    outside = (
        footprint["lon_min"] < lon_min - margin
        or footprint["lon_max"] > lon_max + margin
        or footprint["lat_min"] < lat_min - margin
        or footprint["lat_max"] > lat_max + margin
    )
    if outside:
        raise MoonlibError(
            f"The {label} covers lon {footprint['lon_min']:.2f}.."
            f"{footprint['lon_max']:.2f}, lat {footprint['lat_min']:.2f}.."
            f"{footprint['lat_max']:.2f}, but the regional terrain only spans "
            f"lon {lon_min:.2f}..{lon_max:.2f}, lat {lat_min:.2f}..{lat_max:.2f}. "
            "Reduce span_km, recentre the view, or fetch a larger window with "
            "tools/fetch_region_dem.py."
        )


def _rolled_basis(
    right: np.ndarray, up: np.ndarray, rotation_deg: float
) -> Tuple[np.ndarray, np.ndarray]:
    """Roll the camera about the line of sight.

    Everything downstream only ever sees ``right`` and ``up``, so nothing else
    has to know the frame was rotated -- but everything that *measures* the
    frame does, which is why this is one function rather than two copies.
    """
    if not rotation_deg:
        return right, up
    angle = math.radians(float(rotation_deg))
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    return cos_a * right + sin_a * up, -sin_a * right + cos_a * up


def selection_footprint(
    view: MoonView,
    lon_deg: float,
    lat_deg: float,
    *,
    fov_radius_arcsec: float,
    width: int,
    height: int,
    rotation_deg: float = 0.0,
    terrain: Optional[MoonTerrain] = None,
) -> Optional[Dict[str, float]]:
    """The lon/lat box one close-up frame really covers.

    Near the limb a frame is foreshortened, so its ground footprint is much
    longer than the small-angle span: measuring it here, with the same
    :func:`_region_coverage` the window cutter uses, is what makes "download the
    area I am looking at" mean what the reader sees.
    """
    camera = _region_camera(view, lon_deg, lat_deg, terrain)
    span = 2.0 * float(camera["distance"]) * math.tan(
        math.radians(float(fov_radius_arcsec) / 3600.0))
    right, up = _rolled_basis(camera["right"], camera["up"], rotation_deg)
    pixel_scale = (2.0 * math.tan(0.5 * float(span) / float(camera["distance"]))
                   / max(1, min(int(width), int(height))))
    return _region_coverage(
        camera["origin"], camera["forward"], right, up, pixel_scale,
        int(width), int(height), None,
    )


def region_terrain(
    views: Any,
    lon_deg: float,
    lat_deg: float,
    *,
    span_km: Optional[float] = None,
    fov_radius_arcsec: Optional[float] = None,
    rotation_deg: float = 0.0,
    settings: Optional[MoonRenderSettings] = None,
    terrain: Optional[MoonTerrain] = None,
) -> Optional[MoonTerrain]:
    """One high-resolution window covering a whole *run* of close-ups.

    Exposed because a run -- an animation, a series of stills of the same
    target -- must be rendered with **one** terrain window rather than a fresh
    one per frame.  A window's grid is anchored to the source raster's pixels,
    so as the frame's footprint drifts (the Moon's distance changes, the
    libration moves the camera) a re-cut window slides by whole source pixels
    and the terrain under the frame changes with it.  At a grazing sun those
    sub-pixel shifts move shadow edges, and the animation shows shadows that
    jump from frame to frame while nothing physical has happened.

    *views* is one :class:`Moonlib MoonView` or a sequence of them -- the
    instants the run spans.  The window covers all of their footprints at once,
    so every frame of the run finds its ground inside it.

    Returns ``None`` when there is nothing finer to gain (no high-resolution
    raster on disk, or the baked pyramid is already as good), which is the same
    answer :func:`render_region` would reach on its own.
    """
    options = settings or MoonRenderSettings()
    if span_km is None and fov_radius_arcsec is None:
        raise MoonlibError(
            "region_terrain needs span_km or fov_radius_arcsec to know how much "
            "ground the run covers."
        )
    if isinstance(views, MoonView):
        views = [views]
    boxes: List[Dict[str, float]] = []
    for view in views:
        camera = _region_camera(view, lon_deg, lat_deg, terrain)
        span = span_km
        if span is None:
            span = 2.0 * float(camera["distance"]) * math.tan(
                math.radians(float(fov_radius_arcsec) / 3600.0)
            )
        right, up = _rolled_basis(camera["right"], camera["up"], rotation_deg)
        # The pixel scale the frame will actually be rendered at, so the box is
        # the frame's own coverage rather than a guess at it.
        pixel_scale = (
            2.0 * math.tan(0.5 * float(span) / float(camera["distance"]))
            / min(int(options.width), int(options.height))
        )
        box = _region_coverage(
            camera["origin"], camera["forward"], right, up, pixel_scale,
            int(options.width), int(options.height), None,
        )
        if box is not None:
            boxes.append(box)
    if not boxes:
        return None
    merged = {
        "lon_min": min(box["lon_min"] for box in boxes),
        "lon_max": max(box["lon_max"] for box in boxes),
        "lat_min": min(box["lat_min"] for box in boxes),
        "lat_max": max(box["lat_max"] for box in boxes),
    }
    return _terrain_for_footprint(merged, base=terrain)


def _region_camera(
    view: MoonView, lon_deg: float, lat_deg: float, terrain: Optional[MoonTerrain]
) -> Dict[str, Any]:
    """Observer position and camera basis aimed at one selenographic point."""
    target = _target_vector(lon_deg, lat_deg, terrain)
    observer_position = float(view.distance_km) * np.asarray(
        view.observer_direction, dtype=np.float64
    )
    to_target = target - observer_position
    distance = float(np.linalg.norm(to_target))
    forward = to_target / distance
    pole = np.array([0.0, 0.0, 1.0])
    up = pole - float(np.dot(pole, forward)) * forward
    up /= float(np.linalg.norm(up))
    right = np.cross(forward, up)
    right /= float(np.linalg.norm(right))
    return {
        "target": target,
        "origin": observer_position,
        "distance": distance,
        "forward": forward,
        "right": right,
        "up": up,
    }


def _sphere_hit(origin: np.ndarray, directions: np.ndarray) -> np.ndarray:
    """Nearest intersection of rays with the reference sphere, in km."""
    reference = MOON_REFERENCE_RADIUS_KM
    distance = float(np.linalg.norm(origin))
    b = np.einsum("ij,j->i", directions, origin)
    disc = b * b - (distance * distance - reference * reference)
    root = np.sqrt(np.maximum(disc, 0.0))
    return origin[None, :] + np.maximum(-b - root, 0.0)[:, None] * directions


def _surface_footprint(
    camera: Dict[str, Any], sky_span_km: float
) -> Tuple[float, float]:
    """Surface kilometres the frame covers, across and along the tilt.

    Measured by intersecting the frame-edge rays with the reference sphere and
    taking the great-circle distance between opposite edges.  Measuring beats
    deriving here: the compression depends on the angle between the frame axes
    and the direction the surface leans away, which is neither purely
    north-south nor equal to ``1 / cos(emergence)`` in general.
    """
    reference = MOON_REFERENCE_RADIUS_KM
    half = 0.5 * sky_span_km / camera["distance"]
    forward, right, up = camera["forward"], camera["right"], camera["up"]
    extents = []
    for axis in (right, up):
        edge = []
        for sign in (-1.0, 1.0):
            direction = forward + sign * half * axis
            edge.append(direction / np.linalg.norm(direction))
        points = _sphere_hit(camera["origin"], np.stack(edge))
        cosine = float(np.dot(points[0], points[1])) / (reference * reference)
        extents.append(reference * math.acos(max(-1.0, min(1.0, cosine))))
    return extents[0], extents[1]


def required_sky_span_km(
    view: MoonView,
    lon_deg: float,
    lat_deg: float,
    diameter_km: float,
    *,
    margin: float = 1.25,
    terrain: Optional[MoonTerrain] = None,
) -> float:
    """Sky span that puts a feature of *diameter_km* fully inside the frame.

    The frame's *sky* span and the *ground* it covers are not the same number:
    where the surface leans away from the viewer, one unit of sky sweeps more
    than one unit of ground, and how much more depends on the angle between the
    frame axes and the direction of lean.  So this measures the ground covered
    for a trial span and scales, rather than assuming a factor.

    *margin* is applied on top, so ``1.25`` leaves a quarter of the feature's
    diameter as breathing room around it.

    Examples
    --------
    >>> span = required_sky_span_km(view, -14.4, -58.4, 231.0)   # Clavius
    """
    if diameter_km <= 0.0:
        raise MoonlibError("diameter_km must be positive.")
    if margin < 1.0:
        raise MoonlibError("margin must be at least 1.0.")
    camera = _region_camera(view, lon_deg, lat_deg, terrain)
    probe = 100.0
    across, along = _surface_footprint(camera, probe)
    ground = min(across, along)
    if ground <= 0.0:  # pragma: no cover - only for a degenerate target
        raise MoonlibError("cannot measure the frame footprint at this target.")
    return margin * float(diameter_km) * probe / ground


def render_feature(
    view: MoonView,
    feature: Any,
    *,
    margin: float = 1.25,
    settings: Optional[MoonRenderSettings] = None,
    terrain: Optional[MoonTerrain] = None,
    progress: Optional[Any] = None,
) -> MoonRender:
    """Render a close-up sized to fit a named or measured landform.

    *feature* may be a :class:`moonlib_s.models.SurfaceFeature`, a catalogue name
    (``"Clavius"``, ``"克拉维斯环形山"``), or a diameter in kilometres together
    with the frame centre via :func:`render_region`'s ``cover_km``.

    See Also
    --------
    moonlib_s.features.find_feature, moonlib_s.features.feature_from_measurement
    """
    from .features import find_feature

    resolved = find_feature(feature) if isinstance(feature, str) else feature
    render = render_region(
        view,
        resolved.lon_deg,
        resolved.lat_deg,
        cover_km=resolved.diameter_km,
        margin=margin,
        settings=settings,
        terrain=terrain,
        progress=progress,
    )
    object.__setattr__(render.region, "feature_name", resolved.label)
    object.__setattr__(render.region, "covering_diameter_km", resolved.diameter_km)
    render.stats["feature"] = resolved.to_dict()
    return render


def _auto_terrain(
    view: MoonView,
    lon_deg: float,
    lat_deg: float,
    span_km: float,
    aspect: float = 1.0,
    base: Optional[MoonTerrain] = None,
) -> Optional[MoonTerrain]:
    """Cut a high-resolution window for a close-up, if a global DEM allows it.

    Close-ups are the one case where the baked global pyramid is the wrong
    tool: its level 0 is deliberately decimated so that a whole-disk render
    fits in memory, which is exactly the resolution a close-up needs.  When a
    fine global raster is on disk we therefore read just the window the frame
    will touch -- about 1% of the file, in a fraction of a second -- and use
    that instead.  Returns ``None`` when there is nothing finer to gain.
    """
    from .terrain import global_dem_sources, open_region_at, open_terrain

    sources = global_dem_sources()
    if not sources:
        return None
    if base is None:
        base = open_terrain()
    best_km = base.dem.km_per_pixel(0)
    chosen = sources[0]
    if chosen[1] >= best_km * 0.9:
        return None  # the baked pyramid is already as good as what is on disk
    camera = _region_camera(view, lon_deg, lat_deg, None)
    # The window has to hold the *longer* side of the frame, so probe with the
    # span scaled by the aspect ratio.
    across, along = _surface_footprint(camera, span_km * max(1.0, float(aspect)))
    reach = 0.75 * math.hypot(across, along)
    try:
        return open_region_at(
            lon_deg, lat_deg, reach, source=chosen[0], terrain=base, pad=1.25
        )
    except MoonlibError:  # pragma: no cover - window refused, fall back
        return None

#: Largest window worth cutting from a source raster, in source pixels.  At the
#: 256 px/deg global product this is about 24 by 24 degrees, or a 650 km square,
#: which is far more than a close-up needs; a frame wide enough to want more is
#: a whole-Moon view, and the baked global pyramid already covers that at a
#: resolution suited to the scale.
_MAX_WINDOW_PIXELS = 6144 * 6144


#: Margin added around a frame's footprint when cutting a window, in degrees.
#: Wide enough that a grazing-sun shadow ray still lands inside the window; the
#: page and the region downloader both use it, so a patch is always at least as
#: big as the window the renderer will ask for.
WINDOW_MARGIN_DEG = 2.5


def _terrain_for_footprint(
    footprint: Optional[Dict[str, float]],
    *,
    base: Optional[MoonTerrain] = None,
    margin_deg: float = WINDOW_MARGIN_DEG,
) -> Optional[MoonTerrain]:
    """Cut a high-resolution window that covers a measured frame footprint.

    Returns ``None`` when there is nothing finer to gain *or* when the window
    would be unreasonably large, so the caller can fall back to whatever it
    already had.  Falling back is the right answer for an enormous frame: the
    baked global pyramid has no bounds, so a whole-Moon selection simply gets
    the resolution appropriate to that scale instead of failing a coverage
    check.

    A *base* that is already a regional window covering the footprint is kept as
    it is.  That is what lets a caller cut one window and render a whole run of
    frames with it -- see :func:`region_terrain` -- and it matters numerically,
    not just for I/O: a window's grid is anchored to the source raster's pixels,
    so a window re-cut for every frame samples the same terrain at slightly
    different points, and at a grazing sun that shows up as shadows that jump
    from frame to frame.
    """
    from .terrain import (
        find_covering_region,
        global_dem_sources,
        open_region,
        open_region_box,
        open_terrain,
    )

    if footprint is None:
        return None
    if base is not None and base.dem.is_regional:
        if _footprint_fits(footprint, base.dem.bounds):
            return None
    if base is None:
        base = open_terrain()

    # A patch downloaded for this neighbourhood is the best answer there is:
    # it exists because somebody asked for this ground at this scale, and it is
    # small enough to read whole.  It wins over the global raster even when the
    # two have the same pixel size -- a SLDEM2015 patch is the same 59 m as the
    # LOLA GDR tile but its gaps are measured rather than interpolated.
    # Only a small margin is *required* of a patch -- the frame itself plus a
    # little room.  A patch is downloaded with the full margin, but demanding it
    # here would throw away a usable patch over a tenth of a degree of frame
    # measurement, and the shadow rays that the margin exists for are already
    # inside the downloaded area.
    patch_margin = min(float(margin_deg), 0.5)
    patch = find_covering_region(
        footprint["lon_min"] - patch_margin, footprint["lon_max"] + patch_margin,
        footprint["lat_min"] - patch_margin, footprint["lat_max"] + patch_margin,
    )
    if patch is not None and patch[1] < base.dem.km_per_pixel(0) * 0.999:
        try:
            return open_region(patch[0], terrain=base)
        except MoonlibError:  # pragma: no cover - unreadable patch, fall back
            pass

    sources = global_dem_sources()
    if not sources:
        return None
    chosen = sources[0]
    if chosen[1] >= base.dem.km_per_pixel(0) * 0.9:
        return None  # the baked pyramid is already as good as what is on disk

    span_lon = footprint["lon_max"] - footprint["lon_min"] + 2 * margin_deg
    span_lat = footprint["lat_max"] - footprint["lat_min"] + 2 * margin_deg
    # Past this the frame is a large-scale view of the Moon, where the baked
    # pyramid's 0.95 km/px is the resolution that suits the scale; cutting a
    # 150-degree window would cost gigabytes to no visible benefit.
    if span_lon >= 60.0 or span_lat >= 60.0:
        return None
    # Ground kilometres per degree of latitude, over the source's pixel size.
    pixels_per_degree = (math.radians(1.0) * MOON_REFERENCE_RADIUS_KM) / chosen[1]
    # Coarsen rather than give up: a wide frame still gets a window, just one
    # sampled more thinly, which sits between the source and the baked pyramid.
    stride = 1
    while (span_lon * pixels_per_degree / stride
           * span_lat * pixels_per_degree / stride) > _MAX_WINDOW_PIXELS:
        stride *= 2
        if stride > 64:
            return None
    box = (
        footprint["lon_min"] - margin_deg,
        footprint["lon_max"] + margin_deg,
        footprint["lat_min"] - margin_deg,
        footprint["lat_max"] + margin_deg,
    )
    try:
        window = open_region_box(
            box[0], box[1], box[2], box[3],
            source=chosen[0],
            terrain=base,
            stride=stride,
        )
    except MoonlibError:  # pragma: no cover - window refused, keep what we had
        return None
    # Last line of defence.  extract_region clamps to the raster edges, and a
    # rounding difference between this measurement and the renderer's would
    # otherwise surface much later as a coverage failure on a valid selection.
    # The baked pyramid has no bounds, so falling back always works.
    lon_min, lon_max, lat_min, lat_max = window.dem.bounds
    if (
        footprint["lon_min"] < lon_min
        or footprint["lon_max"] > lon_max
        or footprint["lat_min"] < lat_min
        or footprint["lat_max"] > lat_max
    ):
        return None
    return window


def _footprint_fits(
    footprint: Optional[Dict[str, float]], bounds: Optional[Any]
) -> bool:
    """Whether a measured frame footprint lies inside a raster's bounds."""
    if footprint is None or bounds is None:
        return True  # a global raster covers everything
    lon_min, lon_max, lat_min, lat_max = bounds
    return not (
        footprint["lon_min"] < lon_min
        or footprint["lon_max"] > lon_max
        or footprint["lat_min"] < lat_min
        or footprint["lat_max"] > lat_max
    )


def _target_vector(
    lon_deg: float, lat_deg: float, terrain: Optional[MoonTerrain]
) -> np.ndarray:
    """Position of a selenographic point in MOON_ME (km), on the height field."""
    from .frames import spherical_to_vector

    normal = np.asarray(spherical_to_vector(float(lat_deg), float(lon_deg)))
    radius = MOON_REFERENCE_RADIUS_KM
    if terrain is not None:
        radius += float(terrain.dem.sample(float(lon_deg), float(lat_deg), 0))
    return normal * radius


def region_geometry(
    view: MoonView,
    lon_deg: float,
    lat_deg: float,
    span_km: float,
    settings: Optional[MoonRenderSettings] = None,
    terrain: Optional[MoonTerrain] = None,
) -> "MoonRegion":
    """Resolve the viewing and illumination geometry of a close-up target.

    Split out from :func:`render_region` so that a caller can ask "is this
    region observable, and what would it look like?" without paying for a
    render.
    """
    from .frames import spherical_to_vector

    options = settings or MoonRenderSettings()
    normal = np.asarray(spherical_to_vector(float(lat_deg), float(lon_deg)))
    target = _target_vector(lon_deg, lat_deg, terrain)

    observer_position = float(view.distance_km) * np.asarray(
        view.observer_direction, dtype=np.float64
    )
    to_observer = observer_position - target
    to_observer /= max(float(np.linalg.norm(to_observer)), _TINY)
    emergence_cos = float(np.dot(normal, to_observer))
    if emergence_cos <= 0.0:
        raise MoonlibError(
            f"({lon_deg:+.2f}, {lat_deg:+.2f}) is on the far side as seen from "
            "this observer at this instant; the Sun-Moon-observer geometry "
            "would have to change before it could be imaged."
        )

    sun = np.asarray(view.sun_direction, dtype=np.float64)
    incidence_cos = float(np.dot(normal, sun))
    emergence_deg = math.degrees(math.acos(min(1.0, emergence_cos)))
    incidence_deg = (
        math.degrees(math.acos(min(1.0, max(-1.0, incidence_cos))))
        if incidence_cos > 0.0
        else float("nan")
    )

    terrain_km = (
        float(terrain.dem.km_per_pixel(0)) if terrain is not None else float("nan")
    )
    distance = float(np.linalg.norm(observer_position - target))
    ground_km = distance * (
        2.0 * math.tan(math.atan(0.5 * span_km / distance))
        / min(options.width, options.height)
    )
    return MoonRegion(
        lon_deg=float(lon_deg),
        lat_deg=float(lat_deg),
        span_km=float(span_km),
        width=int(options.width),
        height=int(options.height),
        distance_km=distance,
        emergence_deg=emergence_deg,
        incidence_deg=incidence_deg,
        solar_elevation_deg=(90.0 - incidence_deg)
        if incidence_deg == incidence_deg
        else float("nan"),
        foreshortening=emergence_cos,
        ground_sample_km=float(ground_km),
        terrain_km_per_pixel=terrain_km,
        fov_arcsec=math.degrees(2.0 * math.atan(0.5 * span_km / distance)) * 3600.0,
        illuminated=incidence_cos > 0.0,
    )


def _earthshine_radiance(
    view: MoonView,
    points: np.ndarray,
    normals: np.ndarray,
    mu: np.ndarray,
    view_direction: np.ndarray,
    albedo: np.ndarray,
    terrain: MoonTerrain,
    options: MoonRenderSettings,
    *,
    steps: int,
    pixel_km: float,
) -> np.ndarray:
    """Earthshine radiance factor ``I/F`` at the given surface points.

    Two things separate earthshine from sunlight.  The source sits where the
    observer is, so the scattering is always retroreflective (phase angle ~0)
    whatever the Moon's phase happens to be.  And the Earth subtends about 2 deg
    from the Moon, so its disk has to be integrated: that is what turns the
    earthshine terminator into a soft band tens of kilometres wide instead of a
    hard edge, and what lets earthshine reach into craters whose walls would
    block a point source.
    """
    earth_position = np.asarray(view.earth_direction, dtype=np.float64) * float(
        view.earth_distance_km
    )
    to_earth = earth_position[None, :] - points
    to_earth /= np.maximum(np.linalg.norm(to_earth, axis=1, keepdims=True), _TINY)
    mean_direction = np.mean(to_earth, axis=0)
    mean_direction /= max(float(np.linalg.norm(mean_direction)), _TINY)
    offsets = _cone_offsets(
        mean_direction, math.radians(0.5 * view.earth_angular_diameter_deg),
        options.earth_samples,
    )

    intensity = view.earthshine_ratio * options.earthshine_gain
    if intensity <= 0.0:
        return np.zeros((points.shape[0], 3), dtype=np.float64)

    # Occlusion is evaluated once, for the centre of the Earth's disk, and only
    # where the Earth is close enough to the local horizon for topography to
    # matter.  Marching one shadow ray per Earth sample per pixel would dominate
    # the cost for a sub-percent change in the result.
    sin_elevation = np.einsum("ij,j->i", points, mean_direction) / np.maximum(
        np.linalg.norm(points, axis=1), _TINY
    )
    active = np.einsum("ij,ij->i", normals, to_earth) < 0.08
    blocked = _occlusion(
        points,
        mean_direction,
        terrain,
        steps=steps,
        max_range_km=0.5 * options.shadow_range_km,
        pixel_km=pixel_km,
        sin_elevation=sin_elevation,
        active=active,
    )
    visibility = np.where(blocked, 0.0, 1.0)

    accumulated = np.zeros(points.shape[0], dtype=np.float64)
    for offset in offsets:
        sample = to_earth + offset[None, :]
        sample /= np.maximum(np.linalg.norm(sample, axis=1, keepdims=True), _TINY)
        mu_earth = np.clip(np.einsum("ij,ij->i", normals, sample), 0.0, 1.0)
        cos_phase = np.clip(np.einsum("ij,ij->i", sample, -view_direction), -1.0, 1.0)
        accumulated += disk_function(
            options.photometric_model, mu_earth, mu, np.degrees(np.arccos(cos_phase))
        )
    disk_mean = accumulated / len(offsets)

    tint = np.asarray(options.earthshine_tint, dtype=np.float64)
    tint = tint / float(np.mean(tint))
    return albedo * (intensity * visibility * disk_mean)[:, None] * tint[None, :]


# ---------------------------------------------------------------------------
# tone mapping
# ---------------------------------------------------------------------------
def apply_tone_mapping(
    radiance: np.ndarray,
    *,
    tone: Any = ToneMapping.SRGB,
    exposure_ev: float = 0.0,
    auto_exposure: bool = True,
    reference: Optional[np.ndarray] = None,
    asinh_softening: float = 1.0e-3,
    key: float = 1.0,
    scale: Optional[float] = None,
) -> np.ndarray:
    """Map linear radiance onto displayable values in ``[0, 1]``.

    Parameters
    ----------
    radiance
        Linear ``I/F`` array, any shape.
    tone
        :class:`moonlib_s.models.ToneMapping` or its string value.
    exposure_ev
        Exposure in stops, applied before the tone curve.
    auto_exposure
        Scale so that the brightest *sunlit* pixel of *reference* (or of
        *radiance* when no reference is given) lands at 1.0.
    reference
        Array used to key the automatic exposure; passing the sunlit component
        keeps the sunlit surface correctly exposed however bright the
        earthshine is.
    asinh_softening
        Transition point of the asinh stretch; smaller values compress harder
        and therefore reveal fainter detail.
    key
        Mid-grey target of the Reinhard curve.
    scale
        The exposure itself, used instead of deriving one here.  **A renderer
        that has already metered the frame must pass this**, or the frame is
        metered twice -- once by the rule it asked for and once by the 99.9th
        percentile below -- and the second one is what the pixels get.  That is
        exactly how ``exposure_meter="top_mean"`` came to move a number in
        ``stats`` while the animation it was added for kept flickering: the
        statistic an animation needs is not the one this default computes.
        ``None`` keeps the historical behaviour (meter here, on *reference*).
    """
    kind = as_tone_mapping(tone)
    data = np.asarray(radiance, dtype=np.float64)
    if scale is not None:
        factor = float(scale)
    else:
        factor = float(2.0 ** exposure_ev)
        if auto_exposure:
            probe = np.asarray(reference if reference is not None else data,
                               dtype=np.float64)
            positive = probe[probe > 0.0]
            if positive.size:
                factor *= 1.0 / float(np.percentile(positive, 99.9))
    exposed = np.maximum(data * factor, 0.0)

    if kind is ToneMapping.LINEAR:
        out = exposed
    elif kind is ToneMapping.SRGB:
        out = _linear_to_srgb(np.clip(exposed, 0.0, 1.0))
    elif kind is ToneMapping.REINHARD:
        scaled = exposed / max(key, _TINY)
        out = scaled / (1.0 + scaled)
    else:
        softening = asinh_softening
        if kind is ToneMapping.VISUAL:
            softening = VISUAL_ASINH_SOFTENING
            if auto_exposure:
                positive = exposed[exposed > 0.0]
                if positive.size > 100:
                    softening = float(
                        np.percentile(positive, VISUAL_KNEE_PERCENTILE)
                    )
        softening = max(softening, 1e-12)
        stretch = math.asinh(1.0 / softening)
        out = np.arcsinh(exposed / softening) / stretch
    return np.clip(out, 0.0, 1.0).astype(np.float32)


def _linear_to_srgb(values: np.ndarray) -> np.ndarray:
    """IEC 61966-2-1 opto-electronic transfer function."""
    low = values * 12.92
    high = 1.055 * np.power(np.maximum(values, 0.0), 1.0 / 2.4) - 0.055
    return np.where(values <= 0.0031308, low, high)


# ---------------------------------------------------------------------------
# convenience
# ---------------------------------------------------------------------------
def view_summary(view: MoonView) -> Dict[str, Any]:
    """Compact, JSON-safe digest of a :class:`MoonView`."""
    return {
        "when_utc": view.when_utc.isoformat(),
        "observer": view.observer.as_dict(),
        "distance_km": view.distance_km,
        "angular_diameter_deg": view.angular_diameter_deg,
        "libration_lon_deg": view.libration_lon_deg,
        "libration_lat_deg": view.libration_lat_deg,
        "sub_solar_lon_deg": view.sub_solar_lon_deg,
        "sub_solar_lat_deg": view.sub_solar_lat_deg,
        "colongitude_deg": view.colongitude_deg,
        "phase_angle_deg": view.phase_angle_deg,
        # What the Moon is worth in the sky at this phase and distance.  The
        # page prints it; nothing in the render depends on it.
        "moon_magnitude": moon_magnitude(view.phase_angle_deg, view.distance_km),
        "illuminated_percent": view.illuminated_percent,
        "bright_limb_position_angle_deg": view.bright_limb_position_angle_deg,
        "earth_distance_km": view.earth_distance_km,
        "earth_angular_diameter_deg": view.earth_angular_diameter_deg,
        "earth_illuminated_percent": view.earth_illuminated_percent,
        "earthshine_ratio": view.earthshine_ratio,
        "earthshine_magnitude": view.earthshine_magnitude,
        # 地照亮度: the Moon's earthshine-lit night side in our own sky.
        "earthshine_apparent_magnitude": view.earthshine_apparent_magnitude,
        "earthshine_magnitudes_below_sunlight":
            view.earthshine_magnitudes_below_sunlight,
    }
