"""Lunar phase rendering.

Conventions — read this before using :func:`moon_phase_patch`
-------------------------------------------------------------
The legacy ``moon_phase_patch`` documented its ``phase_angle`` argument with one
convention ("0 = new, 90 = quarter, 180 = full") while
:mod:`moonlib.illumination` uses the physically standard one ("0 = full,
180 = new"), and callers bridged the two with an undocumented ``180 - pa``.
That is how sign errors happen.

Here the public argument is :class:`moonlib.models.CrescentGeometry`-friendly:

``illumination_percent``
    The illuminated fraction as a percentage, ``0`` = new, ``100`` = full.
    This is the value the rest of the library reports, so no conversion is
    needed anywhere.

For callers that only have a phase angle, :func:`phase_angle_to_illumination`
does the conversion explicitly, and the function accepts either.

The lit side is drawn to the **right** of the disk when ``limb_angle_deg`` is
zero; ``limb_angle_deg`` rotates it, measured counter-clockwise in the axes'
coordinate system.
"""

from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np

from ..illumination import phase_angle_from_fraction
from ..models import CrescentGeometry
from ._mpl import require_matplotlib

__all__ = [
    "phase_angle_to_illumination",
    "illumination_to_phase_angle",
    "moon_phase_patch",
    "draw_moon_disk",
]


def phase_angle_to_illumination(phase_angle_deg: float) -> float:
    """Illuminated percentage for a physical phase angle (0 = full, 180 = new)."""
    return (1.0 + math.cos(math.radians(phase_angle_deg))) * 50.0


def illumination_to_phase_angle(illumination_percent: float) -> float:
    """Physical phase angle in degrees for an illuminated percentage."""
    return phase_angle_from_fraction(max(0.0, min(100.0, illumination_percent)) / 100.0)


def moon_phase_patch(
    x: float,
    y: float,
    *,
    radius: float = 1.0,
    illumination_percent: float = 50.0,
    limb_angle_deg: float = 0.0,
    face_color: str = "#e8e8e0",
    dark_color: str = "#101418",
    earthshine: bool = False,
    alpha: float = 1.0,
    resolution: int = 240,
    ax: Optional[Any] = None,
) -> Any:
    """Build a matplotlib patch showing the Moon at a given phase.

    Parameters
    ----------
    x, y
        Centre in axes data coordinates.
    radius
        Disk radius in the same units.
    illumination_percent
        Illuminated fraction, ``0`` = new, ``100`` = full.
    limb_angle_deg
        Rotation of the lit side, degrees counter-clockwise.  With ``0`` the
        bright limb faces right (``+x``).
    face_color, dark_color
        Colours of the lit and unlit hemispheres.
    earthshine
        Draw the unlit hemisphere faintly instead of leaving it black, as it
        appears to the dark-adapted eye.
    ax
        Axes whose data transform the patch is expressed in.  Required — the
        legacy default of "whatever the current axes happen to be" made the
        patch silently attach to the wrong figure inside multi-axes layouts.

    Returns
    -------
    matplotlib.patches.Patch
    """
    plt, _ = require_matplotlib()
    from matplotlib.patches import Circle, PathPatch
    from matplotlib.path import Path
    from matplotlib.transforms import Affine2D

    if ax is None:
        ax = plt.gca()

    fraction = max(0.0, min(1.0, illumination_percent / 100.0))

    def transform():
        return Affine2D().rotate_deg(limb_angle_deg).translate(x, y) + ax.transData

    background_color = "#1a2a40" if earthshine else dark_color
    background_alpha = (0.7 if earthshine else 1.0) * alpha
    backdrop = Circle(
        (0.0, 0.0), radius, fc=background_color, ec="none", alpha=background_alpha, zorder=0
    )
    backdrop.set_transform(transform())
    ax.add_patch(backdrop)

    if fraction <= 1e-4:
        return backdrop

    if fraction >= 1.0 - 1e-4:
        patch = Circle((0.0, 0.0), radius, fc=face_color, ec="none", alpha=alpha, zorder=1)
        patch.set_transform(transform())
        ax.add_patch(patch)
        return patch

    # The terminator projects to a semi-ellipse whose semi-minor axis is
    # ``r * cos(phase angle) = r * (2f - 1)``, measured from the *dark* side.
    # Walking the outer limb from the top down the lit (+x) side and returning
    # along the terminator therefore requires the negated axis: with k = 2f-1,
    # the terminator abscissa is -k*r*cos(theta).  The two degenerate cases
    # confirm the sign — f = 1 gives the full disk, f = 0 gives zero area.
    terminator_axis = -(2.0 * fraction - 1.0)
    samples = max(16, resolution // 2)

    # Right half of the limb, top to bottom, then the terminator back up.
    theta_outer = np.linspace(math.pi / 2.0, -math.pi / 2.0, samples)
    theta_inner = np.linspace(-math.pi / 2.0, math.pi / 2.0, samples)

    outer = np.column_stack(
        [radius * np.cos(theta_outer), radius * np.sin(theta_outer)]
    )
    inner = np.column_stack(
        [
            radius * terminator_axis * np.cos(theta_inner),
            radius * np.sin(theta_inner),
        ]
    )
    vertices = np.vstack([outer, inner, outer[:1]])

    codes = np.full(len(vertices), Path.LINETO, dtype=np.uint8)
    codes[0] = Path.MOVETO
    codes[-1] = Path.CLOSEPOLY

    patch = PathPatch(
        Path(vertices, codes), fc=face_color, ec="none", alpha=alpha, zorder=1
    )
    patch.set_transform(transform())
    ax.add_patch(patch)
    return patch


def draw_moon_disk(
    ax: Any,
    geometry: CrescentGeometry,
    *,
    x: float,
    y: float,
    radius: float,
    use_topocentric: bool = True,
    **kwargs: Any,
) -> Any:
    """Draw the Moon as it appears to the observer, oriented correctly.

    ``geometry.bright_limb_angle_deg`` is the position angle of the Sun
    measured from celestial north through east.  Rendering it directly in an
    equatorial view is exact; in a horizon (azimuth/elevation) view the correct
    chart direction also depends on where the Moon is, which is why the caller
    must supply the plot's own convention through *kwargs* if it differs.
    """
    illumination = (
        geometry.illumination_percent_topo
        if use_topocentric
        else geometry.illumination_percent_geo
    )
    limb_angle = kwargs.pop("limb_angle_deg", None)
    if limb_angle is None:
        # The patch draws the limb towards +x; the position angle is measured
        # from +y (north) towards +x (east), so subtract 90 degrees.
        limb_angle = geometry.bright_limb_angle_deg - 90.0
    patch = moon_phase_patch(
        x,
        y,
        radius=radius,
        illumination_percent=illumination,
        limb_angle_deg=limb_angle,
        ax=ax,
        **kwargs,
    )
    ax.add_patch(patch)
    return patch
