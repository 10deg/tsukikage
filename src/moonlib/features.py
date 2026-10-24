"""Named lunar landforms and automatic measurement of their size.

A close-up is only useful if the frame is sized to the subject: too small and
the feature is cropped, too large and it is a few pixels in a field of rubble.
That needs a diameter, and diameters come from two places:

* the **catalogue** in ``data/lunar_features.csv`` -- published IAU values for
  a few dozen showpiece targets;
* **measurement from the DEM** for anything not in it, via
  :func:`estimate_diameter_km`, which profiles the topography radially from the
  given point and looks for the rim crest.

The estimator is deliberately simple and its result is reported with a
contrast figure so a caller can tell a confident measurement from a shrug.  It
also serves as an independent check on the catalogue: the test suite compares
the two for the crisp, well-formed craters, which would catch a mistyped
diameter in either place.
"""

from __future__ import annotations

import csv
import math
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .config import package_data_dir
from .errors import TargetDataError
from .models import SurfaceFeature
from .terrain import MOON_REFERENCE_RADIUS_KM, MoonTerrain

__all__ = [
    "FEATURE_FILE",
    "load_features",
    "feature_names",
    "find_feature",
    "estimate_diameter_km",
    "feature_from_measurement",
]

#: Catalogue of showpiece landforms shipped with the package.
FEATURE_FILE = "lunar_features.csv"


def _catalogue_path() -> Path:
    return package_data_dir() / FEATURE_FILE


def load_features(path: Optional[Any] = None) -> Tuple[SurfaceFeature, ...]:
    """Read the shipped catalogue of named landforms.

    Parameters
    ----------
    path
        Override the catalogue file; useful for a user-supplied list with the
        same columns.

    Raises
    ------
    TargetDataError
        When the file is missing or a row cannot be parsed.
    """
    source = Path(path) if path is not None else _catalogue_path()
    if not source.is_file():
        raise TargetDataError(f"Feature catalogue not found: {source}")
    features: List[SurfaceFeature] = []
    try:
        with source.open("r", encoding="utf-8", newline="") as handle:
            for row_number, row in enumerate(csv.DictReader(handle), start=2):
                name = (row.get("English Name") or "").strip()
                if not name:
                    continue
                try:
                    lat = float(row["Lat"])
                    lon = float(row["Lon"])
                    diameter = float(row["Diameter"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise TargetDataError(
                        f"{source.name} row {row_number}: cannot read Lat/Lon/"
                        f"Diameter for {name!r} ({exc})."
                    ) from exc
                if not (-90.0 <= lat <= 90.0):
                    raise TargetDataError(
                        f"{source.name} row {row_number}: latitude {lat} is off "
                        "the globe."
                    )
                if diameter <= 0.0:
                    raise TargetDataError(
                        f"{source.name} row {row_number}: diameter {diameter} "
                        "must be positive."
                    )
                features.append(
                    SurfaceFeature(
                        name_en=name,
                        name_zh=(row.get("名称") or "").strip(),
                        lat_deg=lat,
                        lon_deg=((lon + 180.0) % 360.0) - 180.0,
                        diameter_km=diameter,
                        kind=(row.get("Type") or "Crater").strip(),
                        note=(row.get("Note") or "").strip(),
                    )
                )
    except OSError as exc:  # pragma: no cover - racing deletion
        raise TargetDataError(f"Cannot read {source}: {exc}") from exc
    if not features:
        raise TargetDataError(f"{source} contains no usable features.")
    return tuple(features)


def feature_names() -> Tuple[str, ...]:
    """Every name in the catalogue, English first."""
    names: List[str] = []
    for feature in load_features():
        names.append(feature.name_en)
        if feature.name_zh:
            names.append(feature.name_zh)
    return tuple(names)


def find_feature(name: str) -> SurfaceFeature:
    """Look a feature up by English or Chinese name.

    Matching ignores case, spaces and the generic suffixes people habitually
    add ("Clavius crater"), because that is how the name arrives from a command
    line.
    """
    wanted = _normalise(name)
    catalogue = load_features()
    for feature in catalogue:
        if _normalise(feature.name_en) == wanted:
            return feature
    for feature in catalogue:
        if feature.name_zh and _normalise(feature.name_zh) == wanted:
            return feature
    # Fall back to a substring match so "clavius" finds "Clavius", and so a
    # feature known by more than one name answers to either of them.
    for feature in catalogue:
        for candidate in (feature.name_en, feature.name_zh):
            if candidate and wanted in _normalise(candidate):
                return feature
    raise TargetDataError(
        f"No feature named {name!r} in the catalogue. Try one of: "
        + ", ".join(f.name_en for f in catalogue[:12])
        + ", ..."
    )


def _normalise(text: str) -> str:
    """Fold a name to a comparable key.

    Accents are stripped so that "Vallis Schroteri" finds "Vallis Schröteri",
    which matters because the name is far more often typed without the umlaut
    than with it.
    """
    drop = ("crater", "ring", "环形山", "陨石坑", "山", "the")
    folded = unicodedata.normalize("NFKD", str(text))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    cleaned = folded.strip().lower()
    for suffix in drop:
        cleaned = cleaned.replace(suffix, "")
    return "".join(cleaned.split())


# ---------------------------------------------------------------------------
# measurement from the DEM
# ---------------------------------------------------------------------------
def estimate_diameter_km(
    terrain: MoonTerrain,
    lon_deg: float,
    lat_deg: float,
    *,
    search_km: float = 260.0,
    step_km: float = 2.0,
    azimuths: int = 72,
    level: int = 2,
) -> Dict[str, Any]:
    """Measure the diameter of the landform centred near a point.

    The method is a radial profile: average the elevation on rings of
    increasing radius and look for the ring that stands highest above both the
    interior and the surroundings.  For a crater that ring is the rim crest, so
    twice its radius is the diameter.  It is the same thing an observer does
    with a crosshair and a measuring scale.

    Parameters
    ----------
    terrain
        Opened terrain; the finer its level 0, the smaller the feature this can
        resolve.
    lon_deg, lat_deg
        Approximate centre.  A crater's published coordinates are its centre,
        so no searching is needed in practice.
    search_km
        How far out to look for a rim, in kilometres of surface distance.
    step_km
        Radial sampling step.  Sets the precision of the result.
    azimuths
        How many directions to average over.  More is smoother but slower.
    level
        Pyramid level used for the profile.

    Returns
    -------
    dict
        ``diameter_km``, ``rim_radius_km``, ``rim_relief_km`` (how far the rim
        stands above the interior) and ``contrast`` (rim relief relative to the
        whole profile range).  A low contrast means the "rim" is not
        convincing, which is what a heavily degraded or non-crater target looks
        like.
    """
    if search_km <= 0.0 or step_km <= 0.0 or azimuths < 8:
        raise TargetDataError(
            "estimate_diameter_km needs a positive search radius and step and "
            "at least 8 azimuths."
        )
    radii = np.arange(step_km, search_km + step_km, step_km)
    theta = np.linspace(0.0, 2.0 * math.pi, int(azimuths), endpoint=False)
    # Walk outwards on the sphere from the target: a step of d km in azimuth a
    # moves d*sin(a)/R radians east and d*cos(a)/R radians north.
    radius_rad = radii / MOON_REFERENCE_RADIUS_KM
    lon = np.radians(np.asarray(lon_deg, dtype=np.float64)) + np.outer(
        radius_rad, np.sin(theta) / np.cos(np.radians(lat_deg))
    )
    lat = np.radians(np.asarray(lat_deg, dtype=np.float64)) + np.outer(
        radius_rad, np.cos(theta)
    )
    lats = np.clip(np.degrees(lat), -89.99, 89.99)
    heights = np.asarray(
        terrain.height_km(np.degrees(lon), lats, level), dtype=np.float64
    )
    profile = heights.mean(axis=1)

    if profile.size < 5:  # pragma: no cover - guarded by the step argument
        raise TargetDataError("search_km/step_km leave too few samples.")

    interior = float(np.mean(profile[: max(1, profile.size // 8)]))
    # The rim is the highest ring in the inner two thirds of the search window;
    # beyond that the profile has usually left the feature behind.
    inner = profile[: max(3, int(round(profile.size * 0.66)))]
    rim_index = int(np.argmax(inner))
    rim_radius = float(radii[rim_index])
    rim_height = float(profile[rim_index])
    outer = float(np.mean(profile[-max(1, profile.size // 8):]))
    relief = rim_height - interior
    span = float(np.max(profile) - np.min(profile))
    contrast = relief / span if span > 0.0 else 0.0
    return {
        "diameter_km": 2.0 * rim_radius,
        "rim_radius_km": rim_radius,
        "rim_relief_km": relief,
        "rim_height_km": rim_height,
        "interior_km": interior,
        "outer_km": outer,
        "contrast": float(contrast),
        "level": int(level),
    }


def feature_from_measurement(
    terrain: MoonTerrain,
    lon_deg: float,
    lat_deg: float,
    *,
    name: Optional[str] = None,
    search_km: float = 260.0,
    level: int = 2,
) -> SurfaceFeature:
    """Build a :class:`SurfaceFeature` by measuring the DEM at a point."""
    measured = estimate_diameter_km(
        terrain, lon_deg, lat_deg, search_km=search_km, level=level
    )
    label = name or f"({lon_deg:+.2f}, {lat_deg:+.2f})"
    return SurfaceFeature(
        name_en=label,
        name_zh="",
        lat_deg=float(lat_deg),
        lon_deg=float(lon_deg),
        diameter_km=float(measured["diameter_km"]),
        kind="Measured",
        note=(
            f"measured from the DEM at level {measured['level']}; rim relief "
            f"{measured['rim_relief_km']:.2f} km, contrast "
            f"{measured['contrast']:.2f}"
        ),
        diameter_source="dem",
    )
