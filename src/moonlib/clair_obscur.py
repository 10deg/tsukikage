"""Data-driven Clair-Obscur effect catalogue.

The built-in table is ``data/clair_obscur.csv``; a user table with the same
columns may sit beside it or anywhere the caller names.  This module only
reads and normalises the tables: the forecast engine and the page stay in
``events`` / ``web``.  Keeping the parser separate means the editable file can
be validated without starting a lunar render.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import package_data_dir

__all__ = [
    "CLAIR_OBSCUR_FILE",
    "ClairObscurSpec",
    "load_clair_obscur",
    "load_clair_obscur_file",
    "spec_key",
    "display_label",
]

CLAIR_OBSCUR_FILE = "clair_obscur.csv"

#: The table's columns, in the order the reference workbook uses.  The last
#: four are free text and are not interpreted here.
COLUMNS = (
    "事件", "关键地貌", "类型", "晨昏线类型", "锚点纬度", "锚点经度",
    "余经度", "余经度误差", "起始太阳高度", "结束太阳高度",
    "最佳时刻太阳高度", "参考照片", "备注",
)


def spec_key(title: str) -> str:
    """Stable ASCII key for one row, independent of its position in the file."""
    folded = unicodedata.normalize("NFKD", str(title or ""))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    slug = re.sub(r"[^a-z0-9]+", "_", folded.casefold()).strip("_")
    return slug or "event"


def _number(value: Any) -> Optional[float]:
    text = str(value or "").strip().replace("°", "").replace(",", ".")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    # ``nan`` and ``inf`` are not useful thresholds; treat them as absent.
    return number if number == number and abs(number) != float("inf") else None


def _direction(text: Any) -> Optional[str]:
    value = str(text or "").strip()
    if "日升" in value or "日出" in value:
        return "rise"
    if "日落" in value:
        return "set"
    return None


@dataclass(frozen=True)
class ClairObscurSpec:
    """One row of the editable Clair-Obscur table.

    Two shapes are accepted, matching the reference workbook:

    * an **anchored** row has ``lat``/``lon`` and a solar-altitude band;
    * a **colongitude-only** row has no anchor, and its window is written in
      the two solar-altitude columns while the best value is in 余经度.
    """

    key: str
    title: str
    source: str
    row: int
    feature: str = ""
    kind: str = ""
    direction: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    colong: Optional[float] = None
    colong_tolerance: Optional[float] = None
    alt_start: Optional[float] = None
    alt_end: Optional[float] = None
    alt_best: Optional[float] = None
    colong_start: Optional[float] = None
    colong_end: Optional[float] = None
    colong_best: Optional[float] = None
    reference: str = ""
    note: str = ""

    @property
    def anchored(self) -> bool:
        return self.lat is not None and self.lon is not None

    @property
    def colongitude_only(self) -> bool:
        return (not self.anchored and self.colong is not None
                and self.colong_start is not None
                and self.colong_end is not None)

    @property
    def incomplete(self) -> bool:
        if self.anchored:
            return (self.alt_start is None or self.alt_end is None
                    or self.alt_best is None)
        return not self.colongitude_only

    @property
    def sort_colongitude(self) -> Optional[float]:
        return self.colong

    @property
    def menu_label(self) -> str:
        return display_label(self)


def display_label(spec: ClairObscurSpec) -> str:
    """The page's menu text: ``余经度·事件名`` (or an em dash when absent)."""
    value = spec.sort_colongitude
    if value is None:
        prefix = "—"
    elif abs(value - round(value)) < 1e-9:
        prefix = f"{int(round(value))}°"
    else:
        prefix = f"{value:g}°"
    return f"{prefix}·{spec.title}"


def _read(path: Path, *, source: str) -> List[ClairObscurSpec]:
    if not path.is_file():
        return []
    specs: List[ClairObscurSpec] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row_number, row in enumerate(reader, start=2):
                title = str(row.get("事件") or "").strip()
                if not title:
                    continue
                lat = _number(row.get("锚点纬度"))
                lon = _number(row.get("锚点经度"))
                colong = _number(row.get("余经度"))
                tolerance = _number(row.get("余经度误差"))
                alt_start = _number(row.get("起始太阳高度"))
                alt_end = _number(row.get("结束太阳高度"))
                alt_best = _number(row.get("最佳时刻太阳高度"))
                direction = _direction(row.get("晨昏线类型"))
                if direction is None and alt_start is not None and alt_end is not None:
                    direction = "set" if alt_start > alt_end else "rise"
                if alt_best is None and alt_start is not None and alt_end is not None:
                    alt_best = 0.5 * (alt_start + alt_end)
                anchored = lat is not None and lon is not None
                colong_start = colong_end = colong_best = None
                if (not anchored and colong is not None
                        and alt_start is not None and alt_end is not None):
                    # Reference-file convention for colongitude-only rows.
                    colong_start, colong_end, colong_best = (
                        alt_start, alt_end, colong)
                    alt_start = alt_end = alt_best = None
                specs.append(ClairObscurSpec(
                    key=spec_key(title),
                    title=title,
                    source=source,
                    row=row_number,
                    feature=str(row.get("关键地貌") or "").strip(),
                    kind=str(row.get("类型") or "").strip(),
                    direction=direction,
                    lat=lat,
                    lon=lon,
                    colong=colong,
                    colong_tolerance=tolerance,
                    alt_start=alt_start,
                    alt_end=alt_end,
                    alt_best=alt_best,
                    colong_start=colong_start,
                    colong_end=colong_end,
                    colong_best=colong_best,
                    reference=str(row.get("参考照片") or "").strip(),
                    note=str(row.get("备注") or "").strip(),
                ))
    except (OSError, ValueError):
        return []
    return specs


def load_clair_obscur_file(path: Any) -> Tuple[ClairObscurSpec, ...]:
    """Read one table; missing or malformed rows are skipped, not fatal."""
    return tuple(_read(Path(path), source=Path(path).name))


def load_clair_obscur(
    user_path: Optional[Any] = None,
    *,
    builtin_path: Optional[Any] = None,
) -> Tuple[ClairObscurSpec, ...]:
    """Built-in table with an optional user table merged over it.

    A user row with the same visible title replaces the built-in row, so a
    reader can correct a condition without deleting the shipped line.  New
    titles are appended.  The result is sorted by the reference colongitude;
    rows without one go last, in title order.
    """
    builtin = Path(builtin_path) if builtin_path is not None else (
        package_data_dir() / CLAIR_OBSCUR_FILE)
    merged: Dict[str, ClairObscurSpec] = {}
    for spec in _read(builtin, source=builtin.name):
        merged[spec.key] = spec
    if user_path is not None:
        user = Path(user_path)
        for spec in _read(user, source=user.name):
            merged[spec.key] = spec
    return tuple(sorted(
        merged.values(),
        key=lambda item: (
            item.sort_colongitude is None,
            item.sort_colongitude if item.sort_colongitude is not None else 0.0,
            item.title.casefold(),
        ),
    ))
