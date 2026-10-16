#!/usr/bin/env python3
"""Example 1 — a complete lunar report.

Corresponds to the legacy ``moon.ipynb`` notebook, but with no notebook state,
no duplicated SPICE code and no manual ``sys.path`` surgery::

    python examples/01_moon_report.py
    python examples/01_moon_report.py --lat 40.0 --lon 116.4 --at 2026-05-25T15:30:00
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from _cli import moonlib_s  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lat", type=float, default=30, help="latitude (deg)")
    parser.add_argument("--lon", type=float, default=120, help="longitude (deg)")
    parser.add_argument("--height", type=float, default=0.0, help="elevation (km)")
    parser.add_argument("--at", help="instant (ISO-8601 UTC); default: now")
    parser.add_argument(
        "--rise-set-mode",
        choices=("observational", "geometric"),
        default="observational",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    observer = moonlib_s.Observer(lat=args.lat, lon=args.lon, height_km=args.height)
    when = args.at or datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # 1. The full report: state + moonrise / transit / moonset
    # ------------------------------------------------------------------
    report = moonlib_s.moon_report(observer, when, rise_set_mode=args.rise_set_mode)
    print(moonlib_s.reporting.format_moon_report(report))

    # ------------------------------------------------------------------
    # 2. The lightweight entry point.  Same physics, no rise/set search:
    #    about 0.5 ms instead of 15 ms, which is what scanning code wants.
    # ------------------------------------------------------------------
    state = moonlib_s.moon_state(observer, when)
    print(
        f"quick check: illum {state.illuminated_percent:.3f}%  "
        f"alt {state.elevation_deg:+.2f} deg  "
        f"az {state.azimuth_deg:.2f} deg  "
        f"diameter {state.angular_diameter_arcsec:.2f}\""
    )

    # ------------------------------------------------------------------
    # 3. Every result is a frozen dataclass with a JSON-safe view, which is
    #    exactly what the future web/APP layer will consume.
    # ------------------------------------------------------------------
    payload = moonlib_s.service.lunar_report(observer, when)
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    print(f"\nservice payload: {len(encoded):,} bytes, kind={payload['kind']!r}, "
          f"schema={payload['schema']!r}")

    # ------------------------------------------------------------------
    # 4. One-liners the legacy library had no equivalent for.
    # ------------------------------------------------------------------
    print("\nnext moonrise / moonset (local):")
    print("  rise", observer.format_local(report.rise_set.next_rise_utc)
          if report.rise_set.next_rise_utc else "  rise -")
    print("  set ", observer.format_local(report.rise_set.next_set_utc)
          if report.rise_set.next_set_utc else "  set  -")

    upper = report.transit.upper_limb_elevation_deg if report.transit else float("nan")
    print(f"upper limb elevation at transit: {upper:+.3f} deg")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
