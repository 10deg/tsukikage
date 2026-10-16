"""Plotting helpers (optional; requires matplotlib).

Every function returns a :class:`matplotlib.figure.Figure` and never calls
``plt.show()``, so the same code serves notebooks, scripts, the CLI
(``--save``) and a future web backend (render to PNG bytes).

Importing this subpackage does **not** import matplotlib; the dependency is
resolved on the first call, and a missing matplotlib produces
:class:`~moonlib.errors.PlottingUnavailableError` with installation advice.
"""

from __future__ import annotations

from ._mpl import (
    DARK_BG,
    close_figure,
    figure_to_png,
    finish_figure,
    new_figure,
    require_matplotlib,
)
from .moonview import (
    encode_png,
    plot_moon_view,
    render_to_png,
    write_array_png,
    write_radiance_npy,
    write_radiance_png,
)
from .phase import (
    draw_moon_disk,
    illumination_to_phase_angle,
    moon_phase_patch,
    phase_angle_to_illumination,
)
from .sky import TWILIGHT_BANDS, find_local_sunset, plot_sky, sky_background

__all__ = [
    "require_matplotlib",
    "new_figure",
    "finish_figure",
    "close_figure",
    "figure_to_png",
    "DARK_BG",
    "TWILIGHT_BANDS",
    "sky_background",
    "plot_sky",
    "find_local_sunset",
    "moon_phase_patch",
    "draw_moon_disk",
    "phase_angle_to_illumination",
    "illumination_to_phase_angle",
    "plot_moon_view",
    "render_to_png",
    "encode_png",
    "write_radiance_png",
    "write_radiance_npy",
    "write_array_png",
]
