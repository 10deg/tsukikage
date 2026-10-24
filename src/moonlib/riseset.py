"""Moonrise, transit (culmination) and moonset.

Semantics — a deliberate fix
----------------------------
The legacy implementation searched each event independently over a fixed
48-hour window: the last moonrise *before* the reference instant, the first
moonset *after* it, and the globally highest transit anywhere in the window.
Those three could belong to different diurnal cycles.

Here the **transit nearest the reference instant** defines the cycle.  The
moonrise reported is the last rise at or before that transit and the moonset is
the first set at or after it, so ``rise <= transit <= set`` always holds and the
three describe one physical passage of the Moon.  ``next_rise`` and
``next_set`` are additionally provided relative to the reference instant, which
is what a user interface usually wants to display.

Rise/set convention
-------------------
``observational`` (default) places the Moon's **centre** at +0.125°, which
corresponds to the upper limb touching the visible horizon once atmospheric
refraction (~34′), the semi-diameter (~15.5′) and the horizontal parallax
(~57′) are accounted for — the USNO/JPL convention.  ``geometric`` uses a
centre elevation of 0° with no atmosphere.
"""

from __future__ import annotations

import enum
import math
from typing import Optional, Tuple

import numpy as np

from .errors import MoonlibError
from .frames import az_el, geometric_position
from .kernels import kernel_session
from .models import RiseSetEvents, TransitDetails
from .numerics import bisect_root, golden_minimum
from .observer import Observer
from .timeutil import SECONDS_PER_HOUR, to_et, to_utc, utc_now

__all__ = [
    "RiseSetMode",
    "RISE_SET_THRESHOLD_DEG",
    "rise_set_transit",
    "transit_details",
]

#: Centre-elevation thresholds (degrees) for the two rise/set conventions.
RISE_SET_THRESHOLD_DEG = {
    "observational": 0.125,
    "geometric": 0.0,
}


class RiseSetMode(str, enum.Enum):
    """Rise/set elevation convention."""

    OBSERVATIONAL = "observational"
    GEOMETRIC = "geometric"

    @property
    def threshold_deg(self) -> float:
        return RISE_SET_THRESHOLD_DEG[self.value]


# ---------------------------------------------------------------------------
# internal sampling helpers
# ---------------------------------------------------------------------------
def _elevation(et: float, observer: Observer) -> float:
    return az_el("MOON", et, observer).elevation_deg


def _elevation_grid(ets: np.ndarray, observer: Observer) -> np.ndarray:
    """Elevation of the Moon over a grid of ephemeris times (degrees)."""
    out = np.empty(ets.size, dtype=float)
    for index, et in enumerate(ets):
        out[index] = _elevation(float(et), observer)
    return out


def _refine_transit(
    observer: Observer, low: float, high: float
) -> Tuple[float, float]:
    """Golden-section refinement of the elevation maximum inside a bracket."""
    return golden_minimum(
        lambda t: -_elevation(t, observer), low, high, tol=1e-3, max_iter=80
    )


def _crossing(
    observer: Observer,
    threshold: float,
    low: float,
    high: float,
) -> Optional[float]:
    """Refine a threshold crossing inside ``[low, high]``."""
    try:
        return bisect_root(
            lambda t: _elevation(t, observer) - threshold, low, high, tol=1e-3
        )
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def rise_set_transit(
    observer: Observer,
    when=None,
    mode: "RiseSetMode | str" = RiseSetMode.OBSERVATIONAL,
    *,
    transit_step_s: float = 600.0,
    event_step_s: float = 240.0,
    search_hours: float = 30.0,
) -> RiseSetEvents:
    """Moonrise, transit and moonset for the cycle containing *when*.

    Parameters
    ----------
    observer
        Observing site.
    when
        Reference instant; anything accepted by :func:`moonlib.timeutil.to_et`.
        Defaults to now.
    mode
        ``'observational'`` (default) or ``'geometric'``.
    transit_step_s
        Coarse step used to locate the transit (seconds).
    event_step_s
        Coarse step used to locate rise/set crossings (seconds).  Four minutes
        is fine for mid-latitudes; shorten it if you observe above ~65°
        latitude where horizon grazes can be brief.
    search_hours
        Half-width of the rise/set search window around the transit.

    Returns
    -------
    RiseSetEvents
        ``rise_utc`` / ``set_utc`` may be ``None`` when no crossing occurs
        within the window (polar day/night or a circumpolar Moon).
    """
    observer = Observer.from_value(observer)
    if isinstance(mode, RiseSetMode):
        resolved_mode = mode
    else:
        try:
            resolved_mode = RiseSetMode(mode)
        except ValueError as exc:
            raise MoonlibError(
                f"Unknown rise/set mode {mode!r}; use 'observational' or 'geometric'."
            ) from exc
    threshold = resolved_mode.threshold_deg

    et_ref = to_et(utc_now() if when is None else when)

    # ---- 1. locate the transit nearest the reference instant --------------
    half_span = 12.6 * SECONDS_PER_HOUR
    transit_grid = np.arange(
        et_ref - half_span, et_ref + half_span + transit_step_s, transit_step_s
    )
    transit_elev = _elevation_grid(transit_grid, observer)

    candidates = [
        index
        for index in range(1, transit_grid.size - 1)
        if transit_elev[index] >= transit_elev[index - 1]
        and transit_elev[index] >= transit_elev[index + 1]
    ]
    if not candidates:
        candidates = [int(np.argmax(transit_elev))]

    transit_et = None
    transit_elevation = None
    best_distance = None
    for index in candidates:
        low = float(transit_grid[max(0, index - 1)])
        high = float(transit_grid[min(transit_grid.size - 1, index + 1)])
        et_peak, neg_elevation = _refine_transit(observer, low, high)
        distance = abs(et_peak - et_ref)
        if best_distance is None or distance < best_distance:
            best_distance = distance
            transit_et = et_peak
            transit_elevation = -neg_elevation

    assert transit_et is not None  # candidates is never empty

    # ---- 2. rise / set crossings around that transit ----------------------
    span = search_hours * SECONDS_PER_HOUR
    grid = np.arange(
        transit_et - span, transit_et + span + event_step_s, event_step_s
    )
    elev = _elevation_grid(grid, observer)
    above = elev - threshold

    transit_index = int(np.argmin(np.abs(grid - transit_et)))

    rise_et: Optional[float] = None
    for index in range(transit_index - 1, 0, -1):
        if above[index] < 0.0 <= above[index + 1]:
            rise_et = _crossing(
                observer, threshold, float(grid[index]), float(grid[index + 1])
            )
            break

    set_et: Optional[float] = None
    for index in range(transit_index, grid.size - 1):
        if above[index] >= 0.0 > above[index + 1]:
            set_et = _crossing(
                observer, threshold, float(grid[index]), float(grid[index + 1])
            )
            break

    # ---- 3. next rise / next set relative to the reference instant --------
    next_rise_et: Optional[float] = None
    for index in range(0, grid.size - 1):
        if grid[index] < et_ref:
            continue
        if above[index] < 0.0 <= above[index + 1]:
            next_rise_et = _crossing(
                observer, threshold, float(grid[index]), float(grid[index + 1])
            )
            break

    next_set_et: Optional[float] = None
    for index in range(0, grid.size - 1):
        if grid[index] < et_ref:
            continue
        if above[index] >= 0.0 > above[index + 1]:
            next_set_et = _crossing(
                observer, threshold, float(grid[index]), float(grid[index + 1])
            )
            break

    return RiseSetEvents(
        rise_utc=None if rise_et is None else to_utc(rise_et),
        transit_utc=to_utc(transit_et),
        set_utc=None if set_et is None else to_utc(set_et),
        mode=resolved_mode.value,
        rise_azimuth_deg=(
            None if rise_et is None else az_el("MOON", rise_et, observer).azimuth_deg
        ),
        set_azimuth_deg=(
            None if set_et is None else az_el("MOON", set_et, observer).azimuth_deg
        ),
        transit_elevation_deg=transit_elevation,
        next_rise_utc=None if next_rise_et is None else to_utc(next_rise_et),
        next_set_utc=None if next_set_et is None else to_utc(next_set_et),
    )


def transit_details(observer: Observer, transit_et: float) -> TransitDetails:
    """Elevation, azimuth and apparent size of the Moon at a transit instant."""
    observer = Observer.from_value(observer)
    horizontal = az_el("MOON", transit_et, observer)
    constants = kernel_session().constants
    distance = float(
        np.linalg.norm(geometric_position("MOON", transit_et, center="EARTH"))
    )
    diameter_deg = math.degrees(
        2.0 * math.atan2(constants.moon_radius_km, distance)
    )
    return TransitDetails(
        when_utc=to_utc(transit_et),
        et=float(transit_et),
        elevation_deg=horizontal.elevation_deg,
        azimuth_deg=horizontal.azimuth_deg,
        angular_diameter_arcsec=diameter_deg * 3600.0,
    )
