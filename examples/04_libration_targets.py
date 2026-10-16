#!/usr/bin/env python3
"""Example 4 — libration-driven limb target prediction.

Replaces ``libration_targets_prediction.ipynb``.  The notebook called the full
``compute_lunar_data`` at every step of its sweep (which internally searches
moonrise/moonset over 48 h and the moon age over 35 d, about 2000 SPICE calls
per step); here the same sweep runs through one vectorised evaluation.

    python examples/04_libration_targets.py --from 2026-07-22 --to 2026-09-20
    python examples/04_libration_targets.py --at 2026-09-15T12:00:00 --list
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from _cli import add_output_argument, moonlib_s  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", default="2026-07-22")
    parser.add_argument("--to", dest="end", default="2026-09-20")
    parser.add_argument("--lat", type=float, default=30)
    parser.add_argument("--lon", type=float, default=120)
    parser.add_argument("--step-minutes", type=float, default=30.0)
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--at", help="single-instant query instead of a sweep")
    parser.add_argument("--list", action="store_true", help="list the catalogue")
    add_output_argument(parser, kind="dir")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    observer = moonlib_s.Observer(lat=args.lat, lon=args.lon)
    targets = moonlib_s.libration.load_targets()
    print(f"loaded {len(targets)} limb targets from the packaged catalogue")

    if args.list:
        for target in targets:
            print(
                f"  {target.english_name:<22} {target.name_zh:<12} "
                f"lat={target.lat_deg:+7.2f} lon={target.lon_deg:+8.2f} "
                f"r_eff={target.effective_radius_deg:6.3f}  {target.region}"
            )
        return 0

    if args.at:
        moment = datetime.fromisoformat(args.at).replace(tzinfo=timezone.utc)
        et = moonlib_s.to_et(moment)
        state = moonlib_s.moon_state(observer, moment)
        print(
            f"\nAt {moment:%Y-%m-%d %H:%M:%S} UTC   "
            f"sub-Earth lon {state.libration.sub_earth_lon_deg:+.3f} "
            f"lat {state.libration.sub_earth_lat_deg:+.3f}   "
            f"colongitude {state.libration.colongitude_deg:.2f}"
        )
        print(f"Moon elevation {state.elevation_deg:+.2f} deg, "
              f"illumination {state.illuminated_percent:.1f} %")
        visibilities = [
            moonlib_s.libration.target_visibility(
                target,
                et,
                observer,
                moon_elevation_deg=state.elevation_deg,
                illumination_percent=state.illuminated_percent,
            )
            for target in targets
        ]
        observable = [item for item in visibilities if item.is_observable]
        print(f"\n{len(observable)} targets simultaneously visible, sunlit and above the horizon:")
        print(moonlib_s.reporting.format_visible_targets(observable))
        return 0

    window = moonlib_s.as_time_range(args.start, args.end)
    started = time.perf_counter()
    samples = moonlib_s.libration.sweep(
        targets, window, observer, step_minutes=args.step_minutes
    )
    elapsed = time.perf_counter() - started
    print(
        f"swept {len(samples)} steps x {len(targets)} targets in {elapsed:.2f} s "
        f"({window.duration_days:.1f} days at {args.step_minutes:g}-minute steps)"
    )

    rows = moonlib_s.libration.summarize(samples, targets)[: args.top]
    print()
    print(moonlib_s.reporting.format_target_summary(rows))

    # Best observing window for the two polar-limb targets with the largest margin.
    for name in ("Plaskett", "Rozhdestvenskiy"):
        windows = moonlib_s.libration.best_windows(samples, name, top_n=3)
        if not windows:
            print(f"{name}: no qualifying window in this period")
            continue
        print(f"{name} — best windows (one per distinct window):")
        for visibility in windows:
            print(
                f"  {visibility.when_utc:%Y-%m-%d %H:%M} UTC  "
                f"grade={visibility.grade.value:<8} "
                f"margin={visibility.margin_deg:+.3f}  "
                f"sun inc={visibility.solar_incidence_deg:5.1f}  "
                f"moon el={visibility.moon_elevation_deg:+.1f}"
            )

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    target = outdir / "limb_target_summary.json"
    target.write_text(
        json.dumps(
            moonlib_s.service.limb_target_sweep(
                observer, window, step_minutes=args.step_minutes
            ),
            ensure_ascii=False,
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
