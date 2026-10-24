#!/usr/bin/env python3
"""Region-sized downloads from the tiled online elevation products.

The full products are tens of gigabytes; a close-up needs a few degrees.  Both
catalogues below are plain binary tiles on a simple cylindrical grid, so a patch
is a set of HTTP ``Range`` reads -- no GDAL, no JP2 decoder, no tiling scheme to
implement.

Two products, and they are different trades:

``sldem2015-512``
    LOLA plus Kaguya Terrain Camera stereo, 512 px/deg (59 m).  The same pixel
    size as the LOLA GDR 512 product already on disk, but the 1-4 km gaps
    between LOLA tracks are *measured* rather than interpolated, which is what a
    grazing-sun shadow edge shows first.
``lola-1024``
    The LOLA GDR at 1024 px/deg (29.6 m): twice the sampling, but it is the
    2011 preliminary solution and only about 5% of its cells hold an
    observation, so most of the extra detail is interpolation.  Offered because
    it is the only range-fetchable product with a finer grid.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from .errors import MoonlibError

ProgressHook = Callable[[float, str], None]

#: How many range requests are in flight at once.  These hosts throttle a single
#: connection hard; ten is what turns a four-minute patch into twenty seconds.
WORKERS = 6


def _tag(value: float, digits: int = 2, upper: bool = False) -> str:
    """``-30`` -> ``30s`` / ``30S``, ``0`` -> ``00n`` / ``00N``."""
    sign = "n" if value >= 0 else "s"
    text = f"{abs(int(round(value))):0{digits}d}{sign}"
    return text.upper() if upper else text


def _sldem_lat_tag(value: float, band_south: float) -> str:
    """A tile edge tag, with the equator taking its band's hemisphere.

    SLDEM2015 names the southern 30-degree band ``30S_00S`` and the northern one
    ``00N_30N``: a bare ``00N`` for the southern band's top edge is a 404, even
    though zero is "not negative".  The band decides which zero it is.
    """
    if abs(float(value)) < 1e-9:
        return "00S" if band_south < 0 else "00N"
    return _tag(value, 2, upper=True)


def _sldem_tile(south: float, north: float, west: float, east: float) -> str:
    # Note the ``_FLOAT`` before the extension: the same tree also holds the
    # azimuth and slope products, whose tiles are named ..._AZ_... / ..._SL_...
    return (f"SLDEM2015_512_{_sldem_lat_tag(south, south)}"
            f"_{_sldem_lat_tag(north, south)}"
            f"_{int(west):03d}_{int(east):03d}_FLOAT.IMG")


def _lola_tile(south: float, north: float, west: float, east: float, ppd: int) -> str:
    return (f"ldem_{ppd}_{_tag(south)}_{_tag(north)}"
            f"_{int(west):03d}_{int(east):03d}.img")


CATALOGUE: Dict[str, Dict[str, Any]] = {
    "sldem2015-512": {
        "label": "SLDEM2015 512 ppd · 辉夜号立体填缝",
        "pixels_per_degree": 512,
        "metres_per_px": 59.2,
        "dtype": "<f4",
        "to_km": 1.0,                        # already kilometres
        "head": 0,
        "lat_step": 30.0,
        "lon_step": 45.0,
        "base_url": "https://imbrium.mit.edu/DATA/SLDEM2015/TILES/FLOAT_IMG/",
        "tile": _sldem_tile,
        "note": "59 m 格点，但轨道间由辉夜号 TC 立体补测：掠射阴影边缘比纯 LOLA 干净。",
    },
    "lola-1024": {
        "label": "LOLA GDR 1024 ppd · 29.6 m（preliminary）",
        "pixels_per_degree": 1024,
        "metres_per_px": 29.6,
        "dtype": "<i2",
        "to_km": 5e-4,                       # half-metres -> kilometres
        "head": 0,
        "lat_step": 15.0,
        "lon_step": 30.0,
        "base_url": ("https://pds-geosciences.wustl.edu/lro/lro-l-lola-3-rdr-v1"
                     "/lrolol_1xxx/data/lola_gdr/cylindrical/img/"),
        "tile": lambda s, n, w, e: _lola_tile(s, n, w, e, 1024),
        "note": "格点细一倍，但 2011 preliminary 解算、约 95% 格点没有观测，细节多为内插。",
    },
}


def catalogue() -> List[Dict[str, Any]]:
    """The online products, in the shape a menu wants."""
    out = []
    for key, product in CATALOGUE.items():
        itemsize = np.dtype(product["dtype"]).itemsize
        out.append({
            "id": key,
            "label": product["label"],
            "metres_per_px": product["metres_per_px"],
            "pixels_per_degree": product["pixels_per_degree"],
            "note": product["note"],
            "megabytes_per_square_degree": round(
                product["pixels_per_degree"] ** 2 * itemsize / 1e6, 2),
        })
    return out


_LOCAL = threading.local()


def _connection(url: str) -> Any:
    """One keep-alive HTTPS connection per worker thread.

    Per-row requests are what makes a 2-degree patch cheap in bytes, but a fresh
    TLS handshake per row is what makes the same patch fail: these hosts drop
    connections opened in a burst, and a 2000-row patch at one handshake a row
    hits that limit every time.  One connection per thread fixes both.
    """
    import http.client
    from urllib.parse import urlsplit

    pieces = urlsplit(url)
    conn = getattr(_LOCAL, "conn", None)
    if conn is None or getattr(_LOCAL, "host", None) != pieces.netloc:
        if conn is not None:
            conn.close()
        conn = http.client.HTTPSConnection(pieces.netloc, timeout=120)
        _LOCAL.conn = conn
        _LOCAL.host = pieces.netloc
    return conn, pieces.path


def _drop_connection() -> None:
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:  # pragma: no cover - already broken
            pass
    _LOCAL.conn = None
    _LOCAL.host = None


def _fetch_row(url: str, start: int, stop: int) -> bytes:
    for attempt in range(5):
        try:
            conn, path = _connection(url)
            conn.request("GET", path, headers={"Range": f"bytes={start}-{stop}"})
            response = conn.getresponse()
            payload = response.read()
            if response.status != 206 or len(payload) != stop - start + 1:
                raise OSError(f"HTTP {response.status}, {len(payload)} bytes")
            return payload
        except Exception:  # noqa: BLE001 - retried, then raised
            _drop_connection()
            if attempt == 4:
                raise
            time.sleep(0.5 + 0.5 * attempt)
    raise AssertionError("unreachable")  # pragma: no cover


def patch_bounds_for(
    lon_min: float,
    lon_max: float,
    lat_min: float,
    lat_max: float,
    *,
    margin_deg: float = 2.5,
) -> tuple:
    """The rectangle to download: the frame's footprint plus the renderer's margin.

    The margin is what the window cutter adds when it reads a close-up; if the
    patch were only as big as the frame, the renderer would decline to use it
    and the download would have been for nothing.
    """
    import math

    # A little slack on top of the renderer's margin: the frame it measures at
    # render time differs slightly from the one the download was sized on, and a
    # patch that is a tenth of a degree too small is a patch that gets declined.
    margin_deg = float(margin_deg) + 1.2
    lon_span = margin_deg / max(
        math.cos(math.radians(max(abs(lat_min), abs(lat_max)))), 1e-3)
    west = (lon_min - lon_span) % 360.0
    east = (lon_max + lon_span) % 360.0
    if west >= east:  # the margin pushed the box across 0 degrees
        west, east = 0.0, min(360.0, east + (360.0 - west))
    return (min(west, 359.0), max(min(east, 360.0), west + 0.5),
            max(-90.0, lat_min - margin_deg), min(90.0, lat_max + margin_deg))


def _tile_for(product: Dict[str, Any], lat: float, lon: float) -> Dict[str, Any]:
    """The tile covering a point, with the point's offsets inside it."""
    ppd = product["pixels_per_degree"]
    lon = float(lon) % 360.0
    lat_step, lon_step = product["lat_step"], product["lon_step"]
    south = float(np.floor((lat + 90.0) / lat_step) * lat_step - 90.0)
    west = float(np.floor(lon / lon_step) * lon_step)
    south = min(max(south, -90.0), 90.0 - lat_step)
    west = min(max(west, 0.0), 360.0 - lon_step)
    north, east = south + lat_step, west + lon_step
    width = int(round(lon_step * ppd))
    height = int(round(lat_step * ppd))
    return {
        "name": product["tile"](south, north, west, east),
        "lat_band": (south, north),
        "lon_band": (west, east),
        "width": width,
        "height": height,
        "row": int(round((north - lat) * ppd - 0.5)),
        "col": int(round((lon - west) * ppd - 0.5)),
    }


def fetch_patch(
    product_id: str,
    lon_min: float,
    lon_max: float,
    lat_min: float,
    lat_max: float,
    destination: Path,
    *,
    progress: Optional[ProgressHook] = None,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """Download one rectangle and write a regional ``.npy``/``.json`` pair.

    The patch is aligned to the product's own grid (so every sample is a real
    sample, not a resampled one) and stored as kilometres above the 1737.4 km
    sphere, running east from ``lon_min`` -- what
    :func:`moonlib.terrain.load_region_dem` reads.
    """
    product = CATALOGUE.get(product_id)
    if product is None:
        raise MoonlibError(f"未知的在线数据源：{product_id}")
    ppd = product["pixels_per_degree"]
    raw_lon_min, raw_lon_max = float(lon_min), float(lon_max)
    if raw_lon_max < raw_lon_min or (raw_lon_max - raw_lon_min) > 180.0:
        raise MoonlibError("跨 0° 经线的选框暂不支持，请把选框放在 0–360°E 的同一侧。")
    lon_min, lon_max = raw_lon_min % 360.0, raw_lon_max % 360.0
    lat_min, lat_max = sorted((float(lat_min), float(lat_max)))
    if lon_max - lon_min > 90.0 or lat_max - lat_min > 60.0:
        raise MoonlibError("区域太大：单次补片请控制在 90° × 60° 以内。")
    west = np.floor(lon_min * ppd) / ppd
    east = np.ceil(lon_max * ppd) / ppd
    south = np.floor(lat_min * ppd) / ppd
    north = np.ceil(lat_max * ppd) / ppd
    width = int(round((east - west) * ppd))
    height = int(round((north - south) * ppd))
    if width < 8 or height < 8:
        raise MoonlibError("区域太小：至少要有 8 × 8 个像素。")

    lat_step = product["lat_step"]
    lon_step = product["lon_step"]
    lat_bands = [(-90.0 + i * lat_step, -90.0 + (i + 1) * lat_step)
                 for i in range(int(round(180.0 / lat_step)))]
    lon_bands = [(i * lon_step, (i + 1) * lon_step)
                 for i in range(int(round(360.0 / lon_step)))]
    jobs = []
    for band_south, band_north in lat_bands:
        for band_west, band_east in lon_bands:
            lo_lat, hi_lat = max(south, band_south), min(north, band_north)
            lo_lon, hi_lon = max(west, band_west), min(east, band_east)
            if lo_lat >= hi_lat or lo_lon >= hi_lon:
                continue
            jobs.append({
                "url": product["base_url"] + product["tile"](
                    band_south, band_north, band_west, band_east),
                "name": product["tile"](band_south, band_north, band_west, band_east),
                "rows": (int(round((band_north - hi_lat) * ppd)),
                         int(round((band_north - lo_lat) * ppd))),
                "cols": (int(round((lo_lon - band_west) * ppd)),
                         int(round((hi_lon - band_west) * ppd))),
                "paste": (int(round((north - hi_lat) * ppd)),
                          int(round((north - lo_lat) * ppd)),
                          int(round((lo_lon - west) * ppd)),
                          int(round((hi_lon - west) * ppd))),
                "tile_size": (int(round(lon_step * ppd)),
                              int(round(lat_step * ppd))),
            })
    if not jobs:
        raise MoonlibError("这个矩形没有落在任何瓦片里。")

    dtype = np.dtype(product["dtype"])
    values = np.zeros((height, width), dtype=np.float32)
    label = name or product_id
    todo = sum(job["rows"][1] - job["rows"][0] for job in jobs)
    done = [0]
    lock = threading.Lock()

    for index, job in enumerate(jobs, start=1):
        row0, row1 = job["rows"]
        col0, col1 = job["cols"]
        row_bytes = job["tile_size"][0] * dtype.itemsize
        block = np.empty((row1 - row0, col1 - col0), dtype=dtype)

        def one(offset: int, job=job, block=block, row0=row0, col0=col0,
                col1=col1, row_bytes=row_bytes, dtype=dtype) -> None:
            row = row0 + offset
            start = product["head"] + row * row_bytes + col0 * dtype.itemsize
            stop = start + (col1 - col0) * dtype.itemsize - 1
            block[offset] = np.frombuffer(_fetch_row(job["url"], start, stop),
                                          dtype=dtype)
            with lock:
                done[0] += 1
                if progress is not None and (done[0] % 64 == 0 or done[0] == todo):
                    progress(done[0] / max(todo, 1),
                             f"{label} {done[0]}/{todo} 行（瓦片 {index}/{len(jobs)}）")

        with ThreadPoolExecutor(WORKERS) as pool:
            list(pool.map(one, range(row1 - row0)))

        py0, py1, px0, px1 = job["paste"]
        values[py0:py1, px0:px1] = np.asarray(block, dtype=np.float32) * product["to_km"]

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # np.save appends ".npy" to a path that does not already end in it, so the
    # temporary is written through a handle instead of by name.
    temporary = destination.with_name(destination.name + ".part")
    with open(temporary, "wb") as handle:
        np.save(handle, values)
    temporary.replace(destination)
    sidecar = {
        "name": label,
        "shape": [int(values.shape[0]), int(values.shape[1])],
        "bounds": [float(west), float(east), float(south), float(north)],
        "unit": "km",
        "source": product_id,
        "pixels_per_degree": ppd,
        "km_per_px": float(30.323 / ppd),
        "fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    destination.with_suffix(".json").write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if progress is not None:
        progress(1.0, f"{label}：完成")
    return sidecar
