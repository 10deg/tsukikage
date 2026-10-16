#!/usr/bin/env python3
"""Example 3 — thin-crescent observing windows.

Replaces both ``crescent_plot.ipynb`` and ``extreme_crescent.ipynb``: those two
notebooks between them contained a byte-identical copy of the library's broken
``crescent_plot`` (which raised ``NameError`` because it referenced the bare
names ``plt`` and ``Circle``) and a full re-implementation of the window search
whose scoring coefficients had already drifted away from the library's.

    python examples/03_crescent_windows.py --year 2026
    python examples/03_crescent_windows.py --year 2026 --sky 2026-01-20T09:39
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from _cli import add_output_argument, moonlib  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--lat", type=float, default=30)
    parser.add_argument("--lon", type=float, default=120)
    parser.add_argument("--illum-max", type=float, default=25.0)
    parser.add_argument("--step-minutes", type=float, default=5.0)
    parser.add_argument("--sky", help="render a sky chart for this instant instead")
    add_output_argument(parser, kind="dir")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    observer = moonlib.Observer(lat=args.lat, lon=args.lon)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    if args.sky:
        moment = datetime.fromisoformat(args.sky).replace(tzinfo=timezone.utc)
        figure = moonlib.plotting.plot_sky(observer, moment, span_hours=1.5)
        target = outdir / "crescent_sky.png"
        figure.savefig(target, dpi=120)
        import matplotlib.pyplot as plt

        plt.close(figure)
        print(f"wrote {target}")
        return 0

    criteria = moonlib.crescent.CrescentCriteria(
        illumination_max_percent=args.illum_max,
        step_minutes=args.step_minutes,
    )
    sessions = moonlib.crescent.find_crescent_windows(
        observer, args.year, criteria=criteria
    )
    print(f"{len(sessions)} sessions in {args.year}\n")
    print(
        moonlib.reporting.format_crescent_windows(
            sessions, observer, ranked=True, top_n=6
        )
    )

    # The advisory criteria are reported per session and never used to filter.
    best = min(sessions, key=lambda session: session.photo_score)
    print("Best-scoring session of the year")
    print(f"  local best : {best.best_local:%Y-%m-%d %H:%M:%S %Z}")
    print(f"  period     : {best.period}")
    print(f"  age        : {best.age_days:+.3f} d")
    print(f"  illum      : {best.geometry.illumination_percent_geo:.3f} %")
    print(f"  crescent   : {best.geometry.crescent_width_arcsec:.2f}\" wide")
    print(f"  bright limb: position angle {best.geometry.bright_limb_angle_deg:.1f} deg")
    for name, verdict in best.visibility.items():
        print(f"  {name:<8}: {verdict['description']}")

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure = moonlib.plotting.plot_sky(observer, best.best_utc, span_hours=1.5)
    target = outdir / "crescent_best.png"
    figure.savefig(target, dpi=120)
    plt.close(figure)
    print(f"\nwrote {target}")

    # ------------------------------------------------------------------
    # Elongation vs illumination: the theoretical curve `illum = (1 -
    # cos elong) / 2` only holds exactly for a geocentric, zero-latitude
    # geometry, so the points scatter about it by a few tenths of a percent.
    # The Danjon limit (~7 deg elongation) is drawn as a reference line; it is
    # shown, never used to filter.
    # ------------------------------------------------------------------
    import numpy as np

    elongations = [session.geometry.elongation_deg for session in sessions]
    illuminations = [session.geometry.illumination_percent_geo for session in sessions]
    curve = np.linspace(0.0, 60.0, 400)
    theoretical = (1.0 - np.cos(np.radians(curve))) * 50.0

    figure, axes = plt.subplots(figsize=(9.5, 6.5))
    scatter = axes.scatter(
        elongations,
        illuminations,
        c=[session.age_days for session in sessions],
        cmap="twilight",
        s=34,
        edgecolor="white",
        linewidth=0.3,
    )
    axes.plot(curve, theoretical, "--", color="#c44e52", lw=1.2,
              label="illum = (1 - cos ARCL) / 2")
    axes.axvline(moonlib.crescent.DANJON_LIMIT_DEG, color="#555555", ls=":",
                 label=f"Danjon limit ({moonlib.crescent.DANJON_LIMIT_DEG:.0f} deg)")
    axes.set_xlim(0.0, 60.0)
    axes.set_ylim(0.0, 30.0)
    axes.set_xlabel("elongation / ARCL (deg)")
    axes.set_ylabel("illumination (per cent)")
    axes.set_title(
        f"Crescent sessions {args.year} — {len(sessions)} windows, "
        f"lat {observer.lat:.2f} lon {observer.lon:.2f}"
    )
    axes.grid(alpha=0.3)
    axes.legend(fontsize=8)
    figure.colorbar(scatter, ax=axes, label="signed moon age (days)")
    figure.tight_layout()
    target = outdir / "crescent_elongation_illumination.png"
    figure.savefig(target, dpi=120)
    plt.close(figure)
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
