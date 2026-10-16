#!/usr/bin/env python3
"""Example 5 — catalogues ranked by apparent size.

Replaces ``moon_calc.ipynb``.  That notebook's ``find_moons_by_illumination``
looked illumination values up through a string rewrite
(``d[illum_key.replace('_frac', '_pct')]``) and decided waxing/waning with two
different rules in two different places; here both are typed and single-sourced.

    python examples/05_illumination_search.py --percent 85.3 --year 2026
    python examples/05_illumination_search.py --supermoon --year 2026
"""

from __future__ import annotations

import argparse
from pathlib import Path

from _cli import add_output_argument, moonlib  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--percent", type=float, default=85.3)
    parser.add_argument("--lat", type=float, default=30)
    parser.add_argument("--lon", type=float, default=120)
    parser.add_argument("--waxing", dest="waxing", action="store_true", default=None)
    parser.add_argument("--waning", dest="waxing", action="store_false")
    parser.add_argument("--tolerance", type=float, default=3.0)
    parser.add_argument(
        "--supermoon", action="store_true", help="rank the year's full moons by size"
    )
    parser.add_argument("--no-plots", action="store_true")
    add_output_argument(parser, kind="dir")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    observer = moonlib.Observer(lat=args.lat, lon=args.lon)

    if args.supermoon:
        full_moons = [
            entry
            for entry in moonlib.phase_calendar(args.year, observer)
            if entry.kind is moonlib.PhaseKind.FULL_MOON
        ]
        full_moons.sort(key=lambda entry: entry.angular_diameter_arcsec, reverse=True)
        print(f"{len(full_moons)} full moons in {args.year}, largest first\n")
        print(f"{'#':>3}  {'UTC':^19}  {'Distance':>12}  {'Diameter':>10}  Class")
        print("-" * 62)
        for index, entry in enumerate(full_moons, start=1):
            if index <= 3:
                kind = "supermoon"
            elif index > len(full_moons) - 3:
                kind = "micromoon"
            else:
                kind = ""
            print(
                f"{index:>3}  {entry.when_utc:%Y-%m-%d %H:%M:%S}  "
                f"{entry.distance_km:>10,.0f} km  "
                f"{entry.angular_diameter_arcsec:>8.2f}\"  {kind}"
            )
        return 0

    matches = moonlib.find_moons_by_illumination(
        args.percent,
        args.year,
        observer,
        waxing=args.waxing,
        phase_tolerance_deg=args.tolerance,
    )
    print(moonlib.reporting.format_illumination_matches(matches))

    if args.no_plots or not matches:
        return 0

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    figure, axes = plt.subplots(1, 2, figsize=(13, 5))
    months = [match.month for match in matches]
    order = sorted(range(len(matches)), key=lambda index: months[index])
    axes[0].bar(
        [f"{months[index]:02d}" for index in order],
        [matches[index].angular_diameter_arcsec for index in order],
        color="#4c72b0",
    )
    axes[0].set_xlabel("local month")
    axes[0].set_ylabel("apparent diameter (arcsec)")
    axes[0].set_title(f"Moons at {args.percent:g} % illumination, {args.year}")
    axes[0].grid(alpha=0.3, axis="y")

    axes[1].scatter(
        [match.distance_km for match in matches],
        [match.angular_diameter_arcsec for match in matches],
        c=[match.elevation_deg for match in matches],
        cmap="viridis",
        s=60,
    )
    for match in matches:
        axes[1].annotate(
            f"{match.month:02d}",
            (match.distance_km, match.angular_diameter_arcsec),
            fontsize=7,
        )
    axes[1].set_xlabel("distance (km)")
    axes[1].set_ylabel("apparent diameter (arcsec)")
    axes[1].set_title("Size vs distance (colour = elevation at that instant)")
    axes[1].grid(alpha=0.3)

    figure.tight_layout()
    target = outdir / f"illumination_{args.percent:g}_{args.year}.png"
    figure.savefig(target, dpi=110)
    plt.close(figure)
    print(f"\nwrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
