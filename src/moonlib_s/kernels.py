"""SPICE kernel lifecycle management.

This module owns every interaction with the SPICE kernel pool and is the
single source of truth for the physical constants that are read out of the
kernels.  Nothing else in :mod:`moonlib_s` calls :func:`spiceypy.furnsh` or
:func:`spiceypy.kclear`.

Compared with the legacy ``moonlib_s.init_spice``:

* initialisation is **automatic** — the first computation triggers it, so the
  "constants are ``None`` before ``init_spice()``" footgun is gone;
* re-initialisation with a *different* configuration actually reloads the
  pool instead of being silently ignored;
* the loaded configuration and its ephemeris coverage are queryable.

Thread safety
-------------
CSPICE is a single-threaded library: the kernel pool, the DAF file table, the
frame/body name hashes and the error state all live in process-global
variables that every call may *write* (looking up a frame by name inserts it
into a hash, searching an SPK rewinds that file's "current array").  Two
threads inside it at once therefore corrupt each other's bookkeeping, and the
damage outlives the request that caused it.  That is what a threaded server
produces: ``spice.spkpos('SUN', et, 'MOON_ME', ...)`` from several request
threads at once gave ``SPICE(BADVARIABLESIZE)`` — *the kernel variable
FRAME_31008_CLASS was expected to have size not exceeding 1 but in fact has
size 2* — followed by ``NOCURRENTARRAY`` and, on a longer run, a hard abort
from a corrupted pool.

Every call to SPICE in :mod:`moonlib_s` therefore goes through
:data:`spice` below, which serialises calls on one re-entrant lock.  The lock
is also held across the whole ``kclear`` + ``furnsh`` sequence so a reader in
another thread can never observe a half-loaded pool.
"""

from __future__ import annotations

import functools
import math
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple

import numpy as np
import spiceypy as _spiceypy

from .config import (
    KERNEL_MODES,
    MODE_KERNELS,
    Settings,
    load_settings,
)
from .errors import ConfigurationError, KernelNotFoundError, OutOfRangeError

__all__ = [
    "spice",
    "SpiceConstants",
    "KernelSession",
    "init_kernels",
    "kernel_session",
    "shutdown_kernels",
    "is_initialized",
    "loaded_kernels",
    "describe",
]

_BASE_FRAME = "J2000"
_MOON_BODY = "MOON"
_EARTH_BODY = "EARTH"

#: The one lock every SPICE call is serialised on.  Re-entrant, because loading
#: the pool is itself a sequence of calls that must be atomic with respect to
#: readers in other threads.
_SPICE_LOCK = threading.RLock()


class _LockedSpice:
    """``spiceypy`` whose callables all run inside :data:`_SPICE_LOCK`.

    Non-callable attributes (module constants, ``__version__``) are passed
    through untouched, and each wrapped callable is built once and cached, so
    an attribute lookup per call costs a dict hit.
    """

    __slots__ = ("_module", "_calls")

    def __init__(self, module) -> None:
        self._module = module
        self._calls: dict = {}

    def __getattr__(self, name: str):
        attribute = getattr(self._module, name)
        if not callable(attribute):
            return attribute
        try:
            return self._calls[name]
        except KeyError:
            function = attribute

            @functools.wraps(function)
            def call(*args, **kwargs):
                with _SPICE_LOCK:
                    return function(*args, **kwargs)

            self._calls[name] = call
            return call

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        version = getattr(self._module, "__version__", "?")
        return f"<locked spiceypy {version}>"


#: The only handle to SPICE in :mod:`moonlib_s`; other modules import it from
#: here (``from .kernels import spice``).  Importing :mod:`spiceypy` directly
#: would step outside the lock and bring the races described above back.
spice = _LockedSpice(_spiceypy)


@dataclass(frozen=True)
class SpiceConstants:
    """Physical constants read from the loaded kernel pool.

    Instances are immutable, so a value handed to a caller can never be
    invalidated by a later kernel reload.
    """

    earth_radius_km: float
    earth_polar_radius_km: float
    earth_flattening: float
    moon_radius_km: float
    earth_gm_km3_s2: float
    moon_gm_km3_s2: float

    #: Degrees per radian.
    deg_per_rad: float = 180.0 / math.pi
    #: Radians per degree.
    rad_per_deg: float = math.pi / 180.0
    #: Hours per radian (for right ascension).
    hours_per_rad: float = 12.0 / math.pi
    #: Days per Julian century.
    days_per_century: float = 36525.0

    @property
    def moon_earth_mu(self) -> float:
        """Gravitational parameter of the Earth+Moon system (km³/s²)."""
        return self.earth_gm_km3_s2 + self.moon_gm_km3_s2


class KernelSession:
    """An immutable description of the currently loaded kernel pool."""

    __slots__ = ("settings", "loaded", "coverage", "constants")

    def __init__(
        self,
        settings: Settings,
        loaded: Tuple[str, ...],
        coverage: Optional[Tuple[float, float]],
        constants: SpiceConstants,
    ) -> None:
        self.settings = settings
        self.loaded = loaded
        self.coverage = coverage
        self.constants = constants

    # -- convenience proxies ------------------------------------------------
    @property
    def kernel_dir(self) -> Path:
        return self.settings.kernel_dir

    @property
    def mode(self) -> str:
        return self.settings.kernel_mode

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"KernelSession(mode={self.mode!r}, kernel_dir={str(self.kernel_dir)!r}, "
            f"kernels={len(self.loaded)})"
        )

    def coverage_utc(self) -> Optional[Tuple[datetime, datetime]]:
        """Ephemeris coverage as UTC datetimes.

        Returns ``None`` both when no SPK is loaded and when an endpoint falls
        outside the ISO calendar year range — which is the case for DE441,
        whose start (about 13 200 BC) cannot be written as an ISO date.  Use
        :meth:`coverage_text` when a printable form is needed for any epoch.
        """
        if self.coverage is None:
            return None
        start = _et_to_utc_safe(self.coverage[0])
        end = _et_to_utc_safe(self.coverage[1])
        if start is None or end is None:
            return None
        return (start, end)

    def coverage_text(self) -> Optional[Tuple[str, str]]:
        """Coverage endpoints as SPICE calendar strings.

        ``spice.etcal`` renders epochs outside the ISO year range (for example
        ``13201 B.C. JAN 01``), so this is the honest display form for the
        long-ephemeris mode.
        """
        if self.coverage is None:
            return None
        return (
            _et_to_calendar(self.coverage[0]),
            _et_to_calendar(self.coverage[1]),
        )

    def require_epoch(self, et: float, *, what: str = "epoch") -> None:
        """Raise :class:`OutOfRangeError` when *et* is outside coverage."""
        if self.coverage is None:
            return
        start, end = self.coverage
        if et < start or et > end:
            text = self.coverage_text() or ("?", "?")
            raise OutOfRangeError(
                f"Requested {what} is outside the loaded ephemeris coverage "
                f"({text[0]} .. {text[1]}). "
                f"Use mode='long' (DE441) for multi-millennium work."
            )


def _et_to_utc_safe(et: float) -> Optional[datetime]:
    """``et2utc`` returning ``None`` for epochs outside the ISO year range.

    ``et2utc`` raises ``SpiceYEAROUTOFRANGE`` for years before 1 AD, which is
    exactly what DE441's start is.  Returning ``None`` keeps that fact visible
    instead of silently substituting a placeholder date.
    """
    try:
        text = spice.et2utc(et, "ISOC", 3)
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    except Exception:
        # Two distinct failure modes, both meaning "not an ISO date":
        # et2utc raises for years before 1 AD, and datetime.fromisoformat
        # cannot parse the 5-digit year DE441 ends with (17191).
        return None


def _et_to_calendar(et: float) -> str:
    """SPICE calendar string, valid for any epoch the kernels cover."""
    try:
        return spice.etcal(et, 60).strip()
    except Exception:  # pragma: no cover - defensive
        return f"ET {et:.3f}"


_STATE_LOCK = threading.RLock()
_SESSION: Optional[KernelSession] = None
_LOADED_FILES: Tuple[str, ...] = ()


def _read_constants() -> SpiceConstants:
    earth_radii = np.asarray(spice.bodvrd(_EARTH_BODY, "RADII", 3)[1], dtype=float)
    moon_radii = np.asarray(spice.bodvrd(_MOON_BODY, "RADII", 3)[1], dtype=float)
    earth_gm = float(spice.bodvrd(_EARTH_BODY, "GM", 1)[1][0])
    moon_gm = float(spice.bodvrd(_MOON_BODY, "GM", 1)[1][0])
    return SpiceConstants(
        earth_radius_km=float(earth_radii[0]),
        earth_polar_radius_km=float(earth_radii[2]),
        earth_flattening=float((earth_radii[0] - earth_radii[2]) / earth_radii[0]),
        moon_radius_km=float(moon_radii.mean()),
        earth_gm_km3_s2=earth_gm,
        moon_gm_km3_s2=moon_gm,
    )


def _bsp_coverage(paths: Iterable[Path]) -> Optional[Tuple[float, float]]:
    """Union of the coverage intervals of the given SPK files."""
    windows = []
    for path in paths:
        if path.suffix.lower() != ".bsp":
            continue
        try:
            ids = list(spice.spkobj(str(path)))
        except Exception:  # pragma: no cover - unreadable/unsupported SPK
            continue
        for body in ids:
            try:
                windows.append(spice.spkcov(str(path), body))
            except Exception:  # pragma: no cover
                continue
    if not windows:
        return None
    combined = spice.wnunid(windows[0], windows[0])
    for window in windows[1:]:
        combined = spice.wnunid(combined, window)
    if spice.wncard(combined) == 0:  # pragma: no cover
        return None
    lows = []
    highs = []
    for index in range(spice.wncard(combined)):
        low, high = spice.wnfetd(combined, index)
        lows.append(float(low))
        highs.append(float(high))
    return (min(lows), max(highs))


def _validate_kernels(settings: Settings) -> Tuple[Path, ...]:
    missing = [
        name
        for name in settings.kernel_files
        if not (settings.kernel_dir / name).is_file()
    ]
    if missing:
        raise KernelNotFoundError(missing, settings.kernel_dir)
    return tuple(settings.kernel_dir / name for name in settings.kernel_files)


def _same_configuration(settings: Settings) -> bool:
    return _SESSION is not None and _SESSION.settings == settings


def init_kernels(
    kernel_dir: Optional[Path] = None,
    mode: Optional[str] = None,
    extra_kernels: Optional[Sequence[str]] = None,
    *,
    reload: bool = False,
    verbose: bool = False,
) -> KernelSession:
    """Load (or reload) the SPICE kernel pool and return the active session.

    Parameters
    ----------
    kernel_dir
        Directory holding the kernel files.  ``None`` resolves through
        :func:`moonlib_s.config.load_settings`.
    mode
        ``'recent'`` (DE440 only, defaults, ~1850–2150) or ``'long'``
        (DE441 on top, roughly 13 200 BC – 17 191 AD).
    extra_kernels
        Additional kernel filenames to load after the standard set.
    reload
        Force a reload even when the requested configuration is already active.
    verbose
        Print each loaded kernel (useful for CLI diagnostics).

    Notes
    -----
    Calling this repeatedly with the same arguments is a cheap no-op.  Calling
    it with *different* arguments reloads the pool — the legacy implementation
    silently ignored such calls.
    """
    global _SESSION, _LOADED_FILES

    with _STATE_LOCK:
        base = _SESSION.settings if _SESSION is not None else None
        settings = load_settings(
            kernel_dir=kernel_dir,
            kernel_mode=mode,
            extra_kernels=extra_kernels,
            base=base,
        )
        if not reload and _same_configuration(settings):
            return _SESSION  # type: ignore[return-value]

        paths = _validate_kernels(settings)

        # Held across the whole reload, not just across each call: a reader in
        # another thread has to wait for a *complete* pool rather than run
        # against one that has been cleared but not yet refilled.
        with _SPICE_LOCK:
            spice.kclear()
            for path in paths:
                spice.furnsh(str(path))
                if verbose:
                    # Diagnostics go to stderr so that `moon <cmd> --json` keeps a
                    # clean, machine-parseable stdout.
                    print(f"  loaded {path.name}", file=sys.stderr)

            constants = _read_constants()
            coverage = _bsp_coverage(paths)
        _LOADED_FILES = tuple(path.name for path in paths)
        _SESSION = KernelSession(
            settings=settings,
            loaded=_LOADED_FILES,
            coverage=coverage,
            constants=constants,
        )
        return _SESSION


def kernel_session() -> KernelSession:
    """Return the active session, initialising with defaults if necessary."""
    session = _SESSION
    if session is not None:
        return session
    return init_kernels()


def is_initialized() -> bool:
    """True when a kernel pool is currently loaded."""
    return _SESSION is not None


def loaded_kernels() -> Tuple[str, ...]:
    """Filenames of the kernels currently loaded (empty when uninitialised)."""
    return _LOADED_FILES


def shutdown_kernels() -> None:
    """Unload every kernel.  Mainly useful in tests."""
    global _SESSION, _LOADED_FILES
    with _STATE_LOCK:
        spice.kclear()
        _SESSION = None
        _LOADED_FILES = ()


def describe() -> dict:
    """Human/machine readable description of the active session."""
    session = kernel_session()
    span = session.coverage_utc()
    text = session.coverage_text()
    return {
        "kernel_dir": str(session.kernel_dir),
        "mode": session.mode,
        "loaded": list(session.loaded),
        "available_modes": list(KERNEL_MODES),
        "mode_kernels": {k: list(v) for k, v in MODE_KERNELS.items()},
        # ISO form when the calendar allows it, otherwise null; the SPICE
        # calendar form below always works, including BC epochs.
        "coverage_utc": (
            None if span is None else [span[0].isoformat(), span[1].isoformat()]
        ),
        "coverage_text": None if text is None else [text[0], text[1]],
        "constants": {
            "moon_radius_km": session.constants.moon_radius_km,
            "earth_radius_km": session.constants.earth_radius_km,
            "earth_flattening": session.constants.earth_flattening,
        },
    }


def validate_mode(mode: str) -> str:
    """Validate a kernel mode string and return it unchanged."""
    if mode not in KERNEL_MODES:
        raise ConfigurationError(
            f"Unknown kernel mode {mode!r}; choose one of {', '.join(KERNEL_MODES)}."
        )
    return mode
