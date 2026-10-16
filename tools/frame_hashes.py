#!/usr/bin/env python3
"""Pin the renderer's output with frame hashes, so a refactor cannot move it.

Run it before and after a change to the render path::

    ./.venv/bin/python tools/frame_hashes.py            # compare with the record
    ./.venv/bin/python tools/frame_hashes.py --update   # re-record after a deliberate change

Three frames are drawn, each chosen because it exercises a different part of the
pipeline, and the sha256 of the 8-bit RGB output is compared with the value
recorded in ``tests/data/frame_hashes.json``:

``disk400``
    The whole disk at 400 px, 48 shadow steps: the reference the project has
    used since the GPU path was measured, and the one number most changes move.
``region400``
    A close-up through the same API the page uses, so the region renderer and
    its exposure path are covered too.
``quarter400``
    A first-quarter disk at 96 steps, where the terminator crosses the frame and
    the shadow march is actually doing work.

Two things make the hashes reproducible and worth trusting:

* the renders run through :class:`server.MoonService`, which is the page's own
  path -- not a private test-only entry point;
* ``MOONLIB_BAND_WORKERS=1`` is forced before :mod:`moonlib_s` is imported, because
  bands reduce in a different order when they run in parallel and the last bits
  of a float sum are not associative.  A frame hash is only a guardrail if it is
  the same number on every machine.

The record is JSON rather than a constant in this file so that a test can read
the same numbers instead of carrying a second copy of them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "web"))

# Before moonlib_s, so every band runs in the same order on every machine.
os.environ["MOONLIB_BAND_WORKERS"] = "1"

import numpy as np  # noqa: E402

import server  # noqa: E402

RECORD = ROOT / "tests" / "data" / "frame_hashes.json"

#: The observing site and instant the disk frames are drawn from.
SITE = {"lat": 30, "lon": 120}
NEAR_FULL = "2026-09-26T12:00:00"
FIRST_QUARTER = "2026-01-10T16:00:00"

#: The close-up: a point in the southern highlands, seen through a 150" radius.
REGION = {"lon": -11.36, "lat": -43.31, "fov_radius_arcsec": 150.0,
          "rotation_deg": 0.0}


def _when(at: str) -> dict:
    return {"at": at, "tz_offset_hours": 8, "observer": dict(SITE)}


def _hash(image: np.ndarray) -> str:
    return hashlib.sha256(np.asarray(image).astype(np.uint8).tobytes()).hexdigest()[:12]


#: Every pinned frame, as data.
#:
#: This list is the single definition of *what is pinned*: this tool measures it
#: and ``tests/test_frames.py`` asserts each entry against the record, so the two
#: cannot describe different frames.  They used to carry separate copies of the
#: instants, widths and step counts, which is exactly the drift the shared record
#: file was supposed to prevent -- a frame could be edited in one place and the
#: guardrail would go on measuring the other.
#:
#: ``kind`` is ``"disk"`` or ``"region"``, meaning the frame is drawn through
#: :meth:`server.MoonService.disk` or :meth:`server.MoonService.region`.
FRAMES = (
    {
        "key": "disk400",
        "kind": "disk",
        "why": "the whole disk at 400 px, 48 steps: the project's reference frame",
        "when": NEAR_FULL,
        "width": 400,
        "samples": 1,
        "shadow_steps": 48,
    },
    {
        "key": "quarter400",
        "kind": "disk",
        "why": "first quarter at 96 steps, where the shadow march does real work",
        "when": FIRST_QUARTER,
        "width": 400,
        "samples": 1,
        "shadow_steps": 96,
    },
    {
        "key": "region400",
        "kind": "region",
        "why": "a close-up through the region API, the one the page also uses",
        "when": NEAR_FULL,
        "width": 400,
        "samples": 1,
        "shadow_steps": 48,
        "selection": REGION,
    },
)


def draw(frame: dict, service: Any) -> np.ndarray:
    """Render one entry of :data:`FRAMES` through the page's own service."""
    if frame["kind"] == "disk":
        image, _info = service.disk(
            {"when": _when(frame["when"])},
            width=frame["width"], samples=frame["samples"],
            shadow_steps=frame["shadow_steps"],
        )
        return image
    request = {
        "when": _when(frame["when"]),
        "selection": dict(frame["selection"]),
        "output": {"width": frame["width"], "height": frame["width"],
                   "samples": frame["samples"]},
        "shadow_steps": frame["shadow_steps"],
        "telescope": {}, "camera": {}, "processing": {},
    }
    image, _info, _render_id, _raw = service.region(request)
    return image


def measure(service: Any = None) -> dict:
    """Render every frame in :data:`FRAMES` and return ``{key: sha256[:12]}``."""
    if service is None:
        service = server.MoonService()
    return {frame["key"]: _hash(draw(frame, service)) for frame in FRAMES}


#: The record's one non-hash entry: which rasters the frames were drawn from.
#: A whole-disk frame is drawn from the finest global raster on disk, so a
#: machine with the 256 px/deg DEM and a machine with only the 64 px/deg one
#: render *different pictures* -- correctly, but not comparably.
SOURCES_KEY = "_sources"


def sources_fingerprint() -> Dict[str, Any]:
    """Name and pixel size of the rasters the frames are drawn from."""
    from moonlib_s.terrain import find_sources, global_dem_sources

    try:
        found = global_dem_sources()
    except Exception:  # pragma: no cover - unreadable rasters
        found = []
    try:
        chosen = find_sources()
    except Exception:  # pragma: no cover - no rasters at all
        return {"dem": None, "albedo": None, "sources": []}
    return {
        "dem": chosen.dem_path.name,
        "albedo": None if chosen.albedo_path is None else chosen.albedo_path.name,
        "sources": [[path.name, round(km, 6)] for path, km in found],
    }


def load() -> dict:
    if not RECORD.is_file():
        return {}
    with RECORD.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", action="store_true",
                        help="re-record the hashes instead of comparing with them")
    args = parser.parse_args(argv)

    recorded = load()
    measured = measure()

    if args.update:
        RECORD.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(measured)
        payload[SOURCES_KEY] = sources_fingerprint()
        with RECORD.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        for name, value in sorted(measured.items()):
            print(f"{name:12s} {value}  (recorded)")
        return 0

    failed = False
    # The record carries one entry that is not a hash: which rasters the frames
    # were drawn from.  It is metadata for the reader, not a frame to compare.
    recorded = {name: value for name, value in recorded.items()
                if name != SOURCES_KEY}
    for name in sorted(measured):
        want = recorded.get(name)
        got = measured[name]
        if want is None:
            mark = "NEW"          # a frame the record does not know about yet
        elif want == got:
            mark = "same"
        else:
            mark, failed = "CHANGED", True
        print(f"{name:12s} {got}  {mark}"
              + (f" (recorded {want})" if want and want != got else ""))
    for name in sorted(set(recorded) - set(measured)):
        print(f"{name:12s} {'-' * 12}  MISSING (in the record, not measured)")
        failed = True
    if failed:
        print("\nA frame moved. If that was deliberate, re-record with --update.")
        return 1
    print("\nAll frames match the record.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
