"""Configuration resolution for :mod:`moonlib`.

Resolution order (first hit wins) for every setting:

1. explicit keyword argument
2. environment variable
3. user configuration file (``~/.config/moonlib/config.json``)
4. built-in default

The only setting that has no sensible built-in default is the SPICE kernel
directory; it is discovered by walking up from the package location and from
the current working directory looking for a folder that contains the required
kernel files.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

from .errors import ConfigurationError

__all__ = [
    "ENV_KERNEL_DIR",
    "ENV_KERNEL_MODE",
    "ENV_CONFIG_FILE",
    "ENV_CACHE_DIR",
    "KERNEL_MODES",
    "BASE_KERNELS",
    "MODE_KERNELS",
    "OPTIONAL_KERNELS",
    "KERNEL_PROBE_FILE",
    "CACHE_DIRNAME",
    "CONFIG_DIRNAME",
    "LEGACY_CONFIG_DIRNAME",
    "Settings",
    "load_settings",
    "find_kernel_dir",
    "user_config_path",
    "user_config_paths",
    "package_data_dir",
    "cache_dir",
]

ENV_KERNEL_DIR = "MOONLIB_KERNEL_DIR"
ENV_KERNEL_MODE = "MOONLIB_KERNEL_MODE"
ENV_CONFIG_FILE = "MOONLIB_CONFIG"
ENV_CACHE_DIR = "MOONLIB_CACHE_DIR"

#: Repository-local directory used for derived data (see :func:`cache_dir`).
CACHE_DIRNAME = ".cache"

#: Directory name of the per-user configuration, under ``$XDG_CONFIG_HOME``.
#: The package was called ``moonlib_s`` before 1.5.0, and the configuration
#: file is written *by hand* (it is where a non-default ``kernel_dir`` lives),
#: so a file left under the old name is still read -- see
#: :func:`user_config_paths`.
CONFIG_DIRNAME = "moonlib"
LEGACY_CONFIG_DIRNAME = "moonlib_s"

#: Valid ephemeris modes, cheapest first.
KERNEL_MODES: Tuple[str, ...] = ("recent", "long")

#: Kernels that are always loaded, whatever the mode.
BASE_KERNELS: Tuple[str, ...] = (
    "naif0012.tls",                 # leap seconds
    "de440s.bsp",                   # JPL DE440 short planetary ephemeris
    "pck00010.tpc",                 # planetary constants (radii, orientation)
    "gm_de440.tpc",                 # GM constants consistent with DE440
    "moon_pa_de440_200625.bpc",     # lunar physical libration (frames kernel)
    "moon_de440_220930.tf",         # IAU_MOON / MOON_ME / MOON_PA frame chain
    "earth_latest_high_prec.bpc",   # high-precision Earth orientation (EOP)
)

#: Extra ephemeris kernels loaded per mode, on top of :data:`BASE_KERNELS`.
MODE_KERNELS: Dict[str, Tuple[str, ...]] = {
    "recent": (),
    "long": ("de441_part-1.bsp", "de441_part-2.bsp"),
}

#: Kernels that may be present but are never auto-loaded.
OPTIONAL_KERNELS: Tuple[str, ...] = ("de442s.bsp", "pck00011.tpc")

#: A directory is accepted as a kernel directory only if it holds this file.
KERNEL_PROBE_FILE = "naif0012.tls"


@dataclass(frozen=True)
class Settings:
    """Resolved runtime settings."""

    kernel_dir: Path
    kernel_mode: str = "recent"
    extra_kernels: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kernel_mode not in KERNEL_MODES:
            raise ConfigurationError(
                f"Unknown kernel mode {self.kernel_mode!r}; "
                f"choose one of {', '.join(KERNEL_MODES)}."
            )

    @property
    def kernel_files(self) -> Tuple[str, ...]:
        """Filenames that must exist for this configuration."""
        return BASE_KERNELS + tuple(self.extra_kernels) + MODE_KERNELS[self.kernel_mode]

    def with_overrides(self, **kwargs: Any) -> "Settings":
        """Return a copy with the given fields replaced (``None`` values ignored)."""
        clean = {k: v for k, v in kwargs.items() if v is not None}
        return replace(self, **clean) if clean else self

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kernel_dir": str(self.kernel_dir),
            "kernel_mode": self.kernel_mode,
            "extra_kernels": list(self.extra_kernels),
        }


def user_config_path() -> Path:
    """Path of the optional user configuration file."""
    override = os.environ.get(ENV_CONFIG_FILE)
    if override:
        return Path(override).expanduser()
    return _config_root() / CONFIG_DIRNAME / "config.json"


def user_config_paths() -> Tuple[Path, ...]:
    """Config files to try, in order: the current name, then the old one.

    ``$MOONLIB_CONFIG`` wins outright, exactly as before -- an explicit path is
    not something to second-guess.
    """
    path = user_config_path()
    if os.environ.get(ENV_CONFIG_FILE):
        return (path,)
    legacy = path.parent.parent / LEGACY_CONFIG_DIRNAME / path.name
    return (path, legacy)


def _config_root() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return Path(base).expanduser() if base else Path.home() / ".config"


def _read_user_config() -> Dict[str, Any]:
    for path in user_config_paths():
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:  # pragma: no cover - environment specific
            raise ConfigurationError(f"Cannot read config file {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigurationError(f"Config file {path} must contain a JSON object.")
        return data
    return {}


def _is_kernel_dir(path: Path) -> bool:
    try:
        return path.is_dir() and (path / KERNEL_PROBE_FILE).is_file()
    except OSError:  # pragma: no cover - permission problems
        return False


def _candidate_roots() -> Iterable[Path]:
    """Directories whose ``kernels`` subfolder (or themselves) we probe."""
    here = Path(__file__).resolve()
    # .../<repo>/src/moonlib/config.py -> <repo>/src/moonlib, <repo>/src, <repo>, ...
    for parent in here.parents:
        yield parent
        yield parent / "kernels"
    cwd = Path.cwd().resolve()
    for parent in (cwd, *cwd.parents):
        yield parent
        yield parent / "kernels"


#: Appended to every "no kernels here" message: the files are downloads, and a
#: reader who has just cloned the repository has none of them yet.
KERNEL_HELP = (
    "The SPICE kernels are downloads, not part of the repository: see "
    "kernels/README.md for the file list and the NAIF URLs "
    "(https://naif.jpl.nasa.gov/pub/naif/generic_kernels/)."
)


def find_kernel_dir(explicit: Optional[Path] = None) -> Optional[Path]:
    """Best-effort discovery of a usable SPICE kernel directory.

    Returns ``None`` when nothing usable is found, so that the caller can
    raise a helpful error listing what was searched.
    """
    if explicit is not None:
        candidate = Path(explicit).expanduser()
        if not _is_kernel_dir(candidate):
            raise ConfigurationError(
                f"kernel_dir={str(candidate)!r} does not contain "
                f"{KERNEL_PROBE_FILE}. " + KERNEL_HELP
            )
        return candidate.resolve()

    env_value = os.environ.get(ENV_KERNEL_DIR)
    if env_value:
        candidate = Path(env_value).expanduser()
        if not _is_kernel_dir(candidate):
            raise ConfigurationError(
                f"{ENV_KERNEL_DIR}={env_value!r} does not contain "
                f"{KERNEL_PROBE_FILE}. " + KERNEL_HELP
            )
        return candidate.resolve()

    config_value = _read_user_config().get("kernel_dir")
    if config_value:
        candidate = Path(str(config_value)).expanduser()
        if _is_kernel_dir(candidate):
            return candidate.resolve()

    seen = set()
    for candidate in _candidate_roots():
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if _is_kernel_dir(candidate):
            return candidate.resolve()
    return None


def package_data_dir() -> Path:
    """Directory holding package data shipped with :mod:`moonlib`."""
    return Path(__file__).resolve().parent / "data"


def _project_root() -> Optional[Path]:
    """Best-effort location of the source checkout that holds ``kernels/``.

    Returns ``None`` for a wheel installed into site-packages, where there is
    no checkout to write derived data next to.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "kernels").is_dir() or (parent / "pyproject.toml").is_file():
            return parent
    return None


def cache_dir(explicit: Optional[Path] = None) -> Path:
    """Directory for derived, regenerable data (terrain pyramids, …).

    Resolution order matches the rest of the configuration:

    1. the *explicit* argument,
    2. ``$MOONLIB_CACHE_DIR``,
    3. ``"cache_dir"`` in the user configuration file,
    4. ``<checkout>/.cache/moonlib`` when running from a source checkout,
    5. ``${XDG_CACHE_HOME:-~/.cache}/moonlib`` otherwise.

    The directory is **not** created here; callers create it lazily so that a
    read-only consumer never touches the filesystem.
    """
    if explicit is not None:
        return Path(explicit).expanduser()

    env_value = os.environ.get(ENV_CACHE_DIR)
    if env_value:
        return Path(env_value).expanduser()

    config_value = _read_user_config().get("cache_dir")
    if config_value:
        return Path(str(config_value)).expanduser()

    root = _project_root()
    if root is not None:
        return root / CACHE_DIRNAME / "moonlib"

    base = os.environ.get("XDG_CACHE_HOME")
    fallback = Path(base).expanduser() if base else Path.home() / ".cache"
    return fallback / "moonlib"


def load_settings(
    kernel_dir: Optional[Path] = None,
    kernel_mode: Optional[str] = None,
    extra_kernels: Optional[Sequence[str]] = None,
    *,
    base: Optional[Settings] = None,
) -> Settings:
    """Resolve settings from arguments, environment, config file and defaults.

    Parameters
    ----------
    kernel_dir, kernel_mode, extra_kernels
        Explicit overrides; ``None`` means "fall through to the next source".
    base
        Existing settings used as the lowest-priority fallback.
    """
    config = _read_user_config()

    if kernel_dir is not None:
        resolved_dir: Optional[Path] = find_kernel_dir(kernel_dir)
    elif base is not None:
        resolved_dir = base.kernel_dir
    else:
        resolved_dir = find_kernel_dir()

    if resolved_dir is None:
        raise ConfigurationError(
            "Cannot locate a SPICE kernel directory. Provide one explicitly via "
            f"moonlib.init_kernels(kernel_dir=...), set {ENV_KERNEL_DIR}, or add "
            '{"kernel_dir": "/path/to/kernels"} to '
            f"{user_config_path()}. A directory qualifies when it contains "
            f"{KERNEL_PROBE_FILE}. " + KERNEL_HELP
        )

    if kernel_mode is not None:
        resolved_mode = kernel_mode
    elif ENV_KERNEL_MODE in os.environ:
        resolved_mode = os.environ[ENV_KERNEL_MODE]
    elif base is not None:
        resolved_mode = base.kernel_mode
    else:
        resolved_mode = str(config.get("kernel_mode", "recent"))

    if extra_kernels is not None:
        resolved_extra = tuple(extra_kernels)
    elif base is not None:
        resolved_extra = base.extra_kernels
    else:
        resolved_extra = tuple(config.get("extra_kernels", ()))

    return Settings(
        kernel_dir=resolved_dir,
        kernel_mode=resolved_mode,
        extra_kernels=resolved_extra,
    )
