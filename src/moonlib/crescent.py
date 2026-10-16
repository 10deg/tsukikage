"""Thin-crescent observing windows, visibility criteria and quality scoring.

What this module answers
------------------------
"On which evenings or mornings in the coming months will the crescent be
photographable from my site, and when exactly is the best moment?"

Screening versus judging
------------------------
Two separate ideas are deliberately kept apart:

**Screening** (:class:`CrescentCriteria`) is a purely geometric envelope — the
Sun between −8° and +6°, the Moon at least 2° up, illumination between 0.05 %
and 25 %.  It is what the legacy library used, and it is what decides which
instants become candidate sessions.

**Judging** (:func:`photo_score` and the published criteria) ranks those
candidates.  The score is a weighted sum of piecewise-linear penalties with
explicit "sweet bands" (illumination 1–8 %, elongation 7–20°, Moon 5–20° up,
Sun −7° to −3°).  The empirical Danjon / Odeh / Yallop criteria are reported as
*advisory verdicts* alongside every session; they are **not** used to filter,
because they are published empirical models whose applicability varies with
site and observer.

Determinism
-----------
The legacy scorer saturated at 0 over wide bands, so its ranking — and its
"best moment" — were arbitrary among ties, and it reselected the earliest
instant by construction.  Here ties are broken explicitly:
``|sun elevation - sweet centre|`` first, then the higher Moon, and the best
moment is re-refined onto the Sun-elevation sweet centre with a bisection when
that target is reachable.  Two runs on the same inputs therefore always return
the same instant.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .errors import MoonlibError
from .frames import angular_separation, az_el, position_angle, topocentric_vector
from .illumination import (
    PhaseMode,
    angular_diameter_arcsec,
    crescent_width_arcsec,
    geocentric_phase_angle_deg,
    illuminated_fraction,
    scan_geometry,
    topocentric_phase_angle_deg,
)
from .models import CrescentGeometry, CrescentWindow
from .numerics import bisect_root, wrap_angle
from .observer import Observer
from .phases import find_new_moons, moon_age
from .timeutil import SYNODIC_MONTH_DAYS, TimeRange, as_time_range, to_utc, utc_now

__all__ = [
    "CrescentCriteria",
    "ScoreWeights",
    "CriterionVerdict",
    "DANJON_LIMIT_DEG",
    "danjon_criterion",
    "odeh_criterion",
    "yallop_criterion",
    "crescent_geometry",
    "photo_score",
    "crescent_visibility",
    "find_crescent_windows",
]

#: Danjon limit: the minimum Sun-Moon elongation (degrees) at which a crescent
#: has ever been reported with the naked eye (Danjon 1932).  It is an empirical
#: constant, not a derived quantity.
DANJON_LIMIT_DEG = 7.0


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CrescentCriteria:
    """Geometric screening envelope for candidate crescent sessions."""

    sun_elevation_min_deg: float = -8.0
    sun_elevation_max_deg: float = 6.0
    moon_elevation_min_deg: float = 2.0
    illumination_min_percent: float = 0.05
    illumination_max_percent: float = 25.0
    waxing: Optional[bool] = None
    """``None`` for both evening (waxing) and morning (waning) sessions."""

    scan_days_around_new_moon: float = 3.0
    step_minutes: float = 5.0
    refine: bool = True

    def contains(
        self,
        sun_elevation_deg: float,
        moon_elevation_deg: float,
        illumination_percent: float,
        moon_age_days: float,
    ) -> bool:
        if not (
            self.sun_elevation_min_deg
            <= sun_elevation_deg
            <= self.sun_elevation_max_deg
        ):
            return False
        if moon_elevation_deg < self.moon_elevation_min_deg:
            return False
        if not (
            self.illumination_min_percent
            <= illumination_percent
            <= self.illumination_max_percent
        ):
            return False
        if self.waxing is not None and (moon_age_days >= 0.0) != self.waxing:
            return False
        return True


@dataclass(frozen=True)
class ScoreWeights:
    """Piecewise-linear penalty weights for :func:`photo_score`.

    Every field maps one-to-one onto a segment of the legacy scoring function,
    so behaviour is preserved while the numbers become inspectable and tunable.
    Lower total score is better; the theoretical minimum is 0.
    """

    # -- illuminated percentage -------------------------------------------
    illum_sweet_low: float = 1.0
    illum_sweet_high: float = 8.0
    illum_hard_low: float = 0.5
    illum_mid_high: float = 15.0
    illum_slope_very_low: float = 20.0
    illum_slope_low: float = 5.0
    illum_slope_mid: float = 0.5
    illum_slope_high: float = 1.0
    illum_high_offset: float = 3.5

    # -- elongation --------------------------------------------------------
    elong_sweet_low: float = 7.0
    elong_sweet_high: float = 20.0
    elong_hard_low: float = 5.0
    elong_high: float = 30.0
    elong_slope_very_low: float = 3.0
    elong_slope_low: float = 2.0
    elong_slope_high: float = 0.3
    elong_slope_very_high: float = 0.5
    elong_high_offset: float = 3.0

    # -- Moon elevation ----------------------------------------------------
    moon_sweet_low: float = 5.0
    moon_sweet_high: float = 20.0
    moon_hard_low: float = 2.0
    moon_high: float = 30.0
    moon_slope_very_low: float = 10.0
    moon_slope_low: float = 3.0
    moon_slope_high: float = 0.2
    moon_slope_very_high: float = 0.3
    moon_high_offset: float = 2.0

    # -- Sun elevation -----------------------------------------------------
    sun_sweet_low: float = -7.0
    sun_sweet_high: float = -3.0
    sun_low: float = -8.0
    sun_slope_below: float = 1.5
    sun_slope_mid: float = 1.0
    sun_slope_above: float = 2.0
    sun_low_offset: float = 2.0

    @property
    def sun_sweet_centre(self) -> float:
        """Mid-point of the Sun-elevation sweet band (the refinement target)."""
        return 0.5 * (self.sun_sweet_low + self.sun_sweet_high)

    def as_dict(self) -> Dict[str, float]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


#: Library default weights (identical to the legacy ``photo_score_point``).
DEFAULT_WEIGHTS = ScoreWeights()


@dataclass(frozen=True)
class CriterionVerdict:
    """The verdict of one published crescent visibility criterion."""

    name: str
    value: float
    verdict: str
    description: str
    reference: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "verdict": self.verdict,
            "description": self.description,
            "reference": self.reference,
        }


# ---------------------------------------------------------------------------
# published empirical criteria (advisory)
# ---------------------------------------------------------------------------
def danjon_criterion(
    arc_of_light_deg: float, limit_deg: float = DANJON_LIMIT_DEG
) -> CriterionVerdict:
    """Danjon limit: a crescent is not expected below ~7° of elongation."""
    if arc_of_light_deg < limit_deg:
        verdict = "not_visible"
        description = (
            f"ARCL {arc_of_light_deg:.2f} deg is below the Danjon limit "
            f"{limit_deg:.1f} deg; the crescent is not expected to be seen."
        )
    else:
        verdict = "visible"
        description = (
            f"ARCL {arc_of_light_deg:.2f} deg exceeds the Danjon limit "
            f"{limit_deg:.1f} deg."
        )
    return CriterionVerdict(
        name="danjon",
        value=arc_of_light_deg,
        verdict=verdict,
        description=description,
        reference="Danjon (1932), empirical limit of about 7 deg elongation",
    )


def odeh_criterion(
    arc_of_vision_deg: float, arc_of_light_deg: float
) -> CriterionVerdict:
    """Odeh's (2006) visibility parameter ``V``.

    With the normalised arc of light ``W = ARCL / 10`` (degrees),

    ``V = ARCV - (-0.1018*W^3 + 0.7319*W^2 - 6.3226*W + 7.1651)``

    Reported bands: ``V >= 5.65`` easily visible, ``2.00 .. 5.65`` visible under
    perfect conditions, ``-0.96 .. 2.00`` may need optical aid,
    ``-1.85 .. -0.96`` will need optical aid, below ``-1.85`` not visible.

    .. note::
        The normalisation matters.  The published polynomial is a fit in
        ``W = ARCL/10``, exactly like Yallop's ``q``; feeding it ARCL in degrees
        makes the cubic term dominate and yields values in the hundreds — which
        is how this implementation was caught during verification.  With the
        correct normalisation ``V`` lands in the 1-20 range for real crescent
        geometries, consistent with ``q`` computed from the same inputs.

    This is a published empirical model reproduced for advisory use; it is not
    used to filter sessions.
    """
    w = float(arc_of_light_deg) / 10.0
    threshold = -0.1018 * w ** 3 + 0.7319 * w ** 2 - 6.3226 * w + 7.1651
    value = float(arc_of_vision_deg) - threshold
    if value >= 5.65:
        verdict, label = "visible", "easily visible"
    elif value >= 2.00:
        verdict, label = "visible", "visible under perfect conditions"
    elif value >= -0.96:
        verdict, label = "marginal", "may need optical aid to find"
    elif value >= -1.85:
        verdict, label = "marginal", "will need optical aid"
    else:
        verdict, label = "not_visible", "not visible with a telescope"
    return CriterionVerdict(
        name="odeh",
        value=value,
        verdict=verdict,
        description=f"Odeh V = {value:+.2f} ({label})",
        reference="Odeh (2006), Experimental Astronomy 18, 39",
    )


def yallop_criterion(
    arc_of_vision_deg: float, arc_of_light_deg: float
) -> CriterionVerdict:
    """Yallop's (1997) visibility parameter ``q``.

    ``q = (ARCV - (11.8371 - 6.3226*W + 0.7319*W^2 - 0.1018*W^3)) / 10`` with
    ``W = ARCL / 10`` in degrees.  Reported bands: ``q > 0.216`` easily
    visible, ``-0.014 .. 0.216`` visible under perfect conditions,
    ``-0.160 .. -0.014`` may need optical aid, ``-0.232 .. -0.160`` needs
    optical aid, ``-0.293 .. -0.232`` not visible with CCD, below ``-0.293``
    not visible.

    Reproduced for advisory use; not used to filter sessions.
    """
    w = float(arc_of_light_deg) / 10.0
    threshold = 11.8371 - 6.3226 * w + 0.7319 * w ** 2 - 0.1018 * w ** 3
    value = (float(arc_of_vision_deg) - threshold) / 10.0
    if value > 0.216:
        verdict, label = "visible", "easily visible"
    elif value > -0.014:
        verdict, label = "visible", "visible under perfect conditions"
    elif value > -0.160:
        verdict, label = "marginal", "may need optical aid"
    elif value > -0.232:
        verdict, label = "marginal", "will need optical aid"
    elif value > -0.293:
        verdict, label = "not_visible", "not visible with a CCD"
    else:
        verdict, label = "not_visible", "not visible"
    return CriterionVerdict(
        name="yallop",
        value=value,
        verdict=verdict,
        description=f"Yallop q = {value:+.3f} ({label})",
        reference="Yallop (1997), NAO Technical Note 69",
    )


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------
def crescent_geometry(
    et: float, observer: Observer, *, new_moon_et: Optional[float] = None
) -> CrescentGeometry:
    """Full crescent geometry at one instant for one observer.

    Parameters
    ----------
    et
        Ephemeris time.
    observer
        Observing site.
    new_moon_et
        When the associated new-moon instant is already known, pass it: the
        signed lunar age is then simple arithmetic instead of a root search,
        which matters because this function is called for every candidate
        sample of every session.
    """
    observer = Observer.from_value(observer)
    moon_horizontal = az_el("MOON", et, observer)
    sun_horizontal = az_el("SUN", et, observer)

    moon_vector, _ = topocentric_vector("MOON", et, observer)
    sun_vector, _ = topocentric_vector("SUN", et, observer)
    elongation = math.degrees(angular_separation(moon_vector, sun_vector))

    # Bright limb: position angle of the Sun as seen from the Moon.  The legacy
    # code used atan2(delta_elevation, delta_azimuth), which ignores that the
    # azimuth/elevation plane is not orthonormal away from the equator.
    limb_angle = math.degrees(position_angle(moon_vector, sun_vector)) % 360.0

    # Illumination is computed from the *topocentric* phase angle for the
    # observer-facing value and from the geocentric one for the conventional
    # value; both use the same spherical-illumination relation.
    geo_angle = geocentric_phase_angle_deg(et)
    topo_angle = topocentric_phase_angle_deg(et, observer)
    geo_percent = illuminated_fraction(geo_angle) * 100.0
    topo_percent = illuminated_fraction(topo_angle) * 100.0

    diameter = angular_diameter_arcsec(moon_horizontal.range_km)

    return CrescentGeometry(
        elongation_deg=elongation,
        arc_of_light_deg=elongation,
        arc_of_vision_deg=moon_horizontal.elevation_deg - sun_horizontal.elevation_deg,
        sun_elevation_deg=sun_horizontal.elevation_deg,
        moon_elevation_deg=moon_horizontal.elevation_deg,
        moon_azimuth_deg=moon_horizontal.azimuth_deg,
        sun_azimuth_deg=sun_horizontal.azimuth_deg,
        relative_azimuth_deg=math.degrees(
            wrap_angle(
                math.radians(moon_horizontal.azimuth_deg - sun_horizontal.azimuth_deg)
            )
        ),
        illumination_percent_geo=geo_percent,
        illumination_percent_topo=topo_percent,
        phase_angle_geo_deg=geo_angle,
        phase_angle_topo_deg=topo_angle,
        angular_diameter_arcsec=diameter,
        crescent_width_arcsec=crescent_width_arcsec(topo_percent / 100.0, diameter),
        bright_limb_angle_deg=limb_angle,
        moon_age_days=_signed_age_days(et, new_moon_et),
    )


def _signed_age_days(et: float, new_moon_et: Optional[float] = None) -> float:
    """Days relative to the associated new moon, negative before it."""
    if new_moon_et is not None:
        return (et - new_moon_et) / 86400.0
    age = moon_age(et)
    if age.age_days <= 0.5 * SYNODIC_MONTH_DAYS:
        return age.age_days
    return age.age_days - SYNODIC_MONTH_DAYS


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------
def _band_penalty(
    value: float,
    sweet_low: float,
    sweet_high: float,
    hard_low: Optional[float],
    hard_high: Optional[float],
    slope_below: float,
    slope_low: float,
    slope_high: float,
    slope_above: float,
    high_offset: float,
) -> float:
    """Piecewise-linear penalty: zero inside the sweet band."""
    if sweet_low <= value <= sweet_high:
        return 0.0
    if value < sweet_low:
        if hard_low is not None and value < hard_low:
            return (hard_low - value) * slope_below + (sweet_low - hard_low) * slope_low
        return (sweet_low - value) * slope_low
    if hard_high is not None and value > hard_high:
        return (
            (value - hard_high) * slope_above
            + (hard_high - sweet_high) * slope_high
            + high_offset
        )
    return (value - sweet_high) * slope_high


def photo_score(
    geometry: CrescentGeometry,
    weights: ScoreWeights = DEFAULT_WEIGHTS,
    phase_mode: "PhaseMode | str" = PhaseMode.GEOCENTRIC,
) -> float:
    """Photographic quality score for one instant; **lower is better**.

    The four contributions are illumination, elongation, Moon elevation and
    Sun elevation, each penalised piecewise-linearly outside its sweet band.

    Parameters
    ----------
    geometry
        Output of :func:`crescent_geometry`.
    weights
        Band edges and slopes; see :class:`ScoreWeights`.
    phase_mode
        Which illumination to score against (``'geocentric'`` or
        ``'topocentric'``).
    """
    illumination = geometry.illumination(phase_mode)
    elongation = geometry.elongation_deg
    moon = geometry.moon_elevation_deg
    sun = geometry.sun_elevation_deg

    illumination_penalty = _band_penalty(
        illumination,
        weights.illum_sweet_low,
        weights.illum_sweet_high,
        weights.illum_hard_low,
        weights.illum_mid_high,
        weights.illum_slope_very_low,
        weights.illum_slope_low,
        weights.illum_slope_mid,
        weights.illum_slope_high,
        weights.illum_high_offset,
    )
    elongation_penalty = _band_penalty(
        elongation,
        weights.elong_sweet_low,
        weights.elong_sweet_high,
        weights.elong_hard_low,
        weights.elong_high,
        weights.elong_slope_very_low,
        weights.elong_slope_low,
        weights.elong_slope_high,
        weights.elong_slope_very_high,
        weights.elong_high_offset,
    )
    moon_penalty = _band_penalty(
        moon,
        weights.moon_sweet_low,
        weights.moon_sweet_high,
        weights.moon_hard_low,
        weights.moon_high,
        weights.moon_slope_very_low,
        weights.moon_slope_low,
        weights.moon_slope_high,
        weights.moon_slope_very_high,
        weights.moon_high_offset,
    )

    # Sun elevation is inverted: "below the sweet band" means the sky is too
    # bright, and there is a separate penalty for the Sun still being up.
    if weights.sun_sweet_low <= sun <= weights.sun_sweet_high:
        sun_penalty = 0.0
    elif sun < weights.sun_sweet_low:
        if sun < weights.sun_low:
            sun_penalty = (
                abs(sun + weights.sun_low) * weights.sun_slope_below
                + abs(weights.sun_low - weights.sun_sweet_low) * weights.sun_slope_mid
                + weights.sun_low_offset
            )
        else:
            sun_penalty = abs(sun - weights.sun_sweet_low) * weights.sun_slope_mid
    else:
        sun_penalty = (sun - weights.sun_sweet_high) * weights.sun_slope_above

    return float(
        illumination_penalty + elongation_penalty + moon_penalty + sun_penalty
    )


def crescent_visibility(
    geometry: CrescentGeometry,
    *,
    include: Sequence[str] = ("danjon", "odeh", "yallop"),
) -> Dict[str, Dict[str, Any]]:
    """Advisory verdicts from the published empirical criteria.

    Returns a mapping of criterion name to :meth:`CriterionVerdict.to_dict`.
    These verdicts are informative only — :func:`find_crescent_windows` never
    filters on them.
    """
    verdicts: Dict[str, CriterionVerdict] = {}
    lookup = {
        "danjon": lambda: danjon_criterion(geometry.arc_of_light_deg),
        "odeh": lambda: odeh_criterion(
            geometry.arc_of_vision_deg, geometry.arc_of_light_deg
        ),
        "yallop": lambda: yallop_criterion(
            geometry.arc_of_vision_deg, geometry.arc_of_light_deg
        ),
    }
    for name in include:
        factory = lookup.get(name)
        if factory is None:
            raise MoonlibError(
                f"Unknown crescent criterion {name!r}; "
                f"choose from {', '.join(sorted(lookup))}"
            )
        verdicts[name] = factory()
    return {name: verdict.to_dict() for name, verdict in verdicts.items()}


# ---------------------------------------------------------------------------
# window search
# ---------------------------------------------------------------------------
def find_crescent_windows(
    observer: Observer,
    time_range=None,
    *,
    criteria: Optional[CrescentCriteria] = None,
    weights: ScoreWeights = DEFAULT_WEIGHTS,
    phase_mode: "PhaseMode | str" = PhaseMode.GEOCENTRIC,
    include_criteria: Sequence[str] = ("danjon", "odeh", "yallop"),
) -> List[CrescentWindow]:
    """Find every crescent observing session in a period.

    Parameters
    ----------
    observer
        Observing site.
    time_range
        Anything accepted by :func:`moonlib.timeutil.as_time_range`; defaults
        to now plus 365 days.
    criteria
        Screening envelope; see :class:`CrescentCriteria`.
    weights
        Scoring weights used for ranking and for picking the best moment.
    phase_mode
        Which illumination drives filtering and scoring.
    include_criteria
        Which advisory criteria to attach to each window.

    Returns
    -------
    list of CrescentWindow
        Chronological.  Each window carries the best instant, its full
        geometry, the photo score, the advisory verdicts and the range of
        conditions seen during the session.
    """
    from collections import defaultdict

    observer = Observer.from_value(observer)
    criteria = criteria or CrescentCriteria()
    mode = phase_mode if isinstance(phase_mode, PhaseMode) else PhaseMode(phase_mode)

    if time_range is None:
        window = TimeRange.utc_days(utc_now(), 365.0)
    else:
        window = as_time_range(time_range)

    margin = criteria.scan_days_around_new_moon * 86400.0
    search_range = TimeRange(window.start_et - margin, window.end_et + margin)
    new_moons = find_new_moons(search_range)
    if not new_moons:
        return []

    step_seconds = criteria.step_minutes * 60.0
    samples: List[Dict[str, Any]] = []

    for index, new_moon in enumerate(new_moons):
        t_start = new_moon.et - margin
        t_end = new_moon.et + margin
        grid = TimeRange(t_start, t_end).grid(step_seconds)
        grid = grid[(grid >= window.start_et) & (grid <= window.end_et)]
        if grid.size == 0:
            continue

        scan = scan_geometry(grid, observer, with_horizontal=True)
        illumination = scan.illumination(mode)
        ages = (grid - new_moon.et) / 86400.0

        for position in range(grid.size):
            age = float(ages[position])
            if not criteria.contains(
                float(scan.sun_elevation_deg[position]),
                float(scan.elevation_deg[position]),
                float(illumination[position]) * 100.0,
                age,
            ):
                continue
            samples.append(
                {
                    "et": float(grid[position]),
                    "new_moon_et": new_moon.et,
                    "new_moon_utc": new_moon.when_utc,
                    "new_moon_index": index,
                    "age_days": age,
                    "period": "evening" if age > 0.0 else "morning",
                    "sun_elevation_deg": float(scan.sun_elevation_deg[position]),
                    "moon_elevation_deg": float(scan.elevation_deg[position]),
                    "illumination_percent": float(illumination[position]) * 100.0,
                }
            )

    if not samples:
        return []

    grouped: Dict[Tuple[int, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        local = observer.local_time(to_utc(sample["et"]))
        grouped[
            (sample["new_moon_index"], sample["period"], local.strftime("%Y-%m-%d"))
        ].append(sample)

    windows: List[CrescentWindow] = []
    for (_, period, local_date), group in grouped.items():
        best_sample, best_geometry, best_score = _select_best(
            group, observer, criteria, weights, mode
        )
        windows.append(
            _build_window(
                best_sample,
                best_geometry,
                best_score,
                group,
                observer,
                period,
                local_date,
                mode,
                include_criteria,
            )
        )

    windows.sort(key=lambda item: item.best_utc)
    return windows


def _select_best(
    group: List[Dict[str, Any]],
    observer: Observer,
    criteria: CrescentCriteria,
    weights: ScoreWeights,
    mode: PhaseMode,
) -> Tuple[Dict[str, Any], CrescentGeometry, float]:
    """Pick — and optionally refine — the best instant of one session."""
    new_moon_et = group[0]["new_moon_et"]
    best_sample = group[0]
    best_geometry = crescent_geometry(
        best_sample["et"], observer, new_moon_et=new_moon_et
    )
    best_key = _score_tiebreak_mode(best_geometry, weights, mode)

    for sample in group[1:]:
        geometry = crescent_geometry(sample["et"], observer, new_moon_et=new_moon_et)
        key = _score_tiebreak_mode(geometry, weights, mode)
        if key < best_key:
            best_key, best_sample, best_geometry = key, sample, geometry

    if not criteria.refine:
        return best_sample, best_geometry, best_key[0]

    refined_et = _refine_best_et(best_sample, observer, criteria, weights, mode)
    if refined_et is not None:
        refined_geometry = crescent_geometry(
            refined_et, observer, new_moon_et=new_moon_et
        )
        refined_key = _score_tiebreak_mode(refined_geometry, weights, mode)
        if refined_key <= best_key:
            refined_sample = dict(best_sample)
            refined_sample["et"] = refined_et
            return refined_sample, refined_geometry, refined_key[0]

    return best_sample, best_geometry, best_key[0]


def _score_tiebreak_mode(
    geometry: CrescentGeometry, weights: ScoreWeights, mode: PhaseMode
) -> Tuple[float, float, float]:
    score = photo_score(geometry, weights, mode)
    return (
        score,
        abs(geometry.sun_elevation_deg - weights.sun_sweet_centre),
        -geometry.moon_elevation_deg,
    )


def _refine_best_et(
    sample: Dict[str, Any],
    observer: Observer,
    criteria: CrescentCriteria,
    weights: ScoreWeights,
    mode: PhaseMode,
) -> Optional[float]:
    """Land the best moment exactly on the Sun-elevation sweet centre.

    Inside the sweet bands the score is flat, so the "best instant" would
    otherwise be an arbitrary point of a plateau.  Solving
    ``sun_elevation(t) = sweet centre`` inside the coarse step gives a
    reproducible, physically meaningful choice: the moment the sky is exactly
    as dark as the scorer prefers.
    """
    target = weights.sun_sweet_centre
    half_step = criteria.step_minutes * 60.0 * 0.5
    low = sample["et"] - half_step
    high = sample["et"] + half_step

    def residual(et: float) -> float:
        return az_el("SUN", et, observer).elevation_deg - target

    try:
        low_value = residual(low)
        high_value = residual(high)
    except Exception:  # pragma: no cover - defensive
        return None
    if low_value == 0.0:
        return low
    if high_value == 0.0:
        return high
    if low_value * high_value > 0.0:
        return None
    try:
        candidate = bisect_root(residual, low, high, tol=1e-3)
    except ValueError:  # pragma: no cover - defensive
        return None

    geometry = crescent_geometry(
        candidate, observer, new_moon_et=sample["new_moon_et"]
    )
    if not criteria.contains(
        geometry.sun_elevation_deg,
        geometry.moon_elevation_deg,
        geometry.illumination(mode),
        sample["age_days"] + (candidate - sample["et"]) / 86400.0,
    ):
        return None
    return candidate


def _build_window(
    best_sample: Dict[str, Any],
    geometry: CrescentGeometry,
    score: float,
    group: List[Dict[str, Any]],
    observer: Observer,
    period: str,
    local_date: str,
    mode: PhaseMode,
    include_criteria: Sequence[str],
) -> CrescentWindow:
    best_utc = to_utc(best_sample["et"])
    # Derive the date label from the *refined* best instant rather than from the
    # group key: the refinement can move the moment by up to half a coarse step,
    # which occasionally crosses local midnight, and a label that disagreed with
    # best_local would be a trap for any consumer.
    local_date = observer.local_time(best_utc).strftime("%Y-%m-%d")
    sun_range = (
        min(item["sun_elevation_deg"] for item in group),
        max(item["sun_elevation_deg"] for item in group),
    )
    moon_range = (
        min(item["moon_elevation_deg"] for item in group),
        max(item["moon_elevation_deg"] for item in group),
    )
    return CrescentWindow(
        period=period,
        local_date=local_date,
        best_utc=best_utc,
        best_local=observer.local_time(best_utc),
        new_moon_utc=best_sample["new_moon_utc"],
        age_days=(best_sample["et"] - best_sample["new_moon_et"]) / 86400.0,
        geometry=geometry,
        photo_score=score,
        visibility=crescent_visibility(geometry, include=include_criteria),
        candidate_points=len(group),
        moon_elevation_range_deg=moon_range,
        sun_elevation_range_deg=sun_range,
    )
