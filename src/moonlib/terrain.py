"""Global lunar surface terrain: elevation and colour products.

This module owns the *data* half of the observation simulator: it knows where
the global raster products live, how to read them without loading gigabytes
into RAM, how to build and memory-map a downsampled pyramid, and how to sample
height, normal and albedo at arbitrary selenographic coordinates.  Nothing here
touches SPICE, matplotlib or the network.

Products
--------
Both files are NASA SVS "CGI Moon Kit" rasters built from LRO data.

``ldem_<ppd>.tif``
    LOLA global topography, 32-bit float, **kilometres of height above a
    1737.4 km reference sphere** -- not radius, and not the half-metre integer
    encoding used by the PDS product of the same name (SVS divided the int16
    source by 2000).  The shipped file is 64 pixels per degree.  Values are
    quantised to 0.5 m, which is exactly the step :data:`_DEM_QUANTUM_KM`
    stores, so the pyramid is lossless.  LOLA's own vertical accuracy is about
    1 m, but the grid is a smoothed product: LOLA track gaps of 1-4 km were
    filled by interpolation, so its effective horizontal resolution is coarser
    than the 473.8 m pixel size suggests.

``lroc_color.exr``
    LROC WAC global colour mosaic, half-float **linear** RGB, 76 pixels per
    degree.  The companion ``lroc_color_16bit_srgb.tif`` is the same image
    re-encoded as 16-bit sRGB; the linear EXR is preferred because a renderer
    must not have to guess a transfer function, and it is the only one of the
    two that :mod:`moonlib` can read without an image library (it is ZIPS
    compressed, which is raw zlib per scanline).  The three channels are the
    643 nm, 566 nm and 415 nm WAC bands.

    The mosaic is **photometrically normalised**, not a photograph: the Hapke
    incidence/emission/phase dependence was already removed, normalised to
    i = 60 deg, e = 0 deg, g = 60 deg (Sato et al. 2014).  Renderers must drive
    it with their own light rather than dividing shading out of it.  It is also
    not normal albedo, and it is *relative*: SVS adjusted exposure and white
    balance for human viewing, so its absolute level carries no photometric
    meaning (see :data:`RELATIVE_ALBEDO_NOTE`).  Coverage is 70N-70S from LROC;
    the polar caps are filled with a lower-resolution monochromatic LOLA
    albedo map, which is why the poles look grey.

Grid convention
---------------
Both rasters are equirectangular, 2:1, full sphere, row 0 = +90 deg latitude,
and **centred on 0 deg longitude** -- column 0 is 180 deg, the seam runs down
the middle of the far side, and longitude increases eastward::

    lon_deg = (col + 0.5) / width * 360 - 180
    lat_deg = 90 - (row + 0.5) / height * 180

That is the PDS grid rolled by 180 deg, which is why naive use of the PDS
conventions puts every mare on the wrong side of the Moon.  The two rasters
share the frame but not the grid (76 ppd against 64 ppd, ratio 19/16), so they
are sampled independently and never resampled onto each other.

Nyquist note
------------
Both rasters are far larger than any sane render, so a pyramid is baked once
into a memory-mappable cache (see :func:`open_terrain`) and the renderer picks
the level that matches its output pixel footprint, which doubles as
anti-aliasing.
"""

from __future__ import annotations

import json
import math
import os
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from .config import cache_dir as resolve_cache_dir
from .errors import TerrainDataError

__all__ = [
    "MOON_REFERENCE_RADIUS_KM",
    "RELATIVE_ALBEDO_NOTE",
    "DEFAULT_NORMAL_ALBEDO",
    "ENV_TERRAIN_DIR",
    "TOPOGRAPHY_PROBE",
    "TerrainSources",
    "found_sources",
    "find_sources",
    "RasterPyramid",
    "MoonTerrain",
    "open_terrain",
    "load_region_dem",
    "open_region",
    "open_region_at",
    "open_region_box",
    "extract_region",
    "find_region_files",
    "global_dem_sources",
    "terrain_cache_root",
    "bake_terrain_cache",
    "clear_terrain_cache",
    "SurfaceMesh",
]

#: Datum of the LOLA ``ldem`` products: heights are metres/kilometres above
#: this sphere, *not* above an ellipsoid.
MOON_REFERENCE_RADIUS_KM = 1737.4

#: Environment override for the directory holding the global rasters.
ENV_TERRAIN_DIR = "MOONLIB_TERRAIN_DIR"

#: Environment overrides naming the two rasters *by file*.  A directory is the
#: right shape when the products live where the project expects them; a file
#: path is what a reader who downloaded one raster somewhere else actually has,
#: and it saves them from having to invent a directory and rename anything.
ENV_DEM = "MOONLIB_DEM"
ENV_ALBEDO = "MOONLIB_ALBEDO"

#: A directory qualifies as a terrain directory when it holds this file.
TOPOGRAPHY_PROBE = "ldem_64.tif"

#: Disk-averaged normal albedo of the Moon in the visible, the classical value
#: from Lane & Irvine (1973).  Because the mosaic is relative, this is the one
#: number that puts the render into photometric units; the mosaic is divided by
#: its own global mean first, so the parameter really is the disk average.
DEFAULT_NORMAL_ALBEDO = 0.136

RELATIVE_ALBEDO_NOTE = (
    "The SVS/LROC colour mosaic is a relative reflectance product. It is "
    "photometrically normalised (i = 60 deg, e = 0 deg, g = 60 deg) but not "
    "calibrated to normal albedo, and SVS adjusted its exposure and white "
    "balance for human viewing. moonlib therefore divides it by its own global "
    "mean and multiplies by normal_albedo, so that parameter is the "
    "disk-averaged normal albedo. Ratios between features, and between the "
    "sunlit and earthshine-lit hemispheres, do not depend on that choice at "
    "all: they cancel."
)

#: Cache layout version; bumped when the on-disk format changes.  The requested
#: resolution is part of the cache *directory name* (see :func:`_cache_key`), so
#: several resolutions can coexist and none of them is mistaken for another.
#: Bumped whenever the *meaning* of a cache file changes.  2: the topography
#: levels are kilometres of height, converted from the source's own units --
#: before that the USGS 118 m mosaic was quantised without its half-metre scale
#: and baked as saturated int16, so any cache of it is rubbish.
_CACHE_FORMAT = 2

#: Elevation is stored as int16 in units of this many kilometres (0.5 m).
_DEM_QUANTUM_KM = 0.0005

#: Albedo is stored as uint16 covering [0, 1].
_ALBEDO_QUANTUM = 1.0 / 65535.0

ProgressHook = Callable[[float, str], None]


def _noop_progress(fraction: float, message: str) -> None:  # pragma: no cover
    """Default progress hook: silently ignore."""


# ---------------------------------------------------------------------------
# source discovery
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TerrainSources:
    """The raster files backing a :class:`MoonTerrain`."""

    dem_path: Path
    albedo_path: Optional[Path] = None
    albedo_encoding: str = "linear"
    """``"linear"`` for the half-float EXR, ``"srgb"`` for 16-bit sRGB TIFF."""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "dem": str(self.dem_path),
            "albedo": str(self.albedo_path) if self.albedo_path else None,
            "albedo_encoding": self.albedo_encoding,
        }

    def key(self) -> str:
        """Stable identity of this source set, for the cache directory name."""
        parts = []
        for path in (self.dem_path, self.albedo_path):
            if path is None:
                parts.append("none")
                continue
            try:
                stat = path.stat()
                parts.append(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}")
            except OSError:  # pragma: no cover - racing deletion
                parts.append(f"{path.name}:missing")
        parts.append(self.albedo_encoding)
        return "|".join(parts)


def _terrain_search_roots(explicit: Optional[Path]) -> List[Path]:
    roots: List[Path] = []
    if explicit is not None:
        roots.append(Path(explicit).expanduser())
    env_value = os.environ.get(ENV_TERRAIN_DIR)
    if env_value:
        roots.append(Path(env_value).expanduser())
    # A checkout keeps the rasters next to the SPICE kernels; accept both the
    # kernel directory itself and its parent.
    for parent in Path(__file__).resolve().parents:
        roots.append(parent / "kernels")
        roots.append(parent)
        if (parent / "kernels").is_dir():
            break
    roots.append(Path.cwd())
    seen = set()
    unique: List[Path] = []
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _dem_rank(path: Path) -> Tuple[int, int]:
    """Sort key preferring the highest-resolution ``ldem_<ppd>`` file."""
    stem = path.stem
    ppd = 0
    if stem.startswith("ldem_"):
        try:
            ppd = int(stem.split("_", 1)[1])
        except ValueError:
            ppd = 0
    return (ppd, 0 if path.suffix.lower() == ".tif" else 1)


def find_sources(
    *,
    dem: Optional[Path] = None,
    albedo: Optional[Path] = None,
    search_dir: Optional[Path] = None,
) -> TerrainSources:
    """Locate the global elevation and colour rasters.

    Parameters
    ----------
    dem, albedo
        Explicit file paths; when given they must exist.
    search_dir
        Extra directory to probe first.

    Raises
    ------
    TerrainDataError
        When no usable topography raster can be found.
    """
    import glob as _glob

    roots = _terrain_search_roots(search_dir)

    # A path in the environment is a request, not a hint: if it is set and wrong
    # the reader wants to hear about it, not to be quietly handed the 64 ppd
    # raster that happened to be found instead.
    if dem is None:
        dem = os.environ.get(ENV_DEM) or None
    if albedo is None:
        albedo = os.environ.get(ENV_ALBEDO) or None

    if dem is not None:
        dem_path = Path(dem).expanduser()
        if not dem_path.is_file():
            raise TerrainDataError(f"Topography raster not found: {dem_path}")
    else:
        # Whatever is on disk, finest first: the raster's own pixel size decides,
        # not its name.  Matching only ``ldem_*.tif`` used to pick the 64 px/deg
        # product while the 256 px/deg one -- the file whose whole point is to be
        # the best topography available -- sat unread beside it.
        found = global_dem_sources(search_dir)
        if found:
            dem_path = found[0][0]
        else:
            dem_path = None
            candidates: List[Path] = []
            for root in roots:
                candidates.extend(Path(p) for p in _glob.glob(str(root / "ldem_*.tif")))
                candidates.extend(Path(p) for p in _glob.glob(str(root / "ldem_*.tiff")))
                if candidates:
                    break
            if candidates:
                dem_path = sorted(set(candidates), key=_dem_rank)[-1]
    if dem_path is None:
        searched = ", ".join(str(r) for r in roots)
        raise TerrainDataError(
            "Cannot locate a LOLA topography raster (ldem_<ppd>.tif). Searched: "
            f"{searched}. Point moonlib at it with {ENV_DEM}=/path/to/dem.tif, "
            f"{ENV_TERRAIN_DIR}=/path/to/dir, the \"terrain_dir\" configuration "
            "key, or moonlib.open_terrain(dem=...). To fetch the 256 px/deg "
            "global product (8.5 GB) run tools/fetch_lola_global.py."
        )

    if albedo is not None:
        albedo_path: Optional[Path] = Path(albedo).expanduser()
        if not albedo_path.is_file():
            raise TerrainDataError(f"Colour raster not found: {albedo_path}")
        encoding = "srgb" if "srgb" in albedo_path.name.lower() else "linear"
        if albedo_path.suffix.lower() == ".exr":
            encoding = "linear"
        return TerrainSources(dem_path, albedo_path, encoding)

    # Prefer the linear EXR over any sRGB integer encoding of the same mosaic:
    # no transfer function has to be assumed, no dynamic range is clipped, and
    # it can be read scanline by scanline.  The sRGB TIFF is a supported
    # fallback and produces the same numbers (verified to 1e-3 in the test
    # suite), it just goes through Pillow and decodes in one pass.
    albedo_path = None
    encoding = "linear"
    for root in roots:
        for pattern, enc in (("lroc_color.exr", "linear"),
                             ("lroc_color*.tif", "srgb"),
                             ("lroc_color*.tiff", "srgb"),
                             ("*color*.exr", "linear"),
                             ("*color*.tif", "srgb")):
            hits = sorted(Path(p) for p in _glob.glob(str(root / pattern)))
            if hits:
                albedo_path = hits[0]
                encoding = enc
                break
        if albedo_path is not None:
            break
    return TerrainSources(dem_path, albedo_path, encoding)


def terrain_cache_root(base: Optional[Path] = None) -> Path:
    """Directory holding the terrain pyramids, ``<base cache>/terrain``.

    Everything that takes a *cache* argument -- :func:`open_terrain`,
    :func:`bake_terrain_cache`, :func:`clear_terrain_cache` -- accepts this
    value directly, so the location can be computed once and passed around
    without nesting it twice.
    """
    return resolve_cache_dir(base) / "terrain"


# ---------------------------------------------------------------------------
# raster readers
# ---------------------------------------------------------------------------
_TSIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8,
          11: 4, 12: 8, 16: 8, 17: 8, 18: 8}
_TFMT = {1: "B", 2: "c", 3: "H", 4: "I", 5: "II", 6: "b", 7: "B",
         8: "h", 9: "i", 10: "q", 11: "f", 12: "d", 16: "Q", 17: "q", 18: "Q"}


class _StrippedTiff:
    """Row-addressable reader for TIFFs stored one row per strip.

    The SVS rasters are written by IDL with ``RowsPerStrip = 1``, which makes
    every scanline independently addressable.  That is what allows a windowed
    read of a 1 GB file without decoding it.

    Both classic TIFF and BigTIFF are accepted: the USGS global LOLA mosaic is
    a BigTIFF, and its layout is otherwise exactly what this class wants.

    Compressed strips are refused by default: LZW in pure Python decodes at
    roughly 0.1 MB/s, which would turn a one-time bake into hours, so the
    colour mosaic goes through Pillow instead (see
    :func:`_downsample_compressed_tiff`).  ``allow_compressed`` exists only so
    that :func:`_tiff_layout` can read the *header* of such a file and report
    its geometry.
    """

    def __init__(self, path: Path, *, allow_compressed: bool = False):
        self.path = Path(path)
        try:
            self._handle = open(self.path, "rb")
        except OSError as exc:
            raise TerrainDataError(f"Cannot open {self.path}: {exc}") from exc
        header = self._handle.read(2)
        self.little = header == b"II"
        if header not in (b"II", b"MM"):
            raise TerrainDataError(f"{self.path} is not a TIFF (bad byte order mark).")
        self.big = self._read_u16(2) == 43
        if self.big:
            # Bytes 4-5 hold the offset size; 6-7 are reserved and must be zero,
            # which nothing here depends on, so they are read for the seek below.
            offset_size = self._read_u16(4)
            if offset_size != 8:
                raise TerrainDataError(
                    f"{self.path}: BigTIFF with {offset_size}-byte offsets is not "
                    "supported."
                )
            self._handle.seek(8)
            offset = self._read_u64()
        else:
            self._handle.seek(4)
            offset = self._read_u32()
        tags = self._read_ifd(offset)
        try:
            self.width = self._tag(tags, 256)[0]
            self.height = self._tag(tags, 257)[0]
            self.bits = self._tag(tags, 258)[0]
            self.compression = self._tag(tags, 259)[0]
            self.samples = self._tag(tags, 277)[0]
            rows_per_strip = self._tag(tags, 278)[0]
            self.offsets = self._tag(tags, 273)
            self.counts = self._tag(tags, 279)
            self.sample_format = self._tag(tags, 339)[0] if 339 in tags else 1
        except KeyError as exc:  # pragma: no cover - malformed file
            raise TerrainDataError(f"{self.path}: missing TIFF tag {exc}.") from exc
        # GDAL records "this sample means nothing" in the ASCII tag 42113.
        self.nodata = _parse_nodata(tags.get(42113))
        self.rows_per_strip = max(1, rows_per_strip)
        if self.compression != 1 and not allow_compressed:
            raise TerrainDataError(
                f"{self.path} uses TIFF compression {self.compression}; only "
                "uncompressed strips can be read row by row. Use the linear "
                "lroc_color.exr, or let the colour pyramid go through Pillow "
                "with ``pip install 'moonlib[tiff]'``."
            )
        if self.sample_format == 3 and self.bits == 32:
            self.dtype = np.dtype("<f4" if self.little else ">f4")
        elif self.bits == 16 and self.sample_format == 2:
            self.dtype = np.dtype("<i2" if self.little else ">i2")
        elif self.bits == 16:
            self.dtype = np.dtype("<u2" if self.little else ">u2")
        elif self.bits == 8:
            self.dtype = np.dtype("u1")
        else:
            raise TerrainDataError(
                f"{self.path}: unsupported sample layout "
                f"(bits={self.bits}, sample_format={self.sample_format})."
            )
        self.row_bytes = self.width * self.samples * (self.bits // 8)
        if self.compression == 1:
            self._raw = np.memmap(str(self.path), dtype=np.uint8, mode="r")
        else:
            self._raw = None

    def close(self) -> None:
        """Release the header handle, and the memory map where there is one."""
        try:
            self._handle.close()
        except OSError:  # pragma: no cover - defensive
            pass
        raw = getattr(self, "_raw", None)
        if raw is not None:
            del raw
            self._raw = None

    # -- low level tag plumbing ------------------------------------------
    @property
    def _endian(self) -> str:
        return "<" if self.little else ">"

    def _read_u16(self, offset: int) -> int:
        self._handle.seek(offset)
        return struct.unpack(self._endian + "H", self._handle.read(2))[0]

    def _read_u32(self) -> int:
        return struct.unpack(self._endian + "I", self._handle.read(4))[0]

    def _read_u64(self) -> int:
        return struct.unpack(self._endian + "Q", self._handle.read(8))[0]

    def _read_ifd(self, offset: int) -> Dict[int, Tuple[int, int, bytes]]:
        """Read an image file directory from either TIFF flavour.

        Classic entries are 12 bytes with a 4-byte value field; BigTIFF entries
        are 20 bytes with 8, which is the only structural difference that
        matters here.
        """
        handle = self._handle
        handle.seek(offset)
        endian = self._endian
        if self.big:
            count = struct.unpack(endian + "Q", handle.read(8))[0]
            entry, field = 20, 8
            head = endian + "HHQ"
        else:
            count = struct.unpack(endian + "H", handle.read(2))[0]
            entry, field = 12, 4
            head = endian + "HHI"
        blob = handle.read(entry * count)
        tags: Dict[int, Tuple[int, int, bytes]] = {}
        for index in range(count):
            chunk = blob[entry * index: entry * (index + 1)]
            tag, kind, length = struct.unpack(head, chunk[: 4 + field])
            tags[tag] = (kind, length, chunk[4 + field: entry])
        return tags

    def _tag(self, tags: Dict[int, Tuple[int, int, bytes]], tag: int) -> Tuple[int, ...]:
        kind, count, value = tags[tag]
        size = _TSIZE[kind]
        inline = 8 if self.big else 4
        if count * size <= inline:
            data = value[:count * size]
        else:
            code = self._endian + ("Q" if self.big else "I")
            offset = struct.unpack(code, value[: inline])[0]
            position = self._handle.tell()
            self._handle.seek(offset)
            data = self._handle.read(count * size)
            self._handle.seek(position)
        return struct.unpack(self._endian + "%d%s" % (count, _TFMT[kind]), data)

    # -- public ----------------------------------------------------------
    def row(self, index: int) -> np.ndarray:
        """Return scanline *index* as an array of shape ``(width, samples)``."""
        if not 0 <= index < self.height:
            raise TerrainDataError(f"row {index} outside 0..{self.height - 1}.")
        strip = index // self.rows_per_strip
        offset = self.offsets[strip]
        start = offset + (index % self.rows_per_strip) * self.row_bytes
        raw = self._raw[start:start + self.row_bytes]
        values = np.frombuffer(raw.tobytes(), dtype=self.dtype)
        if self.samples > 1:
            return values.reshape(self.width, self.samples)
        return values


_EXR_PIXEL_HALF = 1
_EXR_PIXEL_FLOAT = 2
_EXR_NO_COMPRESSION = 0
_EXR_RLE = 1
_EXR_ZIPS = 2
_EXR_ZIP = 3
_EXR_LINES_PER_BLOCK = {_EXR_NO_COMPRESSION: 1, _EXR_RLE: 1, _EXR_ZIPS: 1, _EXR_ZIP: 16}


class _ScanlineExr:
    """Minimal scanline OpenEXR reader (no compression, RLE, ZIPS and ZIP).

    The SVS colour mosaic is ZIPS-coded half-float, i.e. one zlib stream per
    scanline, which makes it random-access and cheap to decode on demand.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self._handle = open(self.path, "rb")
        except OSError as exc:
            raise TerrainDataError(f"Cannot open {self.path}: {exc}") from exc
        magic, _version = struct.unpack("<II", self._handle.read(8))
        if magic != 0x01312F76:
            raise TerrainDataError(f"{self.path} is not an OpenEXR file.")
        attributes = self._read_attributes()
        try:
            window = struct.unpack("<4i", attributes["dataWindow"][1])
            self.compression = struct.unpack("<B", attributes["compression"][1][:1])[0]
            channels = self._parse_channels(attributes["channels"][1])
        except KeyError as exc:
            raise TerrainDataError(f"{self.path}: missing EXR attribute {exc}.") from exc
        self.x_min, self.y_min, self.x_max, self.y_max = window
        self.width = self.x_max - self.x_min + 1
        self.height = self.y_max - self.y_min + 1
        if self.compression not in _EXR_LINES_PER_BLOCK:
            raise TerrainDataError(
                f"{self.path}: EXR compression {self.compression} is not supported "
                "(only none/RLE/ZIPS/ZIP scanline images are)."
            )
        self.lines_per_block = _EXR_LINES_PER_BLOCK[self.compression]
        # Channel names come back in file order and the payload is planar per
        # scanline, so remember the mapping to canonical R, G, B.
        self.channel_names = [name for name, _kind in channels]
        self._kinds = [kind for _name, kind in channels]
        self.offsets = np.frombuffer(self._handle.read(self.height * 8), dtype="<u8")
        self._rgb_index = self._resolve_rgb_order()

    def _read_attributes(self) -> Dict[str, Tuple[str, bytes]]:
        attributes: Dict[str, Tuple[str, bytes]] = {}
        while True:
            name = self._read_cstring()
            if not name:
                break
            kind = self._read_cstring()
            size = struct.unpack("<I", self._handle.read(4))[0]
            attributes[name] = (kind, self._handle.read(size))
        return attributes

    def _read_cstring(self) -> str:
        collected = bytearray()
        while True:
            char = self._handle.read(1)
            if char in (b"\x00", b""):
                break
            collected += char
        return collected.decode("ascii", "replace")

    @staticmethod
    def _parse_channels(blob: bytes) -> List[Tuple[str, int]]:
        channels: List[Tuple[str, int]] = []
        index = 0
        while index < len(blob) and blob[index] != 0:
            end = blob.index(b"\x00", index)
            name = blob[index:end].decode("ascii", "replace")
            kind = struct.unpack("<i", blob[end + 1:end + 5])[0]
            channels.append((name, kind))
            index = end + 1 + 16
        if not channels:
            raise TerrainDataError("EXR channel list is empty.")
        return channels

    def _resolve_rgb_order(self) -> List[int]:
        """Indices of R, G, B inside the file's channel order."""
        names = self.channel_names
        if all(name in names for name in ("R", "G", "B")):
            return [names.index("R"), names.index("G"), names.index("B")]
        if len(names) == 1:
            return [0, 0, 0]
        if len(names) >= 3:
            return [2, 1, 0]
        raise TerrainDataError(
            f"{self.path}: cannot map channels {names!r} onto RGB."
        )

    def block(self, index: int) -> Tuple[int, np.ndarray]:
        """Decode scanline block *index*.

        Returns ``(first_row, data)`` where *data* has shape
        ``(n_lines, n_channels, width)`` in file channel order.
        """
        offset = int(self.offsets[index])
        self._handle.seek(offset)
        row, size = struct.unpack("<ii", self._handle.read(8))
        payload = self._handle.read(size)
        n_lines = min(self.lines_per_block, self.height - row)
        n_channels = len(self.channel_names)
        expected = self.width * n_channels * n_lines * 2
        if self.compression == _EXR_NO_COMPRESSION:
            data = np.frombuffer(payload, dtype=np.uint8)
        else:
            data = self._decompress(payload, expected)
        if data.size != expected:
            raise TerrainDataError(
                f"{self.path}: block {index} decoded to {data.size} bytes, "
                f"expected {expected}."
            )
        half = data.view("<f2").reshape(n_lines, n_channels, self.width)
        return row, half

    def _decompress(self, payload: bytes, expected: int) -> np.ndarray:
        if self.compression == _EXR_RLE:
            raw = _exr_rle_decode(payload, expected)
        else:
            try:
                raw = np.frombuffer(zlib.decompress(payload), dtype=np.uint8)
            except zlib.error as exc:
                raise TerrainDataError(
                    f"{self.path}: corrupt compressed block ({exc})."
                ) from exc
        return _exr_postprocess(raw, expected)

    def row_rgb(self, index: int) -> np.ndarray:
        """Return scanline *index* as linear RGB, shape ``(width, 3)`` float32."""
        block_index = index // self.lines_per_block
        row, data = self.block(block_index)
        line = data[index - row]
        if self.channel_names == ["Y"]:  # pragma: no cover - greyscale EXR
            grey = line[0]
            return np.repeat(grey[:, None], 3, axis=1)
        order = self._rgb_index
        return np.stack([line[order[0]], line[order[1]], line[order[2]]], axis=1)


def _exr_postprocess(raw: np.ndarray, expected: int) -> np.ndarray:
    """Undo the EXR ZIP predictor and the two-half interleave."""
    values = np.frombuffer(raw.tobytes(), dtype=np.uint8).astype(np.int64)
    if values.size != expected:  # pragma: no cover - defensive
        raise TerrainDataError(
            f"EXR block size mismatch: {values.size} != {expected}."
        )
    predicted = np.empty_like(values)
    predicted[0] = values[0]
    if values.size > 1:
        predicted[1:] = values[1:] - 128
    reconstructed = (np.cumsum(predicted) & 0xFF).astype(np.uint8)
    half = (expected + 1) // 2
    out = np.empty(expected, dtype=np.uint8)
    out[0::2] = reconstructed[:half]
    out[1::2] = reconstructed[half:half + (expected - half)]
    return out


def _exr_rle_decode(payload: bytes, expected: int) -> np.ndarray:
    """Undo EXR's byte-oriented run-length encoding."""
    out = bytearray()
    index = 0
    length = len(payload)
    while index < length:
        count = struct.unpack("<b", payload[index:index + 1])[0]
        index += 1
        if count < 0:
            run = -count
            out += payload[index:index + run]
            index += run
        elif count == 0:  # pragma: no cover - malformed stream
            break
        else:
            out += payload[index:index + 1] * (count + 1)
            index += 1
    return np.frombuffer(bytes(out), dtype=np.uint8)


# ---------------------------------------------------------------------------
# python sRGB transfer (only used when an sRGB integer product is supplied)
# ---------------------------------------------------------------------------
def srgb_to_linear(values: np.ndarray) -> np.ndarray:
    """Convert IEC 61966-2-1 encoded values in ``[0, 1]`` to linear light."""
    # Evaluated in double precision: the exponent 2.4 amplifies the rounding
    # error of a float32 base enough to show up in a bit-exact comparison.
    data = np.asarray(values, dtype=np.float64)
    low = data / 12.92
    high = np.power((np.maximum(data, 0.0) + 0.055) / 1.055, 2.4)
    return np.where(data <= 0.04045, low, high).astype(np.float32)


# ---------------------------------------------------------------------------
# pyramid
# ---------------------------------------------------------------------------
@dataclass
class RasterPyramid:
    """A global equirectangular raster plus its successive half-resolution levels.

    Level 0 is the finest *cached* level, not necessarily the source
    resolution: a pyramid baked from ``ldem_64.tif`` starts at 11520x5760 so
    that the cache stays a few hundred megabytes instead of a few gigabytes.

    Sampling uses pixel-centre coordinates.  A *global* raster follows the SVS
    convention and wraps in longitude::

        lon_deg = (col + 0.5) / W * 360 - 180
        lat_deg = 90 - (row + 0.5) / H * 180

    A *regional* raster sets :attr:`bounds` and is instead laid out inside that
    box, with sampling clamped at the edges rather than wrapped::

        lon_deg = lon_min + (col + 0.5) * dlon
        lat_deg = lat_max - (row + 0.5) * dlat

    Regional rasters are how a high-resolution DEM patch (SLDEM2015, the USGS
    256 px/deg LOLA mosaic, an LROC DTM) is layered under a close-up without
    disturbing the global products.
    """

    name: str
    levels: List[Any]
    label: str = ""
    unit: str = ""
    scale: float = 1.0
    offset: float = 0.0
    relative_mean: float = 1.0
    value_range: Optional[Tuple[float, float]] = None
    """``(min, max)`` of level 0 in *stored* units, when it is known.

    The renderer needs the global height range before it can march a ray (the
    shell it steps through is bounded by it), and measuring it means touching
    every sample of level 0 -- 2.1 GB and two minutes on the USGS 256 px/deg
    product.  The bake knows the answer already, so it records it; ``None``
    means "measure it", which is what a pyramid built in memory does.
    """
    bounds: Optional[Tuple[float, float, float, float]] = None
    """``(lon_min, lon_max, lat_min, lat_max)`` in degrees for a regional grid.

    ``None`` means a full-sphere equirectangular raster that wraps in
    longitude.
    """
    """Area-weighted global mean of the stored values, in stored units.

    For the colour mosaic this is what makes ``normal_albedo`` mean the disk
    average instead of an arbitrary scale factor; for the topography it is a
    plain diagnostic.
    """

    def __post_init__(self) -> None:
        if not self.levels:
            raise TerrainDataError(f"{self.name}: pyramid has no levels.")
        if self.bounds is not None:
            lon_min, lon_max, lat_min, lat_max = self.bounds
            if not (lon_max > lon_min and lat_max > lat_min):
                raise TerrainDataError(
                    f"{self.name}: degenerate bounds {self.bounds}."
                )
            if lat_min < -90.0 or lat_max > 90.0:
                raise TerrainDataError(
                    f"{self.name}: latitude bounds {self.bounds} leave the globe."
                )

    # -- geometry --------------------------------------------------------
    @property
    def base_shape(self) -> Tuple[int, int]:
        return (int(self.levels[0].shape[0]), int(self.levels[0].shape[1]))

    @property
    def n_levels(self) -> int:
        return len(self.levels)

    @property
    def is_regional(self) -> bool:
        return self.bounds is not None

    @property
    def pixel_degrees(self) -> float:
        """Level-0 pixel spacing in degrees (isotropic for a regional grid)."""
        if self.bounds is None:
            return 360.0 / self.base_shape[1]
        lon_min, lon_max, _lat_min, _lat_max = self.bounds
        return (lon_max - lon_min) / self.base_shape[1]

    @property
    def span_km(self) -> Tuple[float, float]:
        """Ground extent of the level-0 grid: ``(east_west, north_south)`` km."""
        height, width = self.base_shape
        if self.bounds is None:
            return (
                math.radians(360.0) * MOON_REFERENCE_RADIUS_KM,
                math.radians(180.0) * MOON_REFERENCE_RADIUS_KM,
            )
        lon_min, lon_max, lat_min, lat_max = self.bounds
        return (
            math.radians(lon_max - lon_min) * MOON_REFERENCE_RADIUS_KM,
            math.radians(lat_max - lat_min) * MOON_REFERENCE_RADIUS_KM,
        )

    @property
    def shape(self) -> Tuple[int, int, int]:
        """Shape of the finest level, channels last (1 for single band)."""
        level = self.levels[0]
        return level.shape if level.ndim == 3 else level.shape + (1,)

    def level(self, index: int) -> Any:
        """Array of pyramid level *index*, clamped into range."""
        clamped = max(0, min(int(index), self.n_levels - 1))
        return self.levels[clamped]

    def grid_shape(self, index: int) -> Tuple[int, int]:
        level = self.level(index)
        return (int(level.shape[0]), int(level.shape[1]))

    def degrees_per_pixel(self, index: int) -> float:
        return self.pixel_degrees * (2 ** max(0, int(index)))

    def km_per_pixel(self, index: int, radius_km: float = MOON_REFERENCE_RADIUS_KM) -> float:
        """Equatorial ground sample distance of level *index*, in kilometres."""
        return math.radians(self.degrees_per_pixel(index)) * radius_km

    def level_for_ground_scale(self, km_per_pixel: float) -> int:
        """Level whose pixels best match a requested ground sample distance.

        Picking the level that *matches* the output footprint is what keeps a
        512-pixel image of the whole Moon from aliasing: at that size one pixel
        already spans about 7 km, so sampling the 0.95 km level would only add
        sparkle.  Coarser requests than the pyramid can serve clamp to the
        coarsest level, finer ones to level 0.
        """
        if km_per_pixel <= 0.0:
            return 0
        base_km = self.km_per_pixel(0)
        if base_km <= 0.0:  # pragma: no cover - defensive
            return 0
        index = int(round(math.log2(max(km_per_pixel, 1e-9) / base_km)))
        return max(0, min(index, self.n_levels - 1))

    # -- sampling --------------------------------------------------------
    def _pixel_lon(self, lon: np.ndarray, width: int, level: int) -> np.ndarray:
        """Fractional column index of a longitude on this grid."""
        if self.bounds is None:
            x = (lon + 180.0) * (width / 360.0) - 0.5
            base = np.floor(x).astype(np.int64)
            return np.mod(base, width) + (x - np.floor(x))
        lon_min, lon_max, _lat_min, _lat_max = self.bounds
        dlon = (lon_max - lon_min) / width
        return np.clip((lon - lon_min) / dlon - 0.5, 0.0, width - 1.0)

    def _pixel_lat(self, lat: np.ndarray, height: int) -> np.ndarray:
        """Fractional row index of a latitude on this grid."""
        if self.bounds is None:
            return np.clip((90.0 - lat) * (height / 180.0) - 0.5, 0.0, height - 1.0)
        _lon_min, _lon_max, lat_min, lat_max = self.bounds
        dlat = (lat_max - lat_min) / height
        return np.clip((lat_max - lat) / dlat - 0.5, 0.0, height - 1.0)

    def sample(self, lon_deg: Any, lat_deg: Any, level: int = 0) -> np.ndarray:
        """Bilinearly sample the raster.

        A global raster wraps in longitude and clamps in latitude; a regional
        raster clamps in both.  Returns shape ``query_shape`` for a single-band
        raster and ``query_shape + (channels,)`` otherwise, always ``float32``.
        """
        array = self.level(level)
        height, width = array.shape[0], array.shape[1]
        lon = np.asarray(lon_deg, dtype=np.float64)
        lat = np.asarray(lat_deg, dtype=np.float64)
        x = self._pixel_lon(lon, width, level)
        y = self._pixel_lat(lat, height)
        x0 = np.floor(x).astype(np.int64)
        frac_x = (x - x0).astype(np.float32)
        x0 = np.mod(x0, width) if self.bounds is None else np.clip(x0, 0, width - 1)
        x1 = np.mod(x0 + 1, width) if self.bounds is None else np.minimum(x0 + 1, width - 1)
        y0 = np.clip(np.floor(y).astype(np.int64), 0, height - 1)
        frac_y = (y - y0).astype(np.float32)
        y1 = np.minimum(y0 + 1, height - 1)

        v00 = np.asarray(array[y0, x0], dtype=np.float32)
        v01 = np.asarray(array[y0, x1], dtype=np.float32)
        v10 = np.asarray(array[y1, x0], dtype=np.float32)
        v11 = np.asarray(array[y1, x1], dtype=np.float32)
        if v00.ndim > frac_x.ndim:
            frac_x = frac_x[..., None]
            frac_y = frac_y[..., None]
        top = v00 + (v01 - v00) * frac_x
        bottom = v10 + (v11 - v10) * frac_x
        return (top + (bottom - top) * frac_y) * self.scale + self.offset

    def slopes(
        self,
        lon_deg: Any,
        lat_deg: Any,
        level: int = 0,
        radius_km: float = MOON_REFERENCE_RADIUS_KM,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Dimensionless physical surface slopes ``(east, north)``.

        Both components are measured over a baseline of exactly one *equatorial*
        pixel of the level, which is what makes them comparable: a naive
        central difference in longitude would shrink towards zero metres per
        pixel at the poles, so that DEM quantisation alone produced kilometre
        per kilometre slopes and the polar normals pointed sideways.  Widening
        the longitude offset by ``1 / cos(lat)`` keeps the ground baseline
        constant, and the ``cos(lat)`` in the denominator then cancels out.
        """
        height, width = self.grid_shape(level)
        pixel_deg = self.degrees_per_pixel(level)
        d_lon_rad = math.radians(pixel_deg)
        d_lat_rad = math.radians(pixel_deg)
        baseline_east = radius_km * d_lon_rad
        baseline_north = radius_km * d_lat_rad

        lon = np.asarray(lon_deg, dtype=np.float64)
        lat = np.asarray(lat_deg, dtype=np.float64)
        cos_lat = np.abs(np.cos(np.radians(lat)))

        # Longitude offset that keeps the ground baseline at one pixel.
        d_lon_eff = np.minimum(d_lon_rad / np.maximum(cos_lat, 1e-12), 0.5 * math.pi)
        east_distance = np.maximum(radius_km * cos_lat * d_lon_eff, 0.5 * baseline_east)
        east = self.sample(lon + np.degrees(d_lon_eff), lat, level)
        west = self.sample(lon - np.degrees(d_lon_eff), lat, level)
        north = self.sample(lon, np.clip(lat + math.degrees(d_lat_rad), -90.0, 90.0), level)
        south = self.sample(lon, np.clip(lat - math.degrees(d_lat_rad), -90.0, 90.0), level)
        slope_east = (east - west) / (2.0 * east_distance)
        slope_north = (north - south) / (2.0 * baseline_north)
        return slope_east, slope_north

    # -- introspection ---------------------------------------------------
    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "unit": self.unit,
            "level0_shape": list(self.base_shape),
            "n_levels": self.n_levels,
            "level0_degrees_per_pixel": self.degrees_per_pixel(0),
            "level0_km_per_pixel": self.km_per_pixel(0),
            "scale": self.scale,
            "offset": self.offset,
            "relative_mean": self.relative_mean,
        }


# ---------------------------------------------------------------------------
# cache baking / loading
# ---------------------------------------------------------------------------
def _ensure_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TerrainDataError(f"Cannot create cache directory {path}: {exc}") from exc


def _dem_source_level(sources: TerrainSources, detail: int) -> Tuple[int, int, int, int]:
    """Return ``(base_level, width, height, source_width, source_height)``."""
    if sources.dem_path.suffix.lower() in (".tif", ".tiff"):
        reader = _StrippedTiff(sources.dem_path)
        width, height = reader.width, reader.height
    else:  # pragma: no cover - only TIFF topography is shipped
        raise TerrainDataError(
            f"Unsupported topography format {sources.dem_path.suffix!r}; "
            "expected a stripped .tif."
        )
    return detail, width, height


def _downsample_band_stream(
    row_source: Callable[[int], np.ndarray],
    source_height: int,
    factor: int,
) -> np.ndarray:
    """Box-reduce a full-resolution raster by *factor* in both axes.

    Rows are pulled one at a time through *row_source* so that a 1 GB source
    never has to be resident; only ``factor`` scanlines plus the output are
    held at once.
    """
    if source_height % factor:
        raise TerrainDataError(
            f"Source height {source_height} is not divisible by {factor}."
        )
    if factor == 1:
        return np.stack(
            [np.asarray(row_source(index), dtype=np.float32) for index in range(source_height)],
            axis=0,
        )
    reduced_rows: List[np.ndarray] = []
    pending: List[np.ndarray] = []
    for index in range(source_height):
        pending.append(np.asarray(row_source(index), dtype=np.float32))
        if len(pending) == factor:
            stacked = np.stack(pending, axis=0)          # (factor, width[, channels])
            width = stacked.shape[1]
            if width % factor:
                raise TerrainDataError(
                    f"Source width {width} is not divisible by {factor}."
                )
            if stacked.ndim == 3:
                blocks = stacked.reshape(factor, width // factor, factor, stacked.shape[2])
            else:
                blocks = stacked.reshape(factor, width // factor, factor)
            reduced_rows.append(blocks.mean(axis=(0, 2)).astype(np.float32, copy=False))
            pending = []
    return np.stack(reduced_rows, axis=0)


def _build_pyramid_levels(base: np.ndarray, extra_levels: int) -> List[np.ndarray]:
    """Successive 2x2 box reductions of *base*."""
    levels = [base]
    current = base
    for _ in range(extra_levels):
        height, width = current.shape[0], current.shape[1]
        if height < 2 or width < 2:
            break
        even_h = height - (height % 2)
        even_w = width - (width % 2)
        trimmed = current[:even_h, :even_w]
        if trimmed.ndim == 3:
            reduced = trimmed.reshape(even_h // 2, 2, even_w // 2, 2, trimmed.shape[2]).mean(axis=(1, 3))
        else:
            reduced = trimmed.reshape(even_h // 2, 2, even_w // 2, 2).mean(axis=(1, 3))
        reduced = reduced.astype(np.float32, copy=False)
        levels.append(reduced)
        current = reduced
    return levels


def _area_weighted_mean(level: np.ndarray) -> float:
    """Mean over the sphere, weighting each row by its true surface area."""
    height = level.shape[0]
    latitudes = np.radians(90.0 - (np.arange(height) + 0.5) * (180.0 / height))
    weights = np.cos(latitudes)
    data = np.asarray(level, dtype=np.float64)
    channels = 1
    if data.ndim == 3:
        weights = weights[:, None, None]
        channels = data.shape[2]
    else:
        weights = weights[:, None]
    total = float(weights.sum()) * data.shape[1] * channels
    return float((data * weights).sum() / total)


def _pyramid_depth(width: int, height: int, minimum: int = 8) -> int:
    depth = 0
    while width // (2 ** (depth + 1)) >= minimum and height // (2 ** (depth + 1)) >= minimum:
        depth += 1
    return depth


def _encode_dem(level: np.ndarray) -> np.ndarray:
    quantised = np.rint(np.asarray(level, dtype=np.float64) / _DEM_QUANTUM_KM)
    return np.clip(quantised, -32768.0, 32767.0).astype(np.int16)


def _encode_albedo(level: np.ndarray) -> np.ndarray:
    quantised = np.rint(np.asarray(level, dtype=np.float64) / _ALBEDO_QUANTUM)
    return np.clip(quantised, 0.0, 65535.0).astype(np.uint16)


@dataclass
class _Manifest:
    payload: Dict[str, Any] = field(default_factory=dict)

    def write(self, path: Path) -> None:
        _ensure_dir(path.parent)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(self.payload, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)


def bake_terrain_cache(
    sources: Optional[TerrainSources] = None,
    *,
    cache: Optional[Path] = None,
    detail: int = 1,
    albedo_detail: int = 2,
    include_albedo: bool = True,
    rebuild: bool = False,
    progress: Optional[ProgressHook] = None,
) -> Path:
    """Bake (or reuse) the terrain pyramid cache and return its directory.

    Parameters
    ----------
    sources
        Raster files; discovered with :func:`find_sources` when omitted.
    cache
        Terrain cache directory; defaults to :func:`terrain_cache_root`.
    detail, albedo_detail
        ``log2`` of the decimation applied to the source before it becomes
        pyramid level 0.  ``detail=1`` keeps the DEM at 11520x5760
        (~132 MB) and ``albedo_detail=2`` keeps the colour mosaic at
        6840x3420 (~140 MB).
    include_albedo
        Set ``False`` to bake topography only.
    rebuild
        Ignore an existing, matching cache.
    progress
        ``callable(fraction, message)`` for long bakes.
    """
    hook = progress or _noop_progress
    if sources is None:
        sources = find_sources()
    include_albedo = bool(include_albedo) and sources.albedo_path is not None
    root = Path(cache) if cache is not None else terrain_cache_root()
    request = {
        "dem_detail": int(detail),
        "albedo_detail": int(albedo_detail),
        "include_albedo": include_albedo,
    }
    target = root / _cache_key(sources, detail, albedo_detail, include_albedo)
    manifest_path = target / "manifest.json"

    if not rebuild and manifest_path.is_file():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # pragma: no cover - corrupt cache
            existing = None
        if (
            existing
            and existing.get("format") == _CACHE_FORMAT
            and existing.get("source", {}).get("key") == sources.key()
            and existing.get("request") == request
        ):
            hook(1.0, "cache hit")
            return target

    _ensure_dir(target)
    hook(0.0, "reading topography")
    payload: Dict[str, Any] = {
        "format": _CACHE_FORMAT,
        "request": request,
        "source": {
            "key": sources.key(),
            "dem": _fingerprint(sources.dem_path),
            "albedo": _fingerprint(sources.albedo_path),
            "albedo_encoding": sources.albedo_encoding,
        },
        "reference_radius_km": MOON_REFERENCE_RADIUS_KM,
        "relative_albedo_note": RELATIVE_ALBEDO_NOTE,
    }

    dem_reader = _StrippedTiff(sources.dem_path)
    factor = 2 ** max(0, int(detail))
    if dem_reader.width % factor or dem_reader.height % factor:
        raise TerrainDataError(
            f"{sources.dem_path.name}: {dem_reader.width}x{dem_reader.height} "
            f"is not divisible by {factor}."
        )
    source_width, source_height = dem_reader.width, dem_reader.height

    # The stored samples are not kilometres: the USGS mosaic is int16
    # half-metres.  ``_encode_dem`` quantises level 0 to ``_DEM_QUANTUM_KM``, so
    # a row that arrives in the file's own units is multiplied by 2000 and
    # saturates to +-32767 -- a cache that looks plausible and is 99.9% wrong.
    # The window path has always scaled (_dem_scale in extract_region); this is
    # the same conversion for the pyramid.
    dem_scale = _dem_scale(dem_reader, sources.dem_path)
    sentinel = None if dem_reader.nodata is None else dem_reader.nodata * dem_scale

    def dem_rows(index: int) -> np.ndarray:
        values = np.asarray(dem_reader.row(index), dtype=np.float32) * dem_scale
        if sentinel is not None:
            values = _fill_nodata(values, sentinel)
        return values

    coarse = _downsample_band_stream(dem_rows, source_height, factor)
    dem_levels = _build_pyramid_levels(
        coarse, _pyramid_depth(coarse.shape[1], coarse.shape[0])
    )
    del coarse
    payload["dem"] = {
        "dtype": "int16",
        "unit": "km",
        "quantum": _DEM_QUANTUM_KM,
        "reference_radius_km": MOON_REFERENCE_RADIUS_KM,
        "source_detail": int(detail),
        "source_shape": [source_height, source_width],
        "levels": [
            _level_entry(index, level, _encode_dem)
            for index, level in enumerate(dem_levels)
        ],
    }
    _write_levels(target / "dem", dem_levels, encoder=_encode_dem)
    del dem_levels
    hook(0.55, "reading colour mosaic")

    if include_albedo and sources.albedo_path is not None:
        albedo_levels = _bake_albedo_levels(sources, albedo_detail, hook)
        summary_level = albedo_levels[min(3, len(albedo_levels) - 1)]
        payload["albedo"] = {
            "dtype": "uint16",
            "unit": "relative reflectance",
            "quantum": _ALBEDO_QUANTUM,
            "relative_mean": _area_weighted_mean(summary_level),
            "encoding": sources.albedo_encoding,
            "source_detail": int(albedo_detail),
            "source_shape": [
                albedo_levels[0].shape[0] * factor,
                albedo_levels[0].shape[1] * factor,
            ],
            "levels": [
                _level_entry(index, level, _encode_albedo)
                for index, level in enumerate(albedo_levels)
            ],
        }
        _write_levels(target / "albedo", albedo_levels, encoder=_encode_albedo)
        del albedo_levels, summary_level

    hook(0.95, "writing manifest")
    _Manifest(payload).write(manifest_path)
    hook(1.0, "cache ready")
    return target


def _pillow_module() -> Any:
    """Import Pillow, or explain what to install.

    Pillow is only needed for one thing: reading a *compressed* colour TIFF.
    The linear EXR -- which is the better product for rendering anyway -- is
    read directly, and so is any uncompressed TIFF.  Pillow arrives for free
    with ``moonlib[plot]`` because matplotlib depends on it.
    """
    try:
        from PIL import Image  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise TerrainDataError(
            "This colour mosaic is a compressed TIFF, which needs Pillow to "
            "decode. Install it with: pip install 'moonlib[tiff]'. "
            "Alternatively read the linear lroc_color.exr, which moonlib "
            "decodes itself, or convert the TIFF to an uncompressed one."
        ) from exc
    Image.MAX_IMAGE_PIXELS = None
    return Image


def _downsample_compressed_tiff(
    path: Path, factor: int, hook: ProgressHook
) -> np.ndarray:
    """Decode a compressed colour TIFF with Pillow and box-reduce it.

    Pillow hands back 8 bits per channel for a 16-bit RGB TIFF (it keeps the
    high byte), which is plenty for an albedo map: the resulting error is under
    one part in 255 of an sRGB code value.  Decoding has to happen in one go,
    so the full-resolution array exists briefly; the reduction is done in row
    blocks so that no float copy of it is ever made.
    """
    Image = _pillow_module()
    try:
        image = Image.open(path)
        image.load()
    except OSError as exc:  # pragma: no cover - corrupt file
        raise TerrainDataError(f"Cannot decode {path} with Pillow: {exc}") from exc
    if image.mode != "RGB":
        image = image.convert("RGB")
    width, height = image.size
    if width % factor or height % factor:
        raise TerrainDataError(
            f"{path.name}: {width}x{height} is not divisible by {factor}."
        )
    rows_needed = height // factor
    columns_needed = width // factor
    reduced = np.empty((rows_needed, columns_needed, 3), dtype=np.float32)
    block = max(1, min(rows_needed, 128))
    try:
        source = np.asarray(image, dtype=np.uint8)
    except MemoryError as exc:  # pragma: no cover - very large image
        raise TerrainDataError(
            f"Not enough memory to decode {path.name}; reduce albedo_detail."
        ) from exc
    hook(0.65, "reducing colour mosaic")
    for start_row in range(0, rows_needed, block):
        stop_row = min(start_row + block, rows_needed)
        chunk = source[start_row * factor: stop_row * factor]
        # (rows, factor, cols//factor, factor, 3) -> mean over the two factor axes
        reshaped = chunk.reshape(
            stop_row - start_row, factor, columns_needed, factor, 3
        )
        reduced[start_row:stop_row] = reshaped.mean(axis=(1, 3)) / 255.0
    del source
    image.close()
    return reduced


def _bake_albedo_levels(
    sources: TerrainSources, albedo_detail: int, hook: ProgressHook
) -> List[np.ndarray]:
    """Build the colour pyramid, normalised to linear reflectance in [0, 1]."""
    path = sources.albedo_path
    if path is None:  # pragma: no cover - guarded by caller
        raise TerrainDataError("No colour raster configured.")
    factor = 2 ** max(0, int(albedo_detail))
    suffix = path.suffix.lower()
    coarse: np.ndarray
    integer_peak = 1.0

    if suffix == ".exr":
        reader: Any = _ScanlineExr(path)
        width, height = reader.width, reader.height
        if width % factor or height % factor:
            raise TerrainDataError(
                f"{path.name}: {width}x{height} is not divisible by {factor}."
            )
        coarse = _downsample_band_stream(reader.row_rgb, height, factor)
    elif suffix in (".tif", ".tiff"):
        width, height, bits, samples, uncompressed = _tiff_layout(path)
        if samples < 3:
            raise TerrainDataError(
                f"{path.name}: expected a 3-channel colour raster, found {samples}."
            )
        if width % factor or height % factor:
            raise TerrainDataError(
                f"{path.name}: {width}x{height} is not divisible by {factor}."
            )
        if uncompressed:
            tiff = _StrippedTiff(path)
            integer_peak = float(2 ** tiff.bits - 1)

            def fetch(index: int, _tiff: _StrippedTiff = tiff) -> np.ndarray:
                return np.asarray(_tiff.row(index), dtype=np.float32)[:, :3]

            coarse = _downsample_band_stream(fetch, height, factor)
        else:
            coarse = _downsample_compressed_tiff(path, factor, hook)
    else:
        raise TerrainDataError(
            f"Unsupported colour raster format {suffix!r}; expected .exr or .tif."
        )

    if integer_peak > 1.0:
        coarse = coarse / integer_peak
    if sources.albedo_encoding == "srgb":
        coarse = srgb_to_linear(coarse)
    hook(0.75, "building colour pyramid")
    levels = _build_pyramid_levels(
        coarse, _pyramid_depth(coarse.shape[1], coarse.shape[0])
    )
    del coarse
    return levels


def _tiff_layout(path: Path) -> Tuple[int, int, int, int, bool]:
    """``(width, height, bits, samples, uncompressed)`` without decoding pixels."""
    reader = _StrippedTiff(path, allow_compressed=True)
    try:
        return (
            reader.width,
            reader.height,
            int(reader.bits),
            int(reader.samples),
            reader.compression == 1,
        )
    finally:
        reader.close()


def _fingerprint(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:  # pragma: no cover - racing deletion
        return {"path": str(path), "size": None, "mtime_ns": None}
    return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _cache_key(
    sources: TerrainSources,
    detail: int,
    albedo_detail: int,
    include_albedo: bool,
) -> str:
    """Directory name identifying one *resolution* of the pyramid.

    The requested decimation is part of the identity, not just the source
    files: asking for ``detail=0`` and being handed a ``detail=1`` cache would
    silently render at half the resolution, which is exactly the kind of
    failure that is invisible until someone measures the picture.
    """
    import hashlib

    identity = "|".join(
        [
            sources.key(),
            f"dem={int(detail)}",
            f"albedo={int(albedo_detail) if include_albedo else 'off'}",
        ]
    )
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]
    return f"{sources.dem_path.stem}-{digest}"


def _level_entry(
    index: int, level: np.ndarray, encoder: Optional[Callable] = None
) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "index": index,
        "file": f"L{index}.npy",
        "shape": [int(level.shape[0]), int(level.shape[1])]
        + ([int(level.shape[2])] if level.ndim == 3 else []),
    }
    if index == 0:
        # The range of what is *stored*, not of the float level it came from:
        # the two differ by up to half a quantum, and the renderer compares
        # against the stored values.  Cheap here (the array is in memory, and
        # the encoder runs anyway to write it) and expensive to rediscover: see
        # RasterPyramid.value_range.
        stored = encoder(level) if encoder is not None else level
        entry["min"] = float(np.min(stored))
        entry["max"] = float(np.max(stored))
    return entry


def _write_levels(
    directory: Path,
    levels: List[np.ndarray],
    encoder: Callable[[np.ndarray], np.ndarray],
) -> None:
    """Quantise and write each level, releasing the float buffer as we go."""
    _ensure_dir(directory)
    for index, level in enumerate(levels):
        path = directory / f"L{index}.npy"
        tmp = path.with_suffix(".npy.tmp")
        encoded = encoder(level)
        levels[index] = None  # type: ignore[call-overload]
        with open(tmp, "wb") as handle:
            np.save(handle, encoded, allow_pickle=False)
        del encoded
        os.replace(tmp, path)


def _load_level(path: Path) -> np.ndarray:
    try:
        return np.load(str(path), mmap_mode="r", allow_pickle=False)
    except (OSError, ValueError) as exc:
        raise TerrainDataError(f"Cannot memory-map pyramid level {path}: {exc}") from exc


def clear_terrain_cache(cache: Optional[Path] = None) -> int:
    """Delete every baked terrain pyramid; returns the number removed."""
    import shutil

    root = Path(cache) if cache is not None else terrain_cache_root()
    if not root.is_dir():
        return 0
    removed = 0
    for child in sorted(root.iterdir()):
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# facade
# ---------------------------------------------------------------------------
class MoonTerrain:
    """Sampling façade over the global topography and colour pyramids.

    Instances are cheap to hold: every level is a memory map, so the operating
    system pages in only what a render actually touches.
    """

    def __init__(
        self,
        dem: RasterPyramid,
        albedo: Optional[RasterPyramid],
        sources: TerrainSources,
        cache_path: Optional[Path] = None,
        manifest: Optional[Dict[str, Any]] = None,
    ):
        self.dem = dem
        self.albedo = albedo
        self.sources = sources
        self.cache_path = cache_path
        self.manifest = manifest or {}

    # -- convenience -----------------------------------------------------
    @property
    def reference_radius_km(self) -> float:
        return MOON_REFERENCE_RADIUS_KM

    @property
    def has_albedo(self) -> bool:
        return self.albedo is not None

    def height_km(self, lon_deg: Any, lat_deg: Any, level: int = 0) -> np.ndarray:
        """Elevation above the 1737.4 km reference sphere, kilometres."""
        return self.dem.sample(lon_deg, lat_deg, level)

    def albedo_rgb(
        self, lon_deg: Any, lat_deg: Any, level: int = 0
    ) -> Optional[np.ndarray]:
        """Relative linear RGB reflectance, or ``None`` without a mosaic."""
        if self.albedo is None:
            return None
        return self.albedo.sample(lon_deg, lat_deg, level)

    def radius_km(self, lon_deg: Any, lat_deg: Any, level: int = 0) -> np.ndarray:
        """Surface radius, i.e. reference sphere plus elevation."""
        return self.reference_radius_km + self.height_km(lon_deg, lat_deg, level)

    def normals(
        self, lon_deg: Any, lat_deg: Any, level: int = 0
    ) -> np.ndarray:
        """Outward surface unit normals at the given selenographic points."""
        slope_east, slope_north = self.dem.slopes(lon_deg, lat_deg, level)
        up, east, north = _local_basis(lon_deg, lat_deg)
        normal = (
            up
            - np.asarray(slope_east, dtype=np.float64)[..., None] * east
            - np.asarray(slope_north, dtype=np.float64)[..., None] * north
        )
        return (normal / np.linalg.norm(normal, axis=-1, keepdims=True)).astype(np.float32)

    # -- 3D data ---------------------------------------------------------
    def build_mesh(
        self,
        *,
        lon_range: Tuple[float, float] = (-180.0, 180.0),
        lat_range: Tuple[float, float] = (-90.0, 90.0),
        level: int = 2,
        step: Optional[int] = None,
        with_albedo: bool = True,
        albedo_level: Optional[int] = None,
    ) -> "SurfaceMesh":
        """Triangulate a selenographic patch into a displaced sphere mesh."""
        return _build_mesh(
            self,
            lon_range=lon_range,
            lat_range=lat_range,
            level=level,
            step=step,
            with_albedo=with_albedo,
            albedo_level=albedo_level,
        )

    def visible_mesh(
        self,
        view: Any,
        *,
        level: int = 2,
        margin_deg: float = 2.0,
        with_albedo: bool = True,
        rings: Optional[int] = None,
    ) -> "SurfaceMesh":
        """Mesh of the hemisphere a viewer sees, clipped to the limb.

        *view* is anything exposing ``sub_observer_lon_deg`` /
        ``sub_observer_lat_deg`` (see :class:`moonlib.models.MoonView`).
        """
        return _build_visible_mesh(
            self,
            view,
            level=level,
            margin_deg=margin_deg,
            with_albedo=with_albedo,
            rings=rings,
        )

    # -- introspection ---------------------------------------------------
    def as_dict(self) -> Dict[str, Any]:
        return {
            "sources": self.sources.as_dict(),
            "cache_path": str(self.cache_path) if self.cache_path else None,
            "dem": self.dem.as_dict(),
            "albedo": self.albedo.as_dict() if self.albedo else None,
            "reference_radius_km": self.reference_radius_km,
            "albedo_note": RELATIVE_ALBEDO_NOTE,
            "cache_format": _CACHE_FORMAT,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        dem_shape = "x".join(str(v) for v in self.dem.base_shape)
        return (
            f"<MoonTerrain dem={self.sources.dem_path.name} "
            f"level0={dem_shape} albedo="
            f"{'yes' if self.has_albedo else 'no'}>"
        )


def _local_basis(lon_deg: Any, lat_deg: Any) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Selenographic ``(up, east, north)`` unit vectors in the MOON_ME frame."""
    lon = np.radians(np.asarray(lon_deg, dtype=np.float64))
    lat = np.radians(np.asarray(lat_deg, dtype=np.float64))
    cos_lat = np.cos(lat)
    sin_lat = np.sin(lat)
    cos_lon = np.cos(lon)
    sin_lon = np.sin(lon)
    up = np.stack([cos_lat * cos_lon, cos_lat * sin_lon, sin_lat], axis=-1)
    east = np.stack([-sin_lon, cos_lon, np.zeros_like(lon)], axis=-1)
    north = np.stack([-sin_lat * cos_lon, -sin_lat * sin_lon, cos_lat], axis=-1)
    return up, east, north


def open_terrain(
    *,
    sources: Optional[TerrainSources] = None,
    dem: Optional[Path] = None,
    albedo: Optional[Path] = None,
    cache: Optional[Path] = None,
    detail: int = 1,
    albedo_detail: int = 2,
    bake: bool = True,
    rebuild: bool = False,
    include_albedo: bool = True,
    progress: Optional[ProgressHook] = None,
) -> MoonTerrain:
    """Open the global terrain products, baking the pyramid cache if needed.

    Parameters
    ----------
    sources
        Pre-resolved source files; when omitted they are discovered.
    dem, albedo
        Explicit raster paths, forwarded to :func:`find_sources`.
    cache
        Terrain cache directory; ``None`` uses :func:`terrain_cache_root`.
    detail, albedo_detail
        Decimation of the finest cached level (see :func:`bake_terrain_cache`).
    bake
        When ``False``, a missing cache raises instead of being built.
    rebuild
        Force a rebuild even when a matching cache exists.

    Raises
    ------
    TerrainDataError
        When the rasters are missing, unreadable, or the cache is absent and
        *bake* is ``False``.
    """
    if sources is None:
        sources = find_sources(dem=dem, albedo=albedo)
    include_albedo = bool(include_albedo) and sources.albedo_path is not None
    root = Path(cache) if cache is not None else terrain_cache_root()
    target = root / _cache_key(sources, detail, albedo_detail, include_albedo)
    manifest_path = target / "manifest.json"

    if rebuild or not manifest_path.is_file():
        if not bake:
            raise TerrainDataError(
                f"No baked terrain pyramid at {target}. Build it with "
                "moonlib.terrain.bake_terrain_cache(), the 'moon terrain --bake' "
                "command, or call open_terrain(bake=True)."
            )
        target = bake_terrain_cache(
            sources,
            cache=cache,
            detail=detail,
            albedo_detail=albedo_detail,
            include_albedo=include_albedo,
            rebuild=rebuild,
            progress=progress,
        )
        manifest_path = target / "manifest.json"

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TerrainDataError(
            f"Terrain cache manifest {manifest_path} is unreadable: {exc}. "
            "Rebuild with rebuild=True."
        ) from exc

    expected = {
        "dem_detail": int(detail),
        "albedo_detail": int(albedo_detail),
        "include_albedo": include_albedo,
    }
    if manifest.get("request") not in (None, expected):
        raise TerrainDataError(
            f"Terrain cache {target} was baked for {manifest['request']} but "
            f"{expected} was requested, and the directory name did not "
            "distinguish them. Rebuild with rebuild=True."
        )

    dem_block = manifest.get("dem")
    if not dem_block:
        raise TerrainDataError(f"Terrain cache {target} has no topography levels.")
    dem_pyramid = _pyramid_from_manifest("dem", dem_block, target / "dem")
    dem_pyramid.scale = float(dem_block.get("quantum", _DEM_QUANTUM_KM))
    dem_pyramid.offset = 0.0

    albedo_pyramid = None
    albedo_block = manifest.get("albedo")
    if albedo_block:
        albedo_pyramid = _pyramid_from_manifest("albedo", albedo_block, target / "albedo")
        albedo_pyramid.scale = float(albedo_block.get("quantum", _ALBEDO_QUANTUM))
        albedo_pyramid.relative_mean = float(albedo_block.get("relative_mean", 1.0))
    return MoonTerrain(dem_pyramid, albedo_pyramid, sources, target, manifest)


def _pyramid_from_manifest(
    name: str, block: Dict[str, Any], directory: Path
) -> RasterPyramid:
    levels = []
    for entry in block.get("levels", ()):
        path = directory / str(entry["file"])
        if not path.is_file():
            raise TerrainDataError(
                f"Terrain cache is incomplete: {path} is missing. Rebuild it."
            )
        levels.append(_load_level(path))
    if not levels:
        raise TerrainDataError(f"Terrain cache level list for {name!r} is empty.")
    label = "elevation" if name == "dem" else "relative reflectance"
    recorded = block.get("levels") or ()
    value_range = None
    if recorded and "min" in recorded[0] and "max" in recorded[0]:
        value_range = (float(recorded[0]["min"]), float(recorded[0]["max"]))
    return RasterPyramid(
        name=name,
        levels=levels,
        label=label,
        unit=block.get("unit", ""),
        relative_mean=float(block.get("relative_mean", 1.0)),
        value_range=value_range,
    )


# ---------------------------------------------------------------------------
# meshes
# ---------------------------------------------------------------------------
@dataclass
class SurfaceMesh:
    """A displaced-sphere triangle mesh in the MOON_ME frame.

    Attributes
    ----------
    vertices
        ``(n, 3)`` float32 positions in kilometres.
    normals
        ``(n, 3)`` float32 outward unit normals derived from the DEM.
    uv
        ``(n, 2)`` float32 selenographic ``(lon_deg, lat_deg)`` of each vertex,
        which is also the texture coordinate pair of the albedo mosaic.
    colors
        ``(n, 3)`` float32 linear relative albedo, or ``None``.
    faces
        ``(m, 3)`` int32 triangle indices.
    """

    vertices: np.ndarray
    normals: np.ndarray
    uv: np.ndarray
    faces: np.ndarray
    colors: Optional[np.ndarray] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def n_vertices(self) -> int:
        return int(self.vertices.shape[0])

    @property
    def n_faces(self) -> int:
        return int(self.faces.shape[0])

    def as_dict(self) -> Dict[str, Any]:
        return {
            "n_vertices": self.n_vertices,
            "n_faces": self.n_faces,
            "has_colors": self.colors is not None,
            **{k: v for k, v in self.metadata.items()},
        }

    # -- writers ---------------------------------------------------------
    def export_ply(self, path: Any) -> Path:
        """Write a binary little-endian PLY with normals and vertex colours."""
        target = Path(path)
        has_color = self.colors is not None
        lines = [
            "ply",
            "format binary_little_endian 1.0",
            "comment generated by moonlib (moonlib.terrain.SurfaceMesh)",
            "element vertex %d" % self.n_vertices,
            "property float x",
            "property float y",
            "property float z",
            "property float nx",
            "property float ny",
            "property float nz",
            "property float u",
            "property float v",
        ]
        if has_color:
            lines += ["property uchar red", "property uchar green", "property uchar blue"]
        lines += ["element face %d" % self.n_faces, "property list uchar int vertex_indices", "end_header"]

        vertex_dtype = np.dtype([
            ("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
            ("nx", "<f4"), ("ny", "<f4"), ("nz", "<f4"),
            ("u", "<f4"), ("v", "<f4"),
        ] + ([("red", "u1"), ("green", "u1"), ("blue", "u1")] if has_color else []))
        records = np.empty(self.n_vertices, dtype=vertex_dtype)
        records["x"], records["y"], records["z"] = self.vertices[:, 0], self.vertices[:, 1], self.vertices[:, 2]
        records["nx"], records["ny"], records["nz"] = self.normals[:, 0], self.normals[:, 1], self.normals[:, 2]
        records["u"] = self.uv[:, 0]
        records["v"] = self.uv[:, 1]
        if has_color:
            encoded = np.clip(np.asarray(self.colors, dtype=np.float32) * 255.0 + 0.5, 0, 255)
            records["red"] = encoded[:, 0].astype(np.uint8)
            records["green"] = encoded[:, 1].astype(np.uint8)
            records["blue"] = encoded[:, 2].astype(np.uint8)

        face_dtype = np.dtype([("count", "u1"), ("a", "<i4"), ("b", "<i4"), ("c", "<i4")])
        face_records = np.empty(self.n_faces, dtype=face_dtype)
        face_records["count"] = 3
        face_records["a"] = self.faces[:, 0]
        face_records["b"] = self.faces[:, 1]
        face_records["c"] = self.faces[:, 2]

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as handle:
                handle.write(("\n".join(lines) + "\n").encode("ascii"))
                handle.write(records.tobytes())
                handle.write(face_records.tobytes())
        except OSError as exc:
            raise TerrainDataError(f"Cannot write mesh {target}: {exc}") from exc
        return target

    def export_obj(self, path: Any) -> Path:
        """Write a Wavefront OBJ with texture coordinates and vertex colours.

        Vertex colours use the widely implemented ``v x y z r g b`` extension;
        ``vt`` carries selenographic longitude/latitude normalised to ``[0, 1]``
        so that an albedo mosaic can be mapped by any OBJ consumer.
        """
        target = Path(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("w", encoding="ascii") as handle:
                handle.write("# generated by moonlib (moonlib.terrain.SurfaceMesh)\n")
                handle.write("o moon_surface\n")
                unit = 1.0 / (math.pi * MOON_REFERENCE_RADIUS_KM)
                if self.colors is not None:
                    colors = np.clip(np.asarray(self.colors, dtype=np.float64), 0.0, 1.0)
                    for index in range(self.n_vertices):
                        x, y, z = self.vertices[index]
                        r, g, b = colors[index]
                        handle.write(
                            f"v {x * unit:.7f} {y * unit:.7f} {z * unit:.7f} "
                            f"{r:.5f} {g:.5f} {b:.5f}\n"
                        )
                else:
                    for index in range(self.n_vertices):
                        x, y, z = self.vertices[index]
                        handle.write(f"v {x * unit:.7f} {y * unit:.7f} {z * unit:.7f}\n")
                for index in range(self.n_vertices):
                    x, y, z = self.normals[index]
                    handle.write(f"vn {x:.6f} {y:.6f} {z:.6f}\n")
                for index in range(self.n_vertices):
                    lon, lat = self.uv[index]
                    u = (lon + 180.0) / 360.0
                    v = (lat + 90.0) / 180.0
                    handle.write(f"vt {u:.7f} {v:.7f}\n")
                for index in range(self.n_faces):
                    a, b, c = (int(v) + 1 for v in self.faces[index])
                    handle.write(f"f {a}/{a}/{a} {b}/{b}/{b} {c}/{c}/{c}\n")
        except OSError as exc:
            raise TerrainDataError(f"Cannot write mesh {target}: {exc}") from exc
        return target


def _mesh_grid(
    terrain: MoonTerrain,
    lon_range: Tuple[float, float],
    lat_range: Tuple[float, float],
    level: int,
    step: Optional[int],
) -> Tuple[np.ndarray, np.ndarray]:
    height, width = terrain.dem.grid_shape(level)
    if step is None:
        # Aim at roughly half a million vertices, rounded to whole pixels.
        step = max(1, int(round(math.sqrt(width * height / 500_000.0))))
    d_lon = 360.0 / width
    d_lat = 180.0 / height
    lon_min, lon_max = lon_range
    lat_min, lat_max = lat_range
    n_lon = max(2, int(round((lon_max - lon_min) / (d_lon * step))) + 1)
    n_lat = max(2, int(round((lat_max - lat_min) / (d_lat * step))) + 1)
    lons = lon_min + np.arange(n_lon) * (lon_max - lon_min) / (n_lon - 1)
    lats = lat_min + np.arange(n_lat) * (lat_max - lat_min) / (n_lat - 1)
    return np.meshgrid(lons, lats)


def _assemble_mesh(
    terrain: MoonTerrain,
    lons: np.ndarray,
    lats: np.ndarray,
    level: int,
    with_albedo: bool,
    albedo_level: Optional[int],
    metadata: Dict[str, Any],
) -> SurfaceMesh:
    flat_lon = lons.ravel().astype(np.float64)
    flat_lat = lats.ravel().astype(np.float64)
    radii = np.asarray(terrain.radius_km(flat_lon, flat_lat, level), dtype=np.float64)
    up, _east, _north = _local_basis(flat_lon, flat_lat)
    vertices = (up * radii[:, None]).astype(np.float32)
    normals = np.asarray(terrain.normals(flat_lon, flat_lat, level), dtype=np.float32)
    uv = np.stack([flat_lon, flat_lat], axis=1).astype(np.float32)

    colors = None
    if with_albedo and terrain.has_albedo:
        colors = terrain.albedo_rgb(
            flat_lon, flat_lat, level if albedo_level is None else albedo_level
        )
        if colors is not None:
            colors = np.asarray(colors, dtype=np.float32)

    rows, cols = lons.shape
    row_index = np.arange(rows - 1)[:, None]
    col_index = np.arange(cols - 1)[None, :]
    top_left = (row_index * cols + col_index).ravel()
    bottom_left = top_left + cols
    faces = np.concatenate(
        [
            np.stack([top_left, bottom_left, top_left + 1], axis=1),
            np.stack([top_left + 1, bottom_left, bottom_left + 1], axis=1),
        ],
        axis=0,
    ).astype(np.int32)
    return SurfaceMesh(
        vertices=vertices,
        normals=normals,
        uv=uv,
        faces=faces,
        colors=colors,
        metadata=metadata,
    )


def _build_mesh(
    terrain: MoonTerrain,
    *,
    lon_range: Tuple[float, float],
    lat_range: Tuple[float, float],
    level: int,
    step: Optional[int],
    with_albedo: bool,
    albedo_level: Optional[int],
) -> SurfaceMesh:
    lons, lats = _mesh_grid(terrain, lon_range, lat_range, level, step)
    return _assemble_mesh(
        terrain,
        lons,
        lats,
        level,
        with_albedo,
        albedo_level,
        metadata={
            "kind": "patch",
            "pyramid_level": int(level),
            "lon_range": [float(lon_range[0]), float(lon_range[1])],
            "lat_range": [float(lat_range[0]), float(lat_range[1])],
            "reference_radius_km": MOON_REFERENCE_RADIUS_KM,
            "frame": "MOON_ME",
            "units": "km",
        },
    )


def _build_visible_mesh(
    terrain: MoonTerrain,
    view: Any,
    *,
    level: int,
    margin_deg: float,
    with_albedo: bool,
    rings: Optional[int] = None,
) -> SurfaceMesh:
    """Mesh of the near side as one observer sees it.

    The patch is a *polar cap* around the sub-observer point rather than a
    longitude/latitude box.  A box cannot do better than the whole globe here:
    a hemisphere centred on a point away from the equator reaches a pole, and
    once it does it spans every longitude, so half of a box mesh would be
    wasted on terrain that is facing away.  A cap grid is also the topology a
    viewer wants, with the limb as a clean outer ring.
    """
    lat0 = float(view.sub_observer_lat_deg)
    lon0 = float(view.sub_observer_lon_deg)
    cap_deg = 90.0 + max(0.0, float(margin_deg))
    n_rings = int(rings) if rings else max(24, int(round(cap_deg * 2.0)))
    n_azimuth = max(32, 2 * n_rings)

    up_axis, east_axis, north_axis = _local_basis(lon0, lat0)
    radial = np.linspace(0.0, np.radians(cap_deg), n_rings + 1)
    azimuth = np.linspace(0.0, 2.0 * np.pi, n_azimuth, endpoint=False)
    ring_index, azimuth_index = np.meshgrid(
        np.arange(n_rings + 1), np.arange(n_azimuth), indexing="ij"
    )
    rho = radial[ring_index]
    theta = azimuth[azimuth_index]

    cos_rho = np.cos(rho)
    sin_rho = np.sin(rho)
    directions = (
        cos_rho[..., None] * up_axis[None, None, :]
        + (sin_rho * np.cos(theta))[..., None] * east_axis[None, None, :]
        + (sin_rho * np.sin(theta))[..., None] * north_axis[None, None, :]
    )
    flat = directions.reshape(-1, 3)
    lon = np.degrees(np.arctan2(flat[:, 1], flat[:, 0]))
    lat = np.degrees(np.arcsin(np.clip(flat[:, 2], -1.0, 1.0)))

    radii = np.asarray(terrain.radius_km(lon, lat, level), dtype=np.float64)
    vertices = (flat * radii[:, None]).astype(np.float32)
    normals = np.asarray(terrain.normals(lon, lat, level), dtype=np.float32)
    uv = np.stack([lon, lat], axis=1).astype(np.float32)

    colors = None
    if with_albedo and terrain.has_albedo:
        sampled = terrain.albedo_rgb(lon, lat, level)
        if sampled is not None:
            colors = np.asarray(sampled, dtype=np.float32)

    # The innermost ring is degenerate (all n_azimuth vertices coincide at the
    # centre), so fan those to a single apex and quad the rings after it.
    n_az = n_azimuth
    centre_index = 0
    first_ring = np.arange(1, n_az + 1)
    next_ring = first_ring + n_az
    faces = [
        np.stack(
            [
                np.full(n_az, centre_index),
                first_ring,
                np.roll(first_ring, -1),
            ],
            axis=1,
        )
    ]
    for ring in range(1, n_rings):
        inner = 1 + (ring - 1) * n_az + np.arange(n_az)
        outer = inner + n_az
        faces.append(
            np.stack([inner, outer, np.roll(inner, -1)], axis=1)
        )
        faces.append(
            np.stack([np.roll(inner, -1), outer, np.roll(outer, -1)], axis=1)
        )
    del next_ring
    face_array = np.concatenate(faces, axis=0).astype(np.int32)

    return SurfaceMesh(
        vertices=vertices,
        normals=normals,
        uv=uv,
        faces=face_array,
        colors=colors,
        metadata={
            "kind": "visible_disk",
            "pyramid_level": int(level),
            "sub_observer_lon_deg": lon0,
            "sub_observer_lat_deg": lat0,
            "cap_radius_deg": cap_deg,
            "rings": int(n_rings),
            "azimuths": int(n_azimuth),
            "reference_radius_km": MOON_REFERENCE_RADIUS_KM,
            "frame": "MOON_ME",
            "units": "km",
        },
    )


def found_sources(**kwargs: Any) -> Optional[TerrainSources]:
    """Like :func:`find_sources` but returns ``None`` instead of raising."""
    try:
        return find_sources(**kwargs)
    except TerrainDataError:
        return None


# ---------------------------------------------------------------------------
# regional high-resolution windows
# ---------------------------------------------------------------------------
def find_region_files(root: Optional[Path] = None) -> List[Path]:
    """Regional DEM windows available under ``<kernels>/regions``."""
    candidates: List[Path] = []
    if root is not None:
        candidates.append(Path(root).expanduser())
    for base in _terrain_search_roots(None):
        candidates.append(base / "regions")
    found: List[Path] = []
    for directory in candidates:
        if directory.is_dir():
            found.extend(sorted(directory.glob("*.npy")))
    unique: List[Path] = []
    seen = set()
    for path in found:
        if str(path) not in seen:
            seen.add(str(path))
            unique.append(path)
    return unique


def load_region_dem(source: Any) -> RasterPyramid:
    """Load a regional DEM window as a :class:`RasterPyramid`.

    *source* is either a path to a ``.npy``/``.json`` pair written by
    ``tools/fetch_lola_global.py``, or a bare region name resolved against the
    ``regions`` directory next to the SPICE kernels.

    The stored array is already kilometres above the 1737.4 km sphere, so no
    scaling is applied; only the geometry differs from a global raster.
    """
    path = Path(source).expanduser()
    if path.suffix != ".npy":
        path = path.with_suffix(".npy")
    if not path.is_file():
        for candidate in find_region_files():
            if candidate.stem == Path(source).stem:
                path = candidate
                break
    meta_path = path.with_suffix(".json")
    if not path.is_file():
        available = ", ".join(p.stem for p in find_region_files()) or "none"
        raise TerrainDataError(
            f"No regional DEM at {path}. Fetch one with "
            "tools/fetch_lola_global.py; regions currently available: "
            f"{available}."
        )
    if not meta_path.is_file():
        raise TerrainDataError(
            f"{path} has no .json sidecar describing its grid, so its "
            "georeferencing is unknown. Re-fetch it with "
            "tools/fetch_lola_global.py."
        )
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TerrainDataError(f"Cannot read {meta_path}: {exc}") from exc
    base = np.load(str(path), mmap_mode="r", allow_pickle=False)
    if base.ndim != 2:
        raise TerrainDataError(
            f"{path}: expected a single-band raster, found shape {base.shape}."
        )
    bounds = tuple(float(v) for v in meta["bounds"])
    if list(base.shape) != list(meta["shape"]):
        raise TerrainDataError(
            f"{path}: array is {base.shape} but the sidecar says {meta['shape']}."
        )
    levels = _build_pyramid_levels(
        np.asarray(base, dtype=np.float32),
        _pyramid_depth(int(base.shape[1]), int(base.shape[0])),
    )
    return RasterPyramid(
        name=meta.get("name", path.stem),
        levels=levels,
        label="elevation",
        unit=meta.get("unit", "km"),
        scale=1.0,
        bounds=bounds,
    )


def open_region(
    source: Any,
    *,
    terrain: Optional[MoonTerrain] = None,
    progress: Optional[ProgressHook] = None,
) -> MoonTerrain:
    """A :class:`MoonTerrain` whose topography is a regional high-resolution window.

    The colour mosaic stays global: it is a global product and there is no
    finer regional version of it, so a close-up gets its detail from the
    topography and its colour from the same map the whole-disk render uses.
    The returned object can be passed straight to
    :func:`moonlib.moonview.render_region`.
    """
    region = load_region_dem(source)
    base = terrain if terrain is not None else open_terrain(progress=progress)
    return MoonTerrain(
        region,
        base.albedo,
        base.sources,
        base.cache_path,
        dict(base.manifest, regional_dem=True),
    )


# ---------------------------------------------------------------------------
# windows cut from a local global raster
# ---------------------------------------------------------------------------
#: Products whose pixel values are not already kilometres, keyed by a fragment
#: of the file name.  Detecting from the TIFF sample format covers the rest.
_KNOWN_DEM_SCALES = (
    ("LDEM_118m", 0.5 / 1000.0),      # USGS global LOLA: int16, half-metres
    ("SLDEM2015", 1.0),               # PDS SLDEM tiles: float32, kilometres
)


def _parse_nodata(tag: Optional[Tuple[int, int, bytes]]) -> Optional[float]:
    """The GDAL_NODATA value from a TIFF tag entry, or ``None``.

    The tag is ASCII and NUL-padded: ``b'-32768\\x00\\x00'`` is the one USGS
    puts on the 118 m LOLA mosaic.  A file with no such tag -- the SVS
    ``ldem_64.tif``, for instance -- has no nodata concept at all.
    """
    if not tag:
        return None
    kind, count, value = tag
    size = _TSIZE.get(kind, 1)
    inline = 8 if count * size <= 4 else count * size
    raw = value[:max(1, min(count * size, inline))]
    text = raw.split(b"\x00")[0].decode("ascii", "ignore").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _fill_nodata(values: np.ndarray, sentinel: float) -> np.ndarray:
    """Replace *sentinel* samples by a horizontal interpolation over the row.

    A DEM's nodata is "no measurement here", and the renderer has no way to
    draw a hole: it needs a height.  The neighbours in the same row are the
    closest thing to a measurement that exists, so the gap is filled with the
    line through them; a row that is entirely nodata is left at the datum, which
    is at worst a few kilometres of error over a seam that carries no data
    anyway.
    """
    bad = values == sentinel
    if not bad.any():
        return values
    if bad.all():
        return np.zeros_like(values)
    good = ~bad
    columns = np.arange(values.size, dtype=np.float64)
    values = values.copy()
    values[bad] = np.interp(columns[bad], columns[good], values[good])
    return values


def _dem_scale(reader: "_StrippedTiff", path: Path) -> float:
    """Factor converting a DEM's stored samples to kilometres.

    The SVS ``ldem_64.tif`` stores kilometres as float32; the USGS global LOLA
    mosaic stores half-metres as int16.  Both are "the LOLA DEM" and both are on
    the same 1737.4 km sphere, so reading either is just a matter of getting
    this factor right -- and getting it wrong by 1000x is exactly the kind of
    error that looks plausible in a rendered image.
    """
    if reader.sample_format == 3 and reader.bits == 32:
        return 1.0
    for fragment, scale in _KNOWN_DEM_SCALES:
        if fragment.lower() in path.name.lower():
            return scale
    if reader.bits == 16 and reader.sample_format == 2:
        raise TerrainDataError(
            f"{path.name} stores 16-bit integers but is not a recognised DEM "
            "product, so its units are unknown. Add it to _KNOWN_DEM_SCALES "
            "once you know whether the samples are metres, half-metres or "
            "something else."
        )
    raise TerrainDataError(
        f"{path.name}: cannot interpret a {reader.bits}-bit sample format "
        f"{reader.sample_format} as an elevation raster."
    )


def global_dem_sources(
    search_dir: Optional[Path] = None,
) -> List[Tuple[Path, float]]:
    """Global elevation rasters present locally, finest first.

    Returns ``(path, km_per_pixel)`` pairs.  This is the offline substitute for
    knowing which product is on disk: :func:`open_region_at` and
    :func:`open_terrain` both take the finest entry they can find.

    Any single-band, equirectangular raster counts, and its pixel size is read
    from its own width -- so a product whose name says nothing about resolution
    (USGS ships the 256 px/deg DEM as ``Lunar_LRO_LOLA_Global_LDEM_118m_...``)
    is ranked on what it holds rather than on what it is called.
    """
    named = os.environ.get(ENV_DEM)
    if named:
        # A raster named by environment *is* the answer: the reader asked for
        # this file, so the finest scale the page offers, the windows the
        # renderer cuts and the pixels it draws all have to come from it rather
        # than from whatever else happens to be in the directory beside it.
        chosen = _measure_global_raster(Path(named).expanduser())
        if chosen:
            return chosen
    found: List[Tuple[Path, float]] = []
    for base in _terrain_search_roots(search_dir):
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.tif")) + sorted(base.glob("*.tiff")):
            try:
                reader = _StrippedTiff(path, allow_compressed=True)
            except TerrainDataError:
                continue
            try:
                if reader.width < 360 or reader.samples != 1:
                    continue  # not a single-band global raster
                if reader.height * 2 != reader.width:
                    continue  # not equirectangular on a full sphere
                km = math.radians(360.0 / reader.width) * MOON_REFERENCE_RADIUS_KM
            finally:
                reader.close()
            found.append((path, km))
    found.sort(key=lambda item: item[1])
    return found


def _measure_global_raster(path: Path) -> List[Tuple[Path, float]]:
    """``[(path, km_per_pixel)]`` for one raster, or ``[]`` if it is unusable."""
    if not path.is_file():
        return []
    try:
        reader = _StrippedTiff(path, allow_compressed=True)
    except TerrainDataError:
        return []
    try:
        if reader.samples != 1 or reader.width < 2 or reader.height < 2:
            return []
        km = math.radians(360.0 / reader.width) * MOON_REFERENCE_RADIUS_KM
    finally:
        reader.close()
    return [(path, km)]


def find_global_dem(minimum_pixels_per_degree: float = 0.0) -> Optional[Path]:
    """Finest local global DEM at or above a resolution, or ``None``."""
    for path, km in global_dem_sources():
        if km > 0.0 and 1.0 / km >= minimum_pixels_per_degree:
            return path
    return None


def extract_region(
    source: Any,
    lon_min: float,
    lon_max: float,
    lat_min: float,
    lat_max: float,
    *,
    stride: int = 1,
) -> RasterPyramid:
    """Cut a lon/lat window out of a global raster on disk.

    Only the rows and columns inside the window are read, so pulling a 12 deg
    window out of an 8.5 GB global DEM touches about 1% of the file.  The
    result is a :class:`RasterPyramid` with :attr:`~RasterPyramid.bounds` set,
    already converted to kilometres.

    *stride* keeps every n-th source pixel.  It is how a frame too wide to hold
    at full resolution still gets a window -- a coarser one -- instead of
    dropping all the way back to the global pyramid.
    """
    path = Path(source).expanduser()
    if not path.is_file():
        raise TerrainDataError(f"No raster at {path}.")
    lon_min, lon_max = sorted((float(lon_min), float(lon_max)))
    lat_min, lat_max = sorted((float(lat_min), float(lat_max)))
    reader = _StrippedTiff(path)
    try:
        scale = _dem_scale(reader, path)
        ppd = reader.width / 360.0
        col0 = max(0, int(math.floor((lon_min + 180.0) * ppd)))
        col1 = min(reader.width, int(math.ceil((lon_max + 180.0) * ppd)))
        row0 = max(0, int(math.floor((90.0 - lat_max) * ppd)))
        row1 = min(reader.height, int(math.ceil((90.0 - lat_min) * ppd)))
        if col1 - col0 < 8 or row1 - row0 < 8:
            raise TerrainDataError(
                f"The window lon {lon_min:.3f}..{lon_max:.3f}, lat "
                f"{lat_min:.3f}..{lat_max:.3f} is smaller than 8 pixels at "
                f"{ppd:.0f} px/deg."
            )
        step = max(1, int(stride))
        rows = list(range(row0, row1, step))
        columns = slice(col0, col1, step)
        block = np.empty((len(rows), len(range(col0, col1, step))), dtype=np.float32)
        nodata = None if reader.nodata is None else reader.nodata * scale
        for offset, row_index in enumerate(rows):
            line = np.asarray(reader.row(row_index), dtype=np.float32)
            line = line if line.ndim == 1 else line[:, 0]
            if nodata is not None:
                line = _fill_nodata(line, nodata)
            block[offset] = line[columns]
        # The bounds keep the source's degrees: the decimated grid covers the
        # same box, it just has fewer samples across it, so RasterPyramid
        # derives the coarser pixel spacing from the reduced width by itself.
    finally:
        reader.close()
    if scale != 1.0:
        block *= scale
    bounds = (
        col0 / ppd - 180.0,
        col1 / ppd - 180.0,
        90.0 - row1 / ppd,
        90.0 - row0 / ppd,
    )
    levels = _build_pyramid_levels(
        block, _pyramid_depth(block.shape[1], block.shape[0])
    )
    return RasterPyramid(
        name=path.stem,
        levels=levels,
        label="elevation",
        unit="km",
        scale=1.0,
        bounds=bounds,
    )


def open_region_box(
    lon_min: float,
    lon_max: float,
    lat_min: float,
    lat_max: float,
    *,
    source: Optional[Any] = None,
    terrain: Optional[MoonTerrain] = None,
    stride: int = 1,
    progress: Optional[ProgressHook] = None,
) -> MoonTerrain:
    """A high-resolution terrain window covering an explicit lon/lat box.

    Preferred over :func:`open_region_at` when the caller has already measured
    what the frame covers: a radius guess can be too small for an elongated or
    steeply tilted frame, and the shortfall only shows up later as a coverage
    failure.
    """
    chosen = Path(source) if source is not None else find_global_dem()
    if chosen is None:
        raise TerrainDataError(
            "No global DEM on disk to cut a window from. Run "
            "tools/fetch_lola_global.py, or pass source= explicitly."
        )
    window = extract_region(
        chosen, lon_min, lon_max, lat_min, lat_max, stride=stride
    )
    base = terrain if terrain is not None else open_terrain(progress=progress)
    return MoonTerrain(
        window,
        base.albedo,
        base.sources,
        base.cache_path,
        dict(base.manifest, windowed_from=str(chosen)),
    )


def open_region_at(
    lon_deg: float,
    lat_deg: float,
    radius_km: float,
    *,
    source: Optional[Any] = None,
    terrain: Optional[MoonTerrain] = None,
    pad: float = 1.35,
    progress: Optional[ProgressHook] = None,
) -> MoonTerrain:
    """A high-resolution terrain window centred on a point, read from disk.

    *radius_km* is how far from the centre the close-up needs to reach; *pad*
    adds margin so that rays which leave the frame still land inside the
    window.  When *source* is omitted the finest global DEM on disk is used,
    which is what makes this offline: once the product is in ``kernels/`` the
    same call works for any target without a network round trip.
    """
    if radius_km <= 0.0:
        raise TerrainDataError("radius_km must be positive.")
    if pad < 1.0:
        raise TerrainDataError("pad must be at least 1.0.")
    chosen = Path(source) if source is not None else find_global_dem()
    if chosen is None:
        raise TerrainDataError(
            "No global DEM on disk to cut a window from. Run "
            "tools/fetch_lola_global.py, or pass source= explicitly."
        )
    reach_deg = pad * radius_km / (math.radians(1.0) * MOON_REFERENCE_RADIUS_KM)
    lon_span = reach_deg / max(math.cos(math.radians(lat_deg)), 1e-3)
    window = extract_region(
        chosen,
        lon_deg - lon_span,
        lon_deg + lon_span,
        lat_deg - reach_deg,
        lat_deg + reach_deg,
    )
    base = terrain if terrain is not None else open_terrain(progress=progress)
    return MoonTerrain(
        window,
        base.albedo,
        base.sources,
        base.cache_path,
        dict(base.manifest, windowed_from=str(chosen)),
    )
