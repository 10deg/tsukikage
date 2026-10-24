#!/usr/bin/env python3
"""Bake the lunar terrain pyramid cache.

The renderer samples the global LOLA/LROC rasters through a downsampled,
memory-mappable pyramid.  :func:`moonlib.terrain.open_terrain` builds it
automatically on first use, so running this script is optional -- but it lets
you see the progress, choose the resolution, or warm the cache before a batch
run.

Usage
-----
    ./.venv/bin/python tools/bake_terrain.py                 # default levels
    ./.venv/bin/python tools/bake_terrain.py --full          # keep 64 ppd DEM
    ./.venv/bin/python tools/bake_terrain.py --no-albedo     # topography only
    ./.venv/bin/python tools/bake_terrain.py --clear         # remove it again

The default cache is about 350 MB (topography at 32 ppd plus the colour mosaic
at 19 ppd), plus a 530 MB topography level if ``--full`` is given.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from _bootstrap import use_src  # noqa: E402

moonlib = use_src()


def _progress(fraction: float, message: str) -> None:
    bar_width = 32
    filled = int(round(bar_width * fraction))
    bar = "#" * filled + "-" * (bar_width - filled)
    sys.stderr.write(f"\r  [{bar}] {fraction * 100:5.1f}%  {message:<24}")
    sys.stderr.flush()
    if fraction >= 1.0:
        sys.stderr.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bake the lunar terrain pyramid cache.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--detail", type=int, default=1,
        help="log2 decimation of the finest cached topography level "
             "(0 keeps the full 64 pixels/degree source, +530 MB)",
    )
    parser.add_argument(
        "--albedo-detail", type=int, default=2, help="same, for the colour mosaic"
    )
    parser.add_argument("--no-albedo", action="store_true", help="topography only")
    parser.add_argument("--rebuild", action="store_true", help="ignore any existing cache")
    parser.add_argument("--clear", action="store_true", help="delete every cached pyramid")
    parser.add_argument("--cache-dir", help="terrain cache directory")
    parser.add_argument(
        "--check", action="store_true",
        help="only report what is currently cached, do not build anything",
    )
    args = parser.parse_args()

    cache = Path(args.cache_dir) if args.cache_dir else None

    if args.clear:
        removed = moonlib.clear_terrain_cache(cache)
        print(f"removed {removed} cached pyramid(s)")
        return 0

    sources = moonlib.find_sources()
    print(f"topography : {sources.dem_path}")
    print(f"colour     : {sources.albedo_path}")

    if args.check:
        terrain = moonlib.open_terrain(cache=cache, bake=False)
        print(f"cache      : {terrain.cache_path}")
        print(f"levels     : topography {terrain.dem.n_levels}, "
              f"colour {terrain.albedo.n_levels if terrain.albedo else 0}")
        return 0

    started = time.time()
    target = moonlib.bake_terrain_cache(
        sources,
        cache=cache,
        detail=args.detail,
        albedo_detail=args.albedo_detail,
        include_albedo=not args.no_albedo,
        rebuild=args.rebuild,
        progress=_progress,
    )
    elapsed = time.time() - started
    total_bytes = sum(path.stat().st_size for path in target.rglob("*") if path.is_file())
    print(f"cache      : {target}")
    print(f"size       : {total_bytes / 1024 ** 2:.0f} MB")
    print(f"elapsed    : {elapsed:.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
