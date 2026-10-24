"""Lunar eclipses: the Earth's shadow, and the light its air lets through.

The geometry is the classical one already used by :mod:`moonlib.events` to
*forecast* eclipses: the Earth's umbra and penumbra are the Earth's apparent
disc at the Moon minus and plus the Sun's, with the conventional 2 % stretch of
the umbra that stands in for atmospheric refraction.  This module keeps the
same construction but answers a different question -- not "is there an eclipse
that night?" but "what does the Moon look like *at this instant*?" -- so it
also carries the vectors a renderer needs:

* :func:`eclipse_state` classifies the eclipse at one epoch and reports the
  shadow's angular radii, the Moon's offset from the shadow axis, and the Earth
  and Sun positions in ``MOON_ME``.
* :meth:`LunarEclipse.surface_light` turns those vectors into per-point
  illumination for the renderer: the fraction of the solar disc still visible,
  a colour for the last direct rays, and the refracted glow of the atmosphere
  inside the umbra.

The atmosphere is deliberately more than a black disc.  Light bent around the
Earth's limb keeps the umbra filled with a copper-red glow, and the part that
grazes the ozone layer arrives cyan: that is the **turquoise band** at the
umbral rim.  The brightness used here is an order of magnitude brighter than
the physical one so that the band survives an ordinary 8-bit exposure; it is a
display convention, not a claim about the sky.  The colour is physical: ozone's
Chappuis band absorbs the red end, which is exactly why the band looks
turquoise rather than white.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import numpy as np

from .frames import body_position_moon_frame
from .kernels import kernel_session
from .terrain import MOON_REFERENCE_RADIUS_KM

__all__ = [
    "ECLIPSE_KINDS",
    "LUNAR_ECLIPSE_LABELS",
    "UMBRA_ENLARGEMENT",
    "LunarEclipse",
    "eclipse_state",
]

#: The umbra is stretched by this factor for the atmosphere, as in every
#: almanac's shadow geometry and in :mod:`moonlib.events`.
UMBRA_ENLARGEMENT = 1.02

#: The Sun's radius, km.  The apparent radius at the Moon is derived from it
#: rather than fixed, because the Earth-Moon distance changes by 12 %.
SUN_RADIUS_KM = 695700.0

#: Eclipse classes, in the order the page shows them.  ``none`` is the ordinary
#: Moon and is what almost every instant returns.
ECLIPSE_KINDS: Tuple[str, ...] = ("none", "penumbral", "partial", "total")

#: Display names.  A lunar eclipse *is* 月全食 or 月偏食; "月食 · 偏食" would say
#: the same thing twice.
LUNAR_ECLIPSE_LABELS: Dict[str, Tuple[str, str]] = {
    "none": ("无月食", "No eclipse"),
    "penumbral": ("月半影食", "Penumbral lunar eclipse"),
    "partial": ("月偏食", "Partial lunar eclipse"),
    "total": ("月全食", "Total lunar eclipse"),
}

#: How bright the refracted light is, as a fraction of the direct sunlight at
#: normal incidence.  The physical rim is roughly 1e-3 and the deepest umbra
#: 1e-4; these numbers are ten times that so that a single 8-bit exposure shows
#: the red interior and the cyan rim at the same time.  They are display
#: conventions and are named as such.
REFRACTED_RED_EDGE = 5.0e-3
REFRACTED_TEAL_EDGE = 1.0e-2
REFRACTED_AMBER_EDGE = 1.0e-3
#: The warm colour of the deep umbra, and the ozone blue of the rim.
RED_TINT = (1.0, 0.16, 0.05)
AMBER_TINT = (1.0, 0.42, 0.12)
TEAL_TINT = (0.04, 0.92, 0.80)


def _angular_radius_deg(radius_km: float, distance_km: np.ndarray) -> np.ndarray:
    """Apparent angular radius of a sphere, safe against a zero distance."""
    ratio = np.clip(radius_km / np.maximum(distance_km, 1e-9), 0.0, 1.0)
    return np.degrees(np.arcsin(ratio))


def _disk_overlap_fraction(
    separation: np.ndarray, first: np.ndarray, second: np.ndarray
) -> np.ndarray:
    """Fraction of the *second* disc covered by the first one.

    The standard circle-circle lens area, written for arrays.  ``first`` is the
    Earth (the larger disc) and ``second`` the Sun; the case that matters for an
    eclipse is the Sun being wholly or partly hidden.
    """
    larger = np.maximum(first, second)
    smaller = np.minimum(first, second)
    separation = np.asarray(separation, dtype=np.float64)
    # Disjoint, and one disc wholly inside the other.  ``np.where`` evaluates
    # both branches, so every expression below is guarded against the
    # separation going to zero.
    disjoint = separation >= first + second
    covered = separation <= larger - smaller
    safe = np.maximum(separation, 1e-12)
    cosine_first = np.clip(
        (separation ** 2 + first ** 2 - second ** 2) / (2.0 * safe * first),
        -1.0, 1.0,
    )
    cosine_second = np.clip(
        (separation ** 2 + second ** 2 - first ** 2) / (2.0 * safe * second),
        -1.0, 1.0,
    )
    # Heron's expression for the four-point product; a negative value only
    # appears from rounding on the disjoint boundary.
    radicand = np.maximum(
        (-separation + first + second)
        * (separation + first - second)
        * (separation - first + second)
        * (separation + first + second),
        0.0,
    )
    lens = (
        first ** 2 * np.arccos(cosine_first)
        + second ** 2 * np.arccos(cosine_second)
        - 0.5 * np.sqrt(radicand)
    )
    area_second = math.pi * np.maximum(second, 1e-12) ** 2
    fraction = np.where(disjoint, 0.0, np.where(covered, 1.0, lens / area_second))
    return np.clip(fraction, 0.0, 1.0)


def _smoothstep(values: np.ndarray) -> np.ndarray:
    """Cubic smoothstep on ``[0, 1]``; zero below and one above."""
    x = np.clip(values, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


@dataclass(frozen=True)
class LunarEclipse:
    """The Earth's shadow at one instant, ready for a renderer or a caption."""

    kind: str
    magnitude: float
    """Umbral magnitude: the fraction of the Moon's diameter inside the umbra.

    Greater than one during a total eclipse, which is the almanac's convention.
    """
    penumbral_magnitude: float
    separation_deg: float
    """Angle between the Moon's centre and the shadow axis, as seen from Earth.

    Equal to the Earth-Sun separation as seen from the Moon, which is the way
    the local illumination code measures it.
    """
    moon_radius_deg: float
    umbra_radius_deg: float
    penumbra_radius_deg: float
    earth_angular_radius_deg: float
    sun_angular_radius_deg: float
    moon_distance_km: float
    earth_distance_km: float
    sun_distance_km: float
    moon_radius_km: float
    earth_radius_km: float
    sun_radius_km: float
    earth_position_km: Tuple[float, float, float]
    sun_position_km: Tuple[float, float, float]

    @property
    def active(self) -> bool:
        """Whether the Moon is touching the penumbra at all."""
        return self.kind != "none"

    @property
    def label(self) -> str:
        return LUNAR_ECLIPSE_LABELS.get(self.kind, LUNAR_ECLIPSE_LABELS["none"])[0]

    @property
    def label_en(self) -> str:
        return LUNAR_ECLIPSE_LABELS.get(self.kind, LUNAR_ECLIPSE_LABELS["none"])[1]

    def to_dict(self, *, vectors: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "kind": self.kind,
            "active": self.active,
            "label": self.label,
            "label_en": self.label_en,
            "magnitude": float(self.magnitude),
            "penumbral_magnitude": float(self.penumbral_magnitude),
            "separation_deg": float(self.separation_deg),
            "moon_radius_deg": float(self.moon_radius_deg),
            "umbra_radius_deg": float(self.umbra_radius_deg),
            "penumbra_radius_deg": float(self.penumbra_radius_deg),
            "earth_angular_radius_deg": float(self.earth_angular_radius_deg),
            "sun_angular_radius_deg": float(self.sun_angular_radius_deg),
            "moon_distance_km": float(self.moon_distance_km),
            "earth_distance_km": float(self.earth_distance_km),
            "sun_distance_km": float(self.sun_distance_km),
        }
        if vectors:
            payload["earth_position_km"] = [float(v) for v in self.earth_position_km]
            payload["sun_position_km"] = [float(v) for v in self.sun_position_km]
        return payload

    # -- the local illumination a renderer needs -----------------------
    def surface_light(self, points: np.ndarray) -> Dict[str, np.ndarray]:
        """Illumination at ``(n, 3)`` surface points in ``MOON_ME``, in km.

        Returns ``sun_visible`` (0..1), ``sun_tint`` (``n, 3``), ``refracted``
        (``n, 3``, a fraction of normal-incidence sunlight) and
        ``earth_direction`` (``n, 3``, the unit vector from each point to the
        Earth).  Only the points in the umbra receive ``refracted`` light; the
        penumbra gets a partial direct Sun with a slightly reddened edge.
        """
        points = np.asarray(points, dtype=np.float64)
        earth = np.asarray(self.earth_position_km, dtype=np.float64)[None, :]
        sun = np.asarray(self.sun_position_km, dtype=np.float64)[None, :]
        to_earth = earth - points
        to_sun = sun - points
        earth_distance = np.linalg.norm(to_earth, axis=1)
        sun_distance = np.linalg.norm(to_sun, axis=1)
        earth_unit = to_earth / np.maximum(earth_distance, 1e-12)[:, None]
        sun_unit = to_sun / np.maximum(sun_distance, 1e-12)[:, None]

        earth_angle = _angular_radius_deg(self.earth_radius_km, earth_distance)
        sun_angle = _angular_radius_deg(self.sun_radius_km, sun_distance)
        cosine = np.clip(np.einsum("ij,ij->i", earth_unit, sun_unit), -1.0, 1.0)
        separation = np.degrees(np.arccos(cosine))

        covered = _disk_overlap_fraction(separation, earth_angle, sun_angle)
        sun_visible = 1.0 - covered

        # The umbra's edge, point by point: the Earth's disc minus the Sun's,
        # stretched the conventional 2 %.  ``depth`` runs from 0 at that edge
        # to 1 at the centre of the shadow.
        umbra = np.maximum((earth_angle - sun_angle) * UMBRA_ENLARGEMENT, 1e-9)
        depth = np.clip((umbra - separation) / umbra, 0.0, 1.0)
        # Ramp the atmosphere in just inside the rim rather than switching it
        # on at the geometric boundary: the visible band is a strip, not a
        # step, and the strip is about the Sun's own angular width deep.
        gate = _smoothstep((umbra - separation) / np.maximum(0.25 * sun_angle, 1e-9))

        red = REFRACTED_RED_EDGE * np.exp(-1.5 * depth) * gate
        # The ozone band sits just inside the umbral rim.  During a total
        # eclipse no point of the Moon reaches the rim itself -- the shallowest
        # visible ground is ``(umbra - separation - moon_radius)`` into the
        # shadow -- so the band follows the Moon's own nearest limb instead.
        # Otherwise it would be mathematically present and never once visible,
        # which is the opposite of the effect observers report.
        centre_umbra = max(float(self.umbra_radius_deg), 1e-9)
        depth_floor = float(np.clip(
            (centre_umbra - float(self.separation_deg)
             - float(self.moon_radius_deg)) / centre_umbra,
            0.0, 0.9,
        ))
        teal_centre = depth_floor + (0.035 if self.kind == "total" else 0.035)
        teal_width = 0.04 if self.kind == "total" else 0.025
        teal = REFRACTED_TEAL_EDGE * np.exp(
            -0.5 * ((depth - teal_centre) / teal_width) ** 2
        ) * gate
        amber = REFRACTED_AMBER_EDGE * np.exp(
            -0.5 * ((depth - (teal_centre + 0.16)) / 0.16) ** 2
        ) * gate
        refracted = (
            red[:, None] * np.asarray(RED_TINT)[None, :]
            + amber[:, None] * np.asarray(AMBER_TINT)[None, :]
            + teal[:, None] * np.asarray(TEAL_TINT)[None, :]
        )

        # The last direct rays graze the atmosphere, so just outside the umbra
        # they are noticeably redder than the Sun's own colour.
        near_edge = np.where(
            separation > umbra,
            np.exp(-0.5 * ((separation - umbra) / np.maximum(0.18 * sun_angle, 1e-9)) ** 2),
            0.0,
        )
        sun_tint = np.stack(
            (
                np.ones_like(near_edge),
                1.0 - 0.25 * near_edge,
                1.0 - 0.45 * near_edge,
            ),
            axis=1,
        )
        # Diffraction and refraction blur the edge by roughly the Sun's
        # angular size; keeping the direct and refracted terms disjoint avoids
        # a bright seam exactly where they meet.
        refracted = refracted * (sun_visible < 1e-3)[:, None]
        return {
            "sun_visible": sun_visible,
            "sun_tint": sun_tint,
            "refracted": refracted,
            "earth_direction": earth_unit,
            "depth": depth,
        }


def eclipse_state(et: float) -> LunarEclipse:
    """Classify the Earth's shadow at ephemeris time *et*.

    The Earth and Sun are placed in ``MOON_ME`` so the renderer can measure the
    shadow from the *surface*: across the Moon the direction to the Earth moves
    by its own angular radius (~0.95 deg), which is larger than the whole umbra.
    A single geocentric separation would put the shadow in the wrong place.
    """
    session = kernel_session()
    session.require_epoch(float(et), what="eclipse geometry")
    constants = session.constants
    earth = body_position_moon_frame("EARTH", float(et))
    sun = body_position_moon_frame("SUN", float(et))
    earth_distance = float(np.linalg.norm(earth))
    sun_distance = float(np.linalg.norm(sun))
    if earth_distance <= 0.0 or sun_distance <= 0.0:  # pragma: no cover
        raise ValueError("the Earth and Sun cannot both be at the Moon's centre")

    moon_radius_km = float(constants.moon_radius_km)
    earth_radius_km = float(constants.earth_radius_km)
    moon_radius = math.degrees(math.asin(min(1.0, moon_radius_km / earth_distance)))
    earth_angle = math.degrees(math.asin(min(1.0, earth_radius_km / earth_distance)))
    sun_angle = math.degrees(math.asin(min(1.0, SUN_RADIUS_KM / sun_distance)))
    cosine = float(
        np.clip(np.dot(earth / earth_distance, sun / sun_distance), -1.0, 1.0)
    )
    separation = math.degrees(math.acos(cosine))

    umbra = max(0.0, (earth_angle - sun_angle) * UMBRA_ENLARGEMENT)
    penumbra = earth_angle + sun_angle
    if separation <= umbra - moon_radius:
        kind = "total"
    elif separation <= umbra + moon_radius:
        kind = "partial"
    elif separation <= penumbra + moon_radius:
        kind = "penumbral"
    else:
        kind = "none"

    umbral_magnitude = (umbra + moon_radius - separation) / (2.0 * moon_radius)
    penumbral_magnitude = (
        (penumbra + moon_radius - separation) / (2.0 * moon_radius)
    )
    return LunarEclipse(
        kind=kind,
        magnitude=max(0.0, float(umbral_magnitude)),
        penumbral_magnitude=max(0.0, float(penumbral_magnitude)),
        separation_deg=float(separation),
        moon_radius_deg=float(moon_radius),
        umbra_radius_deg=float(umbra),
        penumbra_radius_deg=float(penumbra),
        earth_angular_radius_deg=float(earth_angle),
        sun_angular_radius_deg=float(sun_angle),
        moon_distance_km=float(earth_distance),
        earth_distance_km=float(earth_distance),
        sun_distance_km=float(sun_distance),
        moon_radius_km=moon_radius_km,
        earth_radius_km=earth_radius_km,
        sun_radius_km=SUN_RADIUS_KM,
        earth_position_km=tuple(float(v) for v in earth),
        sun_position_km=tuple(float(v) for v in sun),
    )
