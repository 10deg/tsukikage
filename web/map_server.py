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
import io
import json
import math
import re
import sys
import threading
import time
import traceback
from collections import OrderedDict

from PIL import Image
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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

MAP_STATIC = Path(__file__).resolve().parent / "map"
TILE_PX = 256
MIN_ZOOM = 0
MAX_ZOOM = 8
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
_MIN_DIAMETER_BY_ZOOM = {0: 220.0, 1: 120.0, 2: 70.0, 3: 35.0, 4: 15.0, 5: 6.0, 6: 2.5, 7: 1.0, 8: 0.0}
#: Classes that are meaningful even when the feature is a point, so the size
#: filter above must not drop them.
_POINT_CLASSES = frozenset({"statio", "lunar100", "libration", "impact", "albedo", "imp", "other"})


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


class LunarMapService:
    """Tiles, features and the small readouts the map page asks for."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._terrain = None
        self._features: Optional[List[Dict[str, Any]]] = None
        self._tiles: "OrderedDict[Tuple[Any, ...], bytes]" = OrderedDict()
        self._detail: "OrderedDict[Tuple[int, int, int], Any]" = OrderedDict()
        self._shadows: "OrderedDict[Tuple[Any, ...], Any]" = OrderedDict()
        self._detail_source = None

    # -- data ------------------------------------------------------------
    def terrain(self):
        with self._lock:
            if self._terrain is None:
                self._terrain = moonlib.open_terrain(include_albedo=True)
            return self._terrain

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
            "version": moonlib.__version__,
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
                "default": key in {"mare", "crater", "mons", "statio"},
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
            classes = {"mare", "crater", "mons", "statio"}
        west = _finite(query.get("west"), -180.0)
        east = _finite(query.get("east"), 180.0)
        south = _finite(query.get("south"), -90.0)
        north = _finite(query.get("north"), 90.0)
        zoom = int(_finite(query.get("zoom"), 0.0))
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        min_diameter = _MIN_DIAMETER_BY_ZOOM.get(zoom, 0.0)
        limit = int(max(1, min(4000, _finite(query.get("limit"), 2000.0))))

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

    # -- tiles -----------------------------------------------------------
    def tile(self, zoom: int, x: int, y: int,
             light: Optional[Tuple[float, float]] = None) -> bytes:
        cols, rows = _tile_counts(zoom)
        if not (0 <= zoom <= MAX_ZOOM and 0 <= x < cols and 0 <= y < rows):
            raise moonlib.MoonlibError("tile outside the pyramid.")
        light_key = (
            (round(float(light[0]), 3), round(float(light[1]), 3))
            if light is not None else None
        )
        key = (int(zoom), int(x), int(y), light_key)
        with self._lock:
            cached = self._tiles.get(key)
            if cached is not None:
                self._tiles.move_to_end(key)
                return cached
        png = self._render_tile(int(zoom), int(x), int(y), light)
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
                     light: Optional[Tuple[float, float]] = None) -> bytes:
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
        ambient = 0.18 if light is None else 0.10
        gain = 0.92 if light is None else 1.05
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
        shade = np.clip(shade, 0.0, 1.15)

        colors = None
        if terrain.albedo is not None:
            try:
                colors = np.asarray(
                    terrain.albedo_rgb(lon, lat, albedo_level), dtype=np.float64
                )
            except Exception:
                colors = None
        if colors is not None and colors.shape[-1] >= 3:
            base = np.clip(colors[..., :3], 0.0, 1.0) ** 0.45
            base = np.clip(base * 1.22, 0.0, 1.0)
        else:
            grey = np.clip(0.55 + 0.55 * (heights - float(np.mean(heights))) / 8.0,
                           0.15, 1.0)
            base = np.repeat(grey[..., None], 3, axis=-1)
        rgb = np.clip(base * shade[..., None], 0.0, 1.0)
        # A soft highlight shoulder: the full-Moon shading otherwise multiplies
        # bright highland albedo straight to 255 and the landforms disappear in
        # a white field.  The curve keeps the midtones and rolls the highlights
        # off instead of clipping them.
        rgb = rgb * 0.92 / (1.0 + 0.18 * rgb)
        image = Image.fromarray((rgb * 255.0 + 0.5).astype(np.uint8))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", compress_level=3)
        return buffer.getvalue()


class Handler(BaseHTTPRequestHandler):
    service: LunarMapService

    server_version = "TsukikageMap/1.0"

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
            if path == "/api/map/features":
                return self._json(200, self.service.feature_query(query))
            if path == "/api/map/search":
                return self._json(200, self.service.search(query))
            if path == "/api/map/point":
                return self._json(200, self.service.point(query))
            if path == "/api/map/lighting":
                return self._json(200, self.service.lighting(query))
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
                png = self.service.tile(zoom, x, y, light)
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
