"""Rendering of the simulated lunar disk.

:func:`plot_moon_view` puts a :class:`moonlib.models.MoonRender` on a
matplotlib axes, and :func:`render_to_png` turns one into PNG bytes without
touching the filesystem.  Both are presentation only: every number comes from
:mod:`moonlib.moonview`, and matplotlib is imported lazily so that a headless
install keeps working.

The linear radiance fields are also exposed through :func:`write_radiance_npy`
and :func:`write_radiance_png`, because the sunlit and earthshine-lit
hemispheres differ by about twelve magnitudes and a single exposure can never
show both.  Real earthshine photographs are composites of two exposures, so the
two components are worth keeping separately.
"""

from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path
from typing import Any, Optional, Sequence, Tuple

import numpy as np

from ..errors import MoonlibError
from ..models import MoonRender, ToneMapping
from ._mpl import DARK_BG, close_figure, require_matplotlib

__all__ = [
    "plot_moon_view",
    "render_to_png",
    "write_radiance_png",
    "write_radiance_npy",
    "write_array_png",
    "encode_png",
]


def plot_moon_view(
    render: MoonRender,
    *,
    ax: Any = None,
    figsize: Optional[Tuple[float, float]] = None,
    title: Optional[str] = None,
    show_limb_grid: bool = False,
    facecolor: str = DARK_BG,
) -> Any:
    """Draw a rendered lunar disk.

    Parameters
    ----------
    render
        Result of :func:`moonlib.moonview.render_moon`.
    ax
        Existing axes to draw into; a new figure and axes are created when
        omitted.
    figsize
        Figure size in inches, only used when *ax* is omitted.
    title
        Caption; a summary of the geometry is generated when omitted.
    show_limb_grid
        Overlay the selenographic graticule at 30 degree spacing, which makes
        the libration visible.
    facecolor
        Background colour of the created figure.

    Returns
    -------
    matplotlib.figure.Figure
        Never displayed by this function.
    """
    pyplot, _matplotlib = require_matplotlib()
    if ax is None:
        figure = pyplot.figure(figsize=figsize or (7.0, 7.0), facecolor=facecolor)
        ax = figure.add_axes([0.0, 0.0, 1.0, 1.0])
    else:
        figure = ax.figure

    ax.imshow(np.clip(render.image, 0.0, 1.0), origin="upper", interpolation="nearest")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_facecolor(facecolor)

    if show_limb_grid:
        _draw_graticule(ax, render)

    if title is None:
        view = render.view
        title = (
            f"{view.when_utc:%Y-%m-%d %H:%M UTC}  "
            f"illum {view.illuminated_percent:.1f}%  "
            f"libration {view.libration_lon_deg:+.1f}, {view.libration_lat_deg:+.1f}  "
            f"{render.settings.tone.value}"
        )
    ax.set_title(title, color="#d8dee9", fontsize=9)
    return figure


def _draw_graticule(ax: Any, render: MoonRender) -> None:
    """Overlay 30-degree selenographic meridians and parallels."""
    view = render.view
    _pyplot, _matplotlib = require_matplotlib()
    from ..moonview import _camera_basis  # local import keeps module import light

    forward, right, up = _camera_basis(view)
    radius = 0.5 * render.settings.disk_fraction * min(
        render.settings.width, render.settings.height
    )
    centre = (
        0.5 * (render.settings.width - 1),
        0.5 * (render.settings.height - 1),
    )
    observer = np.asarray(view.observer_direction, dtype=np.float64)

    def project(lon_deg: float, lat_deg: float) -> Optional[Tuple[float, float]]:
        lat = np.radians(lat_deg)
        lon = np.radians(lon_deg)
        normal = np.array(
            [np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)]
        )
        if float(np.dot(normal, observer)) <= 0.0:
            return None
        # Orthographic projection: a surface point projects onto the camera
        # plane at the components of its *own* unit normal, which is what makes
        # the visible hemisphere map exactly onto the unit disk.  Using the
        # local east/north frame instead would let the graticule spill past the
        # limb.
        u = float(np.dot(normal, right))
        v = float(np.dot(normal, up))
        return (centre[0] + radius * u, centre[1] - radius * v)

    for lon in range(-180, 180, 30):
        points = [
            project(float(lon), float(lat))
            for lat in np.arange(-90.0, 90.5, 2.0)
        ]
        _plot_segments(ax, points)
    for lat in range(-60, 61, 30):
        points = [
            project(float(lon), float(lat)) for lon in np.arange(-180.0, 180.5, 2.0)
        ]
        _plot_segments(ax, points)


def _plot_segments(ax: Any, points: Sequence[Optional[Tuple[float, float]]]) -> None:
    run_x: list = []
    run_y: list = []
    for point in list(points) + [None]:
        if point is None:
            if len(run_x) > 1:
                ax.plot(run_x, run_y, color="#7f8c9b", linewidth=0.5, alpha=0.35)
            run_x, run_y = [], []
        else:
            run_x.append(point[0])
            run_y.append(point[1])


def render_to_png(
    render: MoonRender,
    *,
    scale: int = 1,
    label: bool = False,
    figsize: Optional[Tuple[float, float]] = None,
    dpi: int = 110,
) -> bytes:
    """Render to PNG bytes via matplotlib (no filesystem access)."""
    require_matplotlib()
    side = figsize or (render.width / 100.0, render.height / 100.0)
    figure = plot_moon_view(render, figsize=side, title="" if not label else None)
    buffer = io.BytesIO()
    try:
        figure.savefig(
            buffer,
            format="png",
            dpi=dpi * max(1, scale),
            facecolor=figure.get_facecolor(),
        )
    finally:
        close_figure(figure)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# dependency-free PNG output for the raw radiance fields
# ---------------------------------------------------------------------------
def encode_png(array: np.ndarray) -> bytes:
    """Encode a ``(h, w)`` or ``(h, w, 3)`` array of floats in ``[0, 1]``.

    A small, dependency-free PNG writer (zlib plus the five required chunks),
    used so that a headless installation without matplotlib can still save an
    image.
    """
    data = np.asarray(array)
    if data.ndim == 2:
        data = np.repeat(data[:, :, None], 3, axis=2)
    if data.ndim != 3 or data.shape[2] not in (1, 2, 3, 4):
        raise MoonlibError(
            f"encode_png expects (h, w) or (h, w, 3..4); got shape {data.shape}."
        )
    channels = data.shape[2]
    colour_type = {1: 0, 2: 4, 3: 2, 4: 6}[channels]
    encoded = (np.clip(data, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    height, width = encoded.shape[0], encoded.shape[1]

    raw = bytearray()
    for row in range(height):
        raw.append(0)  # filter type 0 (None)
        raw += encoded[row].tobytes()

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, colour_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


def write_radiance_png(
    render: MoonRender,
    path: Any,
    *,
    component: str = "image",
    exposure_ev: float = 0.0,
    tone: Any = ToneMapping.SRGB,
    auto_exposure: Optional[bool] = None,
) -> Path:
    """Write one radiance component to a PNG without needing matplotlib.

    Parameters
    ----------
    component
        ``"image"`` returns the display image the render already produced,
        honouring the tone curve in its settings.  ``"sunlit"`` and
        ``"earthshine"`` re-expose the individual physical components, which is
        what an HDR composite needs: expose each to its own range and add them.
        For full control over the combination use :func:`write_array_png`.
    exposure_ev, tone
        Only applied to the ``"sunlit"`` and ``"earthshine"`` components.
    auto_exposure
        Expose the selected component to its own full range.  Defaults to
        ``True`` for the single components and ``False`` for ``"image"``: the
        sunlit and earthshine-lit hemispheres differ by about twelve
        magnitudes, so written at a common exposure the earthshine is pure
        black.  Pass ``False`` explicitly to compare them on one scale.
    """
    from ..moonview import apply_tone_mapping

    target = Path(path)
    if auto_exposure is None:
        auto_exposure = component != "image"
    if component == "image":
        picture = render.image
    elif component == "sunlit":
        if render.sunlit is None:
            raise MoonlibError("This render did not keep the sunlit component.")
        picture = apply_tone_mapping(
            render.sunlit, tone=tone, exposure_ev=exposure_ev, auto_exposure=auto_exposure
        )
    elif component == "earthshine":
        if render.earthshine is None:
            raise MoonlibError("This render did not keep the earthshine component.")
        picture = apply_tone_mapping(
            render.earthshine,
            tone=tone,
            exposure_ev=exposure_ev,
            auto_exposure=auto_exposure,
        )
    else:
        raise MoonlibError(
            f"component must be 'image', 'sunlit' or 'earthshine'; got {component!r}."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encode_png(picture))
    return target


def write_array_png(array: np.ndarray, path: Any) -> Path:
    """Write any ``(h, w)`` or ``(h, w, 3)`` float array in ``[0, 1]`` to PNG.

    Use this to save a composite the render itself does not produce, for
    instance sunlit and earthshine exposed separately and added together the
    way a real earthshine photograph is made::

        total = render.sunlit / sunlit_peak + render.earthshine / earthshine_peak
        write_array_png(total / total.max(), "composite.png")
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encode_png(array))
    return target


def write_radiance_npy(render: MoonRender, path: Any) -> Path:
    """Write the linear ``I/F`` radiance stack as a ``.npy`` file (HDR).

    Shape is ``(h, w, 3, 3)``: the first plane is the total, then the sunlit
    and earthshine components.  Keeping the linear values is the only way to
    re-expose the earthshine without losing it to 8-bit quantisation.
    """
    target = Path(path)
    planes = [render.image.astype(np.float32)]
    if render.sunlit is not None:
        planes.append(render.sunlit.astype(np.float32))
    if render.earthshine is not None:
        planes.append(render.earthshine.astype(np.float32))
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as handle:
        np.save(handle, np.stack(planes, axis=-2), allow_pickle=False)
    return target
