#!/usr/bin/env python3
"""The second local service: an interactive lunar map.

This is deliberately separate from ``web/server.py``.  The simulator answers
"what does the Moon look like from here at this instant"; the map answers
"where is this place, and what is around it".  A browser can keep both open,
and neither one has to know about the other's state.

What it serves
--------------
* an offline, dependency-free map page (``web/map/``);
* a 2:1 equirectangular tile pyramid rendered from the DEM and colour mosaic
  that the simulation already has on disk;
* a feature endpoint over the same IAU catalogue the simulator labels with;
* small search / point-elevation endpoints the page uses for its readouts.

Run it with::

    ./.venv/bin/python web/map_server.py --port 8766

The page is a template on purpose: the layer list, the tile scheme and the
feature payload are stable interfaces, so adding a new overlay or a new data
source does not require rewriting the viewer.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import io
import json
import math
import re
import subprocess
import sys
import threading
import time
import traceback
from collections import OrderedDict

from PIL import Image, ImageOps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

import moonlib  # noqa: E402
from moonlib.annotations import (  # noqa: E402
    CLASS_LABELS,
    FEATURE_CLASSES,
    annotation_catalogue,
    feature_class,
)

MAP_VERSION = "1.0.0"
MAP_STATIC = Path(__file__).resolve().parent / "map"
TILE_PX = 256
MIN_ZOOM = 0
MAX_ZOOM = 8
PHOTO_DIR_DEFAULT = Path(__file__).resolve().parent / "map_photos"
PHOTO_DIR_CONFIG = Path(__file__).resolve().parent / "map_photos_dir.json"
PHOTO_LIMIT_BYTES = 64 * 1024 * 1024


def _initial_photo_dir() -> Path:
    try:
        payload = json.loads(PHOTO_DIR_CONFIG.read_text(encoding="utf-8"))
        chosen = Path(str(payload.get("directory") or "")).expanduser()
        if chosen.is_dir():
            return chosen.resolve()
    except (OSError, ValueError):
        pass
    return PHOTO_DIR_DEFAULT


PHOTO_DIR = _initial_photo_dir()
PHOTO_LOCK = threading.Lock()
PHOTO_SCAN_SEEN: Dict[str, Any] = {}
#: Tile-cache entries; a screenful at one zoom is a few dozen, and a reader
#: panning around should not make the process grow without bound.
TILE_CACHE = 384
#: At and above this zoom the 64 ppd global pyramid is visibly coarser than the
#: tile.  Cut a regional window from the best global raster instead of asking
#: the small pyramid for detail that is not in it.
DETAIL_ZOOM = 5
DETAIL_CACHE = 8
#: The map's own "largest useful" catalogue density, by zoom.  Below this the
#: payload is all labels and no picture; above it the page is a wall of text.
_MIN_DIAMETER_BY_ZOOM = {0: 40.0, 1: 25.0, 2: 15.0, 3: 8.0, 4: 4.0, 5: 2.0, 6: 1.0, 7: 0.5, 8: 0.0}
#: Classes that are meaningful even when the feature is a point, so the size
#: filter above must not drop them.
_POINT_CLASSES = frozenset({"statio", "lunar100", "libration", "impact", "albedo", "imp", "other"})
#: The simulator's DEFAULT_OFF_CLASSES: a first look at the Moon is not a
#: catalogue of domes, landing sites or observing lists.  Keep both pages on
#: the same first paint.
DEFAULT_OFF_CLASSES = frozenset({
    "basin", "dome", "pit", "pyroclastic", "imp", "impact",
    "statio", "lunar100", "libration", "other",
})


def _finite(value: Any, default: float = float("nan")) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _tile_counts(zoom: int) -> Tuple[int, int]:
    """Tiles across and down at *zoom* (z=0 is 2 x 1 tiles)."""
    z = max(MIN_ZOOM, min(MAX_ZOOM, int(zoom)))
    return 2 ** (z + 1), 2 ** z


def _lonlat_grid(zoom: int, x: int, y: int) -> Tuple[np.ndarray, np.ndarray]:
    cols, rows = _tile_counts(zoom)
    width = cols * TILE_PX
    height = rows * TILE_PX
    px = x * TILE_PX + np.arange(TILE_PX, dtype=np.float64) + 0.5
    py = y * TILE_PX + np.arange(TILE_PX, dtype=np.float64) + 0.5
    lon = -180.0 + px / width * 360.0
    lat = 90.0 - py / height * 180.0
    return np.meshgrid(lon, lat)


def _sample_photo(pixels: np.ndarray, y0: np.ndarray, x0: np.ndarray,
                  x1: np.ndarray, y1: np.ndarray,
                  fx: np.ndarray, fy: np.ndarray) -> np.ndarray:
    """Bilinear RGBA sample from a uint8 photo, returned as 0–1 floats.

    The photo cache is kept as bytes rather than float64: a 20-megapixel
    photograph is 80 MB of RGBA8 but 640 MB as float64, and the map usually has
    several open.  The interpolation happens here, per requested pixel, not on
    a full-frame copy.
    """
    top = (pixels[y0, x0].astype(np.float64) * (1.0 - fx)
           + pixels[y0, x1].astype(np.float64) * fx)
    bottom = (pixels[y1, x0].astype(np.float64) * (1.0 - fx)
              + pixels[y1, x1].astype(np.float64) * fx)
    return (top * (1.0 - fy) + bottom * fy) / 255.0


class _FallbackDem:
    """A zero-height DEM so the map remains usable without a downloaded raster."""

    name = "flat (no DEM)"
    n_levels = 1

    def level_for_ground_scale(self, km_per_px):
        return 0

    def km_per_pixel(self, level=0):
        return 0.5

    def sample(self, lon, lat, level=0):
        return np.zeros(np.broadcast(np.asarray(lon), np.asarray(lat)).shape)

    def as_dict(self):
        return {"name": self.name, "n_levels": 1, "fallback": True}


class _FallbackTerrain:
    """Flat, albedo-less terrain used until a global DEM is available."""

    fallback = True

    def __init__(self, reason: str):
        self.reason = reason
        self.dem = _FallbackDem()
        self.albedo = None
        self.reference_radius_km = 1737.4
        self.sources = SimpleNamespace(dem_path=Path("flat"), albedo_path=None)


class LunarMapService:
    """Tiles, features and the small readouts the map page asks for."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._terrain = None
        self._terrain_error: Optional[str] = None
        self._features: Optional[List[Dict[str, Any]]] = None
        self._tiles: "OrderedDict[Tuple[Any, ...], bytes]" = OrderedDict()
        self._detail: "OrderedDict[Tuple[int, int, int], Any]" = OrderedDict()
        self._shadows: "OrderedDict[Tuple[Any, ...], Any]" = OrderedDict()
        self._photo_runtime: "OrderedDict[str, Any]" = OrderedDict()
        self._photo_tiles: "OrderedDict[Tuple[Any, ...], bytes]" = OrderedDict()
        self._photo_project: "OrderedDict[str, Any]" = OrderedDict()
        self._photo_coverage: "OrderedDict[str, Any]" = OrderedDict()
        self._photo_mask: "OrderedDict[str, Any]" = OrderedDict()
        self._detail_source = None
        self._download: Dict[str, Any] = {
            "running": False, "last_line": "", "error": "",
            "returncode": None,
        }
        self._download_lock = threading.Lock()

    # -- data ------------------------------------------------------------
    def terrain(self):
        with self._lock:
            if self._terrain is None:
                try:
                    self._terrain = moonlib.open_terrain(include_albedo=True)
                    self._terrain_error = None
                except Exception as error:
                    # Missing DEMs, an unreadable raster or a failed cache bake
                    # must leave a usable map: flat grey tiles still carry the
                    # coordinates, labels, photos and geometry overlays.
                    self._terrain_error = f"{type(error).__name__}: {error}"
                    self._terrain = _FallbackTerrain(self._terrain_error)
            return self._terrain

    def reload_terrain(self) -> None:
        """Drop the current terrain and every tile derived from it."""
        with self._lock:
            self._terrain = None
            self._terrain_error = None
            self._detail_source = None
            self._detail.clear()
            self._tiles.clear()
            self._shadows.clear()

    def data_status(self) -> Dict[str, Any]:
        terrain = self.terrain()
        with self._download_lock:
            download = dict(self._download)
        return {
            "ok": True,
            "dem": getattr(getattr(terrain, "sources", None).dem_path, "name", "flat"),
            "fallback": bool(getattr(terrain, "fallback", False)),
            "error": self._terrain_error or "",
            "download": download,
        }

    def start_data_download(self) -> Dict[str, Any]:
        """Start the existing LOLA fetch tool in the background."""
        script = ROOT / "tools" / "fetch_lola_global.py"
        if not script.is_file():
            raise moonlib.MoonlibError(f"download tool not found: {script}")
        with self._download_lock:
            already = bool(self._download.get("running"))
            if not already:
                self._download = {
                    "running": True, "last_line": "准备下载…", "error": "",
                    "returncode": None,
                }
        if already:
            return self.data_status()
        thread = threading.Thread(target=self._download_worker, args=(script,),
                                  daemon=True)
        thread.start()
        return self.data_status()

    def _download_worker(self, script: Path) -> None:
        process = None
        try:
            process = subprocess.Popen(
                [sys.executable, str(script)], cwd=str(ROOT),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                text = line.strip()
                if text:
                    with self._download_lock:
                        self._download["last_line"] = text[-300:]
            returncode = process.wait()
            with self._download_lock:
                self._download["returncode"] = returncode
                self._download["running"] = False
                if returncode != 0:
                    self._download["error"] = (
                        f"下载失败（退出码 {returncode}）；请查看终端输出")
            if returncode == 0:
                self.reload_terrain()
        except Exception as error:
            if process is not None and process.poll() is None:
                process.kill()
            with self._download_lock:
                self._download["running"] = False
                self._download["error"] = f"{type(error).__name__}: {error}"

    def _detail_source_path(self):
        """The finest suitable global raster for a high-zoom regional window."""
        if self._detail_source is not None:
            return self._detail_source
        try:
            sources = moonlib.terrain.global_dem_sources()
        except Exception:
            sources = []
        # SLDEM2015 is the 59 m product; the USGS 118 m mosaic is the fallback
        # when only that is readable.  Never return the 64 ppd pyramid here:
        # that is the very source this path exists to bypass.
        chosen = None
        for path, _km in sources:
            name = Path(path).name.lower()
            if "sldem" in name:
                chosen = path
                break
            if "118m" in name and chosen is None:
                chosen = path
        if chosen is None and sources:
            chosen = sources[0][0]
        self._detail_source = chosen
        return chosen

    def _tile_terrain(self, zoom: int, x: int, y: int):
        """Terrain for one tile: the baked pyramid, or a high-res window."""
        if zoom < DETAIL_ZOOM:
            return self.terrain()
        key = (int(zoom), int(x), int(y))
        with self._lock:
            cached = self._detail.get(key)
            if cached is not None:
                self._detail.move_to_end(key)
                return cached
        source = self._detail_source_path()
        if source is None:
            return self.terrain()
        lon, lat = _lonlat_grid(zoom, x, y)
        cols, rows = _tile_counts(zoom)
        d_lon = 360.0 / (cols * TILE_PX)
        d_lat = 180.0 / (rows * TILE_PX)
        # The cast-shadow grid reaches 16 base pixels to the sunward side of
        # the tile; a two-pixel DEM window left it sampling outside its own
        # terrain, which is why high-zoom shadows did not sit on the terrain
        # they were drawn over.
        margin_lon = 20.0 * d_lon
        margin_lat = 20.0 * d_lat
        try:
            terrain = moonlib.terrain.open_region_box(
                float(np.min(lon)) - margin_lon,
                float(np.max(lon)) + margin_lon,
                float(np.min(lat)) - margin_lat,
                float(np.max(lat)) + margin_lat,
                source=source,
                terrain=self.terrain(),
            )
        except Exception:
            # A missing or unreadable high-res raster must not take the map
            # down; the baked pyramid still gives a picture.
            terrain = self.terrain()
        with self._lock:
            self._detail[key] = terrain
            self._detail.move_to_end(key)
            while len(self._detail) > DETAIL_CACHE:
                self._detail.popitem(last=False)
        return terrain

    def features(self) -> List[Dict[str, Any]]:
        with self._lock:
            if self._features is not None:
                return self._features
            rows: List[Dict[str, Any]] = []
            try:
                catalogue = annotation_catalogue()
            except Exception:
                catalogue = ()
            for index, feature in enumerate(catalogue):
                lon = _finite(getattr(feature, "lon_deg", None))
                lat = _finite(getattr(feature, "lat_deg", None))
                if not (math.isfinite(lon) and math.isfinite(lat)):
                    continue
                kind = str(getattr(feature, "kind", "") or "Feature")
                diameter = _finite(getattr(feature, "diameter_km", None), 0.0)
                cls = feature_class(kind, max(0.0, diameter))
                name_en = str(getattr(feature, "name_en", "") or "").strip()
                name_zh = str(getattr(feature, "name_zh", "") or "").strip()
                rows.append({
                    "id": index,
                    "name": name_zh or name_en,
                    "name_en": name_en,
                    "name_zh": name_zh,
                    "lon": lon,
                    "lat": lat,
                    "diameter_km": max(0.0, diameter),
                    "kind": kind,
                    "class": cls,
                    "layer": cls,
                    "note": str(getattr(feature, "note", "") or ""),
                    "lunar100": getattr(feature, "lunar100", None),
                })
            rows.sort(key=lambda row: row["diameter_km"], reverse=True)
            self._features = rows
            return rows

    # -- introspection ---------------------------------------------------
    def health(self) -> Dict[str, Any]:
        terrain = self.terrain()
        dem = terrain.dem
        albedo = terrain.albedo
        sources = []
        try:
            for product, km in moonlib.terrain.global_dem_sources():
                sources.append({"name": product.name, "km_per_px": float(km)})
        except Exception:
            pass
        dem_info = dem.as_dict() if hasattr(dem, "as_dict") else {
            "name": str(getattr(dem, "name", "DEM"))
        }
        albedo_info = (
            albedo.as_dict() if hasattr(albedo, "as_dict") else None
        ) if albedo else None
        try:
            dem_info["name"] = terrain.sources.dem_path.name
        except Exception:
            pass
        if albedo_info is not None:
            try:
                albedo_info["name"] = terrain.sources.albedo_path.name
            except Exception:
                pass
        detail_info = None
        detail_source = self._detail_source_path()
        if detail_source is not None:
            detail_info = {"name": Path(detail_source).name}
            for path, km in sources:
                if Path(path).name == Path(detail_source).name:
                    detail_info["km_per_px"] = float(km)
                    break
        return {
            "ok": True,
            "service": "lunar-map",
            "fallback": bool(getattr(terrain, "fallback", False)),
            "fallback_reason": self._terrain_error or "",
            "version": MAP_VERSION,
            "projection": "equirectangular",
            "tile_size": TILE_PX,
            "min_zoom": MIN_ZOOM,
            "max_zoom": MAX_ZOOM,
            "tile_grid": "z=0 is 2 x 1 tiles; each zoom doubles",
            "dem": dem_info,
            "detail_dem": detail_info,
            "albedo": albedo_info,
            "sources": sources,
            "feature_count": len(self.features()),
        }

    def layers(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {key: 0 for key in FEATURE_CLASSES}
        for row in self.features():
            counts[row["class"]] = counts.get(row["class"], 0) + 1
        layers = [{
            "id": "base",
            "type": "raster",
            "label": "月面底图",
            "description": "高程晕渲 + 反照率",
            "default": True,
        }, {
            "id": "graticule",
            "type": "vector",
            "label": "经纬网",
            "default": True,
        }]
        for key in FEATURE_CLASSES:
            if not counts.get(key):
                continue
            zh, en = CLASS_LABELS.get(key, (key, key))
            layers.append({
                "id": key,
                "type": "features",
                "label": zh,
                "label_en": en,
                "count": int(counts.get(key, 0)),
                "default": key not in DEFAULT_OFF_CLASSES,
            })
        return {
            "layers": layers,
            "feature_classes": list(FEATURE_CLASSES),
            "min_zoom": MIN_ZOOM,
            "max_zoom": MAX_ZOOM,
        }

    # -- query -----------------------------------------------------------
    def feature_query(self, query: Dict[str, Any]) -> Dict[str, Any]:
        rows = self.features()
        classes = {
            part for part in str(query.get("classes", "") or "").split(",") if part
        }
        if not classes:
            classes = set(FEATURE_CLASSES) - DEFAULT_OFF_CLASSES
        west = _finite(query.get("west"), -180.0)
        east = _finite(query.get("east"), 180.0)
        south = _finite(query.get("south"), -90.0)
        north = _finite(query.get("north"), 90.0)
        zoom = int(_finite(query.get("zoom"), 0.0))
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        min_diameter = _MIN_DIAMETER_BY_ZOOM.get(zoom, 0.0)
        limit = int(max(1, min(6000, _finite(query.get("limit"), 2000.0))))

        def in_lon(lon: float) -> bool:
            if west <= east:
                return west <= lon <= east
            return lon >= west or lon <= east

        shown: List[Dict[str, Any]] = []
        total = 0
        for row in rows:
            if row["class"] not in classes:
                continue
            if not (south <= row["lat"] <= north and in_lon(row["lon"])):
                continue
            if (row["diameter_km"] < min_diameter
                    and row["class"] not in _POINT_CLASSES
                    and row["diameter_km"] > 0.0):
                continue
            total += 1
            if len(shown) < limit:
                shown.append(row)
        return {
            "features": shown,
            "total": total,
            "shown": len(shown),
            "classes": sorted(classes),
            "min_diameter_km": min_diameter,
            "zoom": zoom,
        }

    def search(self, query: Dict[str, Any]) -> Dict[str, Any]:
        text = str(query.get("q", "") or "").strip().casefold()
        if not text:
            return {"query": "", "results": []}
        limit = int(max(1, min(50, _finite(query.get("limit"), 20.0))))
        found = []
        for row in self.features():
            haystack = " ".join(
                part for part in (row["name"], row["name_en"], row["name_zh"]) if part
            ).casefold()
            if text not in haystack:
                continue
            found.append(row)
        found.sort(key=lambda row: (not row["name"].casefold().startswith(text),
                                    -row["diameter_km"]))
        return {"query": text, "results": found[:limit]}

    def point(self, query: Dict[str, Any]) -> Dict[str, Any]:
        lon = _finite(query.get("lon"))
        lat = _finite(query.get("lat"))
        if not (math.isfinite(lon) and math.isfinite(lat) and -90.0 <= lat <= 90.0):
            raise moonlib.MoonlibError("lon/lat must be finite; lat in -90..90.")
        terrain = self.terrain()
        level = 0
        try:
            level = int(terrain.dem.level_for_ground_scale(0.25))
        except Exception:
            level = 0
        height = float(np.asarray(terrain.dem.sample(lon, lat, level)).reshape(-1)[0])
        return {
            "lon": float(((lon + 180.0) % 360.0) - 180.0),
            "lat": float(lat),
            "elevation_km": height,
            "radius_km": float(terrain.reference_radius_km + height),
            "level": level,
        }

    # -- lighting --------------------------------------------------------
    def lighting(self, query: Dict[str, Any]) -> Dict[str, Any]:
        """Current Moon phase geometry for the map's date/observer fields."""
        lat = _finite(query.get("lat"), 0.0)
        lon = _finite(query.get("lon"), 0.0)
        when = query.get("when") or None
        observer = moonlib.Observer(lat=lat, lon=lon)
        state = moonlib.moon_state(observer=observer, when=when)
        lib = state.libration
        phase = state.phase
        return {
            "when_utc": state.when_utc.isoformat(),
            "observer": {"lat": float(observer.lat), "lon": float(observer.lon)},
            "subsolar": {
                "lon": float(lib.subsolar_lon_deg),
                "lat": float(lib.subsolar_lat_deg),
            },
            "sub_earth": {
                "lon": float(lib.sub_earth_lon_deg),
                "lat": float(lib.sub_earth_lat_deg),
            },
            "colongitude": float(lib.colongitude_deg),
            "phase": {
                "label": phase.label,
                "name_zh": phase.name_zh,
                "name_en": phase.name_en,
                "illumination_percent": float(phase.illuminated_percent_geo),
                "phase_angle_deg": float(phase.phase_angle_geo_deg),
                "age_days": float(phase.age_days),
            },
            "moon": {
                "azimuth_deg": float(state.azimuth_deg),
                "elevation_deg": float(state.elevation_deg),
                "distance_km": float(state.geo_distance_km),
                "topo_distance_km": float(state.topo_distance_km),
                "angular_diameter_arcsec": float(state.angular_diameter_arcsec),
                "ra_hours": float(state.ra_hours),
                "dec_deg": float(state.dec_deg),
                "elongation_deg": float(state.elongation_deg),
                "sun_elevation_deg": float(state.sun_elevation_deg),
                "velocity_kms": float(state.velocity_kms),
                "light_time_s": float(state.light_time_s),
            },
        }

    def _mean_light_vectors(self, zoom: int, x: int, y: int,
                            light: Tuple[float, float]):
        lon, lat = _lonlat_grid(zoom, x, y)
        sun_lon = math.radians(float(light[0]))
        sun_lat = math.radians(float(light[1]))
        sx = math.cos(sun_lat) * math.cos(sun_lon)
        sy = math.cos(sun_lat) * math.sin(sun_lon)
        sz = math.sin(sun_lat)
        lon_r = np.radians(lon)
        lat_r = np.radians(lat)
        east = -np.sin(lon_r) * sx + np.cos(lon_r) * sy
        north = (
            -np.sin(lat_r) * np.cos(lon_r) * sx
            - np.sin(lat_r) * np.sin(lon_r) * sy
            + np.cos(lat_r) * sz
        )
        up = (
            np.cos(lat_r) * np.cos(lon_r) * sx
            + np.cos(lat_r) * np.sin(lon_r) * sy
            + np.sin(lat_r) * sz
        )
        return float(np.mean(east)), float(np.mean(north)), float(np.mean(up))

    def _shadow_mask_for(self, zoom: int, x: int, y: int,
                         light: Optional[Tuple[float, float]],
                         terrain: Any = None) -> Optional[np.ndarray]:
        """The shadow map for a tile, cached by tile and light.

        Zoom 6 is the reference: from there on the shadow is cropped out of
        that tile's map, so the high-zoom picture keeps the shadow shape the
        reader already judged right and the 1024-cell march is not repeated for
        every child tile.
        """
        if light is None:
            return None
        key = (zoom, x, y, round(float(light[0]), 4), round(float(light[1]), 4))
        if key in self._shadows:
            self._shadows.move_to_end(key)
            return self._shadows[key]
        if terrain is None:
            terrain = self._tile_terrain(zoom, x, y)
        east, north, up = self._mean_light_vectors(zoom, x, y, light)
        norm = math.sqrt(east * east + north * north + up * up)
        mask = None
        if norm > 1e-12:
            mask = self._cast_shadow_mask(
                zoom, x, y, light, east / norm, north / norm, up / norm, terrain,
            )
        self._shadows[key] = mask
        self._shadows.move_to_end(key)
        while len(self._shadows) > 64:
            self._shadows.popitem(last=False)
        return mask

    @staticmethod
    def _crop_shadow(mask: Optional[np.ndarray], ix: int, iy: int,
                     factor: int) -> Optional[np.ndarray]:
        if mask is None or factor <= 1:
            return mask
        size = max(1, TILE_PX // factor)
        crop = mask[iy * size:(iy + 1) * size, ix * size:(ix + 1) * size]
        if crop.size == 0:
            return None
        image = Image.fromarray((np.clip(crop, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8))
        return np.asarray(image.resize((TILE_PX, TILE_PX), Image.BILINEAR),
                          dtype=np.float64) / 255.0

    def _photo_project_for(self, photo_id: str) -> Optional[Dict[str, Any]]:
        """The MoonLabeler project alone, without decoding the photograph."""
        if photo_id in self._photo_project:
            self._photo_project.move_to_end(photo_id)
            return self._photo_project[photo_id]
        try:
            directory = _photo_directory(photo_id)
            project = json.loads((directory / "project.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, moonlib.MoonlibError):
            return None
        self._photo_project[photo_id] = project
        self._photo_project.move_to_end(photo_id)
        while len(self._photo_project) > 64:
            self._photo_project.popitem(last=False)
        return project

    def _photo_runtime_for(self, photo_id: str) -> Optional[Dict[str, Any]]:
        if photo_id in self._photo_runtime:
            self._photo_runtime.move_to_end(photo_id)
            return self._photo_runtime[photo_id]
        project = self._photo_project_for(photo_id)
        if project is None:
            return None
        try:
            directory = _photo_directory(photo_id)
            image = Image.open(directory / "photo.jpg").convert("RGBA")
            pixels = np.asarray(image, dtype=np.uint8)
        except (OSError, ValueError, moonlib.MoonlibError):
            return None
        runtime = {"project": project, "pixels": pixels}
        self._photo_runtime[photo_id] = runtime
        self._photo_runtime.move_to_end(photo_id)
        while len(self._photo_runtime) > 4:
            self._photo_runtime.popitem(last=False)
        return runtime

    def _photo_mask_for(self, photo_id: str,
                        image_size: Tuple[int, int]) -> Optional[np.ndarray]:
        """The hand-drawn mask, resized to the photograph, or None."""
        directory = _photo_directory(photo_id)
        path = directory / "mask.png"
        if not path.is_file():
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        key = (photo_id, stat.st_mtime_ns, stat.st_size,
               int(image_size[0]), int(image_size[1]))
        cached = self._photo_mask.get(key)
        if cached is not None:
            self._photo_mask.move_to_end(key)
            return cached
        try:
            mask = Image.open(path).convert("L")
            if mask.size != (int(image_size[0]), int(image_size[1])):
                mask = mask.resize((int(image_size[0]), int(image_size[1])),
                                   Image.NEAREST)
            values = np.asarray(mask, dtype=np.uint8)
        except (OSError, ValueError):
            return None
        self._photo_mask[key] = values
        self._photo_mask.move_to_end(key)
        while len(self._photo_mask) > 4:
            self._photo_mask.popitem(last=False)
        return values

    def _photo_visible_bounds(self, project: Dict[str, Any]):
        """The lon/lat box the photograph covers, from a coarse forward scan."""
        lon = np.linspace(-180.0, 180.0, 361)
        lat = np.linspace(-90.0, 90.0, 181)
        glon, glat = np.meshgrid(lon, lat)
        projected = self._photo_projection(project, glon, glat, np.zeros_like(glat))
        if projected is None:
            return None
        _px, _py, valid, _w, _h = projected
        if not np.any(valid):
            return None
        lons = glon[valid]
        lats = glat[valid]
        west, east = float(np.min(lons)), float(np.max(lons))
        south, north = float(np.min(lats)), float(np.max(lats))
        if east - west > 180.0:
            west, east = -180.0, 180.0
        return west, east, south, north

    def photo_coverage(self, photo_id: str) -> Dict[str, Any]:
        """The catalogue features a photograph's footprint reaches.

        The page filters its library by what is *in* a photograph, so the
        question is answered once, from the same catalogue the labels use, and
        cached until the photo directory changes.
        """
        if photo_id in self._photo_coverage:
            self._photo_coverage.move_to_end(photo_id)
            return self._photo_coverage[photo_id]
        project = self._photo_project_for(photo_id)
        result: Dict[str, Any] = {"features": [], "bounds": None}
        if project is not None:
            bounds = self._photo_visible_bounds(project)
            if bounds is not None:
                west, east, south, north = bounds
                west -= 0.35
                east += 0.35
                south -= 0.35
                north += 0.35
                result["bounds"] = {"west": west, "east": east,
                                    "south": south, "north": north}
                catalogue = self.features()
                lons = np.array([float(row["lon"]) for row in catalogue])
                lats = np.array([float(row["lat"]) for row in catalogue])
                projected = self._photo_projection(
                    project, lons, lats, np.zeros_like(lats))
                visible = projected[2] if projected is not None else np.zeros(
                    lats.shape, dtype=bool)
                rows = []
                for row, lon, lat, seen in zip(catalogue, lons, lats, visible):
                    if not seen:
                        continue
                    if not (south <= lat <= north):
                        continue
                    if west <= east:
                        inside = west <= lon <= east
                    else:
                        inside = lon >= west or lon <= east
                    if not inside:
                        continue
                    rows.append({
                        "name_en": row["name_en"],
                        "name_zh": row["name_zh"],
                        "class": row["class"],
                        "diameter_km": row["diameter_km"],
                    })
                rows.sort(key=lambda item: item["diameter_km"], reverse=True)
                result["features"] = rows
        self._photo_coverage[photo_id] = result
        self._photo_coverage.move_to_end(photo_id)
        while len(self._photo_coverage) > 64:
            self._photo_coverage.popitem(last=False)
        return result

    def list_photos(self) -> List[Dict[str, Any]]:
        entries = _list_photos()
        for entry in entries:
            coverage = self.photo_coverage(entry["id"])
            entry["covered_features"] = coverage["features"]
            entry["bounds"] = coverage["bounds"]
        return entries

    def set_photo_directory(self, directory: Any) -> Dict[str, Any]:
        """Change the library root and persist it for the next service start."""
        raw = str(directory or "").strip()
        if not raw:
            raise moonlib.MoonlibError("照片目录不能为空")
        chosen = Path(raw).expanduser()
        if not chosen.is_dir():
            raise moonlib.MoonlibError(f"照片目录不存在：{chosen}")
        chosen = chosen.resolve()
        global PHOTO_DIR
        PHOTO_DIR = chosen
        PHOTO_SCAN_SEEN.clear()
        try:
            PHOTO_DIR_CONFIG.write_text(
                json.dumps({"directory": str(chosen)}, ensure_ascii=False),
                encoding="utf-8")
        except OSError as error:
            raise moonlib.MoonlibError(f"无法保存照片目录：{error}") from error
        self._photo_project.clear()
        self._photo_runtime.clear()
        self._photo_tiles.clear()
        self._photo_mask.clear()
        self._photo_coverage.clear()
        return {"ok": True, "directory": str(chosen)}

    def delete_photo_mask(self, photo_id: str) -> bool:
        directory = _photo_directory(photo_id)
        path = directory / "mask.png"
        with PHOTO_LOCK:
            existed = path.is_file()
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            for name in ("overlay.json", "overlay.png"):
                try:
                    (directory / name).unlink()
                except FileNotFoundError:
                    pass
        self._photo_mask.clear()
        self._photo_runtime.pop(photo_id, None)
        self._photo_tiles.clear()
        return existed

    def save_photo_mask(self, photo_id: str, image_bytes: bytes) -> Dict[str, Any]:
        directory = _photo_directory(photo_id)
        project = self._photo_project_for(photo_id)
        if project is None or not directory.is_dir():
            raise moonlib.MoonlibError("照片不存在")
        size = project.get("image_size")
        try:
            width, height = int(size[0]), int(size[1])
        except (TypeError, ValueError, IndexError):
            raise moonlib.MoonlibError("工程缺少照片尺寸")
        try:
            mask = Image.open(io.BytesIO(image_bytes)).convert("L")
        except Exception as error:
            raise moonlib.MoonlibError(f"无法读取蒙版：{error}")
        if mask.size != (width, height):
            mask = mask.resize((width, height), Image.BILINEAR)
        with PHOTO_LOCK:
            mask.save(directory / "mask.png", format="PNG", compress_level=6)
            for name in ("overlay.json", "overlay.png"):
                try:
                    (directory / name).unlink()
                except FileNotFoundError:
                    pass
        self._photo_mask.clear()
        self._photo_runtime.pop(photo_id, None)
        self._photo_tiles.clear()
        return _photo_summary(directory) or {}

    def _photo_projection(self, project: Dict[str, Any], lon: np.ndarray,
                          lat: np.ndarray, height_km: np.ndarray):
        """Vectorised MoonLabeler moon->photo mapping for an equirect grid."""
        geometry = project.get("geometry") or {}
        matrix = project.get("matrix") or [[1, 0, 0], [0, 1, 0]]
        try:
            observer = [float(part) for part in geometry["observer"]]
            right = [float(part) for part in geometry["right"]]
            north = [float(part) for part in geometry["north"]]
            distance = float(geometry["distance_km"])
            (a, b, tx), (c, d, ty) = (
                [float(part) for part in row] for row in matrix)
        except (KeyError, TypeError, ValueError):
            return None
        lon_r = np.radians(lon)
        lat_r = np.radians(lat)
        surface = project.get("projection_surface")
        if surface is None:
            refinement = (project.get("automatic_recognition") or {}).get(
                "refinement") or {}
            surface = "terrain" if refinement.get("relief_aware") is True else "sphere"
        radius = 1.0 + (height_km / 1737.4 if surface == "terrain" else 0.0)
        vx = radius * np.cos(lat_r) * np.cos(lon_r)
        vy = radius * np.cos(lat_r) * np.sin(lon_r)
        vz = radius * np.sin(lat_r)
        q = 1737.4 / distance
        depth = vx * observer[0] + vy * observer[1] + vz * observer[2]
        valid = depth > q * (vx * vx + vy * vy + vz * vz)
        denom = 1.0 - q * depth
        valid &= denom > 1e-9
        safe = np.where(valid, denom, 1.0)
        u = (vx * right[0] + vy * right[1] + vz * right[2]) / safe
        w = -(vx * north[0] + vy * north[1] + vz * north[2]) / safe
        px = a * u + b * w + tx
        py = c * u + d * w + ty
        lens = (project.get("automatic_recognition") or {}).get("lens_distortion")
        if lens:
            try:
                center_x = float(lens["center"][0])
                center_y = float(lens["center"][1])
                scale = float(lens["scale"])
                k = float(lens.get("k", 0.0))
                dx = (px - center_x) / scale
                dy = (py - center_y) / scale
                factor = k * (dx * dx + dy * dy) * scale
                px = px + factor * dx
                py = py + factor * dy
            except (KeyError, TypeError, ValueError):
                return None
        size = project.get("image_size")
        try:
            image_w, image_h = int(size[0]), int(size[1])
        except (TypeError, ValueError, IndexError):
            return None
        valid &= (px >= 0) & (px < image_w) & (py >= 0) & (py < image_h)
        return px, py, valid, image_w, image_h

    def photo_overlay(self, photo_id: str) -> Optional[Dict[str, Any]]:
        """The projection-warped equirectangular cache for one photo."""
        runtime = self._photo_runtime_for(photo_id)
        if runtime is None:
            return None
        directory = _photo_directory(photo_id)
        meta_path = directory / "overlay.json"
        image_path = directory / "overlay.png"
        if meta_path.is_file() and image_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if meta.get("version") == 4:
                    meta["url"] = f"/api/map/photos/{photo_id}/overlay.png"
                    return meta
            except (OSError, ValueError):
                pass
        project = runtime["project"]
        pixels = runtime["pixels"]
        surface = project.get("projection_surface")
        if surface is None:
            refinement = (project.get("automatic_recognition") or {}).get(
                "refinement") or {}
            surface = "terrain" if refinement.get("relief_aware") is True else "sphere"
        terrain = self.terrain() if surface == "terrain" else None
        # A coarse visible-moon scan finds the true footprint.  Corner
        # projection cannot: a whole-disk photo has sky at all four corners.
        sample_lon = np.linspace(-180.0, 180.0, 721)
        sample_lat = np.linspace(-90.0, 90.0, 361)
        slon, slat = np.meshgrid(sample_lon, sample_lat)
        sheight = np.zeros_like(slat)
        if terrain is not None:
            try:
                level = int(terrain.dem.level_for_ground_scale(80.0))
                sheight = np.nan_to_num(np.asarray(
                    terrain.dem.sample(slon, slat, level), dtype=np.float64))
            except Exception:
                sheight = np.zeros_like(slat)
        projected = self._photo_projection(project, slon, slat, sheight)
        if projected is None:
            return None
        _px, _py, valid, _w, _h = projected
        if not np.any(valid):
            return None
        lons = slon[valid]
        lats = slat[valid]
        west, east = float(np.min(lons)), float(np.max(lons))
        south, north = float(np.min(lats)), float(np.max(lats))
        if east - west > 180.0:
            west, east = -180.0, 180.0
        west = max(-180.0, west - 0.6)
        east = min(180.0, east + 0.6)
        south = max(-89.0, south - 0.6)
        north = min(89.0, north + 0.6)
        if east <= west or north <= south:
            return None
        span_x = east - west
        width = int(min(8192.0, max(512.0, span_x * 384.0)))
        height = int(min(8192.0, max(256.0, (north - south) * (width / span_x))))
        if width * height > 8_000_000:
            scale = math.sqrt(8_000_000.0 / (width * height))
            width = max(256, int(width * scale))
            height = max(128, int(height * scale))
        lon = np.linspace(west, east, width)
        lat = np.linspace(north, south, height)
        glon, glat = np.meshgrid(lon, lat)
        gheight = np.zeros_like(glat)
        if terrain is not None:
            try:
                level = int(terrain.dem.level_for_ground_scale(32.0))
                gheight = np.nan_to_num(np.asarray(
                    terrain.dem.sample(glon, glat, level), dtype=np.float64))
            except Exception:
                gheight = np.zeros_like(glat)
        projected = self._photo_projection(project, glon, glat, gheight)
        if projected is None:
            return None
        px, py, valid, image_w, image_h = projected
        x0 = np.clip(np.floor(px).astype(np.int64), 0, image_w - 1)
        y0 = np.clip(np.floor(py).astype(np.int64), 0, image_h - 1)
        x1 = np.clip(x0 + 1, 0, image_w - 1)
        y1 = np.clip(y0 + 1, 0, image_h - 1)
        fx = (px - np.floor(px))[..., None]
        fy = (py - np.floor(py))[..., None]
        sample = _sample_photo(pixels, y0, x0, x1, y1, fx, fy)
        sample[..., 3] = np.where(valid, sample[..., 3], 0.0)
        mask = self._photo_mask_for(photo_id, (image_w, image_h))
        if mask is not None:
            mx = np.clip(np.floor(px).astype(np.int64), 0, image_w - 1)
            my = np.clip(np.floor(py).astype(np.int64), 0, image_h - 1)
            sample[..., 3] *= mask[my, mx].astype(np.float64) / 255.0
        sample = np.where(valid[..., None], sample, 0.0)
        rgba = np.clip(sample * 255.0 + 0.5, 0, 255).astype(np.uint8)
        image = Image.fromarray(rgba)
        with PHOTO_LOCK:
            image.save(image_path, format="PNG", compress_level=4)
            meta = {
                "id": photo_id,
                "version": 4,
                "bounds": {"west": west, "east": east,
                           "north": north, "south": south},
                "width": width,
                "height": height,
                "url": f"/api/map/photos/{photo_id}/overlay.png",
            }
            meta_path.write_text(json.dumps(meta, ensure_ascii=False),
                                 encoding="utf-8")
        return meta

    def photo_overlay_tile(self, zoom: int, x: int, y: int,
                           photo_ids: List[str]) -> bytes:
        """A transparent map tile with registered photos composited in order."""
        ids = tuple(photo_ids)
        key = (zoom, x, y, ids)
        if key in self._photo_tiles:
            self._photo_tiles.move_to_end(key)
            return self._photo_tiles[key]
        width = height = TILE_PX
        out = np.zeros((height, width, 4), dtype=np.float64)
        if ids:
            lon, lat = _lonlat_grid(zoom, x, y)
            lon_r = np.radians(lon)
            lat_r = np.radians(lat)
            base_vectors = (
                np.cos(lat_r) * np.cos(lon_r),
                np.cos(lat_r) * np.sin(lon_r),
                np.sin(lat_r),
            )
            terrain = None
            height_km = np.zeros_like(lat_r)
            if any((self._photo_runtime_for(pid) or {}).get("project", {}).get(
                    "projection_surface") == "terrain" for pid in ids):
                try:
                    terrain = self.terrain()
                    rows = 2 ** zoom
                    world_w = rows * 2 * TILE_PX
                    cell_km = 360.0 / world_w * (math.pi / 180.0) * 1737.4
                    level = int(terrain.dem.level_for_ground_scale(cell_km))
                    height_km = np.asarray(terrain.dem.sample(lon, lat, level),
                                           dtype=np.float64)
                    height_km = np.nan_to_num(height_km, nan=0.0, posinf=0.0,
                                              neginf=0.0)
                except Exception:
                    height_km = np.zeros_like(lat_r)
                    terrain = None
            for photo_id in ids:
                runtime = self._photo_runtime_for(photo_id)
                if runtime is None:
                    continue
                project = runtime["project"]
                geometry = project.get("geometry") or {}
                matrix = project.get("matrix") or [[1, 0, 0], [0, 1, 0]]
                try:
                    observer = [float(part) for part in geometry["observer"]]
                    right = [float(part) for part in geometry["right"]]
                    north = [float(part) for part in geometry["north"]]
                    distance = float(geometry["distance_km"])
                    (a, b, tx), (c, d, ty) = (
                        [float(part) for part in row] for row in matrix)
                except (KeyError, TypeError, ValueError):
                    continue
                surface = project.get("projection_surface")
                if surface is None:
                    refinement = (project.get("automatic_recognition") or {}).get(
                        "refinement") or {}
                    surface = ("terrain" if refinement.get("relief_aware") is True
                               else "sphere")
                radius = 1.0 + (height_km / 1737.4 if surface == "terrain"
                                else 0.0)
                vx = base_vectors[0] * radius
                vy = base_vectors[1] * radius
                vz = base_vectors[2] * radius
                q = 1737.4 / distance
                depth = vx * observer[0] + vy * observer[1] + vz * observer[2]
                valid = depth > q * (vx * vx + vy * vy + vz * vz)
                denom = 1.0 - q * depth
                valid &= denom > 1e-9
                safe = np.where(valid, denom, 1.0)
                u = (vx * right[0] + vy * right[1] + vz * right[2]) / safe
                w = -(vx * north[0] + vy * north[1] + vz * north[2]) / safe
                px = a * u + b * w + tx
                py = c * u + d * w + ty
                lens = (project.get("automatic_recognition") or {}).get(
                    "lens_distortion")
                if lens:
                    try:
                        center_x = float(lens["center"][0])
                        center_y = float(lens["center"][1])
                        scale = float(lens["scale"])
                        k = float(lens.get("k", 0.0))
                        dx = (px - center_x) / scale
                        dy = (py - center_y) / scale
                        factor = k * (dx * dx + dy * dy) * scale
                        px = px + factor * dx
                        py = py + factor * dy
                    except (KeyError, TypeError, ValueError):
                        valid[:] = False
                size = project.get("image_size") or [
                    int(runtime["pixels"].shape[1]),
                    int(runtime["pixels"].shape[0]),
                ]
                try:
                    image_w, image_h = int(size[0]), int(size[1])
                except (TypeError, ValueError):
                    continue
                valid &= (px >= 0) & (px < image_w) & (py >= 0) & (py < image_h)
                x0 = np.floor(px).astype(np.int64)
                y0 = np.floor(py).astype(np.int64)
                x1 = np.clip(x0 + 1, 0, image_w - 1)
                y1 = np.clip(y0 + 1, 0, image_h - 1)
                x0 = np.clip(x0, 0, image_w - 1)
                y0 = np.clip(y0, 0, image_h - 1)
                fx = (px - np.floor(px))[..., None]
                fy = (py - np.floor(py))[..., None]
                pixels = runtime["pixels"]
                if pixels.shape[1] != image_w or pixels.shape[0] != image_h:
                    x0 = np.clip((x0 * pixels.shape[1] // max(image_w, 1)),
                                 0, pixels.shape[1] - 1)
                    y0 = np.clip((y0 * pixels.shape[0] // max(image_h, 1)),
                                 0, pixels.shape[0] - 1)
                    x1 = np.clip(x0 + 1, 0, pixels.shape[1] - 1)
                    y1 = np.clip(y0 + 1, 0, pixels.shape[0] - 1)
                sample = _sample_photo(pixels, y0, x0, x1, y1, fx, fy)
                alpha = sample[..., 3]
                mask = self._photo_mask_for(photo_id, (image_w, image_h))
                if mask is not None:
                    mx = np.clip(np.floor(px).astype(np.int64), 0, image_w - 1)
                    my = np.clip(np.floor(py).astype(np.int64), 0, image_h - 1)
                    alpha = alpha * mask[my, mx].astype(np.float64) / 255.0
                valid &= alpha > 0.0
                if not np.any(valid):
                    continue
                src = sample[..., :3]
                src_alpha = alpha[..., None]
                out[..., :3] = np.where(
                    valid[..., None],
                    src * src_alpha + out[..., :3] * (1.0 - src_alpha),
                    out[..., :3],
                )
                out[..., 3:4] = np.where(
                    valid[..., None],
                    src_alpha + out[..., 3:4] * (1.0 - src_alpha),
                    out[..., 3:4],
                )
        rgba = np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8)
        image = Image.fromarray(rgba)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", compress_level=3)
        png = buffer.getvalue()
        self._photo_tiles[key] = png
        self._photo_tiles.move_to_end(key)
        while len(self._photo_tiles) > 256:
            self._photo_tiles.popitem(last=False)
        return png

    # -- tiles -----------------------------------------------------------
    def tile(self, zoom: int, x: int, y: int,
             light: Optional[Tuple[float, float]] = None,
             earthshine: bool = True, shadow_bias: float = 0.0) -> bytes:
        cols, rows = _tile_counts(zoom)
        if not (0 <= zoom <= MAX_ZOOM and 0 <= x < cols and 0 <= y < rows):
            raise moonlib.MoonlibError("tile outside the pyramid.")
        light_key = (
            (round(float(light[0]), 3), round(float(light[1]), 3))
            if light is not None else None
        )
        bias = max(-0.5, min(0.5, float(shadow_bias)))
        key = (int(zoom), int(x), int(y), light_key, bool(earthshine),
               round(bias, 3))
        with self._lock:
            cached = self._tiles.get(key)
            if cached is not None:
                self._tiles.move_to_end(key)
                return cached
        png = self._render_tile(int(zoom), int(x), int(y), light,
                                earthshine=bool(earthshine), shadow_bias=bias)
        with self._lock:
            self._tiles[key] = png
            self._tiles.move_to_end(key)
            while len(self._tiles) > TILE_CACHE:
                self._tiles.popitem(last=False)
        return png

    def _cast_shadow_mask(
        self,
        zoom: int,
        x: int,
        y: int,
        light: Tuple[float, float],
        sun_east: float,
        sun_north: float,
        sun_up: float,
        terrain: Any = None,
    ) -> Optional[np.ndarray]:
        """A 256x256 cast-shadow mask for one tile, or None when it is cheap to skip.

        This is deliberately a *coarse* shadow map: the DEM is sampled on 64 or
        128 cells, a short uphill march finds the horizon, and the result is
        blurred on the way back to tile resolution.  It is not the renderer's
        per-pixel ray march; it is the cheapest way to let a ridge or a crater
        rim throw a shadow at low Sun without turning map tiles into a render
        job.  Only low Sun benefits, so high Sun returns None before doing any
        work.
        """
        from PIL import Image, ImageFilter

        # Below z4 a tile covers tens of degrees.  The terrain under it is
        # necessarily coarser than the shadow, and a binary cast shadow there
        # is the "black grid" artefact: the honest low-zoom treatment is the
        # smooth slope shading, not a blocky shadow map.  Cast shadows start
        # where the tile can resolve them.
        if zoom < 4:
            return None
        if not (math.radians(0.6) < math.asin(max(-1.0, min(1.0, sun_up))) < math.radians(18.0)):
            return None
        horizontal = math.hypot(sun_east, sun_north)
        if horizontal <= 1e-6:
            return None
        sun_east /= horizontal
        sun_north /= horizontal
        altitude = math.asin(max(-1.0, min(1.0, sun_up)))
        tan_alt = math.tan(altitude)
        if not (tan_alt > 0.0):
            return None

        cols, rows = _tile_counts(zoom)
        total_w = cols * TILE_PX
        total_h = rows * TILE_PX
        size = 256
        halo = 32
        cell = TILE_PX // size
        n = size + 2 * halo

        # Grid origins are aligned to the global base-pixel grid, so adjacent
        # tiles sample the same shadow cells along their shared edge.
        start_x = x * TILE_PX - halo * cell
        start_y = y * TILE_PX - halo * cell
        px = start_x + (np.arange(n, dtype=np.float64) + 0.5) * cell
        py = start_y + (np.arange(n, dtype=np.float64) + 0.5) * cell
        px = np.clip(px, 0.5, total_w - 0.5)
        py = np.clip(py, 0.5, total_h - 0.5)
        lon = -180.0 + px / total_w * 360.0
        lat = 90.0 - py / total_h * 180.0
        lon_grid, lat_grid = np.meshgrid(lon, lat)

        cell_km = math.radians(360.0 / total_w) * 1737.4
        cell_km *= max(math.cos(math.radians(float(np.mean(lat)))), 0.08)
        cell_km *= cell
        if terrain is None:
            terrain = self.terrain()
        try:
            level = int(terrain.dem.level_for_ground_scale(cell_km))
        except Exception:
            level = 0
        level = max(0, min(level, int(getattr(terrain.dem, "n_levels", level + 1)) - 1))
        heights = np.asarray(
            terrain.dem.sample(lon_grid, lat_grid, level), dtype=np.float64
        )
        if heights.shape != lon_grid.shape:  # pragma: no cover - defensive
            heights = np.broadcast_to(heights, lon_grid.shape).astype(np.float64)
        if not np.isfinite(heights).any():
            return None
        heights = np.where(np.isfinite(heights), heights, np.nanmin(heights))

        # Direction toward the Sun in integer shadow-grid coordinates.  The
        # grid's row axis runs south, so north is negative row.
        dx_km = math.radians(360.0 / total_w) * 1737.4 * cell * max(
            math.cos(math.radians(float(np.mean(lat)))), 0.08
        )
        dy_km = math.radians(180.0 / total_h) * 1737.4 * cell
        step_x = sun_east / max(dx_km, 1e-9)
        step_y = -sun_north / max(dy_km, 1e-9)
        step_norm = math.hypot(step_x, step_y)
        if step_norm <= 1e-12:
            return None
        step_x /= step_norm
        step_y /= step_norm
        step_km = math.hypot(step_x * dx_km, step_y * dy_km)

        yy, xx = np.mgrid[0:n, 0:n].astype(np.float64)
        shadow = np.full((n, n), -1e30)
        steps = min(48, halo)

        def sample(field: np.ndarray, sample_x: np.ndarray,
                   sample_y: np.ndarray) -> np.ndarray:
            x0 = np.floor(sample_x).astype(np.int64)
            y0 = np.floor(sample_y).astype(np.int64)
            x1 = np.clip(x0 + 1, 0, field.shape[1] - 1)
            y1 = np.clip(y0 + 1, 0, field.shape[0] - 1)
            x0 = np.clip(x0, 0, field.shape[1] - 1)
            y0 = np.clip(y0, 0, field.shape[0] - 1)
            fx = sample_x - np.floor(sample_x)
            fy = sample_y - np.floor(sample_y)
            return (
                field[y0, x0] * (1 - fx) * (1 - fy)
                + field[y0, x1] * fx * (1 - fy)
                + field[y1, x0] * (1 - fx) * fy
                + field[y1, x1] * fx * fy
            )

        for index in range(1, steps + 1):
            upstream = sample(
                heights, xx + index * step_x, yy + index * step_y
            )
            shadow = np.maximum(
                shadow, upstream - tan_alt * index * step_km
            )
        mask = (shadow > heights + 1e-6).astype(np.float64)
        central = mask[halo:halo + size, halo:halo + size]
        image = Image.fromarray((central * 255.0 + 0.5).astype(np.uint8))
        # Remove isolated one-cell shadow/light pixels before the downsample;
        # they are the salt-and-pepper half of the low-angle artefact.
        image = image.filter(ImageFilter.MedianFilter(3))
        if size != TILE_PX:
            image = image.resize((TILE_PX, TILE_PX), Image.BILINEAR)
        # One hairline of antialiasing, not a grey wash: the threshold below
        # still makes everything but the edge itself pure black.
        image = image.filter(ImageFilter.GaussianBlur(2.0))
        return np.asarray(image, dtype=np.float64) / 255.0

    def _render_tile(self, zoom: int, x: int, y: int,
                     light: Optional[Tuple[float, float]] = None,
                     earthshine: bool = True,
                     shadow_bias: float = 0.0) -> bytes:
        from PIL import Image

        terrain = self._tile_terrain(zoom, x, y)
        lon, lat = _lonlat_grid(zoom, x, y)
        cols, rows = _tile_counts(zoom)
        total_w = cols * TILE_PX
        total_h = rows * TILE_PX
        d_lon = 360.0 / total_w
        d_lat = 180.0 / total_h
        center_lat = float(np.mean(lat))
        km_per_px = (
            math.radians(360.0 / total_w) * terrain.reference_radius_km
            * max(math.cos(math.radians(center_lat)), 0.08)
        )
        try:
            level = int(terrain.dem.level_for_ground_scale(km_per_px))
        except Exception:
            level = 0
        level = max(0, min(level, int(getattr(terrain.dem, "n_levels", level + 1)) - 1))
        albedo_level = level
        if terrain.albedo is not None:
            try:
                albedo_level = int(terrain.albedo.level_for_ground_scale(km_per_px))
            except Exception:
                albedo_level = level

        heights = np.asarray(terrain.dem.sample(lon, lat, level), dtype=np.float64)
        if heights.shape != lon.shape:  # pragma: no cover - defensive
            heights = np.broadcast_to(heights, lon.shape).astype(np.float64)
        # One-pixel central differences, in kilometres of ground.  Near the pole
        # a longitude step covers almost no ground, so the cosine is floored:
        # without the floor the hillshade there becomes pure noise.
        cos_lat = np.maximum(np.cos(np.radians(lat)), 0.02)
        dx_km = math.radians(d_lon) * terrain.reference_radius_km * cos_lat
        dy_km = math.radians(d_lat) * terrain.reference_radius_km
        east = np.asarray(terrain.dem.sample(lon + d_lon, lat, level), dtype=np.float64)
        west = np.asarray(terrain.dem.sample(lon - d_lon, lat, level), dtype=np.float64)
        north = np.asarray(terrain.dem.sample(lon, np.minimum(lat + d_lat, 90.0), level),
                           dtype=np.float64)
        south = np.asarray(terrain.dem.sample(lon, np.maximum(lat - d_lat, -90.0), level),
                           dtype=np.float64)
        slope_east = (east - west) / (2.0 * dx_km)
        slope_north = (north - south) / (2.0 * dy_km)
        norm = np.sqrt(slope_east * slope_east + slope_north * slope_north + 1.0)
        normal_east = -slope_east / norm
        normal_north = -slope_north / norm
        normal_up = 1.0 / norm
        if light is None:
            azimuth = math.radians(315.0)
            altitude = math.radians(45.0)
            light_east = math.sin(azimuth) * math.cos(altitude)
            light_north = math.cos(azimuth) * math.cos(altitude)
            light_up = math.sin(altitude)
        else:
            sun_lon = math.radians(float(light[0]))
            sun_lat = math.radians(float(light[1]))
            sx = math.cos(sun_lat) * math.cos(sun_lon)
            sy = math.cos(sun_lat) * math.sin(sun_lon)
            sz = math.sin(sun_lat)
            lon_r = np.radians(lon)
            lat_r = np.radians(lat)
            light_east = -np.sin(lon_r) * sx + np.cos(lon_r) * sy
            light_north = (
                -np.sin(lat_r) * np.cos(lon_r) * sx
                - np.sin(lat_r) * np.sin(lon_r) * sy
                + np.cos(lat_r) * sz
            )
            light_up = (
                np.cos(lat_r) * np.cos(lon_r) * sx
                + np.cos(lat_r) * np.sin(lon_r) * sy
                + np.sin(lat_r) * sz
            )
        direct = (
            normal_east * light_east
            + normal_north * light_north
            + normal_up * light_up
        )
        # The lit map keeps its highlight headroom below 1.0.  With the old
        # gain the subsolar highlands sat at the top of the curve and read as
        # one white field; the shading is still 1.0-ish there, but the tone
        # curve has somewhere to roll off into.
        # 地照 is the earthshine floor on the night side.  Turning it off lets
        # the night (and the core of a cast shadow) go to black; the brightness
        # and shadow-bias controls can then lift whatever is still wanted.
        ambient = 0.20 if light is None else (0.13 if earthshine else 0.0)
        gain = 0.90 if light is None else 0.88
        shadow = None
        if light is not None:
            if zoom >= 6:
                # 132-260x is the magnification where the high-resolution
                # picture and the cast shadow read as one surface.  Freeze the
                # shadow at that tile level: deeper zooms crop and upscale this
                # map instead of recomputing a different shape at every step.
                factor = 2 ** (zoom - 6)
                parent_x = x // factor
                parent_y = y // factor
                parent = self._shadow_mask_for(6, parent_x, parent_y, light)
                shadow = self._crop_shadow(
                    parent, x - parent_x * factor, y - parent_y * factor, factor,
                )
            else:
                shadow = self._shadow_mask_for(zoom, x, y, light, terrain)
        shade = ambient + gain * np.maximum(direct, 0.0)
        if shadow is not None:
            # The shadow core sits at the same ambient floor as the night side:
            # a hard cast shadow is not blacker than the map's own night, and
            # making it zero produced the abrupt black patches.  Only the edge
            # is blended, narrowly, to avoid a staircase.  The whole effect is
            # also faded in from z4; switching it on at full strength when the
            # reader crosses 30x made the same terrain jump.
            blocked = np.clip((shadow - 0.42) / 0.22, 0.0, 1.0)
            strength = min(1.0, max(0.0, 0.35 + 0.65 * (zoom - 4) / 2.0))
            blocked *= strength
            shade = shade * (1.0 - blocked) + ambient * blocked
        # A signed offset on the light term: positive lifts the shadow floor,
        # negative lets the low end of the colour scale clip to black instead
        # of stopping at a grey haze.
        if shadow_bias:
            shade = shade + float(shadow_bias)
        shade = np.clip(shade, 0.0, 1.05)

        colors = None
        if terrain.albedo is not None:
            try:
                colors = np.asarray(
                    terrain.albedo_rgb(lon, lat, albedo_level), dtype=np.float64
                )
            except Exception:
                colors = None
        if colors is not None and colors.shape[-1] >= 3:
            # Do not pre-clip the albedo lift: raising every bright highland to
            # 1.0 before the light is applied was what made 月面光照 read as a
            # flat white field.  The 0.5 gamma keeps the midtones open and the
            # 0.95 ceiling leaves the highlight shoulder somewhere to work.
            base = np.clip(colors[..., :3], 0.0, 1.0) ** 0.5
            base = np.clip(base * 0.95, 0.0, 1.0)
        else:
            grey = np.clip(0.55 + 0.55 * (heights - float(np.mean(heights))) / 8.0,
                           0.15, 1.0)
            base = np.repeat(grey[..., None], 3, axis=-1)
        rgb = np.clip(base * shade[..., None], 0.0, 1.0)
        # A soft highlight shoulder: the full-Moon shading otherwise multiplies
        # bright highland albedo straight towards 255 and the landforms
        # disappear in a white field.  The curve keeps the midtones and rolls
        # the highlights off instead of clipping them; the extra headroom is
        # what 月面光照 was missing at high Sun.
        rgb = rgb * 0.92 / (1.0 + 0.10 * rgb)
        image = Image.fromarray((rgb * 255.0 + 0.5).astype(np.uint8))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", compress_level=3)
        return buffer.getvalue()


# -- personal photo library ------------------------------------------------

_MOON_COORDINATE_SYSTEM = {
    "body": "Moon",
    "frame": "MOON_ME",
    "latitude_type": "planetocentric",
    "longitude_direction": "east",
    "angle_unit": "degree",
    "reference_radius_km": 1737.4,
    "projection": "finite_distance_perspective",
    "pixel_origin": "top_left",
    "pixel_axes": ["right", "down"],
    "pixel_center_offset": [0.5, 0.5],
}


def _valid_vector(value: Any) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return False
    try:
        numbers = [float(part) for part in value]
    except (TypeError, ValueError):
        return False
    return all(math.isfinite(part) for part in numbers)


def _validate_photo_project(project: Any) -> Dict[str, Any]:
    """Check the subset of a MoonLabeler v2 project a map overlay needs."""
    if not isinstance(project, dict):
        raise moonlib.MoonlibError("工程文件不是 JSON 对象")
    if project.get("format") != "MoonLabeler":
        raise moonlib.MoonlibError("不是 MoonLabeler 工程")
    if type(project.get("version")) is not int or project["version"] != 2:
        raise moonlib.MoonlibError("只支持 MoonLabeler 工程版本 2")
    coordinate = project.get("coordinate_system")
    if coordinate is not None:
        if not isinstance(coordinate, dict):
            raise moonlib.MoonlibError("coordinate_system 无效")
        for key, expected in _MOON_COORDINATE_SYSTEM.items():
            if coordinate.get(key) != expected:
                raise moonlib.MoonlibError(f"不支持的坐标约定：{key}")
    geometry = project.get("geometry")
    if not isinstance(geometry, dict):
        raise moonlib.MoonlibError("照片尚未配准：geometry 为空")
    for key in ("observer", "right", "north", "sun"):
        if not _valid_vector(geometry.get(key)):
            raise moonlib.MoonlibError(f"geometry.{key} 必须是三维单位向量")
    try:
        distance = float(geometry.get("distance_km"))
    except (TypeError, ValueError):
        raise moonlib.MoonlibError("geometry.distance_km 无效")
    if not math.isfinite(distance) or distance <= 1737.4:
        raise moonlib.MoonlibError("geometry.distance_km 必须大于月球半径")
    matrix = project.get("matrix")
    if not isinstance(matrix, list) or len(matrix) != 2:
        raise moonlib.MoonlibError("matrix 必须是 2×3 矩阵")
    try:
        (a, b, tx), (c, d, ty) = ([float(part) for part in row] for row in matrix)
    except (TypeError, ValueError):
        raise moonlib.MoonlibError("matrix 含非数值")
    if not all(math.isfinite(part) for part in (a, b, tx, c, d, ty)):
        raise moonlib.MoonlibError("matrix 含非有限数值")
    if abs(a * d - b * c) < 1e-12:
        raise moonlib.MoonlibError("matrix 不可逆")
    surface = project.get("projection_surface")
    if surface is None:
        refinement = (project.get("automatic_recognition") or {}).get("refinement") or {}
        surface = "terrain" if refinement.get("relief_aware") is True else "sphere"
    if surface not in ("sphere", "terrain"):
        raise moonlib.MoonlibError("projection_surface 必须是 sphere 或 terrain")
    size = project.get("image_size")
    if size is not None:
        if (not isinstance(size, (list, tuple)) or len(size) != 2
                or not all(isinstance(part, int) and part > 0 for part in size)):
            raise moonlib.MoonlibError("image_size 必须是两个正整数")
    lens = (project.get("automatic_recognition") or {}).get("lens_distortion")
    if lens is not None:
        if not isinstance(lens, dict):
            raise moonlib.MoonlibError("lens_distortion 无效")
        center = lens.get("center")
        try:
            center_values = [float(part) for part in center] if isinstance(
                center, (list, tuple)) and len(center) == 2 else None
            scale = float(lens.get("scale"))
            k = float(lens.get("k", 0.0))
        except (TypeError, ValueError):
            raise moonlib.MoonlibError("lens_distortion 参数无效")
        if center_values is None or not all(math.isfinite(part) for part in center_values):
            raise moonlib.MoonlibError("lens_distortion.center 无效")
        if not (math.isfinite(scale) and scale > 0 and math.isfinite(k) and 0 <= k <= 0.6):
            raise moonlib.MoonlibError("lens_distortion 参数超出范围")
    return project


def _photo_directory(photo_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{16}", photo_id):
        raise moonlib.MoonlibError("照片编号无效")
    return PHOTO_DIR / photo_id


def _photo_age_days(when: Any) -> Optional[float]:
    if not when:
        return None
    try:
        return float(moonlib.moon_age(moonlib.to_et(str(when))).age_days)
    except Exception:
        return None


def _photo_summary(directory: Path) -> Optional[Dict[str, Any]]:
    try:
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        project = json.loads((directory / "project.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    summary = dict(meta)
    summary["project"] = project
    summary["image_url"] = f"/api/map/photos/{directory.name}/image"
    thumb = directory / "thumb.jpg"
    summary["thumb_url"] = (f"/api/map/photos/{directory.name}/thumb"
                            if thumb.is_file() else summary["image_url"])
    when = str(summary.get("utc") or project.get("utc") or "")
    summary["date"] = when[:10] if len(when) >= 10 else None
    summary["age_days"] = _photo_age_days(when)
    mask = directory / "mask.png"
    summary["mask_url"] = (f"/api/map/photos/{directory.name}/mask"
                           if mask.is_file() else None)
    return summary


def _inside_photo_directory(path: Path) -> bool:
    try:
        path.resolve().relative_to(PHOTO_DIR.resolve())
        return True
    except (OSError, ValueError):
        return False


def _auto_import_photos() -> None:
    """Import loose ``.moonlabel`` + image pairs dropped into the library.

    The generated entries live in their own subdirectories with a meta.json;
    those are the library's own output and are never scanned as inputs.  A
    project is paired with the image named by image_path, or failing that with
    the image beside it sharing the project's stem.
    """
    if not PHOTO_DIR.is_dir():
        return
    candidates = []
    for candidate in PHOTO_DIR.rglob("*"):
        if candidate.suffix.lower() in (".moonlabel", ".json"):
            candidates.append(candidate)
    for project_path in sorted(set(candidates)):
        key = str(project_path.resolve())
        fingerprint = None
        try:
            if not project_path.is_file():
                continue
            parent = project_path.parent
            if (parent / "meta.json").is_file():
                continue
            stat = project_path.stat()
            fingerprint = (stat.st_mtime_ns, stat.st_size)
            if PHOTO_SCAN_SEEN.get(key) == fingerprint:
                continue
            project = json.loads(project_path.read_text(encoding="utf-8"))
            if not isinstance(project, dict) or project.get("format") != "MoonLabeler":
                PHOTO_SCAN_SEEN[key] = fingerprint
                continue
            raw_path = str(project.get("image_path") or "").replace("\\", "/")
            image_path = (parent / raw_path).resolve() if raw_path else None
            if image_path is None or not image_path.is_file():
                image_path = None
                for suffix in (".jpg", ".jpeg", ".png", ".tif", ".tiff",
                               ".bmp", ".webp"):
                    candidate = project_path.with_suffix(suffix)
                    if candidate.is_file():
                        image_path = candidate.resolve()
                        break
            if image_path is None or not _inside_photo_directory(image_path):
                continue
            _store_photo(project, image_path.read_bytes(), image_path.name)
            PHOTO_SCAN_SEEN[key] = fingerprint
            while len(PHOTO_SCAN_SEEN) > 512:
                PHOTO_SCAN_SEEN.pop(next(iter(PHOTO_SCAN_SEEN)))
        except (OSError, ValueError, moonlib.MoonlibError):
            # A malformed loose file is not a reason for the whole library to
            # fail to load; the manual import reports the same validation.
            if fingerprint is not None:
                PHOTO_SCAN_SEEN[key] = fingerprint
            continue


def _list_photos() -> List[Dict[str, Any]]:
    _auto_import_photos()
    if not PHOTO_DIR.is_dir():
        return []
    entries = []
    for directory in PHOTO_DIR.iterdir():
        if not directory.is_dir():
            continue
        summary = _photo_summary(directory)
        if summary is not None:
            entries.append(summary)
    entries.sort(key=lambda item: item.get("created", ""), reverse=True)
    return entries


def _decode_image_data(value: Any) -> bytes:
    if not isinstance(value, str) or "," not in value:
        raise moonlib.MoonlibError("缺少照片数据")
    head, body = value.split(",", 1)
    if ";base64" not in head:
        raise moonlib.MoonlibError("照片必须是 base64 data URL")
    try:
        bytes_value = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError):
        raise moonlib.MoonlibError("照片 base64 损坏")
    if not bytes_value:
        raise moonlib.MoonlibError("照片为空")
    if len(bytes_value) > PHOTO_LIMIT_BYTES:
        raise moonlib.MoonlibError("照片超过 64 MB")
    return bytes_value


def _store_photo(project: Any, image_bytes: bytes,
                 upload_name: str = "") -> Dict[str, Any]:
    project = _validate_photo_project(project)
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
        image = ImageOps.exif_transpose(image)
    except Exception as error:
        raise moonlib.MoonlibError(f"无法读取照片：{error}")
    if image.width * image.height > 80_000_000:
        raise moonlib.MoonlibError("照片像素过多")
    size = [int(image.width), int(image.height)]
    declared = project.get("image_size")
    if declared is not None and [int(part) for part in declared] != size:
        raise moonlib.MoonlibError(
            f"照片尺寸 {size[0]}×{size[1]} 与工程 image_size "
            f"{declared[0]}×{declared[1]} 不一致"
        )
    normalized = dict(project)
    normalized["image_size"] = size
    normalized["image_path"] = "photo.jpg"
    canonical = json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha256(canonical + b"\0" + image_bytes).hexdigest()[:16]
    directory = PHOTO_DIR / digest
    with PHOTO_LOCK:
        if directory.is_dir():
            summary = _photo_summary(directory)
            if summary is not None:
                return summary
        directory.mkdir(parents=True, exist_ok=True)
        icc = image.info.get("icc_profile")
        rgb = image.convert("RGB")
        save_options = {"format": "JPEG", "quality": 94}
        if icc:
            save_options["icc_profile"] = icc
        rgb.save(directory / "photo.jpg", **save_options)
        thumb = rgb.copy()
        thumb.thumbnail((96, 96))
        thumb.save(directory / "thumb.jpg", format="JPEG", quality=82)
        (directory / "project.json").write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        name = upload_name or Path(str(project.get("image_path") or "")).name
        if not name:
            name = digest
        meta = {
            "id": digest,
            "name": name,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "utc": project.get("utc")
            or (project.get("geometry") or {}).get("utc"),
            "image_size": size,
            "projection_surface": normalized.get("projection_surface")
            or ("terrain"
                if ((project.get("automatic_recognition") or {}).get("refinement")
                    or {}).get("relief_aware") is True else "sphere"),
        }
        (directory / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return _photo_summary(directory)


def _delete_photo(photo_id: str) -> bool:
    directory = _photo_directory(photo_id)
    with PHOTO_LOCK:
        if not directory.is_dir():
            return False
        import shutil
        shutil.rmtree(directory)
    return True


class Handler(BaseHTTPRequestHandler):
    service: LunarMapService

    server_version = "TsukikageMap/1.0.0"

    # -- plumbing --------------------------------------------------------
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        # One line per request would drown the two startup lines in a pan/zoom
        # session; errors still go to stderr through ``_send_error``.
        return

    def _send(self, status: int, body: bytes, content_type: str,
              cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            return self._json(404, {"error": f"missing {path.name}"})
        self._send(200, path.read_bytes(), content_type, cache="no-cache")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _read_json_body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            raise moonlib.MoonlibError("Content-Length 无效")
        if length <= 0:
            raise moonlib.MoonlibError("请求为空")
        if length > 96 * 1024 * 1024:
            raise moonlib.MoonlibError("请求超过 96 MB")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise moonlib.MoonlibError("请求不是 UTF-8 JSON")
        if not isinstance(payload, dict):
            raise moonlib.MoonlibError("请求必须是 JSON 对象")
        return payload

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        try:
            if path in ("/", "/index.html"):
                return self._file(MAP_STATIC / "index.html", "text/html; charset=utf-8")
            if path.startswith("/static/"):
                name = Path(path).name
                kind = {
                    ".js": "application/javascript; charset=utf-8",
                    ".css": "text/css; charset=utf-8",
                    ".html": "text/html; charset=utf-8",
                    ".svg": "image/svg+xml",
                    ".png": "image/png",
                }.get(Path(name).suffix.lower(), "application/octet-stream")
                return self._file(MAP_STATIC / name, kind)
            if path == "/api/map/health":
                return self._json(200, self.service.health())
            if path == "/api/map/layers":
                return self._json(200, self.service.layers())
            if path == "/api/map/data":
                return self._json(200, self.service.data_status())
            if path == "/api/map/features":
                return self._json(200, self.service.feature_query(query))
            if path == "/api/map/search":
                return self._json(200, self.service.search(query))
            if path == "/api/map/point":
                return self._json(200, self.service.point(query))
            if path == "/api/map/lighting":
                return self._json(200, self.service.lighting(query))
            if path == "/api/map/photos":
                return self._json(200, {
                    "photos": self.service.list_photos(),
                    "directory": str(PHOTO_DIR),
                })
            match = re.fullmatch(
                r"/api/map/photos/([0-9a-f]{16})/overlay", path)
            if match:
                meta = self.service.photo_overlay(match.group(1))
                if meta is None:
                    return self._error(404, "no overlay for photo")
                return self._json(200, meta)
            match = re.fullmatch(
                r"/api/map/photos/([0-9a-f]{16})/overlay\.png", path)
            if match:
                overlay_path = _photo_directory(match.group(1)) / "overlay.png"
                if not overlay_path.is_file():
                    return self._error(404, "no overlay for photo")
                return self._send(200, overlay_path.read_bytes(), "image/png",
                                  cache="no-store")
            match = re.fullmatch(
                r"/api/map/photos/([0-9a-f]{16})/mask", path)
            if match:
                mask_path = _photo_directory(match.group(1)) / "mask.png"
                if not mask_path.is_file():
                    return self._error(404, "no mask for photo")
                return self._send(200, mask_path.read_bytes(), "image/png",
                                  cache="no-store")
            match = re.fullmatch(r"/api/map/photos/([0-9a-f]{16})/(image|thumb)", path)
            if match:
                directory = _photo_directory(match.group(1))
                photo = directory / ("thumb.jpg" if match.group(2) == "thumb"
                                     else "photo.jpg")
                if not photo.is_file() and match.group(2) == "thumb":
                    photo = directory / "photo.jpg"
                if not photo.is_file():
                    return self._error(404, "missing photo")
                return self._send(200, photo.read_bytes(), "image/jpeg",
                                  cache="no-store")
            match = re.fullmatch(
                r"/api/map/photo-tiles/(\d+)/(\d+)/(\d+)\.png", path)
            if match:
                zoom, x, y = (int(part) for part in match.groups())
                ids = [part for part in (query.get("ids") or "").split(",")
                       if re.fullmatch(r"[0-9a-f]{16}", part)][:16]
                png = self.service.photo_overlay_tile(zoom, x, y, ids)
                return self._send(200, png, "image/png", cache="no-store")
            match = re.fullmatch(r"/api/map/tile/(\d+)/(\d+)/(\d+)\.png", path)
            if match:
                zoom, x, y = (int(part) for part in match.groups())
                light = None
                raw_light = query.get("light")
                if raw_light:
                    parts = raw_light.split(",")
                    if len(parts) == 2:
                        light_lon = _finite(parts[0])
                        light_lat = _finite(parts[1])
                        if math.isfinite(light_lon) and math.isfinite(light_lat):
                            light = (light_lon, light_lat)
                raw_earth = query.get("earth")
                earthshine = raw_earth not in ("0", "false", "off")
                shadow_bias = _finite(query.get("shadow"))
                if not math.isfinite(shadow_bias):
                    shadow_bias = 0.0
                png = self.service.tile(
                    zoom, x, y, light, earthshine=earthshine,
                    shadow_bias=shadow_bias)
                # Tiles carry baked shadows; leaving them in the browser
                # cache means a restart can still be showing yesterday's
                # algorithm, which is exactly the local mismatch reported.
                return self._send(200, png, "image/png", cache="no-store")
            return self._error(404, f"no route {path}")
        except moonlib.MoonlibError as error:
            return self._error(400, str(error))
        except Exception as error:  # pragma: no cover - surfaced to the page
            traceback.print_exc()
            return self._error(500, f"{type(error).__name__}: {error}")


    # -- personal photo library -----------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/map/data/download":
                return self._json(200, self.service.start_data_download())
            match = re.fullmatch(
                r"/api/map/photos/([0-9a-f]{16})/mask", path)
            if match:
                payload = self._read_json_body()
                image_bytes = _decode_image_data(payload.get("image"))
                summary = self.service.save_photo_mask(match.group(1), image_bytes)
                return self._json(200, summary)
            if path == "/api/map/photos/directory":
                payload = self._read_json_body()
                result = self.service.set_photo_directory(payload.get("directory"))
                return self._json(200, result)
            if path != "/api/map/photos":
                return self._error(404, f"no route {path}")
            payload = self._read_json_body()
            project = payload.get("project")
            image_bytes = _decode_image_data(payload.get("image"))
            name = str(payload.get("name") or "").strip()[:160]
            summary = _store_photo(project, image_bytes, name)
            return self._json(201, summary)
        except moonlib.MoonlibError as error:
            return self._error(400, str(error))
        except Exception as error:  # pragma: no cover - surfaced to the page
            traceback.print_exc()
            return self._error(500, f"{type(error).__name__}: {error}")

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            match = re.fullmatch(r"/api/map/photos/([0-9a-f]{16})/mask", path)
            if match:
                deleted = self.service.delete_photo_mask(match.group(1))
                return self._json(200, {"deleted": bool(deleted)})
            match = re.fullmatch(r"/api/map/photos/([0-9a-f]{16})", path)
            if not match:
                return self._error(404, f"no route {path}")
            if not _delete_photo(match.group(1)):
                return self._error(404, "photo not found")
            return self._json(200, {"deleted": True})
        except moonlib.MoonlibError as error:
            return self._error(400, str(error))
        except Exception as error:  # pragma: no cover - surfaced to the page
            traceback.print_exc()
            return self._error(500, f"{type(error).__name__}: {error}")


def build_server(host: str, port: int) -> ThreadingHTTPServer:
    service = LunarMapService()
    print("opening terrain ...", flush=True)
    started = time.time()
    terrain = service.terrain()
    print(
        "terrain ready in %.1f s (%s, %s)"
        % (
            time.time() - started,
            terrain.sources.dem_path.name,
            "colour" if terrain.albedo is not None else "no colour",
        ),
        flush=True,
    )
    started = time.time()
    count = len(service.features())
    print("features ready in %.1f s (%d names)" % (time.time() - started, count),
          flush=True)

    class Bound(Handler):
        pass

    Bound.service = service
    return ThreadingHTTPServer((host, port), Bound)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)

    httpd = build_server(args.host, args.port)
    host, port = httpd.server_address[:2]
    print(f"\n  lunar map  ->  http://{host}:{port}/", flush=True)
    print("  Ctrl-C to stop\n", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
