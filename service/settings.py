"""Configuration, paths and lunar-data discovery for the browser service.

No Qt, no ``timezonefinder`` and no import of the whole dependency stack: this
module is importable with the standard library, and the one heavy probe
(:func:`has_global_dem`) is deliberately kept inside a function.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

APP_NAME = "Tsukikage"
SCHEMA = "tsukikage-service/1"
CONFIG_FILENAME = "service.json"

#: Bundled by the executable.  Kept here as a fallback; the authoritative list
#: is :data:`moonlib.config.BASE_KERNELS`, which the tests compare against.
FALLBACK_KERNELS: Tuple[str, ...] = (
    "naif0012.tls",
    "de440s.bsp",
    "pck00011.tpc",
    "gm_de440.tpc",
    "moon_pa_de440_200625.bpc",
    "moon_de440_220930.tf",
    "earth_latest_high_prec.bpc",
)
LONG_KERNELS: Tuple[str, ...] = (
    "de441_part-1.bsp",
    "de441_part-2.bsp",
)
KERNEL_MODES = ("recent", "long")
KERNEL_MODE_LABELS = {
    "recent": "近期（DE440）",
    "long": "长周期（DE441）",
}


def executable_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return resource_root()


def attachment_dir(override: Optional[os.PathLike | str] = None) -> Path:
    """The ``kernels/`` directory shipped beside the executable.

    The SPICE kernels are no longer embedded in the one-file exe; they are
    required attachments in this directory.  The same directory is also the
    default home for the user's DEM/colour rasters and for the built-in DEM
    downloader, so a portable folder has one obvious data location.
    """
    if override not in (None, ""):
        return Path(str(override)).expanduser().resolve()
    return (executable_dir() / "kernels").resolve()


def resource_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent.parent


def _home() -> Path:
    return Path.home()


def _windows_roots() -> Tuple[Path, Path, Path]:
    roaming = os.environ.get("APPDATA")
    local = os.environ.get("LOCALAPPDATA")
    config = Path(roaming) if roaming else _home() / "AppData" / "Roaming"
    data = Path(local) if local else _home() / "AppData" / "Local"
    return config / APP_NAME, data / APP_NAME / "cache", data / APP_NAME / "logs"


def _macos_roots() -> Tuple[Path, Path, Path]:
    home = _home()
    return (
        home / "Library" / "Application Support" / APP_NAME,
        home / "Library" / "Caches" / APP_NAME,
        home / "Library" / "Logs" / APP_NAME,
    )


def _xdg_roots() -> Tuple[Path, Path, Path]:
    home = _home()
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    cache = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
    state = Path(os.environ.get("XDG_STATE_HOME") or home / ".local" / "state")
    return config / APP_NAME, cache / APP_NAME, state / APP_NAME / "logs"


def platform_roots() -> Tuple[Path, Path, Path]:
    if sys.platform.startswith("win"):
        config, cache, logs = _windows_roots()
    elif sys.platform == "darwin":
        config, cache, logs = _macos_roots()
    else:
        config, cache, logs = _xdg_roots()
    config = Path(os.environ.get("TSUKIKAGE_CONFIG_DIR") or config)
    cache = Path(os.environ.get("TSUKIKAGE_CACHE_DIR") or cache)
    logs = Path(os.environ.get("TSUKIKAGE_LOG_DIR") or logs)
    return config, cache, logs


@dataclass(frozen=True)
class AppPaths:
    config_file: Path
    cache_dir: Path
    log_dir: Path
    settings_dir: Path

    def ensure(self) -> None:
        for directory in (self.config_file.parent, self.cache_dir, self.log_dir, self.settings_dir):
            directory.mkdir(parents=True, exist_ok=True)

    @classmethod
    def discover(cls, config_file: Optional[Path] = None) -> "AppPaths":
        config_root, cache_root, log_root = platform_roots()
        config_file = Path(config_file).expanduser() if config_file else config_root / CONFIG_FILENAME
        return cls(
            config_file=config_file,
            cache_dir=cache_root,
            log_dir=log_root,
            settings_dir=config_file.parent / "settings",
        )


def required_kernels(mode: str = "recent") -> Tuple[str, ...]:
    mode = mode if mode in KERNEL_MODES else "recent"
    try:
        from moonlib.config import BASE_KERNELS, MODE_KERNELS

        return tuple(BASE_KERNELS) + tuple(MODE_KERNELS[mode])
    except Exception:
        return FALLBACK_KERNELS + (LONG_KERNELS if mode == "long" else ())


def missing_kernel_files(kernel_dir: Optional[Path], mode: str = "recent") -> Tuple[List[str], List[str]]:
    """Return ``(missing, empty)`` required kernel names for a mode."""
    mode = mode if mode in KERNEL_MODES else "recent"
    required = required_kernels(mode)
    if kernel_dir is None:
        return list(required), []
    if not kernel_dir.is_dir():
        return list(required), []
    missing: List[str] = []
    empty: List[str] = []
    for name in required:
        candidate = kernel_dir / name
        try:
            if not candidate.is_file():
                missing.append(name)
            elif candidate.stat().st_size <= 0:
                empty.append(name)
        except OSError:
            missing.append(name)
    return missing, empty


def kernel_status(kernel_dir: Optional[Path], mode: str = "recent") -> Tuple[bool, str]:
    mode = mode if mode in KERNEL_MODES else "recent"
    required = required_kernels(mode)
    if kernel_dir is None:
        return False, "未指定内核目录"
    if not kernel_dir.is_dir():
        return False, "内核目录不存在：%s" % kernel_dir
    missing, empty = missing_kernel_files(kernel_dir, mode)
    if missing:
        return False, "内核缺少：" + "、".join(missing)
    if empty:
        return False, "内核文件为空：" + "、".join(empty)
    return True, "%d 个内核文件" % len(required)


def bundled_kernel_dir(override: Optional[os.PathLike | str] = None) -> Optional[Path]:
    """The kernels shipped beside/in the executable, or an explicit override."""
    if override not in (None, ""):
        return Path(str(override)).expanduser().resolve()
    candidates = [
        resource_root() / "kernels",
        executable_dir() / "kernels",
        Path.cwd() / "kernels",
    ]
    for candidate in candidates:
        if (candidate / "naif0012.tls").is_file():
            return candidate.resolve()
    return None


def _large_raster_candidates(directory: Path) -> List[Path]:
    try:
        return sorted(
            entry
            for entry in directory.iterdir()
            if entry.is_file()
            and entry.suffix.lower() in (".tif", ".tiff", ".exr")
            and not entry.name.endswith(".incomplete")
        )
    except OSError:
        return []


def has_global_dem(directory: Optional[os.PathLike | str]) -> bool:
    """True when *directory* itself contains a global 2:1 elevation raster.

    Deliberately scans only that directory.  ``global_dem_sources`` also walks
    the checkout's ``kernels/`` folder, which would make an empty directory look
    valid on a development machine (or, worse, silently pick a raster the user
    did not select).  A single-band 2:1 TIFF is the criterion the renderer uses.
    """
    if directory in (None, ""):
        return False
    path = Path(str(directory)).expanduser()
    if not path.is_dir():
        return False
    try:
        from moonlib.terrain import is_global_dem_raster

        for entry in sorted(path.iterdir()):
            if is_global_dem_raster(entry):
                return True
    except Exception:
        return False
    return False


def data_dir_candidates(
    explicit: Optional[os.PathLike | str] = None,
    configured: Optional[os.PathLike | str] = None,
    paths: Optional[AppPaths] = None,
) -> List[Path]:
    raw: List[Any] = [explicit, configured, os.environ.get("MOONLIB_TERRAIN_DIR")]
    roots = [executable_dir(), Path.cwd(), resource_root()]
    for root in roots:
        raw.extend(
            [
                root,
                root / "data",
                root / "lunar",
                root / "raster",
                root / "dem",
                root / "kernels",
            ]
        )
    seen = set()
    candidates: List[Path] = []
    for item in raw:
        if item in (None, ""):
            continue
        path = Path(str(item)).expanduser()
        if path.suffix.lower() in (".tif", ".tiff", ".exr"):
            path = path.parent
        try:
            path = path.resolve()
        except OSError:
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(path)
    return candidates


def find_data_dir(
    explicit: Optional[os.PathLike | str] = None,
    configured: Optional[os.PathLike | str] = None,
    paths: Optional[AppPaths] = None,
) -> Optional[Path]:
    for candidate in data_dir_candidates(explicit, configured, paths):
        if has_global_dem(candidate):
            return candidate
    return None


def prompt_data_dir() -> Optional[Path]:
    print("未找到月面数据目录。")
    print("需要至少一个全球高程 GeoTIFF（例如 ldem_64.tif、SLDEM2015_512.tif 或")
    print("Lunar_LRO_LOLA_*.tif）。色彩数据可选：lroc_color*.tif 或 lroc_color.exr。")
    while True:
        try:
            answer = input("把数据目录拖到本窗口后回车（直接回车退出）：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not answer:
            return None
        answer = answer.strip().strip('"').strip("'")
        path = Path(answer).expanduser()
        if path.suffix.lower() in (".tif", ".tiff", ".exr"):
            path = path.parent
        try:
            path = path.resolve()
        except OSError:
            pass
        if has_global_dem(path):
            return path
        print("这个目录里没有可用的全球 2:1 高程 TIFF，请换一个。")


def seed_settings(paths: AppPaths) -> None:
    paths.settings_dir.mkdir(parents=True, exist_ok=True)
    source = resource_root() / "web" / "settings" / "default.conf"
    target = paths.settings_dir / "default.conf"
    if source.is_file() and not target.exists():
        try:
            target.write_bytes(source.read_bytes())
        except OSError:
            pass


@dataclass
class ServiceSettings:
    data_dir: str = ""
    cache_dir: str = ""
    host: str = "127.0.0.1"
    port: int = 8765
    map_port: int = 8766
    open_browser: bool = True
    kernel_mode: str = "recent"

    def __post_init__(self) -> None:
        if self.kernel_mode not in KERNEL_MODES:
            self.kernel_mode = "recent"
        try:
            self.port = int(self.port)
        except (TypeError, ValueError):
            self.port = 8765
        if not (0 <= self.port <= 65535):
            self.port = 8765
        try:
            self.map_port = int(self.map_port)
        except (TypeError, ValueError):
            self.map_port = 8766
        if not (0 <= self.map_port <= 65535):
            self.map_port = 8766
        if not self.host:
            self.host = "127.0.0.1"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": SCHEMA,
            "data_dir": self.data_dir,
            "cache_dir": self.cache_dir,
            "host": self.host,
            "port": self.port,
            "map_port": self.map_port,
            "open_browser": bool(self.open_browser),
            "kernel_mode": self.kernel_mode,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ServiceSettings":
        if not isinstance(data, dict):
            return cls()

        def text(value: Any) -> str:
            return value if isinstance(value, str) else ""

        return cls(
            data_dir=text(data.get("data_dir")),
            cache_dir=text(data.get("cache_dir")),
            host=text(data.get("host")) or "127.0.0.1",
            port=data.get("port", 8765),
            map_port=data.get("map_port", 8766),
            open_browser=bool(data.get("open_browser", True)),
            kernel_mode=text(data.get("kernel_mode")) or "recent",
        )


def load_settings(path: Path) -> Tuple[ServiceSettings, Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ServiceSettings(), None
    except OSError as error:
        return ServiceSettings(), "无法读取设置文件：%s" % error
    try:
        payload = json.loads(raw)
    except ValueError as error:
        backup = path.with_name(path.name + ".bad-%d" % int(time.time()))
        try:
            path.replace(backup)
        except OSError:
            backup = None
        note = "设置文件格式错误：%s" % error
        if backup is not None:
            note += "（已备份为 %s）" % backup.name
        return ServiceSettings(), note
    if not isinstance(payload, dict):
        return ServiceSettings(), "设置文件不是 JSON 对象"
    return ServiceSettings.from_dict(payload), None


def save_settings(path: Path, settings: ServiceSettings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(settings.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def apply_environment(
    kernel_dir: Path,
    data_dir: Path,
    cache_dir: Path,
    mode: str = "recent",
) -> Dict[str, str]:
    """Set the ``MOONLIB_*`` variables this process (and its children) use."""
    environment = {
        "MOONLIB_KERNEL_DIR": str(kernel_dir),
        "MOONLIB_KERNEL_MODE": mode,
        "MOONLIB_CACHE_DIR": str(cache_dir),
        "MOONLIB_TERRAIN_DIR": str(data_dir),
    }
    for name in ("MOONLIB_CONFIG", "MOONLIB_DEM", "MOONLIB_ALBEDO"):
        os.environ.pop(name, None)
    os.environ.update(environment)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return environment
