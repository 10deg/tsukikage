#!/usr/bin/env python3
"""Example 6 — which full moon culminates closest to the zenith?

This is the analysis from ``moon_calc.ipynb`` cell 8, rewritten on top of the
library.  The notebook hand-rolled its own full-moon refinement (reaching into
the library's *private* ``_ecliptic_lon_sin_diff``), computed the transit twice
for every candidate, and labelled a search that spanned 100 years as "±500
years".  Here it is a dozen lines of composition:

    find_full_moons(range)  ->  transit_details(...)  ->  sort by upper-limb elevation

The Moon's declination cannot exceed ``23.44 + 5.15 = 28.59`` degrees, because
the lunar orbit is inclined 5.15 degrees to the ecliptic.  A body at
declination ``dec`` culminates at elevation ``90 - |phi - dec|``, so the
theoretical ceiling for the transit elevation at latitude ``phi`` is
``90 - max(0, |phi| - 28.59)`` degrees.  Below that latitude the Moon really can
pass through the zenith.

    python examples/06_zenith_full_moons.py --years 20
    python examples/06_zenith_full_moons.py --years 4000 --lat 23.44   # needs mode='long'
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

from _cli import add_output_argument, moonlib  # noqa: E402

OBLIQUITY_DEG = 23.4392911
LUNAR_INCLINATION_DEG = 5.145
SIDEREAL_MONTH_DAYS = 27.321661


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lat", type=float, default=30)
    parser.add_argument("--lon", type=float, default=120)
    parser.add_argument("--years", type=float, default=20.0, help="half-width of the search")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument(
        "--long-ephemeris",
        action="store_true",
        help="load DE441 explicitly (implied when the span exceeds DE440s coverage)",
    )
    add_output_argument(parser, kind="dir")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    observer = moonlib.Observer(lat=args.lat, lon=args.lon)

    now = datetime.now(timezone.utc)
    span_years = max(1.0, args.years)
    if args.long_ephemeris or span_years > 100.0:
        moonlib.init_kernels(mode="long")

    window = moonlib.TimeRange.covering_years(
        now.year - int(span_years), now.year + int(span_years)
    )
    started = time.perf_counter()
    full_moons = moonlib.find_full_moons(window)
    print(
        f"{len(full_moons)} full moons between {window.start_utc:%Y} and "
        f"{window.end_utc:%Y} ({time.perf_counter() - started:.2f} s)"
    )

    # Transit search is the expensive part (about 15 ms each), so the catalogue
    # is pruned by a cheap proxy first: the maximum possible transit elevation
    # is 90 - |phi - declination|, and the declination of a full moon is
    # +/- (obliquity +/- lunar inclination).  Only the moons whose declination
    # is closest to the latitude can win, and a full moon near the zenith has
    # its declination within a few degrees of it.
    candidates = []
    for event in full_moons:
        state = moonlib.moon_state(observer, event.when_utc)
        candidates.append((abs(state.dec_deg - args.lat), event, state.dec_deg))
    candidates.sort(key=lambda item: item[0])

    shortlist = candidates[: max(args.top * 4, 40)]
    print(f"refining the {len(shortlist)} most promising candidates ...")

    results = []
    for _, event, declination in shortlist:
        details = moonlib.riseset.transit_details(observer, event.et)
        results.append((details.upper_limb_elevation_deg, event, details, declination))
    results.sort(reverse=True)

    max_declination = OBLIQUITY_DEG + LUNAR_INCLINATION_DEG
    theoretical = 90.0 - max(0.0, abs(args.lat) - max_declination)
    print(
        f"\nmaximum possible lunar declination: {max_declination:.2f} deg"
    )
    print(
        f"theoretical maximum transit elevation at lat {args.lat:+.2f}: "
        f"{theoretical:.2f} deg"
    )
    if abs(args.lat) <= max_declination:
        print("  (at this latitude the Moon can pass through the zenith)")
    print(f"{'#':>3}  {'UTC':^19}  {'Dec':>8}  {'Transit el':>10}  {'Upper limb':>10}  {'Deficit':>8}")
    print("-" * 70)
    for index, (elevation, event, details, declination) in enumerate(
        results[: args.top], start=1
    ):
        print(
            f"{index:>3}  {event.when_utc:%Y-%m-%d %H:%M:%S}  "
            f"{declination:+8.3f}  {details.elevation_deg:10.3f}  "
            f"{elevation:10.3f}  {theoretical - elevation:8.3f}"
        )

    best = results[0]
    print(
        f"\nBest case: {best[1].when_utc:%Y-%m-%d %H:%M} UTC, "
        f"transit at {best[2].elevation_deg:.3f} deg, "
        f"upper limb {best[0]:.3f} deg "
        f"({theoretical - best[0]:.3f} deg below the theoretical limit)"
    )
    print(
        "A deficit of a degree or two is expected: reaching the ceiling requires "
        "the full moon to sit simultaneously at maximum ecliptic latitude, at "
        "the declination extreme, and to cross the meridian at exactly the right "
        "instant — and a full moon is by definition opposite the Sun, so its "
        "declination is the mirror of the Sun's season rather than free."
    )

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    target = outdir / "zenith_full_moons.txt"
    with target.open("w", encoding="utf-8") as handle:
        handle.write(
            f"observer lat={args.lat} lon={args.lon}  span={span_years} years\n"
            f"theoretical limit {theoretical:.4f} deg\n\n"
        )
        for elevation, event, details, declination in results:
            handle.write(
                f"{event.when_utc.isoformat()}  dec={declination:+.4f}  "
                f"transit={details.elevation_deg:.4f}  upper={elevation:.4f}\n"
            )
    print(f"\nwrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
