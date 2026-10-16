#!/usr/bin/env python3
"""End-to-end self check: exercise every public capability once.

Run from the repository root::

    python tools/selfcheck.py            # human-readable
    python tools/selfcheck.py --quiet    # only the summary line

This is deliberately *not* a replacement for the test suite: it is the quick
"does the installed library actually work against these kernels" check you run
after changing a kernel, moving the repository, or installing on a new machine.
It fails loudly with a traceback rather than a diff.
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from typing import Callable, List, Tuple

from _bootstrap import use_src  # noqa: E402

moonlib_s = use_src()


REFERENCE = datetime(2026, 5, 25, 15, 30, tzinfo=timezone.utc)
SITE = moonlib_s.Observer(lat=30, lon=120, name="selfcheck")


def _timed(label: str, action: Callable[[], object]) -> Tuple[str, object, float]:
    started = time.perf_counter()
    result = action()
    return label, result, time.perf_counter() - started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--year", type=int, default=2026)
    args = parser.parse_args()

    checks: List[Tuple[str, object, float]] = []
    failures: List[str] = []

    def run(label: str, action: Callable[[], object]) -> object:
        try:
            name, result, elapsed = _timed(label, action)
        except Exception as error:  # noqa: BLE001 - report and keep going
            failures.append(f"{label}: {type(error).__name__}: {error}")
            return None
        checks.append((name, result, elapsed))
        return result

    # -- environment -------------------------------------------------------
    session = run("kernels", lambda: moonlib_s.init_kernels())
    run("describe", lambda: moonlib_s.describe())

    # -- L1: state, report, sun, frames ------------------------------------
    state = run("moon_state", lambda: moonlib_s.moon_state(SITE, REFERENCE))
    report = run("moon_report", lambda: moonlib_s.moon_report(SITE, REFERENCE))
    run("sun_state", lambda: moonlib_s.sun_state(moonlib_s.to_et(REFERENCE), SITE))
    run("equation_of_time", lambda: moonlib_s.equation_of_time_minutes(moonlib_s.to_et(REFERENCE)))
    run("az_el", lambda: moonlib_s.az_el("MOON", moonlib_s.to_et(REFERENCE), SITE))
    run("scan_geometry", lambda: moonlib_s.scan_geometry(
        moonlib_s.TimeRange.utc_days(REFERENCE, 1.0).grid(600.0), SITE, with_horizontal=True))

    # -- invariants --------------------------------------------------------
    if state is not None:
        if not 0.0 <= state.illuminated_fraction <= 1.0:
            failures.append("illuminated fraction out of range")
        if not 0.0 <= state.phase.age_days < 29.54:
            failures.append("moon age out of range")
        if not 1700.0 <= state.angular_diameter_arcsec <= 2100.0:
            failures.append("angular diameter out of range")
        if not abs(state.libration.sub_earth_lon_deg) < 9.0:
            failures.append("libration longitude out of range")
    if report is not None and not report.rise_set.ordering_ok:
        failures.append("rise/transit/set are not ordered")

    # -- L2: phases, crescent, libration, analemma -------------------------
    calendar = run(f"phase_calendar({args.year})", lambda: moonlib_s.phase_calendar(args.year, SITE))
    run("find_phase_events", lambda: moonlib_s.find_phase_events(2026, step_seconds=7200.0))
    run("find_moons_by_illumination", lambda: moonlib_s.find_moons_by_illumination(50.0, args.year, SITE))
    crescents = run(f"find_crescent_windows({args.year})",
                    lambda: moonlib_s.crescent.find_crescent_windows(SITE, args.year))
    targets = run("load_targets", lambda: moonlib_s.libration.load_targets())
    samples = run("libration.sweep(3 d)",
                  lambda: moonlib_s.libration.sweep(
                      moonlib_s.libration.load_targets(),
                      moonlib_s.as_time_range("2026-08-01", "2026-08-04"),
                      SITE, step_minutes=60.0))
    if samples is not None:
        run("libration.summarize", lambda: moonlib_s.libration.summarize(samples, None))
        run("libration.best_windows",
            lambda: moonlib_s.libration.best_windows(samples, "Plaskett", top_n=3))
    points = run("classical_analemma", lambda: moonlib_s.analemma.classical_analemma(SITE, args.year))
    if points is not None:
        run("analemma_extremes", lambda: moonlib_s.analemma.analemma_extremes(points))

    if calendar is not None:
        counts = {}
        for entry in calendar:
            counts[entry.kind.value] = counts.get(entry.kind.value, 0) + 1
        if counts.get("full_moon", 0) < 12:
            failures.append(f"only {counts.get('full_moon', 0)} full moons in {args.year}")
    if targets is not None and len(targets) != 53:
        failures.append(f"expected 53 limb targets, got {len(targets)}")

    # -- service contract --------------------------------------------------
    payloads = run("service payloads", lambda: {
        "lunar_report": moonlib_s.service.lunar_report(SITE, REFERENCE),
        "phase_calendar": moonlib_s.service.phase_calendar(SITE, 2026),
        "crescent_windows": moonlib_s.service.crescent_windows(
            SITE, moonlib_s.TimeRange.utc_days(REFERENCE, 60.0)),
        "illumination_matches": moonlib_s.service.illumination_matches(50.0, SITE, 2026),
        "limb_targets": moonlib_s.service.limb_targets(),
        "analemma": moonlib_s.service.analemma(SITE, 2026, sample_days=(1, 15)),
        "environment": moonlib_s.service.environment(),
    })
    if payloads is not None:
        for kind, payload in payloads.items():
            try:
                encoded = moonlib_s.service.dumps(payload)
            except (TypeError, ValueError) as error:
                failures.append(f"service payload {kind} is not JSON-safe: {error}")
                continue
            if payload.get("kind") != kind:
                failures.append(f"service payload {kind} has kind={payload.get('kind')!r}")
            payloads[kind] = len(encoded)

    # -- reporting / plotting ----------------------------------------------
    if report is not None:
        run("format_moon_report", lambda: moonlib_s.reporting.format_moon_report(report))
    if calendar is not None:
        run("format_phase_calendar",
            lambda: moonlib_s.reporting.format_phase_calendar(calendar, SITE))
    if crescents is not None:
        run("format_crescent_windows",
            lambda: moonlib_s.reporting.format_crescent_windows(crescents, SITE, ranked=True, top_n=3))
    try:
        import matplotlib

        matplotlib.use("Agg")
        figure = run("plot_sky", lambda: moonlib_s.plotting.plot_sky(SITE, REFERENCE))
        if figure is not None:
            from moonlib_s.plotting import close_figure

            close_figure(figure)
        png = run("sky_chart_png", lambda: moonlib_s.service.sky_chart_png(SITE, REFERENCE, dpi=70))
        if png is not None and not png.startswith(b"\x89PNG"):
            failures.append("sky_chart_png did not return PNG bytes")
    except ImportError:
        checks.append(("plotting (skipped: matplotlib missing)", None, 0.0))

    # -- lunar surface simulation ------------------------------------------
    view = run("view_geometry", lambda: moonlib_s.view_geometry(SITE, REFERENCE))
    payload = run(
        "service moon_view",
        lambda: moonlib_s.service.moon_view(SITE, REFERENCE, width=64, height=64, samples=1),
    )
    if payload is not None:
        payloads["moon_view"] = len(moonlib_s.service.dumps(payload))
    if moonlib_s.found_sources() is None:
        checks.append(("terrain (skipped: rasters missing)", None, 0.0))
    else:
        settings = moonlib_s.MoonRenderSettings(
            width=96, height=96, samples_per_pixel=1, shadow_steps=24
        )
        render = run("render_moon", lambda: moonlib_s.render_moon(view=view, settings=settings))
        if render is not None:
            stats = render.stats
            if not 0.60 < stats["disk_fraction_of_frame"] < 0.72:
                failures.append(
                    f"render_moon covered {stats['disk_fraction_of_frame']:.3f} of the frame"
                )
            # Geometry and rendering must agree on how much of the disk is lit.
            if abs(stats["sunlit_fraction_of_disk"] - render.view.illuminated_fraction) > 0.05:
                failures.append("render_moon sunlit fraction disagrees with the phase")
            # Earthshine must be present but about ten magnitudes down.
            if not 8.0 < stats["earthshine_magnitudes_below_sunlit"] < 14.0:
                failures.append("render_moon earthshine ratio is out of range")
        run(
            "format_moon_view",
            lambda: moonlib_s.reporting.format_moon_view(moonlib_s.view_geometry(SITE, REFERENCE)),
        )
        png = run(
            "moon_view_png",
            lambda: moonlib_s.service.moon_view_png(SITE, REFERENCE, width=64, height=64, samples=1),
        )
        if png is not None and not png.startswith(b"\x89PNG"):
            failures.append("moon_view_png did not return PNG bytes")
        mesh = run(
            "visible_mesh",
            lambda: moonlib_s.open_terrain().visible_mesh(view, level=5, rings=24),
        )
        if mesh is not None and mesh.n_faces < 100:
            failures.append("visible_mesh produced too few faces")

    # -- output ------------------------------------------------------------
    if not args.quiet:
        print("=" * 72)
        print("  moonlib_s self check")
        print("=" * 72)
        for label, result, elapsed in checks:
            summary = _summarise(result)
            print(f"  {label:<34} {elapsed * 1000:8.2f} ms   {summary}")
        if payloads is not None:
            print("-" * 72)
            for kind, size in sorted(payloads.items()):
                print(f"  service {kind:<29} {size:8,d} bytes JSON")
        print("=" * 72)

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    mode = session.mode if session is not None else "?"
    text = session.coverage_text() if session is not None else None
    span = f"{text[0]} .. {text[1]}" if text else "unknown"
    print(f"self check PASSED — mode={mode}, coverage {span}")
    return 0


def _summarise(result: object) -> str:
    if result is None:
        return ""
    if isinstance(result, (list, tuple)):
        return f"{len(result)} item(s)"
    if isinstance(result, dict):
        return f"{len(result)} key(s)"
    if isinstance(result, (bytes, bytearray)):
        return f"{len(result):,} bytes"
    if isinstance(result, str):
        return f"{len(result):,} chars"
    name = type(result).__name__
    if name == "MoonState":
        return f"illum {result.illuminated_percent:.3f}%"  # type: ignore[attr-defined]
    if name == "MoonReport":
        return "rise/transit/set ok" if result.rise_set.ordering_ok else "ordering BAD"  # type: ignore[attr-defined]
    if name == "KernelSession":
        return f"mode={result.mode}, {len(result.loaded)} kernels"  # type: ignore[attr-defined]
    if name == "Figure":
        return f"{len(result.axes)} axes"  # type: ignore[attr-defined]
    return name


if __name__ == "__main__":
    raise SystemExit(main())
