#!/usr/bin/env python3
"""Build a global 512 px/deg DEM from SLDEM2015 plus the LOLA polar caps.

SLDEM2015 is the LOLA grid re-solved with Kaguya Terrain Camera stereo pairs:
inside +/-60 degrees the 1-4 km gaps between LOLA tracks are measured rather
than interpolated, which is exactly what a grazing-sun shadow edge shows first.
The PDS delivery is 32 float32 tiles of 45 x 30 degrees (45 GB), and it stops
at +/-60 degrees -- there is no 512 ppd polar product.

This repository draws global rasters on a 2:1 equirectangular grid, so the
polar caps are taken from the LOLA GDR 512 ppd mosaic already produced by
``tools/fetch_lola_512.py``.  The result is one global raster:

``kernels/SLDEM2015_512.tif``
    184 320 x 92 160 int16, half-metres above the 1737.4 km sphere.  Equator
    and mid-latitudes are SLDEM2015; poleward of 60 degrees is LOLA GDR V3.0
    (the same 59.2 m grid, but without the stereo fill, because SLDEM2015 has
    no measurements there).

The download is row-aligned and resumable.  Each tile arrives as 30 chunks of
512 rows (45 MB each); a chunk is converted to half-metres and written straight
into the preallocated BigTIFF, then a marker records that it is safe to skip on
the next run.  There is never a whole tile on disk, so the peak footprint is
the 34 GB output plus a handful of 45 MB chunks.

Usage
-----
    ./.venv/bin/python tools/fetch_sldem_512.py             # download + stitch
    ./.venv/bin/python tools/fetch_sldem_512.py --check     # verify only
    ./.venv/bin/python tools/fetch_sldem_512.py --workers 12
    ./.venv/bin/python tools/fetch_sldem_512.py --reset     # start over
"""

from __future__ import annotations

import argparse
import itertools
import shutil
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from _bootstrap import ROOT, use_src  # noqa: E402
from _striped_tiff import DATA_START, write_int16_header  # noqa: E402

use_src()

BASE_URL = "https://imbrium.mit.edu/DATA/SLDEM2015/TILES/FLOAT_IMG/"
PPD = 512
WIDTH = 360 * PPD                 # 184 320
HEIGHT = 180 * PPD                # 92 160
TILE_W = 45 * PPD                 # 23 040
TILE_H = 30 * PPD                 # 15 360
LAT_LIMIT = 60.0
#: First output row covered by SLDEM2015 (60 N) and the number of rows it spans.
SLDEM_ROW0 = int(round((90.0 - LAT_LIMIT) * PPD))     # 15 360
SLDEM_ROWS = int(round(2.0 * LAT_LIMIT * PPD))        # 61 440
POLAR_ROWS = (SLDEM_ROW0, HEIGHT - SLDEM_ROW0 - SLDEM_ROWS)  # top, bottom
#: One chunk is 512 rows * 23 040 columns * 4 bytes = 45 MB.  Row-aligned so a
#: chunk can be converted and pasted without any temporary tile file.
CHUNK_ROWS = 512
CHUNK_BYTES = CHUNK_ROWS * TILE_W * 4
CHUNKS_PER_TILE = TILE_H // CHUNK_ROWS
#: The host throttles per connection; several ranges at once are what makes 45 GB
#: finish in an evening rather than a week.
WORKERS = 8
#: SLDEM2015 is float32 kilometres; the repository's global rasters are int16
#: half-metres.  The product range (-4.9 .. +3.5 km) fits with a wide margin.
SCALE = 0.0005

OUTPUT = ROOT / "kernels" / "SLDEM2015_512.tif"
LOLA_SOURCE = ROOT / "kernels" / "ldem_512.tif"
MARKERS = ROOT / "kernels" / ".sldem2015_512_chunks"
#: Discovery ignores a raster carrying this side file.  The mosaic is written in
#: place over an hour or two, and offering a half-written product as a data
#: source would let a close-up sample rows that are still zero.
INCOMPLETE = OUTPUT.with_name(OUTPUT.name + ".incomplete")

#: Every 512 ppd SLDEM tile, in the product's own 0-360 E / north-up convention.
BANDS = (
    ("60S_30S", -60.0, -30.0),
    ("30S_00S", -30.0, 0.0),
    ("00N_30N", 0.0, 30.0),
    ("30N_60N", 30.0, 60.0),
)
BLOCKS = tuple(f"{west:03d}_{west + 45:03d}" for west in range(0, 360, 45))


def tile_name(south: float, north: float, west: int, east: int) -> str:
    # The online catalogue owns the naming rule (including the ``30S_00S``
    # equator band); sharing it keeps the downloader and the region-patch path
    # from disagreeing about which files exist.
    from moonlib import online

    return online._sldem_tile(south, north, west, east)


def output_row(north: float) -> int:
    """First output row of a tile whose top edge is *north*."""
    return int(round((90.0 - north) * PPD))


def output_column(west: int) -> int:
    """First output column of a 0-360 E tile in the 180 W..180 E grid."""
    return ((west + 180) % 360) * PPD


def _chunk_start(index: int) -> int:
    return index * CHUNK_BYTES


def _marker(tile: str, index: int) -> Path:
    return MARKERS / f"{tile}.{index:02d}.done"


def _fetch(url: str, start: int, stop: int) -> bytes:
    """One range, retried.  The host occasionally drops a connection in a burst."""
    want = stop - start + 1
    for attempt in range(6):
        try:
            request = urllib.request.Request(
                url, headers={"Range": f"bytes={start}-{stop}"})
            with urllib.request.urlopen(request, timeout=240) as response:
                if response.status != 206:
                    raise OSError(f"range refused (HTTP {response.status})")
                blob = response.read()
            if len(blob) != want:
                raise OSError(f"{len(blob)} of {want} bytes")
            return blob
        except Exception:
            if attempt == 5:
                raise
            time.sleep(1.0 + 1.5 * attempt)
    raise AssertionError("unreachable")  # pragma: no cover


def _encode(blob: bytes) -> np.ndarray:
    """float32 kilometres -> int16 half-metres, exactly the stored convention."""
    values = np.frombuffer(blob, dtype="<f4").reshape(CHUNK_ROWS, TILE_W)
    # SLDEM's own range is -4.899 .. +3.466 km; rint/2000 cannot overflow, but
    # clipping keeps a corrupt range read from wrapping into a valid-looking
    # height at the opposite sign.
    return np.clip(np.rint(values * 2000.0), -32768, 32767).astype("<i2")


def _copy_polar_caps(destination: np.ndarray) -> None:
    """Copy 60-90 N and 60-90 S from the local LOLA 512 mosaic.

    SLDEM2015 has no 512 ppd product beyond +/-60 degrees.  The alternative is
    to resample the 256 ppd LOLA polar tiles, which would be *coarser* than the
    raster already on disk; reusing the global LOLA 512 mosaic keeps the whole
    file at 59.2 m and only gives up the stereo fill where none exists.
    """
    from moonlib import terrain

    if not LOLA_SOURCE.is_file():
        raise SystemExit(
            f"missing {LOLA_SOURCE}\n"
            "The polar caps come from the LOLA GDR 512 mosaic; build it first:\n"
            "  ./.venv/bin/python tools/fetch_lola_512.py"
        )
    reader = terrain._StrippedTiff(LOLA_SOURCE)
    try:
        if (reader.width, reader.height) != (WIDTH, HEIGHT):
            raise SystemExit(
                f"{LOLA_SOURCE.name} is {reader.width} x {reader.height}, "
                f"expected {WIDTH} x {HEIGHT}."
            )
        if reader.samples != 1 or reader.dtype != np.dtype("<i2"):
            raise SystemExit(
                f"{LOLA_SOURCE.name} is not a single-band int16 raster."
            )
        # The mosaic written by fetch_lola_512.py is one contiguous pixel block,
        # so the caps can be copied in large slices instead of row by row.
        strip_bytes = reader.rows_per_strip * reader.row_bytes
        contiguous = all(
            reader.offsets[i] == reader.offsets[0] + i * strip_bytes
            for i in range(len(reader.offsets))
        )
        if contiguous:
            source = np.memmap(
                LOLA_SOURCE, dtype="<i2", mode="r",
                offset=reader.offsets[0], shape=(HEIGHT, WIDTH),
            )
            try:
                for row0, rows in ((0, POLAR_ROWS[0]),
                                   (HEIGHT - POLAR_ROWS[1], POLAR_ROWS[1])):
                    for start in range(row0, row0 + rows, 1024):
                        stop = min(start + 1024, row0 + rows)
                        destination[start:stop] = source[start:stop]
            finally:
                del source
        else:  # pragma: no cover - another writer's layout
            for row0, rows in ((0, POLAR_ROWS[0]),
                               (HEIGHT - POLAR_ROWS[1], POLAR_ROWS[1])):
                for row in range(row0, row0 + rows):
                    destination[row] = reader.row(row)
        destination.flush()
    finally:
        reader.close()
    print(f"  polar caps: {POLAR_ROWS[0]} N rows + {POLAR_ROWS[1]} S rows "
          f"from {LOLA_SOURCE.name}", flush=True)


def _pending_chunks(tile: str) -> list:
    return [index for index in range(CHUNKS_PER_TILE)
            if not _marker(tile, index).is_file()]


def download_tile(
    destination: np.ndarray,
    south: float,
    north: float,
    west: int,
    east: int,
    *,
    workers: int,
) -> None:
    """Fetch every missing row chunk of one tile straight into the output."""
    tile = tile_name(south, north, west, east)
    missing = _pending_chunks(tile)
    if not missing:
        print(f"  {tile}: already written", flush=True)
        return
    url = BASE_URL + tile
    col0 = output_column(west)
    row0 = output_row(north)
    have = CHUNKS_PER_TILE - len(missing)
    print(f"  {tile}: {have}/{CHUNKS_PER_TILE} chunks on disk", flush=True)
    started = time.time()
    done = itertools.count()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_fetch, url, _chunk_start(index),
                        _chunk_start(index) + CHUNK_BYTES - 1): index
            for index in missing
        }
        for future in as_completed(futures):
            index = futures[future]
            blob = future.result()
            rows = _encode(blob)
            target_row = row0 + index * CHUNK_ROWS
            destination[target_row:target_row + CHUNK_ROWS,
                        col0:col0 + TILE_W] = rows
            # Flush before the marker: a marker must never outlive the pixels it
            # claims are on disk, or a resumed run would skip a hole.
            destination.flush()
            marker = _marker(tile, index)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("ok\n", encoding="ascii")
            finished = next(done) + 1
            if finished % 5 == 0 or finished == len(missing):
                elapsed = time.time() - started
                rate = finished * CHUNK_BYTES / max(elapsed, 1e-9) / 1e6
                print(f"    {finished}/{len(missing)} chunks, {rate:.1f} MB/s",
                      flush=True)


def _window(reader, lon: float, lat: float, half_deg: float = 1.2) -> np.ndarray:
    """A small patch of the output in metres, for the shape check."""
    half = int(half_deg * PPD)
    row = int(round((90.0 - lat) * PPD))
    col = int(round(((lon + 180.0) % 360.0) * PPD))
    rows = [
        np.asarray(reader.row(max(0, min(reader.height - 1, row + dy)))[
            col - half:col + half], dtype=np.int16)
        for dy in range(-half, half, 32)
    ]
    return np.concatenate(rows).astype(np.float32) * 0.5


def check() -> int:
    from moonlib import terrain

    if not OUTPUT.is_file():
        print(f"missing {OUTPUT}")
        return 1
    if INCOMPLETE.exists():
        print(f"{OUTPUT.name} is still incomplete ({INCOMPLETE.name} is present)")
        return 1
    reader = terrain._StrippedTiff(OUTPUT)
    print(f"{OUTPUT.name}: {reader.width} x {reader.height}, {reader.bits}-bit, "
          f"samples={reader.samples}, compression={reader.compression}, "
          f"rows/strip={reader.rows_per_strip}, "
          f"{OUTPUT.stat().st_size / 1e9:.1f} GB")
    ok = (reader.width, reader.height) == (WIDTH, HEIGHT)
    ok = ok and reader.samples == 1 and reader.dtype == np.dtype("<i2")
    ok = ok and terrain._dem_scale(reader, OUTPUT) == SCALE

    # A crater is a shape, not a number: the floor is kilometres below the rim,
    # and a block of zeros or a mis-stitched column shows up immediately.
    copernicus = _window(reader, 339.9, 9.6)
    low, high = float(copernicus.min()), float(copernicus.max())
    print(f"  Copernicus (9.6 N, 20.1 W): {low:.0f} .. {high:.0f} m")
    ok = ok and low < -3000.0 and high > -1500.0
    for name, lon, lat in (("Tycho", -11.4, -43.3), ("Imbrium", -15.6, 32.8)):
        values = _window(reader, lon, lat)
        print(f"  {name}: {values.min():.0f} .. {values.max():.0f} m")
        ok = ok and values.max() > -6000.0 and values.min() < 0.0

    # No wholesale hole.  SLDEM2015 has a handful of fill samples, so the test
    # is a fraction, not "every sample is non-zero".
    for label, lat in (("equator", 0.0), ("60 N seam", 59.0), ("75 N", 75.0),
                       ("75 S", -75.0)):
        row = np.asarray(reader.row(int(round((90.0 - lat) * PPD))),
                         dtype=np.int16)
        blank = int((row == 0).sum())
        print(f"  {label} row: {blank} of {row.size} zero samples")
        ok = ok and blank < row.size // 100

    # The polar caps are LOLA, not SLDEM; make sure they were actually copied
    # rather than left as the zero-filled preallocation.
    for label, row_index in (("north pole", 0), ("south pole", HEIGHT - 1)):
        row = np.asarray(reader.row(row_index), dtype=np.int16)
        print(f"  {label}: {float(row.astype(np.float32).min()) * 0.5:.0f} .. "
              f"{float(row.astype(np.float32).max()) * 0.5:.0f} m")
        ok = ok and int(np.count_nonzero(row)) > row.size // 2
    reader.close()

    sources = terrain.discover_dem_sources()
    print("  discoverable:",
          [(path.name, round(km * 1000, 1)) for path, km in sources])
    ok = ok and any(path.resolve() == OUTPUT.resolve() for path, _ in sources)
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="verify the output only")
    parser.add_argument("--reset", action="store_true",
                        help="delete the partial output and its chunk markers")
    parser.add_argument("--workers", type=int, default=WORKERS,
                        help=f"simultaneous range requests (default {WORKERS})")
    args = parser.parse_args(argv)

    if args.check:
        return check()

    if args.reset:
        INCOMPLETE.unlink(missing_ok=True)
        for path in (OUTPUT,):
            if path.is_file():
                print(f"  removing {path} ({path.stat().st_size / 1e9:.1f} GB)",
                      flush=True)
                path.unlink()
        if MARKERS.is_dir():
            shutil.rmtree(MARKERS)
        print("  reset complete", flush=True)

    from _striped_tiff import strip_layout

    layout = strip_layout(WIDTH, HEIGHT)
    # Keep the incomplete mosaic out of the data-source menu for the whole run.
    INCOMPLETE.write_text("downloading\n", encoding="ascii")
    if OUTPUT.is_file():
        if OUTPUT.stat().st_size != layout["size"]:
            raise SystemExit(
                f"{OUTPUT} is {OUTPUT.stat().st_size} bytes, expected "
                f"{layout['size']}. Pass --reset to start over."
            )
        print(f"resuming into {OUTPUT.name} "
              f"({OUTPUT.stat().st_size / 1e9:.1f} GB)", flush=True)
    else:
        free = shutil.disk_usage(ROOT).free
        if free < layout["size"] + (1 << 30):
            raise SystemExit(
                f"need about {layout['size'] / 1e9:.1f} GB free, "
                f"only {free / 1e9:.1f} GB available."
            )
        write_int16_header(OUTPUT, WIDTH, HEIGHT)
        MARKERS.mkdir(parents=True, exist_ok=True)
        print(f"preallocated {OUTPUT.name} "
              f"({layout['size'] / 1e9:.1f} GB)", flush=True)

    target = np.memmap(OUTPUT, dtype="<i2", mode="r+", offset=DATA_START,
                       shape=(HEIGHT, WIDTH))
    started = time.time()
    try:
        polar_marker = MARKERS / "polar.done"
        if polar_marker.is_file():
            print("  polar caps: already written", flush=True)
        else:
            _copy_polar_caps(target)
            polar_marker.parent.mkdir(parents=True, exist_ok=True)
            polar_marker.write_text("ok\n", encoding="ascii")

        band_total = len(BANDS)
        for done, (band, south, north) in enumerate(BANDS, start=1):
            for block in BLOCKS:
                west, east = (int(part) for part in block.split("_"))
                try:
                    download_tile(target, south, north, west, east,
                                  workers=max(1, args.workers))
                except Exception as error:
                    raise SystemExit(
                        f"{band} {block}: {error}\n"
                        "Finished chunks are marked; rerun to resume."
                    ) from error
            elapsed = time.time() - started
            left_bands = band_total - done
            print(f"  {done}/{band_total} bands, {elapsed / 60:.0f} min elapsed, "
                  f"about {elapsed / max(done, 1) * left_bands / 60:.0f} min left",
                  flush=True)
    finally:
        target.flush()
        del target

    # Discovery has to see the finished raster, so the sentinel comes off
    # before the check -- and goes back if the check finds a hole.
    INCOMPLETE.unlink(missing_ok=True)
    result = check()
    if result == 0:
        shutil.rmtree(MARKERS, ignore_errors=True)
    else:
        INCOMPLETE.write_text("incomplete\n", encoding="ascii")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
