#!/usr/bin/env python3
"""Generate the legacy-regression baseline for the test suite.

The refactor must reproduce the legacy ``moonlib`` numbers that were *correct*,
while deliberately changing the ones that were buggy.  This script records the
legacy outputs at fixed epochs into ``tests/data/baseline_legacy.json`` so that
``tests/test_regression_legacy.py`` can assert numerical continuity and
``tests/test_legacy_bugfixes.py`` can pin the intentional differences.

Run it from the repository root::

    python tools/make_legacy_baseline.py

It imports ``legacy/moonlib.py`` (a read-only snapshot of the pre-refactor
library) and never modifies it.

Neither input travels with the repository: the ``legacy/`` snapshot and the
``tests/`` suite this baseline serves both stay in the working copy of the
checkout that developed the library.  Nothing the library does touches either.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from _bootstrap import ROOT  # noqa: E402

LEGACY_DIR = ROOT / "legacy"
KERNEL_DIR = ROOT / "kernels"
OUTPUT = ROOT / "tests" / "data" / "baseline_legacy.json"

#: Fixed observation epochs.  Chosen to cover a waxing gibbous, a thin evening
#: crescent, a thin morning crescent, a full moon and a new moon.
EPOCHS = [
    {"label": "waxing_gibbous", "when": "2026-05-25T15:30:00"},
    {"label": "evening_crescent", "when": "2026-01-20T09:40:00"},
    {"label": "morning_crescent", "when": "2026-01-17T22:40:00"},
    {"label": "full_moon", "when": "2026-12-24T01:28:47"},
    {"label": "new_moon", "when": "2026-08-12T17:37:20"},
    {"label": "far_future", "when": "2100-03-01T00:00:00"},
]

SITES = [
    {"label": "standard", "lat": 30, "lon": 120},
    {"label": "equator", "lat": 0.0, "lon": 0.0},
    {"label": "high_latitude", "lat": 64.14, "lon": -21.94},
]

#: Fields copied verbatim from ``compute_lunar_data``.
FIELDS = [
    "et",
    "distance_km",
    "geo_distance_km",
    "angular_diameter_deg",
    "angular_diameter_arcsec",
    "phase_angle_deg",
    "phase_angle_topo_deg",
    "illuminated_percent",
    "illuminated_percent_topo",
    "age_days",
    "age_visual_days",
    "libration_lon_deg",
    "libration_lat_deg",
    "subsolar_lon_deg",
    "subsolar_lat_deg",
    "colongitude_deg",
    "ra_hours",
    "dec_deg",
    "elongation_deg",
    "elevation_deg",
    "srf_incidence_deg",
    "srf_emission_deg",
    "srf_phase_deg",
    "orb_rp_km",
    "orb_ecc",
    "orb_inc_deg",
    "orb_node_deg",
    "orb_argp_deg",
    "orb_m0_deg",
    "moon_velocity_kms",
    "light_time_sec",
]

#: Fields copied from ``quick_moon_data``.
QUICK_FIELDS = [
    "elevation_deg",
    "sun_elevation_deg",
    "moon_azimuth_deg",
    "sun_azimuth_deg",
    "phase_angle_deg",
    "illumination_pct",
    "elongation_deg",
    "moon_distance_km",
    "moon_angular_diameter_arcsec",
]


def main() -> int:
    if not (LEGACY_DIR / "moonlib.py").is_file():
        print(f"legacy/moonlib.py not found under {ROOT}", file=sys.stderr)
        return 1
    if not (KERNEL_DIR / "naif0012.tls").is_file():
        print(f"SPICE kernels not found in {KERNEL_DIR}", file=sys.stderr)
        return 1

    # The legacy snapshot imports numpy and spiceypy itself; fail with an
    # actionable message rather than a raw ModuleNotFoundError when this is run
    # with an interpreter that lacks them.
    try:
        import numpy  # noqa: F401
        import spiceypy  # noqa: F401
    except ImportError as error:
        print(
            f"Cannot import {error.name}: this tool needs the project "
            f"interpreter.\n"
            f"    {ROOT}/.venv/bin/python "
            f"{Path(__file__).relative_to(ROOT)}",
            file=sys.stderr,
        )
        return 1

    sys.path.insert(0, str(LEGACY_DIR))
    import moonlib as legacy  # type: ignore

    legacy.init_spice(kernel_dir=str(KERNEL_DIR), mode="recent")

    payload = {"generated_by": "tools/make_legacy_baseline.py", "states": [], "quick": [], "catalogues": {}}

    for site in SITES:
        for epoch in EPOCHS:
            moment = datetime.fromisoformat(epoch["when"]).replace(tzinfo=timezone.utc)
            result = legacy.compute_lunar_data(
                lat=site["lat"], lon=site["lon"], dt=moment
            )
            record = {"site": site["label"], "epoch": epoch["label"], "when_utc": epoch["when"]}
            for field in FIELDS:
                record[field] = float(result[field])
            payload["states"].append(record)

            quick = legacy.quick_moon_data(
                result["et"], lat=site["lat"], lon=site["lon"]
            )
            quick_record = dict(record)
            quick_record.pop("et")
            for field in QUICK_FIELDS:
                quick_record[field] = float(quick[field])
            payload["quick"].append(quick_record)

    # New moon / full moon catalogues for 2026: these exercise the only search
    # code path whose results are directly comparable.
    et_start = legacy.datetime_utc_to_et(datetime(2026, 1, 1, tzinfo=timezone.utc))
    et_end = legacy.datetime_utc_to_et(datetime(2027, 1, 1, tzinfo=timezone.utc))
    payload["catalogues"]["new_moons_2026"] = [
        entry["et"] for entry in legacy.find_new_moons(et_start, et_end)
    ]
    payload["catalogues"]["full_moons_2026"] = [
        entry["et"] for entry in legacy.find_full_moons(2026, 2026)
    ]

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")

    print(
        f"wrote {OUTPUT.relative_to(ROOT)}: "
        f"{len(payload['states'])} states, {len(payload['quick'])} quick lookups, "
        f"{len(payload['catalogues']['new_moons_2026'])} new moons, "
        f"{len(payload['catalogues']['full_moons_2026'])} full moons"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
