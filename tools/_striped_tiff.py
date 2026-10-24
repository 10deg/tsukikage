#!/usr/bin/env python3
"""Write the uncompressed, strip-per-rows BigTIFF the terrain reader expects.

``moonlib.terrain._StrippedTiff`` reads a global DEM row by row through the
strip offsets, so a raster written by a tool here must be:

* **uncompressed** -- LZW/Deflate strips would need a decoder in the hot path;
* **not tiled** -- a tile's rows are not contiguous, and the reader follows
  strip offsets, not a tile directory;
* **one strip per small run of rows** -- 16 rows keeps the offset table tiny
  (5760 entries for the 512 ppd global grid) while still allowing a seek per
  strip.

The layout is the same one ``tools/fetch_lola_512.py`` has always written:
64 KiB of room for the header at the front, the pixels, then the BigTIFF IFD
and its offset/count arrays at the end.  Keeping the pixel offset fixed means a
partly downloaded file can be resumed by writing rows in place.
"""

from __future__ import annotations

import struct
from pathlib import Path

#: Bytes reserved before the first pixel.  The IFD lives at the end, so this is
#: pure slack -- but it leaves room for a future georeferencing tag block.
DATA_START = 65_536

#: Rows per strip.  The reader wants a strip small enough to seek per window
#: row but large enough that the offset table stays small.
ROWS_PER_STRIP = 16

#: Every product these tools write is signed 16-bit little-endian.
BITS = 16
SAMPLE_FORMAT = 2  # TIFF SampleFormat: signed integer


def strip_layout(width: int, height: int, *, rows_per_strip: int = ROWS_PER_STRIP):
    """Return the byte layout for an int16 image of ``width x height``.

    The returned mapping carries everything both the writer and a downloader
    need to place a row: the pixel offset, the bytes in one strip, the number of
    strips and the exact final file size.
    """
    width, height, rows_per_strip = int(width), int(height), int(rows_per_strip)
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    if rows_per_strip <= 0 or height % rows_per_strip:
        raise ValueError("height must be a multiple of rows_per_strip")
    strips = height // rows_per_strip
    strip_bytes = rows_per_strip * width * 2
    pixels_end = DATA_START + strips * strip_bytes
    ifd_offset = pixels_end
    counts_offset = ifd_offset + 8 + 11 * 20 + 8
    offsets_offset = counts_offset + 8 + strips * 8
    total = offsets_offset + 8 + strips * 8
    return {
        "width": width,
        "height": height,
        "rows_per_strip": rows_per_strip,
        "strips": strips,
        "strip_bytes": strip_bytes,
        "pixels_end": pixels_end,
        "ifd_offset": ifd_offset,
        "counts_offset": counts_offset,
        "offsets_offset": offsets_offset,
        "size": total,
    }


def write_int16_header(
    path: Path,
    width: int,
    height: int,
    *,
    rows_per_strip: int = ROWS_PER_STRIP,
) -> dict:
    """Preallocate *path* and write a BigTIFF header for the pixel grid.

    The strip offsets depend only on the geometry, so they are written once, up
    front; every later tile copy is a plain seek-and-write.  The file is
    extended to its final size so a short write cannot be mistaken for a
    complete raster.
    """
    path = Path(path)
    layout = strip_layout(width, height, rows_per_strip=rows_per_strip)
    with open(path, "wb") as handle:
        handle.truncate(layout["size"])
        ifd_offset = layout["ifd_offset"]
        entries = (
            (256, 4, 1, width),                       # ImageWidth
            (257, 4, 1, height),                      # ImageLength
            (258, 3, 1, BITS),                        # BitsPerSample
            (259, 3, 1, 1),                           # Compression = none
            (262, 3, 1, 1),                           # Photometric = min-is-black
            (273, 16, layout["strips"], layout["offsets_offset"]),  # StripOffsets
            (277, 3, 1, 1),                           # SamplesPerPixel
            (278, 4, 1, rows_per_strip),              # RowsPerStrip
            (279, 16, layout["strips"], layout["counts_offset"]),   # StripByteCounts
            (284, 3, 1, 1),                           # PlanarConfiguration
            (339, 3, 1, SAMPLE_FORMAT),               # SampleFormat = signed int
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
        handle.seek(layout["counts_offset"])
        handle.write(struct.pack(
            f"<{layout['strips']}Q", *([layout["strip_bytes"]] * layout["strips"])
        ))
        handle.seek(layout["offsets_offset"])
        handle.write(struct.pack(
            f"<{layout['strips']}Q",
            *[DATA_START + k * layout["strip_bytes"] for k in range(layout["strips"])],
        ))
    return layout
