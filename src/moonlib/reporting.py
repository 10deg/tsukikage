"""Human-readable text output.

Computation and presentation are separated on purpose: the modules under
:mod:`moonlib` never print, and everything that produces text for a person lives
here.  Each formatter returns a string (so a web handler or a log line can reuse
it) and a matching ``print_*`` wrapper exists for interactive use.

Labels are bilingual.  ``language`` selects ``'zh'``, ``'en'`` or ``'both'``
(the default), matching the style of the legacy library without hard-coding the
choice into the formatters.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from .models import (
    CrescentWindow,
    IlluminationMatch,
    MoonReport,
    MoonState,
    PhaseCatalogEntry,
    PhaseKind,
    TargetVisibility,
)
from .observer import Observer
from .timeutil import format_offset

__all__ = [
    "format_moon_report",
    "print_moon_report",
    "format_moon_state",
    "format_phase_calendar",
    "print_phase_calendar",
    "format_crescent_windows",
    "print_crescent_windows",
    "format_illumination_matches",
    "format_target_summary",
    "print_target_summary",
    "format_visible_targets",
    "format_analemma_extremes",
    "format_moon_view",
    "format_render_summary",
    "format_region_summary",
]

_RULE = "=" * 74
_THIN = "-" * 74


# ---------------------------------------------------------------------------
# small formatting helpers
# ---------------------------------------------------------------------------
def _label(zh: str, en: str, language: str) -> str:
    if language == "zh":
        return zh
    if language == "en":
        return en
    return f"{zh} / {en}"


def _ra_str(hours: float) -> str:
    """Format decimal hours as ``HHh MMm SS.SSs``."""
    value = hours % 24.0
    hh = int(value)
    mm = int((value - hh) * 60.0)
    ss = (value - hh - mm / 60.0) * 3600.0
    return f"{hh:02d}h {mm:02d}m {ss:05.2f}s"


def _dec_str(degrees: float) -> str:
    """Format decimal degrees as ``+DD deg MM' SS.S"``."""
    sign = "+" if degrees >= 0 else "-"
    magnitude = abs(degrees)
    dd = int(magnitude)
    mm = int((magnitude - dd) * 60.0)
    ss = (magnitude - dd - mm / 60.0) * 3600.0
    return f"{sign}{dd:02d}° {mm:02d}' {ss:04.1f}\""


def _time_str(moment: Optional[datetime], observer: Observer) -> str:
    if moment is None:
        return "-"
    local = moment.astimezone(observer.timezone)
    return f"{local:%Y-%m-%d %H:%M:%S} ({format_offset(observer.timezone, local)})"


def _clock(moment: Optional[datetime], observer: Observer) -> str:
    if moment is None:
        return "  --:--:--"
    return f"{moment.astimezone(observer.timezone):%H:%M:%S}"


def _signed(value: float, digits: int = 2) -> str:
    return f"{value:+.{digits}f}"


# ---------------------------------------------------------------------------
# moon state / report
# ---------------------------------------------------------------------------
def format_moon_state(state: MoonState, language: str = "both") -> str:
    """Compact one-screen rendering of a :class:`~moonlib.models.MoonState`."""
    observer = state.observer
    phase = state.phase
    libration = state.libration
    orbit = state.orbit

    lines: List[str] = []
    lines.append(_RULE)
    lines.append(f"  {_label('月球状态', 'Lunar State', language)}")
    lines.append(_RULE)
    lines.append(
        f"  {_label('观测地点', 'Site', language):<22}: "
        f"lat={observer.lat:.4f}°  lon={observer.lon:.4f}°  "
        f"h={observer.height_km:.3f} km"
    )
    lines.append(
        f"  {_label('时区', 'Timezone', language):<22}: {observer.timezone} "
        f"({observer.utc_offset_label})"
    )
    lines.append(
        f"  {_label('本地时间', 'Local time', language):<22}: "
        f"{state.when_utc.astimezone(observer.timezone):%Y-%m-%d %H:%M:%S %Z}"
    )
    lines.append(
        f"  {_label('UTC 时间', 'UTC time', language):<22}: "
        f"{state.when_utc:%Y-%m-%d %H:%M:%S}"
    )
    lines.append(_THIN)
    lines.append(f"  {_label('月相', 'Phase', language):<22}: {phase.label}")
    lines.append(
        f"  {_label('照明率(地心)', 'Illum (geo)', language):<22}: "
        f"{phase.illuminated_percent_geo:.4f} %"
    )
    lines.append(
        f"  {_label('照明率(站心)', 'Illum (topo)', language):<22}: "
        f"{phase.illuminated_percent_topo:.4f} %"
    )
    lines.append(
        f"  {_label('相位角(地心)', 'Phase angle (geo)', language):<22}: "
        f"{phase.phase_angle_geo_deg:.4f}°"
    )
    lines.append(
        f"  {_label('相位角(站心)', 'Phase angle (topo)', language):<22}: "
        f"{phase.phase_angle_topo_deg:.4f}°"
    )
    lines.append(
        f"  {_label('月龄', 'Moon age', language):<22}: {phase.age_days:.3f} d"
        f"   ({_label('视觉月龄', 'visual', language)} {phase.visual_age_days:.3f} d)"
    )
    lines.append(
        f"  {_label('上次新月', 'Last new moon', language):<22}: "
        f"{phase.last_new_moon_utc.astimezone(observer.timezone):%Y-%m-%d %H:%M:%S}"
    )
    lines.append(_THIN)
    lines.append(
        f"  {_label('赤经', 'RA', language):<22}: {_ra_str(state.ra_hours)} "
        f"({state.ra_hours:.5f} h)"
    )
    lines.append(
        f"  {_label('赤纬', 'Dec', language):<22}: {_dec_str(state.dec_deg)} "
        f"({state.dec_deg:.5f}°)"
    )
    lines.append(
        f"  {_label('方位/高度', 'Azimuth/Elevation', language):<22}: "
        f"{state.azimuth_deg:7.3f}° / {state.elevation_deg:+7.3f}°"
    )
    lines.append(
        f"  {_label('太阳方位/高度', 'Sun az/el', language):<22}: "
        f"{state.sun_azimuth_deg:7.3f}° / {state.sun_elevation_deg:+7.3f}°"
    )
    lines.append(
        f"  {_label('距角', 'Elongation', language):<22}: "
        f"{state.elongation_deg:.4f}°"
    )
    lines.append(
        f"  {_label('地心距', 'Geocentric distance', language):<22}: "
        f"{state.geo_distance_km:,.1f} km"
    )
    lines.append(
        f"  {_label('站心距', 'Topocentric distance', language):<22}: "
        f"{state.topo_distance_km:,.1f} km"
    )
    lines.append(
        f"  {_label('视直径', 'Angular diameter', language):<22}: "
        f"{state.angular_diameter_arcsec:.2f}\" ({state.angular_diameter_deg:.4f}°)"
    )
    lines.append(_THIN)
    lines.append(
        f"  {_label('月面地球直下点', 'Sub-Earth point', language):<22}: "
        f"lon {_signed(libration.sub_earth_lon_deg, 4)}°  "
        f"lat {_signed(libration.sub_earth_lat_deg, 4)}°"
    )
    lines.append(
        f"  {_label('日下点', 'Sub-solar point', language):<22}: "
        f"lon {libration.subsolar_lon_deg:8.4f}°  "
        f"lat {_signed(libration.subsolar_lat_deg, 4)}°"
    )
    lines.append(
        f"  {_label('余经度', 'Colongitude', language):<22}: "
        f"{libration.colongitude_deg:8.4f}°"
    )
    lines.append(
        f"  {_label('表面入射/发射角', 'Incidence/Emission', language):<22}: "
        f"{state.surface.incidence_deg:7.4f}° / {state.surface.emission_deg:7.4f}°"
    )
    lines.append(_THIN)
    lines.append(
        f"  {_label('轨道半长轴', 'Semi-major axis', language):<22}: "
        f"{orbit.semi_major_km:,.2f} km"
    )
    lines.append(
        f"  {_label('近/远地点', 'Peri/Apoapsis', language):<22}: "
        f"{orbit.periapsis_km:,.2f} / {orbit.apoapsis_km:,.2f} km"
    )
    lines.append(
        f"  {_label('偏心率', 'Eccentricity', language):<22}: {orbit.eccentricity:.6f}"
    )
    lines.append(
        f"  {_label('轨道倾角', 'Inclination', language):<22}: "
        f"{orbit.inclination_deg:.4f}°"
    )
    lines.append(
        f"  {_label('升交点/近地点幅角', 'Node/Arg. periapsis', language):<22}: "
        f"{orbit.ascending_node_deg:.4f}° / {orbit.arg_periapsis_deg:.4f}°"
    )
    lines.append(
        f"  {_label('朔望周期', 'Synodic-ish period', language):<22}: "
        f"{orbit.period_days:.4f} d"
    )
    lines.append(
        f"  {_label('月球速度', 'Moon velocity', language):<22}: "
        f"{state.velocity_kms:.4f} km/s"
    )
    lines.append(
        f"  {_label('光行时间', 'Light time', language):<22}: "
        f"{state.light_time_s:.6f} s"
    )
    lines.append(_RULE)
    return "\n".join(lines)


def format_moon_report(report: MoonReport, language: str = "both") -> str:
    """Full report: the state block plus rise, transit and set."""
    observer = report.observer
    events = report.rise_set
    lines = [format_moon_state(report.state, language)]
    lines.append(f"  {_label('周日运动', 'Diurnal events', language)}"
                 f"   (mode = {events.mode})")
    lines.append(_THIN)
    lines.append(
        f"  {_label('月出', 'Moonrise', language):<22}: "
        f"{_time_str(events.rise_utc, observer)}"
        + (
            f"   az {events.rise_azimuth_deg:.2f}°"
            if events.rise_azimuth_deg is not None
            else ""
        )
    )
    lines.append(
        f"  {_label('中天', 'Transit', language):<22}: "
        f"{_time_str(events.transit_utc, observer)}"
        + (
            f"   el {events.transit_elevation_deg:+.2f}°"
            if events.transit_elevation_deg is not None
            else ""
        )
    )
    lines.append(
        f"  {_label('月落', 'Moonset', language):<22}: "
        f"{_time_str(events.set_utc, observer)}"
        + (
            f"   az {events.set_azimuth_deg:.2f}°"
            if events.set_azimuth_deg is not None
            else ""
        )
    )
    lines.append(
        f"  {_label('下次月出/月落', 'Next rise / set', language):<22}: "
        f"{_clock(events.next_rise_utc, observer)} / {_clock(events.next_set_utc, observer)}"
    )
    if report.transit is not None:
        lines.append(
            f"  {_label('中天上边缘高度', 'Upper limb at transit', language):<22}: "
            f"{report.transit.upper_limb_elevation_deg:+.3f}°"
        )
    lines.append(_RULE)
    return "\n".join(lines)


def print_moon_report(report: MoonReport, language: str = "both") -> None:
    """Print :func:`format_moon_report`."""
    print(format_moon_report(report, language))


# ---------------------------------------------------------------------------
# phase calendar
# ---------------------------------------------------------------------------
def format_phase_calendar(
    entries: Sequence[PhaseCatalogEntry],
    observer: Optional[Observer] = None,
    language: str = "both",
) -> str:
    """Table of the four principal phases with distance and apparent size."""
    if not entries:
        return "No phase events."

    header = (
        f"{'#':>3}  {'Phase':<14}  {'UTC':^19}  {'Local':^19}  "
        f"{'Distance':>12}  {'Diameter':>10}  {'Elev':>8}"
    )
    lines = [_RULE, f"  {_label('月相历表', 'Principal phase calendar', language)}",
             _RULE]
    if observer is not None:
        lines.append(
            f"  {_label('观测地点', 'Site', language)}: "
            f"lat={observer.lat:.4f}  lon={observer.lon:.4f}  "
            f"{observer.timezone} ({observer.utc_offset_label})"
        )
    lines.append(header)
    lines.append(_THIN)

    for index, entry in enumerate(entries, start=1):
        event = entry.event
        moment = event.when_utc
        utc_text = f"{moment:%Y-%m-%d %H:%M:%S}"
        if observer is not None:
            local = moment.astimezone(observer.timezone)
            local_text = f"{local:%Y-%m-%d %H:%M:%S}"
        else:
            local_text = "-"
        elevation = (
            f"{entry.elevation_deg:+7.2f}"
            if entry.elevation_deg is not None
            else "      -"
        )
        phase_label = _label(event.kind.name_zh, event.kind.name_en, language)
        lines.append(
            f"{index:>3}  {phase_label:<14}  {utc_text:^19}  {local_text:^19}  "
            f"{entry.distance_km:>10,.0f} km  "
            f"{entry.angular_diameter_arcsec:>8.2f}\"  {elevation}"
        )

    lines.append(_THIN)
    counts: Dict[PhaseKind, int] = {}
    for entry in entries:
        counts[entry.kind] = counts.get(entry.kind, 0) + 1
    summary = "   ".join(
        f"{_label(kind.name_zh, kind.symbol, language)} {counts.get(kind, 0)}"
        for kind in PhaseKind
    )
    lines.append(f"  {_label('统计', 'Counts', language)}: {summary}")

    largest = max(entries, key=lambda item: item.angular_diameter_arcsec)
    smallest = min(entries, key=lambda item: item.angular_diameter_arcsec)
    lines.append(
        f"  {_label('最大视直径', 'Largest', language)}: "
        f"{largest.angular_diameter_arcsec:.2f}\" @ {largest.when_utc:%Y-%m-%d %H:%M} UTC "
        f"({largest.kind.name_en})"
    )
    lines.append(
        f"  {_label('最小视直径', 'Smallest', language)}: "
        f"{smallest.angular_diameter_arcsec:.2f}\" @ {smallest.when_utc:%Y-%m-%d %H:%M} UTC "
        f"({smallest.kind.name_en})"
    )
    lines.append(_RULE)
    return "\n".join(lines)


def print_phase_calendar(
    entries: Sequence[PhaseCatalogEntry],
    observer: Optional[Observer] = None,
    language: str = "both",
) -> None:
    print(format_phase_calendar(entries, observer, language))


# ---------------------------------------------------------------------------
# crescent windows
# ---------------------------------------------------------------------------
def format_crescent_windows(
    windows: Sequence[CrescentWindow],
    observer: Optional[Observer] = None,
    *,
    language: str = "both",
    ranked: bool = False,
    top_n: int = 20,
    group_by_age: bool = True,
) -> str:
    """Table of crescent observing sessions.

    Parameters
    ----------
    ranked
        Sort by score (best first) instead of chronologically.
    group_by_age
        When ranking, split into the three age groups the legacy table used:
        ``|age| < 1 d`` (extreme thin), ``1 <= |age| < 2 d`` (ideal), and
        ``|age| >= 2 d``.
    top_n
        Rows per group when ranking.
    """
    if not windows:
        return "No crescent sessions."

    def render(rows: Sequence[CrescentWindow], title: Optional[str]) -> List[str]:
        block: List[str] = []
        if title:
            block.append(f"  --- {title} ---")
        width_header = 'Width"'
        block.append(
            f"{'#':>3}  {'Local date':^11}  {'Best (local)':^8}  {'Period':^8}  "
            f"{'Age':>7}  {'Illum':>7}  {'Elong':>7}  {'Moon el':>8}  "
            f"{'Sun el':>7}  {width_header:>7}  {'Score':>6}  {'Yallop':>7}"
        )
        block.append(_THIN)
        for index, window in enumerate(rows, start=1):
            geometry = window.geometry
            period = (
                _label("昏", "Evening", language)
                if window.period == "evening"
                else _label("晨", "Morning", language)
            )
            yallop = window.visibility.get("yallop", {}).get("value")
            yallop_text = f"{yallop:+7.3f}" if yallop is not None else f"{'-':>7}"
            block.append(
                f"{index:>3}  {window.local_date:^11}  "
                f"{window.best_local:%H:%M:%S}  {period:^8}  "
                f"{_signed(window.age_days)}d  "
                f"{geometry.illumination_percent_geo:6.2f}%  "
                f"{geometry.elongation_deg:6.1f}°  "
                f"{_signed(geometry.moon_elevation_deg, 1)}°  "
                f"{_signed(geometry.sun_elevation_deg, 1)}°  "
                f"{geometry.crescent_width_arcsec:7.2f}  "
                f"{window.photo_score:6.2f}  {yallop_text}"
            )
        block.append("")
        return block

    lines = [_RULE, f"  {_label('月牙观测窗口', 'Crescent observing windows', language)}",
             _RULE]
    if observer is not None:
        lines.append(
            f"  {_label('观测地点', 'Site', language)}: "
            f"lat={observer.lat:.4f}  lon={observer.lon:.4f}  "
            f"{observer.timezone} ({observer.utc_offset_label})"
        )
    lines.append(
        f"  {_label('会话数', 'Sessions', language)}: {len(windows)}"
    )

    if not ranked:
        lines.extend(render(windows, None))
    else:
        groups = [
            (
                [w for w in windows if abs(w.age_days) < 1.0],
                _label(
                    "第一组 |月龄| < 1 天（极限细月牙）",
                    "Group A |age| < 1 d (extreme thin crescent)",
                    language,
                ),
            ),
            (
                [w for w in windows if 1.0 <= abs(w.age_days) < 2.0],
                _label(
                    "第二组 1 <= |月龄| < 2 天（最佳摄影条件）",
                    "Group B 1 <= |age| < 2 d (ideal for photography)",
                    language,
                ),
            ),
            (
                [w for w in windows if abs(w.age_days) >= 2.0],
                _label(
                    "第三组 |月龄| >= 2 天（较粗月牙）",
                    "Group C |age| >= 2 d (thicker crescent)",
                    language,
                ),
            ),
        ]
        for rows, title in groups:
            rows = sorted(rows, key=lambda item: item.photo_score)[:top_n]
            lines.extend(render(rows, title))

    lines.append(_RULE)
    return "\n".join(lines)


def print_crescent_windows(
    windows: Sequence[CrescentWindow],
    observer: Optional[Observer] = None,
    **kwargs: Any,
) -> None:
    print(format_crescent_windows(windows, observer, **kwargs))


def format_illumination_matches(
    matches: Sequence[IlluminationMatch], language: str = "both"
) -> str:
    """Table of moons matching a requested illumination."""
    if not matches:
        return "No matching moons."
    lines = [
        _RULE,
        f"  {_label('照明率匹配', 'Moons at a requested illumination', language)}",
        _RULE,
        f"{'#':>3}  {'Local month':^11}  {'UTC':^19}  {'Illum':>7}  "
        f"{'Phase':>8}  {'Dev':>6}  {'Elong':>7}  {'Elev':>7}  "
        f"{'Distance':>11}  {'Diameter':>9}  {'Waning':>7}",
        _THIN,
    ]
    for index, match in enumerate(matches, start=1):
        lines.append(
            f"{index:>3}  {match.when_utc:%Y-%m}     "
            f"{match.when_utc:%Y-%m-%d %H:%M:%S}  "
            f"{match.illumination_percent:6.2f}%  "
            f"{match.phase_angle_deg:7.2f}°  "
            f"{match.phase_deviation_deg:5.2f}°  "
            f"{match.elongation_deg:6.1f}°  "
            f"{_signed(match.elevation_deg, 1)}°  "
            f"{match.distance_km:>9,.0f} km  "
            f"{match.angular_diameter_arcsec:>7.2f}\"  "
            f"{'waxing' if match.waxing else 'waning':>7}"
        )
    lines.append(_RULE)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# limb targets
# ---------------------------------------------------------------------------
def format_target_summary(
    rows: Sequence[Dict[str, Any]], language: str = "both"
) -> str:
    """Table produced by :func:`moonlib.libration.summarize`."""
    if not rows:
        return "No targets."
    lines = [
        _RULE,
        f"  {_label('月面边缘目标统计', 'Limb target statistics', language)}",
        _RULE,
        f"{'Target':<30} {'Region':<12} {'r_eff':>7} {'Samples':>8} "
        f"{'Visible%':>9} {'Observ%':>8} {'MaxMarg':>8}  {'Best (UTC)':^19}",
        _THIN,
    ]
    for row in rows:
        name = str(row.get("name", row.get("english_name", "?")))
        if row.get("name_zh"):
            name = f"{name} {row['name_zh']}"
        lines.append(
            f"{name:<30} {str(row.get('region', '')):<12} "
            f"{row.get('effective_radius_deg', 0.0):7.3f} "
            f"{row.get('n_samples', 0):8d} "
            f"{row.get('percent_visible', 0.0):8.1f}% "
            f"{row.get('percent_observable', 0.0):7.1f}% "
            f"{row.get('max_margin_deg', 0.0):+8.2f}  "
            f"{str(row.get('best_when_utc') or '-')[:19]:^19}"
        )
    lines.append(_RULE)
    return "\n".join(lines)


def print_target_summary(rows: Sequence[Dict[str, Any]], language: str = "both") -> None:
    print(format_target_summary(rows, language))


def format_visible_targets(
    visibilities: Sequence[TargetVisibility], language: str = "both"
) -> str:
    """List of targets observable at a single instant."""
    if not visibilities:
        return "No observable targets."
    lines = [
        f"{'Target':<22} {'Grade':<10} {'gamma':>8} {'Margin':>8} "
        f"{'Sun inc':>8}  {'Moon el':>8}",
        _THIN,
    ]
    for visibility in sorted(
        visibilities, key=lambda item: (-item.grade.rank, -item.margin_deg)
    ):
        grade = _label(visibility.grade.name_zh, visibility.grade.value, language)
        lines.append(
            f"{visibility.target.english_name:<22} {grade:<10} "
            f"{visibility.gamma_deg:8.2f} {visibility.margin_deg:+8.2f} "
            f"{visibility.solar_incidence_deg:8.1f}  "
            f"{(visibility.moon_elevation_deg if visibility.moon_elevation_deg is not None else float('nan')):+8.2f}"
        )
    lines.append(_THIN)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# analemma
# ---------------------------------------------------------------------------
def format_analemma_extremes(stats: Dict[str, Any], language: str = "both") -> str:
    """Human-readable rendering of :func:`moonlib.analemma.analemma_extremes`."""

    def pick(key: str, field: str, default: float = 0.0) -> Any:
        block = stats.get(key) or {}
        return block.get(field, default)

    def moment(key: str) -> str:
        value = pick(key, "when_utc", None)
        return "-" if not value else str(value)[:19].replace("T", " ")

    return "\n".join(
        [
            _RULE,
            f"  {_label('太阳八字曲线极值', 'Solar analemma extremes', language)}",
            _RULE,
            f"  {_label('采样点数', 'Samples', language):<22}: {stats.get('n_points')}",
            f"  {_label('时间跨度', 'Span', language):<22}: "
            f"{stats.get('span_days', 0.0):.2f} d",
            f"  {_label('起止', 'First / last', language):<22}: "
            f"{str(stats.get('earliest'))[:10]} .. {str(stats.get('latest'))[:10]}",
            _THIN,
            f"  {_label('最大赤纬', 'Max declination', language):<22}: "
            f"{pick('declination_max', 'declination_deg'):+8.4f}\u00b0  "
            f"{moment('declination_max')}",
            f"  {_label('最小赤纬', 'Min declination', language):<22}: "
            f"{pick('declination_min', 'declination_deg'):+8.4f}\u00b0  "
            f"{moment('declination_min')}",
            f"  {_label('时差最大', 'Max equation of time', language):<22}: "
            f"{pick('equation_of_time_max', 'equation_of_time_minutes'):+8.3f} min  "
            f"{moment('equation_of_time_max')}",
            f"  {_label('时差最小', 'Min equation of time', language):<22}: "
            f"{pick('equation_of_time_min', 'equation_of_time_minutes'):+8.3f} min  "
            f"{moment('equation_of_time_min')}",
            _RULE,
        ]
    )


# ---------------------------------------------------------------------------
# whole-disk observation simulation
# ---------------------------------------------------------------------------
def format_moon_view(view: Any, language: str = "both") -> str:
    """Geometry block for a simulated observation of the lunar disk.

    Reports the two things that decide what the disk looks like: where the
    observer and the Sun sit in selenographic coordinates (libration and
    sub-solar point), and how bright the earthshine is relative to sunlight.
    """
    lines = [_RULE]
    lines.append(
        f"  {_label('月面观测几何', 'Lunar disk geometry', language)}"
        f"   {view.when_utc:%Y-%m-%d %H:%M:%S} UTC"
    )
    lines.append(_RULE)
    observer = view.observer
    lines.append(
        f"  {_label('观测地', 'Site', language):<22}: {observer.name}  "
        f"({observer.lat:+.4f}°, {observer.lon:+.4f}°, {observer.height_km:.3f} km)"
    )
    lines.append(
        f"  {_label('站心距离', 'Distance', language):<22}: "
        f"{view.distance_km:,.1f} km"
    )
    lines.append(
        f"  {_label('视直径', 'Angular diameter', language):<22}: "
        f"{view.angular_diameter_deg:.5f}°  "
        f"({view.angular_diameter_deg * 3600.0:.2f}\")"
    )
    lines.append(_THIN)
    lines.append(
        f"  {_label('天平动', 'Libration', language):<22}: "
        f"lon {view.libration_lon_deg:+.3f}°   lat {view.libration_lat_deg:+.3f}°"
    )
    lines.append(
        f"  {_label('日下点', 'Sub-solar point', language):<22}: "
        f"lon {view.sub_solar_lon_deg:+.3f}°   lat {view.sub_solar_lat_deg:+.3f}°"
        f"   (colongitude {view.colongitude_deg:.3f}°)"
    )
    lines.append(
        f"  {_label('相位角', 'Phase angle', language):<22}: "
        f"{view.phase_angle_deg:.3f}°"
    )
    lines.append(
        f"  {_label('照明率', 'Illuminated', language):<22}: "
        f"{view.illuminated_percent:.3f} %"
    )
    lines.append(
        f"  {_label('亮边位置角', 'Bright limb PA', language):<22}: "
        f"{view.bright_limb_position_angle_deg:.2f}°"
    )
    if view.moon_elevation_deg is not None:
        lines.append(
            f"  {_label('月亮高度', 'Moon elevation', language):<22}: "
            f"{view.moon_elevation_deg:+.2f}°"
        )
    lines.append(_THIN)
    lines.append(
        f"  {_label('地照几何', 'Earthshine geometry', language)}"
    )
    lines.append(
        f"  {_label('地心距', 'Earth distance', language):<22}: "
        f"{view.earth_distance_km:,.1f} km"
    )
    lines.append(
        f"  {_label('地球视直径', 'Earth diameter', language):<22}: "
        f"{view.earth_angular_diameter_deg:.4f}°"
    )
    lines.append(
        f"  {_label('地球相位角', 'Earth phase', language):<22}: "
        f"{view.earth_phase_angle_deg:.3f}°   "
        f"({_label('照明', 'lit', language)} {view.earth_illuminated_percent:.2f} %)"
    )
    lines.append(
        f"  {_label('地照/日照', 'Earthshine / sunlight', language):<22}: "
        f"{view.earthshine_ratio:.4e}"
    )
    lines.append(
        f"  {_label('地球视星等', 'Earth magnitude', language):<22}: "
        f"{view.earthshine_magnitude:+.2f}"
    )
    lines.append(
        f"  {_label('暗面/亮面', 'Dark side / bright', language):<22}: "
        f"{view.earthshine_magnitudes_below_sunlight:.2f} mag"
    )
    lines.append(_RULE)
    return "\n".join(lines)


def format_render_summary(render: Any, language: str = "both") -> str:
    """One block of numbers describing a finished render."""
    stats = render.stats
    settings = render.settings
    lines = [_THIN]
    lines.append(f"  {_label('渲染', 'Render', language)}   "
                 f"{settings.width}x{settings.height}  "
                 f"{settings.samples_per_pixel}x{settings.samples_per_pixel} spp  "
                 f"{stats.get('photometric_model', '?')}  {stats.get('tone', '?')}")
    lines.append(
        f"  {_label('地形层级', 'Terrain level', language):<22}: "
        f"{stats.get('pyramid_level', '?')}  "
        f"({stats.get('terrain_km_per_pixel', 0.0):.2f} km/px, "
        f"{_label('采样', 'sample', language)} "
        f"{stats.get('sample_km', 0.0):.2f} km)"
    )
    lines.append(
        f"  {_label('月面覆盖', 'Disk coverage', language):<22}: "
        f"{100.0 * stats.get('disk_fraction_of_frame', 0.0):.2f} % of frame, "
        f"{stats.get('disk_pixels', 0)} px"
    )
    lines.append(
        f"  {_label('被照亮比例', 'Sunlit fraction', language):<22}: "
        f"{100.0 * stats.get('sunlit_fraction_of_disk', 0.0):.2f} %  "
        f"({_label('阴影', 'shadowed', language)} "
        f"{100.0 * stats.get('shadowed_fraction_of_disk', 0.0):.2f} %)"
    )
    lines.append(
        f"  {_label('日照亮度峰值', 'Peak sunlit I/F', language):<22}: "
        f"{stats.get('peak_sunlit_radiance', 0.0):.5f}"
    )
    lines.append(
        f"  {_label('地照亮度峰值', 'Peak earthshine I/F', language):<22}: "
        f"{stats.get('peak_earthshine_radiance', 0.0):.3e}"
    )
    if "earthshine_magnitudes_below_sunlit" in stats:
        lines.append(
            f"  {_label('暗面/亮面', 'Dark / bright', language):<22}: "
            f"{stats['earthshine_magnitudes_below_sunlit']:.2f} mag"
        )
    lines.append(_THIN)
    return "\n".join(lines)


def format_region_summary(render: Any, language: str = "both") -> str:
    """Block describing a close-up: where it points and what it can resolve."""
    region = getattr(render, "region", None)
    if region is None:  # pragma: no cover - guarded by the caller
        return format_render_summary(render, language)
    stats = render.stats
    lines = [_THIN]
    lines.append(
        f"  {_label('月面特写', 'Surface close-up', language)}   "
        f"({region.lon_deg:+.3f}, {region.lat_deg:+.3f})  "
        f"{region.width}x{region.height}  {stats.get('tone', '?')}"
    )
    lines.append(
        f"  {_label('视场', 'Field of view', language):<24}: "
        f"{region.fov_arcsec:.1f}\"  "
        f"({_label('天球跨度', 'sky span', language)} {region.span_km:.0f} km)"
    )
    lines.append(
        f"  {_label('月面跨度', 'Surface span', language):<24}: "
        f"{region.span_km:.0f} km "
        f"{_label('垂直方向', 'across', language)} / "
        f"{region.surface_span_km:.0f} km "
        f"{_label('倾斜方向', 'along tilt', language)}"
    )
    lines.append(
        f"  {_label('出射角', 'Emergence', language):<24}: "
        f"{region.emergence_deg:.2f}°  "
        f"({_label('压缩', 'foreshortening', language)} "
        f"{region.foreshortening:.3f})"
    )
    lines.append(
        f"  {_label('入射角', 'Incidence', language):<24}: "
        f"{region.incidence_deg:.2f}°  "
        f"({_label('日高', 'sun elevation', language)} "
        f"{region.solar_elevation_deg:+.2f}°)"
    )
    lines.append(
        f"  {_label('地元分辨率', 'Ground sample', language):<24}: "
        f"{region.ground_sample_km * 1000:.0f} m "
        f"{_label('垂直', 'across', language)} / "
        f"{region.tilted_ground_sample_km * 1000:.0f} m "
        f"{_label('倾斜', 'along', language)}"
    )
    terrain_px = region.terrain_km_per_pixel * 1000.0
    verdict = (
        _label('插值放大', 'interpolated', language)
        if region.terrain_limited
        else _label('数据支持', 'data-supported', language)
    )
    lines.append(
        f"  {_label('地形数据', 'Terrain data', language):<24}: "
        f"{terrain_px:.0f} m/px   {verdict} "
        f"({region.resolution_ratio:.2f}x)"
    )
    lines.append(
        f"  {_label('阴影比例', 'Shadowed', language):<24}: "
        f"{100.0 * stats.get('shadowed_fraction_of_disk', 0.0):.2f} %"
    )
    lines.append(_THIN)
    return "\n".join(lines)
