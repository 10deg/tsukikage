"""matplotlib plumbing shared by every plotting function.

matplotlib is an *optional* dependency: :mod:`moonlib` imports and computes
without it.  Every plotting entry point calls :func:`require_matplotlib` first,
which raises :class:`~moonlib.errors.PlottingUnavailableError` with an
actionable message instead of letting an ``ImportError`` escape.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from ..errors import PlottingUnavailableError

__all__ = [
    "require_matplotlib",
    "new_figure",
    "finish_figure",
    "close_figure",
    "figure_to_png",
    "DARK_BG",
]

#: Background colour used by the night-sky charts.
DARK_BG = "#0d1117"

_FIGURE_SIZE = (12.0, 8.0)
_DPI = 110


def require_matplotlib() -> Tuple[Any, Any]:
    """Return ``(pyplot, matplotlib)`` or raise a helpful error."""
    try:
        import matplotlib
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise PlottingUnavailableError() from exc
    return plt, matplotlib


def new_figure(
    figsize: Optional[Tuple[float, float]] = None,
    *,
    facecolor: str = DARK_BG,
    dpi: int = _DPI,
) -> Tuple[Any, Any]:
    """Create a figure/axes pair without displaying it.

    Plotting functions never call ``plt.show()``: they return the figure so the
    caller decides whether to display, save to PNG, or embed it in a notebook
    or a web response.  The legacy library called ``show()`` internally at
    300 dpi, which made figures impossible to reuse and inflated notebook files
    to megabytes.
    """
    plt, _ = require_matplotlib()
    figure, axes = plt.subplots(figsize=figsize or _FIGURE_SIZE, facecolor=facecolor, dpi=dpi)
    axes.set_facecolor(facecolor)
    return figure, axes


def finish_figure(figure: Any, tight: bool = True) -> Any:
    """Apply final layout and return the figure for chaining."""
    if tight:
        try:
            figure.tight_layout()
        except Exception:  # pragma: no cover - layout is best effort
            pass
    return figure


def close_figure(figure: Any) -> None:
    """Release a figure's resources.

    Provided so that :mod:`moonlib.cli` and :mod:`moonlib.service` never need to
    import pyplot themselves — matplotlib stays confined to this subpackage.
    """
    plt, _ = require_matplotlib()
    plt.close(figure)


def figure_to_png(figure: Any, *, dpi: int = 110) -> bytes:
    """Render a figure to PNG bytes without touching the filesystem."""
    import io

    buffer = io.BytesIO()
    figure.savefig(
        buffer, format="png", dpi=dpi, facecolor=figure.get_facecolor()
    )
    return buffer.getvalue()
