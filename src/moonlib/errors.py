"""Exception hierarchy for :mod:`moonlib`.

All library-raised exceptions derive from :class:`MoonlibError` so that
callers (including the future HTTP/APP layer) can catch a single base
class and map it onto a transport-level error response.
"""

from __future__ import annotations

__all__ = [
    "MoonlibError",
    "ConfigurationError",
    "KernelError",
    "KernelNotFoundError",
    "KernelNotLoadedError",
    "OutOfRangeError",
    "TimeError",
    "ObserverError",
    "TargetDataError",
    "TerrainDataError",
    "PlottingUnavailableError",
]


class MoonlibError(Exception):
    """Base class for every error raised by :mod:`moonlib`."""


class ConfigurationError(MoonlibError):
    """Invalid or unresolvable library configuration."""


class KernelError(MoonlibError):
    """Base class for SPICE kernel problems."""


class KernelNotFoundError(KernelError):
    """One or more required SPICE kernels are missing from the kernel directory."""

    def __init__(self, missing, kernel_dir):
        self.missing = tuple(missing)
        self.kernel_dir = kernel_dir
        listing = ", ".join(self.missing)
        super().__init__(
            f"Missing SPICE kernel(s): {listing} (looked in {kernel_dir}). "
            f"Set the kernel directory with moonlib.init_kernels(kernel_dir=...) "
            f"or the {self._env_hint()} environment variable."
        )

    @staticmethod
    def _env_hint() -> str:
        from .config import ENV_KERNEL_DIR

        return ENV_KERNEL_DIR


class KernelNotLoadedError(KernelError):
    """A computation was attempted with no SPICE kernel session available."""


class OutOfRangeError(MoonlibError):
    """A requested epoch lies outside the coverage of the loaded ephemeris."""


class TimeError(MoonlibError):
    """Invalid or ambiguous time input."""


class ObserverError(MoonlibError):
    """Invalid observer definition."""


class TargetDataError(MoonlibError):
    """Invalid lunar-surface target data (CSV schema or values)."""


class TerrainDataError(MoonlibError):
    """Lunar surface terrain data is missing, unreadable or inconsistent.

    Raised for the global elevation / colour products (``ldem_*.tif``,
    ``lroc_color*.tif`` / ``.exr``), for their derived pyramid cache, and for
    requests that fall outside the grid the data actually covers.
    """


class PlottingUnavailableError(MoonlibError):
    """matplotlib is required but not installed."""

    def __init__(self):
        super().__init__(
            "matplotlib is required for moonlib.plotting. "
            "Install it with: pip install -e '.[plot]'"
        )
