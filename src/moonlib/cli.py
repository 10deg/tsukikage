"""Command line interface: ``python -m moonlib`` / ``moon``.

Two output paths, one source of truth:

* ``--json`` goes through :mod:`moonlib.service`, so the CLI emits exactly the
  payload a future HTTP API will serve;
* human-readable output calls the domain layer directly and formats it with
  :mod:`moonlib.reporting`.

No astronomy lives here.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Optional, Sequence

from ._version import __version__
from .errors import MoonlibError

_HELP = {
    "info": "describe the library and kernel pool",
    "moon": "lunar state and rise/set report",
    "phases": "principal phase calendar",
    "crescent": "thin-crescent observing windows",
    "illumination": "moons whose illumination matches a value",
    "libration": "lunar limb target visibility",
    "analemma": "solar analemma",
    "sunpoint": "solar altitude/azimuth at a lunar surface point",
    "plot": "render a sky chart to a PNG file",
    "render": "simulate the Moon as seen from a site (disk, or a close-up)",
    "terrain": "inspect, bake or clear the lunar terrain pyramid cache",
}


# ---------------------------------------------------------------------------
# argument groups
# ---------------------------------------------------------------------------
def _add_observer_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("observer")
    group.add_argument("--lat", type=float, default=30, help="geodetic latitude (deg)")
    group.add_argument(
        "--lon", type=float, default=120, help="longitude, east positive (deg)"
    )
    group.add_argument(
        "--height", type=float, default=0.0, help="elevation above the ellipsoid (km)"
    )


def _add_period_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("period")
    group.add_argument("--year", type=int, help="a single UTC calendar year")
    group.add_argument("--from", dest="start", help="start instant (ISO-8601) or year")
    group.add_argument("--to", dest="end", help="end instant (ISO-8601) or year")


def _add_output_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("output")
    group.add_argument("--json", action="store_true", help="emit the service payload as JSON")
    group.add_argument(
        "--language",
        choices=("both", "zh", "en"),
        default="both",
        help="label language for human-readable output",
    )


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser (exposed for tests and ``--help``)."""
    parser = argparse.ArgumentParser(
        prog="moon",
        description=(
            "Offline lunar computation service. Every result is computed from "
            "local JPL DE440/DE441 SPICE kernels; no network access is used."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"moonlib {__version__}")
    parser.add_argument("--kernel-dir", help="directory holding the SPICE kernels")
    parser.add_argument(
        "--kernel-mode",
        choices=("recent", "long"),
        help="'recent' = DE440 (about 1850-2150), 'long' = DE441 (millennia)",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress progress notes")

    subparsers = parser.add_subparsers(dest="command", required=True)
    commands = {
        name: subparsers.add_parser(name, help=help_text)
        for name, help_text in _HELP.items()
    }

    _add_output_options(commands["info"])

    moon = commands["moon"]
    _add_observer_options(moon)
    _add_output_options(moon)
    moon.add_argument("--at", help="instant (ISO-8601); defaults to now")
    moon.add_argument(
        "--rise-set-mode",
        choices=("observational", "geometric"),
        default="observational",
        help="rise/set elevation convention",
    )
    moon.add_argument("--state-only", action="store_true", help="skip rise/set/transit")
    moon.add_argument("--no-transit-details", action="store_true")

    phases = commands["phases"]
    _add_observer_options(phases)
    _add_period_options(phases)
    _add_output_options(phases)
    phases.add_argument(
        "--kinds",
        nargs="+",
        choices=("new_moon", "first_quarter", "full_moon", "last_quarter"),
        help="restrict to these phases",
    )

    crescent = commands["crescent"]
    _add_observer_options(crescent)
    _add_period_options(crescent)
    _add_output_options(crescent)
    crescent.add_argument("--illum-min", type=float, default=0.05)
    crescent.add_argument("--illum-max", type=float, default=25.0)
    crescent.add_argument("--sun-elev-min", type=float, default=-8.0)
    crescent.add_argument("--sun-elev-max", type=float, default=6.0)
    crescent.add_argument("--moon-elev-min", type=float, default=2.0)
    crescent.add_argument("--step-minutes", type=float, default=5.0)
    crescent.add_argument("--scan-days", type=float, default=3.0)
    crescent.add_argument("--no-refine", action="store_true")
    crescent.add_argument("--ranked", action="store_true", help="rank by photo score")
    crescent.add_argument("--top", type=int, default=20, help="rows per group when ranked")
    crescent.add_argument(
        "--phase-mode", choices=("geocentric", "topocentric"), default="geocentric"
    )

    illumination = commands["illumination"]
    _add_observer_options(illumination)
    _add_period_options(illumination)
    _add_output_options(illumination)
    illumination.add_argument("--percent", type=float, required=True)
    illumination.add_argument("--waxing", dest="waxing", action="store_true", default=None)
    illumination.add_argument("--waning", dest="waxing", action="store_false")
    illumination.add_argument("--tolerance", type=float, default=3.0)
    illumination.add_argument(
        "--phase-mode", choices=("geocentric", "topocentric"), default="topocentric"
    )

    libration = commands["libration"]
    _add_observer_options(libration)
    _add_period_options(libration)
    _add_output_options(libration)
    libration.add_argument("--step-minutes", type=float, default=30.0)
    libration.add_argument("--targets", action="store_true", help="list the catalogue")
    libration.add_argument(
        "--min-grade", choices=("hidden", "marginal", "center", "full"), default="marginal"
    )
    libration.add_argument("--min-moon-elev", type=float, default=0.0)
    libration.add_argument("--max-solar-incidence", type=float, default=90.0)
    libration.add_argument("--include-samples", action="store_true")
    libration.add_argument("--top", type=int, default=60, help="rows in the summary table")

    analemma_parser = commands["analemma"]
    _add_observer_options(analemma_parser)
    _add_period_options(analemma_parser)
    _add_output_options(analemma_parser)
    analemma_parser.add_argument("--hour", type=int, default=12, help="local clock hour")
    analemma_parser.add_argument("--minute", type=int, default=0, help="local clock minute")
    analemma_parser.add_argument(
        "--sample-days", nargs="+", type=int, help="day-of-month values to sample"
    )

    sunpoint = commands["sunpoint"]
    _add_observer_options(sunpoint)
    _add_output_options(sunpoint)
    sunpoint.add_argument("--moon-lon", type=float, required=True,
                          help="selenographic longitude, east positive (deg)")
    sunpoint.add_argument("--moon-lat", type=float, required=True,
                          help="selenographic latitude (deg)")
    sunpoint.add_argument("--at", help="single instant (ISO-8601); defaults to now")
    sunpoint.add_argument("--from", dest="start", help="series start (ISO-8601)")
    sunpoint.add_argument("--to", dest="end", help="series end (ISO-8601)")
    sunpoint.add_argument("--step-minutes", type=float, default=10.0,
                          help="series sampling interval")

    plot = commands["plot"]
    _add_observer_options(plot)
    plot.add_argument("--out", required=True, help="output PNG path")
    plot.add_argument("--at", help="instant (ISO-8601); defaults to now")
    plot.add_argument("--sunset", action="store_true", help="centre on the local sunset")
    plot.add_argument("--span-hours", type=float, default=1.5)
    plot.add_argument("--dpi", type=int, default=110)

    render = commands["render"]
    _add_observer_options(render)
    _add_output_options(render)
    render.add_argument("--at", help="instant (ISO-8601); defaults to now")
    render.add_argument(
        "--viewpoint", default="earth", choices=["earth", "far_side"],
        help="'earth' is the normal near-side view; 'far_side' is the "
             "hypothetical geocentric observer at the antipode of the Earth",
    )
    render.add_argument("--out", help="output PNG path; omit to only print the geometry")
    render.add_argument("--width", type=int, default=1024, help="image width in pixels")
    render.add_argument(
        "--height-px", type=int, default=None, dest="height_px",
        help="image height in pixels; defaults to --width (square)",
    )
    render.add_argument(
        "--samples", type=int, default=2,
        help="sub-pixel samples per axis (4 rays per pixel at 2)",
    )
    render.add_argument(
        "--photometry", default="lunar_lambert",
        choices=["lambert", "lommel_seeliger", "lunar_lambert", "hapke"],
    )
    render.add_argument(
        "--tone", default="srgb", choices=["linear", "srgb", "reinhard", "asinh", "visual"],
        help="'srgb'/'linear' are radiometric, the rest compress the range "
             "until the earthshine-lit hemisphere is visible",
    )
    render.add_argument("--exposure", type=float, default=0.0, help="exposure in stops")
    render.add_argument(
        "--no-auto-exposure", action="store_true",
        help="keep the physical exposure instead of exposing the sunlit side",
    )
    render.add_argument(
        "--no-earthshine", action="store_true", help="disable earthshine entirely"
    )
    render.add_argument(
        "--earthshine-out", help="also write the earthshine component to this PNG"
    )
    render.add_argument(
        "--sunlit-out", help="also write the sunlit component to this PNG"
    )
    render.add_argument(
        "--hdr-out", help="also write the linear I/F radiance stack to this .npy"
    )
    render.add_argument(
        "--sun-samples", type=int, default=1,
        help="samples across the solar disk; >1 resolves the shadow penumbra",
    )
    render.add_argument("--shadow-steps", type=int, default=48)
    render.add_argument("--shadow-range-km", type=float, default=220.0)
    render.add_argument("--earth-samples", type=int, default=13)
    render.add_argument("--earthshine-gain", type=float, default=1.0)
    render.add_argument(
        "--earth-geometric-albedo", type=float, default=None,
        help="geometric albedo of the Earth used by the earthshine term; "
             "omitted means the model default (0.367, the value the ephemeris "
             "assumes).  It scales earthshine linearly, so it is only worth "
             "moving when you are deliberately re-weighting the ashen light",
    )
    render.add_argument("--normal-albedo", type=float, default=None)
    render.add_argument("--ambient", type=float, default=0.0)
    render.add_argument(
        "--sky-ambient", type=float, default=0.0,
        help="skylight reaching shadowed ground, as a fraction of the sunlight "
             "(1.0 is about 6%% on flat ground); 0 disables the term",
    )
    render.add_argument("--sky-samples", type=int, default=26)
    render.add_argument(
        "--roughness", type=float, default=0.0,
        help="mean surface slope in degrees for micro-relief shadowing "
             "(15-25 for lunar soil); 0 disables it",
    )
    render.add_argument("--pyramid-level", type=int, default=None)
    render.add_argument(
        "--asinh-softening", type=float, default=None,
        help="smaller values compress harder and reveal fainter detail",
    )
    render.add_argument(
        "--limb-grid", action="store_true",
        help="overlay the selenographic graticule (needs matplotlib)",
    )
    render.add_argument(
        "--target-lon", type=float,
        help="close-up: selenographic longitude of the frame centre",
    )
    render.add_argument(
        "--target-lat", type=float,
        help="close-up: selenographic latitude of the frame centre",
    )
    render.add_argument(
        "--span-km", type=float, default=200.0,
        help="close-up: surface extent across the shorter side of the frame",
    )
    render.add_argument(
        "--region-dem",
        help="use a high-resolution regional DEM window (tools/fetch_lola_global.py)",
    )
    render.add_argument(
        "--feature",
        help="close-up sized to fit a named landform (e.g. Clavius)",
    )
    render.add_argument(
        "--margin", type=float, default=1.25,
        help="breathing room around --feature, as a multiple of its diameter",
    )
    render.add_argument(
        "--list-features", action="store_true",
        help="list the landforms a close-up can be sized from, then exit",
    )
    render.add_argument(
        "--list-regions", action="store_true",
        help="list the regional DEM windows that are available, then exit",
    )
    render.add_argument(
        "--mesh-out", help="write the visible-hemisphere 3D mesh to this .ply or .obj"
    )
    render.add_argument("--mesh-level", type=int, default=3)

    terrain_cmd = commands["terrain"]
    _add_output_options(terrain_cmd)
    terrain_cmd.add_argument(
        "--bake", action="store_true", help="build or refresh the pyramid cache"
    )
    terrain_cmd.add_argument(
        "--rebuild", action="store_true", help="force a rebuild even if one exists"
    )
    terrain_cmd.add_argument(
        "--clear", action="store_true", help="delete every baked pyramid"
    )
    terrain_cmd.add_argument(
        "--detail", type=int, default=1,
        help="log2 decimation of the finest cached topography level",
    )
    terrain_cmd.add_argument("--albedo-detail", type=int, default=2)
    terrain_cmd.add_argument(
        "--no-albedo", action="store_true", help="bake topography only"
    )
    terrain_cmd.add_argument(
        "--cache-dir", help="terrain cache directory (default <repo>/.cache/moonlib/terrain)"
    )

    return parser


# ---------------------------------------------------------------------------
# input helpers
# ---------------------------------------------------------------------------
def _observer(args: argparse.Namespace):
    from .service import parse_observer

    return parse_observer({"lat": args.lat, "lon": args.lon, "height_km": args.height})


def _period(args: argparse.Namespace) -> Any:
    """Turn ``--year`` / ``--from`` / ``--to`` into something ``as_time_range`` accepts."""
    year = getattr(args, "year", None)
    if year is not None:
        return year
    start = getattr(args, "start", None)
    end = getattr(args, "end", None)
    if start is None and end is None:
        return None
    if start is not None and end is None:
        return int(start) if str(start).isdigit() else start
    if start is None:
        return None
    if str(start).isdigit() and str(end).isdigit():
        return (int(start), int(end))
    return (start, end)


def _emit_json(payload: dict) -> int:
    from .service import dumps

    print(dumps(payload, indent=2))
    return 0


def _print(text: str) -> int:
    print(text)
    return 0


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def _cmd_info(args: argparse.Namespace) -> int:
    from .service import environment

    payload = environment()
    if args.json:
        return _emit_json(payload)

    data = payload["data"]
    # `coverage_text` is the SPICE calendar form and works for BC epochs
    # (DE441 starts about 13 200 BC); `coverage_utc` is null in that case.
    span = data.get("coverage_text") or data.get("coverage_utc")
    lines = [
        "=" * 74,
        f"  moonlib {payload['library_version']}    payload schema {payload['schema']}",
        "=" * 74,
        f"  kernel directory : {data['kernel_dir']}",
        f"  kernel mode      : {data['mode']}",
        "  ephemeris span   : "
        + (f"{span[0]} .. {span[1]}" if span else "unknown"),
        f"  loaded kernels   : {len(data['loaded'])}",
    ]
    lines.extend(f"      {name}" for name in data["loaded"])
    constants = data.get("constants", {})
    if constants:
        lines.append(
            f"  moon radius      : {constants['moon_radius_km']:.3f} km"
        )
        lines.append(
            f"  earth radius     : {constants['earth_radius_km']:.3f} km"
            f"    flattening {constants['earth_flattening']:.9f}"
        )
    lines.append("=" * 74)
    return _print("\n".join(lines))


def _cmd_moon(args: argparse.Namespace) -> int:
    from . import reporting
    from .moon import moon_report, moon_state

    if args.json:
        from .service import lunar_report, lunar_state

        if args.state_only:
            return _emit_json(lunar_state(_observer(args), args.at))
        return _emit_json(
            lunar_report(
                _observer(args),
                args.at,
                rise_set_mode=args.rise_set_mode,
                with_transit_details=not args.no_transit_details,
            )
        )

    if args.state_only:
        return _print(
            reporting.format_moon_state(
                moon_state(_observer(args), args.at), args.language
            )
        )
    report = moon_report(
        _observer(args),
        args.at,
        rise_set_mode=args.rise_set_mode,
        with_transit_details=not args.no_transit_details,
    )
    return _print(reporting.format_moon_report(report, args.language))


def _cmd_phases(args: argparse.Namespace) -> int:
    from . import reporting
    from .models import PhaseKind
    from .phases import phase_calendar as _phase_calendar
    from .timeutil import as_time_range, utc_now

    period = _period(args)
    window = as_time_range(period if period is not None else utc_now().year)
    kinds = tuple(PhaseKind(name) for name in args.kinds) if args.kinds else None

    if args.json:
        from .service import phase_calendar

        return _emit_json(phase_calendar(_observer(args), window, kinds=args.kinds))
    entries = _phase_calendar(window, _observer(args), kinds=kinds)
    return _print(reporting.format_phase_calendar(entries, _observer(args), args.language))


def _cmd_crescent(args: argparse.Namespace) -> int:
    from . import reporting
    from .crescent import CrescentCriteria, find_crescent_windows
    from .timeutil import as_time_range

    criteria = CrescentCriteria(
        illumination_min_percent=args.illum_min,
        illumination_max_percent=args.illum_max,
        sun_elevation_min_deg=args.sun_elev_min,
        sun_elevation_max_deg=args.sun_elev_max,
        moon_elevation_min_deg=args.moon_elev_min,
        step_minutes=args.step_minutes,
        scan_days_around_new_moon=args.scan_days,
        refine=not args.no_refine,
    )
    period = _period(args)
    window = as_time_range(period if period is not None else None)

    if args.json:
        from .service import crescent_windows

        return _emit_json(
            crescent_windows(
                _observer(args),
                window,
                criteria={
                    "illumination_min_percent": args.illum_min,
                    "illumination_max_percent": args.illum_max,
                    "sun_elevation_min_deg": args.sun_elev_min,
                    "sun_elevation_max_deg": args.sun_elev_max,
                    "moon_elevation_min_deg": args.moon_elev_min,
                    "step_minutes": args.step_minutes,
                    "scan_days_around_new_moon": args.scan_days,
                    "refine": not args.no_refine,
                },
                phase_mode=args.phase_mode,
            )
        )
    sessions = find_crescent_windows(
        _observer(args), window, criteria=criteria, phase_mode=args.phase_mode
    )
    return _print(
        reporting.format_crescent_windows(
            sessions,
            _observer(args),
            language=args.language,
            ranked=args.ranked,
            top_n=args.top,
        )
    )


def _cmd_illumination(args: argparse.Namespace) -> int:
    from . import reporting
    from .phases import find_moons_by_illumination
    from .timeutil import as_time_range, utc_now

    period = _period(args)
    window = as_time_range(period if period is not None else utc_now().year)

    if args.json:
        from .service import illumination_matches

        return _emit_json(
            illumination_matches(
                args.percent,
                _observer(args),
                window,
                waxing=args.waxing,
                phase_mode=args.phase_mode,
                phase_tolerance_deg=args.tolerance,
            )
        )
    matches = find_moons_by_illumination(
        args.percent,
        window,
        _observer(args),
        waxing=args.waxing,
        phase_mode=args.phase_mode,
        phase_tolerance_deg=args.tolerance,
    )
    return _print(reporting.format_illumination_matches(matches, args.language))


def _cmd_libration(args: argparse.Namespace) -> int:
    from .libration import load_targets, summarize, sweep
    from .models import VisibilityGrade
    from .timeutil import as_time_range

    if args.targets:
        if args.json:
            from .service import limb_targets

            return _emit_json(limb_targets())
        rows = [
            f"{target.english_name:<24} {target.name_zh:<12} "
            f"lat={target.lat_deg:+7.2f} lon={target.lon_deg:+8.2f} "
            f"r_eff={target.effective_radius_deg:6.3f}  {target.region}"
            for target in load_targets()
        ]
        return _print("\n".join(rows))

    period = _period(args)
    window = as_time_range(period if period is not None else 30)
    grade = VisibilityGrade(args.min_grade)

    if args.json:
        from .service import limb_target_sweep

        return _emit_json(
            limb_target_sweep(
                _observer(args),
                window,
                step_minutes=args.step_minutes,
                min_grade=args.min_grade,
                min_moon_elevation_deg=args.min_moon_elev,
                max_solar_incidence_deg=args.max_solar_incidence,
                include_samples=args.include_samples,
            )
        )

    from . import reporting

    targets = load_targets()
    samples = sweep(targets, window, _observer(args), step_minutes=args.step_minutes)
    rows = summarize(samples, targets, min_grade=grade)
    if args.top and args.top > 0:
        rows = rows[: args.top]
    return _print(reporting.format_target_summary(rows, args.language))


def _cmd_analemma(args: argparse.Namespace) -> int:
    from . import reporting
    from .analemma import analemma_extremes, solar_analemma
    from .timeutil import as_time_range, utc_now

    period = _period(args)
    window = as_time_range(period if period is not None else utc_now().year)

    if args.json:
        from .service import analemma

        return _emit_json(
            analemma(
                _observer(args),
                window,
                local_hour=args.hour,
                local_minute=args.minute,
                sample_days=args.sample_days,
            )
        )
    points = solar_analemma(
        _observer(args),
        window,
        local_hour=args.hour,
        local_minute=args.minute,
        sample_days=args.sample_days,
    )
    return _print(
        reporting.format_analemma_extremes(analemma_extremes(points), args.language)
    )


def _cmd_sunpoint(args: argparse.Namespace) -> int:
    """The Sun above one point on the Moon, once or over a span."""
    import moonlib

    site = moonlib.Observer(lat=args.lat, lon=args.lon, height_km=args.height)
    if args.end or args.start:
        if not (args.start and args.end):
            raise MoonlibError("--from and --to must be given together.")
        rows = moonlib.lunar_surface_solar_series(
            args.moon_lon, args.moon_lat, args.start, args.end,
            step_minutes=args.step_minutes, observer=site,
        )
    else:
        rows = [moonlib.lunar_surface_solar(
            args.moon_lon, args.moon_lat, args.at, site,
        )]
    if args.json:
        from .service import envelope
        return _emit_json(envelope(
            "sunpoint",
            {
                "moon_lon": args.moon_lon, "moon_lat": args.moon_lat,
                "observer": site.as_dict(),
                "at": args.at, "start": args.start, "end": args.end,
                "step_minutes": args.step_minutes,
            },
            [row.to_dict() for row in rows],
        ))
    from .reporting import _RULE
    _print(_RULE)
    _print(f"  月面点 / Lunar point: {args.moon_lon:+.3f}°, {args.moon_lat:+.3f}°")
    _print(_RULE)
    for row in rows:
        _print(
            f"  {row.when_utc.isoformat(timespec='minutes'):<17}"
            f"  高度 {row.altitude_deg:+7.2f}°"
            f"  方位 {row.azimuth_deg:7.2f}°"
            f"  {'阳光' if row.illuminated else '阴影'}"
        )
    _print(_RULE)
    return 0


def _cmd_plot(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .plotting import close_figure, plot_sky

    figure = plot_sky(
        _observer(args),
        args.at,
        at_local_sunset=args.sunset,
        span_hours=args.span_hours,
    )
    path = Path(args.out)
    figure.savefig(path, dpi=args.dpi, facecolor=figure.get_facecolor())
    close_figure(figure)
    print(f"wrote {path} ({path.stat().st_size:,} bytes)")
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    from .models import MoonRenderSettings
    from .moonview import render_moon, view_geometry

    if args.list_features:
        from .features import load_features
        from .reporting import _RULE

        _print(_RULE)
        _print("  可自动取景的地貌 / Landforms with a published diameter")
        _print(_RULE)
        for item in load_features():
            _print(
                f"  {item.name_en:<14} {item.name_zh:<18} "
                f"{item.lat_deg:+7.2f} {item.lon_deg:+8.2f}  "
                f"{item.diameter_km:6.0f} km  {item.kind}"
            )
        _print(_RULE)
        return 0

    if args.list_regions:
        from .reporting import _RULE
        from .terrain import global_dem_sources

        _print(_RULE)
        _print("  本地全球高程 / Local global DEMs")
        _print(_RULE)
        found = global_dem_sources()
        if not found:
            _print("  none; run tools/fetch_lola_global.py for 118 m/px coverage")
        for path, km in found:
            _print(f"  {path.name:<52} {km * 1000:7.1f} m/px")
        _print(_RULE)
        return 0

    close_up = (
        args.target_lon is not None
        or args.target_lat is not None
        or bool(args.feature)
    )
    if args.feature and (args.target_lon is not None or args.target_lat is not None):
        _print("  use either --feature or --target-lon/--target-lat, not both")
        return 1
    if not args.feature and close_up and (
        args.target_lon is None or args.target_lat is None
    ):
        _print("  --target-lon and --target-lat must be given together")
        return 1
    if args.feature:
        from .errors import TargetDataError
        from .features import find_feature

        try:
            resolved = find_feature(args.feature)
        except TargetDataError as error:
            _print(f"  error: {error}")
            return 1
        args.target_lon, args.target_lat = resolved.lon_deg, resolved.lat_deg

    site = _observer(args)
    view = view_geometry(site, args.at, viewpoint=args.viewpoint)

    if close_up and args.json:
        from .service import moon_feature, moon_region

        if args.feature:
            return _emit_json(
                moon_feature(
                    site,
                    args.at,
                    feature=args.feature,
                    viewpoint=args.viewpoint,
                    margin=args.margin,
                    width=args.width,
                    height=args.height_px or args.width,
                    samples=args.samples,
                    photometric_model=args.photometry,
                    tone=args.tone,
                    exposure_ev=args.exposure,
                    auto_exposure=not args.no_auto_exposure,
                    earthshine=not args.no_earthshine,
                    earth_geometric_albedo=args.earth_geometric_albedo,
                    normal_albedo=args.normal_albedo,
                    sun_samples=args.sun_samples,
                    shadow_steps=args.shadow_steps,
                    pyramid_level=args.pyramid_level,
                )
            )

        return _emit_json(
            moon_region(
                site,
                args.at,
                viewpoint=args.viewpoint,
                lon=args.target_lon,
                lat=args.target_lat,
                span_km=args.span_km,
                width=args.width,
                height=args.height_px or args.width,
                samples=args.samples,
                photometric_model=args.photometry,
                tone=args.tone,
                exposure_ev=args.exposure,
                auto_exposure=not args.no_auto_exposure,
                earthshine=not args.no_earthshine,
                earth_geometric_albedo=args.earth_geometric_albedo,
                normal_albedo=args.normal_albedo,
                sun_samples=args.sun_samples,
                shadow_steps=args.shadow_steps,
                region_dem=args.region_dem,
                pyramid_level=args.pyramid_level,
            )
        )

    if args.json:
        from .service import moon_view

        return _emit_json(
            moon_view(
                site,
                args.at,
                viewpoint=args.viewpoint,
                width=args.width,
                height=args.height_px or args.width,
                samples=args.samples,
                photometric_model=args.photometry,
                tone=args.tone,
                exposure_ev=args.exposure,
                auto_exposure=not args.no_auto_exposure,
                earthshine=not args.no_earthshine,
                earth_samples=args.earth_samples,
                earthshine_gain=args.earthshine_gain,
                earth_geometric_albedo=args.earth_geometric_albedo,
                normal_albedo=args.normal_albedo,
                sun_samples=args.sun_samples,
                shadow_steps=args.shadow_steps,
                ambient_fraction=args.ambient,
                sky_ambient=args.sky_ambient,
                sky_samples=args.sky_samples,
                roughness_deg=args.roughness,
                pyramid_level=args.pyramid_level,
                asinh_softening=args.asinh_softening,
            )
        )

    from .reporting import format_moon_view, format_region_summary, format_render_summary

    _print(format_moon_view(view, args.language))

    if close_up:
        from .moonview import region_geometry, required_sky_span_km
        from .terrain import open_region

        target_terrain = open_region(args.region_dem) if args.region_dem else None
        try:
            if args.feature:
                geometry = region_geometry(
                    view, args.target_lon, args.target_lat,
                    required_sky_span_km(
                        view, args.target_lon, args.target_lat,
                        resolved.diameter_km, margin=args.margin,
                        terrain=target_terrain,
                    ),
                    terrain=target_terrain,
                )
            else:
                geometry = region_geometry(
                    view, args.target_lon, args.target_lat, args.span_km,
                    terrain=target_terrain,
                )
        except MoonlibError as error:
            _print(f"  error: {error}")
            return 1
        _print(
            f"  特写目标 / Target       : ({geometry.lon_deg:+.3f}, {geometry.lat_deg:+.3f})  "
            f"emergence {geometry.emergence_deg:.2f}°, "
            f"sun elevation {geometry.solar_elevation_deg:+.2f}°, "
            f"fov {geometry.fov_arcsec:.1f}\""
        )

    if not args.out:
        if args.mesh_out:
            return _write_mesh(args, view)
        return 0

    settings = MoonRenderSettings(
        width=args.width,
        height=args.height_px or args.width,
        samples_per_pixel=args.samples,
        photometric_model=args.photometry,
        tone=args.tone,
        exposure_ev=args.exposure,
        auto_exposure=not args.no_auto_exposure,
        earthshine=not args.no_earthshine,
        earth_samples=args.earth_samples,
        earthshine_gain=args.earthshine_gain,
        sun_samples=args.sun_samples,
        shadow_steps=args.shadow_steps,
        shadow_range_km=args.shadow_range_km,
        ambient_fraction=args.ambient,
        sky_ambient=args.sky_ambient,
        sky_samples=args.sky_samples,
        roughness_deg=args.roughness,
        pyramid_level=args.pyramid_level,
        asinh_softening=args.asinh_softening or 1.0e-3,
        **({} if args.normal_albedo is None else {"normal_albedo": args.normal_albedo}),
        **({} if args.earth_geometric_albedo is None
           else {"earth_geometric_albedo": args.earth_geometric_albedo}),
    )

    def report(fraction: float, message: str) -> None:
        if fraction < 1.0:
            print(f"  {message} … {fraction * 100:.0f}%", file=sys.stderr)

    if close_up:
        from .moonview import render_feature, render_region

        terrain = open_region(args.region_dem) if args.region_dem else None
        if args.feature:
            render = render_feature(
                view, resolved, margin=args.margin, settings=settings,
                terrain=terrain, progress=report,
            )
        else:
            render = render_region(
                view, args.target_lon, args.target_lat, span_km=args.span_km,
                settings=settings, terrain=terrain, progress=report,
            )
        _print(format_region_summary(render, args.language))
    else:
        render = render_moon(site, args.at, settings=settings, view=view, progress=report)
        _print(format_render_summary(render, args.language))

    from .plotting import write_radiance_npy, write_radiance_png

    if args.limb_grid:
        from .plotting import close_figure, plot_moon_view

        figure = plot_moon_view(render, show_limb_grid=True)
        figure.savefig(args.out, dpi=110, facecolor=figure.get_facecolor())
        close_figure(figure)
    else:
        write_radiance_png(render, args.out)
    _print(f"  wrote {args.out}")
    if args.sunlit_out:
        write_radiance_png(render, args.sunlit_out, component="sunlit")
        _print(f"  wrote {args.sunlit_out}")
    if args.earthshine_out:
        write_radiance_png(render, args.earthshine_out, component="earthshine")
        _print(f"  wrote {args.earthshine_out}")
    if args.hdr_out:
        write_radiance_npy(render, args.hdr_out)
        _print(f"  wrote {args.hdr_out}")
    if args.mesh_out:
        return _write_mesh(args, view)
    return 0


def _write_mesh(args: argparse.Namespace, view: Any) -> int:
    from pathlib import Path

    from .errors import MoonlibError
    from .terrain import open_terrain

    terrain = open_terrain()
    mesh = terrain.visible_mesh(view, level=args.mesh_level)
    target = Path(args.mesh_out)
    if target.suffix.lower() == ".obj":
        mesh.export_obj(target)
    elif target.suffix.lower() == ".ply":
        mesh.export_ply(target)
    else:
        raise MoonlibError("--mesh-out must end in .ply or .obj")
    _print(
        f"  wrote {target}  ({mesh.n_vertices} vertices, {mesh.n_faces} faces)"
    )
    return 0


def _cmd_terrain(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .terrain import (
        bake_terrain_cache,
        clear_terrain_cache,
        find_sources,
        open_terrain,
        terrain_cache_root,
    )

    cache = Path(args.cache_dir) if args.cache_dir else None

    if args.clear:
        removed = clear_terrain_cache(cache)
        if args.json:
            from .service import envelope

            return _emit_json(
                envelope("terrain_clear", {}, {"removed": removed})
            )
        _print(f"  removed {removed} cached pyramid(s) from {terrain_cache_root(cache)}")
        return 0

    if args.bake:
        def report(fraction: float, message: str) -> None:
            print(f"  {message} … {fraction * 100:.0f}%", file=sys.stderr)

        sources = find_sources()
        target = bake_terrain_cache(
            sources,
            cache=cache,
            detail=args.detail,
            albedo_detail=args.albedo_detail,
            include_albedo=not args.no_albedo,
            rebuild=args.rebuild,
            progress=report,
        )
        if args.json:
            from .service import envelope

            return _emit_json(
                envelope(
                    "terrain_bake",
                    {"detail": args.detail, "albedo_detail": args.albedo_detail,
                     "include_albedo": not args.no_albedo},
                    {"cache_path": str(target)},
                )
            )
        _print(f"  cache ready at {target}")
        return 0

    if args.json:
        from .service import terrain_info

        return _emit_json(terrain_info(cache))
    try:
        terrain = open_terrain(cache=cache, bake=False)
    except MoonlibError as error:
        _print(f"  {error}")
        return 1
    from .reporting import _RULE  # local: CLI text only

    _print(_RULE)
    _print("  月面地形 / Lunar terrain")
    _print(_RULE)
    info = terrain.as_dict()
    _print(f"  高程 / Topography   : {info['sources']['dem']}")
    _print(f"  纹理 / Colour       : {info['sources']['albedo']}")
    _print(f"  缓存 / Cache        : {info['cache_path']}")
    for name in ("dem", "albedo"):
        block = info.get(name)
        if not block:
            continue
        shape = "x".join(str(v) for v in block["level0_shape"])
        _print(
            f"  {name:<19}: {shape}  level 0  "
            f"({block['level0_km_per_pixel']:.3f} km/px), "
            f"{block['n_levels']} level(s)"
        )
    _print(_RULE)
    return 0


_COMMANDS = {
    "info": _cmd_info,
    "moon": _cmd_moon,
    "phases": _cmd_phases,
    "crescent": _cmd_crescent,
    "illumination": _cmd_illumination,
    "libration": _cmd_libration,
    "analemma": _cmd_analemma,
    "sunpoint": _cmd_sunpoint,
    "plot": _cmd_plot,
    "render": _cmd_render,
    "terrain": _cmd_terrain,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the CLI and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        from .kernels import init_kernels

        init_kernels(
            kernel_dir=args.kernel_dir,
            mode=args.kernel_mode,
            # --json must leave stdout parseable, so kernel-loading diagnostics
            # are suppressed even without --quiet.
            verbose=not args.quiet and not getattr(args, "json", False),
        )
        return _COMMANDS[args.command](args)
    except MoonlibError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
