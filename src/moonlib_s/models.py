"""Immutable result models.

Every public computation returns one of these frozen dataclasses instead of a
bare ``dict``.  Benefits:

* field names are part of a documented, testable contract;
* results cannot be mutated accidentally between the computation layer and the
  presentation layer;
* :meth:`to_dict` produces a **JSON-safe** payload (ISO-8601 strings, plain
  floats, no ``datetime`` / ``numpy`` / ``ZoneInfo`` objects), which is the
  single boundary the future HTTP API and mobile app will consume.

All datetimes stored here are timezone-aware **UTC**.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from .errors import MoonlibError
from .observer import Observer
from .timeutil import SYNODIC_MONTH_DAYS

__all__ = [
    "PhaseKind",
    "PhaseMode",
    "jsonable",
    "MoonPhase",
    "Libration",
    "OrbitElements",
    "SurfaceIllumination",
    "MoonState",
    "RiseSetEvents",
    "TransitDetails",
    "MoonReport",
    "PhaseEvent",
    "PhaseCatalogEntry",
    "IlluminationMatch",
    "CrescentGeometry",
    "CrescentWindow",
    "SunState",
    "AnalemmaPoint",
    "LimbTarget",
    "TargetVisibility",
    "PhotometricModel",
    "ToneMapping",
    "as_photometric_model",
    "as_tone_mapping",
    "MoonView",
    "MoonRegion",
    "SurfaceFeature",
    "MoonRenderSettings",
    "MoonRender",
]


# ---------------------------------------------------------------------------
# generic serialisation helper
# ---------------------------------------------------------------------------
def _fields_dict(obj: Any) -> Dict[str, Any]:
    """Plain dict of a dataclass' declared fields, recursively serialised.

    Unlike :func:`jsonable` this never dispatches back to ``obj.to_dict``, so a
    model can safely build its own payload from it without recursing.
    """
    return {f.name: jsonable(getattr(obj, f.name)) for f in fields(obj)}


def _extend(payload: Dict[str, Any], *args: Any, **values: Any) -> Dict[str, Any]:
    """Merge derived fields into *payload*, serialising them as they go in.

    ``payload.update(...)`` is how every ``to_dict`` adds the convenience fields
    that are computed properties rather than stored ones -- percentages,
    labels, ratios.  Those values never pass through :func:`jsonable`, so they
    used to reach the JSON encoder raw, and a property that is legitimately
    ``nan`` (no terrain, so no terrain pixel size) or ``inf`` (no earthshine, so
    an infinite magnitude drop) then produced a payload that no strict parser
    would accept.  Going through here makes the derived half of a payload obey
    the same rule as the stored half.
    """
    for mapping in args:
        payload.update({str(k): jsonable(v) for k, v in mapping.items()})
    payload.update({str(k): jsonable(v) for k, v in values.items()})
    return payload


def jsonable(value: Any) -> Any:
    """Recursively convert *value* into JSON-serialisable primitives.

    Handles datetimes, enums, dataclasses, mappings, sequences, NumPy scalars
    and arrays.  Dataclasses that define ``to_dict`` are serialised through it,
    so derived convenience fields (percentages, labels) appear in the payload.
    The service layer relies on this so that adding a field to a model can
    never silently break serialisation.

    A non-finite float becomes ``None``.  ``NaN`` is how a model says "not
    applicable here" -- an unlit region has no incidence angle, and a geometry
    query made without terrain has no terrain pixel size -- and the Python-side
    convention is that such a field stays a float, so callers can keep testing
    it with ``x != x``.  JSON cannot write that value: ``json.dumps`` either
    emits the bare token ``NaN``, which is not JSON and which a strict parser
    rejects, or refuses outright.  So the sentinel is translated exactly once,
    here, at the boundary where Python values stop and the payload begins.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, datetime):
        return _iso(value)
    if is_dataclass(value) and not isinstance(value, type):
        to_dict = getattr(type(value), "to_dict", None)
        if callable(to_dict):
            return jsonable(to_dict(value))
        return _fields_dict(value)
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    if hasattr(value, "as_dict") and callable(value.as_dict):
        return jsonable(value.as_dict())
    if hasattr(value, "tolist"):  # numpy arrays and scalars
        return jsonable(value.tolist())
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _iso(value: Optional[datetime]) -> Optional[str]:
    """ISO-8601 string with millisecond precision, or ``None``."""
    if value is None:
        return None
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# phase conventions
# ---------------------------------------------------------------------------
class PhaseMode(str, enum.Enum):
    """Which phase convention drives illumination, filtering and scoring.

    ``GEOCENTRIC``
        The Sun-Moon-Earth-centre angle, i.e. the conventional almanac value.
    ``TOPOCENTRIC``
        The Sun-Moon-observer angle, i.e. what this observer actually sees.
        They differ by up to about a degree when the Moon is near the horizon,
        which is exactly when crescent visibility is decided.

    Defined here, in the shared vocabulary module, because the frozen result
    models expose mode-dependent accessors and must not depend on
    :mod:`moonlib_s.illumination`.
    """

    GEOCENTRIC = "geocentric"
    TOPOCENTRIC = "topocentric"

    @property
    def name_zh(self) -> str:
        return "地心" if self is PhaseMode.GEOCENTRIC else "站心"

    def select(self, geocentric_value: float, topocentric_value: float) -> float:
        """Pick the value matching this mode."""
        return geocentric_value if self is PhaseMode.GEOCENTRIC else topocentric_value


def as_phase_mode(value: "PhaseMode | str") -> PhaseMode:
    """Coerce a string or enum into a :class:`PhaseMode`."""
    return value if isinstance(value, PhaseMode) else PhaseMode(value)


# ---------------------------------------------------------------------------
# phase kinds
# ---------------------------------------------------------------------------
class PhaseKind(str, enum.Enum):
    """The four principal lunar phases."""

    NEW_MOON = "new_moon"
    FIRST_QUARTER = "first_quarter"
    FULL_MOON = "full_moon"
    LAST_QUARTER = "last_quarter"

    @property
    def name_en(self) -> str:
        return _PHASE_EN[self]

    @property
    def name_zh(self) -> str:
        return _PHASE_ZH[self]

    @property
    def label(self) -> str:
        """Bilingual label, e.g. ``'满月 / Full Moon'``."""
        return f"{self.name_zh} / {self.name_en}"

    @property
    def symbol(self) -> str:
        return _PHASE_SYMBOL[self]


_PHASE_EN = {
    PhaseKind.NEW_MOON: "New Moon",
    PhaseKind.FIRST_QUARTER: "First Quarter",
    PhaseKind.FULL_MOON: "Full Moon",
    PhaseKind.LAST_QUARTER: "Last Quarter",
}
_PHASE_ZH = {
    PhaseKind.NEW_MOON: "朔",
    PhaseKind.FIRST_QUARTER: "上弦",
    PhaseKind.FULL_MOON: "望",
    PhaseKind.LAST_QUARTER: "下弦",
}
_PHASE_SYMBOL = {
    PhaseKind.NEW_MOON: "NM",
    PhaseKind.FIRST_QUARTER: "FQ",
    PhaseKind.FULL_MOON: "FM",
    PhaseKind.LAST_QUARTER: "LQ",
}


# ---------------------------------------------------------------------------
# lunar state components
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MoonPhase:
    """Phase geometry of the Moon.

    Two conventions are reported side by side because they differ by up to
    about a degree when the Moon is near the horizon:

    ``geo``
        Geocentric — the Sun–Moon–Earth-centre angle (the conventional value
        used by almanacs).
    ``topo``
        Topocentric — the Sun–Moon–observer angle, i.e. what this observer
        actually sees.
    """

    phase_angle_geo_deg: float
    phase_angle_topo_deg: float
    illuminated_fraction_geo: float
    illuminated_fraction_topo: float
    age_days: float
    """Days since the last ecliptic-longitude new moon, in ``[0, 30)``.

    The synodic month itself varies between about 29.27 and 29.93 days, so
    ``age_days`` can exceed the mean value of 29.5306 days.
    """

    visual_age_days: float
    """Age inferred from elongation, matching the waxing/waning cycle."""

    name_en: str
    name_zh: str
    last_new_moon_utc: datetime

    @property
    def illuminated_percent_geo(self) -> float:
        return self.illuminated_fraction_geo * 100.0

    @property
    def illuminated_percent_topo(self) -> float:
        return self.illuminated_fraction_topo * 100.0

    @property
    def label(self) -> str:
        return f"{self.name_zh} / {self.name_en}"

    @property
    def waxing(self) -> bool:
        """True while the illuminated fraction is increasing."""
        return self.visual_age_days < SYNODIC_MONTH_DAYS * 0.5

    def to_dict(self) -> Dict[str, Any]:
        payload = _fields_dict(self)
        payload["illuminated_percent_geo"] = self.illuminated_percent_geo
        payload["illuminated_percent_topo"] = self.illuminated_percent_topo
        payload["waxing"] = self.waxing
        payload["label"] = self.label
        return payload


@dataclass(frozen=True)
class Libration:
    """Selenographic geometry of the Moon as seen from Earth.

    ``sub_earth_*`` are the selenographic coordinates of the point where the
    Earth is at the zenith.  They are the observable result of optical *and*
    physical libration, and are the quantities an observer uses to decide which
    limb is tipped into view.  The legacy key names ``libration_lon_deg`` /
    ``libration_lat_deg`` are kept as properties.
    """

    sub_earth_lat_deg: float
    sub_earth_lon_deg: float
    subsolar_lat_deg: float
    subsolar_lon_deg: float
    colongitude_deg: float
    """Selenographic colongitude of the Sun (90° minus subsolar longitude)."""

    #: Position angle of the bright limb, degrees from celestial north through
    #: east, as seen by the observer (0 = north, 90 = east).
    bright_limb_angle_deg: Optional[float] = None

    @property
    def libration_lat_deg(self) -> float:
        return self.sub_earth_lat_deg

    @property
    def libration_lon_deg(self) -> float:
        return self.sub_earth_lon_deg

    def to_dict(self) -> Dict[str, Any]:
        payload = _fields_dict(self)
        payload["libration_lat_deg"] = self.libration_lat_deg
        payload["libration_lon_deg"] = self.libration_lon_deg
        return payload


@dataclass(frozen=True)
class OrbitElements:
    """Osculating geocentric orbit elements of the Moon."""

    semi_major_km: float
    periapsis_km: float
    apoapsis_km: float
    eccentricity: float
    inclination_deg: float
    ascending_node_deg: float
    arg_periapsis_deg: float
    mean_anomaly_deg: float
    true_anomaly_deg: float
    period_days: float
    epoch_et: float

    def to_dict(self) -> Dict[str, Any]:
        return _fields_dict(self)


@dataclass(frozen=True)
class SurfaceIllumination:
    """Illumination geometry at the sub-observer point on the lunar surface."""

    incidence_deg: float
    emission_deg: float
    phase_deg: float

    def to_dict(self) -> Dict[str, Any]:
        return _fields_dict(self)


@dataclass(frozen=True)
class MoonState:
    """Complete instantaneous state of the Moon for one observer and instant.

    This is the single source of truth that the legacy ``compute_lunar_data``
    and ``quick_moon_data`` each reimplemented in part.
    """

    when_utc: datetime
    et: float
    observer: Observer
    phase: MoonPhase
    libration: Libration
    orbit: OrbitElements
    surface: SurfaceIllumination

    geo_distance_km: float
    topo_distance_km: float
    angular_diameter_deg: float
    angular_diameter_arcsec: float

    ra_hours: float
    dec_deg: float
    azimuth_deg: float
    elevation_deg: float
    elongation_deg: float
    sun_azimuth_deg: float
    sun_elevation_deg: float

    velocity_kms: float
    light_time_s: float
    moon_radius_km: float

    # -- convenience --------------------------------------------------------
    @property
    def angular_radius_arcsec(self) -> float:
        return self.angular_diameter_arcsec * 0.5

    @property
    def illuminated_fraction(self) -> float:
        """Geocentric illuminated fraction (conventional value)."""
        return self.phase.illuminated_fraction_geo

    @property
    def illuminated_percent(self) -> float:
        return self.phase.illuminated_percent_geo

    @property
    def above_horizon(self) -> bool:
        return self.elevation_deg > 0.0

    def to_dict(self) -> Dict[str, Any]:
        payload = _fields_dict(self)
        payload["angular_radius_arcsec"] = self.angular_radius_arcsec
        payload["illuminated_fraction"] = self.illuminated_fraction
        payload["illuminated_percent"] = self.illuminated_percent
        payload["above_horizon"] = self.above_horizon
        return payload


# ---------------------------------------------------------------------------
# rise / set / transit
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TransitDetails:
    """Details of a meridian transit (culmination)."""

    when_utc: datetime
    et: float
    elevation_deg: float
    azimuth_deg: float
    angular_diameter_arcsec: float

    @property
    def upper_limb_elevation_deg(self) -> float:
        """Elevation of the Moon's upper limb at transit."""
        return self.elevation_deg + self.angular_diameter_arcsec / 7200.0

    def to_dict(self) -> Dict[str, Any]:
        payload = _fields_dict(self)
        payload["upper_limb_elevation_deg"] = self.upper_limb_elevation_deg
        return payload


@dataclass(frozen=True)
class RiseSetEvents:
    """Moonrise, transit and moonset belonging to one diurnal cycle.

    ``rise``, ``transit`` and ``set`` are guaranteed to be ordered and to come
    from the same cycle: the transit nearest the reference instant is found
    first, then the rise immediately before it and the set immediately after
    it.  The legacy implementation searched each event independently over a
    48-hour window, so the three could belong to different cycles.
    """

    rise_utc: Optional[datetime]
    transit_utc: Optional[datetime]
    set_utc: Optional[datetime]
    mode: str
    rise_azimuth_deg: Optional[float] = None
    set_azimuth_deg: Optional[float] = None
    transit_elevation_deg: Optional[float] = None
    next_rise_utc: Optional[datetime] = None
    """First moonrise at or after the reference instant."""

    next_set_utc: Optional[datetime] = None
    """First moonset at or after the reference instant."""

    @property
    def ordering_ok(self) -> bool:
        """True when rise <= transit <= set."""
        times = [t for t in (self.rise_utc, self.transit_utc, self.set_utc) if t]
        return times == sorted(times)

    def to_dict(self) -> Dict[str, Any]:
        return _fields_dict(self)


@dataclass(frozen=True)
class MoonReport:
    """A full report: instantaneous state plus rise/set/transit."""

    state: MoonState
    rise_set: RiseSetEvents
    transit: Optional[TransitDetails] = None

    # -- pass-through convenience ------------------------------------------
    @property
    def when_utc(self) -> datetime:
        return self.state.when_utc

    @property
    def observer(self) -> Observer:
        return self.state.observer

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state.to_dict(),
            "rise_set": self.rise_set.to_dict(),
            "transit": None if self.transit is None else self.transit.to_dict(),
        }


# ---------------------------------------------------------------------------
# catalogues
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PhaseEvent:
    """A principal phase instant."""

    kind: PhaseKind
    et: float
    when_utc: datetime

    @property
    def label(self) -> str:
        return self.kind.label

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "name_en": self.kind.name_en,
            "name_zh": self.kind.name_zh,
            "symbol": self.kind.symbol,
            "et": self.et,
            "when_utc": _iso(self.when_utc),
        }


@dataclass(frozen=True)
class PhaseCatalogEntry:
    """One row of a standard-phase calendar, with local observing geometry."""

    event: PhaseEvent
    distance_km: float
    angular_diameter_arcsec: float
    angular_diameter_deg: float
    elevation_deg: Optional[float] = None

    @property
    def kind(self) -> PhaseKind:
        return self.event.kind

    @property
    def when_utc(self) -> datetime:
        return self.event.when_utc

    def to_dict(self) -> Dict[str, Any]:
        payload = self.event.to_dict()
        _extend(
            payload,
            distance_km=self.distance_km,
            angular_diameter_arcsec=self.angular_diameter_arcsec,
            angular_diameter_deg=self.angular_diameter_deg,
            elevation_deg=self.elevation_deg,
        )
        return payload


@dataclass(frozen=True)
class IlluminationMatch:
    """A moon whose illumination matches a requested value."""

    when_utc: datetime
    et: float
    month: int
    illumination_percent: float
    phase_angle_deg: float
    expected_phase_angle_deg: float
    phase_deviation_deg: float
    angular_diameter_arcsec: float
    angular_diameter_deg: float
    distance_km: float
    elongation_deg: float
    elevation_deg: float
    waxing: bool

    def to_dict(self) -> Dict[str, Any]:
        payload = _fields_dict(self)
        payload["when_utc"] = _iso(self.when_utc)
        return payload


# ---------------------------------------------------------------------------
# crescent observing
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CrescentGeometry:
    """Geometric crescent descriptors at an instant.

    ``arc_of_light_deg`` (ARCL) and ``arc_of_vision_deg`` (ARCV) are the two
    classical arguments of every published crescent visibility criterion;
    ``crescent_width_arcsec`` is the physical width of the lit sliver — the
    quantity an observer actually tries to resolve.
    """

    elongation_deg: float
    """Apparent topocentric Sun-Moon separation (also ARCL)."""

    arc_of_light_deg: float
    """ARCL: arc of light, the great-circle Sun-Moon separation."""

    arc_of_vision_deg: float
    """ARCV: arc of vision, the difference in altitude between the two centres."""

    sun_elevation_deg: float
    moon_elevation_deg: float
    moon_azimuth_deg: float
    sun_azimuth_deg: float
    relative_azimuth_deg: float
    """Moon azimuth minus Sun azimuth, normalised to ``(-180, 180]``."""

    illumination_percent_geo: float
    illumination_percent_topo: float
    phase_angle_geo_deg: float
    phase_angle_topo_deg: float
    angular_diameter_arcsec: float
    crescent_width_arcsec: float
    bright_limb_angle_deg: float
    """Position angle of the bright limb, degrees from celestial north through
    east: the direction the horns point away from."""

    moon_age_days: float
    """Signed days since the associated new moon; negative before it."""

    @property
    def illumination_percent(self) -> float:
        """Geocentric illuminated percentage (conventional value)."""
        return self.illumination_percent_geo

    @property
    def waxing(self) -> bool:
        return self.moon_age_days >= 0.0

    def illumination(self, mode: "PhaseMode | str") -> float:
        """Illuminated percentage for a phase mode (string or enum)."""
        value = mode.value if hasattr(mode, "value") else str(mode)
        if value == "geocentric":
            return self.illumination_percent_geo
        if value == "topocentric":
            return self.illumination_percent_topo
        raise ValueError(f"Unknown phase mode {mode!r}")

    def phase_angle(self, mode: "PhaseMode | str") -> float:
        value = mode.value if hasattr(mode, "value") else str(mode)
        if value == "geocentric":
            return self.phase_angle_geo_deg
        if value == "topocentric":
            return self.phase_angle_topo_deg
        raise ValueError(f"Unknown phase mode {mode!r}")

    def to_dict(self) -> Dict[str, Any]:
        payload = _fields_dict(self)
        payload["illumination_percent"] = self.illumination_percent
        payload["waxing"] = self.waxing
        return payload


@dataclass(frozen=True)
class CrescentWindow:
    """One observing session for a thin crescent."""

    period: str
    """``'evening'`` (waxing, after sunset) or ``'morning'`` (waning, before sunrise)."""

    local_date: str
    """Local calendar date ``YYYY-MM-DD`` at the observing site."""

    best_utc: datetime
    best_local: datetime
    new_moon_utc: datetime
    age_days: float
    geometry: CrescentGeometry
    photo_score: float
    visibility: Dict[str, Any]
    """Criterion verdicts, keyed by criterion name (see :mod:`moonlib_s.crescent`)."""

    candidate_points: int
    moon_elevation_range_deg: Tuple[float, float]
    sun_elevation_range_deg: Tuple[float, float]
    window_start_utc: Optional[datetime] = None
    """First acceptable instant of the session -- the start of the *recommended*
    range, which is the part of the night the crescent is actually workable in."""
    window_end_utc: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = _fields_dict(self)
        payload["best_utc"] = _iso(self.best_utc)
        payload["best_local"] = self.best_local.isoformat()
        payload["new_moon_utc"] = _iso(self.new_moon_utc)
        payload["window_start_utc"] = _iso(self.window_start_utc)
        payload["window_end_utc"] = _iso(self.window_end_utc)
        payload["moon_elevation_range_deg"] = list(self.moon_elevation_range_deg)
        payload["sun_elevation_range_deg"] = list(self.sun_elevation_range_deg)
        return payload


# ---------------------------------------------------------------------------
# solar geometry
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SunState:
    """Instantaneous solar geometry for an observer."""

    when_utc: datetime
    et: float
    ra_hours: float
    dec_deg: float
    azimuth_deg: float
    elevation_deg: float
    distance_km: float
    ecliptic_longitude_deg: float
    equation_of_time_minutes: float
    light_time_s: float

    def to_dict(self) -> Dict[str, Any]:
        return _fields_dict(self)


@dataclass(frozen=True)
class AnalemmaPoint:
    """One sample of a solar analemma."""

    when_utc: datetime
    local_time: str
    day_of_year: int
    month: int
    azimuth_deg: float
    elevation_deg: float
    declination_deg: float
    equation_of_time_minutes: float

    def to_dict(self) -> Dict[str, Any]:
        payload = _fields_dict(self)
        payload["when_utc"] = _iso(self.when_utc)
        return payload


# ---------------------------------------------------------------------------
# lunar limb targets
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LimbTarget:
    """A named feature on the lunar surface.

    A target is modelled as a spherical cap: a centre direction plus an angular
    radius derived from the feature radius and the Moon's radius.
    """

    english_name: str
    name_zh: str
    lat_deg: float
    lon_deg: float
    radius_km: float
    region: str
    feature_type: str
    margin_deg: float = 0.0
    """Extra angular tolerance (elevation / limb profile), defaults to 0."""

    theta_r_deg: float = 0.0
    """Angular radius of the feature, degrees (derived, not user supplied)."""

    def __post_init__(self) -> None:
        if self.radius_km > 0 and self.theta_r_deg <= 0.0:
            import math

            from .kernels import kernel_session

            moon_radius = kernel_session().constants.moon_radius_km
            object.__setattr__(
                self,
                "theta_r_deg",
                math.degrees(math.asin(min(1.0, self.radius_km / moon_radius))),
            )

    @property
    def angular_radius_deg(self) -> float:
        return self.theta_r_deg

    @property
    def effective_radius_deg(self) -> float:
        """Angular radius widened by the terrain margin."""
        return self.theta_r_deg + self.margin_deg

    def to_dict(self) -> Dict[str, Any]:
        payload = _fields_dict(self)
        payload["theta_r_deg"] = self.theta_r_deg
        payload["effective_radius_deg"] = self.effective_radius_deg
        return payload


class VisibilityGrade(str, enum.Enum):
    """How much of a target is inside the Earth-facing hemisphere."""

    HIDDEN = "hidden"
    MARGINAL = "marginal"
    CENTER = "center"
    FULL = "full"

    @property
    def rank(self) -> int:
        return _GRADE_RANK[self]

    @property
    def name_zh(self) -> str:
        return _GRADE_ZH[self]


_GRADE_RANK = {
    VisibilityGrade.HIDDEN: 0,
    VisibilityGrade.MARGINAL: 1,
    VisibilityGrade.CENTER: 2,
    VisibilityGrade.FULL: 3,
}
_GRADE_ZH = {
    VisibilityGrade.HIDDEN: "不可见",
    VisibilityGrade.MARGINAL: "边缘",
    VisibilityGrade.CENTER: "中心越过",
    VisibilityGrade.FULL: "完全可见",
}


@dataclass(frozen=True)
class TargetVisibility:
    """Visibility of one limb target at one instant."""

    target: LimbTarget
    when_utc: datetime
    et: float
    gamma_deg: float
    """Angular distance between the target centre and the sub-Earth point."""

    effective_radius_deg: float
    margin_deg: float
    """``90 - (gamma - r_eff)``; positive means the cap reaches past the limb."""

    grade: VisibilityGrade
    solar_incidence_deg: float
    illuminated: bool
    moon_elevation_deg: Optional[float] = None
    moon_illumination_percent: Optional[float] = None

    @property
    def is_visible(self) -> bool:
        return self.grade is not VisibilityGrade.HIDDEN

    @property
    def is_observable(self) -> bool:
        """Visible, sunlit, and the Moon is above the horizon."""
        return (
            self.is_visible
            and self.illuminated
            and (self.moon_elevation_deg is None or self.moon_elevation_deg > 0.0)
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target": self.target.to_dict(),
            "when_utc": _iso(self.when_utc),
            "et": self.et,
            "gamma_deg": self.gamma_deg,
            "effective_radius_deg": self.effective_radius_deg,
            "margin_deg": self.margin_deg,
            "grade": self.grade.value,
            "grade_zh": self.grade.name_zh,
            "solar_incidence_deg": self.solar_incidence_deg,
            "illuminated": self.illuminated,
            "moon_elevation_deg": self.moon_elevation_deg,
            "moon_illumination_percent": self.moon_illumination_percent,
            "is_visible": self.is_visible,
            "is_observable": self.is_observable,
        }


@dataclass(frozen=True)
class LibrationSweepSample:
    """One time step of a libration / limb-target sweep.

    Carries the lunar geometry at that instant plus the visibility of every
    requested target, so a single sweep can answer many questions without
    recomputing the ephemeris.
    """

    when_utc: datetime
    et: float
    sub_earth_lat_deg: float
    sub_earth_lon_deg: float
    subsolar_lat_deg: float
    subsolar_lon_deg: float
    colongitude_deg: float
    moon_elevation_deg: float
    sun_elevation_deg: float
    illumination_percent: float
    elongation_deg: float
    visibilities: Tuple["TargetVisibility", ...] = ()

    def visible(
        self,
        min_grade: "VisibilityGrade | str" = VisibilityGrade.MARGINAL,
        *,
        require_illuminated: bool = True,
        min_solar_incidence_deg: Optional[float] = None,
        max_solar_incidence_deg: Optional[float] = None,
        min_moon_elevation_deg: Optional[float] = None,
        max_illumination_percent: Optional[float] = None,
    ) -> Tuple["TargetVisibility", ...]:
        """Targets meeting all of the supplied observing conditions."""
        if isinstance(min_grade, str):
            min_grade = VisibilityGrade(min_grade)
        threshold = min_grade.rank
        out = []
        for visibility in self.visibilities:
            if visibility.grade.rank < threshold:
                continue
            if require_illuminated and not visibility.illuminated:
                continue
            if (
                min_solar_incidence_deg is not None
                and visibility.solar_incidence_deg < min_solar_incidence_deg
            ):
                continue
            if (
                max_solar_incidence_deg is not None
                and visibility.solar_incidence_deg > max_solar_incidence_deg
            ):
                continue
            if min_moon_elevation_deg is not None and (
                self.moon_elevation_deg < min_moon_elevation_deg
            ):
                continue
            if (
                max_illumination_percent is not None
                and self.illumination_percent > max_illumination_percent
            ):
                continue
            out.append(visibility)
        return tuple(out)

    def to_dict(self) -> Dict[str, Any]:
        payload = _fields_dict(self)
        payload["visibilities"] = [item.to_dict() for item in self.visibilities]
        return payload


# ---------------------------------------------------------------------------
# whole-disk observation simulation
# ---------------------------------------------------------------------------
class PhotometricModel(str, enum.Enum):
    """Surface scattering law used to shade the simulated lunar disk.

    ``LAMBERT``
        Ideal diffuse reflector.  Wrong for the Moon (it darkens far too
        steeply towards the limb) but useful as a reference.
    ``LOMMEL_SEELIGER``
        The classical "lunar law" ``mu0 / (mu0 + mu)``; reproduces the nearly
        flat appearance of the full Moon.
    ``LUNAR_LAMBERT``
        The empirical McEwen (1991) lunar-Lambert function, the default.  It
        blends Lommel-Seeliger and Lambert with a phase-dependent weight
        ``L(alpha)`` fitted to lunar photometry, and is the model USGS ISIS
        uses to normalise lunar image mosaics.
    ``HAPKE``
        Hapke (2002) single-scattering plus opposition effect, with a
        two-stream Chandrasekhar ``H`` function.
    """

    LAMBERT = "lambert"
    LOMMEL_SEELIGER = "lommel_seeliger"
    LUNAR_LAMBERT = "lunar_lambert"
    HAPKE = "hapke"


def as_photometric_model(value: "PhotometricModel | str") -> PhotometricModel:
    """Coerce a string or enum member into a :class:`PhotometricModel`."""
    if isinstance(value, PhotometricModel):
        return value
    try:
        return PhotometricModel(str(value).strip().lower())
    except ValueError as exc:
        options = ", ".join(item.value for item in PhotometricModel)
        raise MoonlibError(
            f"Unknown photometric model {value!r}; choose one of {options}."
        ) from exc


class ToneMapping(str, enum.Enum):
    """How linear radiance is mapped onto displayable pixel values.

    ``LINEAR`` and ``SRGB`` are radiometric: what is displayed is proportional
    to the physical radiance (up to the usual display transfer).  Because the
    earthshine-lit hemisphere is about ten magnitudes fainter than the sunlit
    one, it stays invisible at any single exposure — which is precisely why
    earthshine photographs are composites.

    ``REINHARD``, ``ASINH`` and ``VISUAL`` are *perceptual*: they compress the
    dynamic range the way a dark-adapted eye does.  ``ASINH`` is the standard
    astronomical stretch of Lupton et al. (2004); ``VISUAL`` is that stretch
    with a softening tuned so the sunlit and earthshine-lit hemispheres are
    both legible, i.e. it approximates what an observer sees rather than what
    a single exposure records.
    """

    LINEAR = "linear"
    SRGB = "srgb"
    REINHARD = "reinhard"
    ASINH = "asinh"
    VISUAL = "visual"


def as_tone_mapping(value: "ToneMapping | str") -> ToneMapping:
    """Coerce a string or enum member into a :class:`ToneMapping`."""
    if isinstance(value, ToneMapping):
        return value
    try:
        return ToneMapping(str(value).strip().lower())
    except ValueError as exc:
        options = ", ".join(item.value for item in ToneMapping)
        raise MoonlibError(
            f"Unknown tone mapping {value!r}; choose one of {options}."
        ) from exc


@dataclass(frozen=True)
class MoonView:
    """Illumination geometry of the Moon for one observer at one instant.

    Every direction is a unit vector in the ``MOON_ME`` selenographic frame,
    which is the frame the global LOLA/LROC rasters are gridded in.  This is
    deliberately *not* the ``IAU_MOON`` frame used by
    :class:`Libration`: the two differ by the physical-libration offset of the
    principal axes, and mixing them would misregister the terrain by up to a
    few hundredths of a degree.
    """

    when_utc: datetime
    observer: Observer
    et: float
    distance_km: float
    """Topocentric distance to the Moon's centre."""
    angular_diameter_deg: float
    """Apparent angular diameter of the 1737.4 km reference sphere."""
    sub_observer_lon_deg: float
    """Selenographic longitude of the topocentric sub-observer point."""
    sub_observer_lat_deg: float
    sub_solar_lon_deg: float
    sub_solar_lat_deg: float
    sun_direction: Tuple[float, float, float]
    """Unit vector from the Moon's centre towards the Sun."""
    earth_direction: Tuple[float, float, float]
    """Unit vector from the Moon's centre towards the Earth's centre."""
    observer_direction: Tuple[float, float, float]
    """Unit vector from the Moon's centre towards the observer."""
    phase_angle_deg: float
    """Sun-Moon-observer angle, i.e. the conventional lunar phase angle."""
    illuminated_fraction: float
    colongitude_deg: float
    earth_distance_km: float
    """Distance from the Moon's centre to the Earth's centre."""
    earth_angular_diameter_deg: float
    """Apparent angular diameter of the Earth as seen from the Moon."""
    earth_phase_angle_deg: float
    """Sun-Earth-Moon angle: the Earth's phase angle as seen from the Moon."""
    earth_illuminated_fraction: float
    earthshine_ratio: float
    """Earthshine irradiance at the Moon divided by the solar irradiance."""
    earthshine_magnitude: float
    """Apparent magnitude of the fully lit Earth as seen from the Moon."""
    earthshine_apparent_magnitude: float
    """Apparent magnitude of the Moon's earthshine-lit night side, from Earth.

    This is 地照亮度: how bright the *dark* part of the Moon -- the part the Sun
    does not light -- looks in our own sky, lit only by the Earth.  It is not
    the brightness of the Earth.  Infinite at full moon, where there is no
    night side to see.
    """
    bright_limb_position_angle_deg: float
    """Position angle of the midpoint of the bright limb, north through east."""
    moon_elevation_deg: Optional[float] = None
    """Geometric elevation of the Moon above the observer's horizon."""

    @property
    def libration_lon_deg(self) -> float:
        """Optical libration in longitude (positive towards the east limb)."""
        return self.sub_observer_lon_deg

    @property
    def libration_lat_deg(self) -> float:
        """Optical libration in latitude (positive towards the north limb)."""
        return self.sub_observer_lat_deg

    @property
    def illuminated_percent(self) -> float:
        return 100.0 * self.illuminated_fraction

    @property
    def earth_illuminated_percent(self) -> float:
        return 100.0 * self.earth_illuminated_fraction

    @property
    def earthshine_magnitudes_below_sunlight(self) -> float:
        """How many magnitudes the earthshine irradiance sits below sunlight."""
        if self.earthshine_ratio <= 0.0:
            return float("inf")
        return -2.5 * math.log10(self.earthshine_ratio)

    def to_dict(self) -> Dict[str, Any]:
        payload = _fields_dict(self)
        # Observer defines as_dict rather than to_dict; going through it keeps
        # the private cached timezone out of the public payload.
        payload["observer"] = self.observer.as_dict()
        _extend(
            payload,
            libration_lon_deg=self.libration_lon_deg,
            libration_lat_deg=self.libration_lat_deg,
            illuminated_percent=self.illuminated_percent,
            earth_illuminated_percent=self.earth_illuminated_percent,
            earthshine_magnitudes_below_sunlight=(
                self.earthshine_magnitudes_below_sunlight
            ),
        )
        return payload


@dataclass(frozen=True)
class SurfaceFeature:
    """A named lunar landform with a published size.

    Used to size a close-up: the field of view is chosen so that the feature's
    real diameter fits, which is the only way to get a frame that is neither
    cropping the feature nor lost in empty surroundings.
    """

    name_en: str
    name_zh: str
    lat_deg: float
    lon_deg: float
    diameter_km: float
    kind: str = "Crater"
    note: str = ""
    diameter_source: str = "catalogue"
    """``"catalogue"`` for a published value, ``"dem"`` for a measured one."""

    @property
    def radius_km(self) -> float:
        return 0.5 * self.diameter_km

    @property
    def label(self) -> str:
        return f"{self.name_zh} / {self.name_en}" if self.name_zh else self.name_en

    def to_dict(self) -> Dict[str, Any]:
        payload = _fields_dict(self)
        payload["radius_km"] = self.radius_km
        payload["label"] = self.label
        return payload


@dataclass(frozen=True)
class MoonRegion:
    """Viewing and illumination geometry of a simulated close-up.

    Produced by :func:`moonlib_s.moonview.region_geometry` and attached to the
    :class:`MoonRender` that :func:`moonlib_s.moonview.render_region` returns, so
    that a close-up carries the same kind of captionable metadata the whole-disk
    render does.
    """

    lon_deg: float
    """Selenographic longitude of the centre of the frame."""
    lat_deg: float
    span_km: float
    """Surface extent requested across the shorter side of the frame."""
    width: int
    height: int
    distance_km: float
    """Distance from the observer to the target point on the surface."""
    emergence_deg: float
    """Angle between the surface normal at the target and the view direction.

    Zero means looking straight down; 90 deg would be exactly on the limb.
    """
    incidence_deg: float
    """Sun angle at the target; ``nan`` when the target is unlit."""
    solar_elevation_deg: float
    """Sun elevation above the target's local horizon; ``nan`` when unlit."""
    foreshortening: float
    """``cos(emergence)``: the factor by which the view compresses the surface."""
    ground_sample_km: float
    """Ground distance per output pixel *across* the direction of tilt.

    This is the finer of the two directions, and therefore the detail the frame
    claims to resolve; see :attr:`tilted_ground_sample_km` for the coarser one.
    """
    terrain_km_per_pixel: float
    """Pixel spacing of the terrain actually used; ``nan`` if unknown."""
    fov_arcsec: float
    illuminated: bool
    feature_name: Optional[str] = None
    """Set when the frame was sized for a named landform."""
    covering_diameter_km: Optional[float] = None
    """Feature diameter the span was chosen to fit, in kilometres."""

    @property
    def surface_span_km(self) -> float:
        """Surface extent along the direction the view compresses.

        The frame subtends ``span_km`` of *sky* at the Moon's distance, but
        along the direction of tilt the surface is inclined to the line of
        sight, so one unit of sky covers ``1 / foreshortening`` units of
        ground.  This is therefore the **larger** of the two surface extents
        the frame covers, and the one to quote when asking whether a feature
        fits: a 231 km crater seen at 64 deg emergence needs only about
        100 km of sky.
        """
        if self.foreshortening <= 0.0:  # pragma: no cover - guarded upstream
            return float("inf")
        return self.span_km / self.foreshortening

    @property
    def tilted_ground_sample_km(self) -> float:
        """Ground distance per pixel along the direction of tilt.

        Always the coarser of the two: the same pixel covers more ground where
        the surface leans away from the viewer.
        """
        if self.foreshortening <= 0.0:  # pragma: no cover - guarded upstream
            return float("inf")
        return self.ground_sample_km / self.foreshortening

    @property
    def terrain_limited(self) -> bool:
        """True when the request is finer than the terrain can actually support.

        Past this point the renderer is interpolating between terrain samples,
        which is empty magnification: the picture gets bigger without getting
        sharper, and topographic shading starts to be invented by the
        interpolator rather than measured.
        """
        if self.terrain_km_per_pixel != self.terrain_km_per_pixel:  # nan
            return False
        return self.ground_sample_km < self.terrain_km_per_pixel

    @property
    def coverage_ratio(self) -> Optional[float]:
        """How many times over the frame covers the feature it was sized for.

        ``None`` when no feature was named.  Anything at or above 1 means the
        whole feature is inside the frame.
        """
        if not self.covering_diameter_km:
            return None
        return self.surface_span_km / self.covering_diameter_km

    @property
    def covers_feature(self) -> Optional[bool]:
        ratio = self.coverage_ratio
        return None if ratio is None else ratio >= 1.0

    @property
    def resolution_ratio(self) -> float:
        """Output pixels per terrain pixel; below 1 means interpolating."""
        if self.terrain_km_per_pixel != self.terrain_km_per_pixel:  # nan
            return float("nan")
        if self.ground_sample_km <= 0.0:  # pragma: no cover - defensive
            return float("nan")
        return self.terrain_km_per_pixel / self.ground_sample_km

    def to_dict(self) -> Dict[str, Any]:
        return _extend(
            _fields_dict(self),
            surface_span_km=self.surface_span_km,
            coverage_ratio=self.coverage_ratio,
            covers_feature=self.covers_feature,
            tilted_ground_sample_km=self.tilted_ground_sample_km,
            terrain_limited=self.terrain_limited,
            resolution_ratio=self.resolution_ratio,
        )


@dataclass(frozen=True)
class MoonRenderSettings:
    """Every knob of a disk rendering, so a result is reproducible."""

    width: int = 1024
    height: int = 1024
    disk_fraction: float = 0.94
    """Fraction of the shorter image side covered by the lunar disk."""
    pyramid_level: Optional[int] = None
    """Terrain level to sample; ``None`` picks one from the pixel footprint."""
    photometric_model: PhotometricModel = PhotometricModel.LUNAR_LAMBERT
    normal_albedo: float = 0.136
    """Disk-averaged normal albedo of the Moon in the visible.

    The colour mosaic is a relative product, so this is the single number that
    converts it into photometric units; the mosaic is divided by its own global
    mean first, which makes the parameter the disk average (Lane & Irvine 1973
    give 0.136).  Bright fresh material lands near 0.25-0.30, as it should.
    """
    ray_steps: int = 16
    """Floor for the number of shell-marching steps of a primary ray.

    The count actually used is driven by the terrain sampling: a ray is stepped
    once per terrain pixel of the level in use, with this value as a floor and
    the 99th percentile over the frame as the working number.
    """
    ray_steps_min: int = 6
    ray_steps_max: int = 2048
    """Ceiling on the shell-marching steps of a single ray.

    It binds only where rays graze the shell: at the native 64 px/deg terrain
    level the outermost fraction of a percent of the disk wants roughly a
    thousand steps.  Raising it is nearly free, because rays that have already
    hit are dropped from the marching set, so the late steps only ever touch
    the handful of rays still looking for a surface.
    """
    bisection_steps: int = 8
    band_workers: int = 0
    """How many horizontal bands may be traced at once.

    ``0`` keeps the historical behaviour: one worker per CPU (capped by the
    number of bands), or whatever ``MOONLIB_BAND_WORKERS`` says.  A positive
    value is a hard ceiling from the caller, which is how the page's 并行数
    control keeps a render from taking the whole machine.
    """
    samples_per_pixel: int = 2
    """Sub-pixel samples per axis, so 4 rays per pixel at the default.

    Terrain is sampled and shadow rays are marched at the *sub-sample*
    footprint (``ground_sample / samples_per_pixel``) and the results are
    box-averaged.  That is what keeps the terminator crisp without the isolated
    speckle a single sample produces: a shadow cast by a hill smaller than one
    pixel has to darken part of a pixel, not all or none of it.
    """
    shadow_steps: int = 48
    """Number of samples along a shadow ray.

    Samples grow geometrically from one frame pixel to ``shadow_range_km``, so
    the near field keeps pixel-scale resolution while a 200 km graze is still
    covered in a few dozen samples.  Each sample reads the pyramid level that
    matches the spacing to the next sample.
    """
    shadow_range_km: float = 220.0
    """How far a shadow caster is searched for along the sun ray."""
    shadow_skip_elevation_deg: float = 30.0
    """Sun elevations above the local horizontal skip the shadow march."""
    sun_samples: int = 1
    """Samples across the solar disk; ``> 1`` resolves the penumbra."""
    earthshine: bool = True
    rotation_deg: float = 0.0
    """Camera roll about the line of sight, counter-clockwise in the image.

    Zero puts selenographic north up, which is the easiest frame to reason
    about though not what an alt-azimuth mount delivers unaided.
    """
    earth_samples: int = 13
    """Samples across the Earth's disk, which is an extended source."""
    earthshine_gain: float = 1.0
    """Multiplier applied to earthshine, for deliberate HDR composites."""
    earth_geometric_albedo: float = 0.367
    earthshine_tint: Tuple[float, float, float] = (0.87, 0.96, 1.17)
    """Relative spectral weighting of earthshine against sunlight."""
    ambient_fraction: float = 0.0
    """Empirical fill for light scattered onto shadowed terrain; 0 disables."""
    sky_ambient: float = 0.0
    """Sky light reaching shadowed ground, as a fraction of the incident sunlight.

    The physical reading: the sky above the Moon is filled by sunlight
    scattered off the surrounding illuminated ground (and, far more faintly, by
    the thin lunar atmosphere).  A surface point receives the part of that dome
    it can actually see, so a crater floor and the shaded side of a ridge come
    out darker than a plain, with the occlusion computed from the terrain
    rather than assumed.

    ``1.0`` is calibrated so that the fill on flat ground is about 6 % of the
    direct sunlight, which is the order of magnitude a scattered-light term
    should have.  ``0`` -- the default -- leaves the renderer bit-for-bit what
    it was before the term existed.  It supersedes :attr:`ambient_fraction`,
    which multiplied the *direct* term and therefore added no occlusion at all.
    """
    roughness_deg: float = 0.0
    """Mean surface slope for Hapke's micro-relief shadowing, degrees.

    The regolith is rough far below any pixel, so part of it shadows itself and
    a rough terminator falls off faster than a smooth one -- the graininess real
    limb photographs show.  ``15-25`` deg is the quoted range for lunar soil and
    is where the caliper sits in the tests; ``0`` -- the default -- leaves the
    renderer bit for bit what it was.

    The factor is 1 at the sub-solar point whatever the value, so a full-Moon
    disk does not darken in its middle; the reduction used, and where it stops
    being trustworthy, are documented on
    :func:`moonlib_s.moonview.roughness_shadowing`.
    """
    sky_samples: int = 26
    """Nominally, how many sky directions per point.

    The horizon band is sampled densely whatever this says -- see
    ``_hemisphere_directions`` -- because that band is where a crater wall
    lives.  ``0`` is what actually turns the term off, so this is a switch with
    a number on it.
    """
    sky_steps: int = 32
    """March budget per sky ray, spread over ``SKY_HORIZON_RANGE_KM``.

    Measured: 32, 48, 64 and 96 steps give the same visibility to four decimals
    (0.9456 versus 0.9459) and the same picture, while 96 costs twice as much.
    The schedule grows geometrically, so most of the near field keeps its
    resolution at any of them; the numbers are in ``docs/07``.
    """
    tone: ToneMapping = ToneMapping.SRGB
    exposure_ev: float = 0.0
    auto_exposure: bool = True
    """Scale the exposure so the brightest sunlit pixel lands at 1.0."""
    exposure_meter: str = "percentile"
    """Which statistic of the lit radiance the auto-exposure is metered on.

    ``"percentile"`` is the 99.9th percentile -- the brightest sunlit pixel,
    near enough, and what a still wants.  It is a *rank* statistic, though: a
    single pixel crossing the cut moves the value, and with it the whole
    frame's brightness.  On one waxing gibbous that showed up as four backwards
    steps of up to 1% against a monotonically brightening scene, and an
    animation metered that way flickers.

    ``"top_mean"`` is the mean of the brightest 1% of the lit pixels.  It sits
    within 2-12% of the percentile on disks, close-ups, terminators and dark
    scenes -- so the picture is the same -- and its worst backwards step over
    the same run is 0.02%: fifty times steadier.  :func:`moonlib_s.anim.animation_frames`
    uses it for the whole run.
    """
    _EXPOSURE_METERS = ("percentile", "top_mean")
    asinh_softening: float = 1.0e-3
    keep_components: bool = True
    """Also return the sunlit and earthshine radiance separately."""

    def __post_init__(self) -> None:
        # Accept the string spellings so callers coming from JSON do not have
        # to import the enums, and so to_dict never depends on which was used.
        object.__setattr__(
            self, "photometric_model", as_photometric_model(self.photometric_model)
        )
        object.__setattr__(self, "tone", as_tone_mapping(self.tone))
        if self.exposure_meter not in self._EXPOSURE_METERS:
            options = ", ".join(self._EXPOSURE_METERS)
            raise MoonlibError(
                f"Unknown exposure meter {self.exposure_meter!r}; choose one of "
                f"{options}."
            )

    def to_dict(self) -> Dict[str, Any]:
        payload = _fields_dict(self)
        payload["photometric_model"] = self.photometric_model.value
        payload["tone"] = self.tone.value
        return payload


@dataclass(frozen=True)
class MoonRender:
    """A rendered lunar disk plus the geometry and statistics behind it.

    The three radiance arrays are **radiance factors** ``I/F``: the reflected
    radiance divided by the incident irradiance over pi.  A perfectly diffuse
    white surface at normal incidence and zero phase angle would read 1.0, so
    the Moon's sunlit surface sits near its normal albedo (about 0.13 on
    average) and the earthshine-lit hemisphere near ``1e-5``.

    ``image`` is what a display wants.  ``sunlit`` and ``earthshine`` are the
    two physical components, returned separately because no single exposure can
    show both.
    """

    view: MoonView
    settings: MoonRenderSettings
    image: Any
    """``(height, width, 3)`` float32 display image in ``[0, 1]``."""
    sunlit: Optional[Any] = None
    """``(height, width, 3)`` float32 sunlit radiance factor, or ``None``."""
    earthshine: Optional[Any] = None
    stats: Dict[str, Any] = field(default_factory=dict)
    region: Optional["MoonRegion"] = None
    """Set when this came from a close-up rather than a whole-disk render."""

    @property
    def height(self) -> int:
        return int(self.image.shape[0])

    @property
    def width(self) -> int:
        return int(self.image.shape[1])

    @property
    def coverage_percent(self) -> float:
        """Fraction of the frame actually covered by the lunar disk."""
        return 100.0 * float(self.stats.get("lit_fraction_of_disk", 0.0)) * float(
            self.stats.get("disk_fraction_of_frame", 0.0)
        )

    def to_dict(self) -> Dict[str, Any]:
        """JSON payload without the pixel arrays."""
        return {
            "view": self.view.to_dict(),
            "settings": self.settings.to_dict(),
            "stats": dict(self.stats),
            "shape": [self.height, self.width],
            "region": self.region.to_dict() if self.region else None,
        }
