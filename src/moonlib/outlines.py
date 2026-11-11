"""Landform outlines shipped beside the gazetteer.

The IAU outline download (``tools/fetch_moon_outlines.py``) leaves two files:
an untouched archive and a drawing copy whose per-feature ``label`` says how
the shape should be drawn and typeset.  The map page and the simulator both
want the drawing copy, because it is already reduced to the geometry a
renderer needs: a circle for a crater, a path for a rille, a ring for a mare.

Nothing here parses or validates that file at import time: a checkout without
the download must still start, just without outlines.  :func:`outline_index`
returns an empty mapping in that case and callers fall back to their own
geometry (the simulator's projected rim, the map's sized markers).
"""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .config import package_data_dir, _project_root

__all__ = [
    "OUTLINE_FILE",
    "outline_index",
    "outline_for",
    "outlines_available",
    "normalise_name",
]

#: File name looked for in the package data directory, then in ``out/``.
OUTLINE_FILE = "moon_outlines.geojson"

_LOCK = threading.RLock()
_CACHE: Optional[Tuple[float, Dict[str, Dict[str, Any]]]] = None


def normalise_name(name: str) -> str:
    """Case- and space-insensitive key for matching a gazetteer row."""
    return " ".join(str(name or "").split()).casefold()


def _candidates() -> Tuple[Path, ...]:
    root = _project_root()
    paths = [package_data_dir() / OUTLINE_FILE]
    if root is not None:
        paths.append(root / "out" / OUTLINE_FILE)
    return tuple(paths)


def _read(path: Path) -> Dict[str, Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    index: Dict[str, Dict[str, Any]] = {}
    for feature in data.get("features") or ():
        properties = feature.get("properties") or {}
        label = properties.get("label") or {}
        geometry = feature.get("geometry") or {}
        if not label or not geometry:
            continue
        entry = {
            "name": properties.get("name") or "",
            "type": properties.get("type") or "",
            "kind": label.get("kind") or "point",
            "center": label.get("center"),
            "radius_km": label.get("radius_km"),
            "radius_deg": label.get("radius_deg"),
            "radius_deg_lon": label.get("radius_deg_lon"),
            "rotation_deg": label.get("rotation_deg"),
            "path": label.get("path"),
            #: A closed IAU ring, kept beside the older path for long mountain
            #: chains: the ring is what gets painted *and* what the name follows.
            #: ``None`` for the features whose only shape is a trace.
            "ring": label.get("ring"),
            #: True when the download only had a bounding box (no trace): the
            #: entry still gives a centre and a heading, but there is no shape
            #: to draw and drawing one would invent a rille.
            "bbox_only": bool(label.get("bbox_only")),
            "geometry": geometry,
        }
        for key in (properties.get("name"), properties.get("clean_name")):
            key = normalise_name(key)
            if key and key not in index:
                index[key] = entry
    return index


def outline_index() -> Dict[str, Dict[str, Any]]:
    """Name -> outline entry, or an empty mapping when the download is absent."""
    global _CACHE
    for path in _candidates():
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        with _LOCK:
            if _CACHE is not None and _CACHE[0] == stamp:
                return _CACHE[1]
            try:
                index = _read(path)
            except (OSError, ValueError):
                index = {}
            _CACHE = (stamp, index)
            return index
    with _LOCK:
        _CACHE = (0.0, {})
    return {}


def outline_for(name: str) -> Optional[Dict[str, Any]]:
    return outline_index().get(normalise_name(name))


def outlines_available() -> bool:
    return bool(outline_index())
