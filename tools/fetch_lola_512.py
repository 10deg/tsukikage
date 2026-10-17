#!/usr/bin/env python3
"""Fetch the PDS LOLA GDR 512 px/deg global DEM into ``kernels/ldem_512.tif``.

The product is 16 tiles of 46 080 x 23 040 int16 samples (34 GB), laid out
0-360 E and north-up.  This script downloads one tile, copies its rows into a
preallocated global equirectangular BigTIFF, and deletes the tile, so the peak
footprint is the 34 GB output plus one 2.1 GB tile instead of 68 GB.

The tiles are fetched as 32 MB ranges over ten connections: the PDS node
throttles a single connection to about half a megabyte a second, and ten of them
give five -- which is the difference between 1.8 hours and 18.  Chunks are
written as separate files, so a killed run resumes where it stopped.

Four details matter and are easy to get wrong:

* **No header.**  The label is a side file; the ``.img`` is exactly
  46 080 x 23 040 samples (2 123 366 400 bytes).  A row is 92 160 bytes.
* **No rescaling.**  The stored sample is already height in half-metres above
  the 1737.4 km sphere (``SCALING_FACTOR = 0.5``), which is exactly what
  :data:`moonlib_s.terrain._KNOWN_DEM_SCALES` expects of ``ldem_<ppd>.tif``, so
  the stitch is a byte copy.
* **Column order.**  The repository reads a global raster as 180 W .. 180 E
  (``x = (lon + 180) * ppd``, see ``terrain.extract_region``), while the PDS
  tiles start at 0 E.  Each 90-degree tile therefore lands at a rotated column.
* **Strip layout.**  The result is an uncompressed, non-tiled BigTIFF with one
  strip per 16 rows: :class:`moonlib_s.terrain._StrippedTiff` reads a row by
  strip offset and refuses tiled or compressed files.

Usage
-----
    ./.venv/bin/python tools/fetch_lola_512.py            # download + stitch
    ./.venv/bin/python tools/fetch_lola_512.py --check    # verify only
    ./.venv/bin/python tools/fetch_lola_512.py --stitch   # tiles already here
"""

from __future__ import annotations

import argparse
import itertools
import shutil
import struct
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from _bootstrap import ROOT, use_src  # noqa: E402

use_src()

BASE_URL = (
    "https://pds-geosciences.wustl.edu/lro/lro-l-lola-3-rdr-v1/lrolol_1xxx"
    "/data/lola_gdr/cylindrical/img/"
)
PPD = 512
WIDTH = 360 * PPD            # 184 320
HEIGHT = 180 * PPD           # 92 160
TILE_W = 90 * PPD            # 46 080
TILE_H = 45 * PPD            # 23 040
#: The PDS label is a separate ``.lbl``; the ``.img`` is the samples and nothing
#: else -- its length is exactly 46 080 x 23 040 x 2.
TILE_HEAD = 0
TILE_BYTES = TILE_W * TILE_H * 2
#: 32 MB per request: big enough that the per-request latency disappears, small
#: enough that a dropped connection costs nothing worth mentioning.
CHUNK = 32 << 20
#: How many range requests are in flight at once.  The PDS node throttles per
#: connection, so this is what decides the wall-clock time.
WORKERS = 10
ROWS_PER_STRIP = 16
DATA_START = 65_536
STRIP_BYTES = ROWS_PER_STRIP * WIDTH * 2
STRIPS = HEIGHT // ROWS_PER_STRIP
PIXELS_END = DATA_START + STRIPS * STRIP_BYTES
OUTPUT = ROOT / "kernels" / "ldem_512.tif"
TILE_DIR = ROOT / "kernels" / ".ldem_512_tiles"

BANDS = (
    # (name, first output row)
    ("45n_90n", 0),
    ("00n_45n", TILE_H),
    ("45s_00s", 2 * TILE_H),
    ("90s_45s", 3 * TILE_H),
)
BLOCKS = ("000_090", "090_180", "180_270", "270_360")


def tile_name(band: str, block: str) -> str:
    return f"ldem_512_{band}_{block}.img"


def output_column(block: str) -> int:
    """First column of a 0-360 E tile in this repository's 180 W..180 E grid."""
    west = int(block.split("_")[0])
    return ((west + 180) % 360) * PPD


def _chunk_path(tile: Path, index: int) -> Path:
    return tile.with_name(f"{tile.name}.{index:05d}.part")


def _fetch_chunk(url: str, tile: Path, index: int) -> None:
    """One 32 MB range, retried, written atomically.

    The PDS node rate-limits a single connection to well under a megabyte a
    second and does not scale with request size, so the only way to finish 34 GB
    in a usable time is several connections at once.  Chunks are independent
    files: a killed run resumes where it stopped, and a half-written chunk is
    never mistaken for a whole one.
    """
    start = index * CHUNK
    stop = min(start + CHUNK, TILE_BYTES) - 1
    target = _chunk_path(tile, index)
    want = stop - start + 1
    for attempt in range(5):
        try:
            request = urllib.request.Request(
                url, headers={"Range": f"bytes={start}-{stop}"})
            with urllib.request.urlopen(request, timeout=180) as response:
                if response.status != 206:
                    raise OSError(f"range refused ({response.status})")
                blob = response.read()
            if len(blob) != want:
                raise OSError(f"{len(blob)} of {want} bytes")
            temporary = target.with_suffix(".part.writing")
            temporary.write_bytes(blob)
            temporary.replace(target)
            return
        except Exception as error:  # noqa: BLE001 - retried, then raised
            if attempt == 4:
                raise SystemExit(f"{tile.name} chunk {index}: {error}") from error
            time.sleep(1.5 * (attempt + 1))


def download(name: str) -> Path:
    """One tile: every missing chunk in parallel, then glued back together."""
    TILE_DIR.mkdir(parents=True, exist_ok=True)
    target = TILE_DIR / name
    if target.exists() and target.stat().st_size == TILE_BYTES:
        return target
    target.unlink(missing_ok=True)
    url = BASE_URL + name
    total = (TILE_BYTES + CHUNK - 1) // CHUNK
    missing = [index for index in range(total)
               if not (_chunk_path(target, index).exists()
                       and _chunk_path(target, index).stat().st_size
                       == min(CHUNK, TILE_BYTES - index * CHUNK))]
    have = total - len(missing)
    print(f"  {name}: {have}/{total} chunks on disk", flush=True)
    started = time.time()
    done = itertools.count()
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = [pool.submit(_fetch_chunk, url, target, index) for index in missing]
        for future in as_completed(futures):
            future.result()
            finished = next(done) + 1
            if finished % 8 == 0 or finished == len(missing):
                elapsed = time.time() - started
                rate = finished * CHUNK / max(elapsed, 1e-9) / 1e6
                print(f"    {finished}/{len(missing)} chunks, {rate:.1f} MB/s",
                      flush=True)
    with open(target, "wb") as out:
        for index in range(total):
            part = _chunk_path(target, index)
            with open(part, "rb") as source:
                shutil.copyfileobj(source, out, 1 << 20)
            part.unlink()
    if target.stat().st_size != TILE_BYTES:  # pragma: no cover - defensive
        raise SystemExit(f"{name}: assembled {target.stat().st_size} bytes")
    return target


def expected_size() -> int:
    return PIXELS_END + 512 + STRIPS * 16


def tile_is_present(destination: Path, row0: int, col0: int) -> bool:
    """Whether one 90 x 45 degree block has already been copied in.

    A killed run leaves the output partly written, and re-downloading the tiles
    it already copied is an hour of the reader's bandwidth.  The test is a grid
    of samples: lunar heights are never exactly zero over a whole block, so a
    block of zeros is a block nobody has written yet.
    """
    if not destination.is_file() or destination.stat().st_size != expected_size():
        return False
    data = np.memmap(destination, dtype="<i2", mode="r", offset=DATA_START,
                     shape=(HEIGHT, WIDTH))
    try:
        probe = data[row0:row0 + TILE_H:501, col0:col0 + TILE_W:499]
        return bool(np.any(probe != 0))
    finally:
        del data


def write_header(path: Path) -> None:
    """Preallocate the file and describe the image before any pixel arrives.

    Strip offsets depend only on the geometry, so the offsets can be written
    once, up front: every tile copy after this is a plain seek-and-write.
    """
    with open(path, "wb") as handle:
        handle.truncate(PIXELS_END + 512 + STRIPS * 16)
        ifd_offset = PIXELS_END
        counts_offset = ifd_offset + 8 + 11 * 20 + 8
        offsets_offset = counts_offset + 8 + STRIPS * 8
        entries = (
            (256, 4, 1, WIDTH),            # ImageWidth
            (257, 4, 1, HEIGHT),           # ImageLength
            (258, 3, 1, 16),               # BitsPerSample
            (259, 3, 1, 1),                # Compression = none
            (262, 3, 1, 1),                # Photometric = min-is-black
            (273, 16, STRIPS, offsets_offset),   # StripOffsets
            (277, 3, 1, 1),                # SamplesPerPixel
            (278, 4, 1, ROWS_PER_STRIP),   # RowsPerStrip
            (279, 16, STRIPS, counts_offset),    # StripByteCounts
            (284, 3, 1, 1),                # PlanarConfiguration
            (339, 3, 1, 2),                # SampleFormat = signed int
        )
        handle.seek(0)
        handle.write(b"II" + struct.pack("<HHHQ", 43, 8, 0, ifd_offset))
        handle.seek(ifd_offset)
        handle.write(struct.pack("<Q", len(entries)))
        for tag, kind, count, value in entries:
            handle.write(struct.pack("<HHQ", tag, kind, count))
            # The value field is 8 bytes and, when the value fits, it starts at
            # its first byte -- left-justified, not right-justified.
            if kind == 3:
                handle.write(struct.pack("<H", value) + b"\0" * 6)
            elif kind == 4:
                handle.write(struct.pack("<I", value) + b"\0" * 4)
            else:
                handle.write(struct.pack("<Q", value))
        handle.write(struct.pack("<Q", 0))
        handle.seek(counts_offset)
        handle.write(struct.pack(f"<{STRIPS}Q", *([STRIP_BYTES] * STRIPS)))
        handle.seek(offsets_offset)
        handle.write(struct.pack(
            f"<{STRIPS}Q", *[DATA_START + k * STRIP_BYTES for k in range(STRIPS)]))


def stitch_tile(destination: Path, band: str, row0: int, block: str) -> None:
    """Copy one 90 x 45 degree tile into the global grid.

    The tile is 46 080 wide and the output 184 320 wide, so a run of tile rows is
    *not* a run of output rows: the copy has to place each row at a column
    offset.  A memmap of the output makes that a strided assignment, which numpy
    does at disk speed, and chunking by rows keeps the working set at 100 MB
    instead of the tile's 2.1 GB.
    """
    name = tile_name(band, block)
    col0 = output_column(block)
    chunk_rows = 1024
    source = np.memmap(TILE_DIR / name, dtype="<i2", mode="r",
                       offset=TILE_HEAD, shape=(TILE_H, TILE_W))
    target = np.memmap(destination, dtype="<i2", mode="r+",
                       offset=DATA_START, shape=(HEIGHT, WIDTH))
    try:
        for offset in range(0, TILE_H, chunk_rows):
            rows = min(chunk_rows, TILE_H - offset)
            target[row0 + offset:row0 + offset + rows,
                   col0:col0 + TILE_W] = source[offset:offset + rows]
        target.flush()
    finally:
        del source
        del target
    print(f"  stitched {name} -> rows {row0}..{row0 + TILE_H}, col {col0}", flush=True)


def repair_band_seams(destination: Path) -> None:
    """Fill the zero run the product leaves in the first row of each latitude band.

    Every PDS tile starts its band with about 2020 zero samples (4 degrees of
    longitude) on line 1 only -- the label warns that "the gridfiles were
    produced in 45 degree latitude bands, and there may be edge artifacts at this
    boundary".  A row of zeros at 59 m resolution is a hairline of sea level
    across the equator and both tropics, which is exactly the sort of artefact
    that shows up as a bright line in a grazing-sun render.

    Only samples that are zero *and* have real data directly above and below are
    touched: a genuine zero crossing is surrounded by values of the same sign,
    so it cannot be mistaken for a fill.
    """
    data = np.memmap(destination, dtype="<i2", mode="r+", offset=DATA_START,
                     shape=(HEIGHT, WIDTH))
    try:
        for row in (0, TILE_H, 2 * TILE_H, 3 * TILE_H):
            here = data[row]
            below = data[row + 1]
            above = data[row - 1] if row else below
            broken = (here == 0) & (below != 0) & (above != 0)
            count = int(broken.sum())
            if not count:
                continue
            filled = (above[broken].astype(np.int32) + below[broken]) // 2
            here[broken] = filled.astype(np.int16)
            print(f"  band seam at row {row}: filled {count} zero samples", flush=True)
        data.flush()
    finally:
        del data


def check() -> int:
    from moonlib_s import terrain

    if not OUTPUT.is_file():
        print(f"missing {OUTPUT}")
        return 1
    reader = terrain._StrippedTiff(OUTPUT)
    print(f"{OUTPUT.name}: {reader.width} x {reader.height}, {reader.bits}-bit, "
          f"samples={reader.samples}, compression={reader.compression}, "
          f"rows/strip={reader.rows_per_strip}, {OUTPUT.stat().st_size / 1e9:.1f} GB")
    ok = (reader.width, reader.height) == (WIDTH, HEIGHT)

    def window(lon: float, lat: float, half_deg: float = 1.5) -> np.ndarray:
        half = int(half_deg * PPD)
        row = int((90.0 - lat) * PPD)
        col = int(((lon + 180.0) % 360) * PPD)
        rows = [
            np.asarray(reader.row(max(0, min(reader.height - 1, row + dy)))[
                col - half:col + half], dtype=np.int16)
            for dy in range(-half, half, 32)
        ]
        return np.concatenate(rows).astype(np.float32) * 0.5

    # A crater is a shape, not a number: the floor is kilometres below the rim,
    # and a block of zeros or a mis-stitched column shows up immediately.
    copernicus = window(339.9, 9.6)
    low, high = float(copernicus.min()), float(copernicus.max())
    print(f"  Copernicus (9.6 N, 20.1 W): {low:.0f} .. {high:.0f} m")
    ok = ok and low < -3000.0 and high > -1500.0
    for name, lon, lat in (("Tycho", -11.4, -43.3), ("Imbrium", -15.6, 32.8)):
        values = window(lon, lat)
        print(f"  {name}: {values.min():.0f} .. {values.max():.0f} m")
        ok = ok and values.max() > -6000.0 and values.min() < 0.0

    # No wholesale hole: the tiles leave a few hundred samples at each pole, but
    # an equatorial row must be all data.
    equator = np.asarray(reader.row(HEIGHT // 2), dtype=np.int16)
    blank = int((equator == 0).sum())
    print(f"  equator row: {blank} of {equator.size} zero samples")
    ok = ok and blank < equator.size // 100
    reader.close()

    sources = terrain.discover_dem_sources()
    print("  discoverable:", [(p.name, round(km * 1000, 1)) for p, km in sources])
    ok = ok and any(p.resolve() == OUTPUT.resolve() for p, _ in sources)
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify the output only")
    parser.add_argument("--stitch", action="store_true",
                        help="stitch tiles that are already downloaded")
    parser.add_argument("--keep-tiles", action="store_true",
                        help="do not delete the .img tiles after stitching")
    args = parser.parse_args(argv)

    if args.check:
        return check()

    # Room for the output, and -- when downloading -- one tile beside it.
    need = (PIXELS_END + 4096) + (0 if args.stitch else TILE_BYTES)
    free = shutil.disk_usage(ROOT).free
    if free < need:
        print(f"warning: {free / 1e9:.1f} GB free, this wants about "
              f"{need / 1e9:.1f} GB", flush=True)

    if OUTPUT.is_file() and OUTPUT.stat().st_size == expected_size():
        print(f"resuming into {OUTPUT} ({OUTPUT.stat().st_size / 1e9:.1f} GB, "
              "already shaped like the product)")
    else:
        write_header(OUTPUT)
        print(f"preallocated {OUTPUT} ({OUTPUT.stat().st_size / 1e9:.1f} GB)")
    started = time.time()
    done = 0
    for index, (band, row0) in enumerate(BANDS):
        for block in BLOCKS:
            name = tile_name(band, block)
            col0 = output_column(block)
            done += 1
            if tile_is_present(OUTPUT, row0, col0):
                print(f"  {name}: already stitched, skipping", flush=True)
                if not args.stitch:
                    (TILE_DIR / name).unlink(missing_ok=True)
                continue
            if not args.stitch:
                download(name)
            if not (TILE_DIR / name).exists():
                raise SystemExit(f"missing tile {name}")
            stitch_tile(OUTPUT, band, row0, block)
            if not args.keep_tiles and not args.stitch:
                (TILE_DIR / name).unlink(missing_ok=True)
            elapsed = time.time() - started
            left = max(0, 16 - done)
            print(f"  {done}/16 tiles, {elapsed / 60:.0f} min elapsed, "
                  f"about {elapsed / max(done - 3, 1) * left / 60:.0f} min left",
                  flush=True)
    repair_band_seams(OUTPUT)
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
