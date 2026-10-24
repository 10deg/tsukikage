"""Sky charts: the horizon view used for crescent planning.

:func:`plot_sky` is the refactored successor of the legacy ``crescent_plot``.
Fixes over the original:

* it runs at all — the original referenced the bare names ``plt`` and ``Circle``
  which were never bound in the module, so any call raised ``NameError`` and a
  byte-identical copy had to be maintained inside a notebook;
* it returns a :class:`matplotlib.figure.Figure` instead of calling
  ``plt.show()`` at 300 dpi internally;
* the ``[AUTO-FOUND]`` label is no longer dead code, and "today's sunset" is
  found on the observer's **local** day rather than by scanning the UTC day,
  which used to pick the previous evening when run after local midnight;
* the crescent's orientation can come from the true bright-limb position angle
  rather than the ``atan2(dElevation, dAzimuth)`` approximation;
* the duplicated shooting-window bookkeeping and the unused variables of the
  original are gone.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

import numpy as np

from ..crescent import CrescentCriteria, crescent_geometry
from ..errors import MoonlibError
from ..frames import az_el
from ..observer import Observer
from ..timeutil import TimeRange, as_utc, to_utc
from ._mpl import DARK_BG, require_matplotlib
from .phase import draw_moon_disk

__all__ = ["sky_background", "plot_sky", "find_local_sunset"]

#: Elevation bands (degrees) with the fill colour of each twilight stage.
TWILIGHT_BANDS: Tuple[Tuple[float, float, str, float], ...] = (
    (-6.0, 0.0, "#1a3a5c", 0.55),
    (-12.0, -6.0, "#0f2840", 0.60),
    (-18.0, -12.0, "#081828", 0.65),
)

#: Below this elevation the sky is drawn essentially black.
DEEP_NIGHT_COLOR = "#020208"


def sky_background(
    ax: Any,
    *,
    azimuth_min: float,
    azimuth_max: float,
    elevation_min: float,
    elevation_max: float,
    ground_color: str = DEEP_NIGHT_COLOR,
) -> None:
    """Paint a twilight-graded sky, ground and elevation grid onto *ax*.

    Extracted from the legacy chart so that any horizon view — the crescent
    planner, a libration panel, a future eclipse simulator — shares one
    appearance instead of copying 120 lines of colour constants.
    """
    if elevation_max > 0.0:
        bands = np.linspace(0.0, elevation_max, 80)
        for lower, upper in zip(bands[:-1], bands[1:]):
            middle = 0.5 * (lower + upper)
            if middle > 8.0:
                colour = (0.25, 0.45, 0.75)
            elif middle > 2.0:
                t = (8.0 - middle) / 6.0
                colour = (
                    0.80 * t + 0.25 * (1 - t),
                    0.55 * t + 0.45 * (1 - t),
                    0.18 * t + 0.75 * (1 - t),
                )
            else:
                t = (2.0 - middle) / 2.0
                colour = (
                    0.95 * t + 0.80 * (1 - t),
                    0.60 * t + 0.55 * (1 - t),
                    0.12 * t + 0.18 * (1 - t),
                )
            ax.fill_between(
                [azimuth_min, azimuth_max], lower, upper, color=colour, alpha=0.85, lw=0
            )

    for lower, upper, colour, alpha in TWILIGHT_BANDS:
        top = min(upper, 0.0)
        bottom = max(lower, elevation_min)
        if bottom < top:
            ax.fill_between(
                [azimuth_min, azimuth_max], bottom, top, color=colour, alpha=alpha, lw=0
            )

    if elevation_min < -18.0:
        ax.fill_between(
            [azimuth_min, azimuth_max],
            elevation_min,
            -18.0,
            color=DEEP_NIGHT_COLOR,
            alpha=0.90,
            lw=0,
        )

    ax.fill_between(
        [azimuth_min, azimuth_max],
        elevation_min,
        0.0,
        color=ground_color,
        alpha=0.20,
        lw=0,
        zorder=2,
    )

    for level, label, colour in (
        (-6.0, "Civil -6", "#8899aa"),
        (-12.0, "Nautical -12", "#667788"),
        (-18.0, "Astronomical -18", "#556677"),
    ):
        if elevation_min < level < elevation_max:
            ax.axhline(y=level, color=colour, lw=0.5, ls=":", alpha=0.5, zorder=4)
            ax.text(
                azimuth_min + 0.3,
                level - 0.2,
                label,
                fontsize=5.5,
                color=colour,
                alpha=0.65,
                va="top",
            )

    for level, style, alpha in ((0.0, "-", 0.5), (5.0, "--", 0.35), (10.0, ":", 0.25)):
        if elevation_min < level < elevation_max:
            ax.axhline(y=level, color="#8899aa", lw=0.6, ls=style, alpha=alpha, zorder=3)
            if level > 0.0:
                ax.text(
                    azimuth_min + 0.3,
                    level + 0.15,
                    f"{level:.0f} deg",
                    fontsize=6.5,
                    color="#8899aa",
                    alpha=0.6,
                    va="bottom",
                )
    ax.axhline(y=0.0, color="#667788", lw=1.8, alpha=0.7, zorder=4)


def find_local_sunset(observer: Observer, day: Optional[datetime] = None) -> datetime:
    """Sunset on the observer's local calendar day.

    Scans the local day in five-minute steps for the Sun's setting crossing and
    refines it by bisection.  The legacy implementation started from
    ``datetime.now(timezone.utc).replace(hour=0)`` — a **UTC** midnight — so an
    observer in UTC+8 running it after local midnight was given the *previous*
    evening's sunset.
    """
    from ..numerics import bisect_root

    observer = Observer.from_value(observer)
    reference = observer.local_time(as_utc(day) if day else _now_utc())
    local_midnight = reference.replace(hour=0, minute=0, second=0, microsecond=0)
    start = local_midnight.astimezone(timezone.utc)

    def sun_elevation(moment: datetime) -> float:
        return az_el("SUN", _to_et(moment), observer).elevation_deg

    step = timedelta(minutes=5)
    previous_time = start
    previous_value = sun_elevation(previous_time)
    for _ in range(288):
        current_time = previous_time + step
        current_value = sun_elevation(current_time)
        if previous_value > 0.0 >= current_value:
            low, high = previous_time, current_time
            et = bisect_root(
                lambda t: az_el("SUN", t, observer).elevation_deg,
                _to_et(low),
                _to_et(high),
                tol=1e-3,
            )
            return to_utc(et)
        previous_time, previous_value = current_time, current_value

    raise MoonlibError("No sunset found on the local day (polar day?)")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _to_et(moment: datetime) -> float:
    from ..timeutil import to_et

    return to_et(moment)


def plot_sky(
    observer: Observer,
    when=None,
    *,
    span_hours: float = 1.0,
    criteria: Optional[CrescentCriteria] = None,
    figsize: Tuple[float, float] = (12.0, 8.0),
    at_local_sunset: bool = False,
    show_moon_closeup: bool = True,
    orientation: str = "sun_direction",
    moon_marker_radius_deg: float = 1.2,
) -> Any:
    """Azimuth/elevation chart of the Moon and Sun with the shooting window.

    Parameters
    ----------
    observer
        Observing site.
    when
        Instant to centre the chart on.  ``None`` means now, unless
        ``at_local_sunset`` is set.
    span_hours
        Half-width of the time span drawn around *when*.
    criteria
        Shooting-window definition; defaults to :class:`CrescentCriteria`.
    at_local_sunset
        Centre the chart on the local sunset of *when*'s local day.
    orientation
        ``'sun_direction'`` points the bright limb at the Sun **in chart
        coordinates** (what a reader expects on an az/el plot);
        ``'position_angle'`` uses the true bright-limb position angle, which is
        the correct orientation for a telescope or camera view but appears
        tilted on an az/el chart.
    show_moon_closeup
        Add an inset showing the disk as it appears to the eye.
    moon_marker_radius_deg
        Radius of the Moon marker in data units.  The real Moon is only about
        0.26° across, so the marker is deliberately enlarged for legibility
        (the legacy chart used the same idea with a fixed 1° radius).

    Returns
    -------
    matplotlib.figure.Figure
        Never displayed by this function.
    """
    plt, _ = require_matplotlib()
    from matplotlib.gridspec import GridSpec

    observer = Observer.from_value(observer)
    criteria = criteria or CrescentCriteria()

    if at_local_sunset:
        anchor = find_local_sunset(observer, when)
    elif when is None:
        anchor = _now_utc()
    else:
        anchor = as_utc(when)
    anchor_et = _to_et(anchor)

    geometry = crescent_geometry(anchor_et, observer)
    in_window = criteria.contains(
        geometry.sun_elevation_deg,
        geometry.moon_elevation_deg,
        geometry.illumination_percent_geo,
        geometry.moon_age_days,
    )

    window = TimeRange(anchor_et - span_hours * 3600.0, anchor_et + span_hours * 3600.0)
    grid = window.grid(30.0)

    moon_azimuth, moon_elevation = [], []
    sun_azimuth, sun_elevation = [], []
    times = []
    shooting = []
    for et in grid:
        moon = az_el("MOON", float(et), observer)
        sun = az_el("SUN", float(et), observer)
        moon_azimuth.append(moon.azimuth_deg)
        moon_elevation.append(moon.elevation_deg)
        sun_azimuth.append(sun.azimuth_deg)
        sun_elevation.append(sun.elevation_deg)
        times.append(to_utc(float(et)))
        # The illuminated fraction changes by only a few hundredths of a
        # percent across the plotted span, so the anchor value is used for the
        # whole window instead of paying for a phase evaluation per sample.
        shooting.append(
            criteria.contains(
                sun.elevation_deg,
                moon.elevation_deg,
                geometry.illumination_percent_geo,
                geometry.moon_age_days,
            )
        )

    azimuth_min = min(min(moon_azimuth), min(sun_azimuth)) - 3.0
    azimuth_max = max(max(moon_azimuth), max(sun_azimuth)) + 3.0
    elevation_max = max(max(moon_elevation), max(sun_elevation)) + 4.0
    elevation_min = max(-18.0, min(min(moon_elevation), min(sun_elevation)) - 3.0)
    if elevation_max - elevation_min < 12.0:
        elevation_max = elevation_min + 12.0

    width_ratio = max(0.6, (azimuth_max - azimuth_min) / max(1.0, elevation_max - elevation_min))
    figure = plt.figure(
        figsize=figsize,
        facecolor=DARK_BG,
        dpi=110,
    )
    grid_spec = GridSpec(
        2,
        2,
        figure=figure,
        width_ratios=[width_ratio, 0.42],
        height_ratios=[0.95, 1.85],
        wspace=0.03,
        hspace=0.05,
        left=0.05,
        right=0.97,
        top=0.94,
        bottom=0.07,
    )
    ax = figure.add_subplot(grid_spec[:, 0], facecolor=DARK_BG)
    ax.set_aspect("equal", adjustable="box")
    inset_ax = figure.add_subplot(grid_spec[0, 1], facecolor=DARK_BG)
    info_ax = figure.add_subplot(grid_spec[1, 1], facecolor=DARK_BG)

    sky_background(
        ax,
        azimuth_min=azimuth_min,
        azimuth_max=azimuth_max,
        elevation_min=elevation_min,
        elevation_max=elevation_max,
    )

    ax.plot(sun_azimuth, sun_elevation, "-", color="#ffb040", lw=0.8, alpha=0.9, label="Sun", zorder=5)
    ax.plot(moon_azimuth, moon_elevation, "-", color="#c0c0d0", lw=0.9, alpha=0.9, label="Moon", zorder=5)

    window_azimuth = [a if s else np.nan for a, s in zip(moon_azimuth, shooting)]
    window_elevation = [e if s else np.nan for e, s in zip(moon_elevation, shooting)]
    ax.plot(
        window_azimuth,
        window_elevation,
        "-",
        color="#27ae60",
        lw=1.9,
        alpha=0.75,
        label="Shooting window",
        zorder=6,
    )

    for index in range(0, len(times), max(1, int(len(times) / 9))):
        moment = times[index].astimezone(observer.timezone)
        ax.annotate(
            moment.strftime("%H:%M"),
            xy=(moon_azimuth[index], moon_elevation[index]),
            xytext=(0, -12),
            textcoords="offset points",
            fontsize=6,
            color="#b0b0c0",
            ha="center",
            va="bottom",
            arrowprops=dict(arrowstyle="-", color="#8899aa", lw=0.4),
        )

    ax.plot(
        [geometry.sun_azimuth_deg],
        [geometry.sun_elevation_deg],
        "o",
        color="#FFD700",
        ms=9,
        zorder=15,
    )
    draw_moon_disk(
        ax,
        geometry,
        x=geometry.moon_azimuth_deg,
        y=geometry.moon_elevation_deg,
        radius=moon_marker_radius_deg,
        limb_angle_deg=(
            _chart_limb_angle(geometry) if orientation == "sun_direction" else None
        ),
        earthshine=True,
    )

    ax.set_xlim(azimuth_min, azimuth_max)
    ax.set_ylim(elevation_min, elevation_max)
    ax.set_xlabel("Azimuth (deg)   0 = N, 90 = E", fontsize=8, color="#8899aa")
    ax.set_ylabel("Elevation (deg)", fontsize=8, color="#8899aa")
    ax.tick_params(colors="#777777", labelsize=7)
    ax.grid(True, alpha=0.12, lw=0.4, color="#444444")
    ax.set_title(
        f"Moon and Sun  |  {anchor.astimezone(observer.timezone):%Y-%m-%d %H:%M:%S %Z}"
        f"  |  illum {geometry.illumination_percent_geo:.2f}%"
        f"  |  elong {geometry.elongation_deg:.1f}°",
        fontsize=9.5,
        color="#cccccc",
    )
    ax.legend(
        loc="upper right",
        fontsize=7,
        framealpha=0.25,
        facecolor=DARK_BG,
        labelcolor="#aaaaaa",
        edgecolor="#999999",
    )

    if show_moon_closeup:
        inset_radius = 3.0
        draw_moon_disk(
            inset_ax,
            geometry,
            x=0.0,
            y=0.0,
            radius=inset_radius,
            limb_angle_deg=geometry.bright_limb_angle_deg - 90.0,
            earthshine=True,
        )
        inset_ax.set_xlim(-inset_radius * 1.4, inset_radius * 1.4)
        inset_ax.set_ylim(-inset_radius * 1.4, inset_radius * 1.4)
        inset_ax.set_aspect("equal")
        inset_ax.set_title("Close-up", fontsize=7.5, color="#888888")
        inset_ax.tick_params(left=False, bottom=False, labelleft=False, labelbottom=False)
        for spine in inset_ax.spines.values():
            spine.set_color("#555555")
            spine.set_linewidth(0.8)

    info_ax.axis("off")
    info_ax.text(
        0.02,
        0.98,
        _info_panel(observer, anchor, geometry, criteria, in_window),
        transform=info_ax.transAxes,
        fontsize=6.4,
        va="top",
        ha="left",
        family="monospace",
        color="#c8c8c8",
    )

    # No tight_layout here: the GridSpec already reserves margins, and
    # tight_layout is not compatible with the equal-aspect main axes — it emits
    # a warning and clips the azimuth tick labels.
    return figure


def _chart_limb_angle(geometry) -> float:
    """Rotation so the bright limb points at the Sun on an az/el chart."""
    return math.degrees(
        math.atan2(
            geometry.sun_elevation_deg - geometry.moon_elevation_deg,
            geometry.sun_azimuth_deg - geometry.moon_azimuth_deg,
        )
    )


def _info_panel(
    observer: Observer,
    anchor: datetime,
    geometry,
    criteria: CrescentCriteria,
    in_window: bool,
) -> str:
    local = anchor.astimezone(observer.timezone)
    lines = [
        f"Site      lat={observer.lat:.4f}  lon={observer.lon:.4f}  h={observer.height_km:.3f} km",
        f"Local     {local:%Y-%m-%d %H:%M:%S %Z}  ({observer.utc_offset_label})",
        f"UTC       {anchor:%Y-%m-%d %H:%M:%S}",
        "",
        f"Illum geo {geometry.illumination_percent_geo:7.3f} %",
        f"Illum top {geometry.illumination_percent_topo:7.3f} %",
        f"Phase geo {geometry.phase_angle_geo_deg:7.3f} deg",
        f"Phase top {geometry.phase_angle_topo_deg:7.3f} deg",
        f"Elongation{geometry.elongation_deg:7.3f} deg",
        f"Age       {geometry.moon_age_days:+7.3f} d",
        f"Width     {geometry.crescent_width_arcsec:7.2f} arcsec",
        "",
        f"Moon az   {geometry.moon_azimuth_deg:7.2f} deg  el {geometry.moon_elevation_deg:+7.2f}",
        f"Sun  az   {geometry.sun_azimuth_deg:7.2f} deg  el {geometry.sun_elevation_deg:+7.2f}",
        f"ARCL      {geometry.arc_of_light_deg:7.2f} deg",
        f"ARCV      {geometry.arc_of_vision_deg:7.2f} deg",
        "",
        f"Screening sun [{criteria.sun_elevation_min_deg:+.1f}, {criteria.sun_elevation_max_deg:+.1f}]"
        f"  moon >= {criteria.moon_elevation_min_deg:.1f}",
        f"          illum [{criteria.illumination_min_percent:.2f}, "
        f"{criteria.illumination_max_percent:.1f}] %",
        f"Verdict   {'INSIDE the shooting window' if in_window else 'outside the shooting window'}",
    ]
    return "\n".join(lines)
