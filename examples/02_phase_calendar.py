#!/usr/bin/env python3
"""Example 2 — the principal-phase calendar for a year.

Corresponds to the legacy ``moon_phase_query.ipynb``.  That notebook defined
its own ``find_moon_phases`` which identified full moons by requiring the phase
angle to drop below 0.02 rad; because the Moon's ecliptic latitude reaches
+/-5.15 deg the true phase angle at full moon is usually 1-5 deg, so 2026 was
reported as having 2 full moons instead of 13.  This script uses the shared
longitude-based finder and plots the apparent-diameter distribution.

    python examples/02_phase_calendar.py --year 2026
    python examples/02_phase_calendar.py --year 2026 --no-plots
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from _cli import add_output_argument, moonlib_s  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--lat", type=float, default=30)
    parser.add_argument("--lon", type=float, default=120)
    parser.add_argument("--no-plots", action="store_true")
    add_output_argument(parser, kind="dir")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    observer = moonlib_s.Observer(lat=args.lat, lon=args.lon)

    entries = moonlib_s.phase_calendar(args.year, observer)
    print(moonlib_s.reporting.format_phase_calendar(entries, observer))

    counts = Counter(entry.kind for entry in entries)
    expected = {moonlib_s.PhaseKind.NEW_MOON: 12, moonlib_s.PhaseKind.FULL_MOON: 13}
    for kind, minimum in expected.items():
        found = counts.get(kind, 0)
        flag = "ok" if found >= minimum else "UNEXPECTED"
        print(f"  {kind.name_en:<14} {found:3d}  (at least {minimum} expected) {flag}")

    if args.no_plots:
        return 0

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    colours = {
        moonlib_s.PhaseKind.NEW_MOON: "#4c72b0",
        moonlib_s.PhaseKind.FIRST_QUARTER: "#55a868",
        moonlib_s.PhaseKind.FULL_MOON: "#c44e52",
        moonlib_s.PhaseKind.LAST_QUARTER: "#8172b2",
    }

    figure, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=False)
    for kind in moonlib_s.PhaseKind:
        subset = [entry for entry in entries if entry.kind is kind]
        days = [entry.when_utc.timetuple().tm_yday for entry in subset]
        diameters = [entry.angular_diameter_arcsec for entry in subset]
        axes[0].scatter(days, diameters, s=28, label=kind.name_en, color=colours[kind])
    axes[0].set_xlabel("day of year")
    axes[0].set_ylabel("apparent diameter (arcsec)")
    axes[0].set_title(f"Apparent lunar diameter by phase, {args.year}")
    axes[0].legend(fontsize=8)
    axes[0].grid(alpha=0.3)

    full_moons = sorted(
        (entry for entry in entries if entry.kind is moonlib_s.PhaseKind.FULL_MOON),
        key=lambda entry: entry.angular_diameter_arcsec,
    )
    labels = [entry.when_utc.strftime("%m-%d") for entry in full_moons]
    axes[1].bar(range(len(full_moons)),
                [entry.angular_diameter_arcsec for entry in full_moons],
                color="#c44e52")
    axes[1].set_xticks(range(len(full_moons)))
    axes[1].set_xticklabels(labels, rotation=60, fontsize=7)
    axes[1].set_ylabel("arcsec")
    axes[1].set_title("Full moons ranked by apparent diameter (supermoon -> micromoon)")
    axes[1].grid(alpha=0.3, axis="y")

    figure.tight_layout()
    target = outdir / f"phase_calendar_{args.year}.png"
    figure.savefig(target, dpi=110)
    plt.close(figure)
    print(f"\nwrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
