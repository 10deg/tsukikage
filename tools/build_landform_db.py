#!/usr/bin/env python3
"""Build the landform database from the two tables that define it.

The database is *these two files*, merged:

``--features``
    ``searchresults.csv`` -- the IAU gazetteer export: what exists, where it is
    and how big it is.  It is the authority for every position and diameter in
    the shipped table, so a feature the IAU has named since this export is
    deliberately **not** in the database: it would have no Chinese name and the
    page would fall back to English for it.
``--translation``
    月球地名.xls -- the official Chinese names and the feature family each one
    belongs to (one sheet per family: 环形坑, 山脉, 海洋, …).  It is the only
    source of 中文名 and of the kind shown in the page.

Both exports were damaged on their way out of the IAU web site, in two
different ways, and both are repaired here rather than guessed at:

* the CSV replaced every non-ASCII character with one *replacement character
  per UTF-8 byte* -- ``Kohlsch␦␦tter`` is Kohlschütter -- so a run of them is
  matched as one character, and where that leaves two candidates (Lamb or Lamé)
  the row's own coordinates decide;
* the spreadsheet replaced every one of them with a single ``_``.

``--spelling`` is optional and used for exactly that repair: a table with
undamaged Unicode names (``tools/fetch_nomenclature.py`` writes one).  Nothing
else is taken from it -- no names, no positions, no membership.

Everything that cannot be matched is reported and left out, never guessed:
attaching 灿德尔 to whichever crater happens to be nearest would be worse than
leaving the row English.

Usage
-----
    ./.venv/bin/python tools/build_landform_db.py \\
        --features searchresults.csv --translation 月球地名.xls \\
        --spelling .cache/moonlib_s/nomenclature/iau-2026.csv

An ``.xls`` has to be converted first (WPS, Excel and LibreOffice all can):

    soffice --headless --convert-to xlsx --outdir /tmp 月球地名.xls
"""

from __future__ import annotations

import argparse
import os
import csv
import json
import math
import re
import sys
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ``fold(..., whitespace="drop")`` throughout: this builder is looking for the
# for the IAU's own collisions, and two names that differ only in spacing are
# exactly the collision it exists to find.  Its spelling pass is the exception
# and passes whitespace="collapse".
from _bootstrap import CATALOGUE_COLUMNS, ROOT, fold, normalise_lon  # noqa: E402

TARGET = ROOT / "src" / "moonlib_s" / "data" / "lunar_nomenclature.csv"
PROVENANCE = TARGET.with_suffix(".provenance.json")

#: The spreadsheet's sheet names, mapped to the kind the rest of moonlib_s speaks.
#: The sheet a name sits on is the user's own classification, so it is what the
#: database records; the page groups these into families for the label budget
#: and decides outlines from them.
SHEET_KINDS = {
    "环形坑": "Crater",
    "卫星坑": "Crater",
    "山脉": "Mons",
    "山脊": "Dorsum",
    "海洋": "Mare",
    "月湾": "Sinus",
    "月湖": "Lacus",
    "峭壁": "Rupes",
    "月谷": "Vallis",
    "月溪": "Rima",
    "月沼": "Palus",
    "坑链": "Catena",
    "海角": "Promontorium",
    "平原": "Planitia",
    "反射率特征点": "Albedo Feature",
    "着陆点": "Statio",
}

#: Column names seen in the two translation workbooks.  The first one (月球地名)
#: labels its sheets 英文名称/中文译名; the second (月面标记, the authority) uses
#: 英文/名称, 环形山 for the crater sheet, 中文名称 for the catena sheet, and puts
#: the header row three rows down on the quadrant sheets.  So the header row is
#: searched for rather than assumed, and the columns are found by any of these.
ENGLISH_HEADERS = ("英文名称", "英文", "英文名", "English Name", "Name")
CHINESE_HEADERS = ("中文译名", "中文名称", "名称", "环形山", "Name1")
LATITUDE_HEADERS = ("y", "纬度")
LONGITUDE_HEADERS = ("x", "经度")
DIAMETER_HEADERS = ("直径", "Diameter")
#: Rows of the sheet that may hold the header (the chart workbook titles its
#: sheets first).
HEADER_ROWS = 4

#: Never a feature name: the 月沼 sheet lost its name column to its own header.
BOGUS_NAMES = {"英文名称", "Name", "名称", "nan", "None", ""}

#: A translation row may only be attached by position when its coordinates sit
#: this close to the feature and clearly closer than anything else.
POSITION_DEG = 0.35
#: How similar two names have to be before a positional match is believed.
SIMILARITY = 0.8


# ---------------------------------------------------------------------------
# names and the damage in them
# ---------------------------------------------------------------------------


DAMAGE = "?\ufffd"


def damage_pattern(name: str) -> Optional[re.Pattern]:
    """Regex for a name with lost characters, or ``None`` if it has none.

    A run of *k* replacement characters stands for **one** character, because
    each replacement character is one byte of one UTF-8 character; a run of
    single ``_`` (the spreadsheet's way of losing them) is one character each.
    Allowing up to *k* characters covers both readings without ever matching
    more than the run could have held.
    """
    key = fold(name, whitespace="drop")
    if not any(character in DAMAGE or character == "_" for character in key):
        return None
    parts: List[str] = []
    run = 0
    for character in key:
        if character in DAMAGE or character == "_":
            run += 1
            continue
        if run:
            parts.append(".{1,%d}" % run)
            run = 0
        parts.append(re.escape(character))
    if run:
        parts.append(".{1,%d}" % run)
    return re.compile("^" + "".join(parts) + "$")


def strip_brackets(name: str) -> str:
    text = str(name or "").strip()
    while len(text) > 2 and text[0] in "[(" and text[-1] in "])":
        text = text[1:-1].strip()
    return text


_TRAILING_LETTER = re.compile(r"([A-Za-z])\s*$")
#: A satellite designation: the parent's name, a space, and one capital letter.
_SATELLITE = re.compile(r"^(.+?)\s+([A-Z])$")


def suffix_letter(text: Any) -> str:
    """Trailing satellite-crater letter, or an empty string.

    Only an **upper-case** letter counts.  The IAU letters its satellites A-Z,
    while plenty of ordinary names end in a lower-case letter -- ``Rosse`` ends
    in an ``e`` -- and reading that as a satellite designation turns a crater
    into a satellite of a different one (``Rosse`` into a satellite of ``Ross``).
    """
    match = _TRAILING_LETTER.search(str(text or "").strip().strip("[]"))
    return match.group(1) if match and match.group(1).isupper() else ""


def is_shifted(row: Dict[str, Any]) -> bool:
    """True when a row's name and its Chinese name disagree about their letter.

    The satellite-crater sheet has exactly one such row: *Cassini Z* carrying
    ``卡塔兰 A`` -- the name column of one line with the position and translation
    of the next.  A lettered crater's letter is part of both of its names, so a
    disagreement is proof of a shift rather than a translation choice, and the
    position is then the only column still telling the truth.
    """
    english, chinese = suffix_letter(row["raw_en"]), suffix_letter(row["zh"])
    return bool(english) and bool(chinese) and english != chinese


def similarity(first: str, second: str) -> float:
    return SequenceMatcher(None, fold(first, whitespace="drop"), fold(second, whitespace="drop")).ratio()


# ---------------------------------------------------------------------------
# reading the sources
# ---------------------------------------------------------------------------
def read_features(path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The IAU export: one row per feature, positions normalised."""
    rows: List[Dict[str, Any]] = []
    dropped: List[str] = []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        for record in csv.DictReader(handle):
            name = (record.get("Feature Name") or "").strip()
            if not name:
                continue
            try:
                lat = float(record.get("Center Latitude") or "")
                lon = normalise_lon(float(record.get("Center Longitude") or ""))
                diameter = float(record.get("Diameter") or 0.0)
            except ValueError:
                dropped.append(f"{name} (unreadable position)")
                continue
            if diameter <= 0.0:
                # A point with no size: it can be neither drawn nor outlined.
                dropped.append(f"{name} (no diameter)")
                continue
            rows.append({"name": name, "lat": lat, "lon": lon, "diameter": diameter})
    return rows, dropped


def read_translation_xlsx(path: Path) -> List[Dict[str, Any]]:
    try:
        import openpyxl
    except ImportError as error:  # pragma: no cover - environment dependent
        raise SystemExit(
            "reading .xls/.xlsx needs openpyxl: pip install openpyxl, or export "
            "the sheets to CSV and pass that instead."
        ) from error
    workbook = openpyxl.load_workbook(path, data_only=True)
    rows: List[Dict[str, Any]] = []
    for sheet in workbook.sheetnames:
        data = list(workbook[sheet].iter_rows(values_only=True))
        if not data:
            continue

        def find_header() -> Tuple[List[str], int]:
            for offset, candidate in enumerate(data[:HEADER_ROWS]):
                text = [str(cell or "").strip() for cell in (candidate or [])]
                if any(name in text for name in CHINESE_HEADERS):
                    return text, offset
            return [str(cell or "").strip() for cell in (data[0] or [])], 0

        header, offset = find_header()

        def column(*names: str) -> Optional[int]:
            for index, text in enumerate(header):
                if text in names:
                    return index
            return None

        name_en = column(*ENGLISH_HEADERS)
        name_zh = column(*CHINESE_HEADERS)
        diameter = column(*DIAMETER_HEADERS)
        if name_zh is None:
            print(f"  skip sheet  : {sheet} (no name column)", file=sys.stderr)
            continue
        if name_en is None:
            # The 月沼 and 反射率 sheets label their rows in Chinese only; their
            # size is what is left to match on.
            print(f"  no English  : {sheet} rows matched by family and diameter",
                  file=sys.stderr)
        lat = column(*LATITUDE_HEADERS)
        lon = column(*LONGITUDE_HEADERS)
        for record in data[offset + 1:]:
            if record is None:
                continue
            english = str(record[name_en] or "").strip() if name_en is not None else ""
            chinese = str(record[name_zh] or "").strip()
            if english in BOGUS_NAMES and chinese in BOGUS_NAMES:
                continue
            if not chinese:
                continue
            rows.append(
                {
                    "sheet": sheet,
                    "raw_en": english,
                    "zh": chinese,
                    "lat": float(record[lat]) if lat is not None and _is_number(record[lat]) else float("nan"),
                    "lon": float(record[lon]) if lon is not None and _is_number(record[lon]) else float("nan"),
                    "diameter": float(record[diameter])
                    if diameter is not None and _is_number(record[diameter]) else float("nan"),
                }
            )
    return rows


def read_translation_csv(path: Path) -> List[Dict[str, Any]]:
    """A CSV export of the workbook: one file, with a column naming the sheet."""
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for record in csv.DictReader(handle):
            sheet = str(record.get("sheet") or record.get("Sheet") or "").strip()
            # The same column vocabulary as the workbooks, plus the catalogue's
            # own schema (English Name / 名称 / Type), so a CSV written by any of
            # the tools here can be fed straight back in.
            def pick(names):
                for name in names:
                    value = record.get(name)
                    if value and str(value).strip():
                        return str(value).strip()
                return ""
            english = pick(ENGLISH_HEADERS)
            chinese = pick(CHINESE_HEADERS)
            if english in BOGUS_NAMES and chinese in BOGUS_NAMES:
                continue
            rows.append(
                {
                    "sheet": sheet,
                    "raw_en": english,
                    "zh": chinese,
                    "lat": _as_float(record.get("y")),
                    "lon": _as_float(record.get("x")),
                    "diameter": _as_float(pick(DIAMETER_HEADERS)),
                    "kind": pick(("Type", "类型")),
                }
            )
    return rows


def _is_number(value: Any) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def satellite_name_rows(path: Path) -> List[Dict[str, Any]]:
    """The workbook's own names for lettered craters, from its 卫星坑 column.

    Each cell runs the letter straight on to the parent's Chinese name
    ("马斯基林D"), which is the convention the derived names follow as well.
    The parent is turned into English through the workbook's own Chinese/English
    sheet, so a cell can only be used when both halves are in the table.
    """
    try:
        import openpyxl
    except ImportError:  # pragma: no cover - environment dependent
        return []
    workbook = openpyxl.load_workbook(path, data_only=True)

    def locate(record: Sequence[Any]) -> Tuple[List[str], int]:
        for offset, candidate in enumerate(record[:HEADER_ROWS]):
            text = [str(cell or "").strip() for cell in (candidate or [])]
            if any(name in text for name in CHINESE_HEADERS):
                return text, offset
        return [str(cell or "").strip() for cell in (record[0] or [])], 0

    def column(header: Sequence[str], names: Sequence[str]) -> Optional[int]:
        for index, text in enumerate(header):
            if text in names:
                return index
        return None

    # Chinese -> English, from whichever sheets carry both.
    zh_to_en: Dict[str, str] = {}
    for sheet in workbook.sheetnames:
        data = list(workbook[sheet].iter_rows(values_only=True))
        if not data:
            continue
        header, offset = locate(data)
        zh, en = column(header, CHINESE_HEADERS), column(header, ENGLISH_HEADERS)
        if zh is None or en is None:
            continue
        for record in data[offset + 1:]:
            if not record:
                continue
            chinese = str(record[zh] or "").strip()
            english = str(record[en] or "").strip()
            if chinese and english:
                zh_to_en.setdefault(chinese, english)

    rows: List[Dict[str, Any]] = []
    for sheet in workbook.sheetnames:
        data = list(workbook[sheet].iter_rows(values_only=True))
        if not data:
            continue
        header, offset = locate(data)
        satellites = column(header, ("卫星坑",))
        if satellites is None:
            continue
        for record in data[offset + 1:]:
            if not record or satellites >= len(record):
                continue
            cell = unicodedata.normalize("NFKC", str(record[satellites] or ""))
            cell = cell.strip().replace(" ", "")
            if cell == "卫星坑" or not cell:
                continue
            match = re.match(r"^(.*?)([A-Za-z])$", cell)
            if match is None:
                continue          # a bare letter continues the cell above; a
                                  # list ("斯特拉博L、B、N") is not one feature
            parent_zh, letter = match.group(1), match.group(2).upper()
            parent_en = zh_to_en.get(parent_zh)
            if not parent_en:
                continue
            rows.append(
                {
                    "sheet": "卫星坑",
                    "raw_en": f"{parent_en} {letter}",
                    "zh": f"{parent_zh}{letter}",
                    "lat": float("nan"),
                    "lon": float("nan"),
                    "diameter": float("nan"),
                }
            )
    return rows


def read_spelling(path: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    """Undamaged names (and kinds) from another catalogue, keyed by folded name."""
    table: Dict[str, Dict[str, Any]] = {}
    if path is None or not path.is_file():
        return table
    with path.open("r", encoding="utf-8", newline="") as handle:
        for record in csv.DictReader(handle):
            name = (record.get("English Name") or "").strip()
            if name:
                table.setdefault(fold(name, whitespace="drop"), record)
    return table


# ---------------------------------------------------------------------------
# merging
# ---------------------------------------------------------------------------
def repair_names(
    features: Sequence[Dict[str, Any]], spelling: Dict[str, Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Restore the characters the CSV export lost; report what it could not."""
    repaired: List[Dict[str, Any]] = []
    problems: List[Dict[str, Any]] = []
    for feature in features:
        name = feature["name"]
        pattern = damage_pattern(name)
        if pattern is None:
            repaired.append(feature)
            continue
        hits = [record["English Name"] for key, record in spelling.items() if pattern.match(key)]
        if len(hits) == 1:
            feature = dict(feature, name=hits[0], source_name=name, repair="unique")
        elif hits:
            # Two readings of the same damage: let the position choose.
            best = min(
                hits,
                key=lambda candidate: _distance(spelling[fold(candidate, whitespace="drop")], feature),
            )
            if _distance(spelling[fold(best, whitespace="drop")], feature) <= 1.0:
                feature = dict(feature, name=best, source_name=name, repair="position")
            else:
                problems.append({"name": name, "candidates": hits[:4]})
                feature = dict(feature, source_name=name, repair="failed")
        else:
            problems.append({"name": name, "candidates": []})
            feature = dict(feature, source_name=name, repair="failed")
        repaired.append(feature)
    return repaired, problems


def _distance(record: Dict[str, Any], feature: Dict[str, Any]) -> float:
    try:
        dlat = float(record["Lat"]) - feature["lat"]
        dlon = ((float(record["Lon"]) - feature["lon"] + 180.0) % 360.0) - 180.0
    except (KeyError, TypeError, ValueError):
        return float("inf")
    return math.hypot(dlat, dlon)


def nearest_feature(
    features: Sequence[Dict[str, Any]], lat: float, lon: float
) -> Optional[Tuple[float, Dict[str, Any], float]]:
    """Closest feature to a position, plus the runner-up distance."""
    if lat != lat or lon != lon:
        return None
    best: Optional[Tuple[float, Dict[str, Any]]] = None
    second = float("inf")
    for feature in features:
        dlat = feature["lat"] - lat
        dlon = ((feature["lon"] - lon + 180.0) % 360.0) - 180.0
        distance = math.hypot(dlat, dlon)
        if best is None or distance < best[0]:
            second = best[0] if best else second
            best = (distance, feature)
        elif distance < second:
            second = distance
    return None if best is None else (best[0], best[1], second)


#: LPND letters a crater "…坑" and a satellite "…坑A".  The base name is shown
#: without the class word; the satellite keeps its parent's full name, so a
#: satellite reads exactly as LPND writes it while its parent reads shorter.
_CRATER_SUFFIX = re.compile(r"坑$")


def strip_crater_suffix(chinese: str) -> str:
    """Drop the class word LPND puts on a plain crater, keeping everything else."""
    return _CRATER_SUFFIX.sub("", str(chinese or "").strip())


def is_satellite_name(name: str) -> bool:
    return _SATELLITE.match(str(name or "").strip()) is not None


def bare_key(name: str) -> str:
    """Folded name with a trailing ``(…)`` alternate spelling removed.

    The IAU records some features under two transliterations -- ``Tsander
    (Zander)``, ``Fridman (Friedmann)`` -- and the translation table was made
    from the older, bare form.  Dropping the parenthesis is what lets 灿德尔
    find its feature instead of being reported as unmatched.
    """
    return fold(re.sub(r"\s*\([^)]*\)\s*$", "", str(name or "")), drop_spaces=True)


def pick(
    candidates: Sequence[Dict[str, Any]], row: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """One feature out of several that fold to the same name.

    Folding away the spaces makes a lettered crater collide with any base crater
    whose name ends in that letter -- ``Watt S`` and ``Watts``, ``Ross E`` and
    ``Rosse``, ``Mee S`` and ``Mees``.  Two things separate them, in order:

    1. a candidate spelled exactly like the row, which is the row's own feature
       even when the row carries no usable position (the chart sheets have no
       coordinates at all);
    2. otherwise the row's coordinates, which land on one of them to within a
       tenth of a degree.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # ``drop_spaces=False``: the spacing is the only thing that tells
    # ``Ross E`` and ``Rosse`` apart, and this pass exists to do that.
    wanted = fold(row["raw_en"], whitespace="collapse")
    spelled = [
        feature for feature in candidates
        if fold(feature["name"], whitespace="collapse") == wanted
    ]
    if len(spelled) == 1:
        return spelled[0]
    if row["lat"] != row["lat"] or row["lon"] != row["lon"]:
        return None
    ranked = sorted(candidates, key=lambda feature: _offset(feature, row))
    if _offset(ranked[0], row) <= 1.0:
        return ranked[0]
    return None


def _offset(feature: Dict[str, Any], row: Dict[str, Any]) -> float:
    dlat = feature["lat"] - row["lat"]
    dlon = ((feature["lon"] - row["lon"] + 180.0) % 360.0) - 180.0
    return math.hypot(dlat, dlon)


def attach(
    row: Dict[str, Any],
    by_key: Dict[str, List[Dict[str, Any]]],
    by_bare: Dict[str, List[Dict[str, Any]]],
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Feature for one translation row, and which rule found it."""
    candidates = [row["raw_en"], strip_brackets(row["raw_en"])]
    for text in candidates:
        if text in BOGUS_NAMES:
            continue
        found = pick(by_key.get(fold(text, whitespace="drop"), ()), row)
        if found is not None:
            return found, "exact"
    for text in candidates:
        if text in BOGUS_NAMES:
            continue
        pattern = damage_pattern(text)
        if pattern is None:
            continue
        hits = [feature for key, group in by_key.items() if pattern.match(key) for feature in group]
        if len(hits) == 1:
            return hits[0], "wildcard"
        found = pick(hits, row)
        if found is not None:
            return found, "wildcard"
        if hits:
            return None, "ambiguous"
    for text in candidates:
        if text in BOGUS_NAMES:
            continue
        found = pick(by_bare.get(bare_key(text), ()), row)
        if found is not None:
            return found, "renamed"
    return None, "unmatched"


#: Sheets whose kind is the coarse bucket rather than a statement: 环形坑 holds
#: everything round, so a row on it must not overrule a specific IAU type.
COARSE_SHEET_KINDS = frozenset({"Crater"})


def resolve_kind(iau_kind, sheet_kind) -> str:
    """Which of the two classifications a feature ends up with.

    The IAU export is the authority on *what* a feature is; a translation sheet
    is the authority on its Chinese name, and the sheet it sits on is a coarse
    classification.  So a sheet may refine the export's generic ``Crater`` --
    the twenty near-side maria the export files that way are on the 月海 sheet
    -- but may not overrule a specific type.  Reiner Gamma is the case that
    proved it: the export says ``Albedo Feature`` and it sits on 环形坑, and
    letting the sheet win turned the Moon's only albedo feature into a crater
    and left the 反射率特征点 switch with nothing behind it.
    """
    iau = str(iau_kind or "").strip()
    sheet = str(sheet_kind or "").strip()
    if sheet and sheet not in COARSE_SHEET_KINDS:
        return sheet
    return iau or sheet or "Crater"


def merge(
    features: List[Dict[str, Any]],
    translation: Sequence[Dict[str, Any]],
    spelling: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Attach a Chinese name (and kind) to every feature that has one."""
    by_key: Dict[str, List[Dict[str, Any]]] = {}
    by_bare: Dict[str, List[Dict[str, Any]]] = {}
    for feature in features:
        by_key.setdefault(fold(feature["name"], whitespace="drop"), []).append(feature)
        by_bare.setdefault(bare_key(feature["name"]), []).append(feature)

    # Rows whose sheet has no English column (the 月沼 and 反射率 sheets) are
    # linked through their Chinese name: whichever table *does* give both names
    # for that place says which feature it is.
    zh_to_en: Dict[str, str] = {}
    for row in translation:
        if row["raw_en"] and row["zh"]:
            zh_to_en.setdefault(row["zh"], row["raw_en"])

    report: Dict[str, Any] = {
        "features": len(features),
        "translation_rows": len(translation),
        "rules": {},
        "unmatched_rows": [],
        "position_attached": [],
        "duplicate_rows": [],
        "unnamed_features": [],
        "derived_names": [],
        "stale_rows": [],
    }
    named: Dict[str, str] = {}
    for row in translation:
        if not row["raw_en"] and row["zh"] in zh_to_en:
            row = dict(row, raw_en=zh_to_en[row["zh"]], rule_hint="linked by Chinese name")
        rule = ""
        target: Optional[Dict[str, Any]] = None
        if is_shifted(row):
            # The name column belongs to the previous line: only the position
            # and the translation are still about this feature.
            rule = "shifted"
        else:
            target, rule = attach(row, by_key, by_bare)
        if target is None and row["raw_en"] not in BOGUS_NAMES and rule != "shifted":
            # A truncated base name in the satellite sheet ("Mee S" for
            # "Mees S"): the position plus a similar name is evidence enough.
            found = nearest_feature(features, row["lat"], row["lon"])
            if found is not None:
                distance, candidate, runner_up = found
                if (
                    distance <= POSITION_DEG
                    and distance * 2.0 <= runner_up
                    and similarity(candidate["name"], row["raw_en"]) >= SIMILARITY
                ):
                    target, rule = candidate, "position"
        if target is None and (row["raw_en"] in BOGUS_NAMES or rule == "shifted"):
            # A row whose name column is unusable -- the 月沼 sheet -- or the one
            # shifted row: the position is all it has left.
            found = nearest_feature(features, row["lat"], row["lon"])
            if found is not None:
                distance, candidate, runner_up = found
                if distance <= 1.0 and distance * 1.15 <= runner_up:
                    target, rule = candidate, "position"
                    report["position_attached"].append(
                        {"source": row["raw_en"], "zh": row["zh"],
                         "matched": candidate["name"], "offset_deg": round(distance, 3)}
                    )
        report["rules"][rule or "unmatched"] = report["rules"].get(rule or "unmatched", 0) + 1
        if target is None:
            report["unmatched_rows"].append(
                {"sheet": row["sheet"], "name": row["raw_en"], "zh": row["zh"]}
            )
            continue
        if not row["zh"]:
            continue
        if target["name"] in named and named[target["name"]] != row["zh"]:
            report["duplicate_rows"].append(
                {"feature": target["name"], "kept": named[target["name"]],
                 "dropped": row["zh"], "from": row.get("source", "")}
            )
            continue
        kind = resolve_kind(target.get("kind"), SHEET_KINDS.get(row["sheet"]))
        chinese = row["zh"]
        # The raw LPND wording is kept for two reasons: a satellite is named
        # from it, and it is what the provenance can be checked against.
        target["zh_raw"] = chinese
        if kind == "Crater" and not is_satellite_name(target["name"]):
            chinese = strip_crater_suffix(chinese)
        named[target["name"]] = chinese
        target["zh"] = chinese
        target["kind"] = kind
        # The table's own coordinates are older than the export's in places
        # (Montes Cordillera sits thirteen degrees away), which is worth
        # reporting and never worth "fixing": the name is the key, the position
        # in the database comes from the IAU export.
        offset = _offset(target, row) if row["lat"] == row["lat"] else float("nan")
        if offset == offset and offset > 0.5:
            report["stale_rows"].append(
                {"feature": target["name"], "offset_deg": round(offset, 2), "zh": row["zh"]}
            )

    # Lettered craters the table happens not to list, whose base crater it does:
    # the table's own convention is the base name plus the letter ("阿贝 H",
    # "米 S"), so the same name is derived rather than left English.  Every one
    # is recorded, because a derived name is not an official one.
    by_name = {fold(feature["name"], whitespace="drop"): feature for feature in features}
    for feature in features:
        if feature.get("zh"):
            continue
        match = _SATELLITE.match(feature["name"].strip())
        if match is None:
            continue
        base, letter = match.group(1), match.group(2)
        parent = by_name.get(fold(base, whitespace="drop"))
        if parent is None or not parent.get("zh"):
            continue
        # The letter runs straight on to the parent's full LPND name, so a
        # satellite keeps the 坑 its parent no longer shows.
        feature["zh"] = f"{parent.get('zh_raw') or parent['zh']}{letter}"
        feature["zh_derived"] = True
        report["derived_names"].append(
            {"feature": feature["name"], "zh": feature["zh"], "from": parent["name"]}
        )

    rows: List[Dict[str, Any]] = []
    for feature in sorted(features, key=lambda item: fold(item["name"], whitespace="drop")):
        kind = feature.get("kind")
        if kind is None:
            reference = spelling.get(fold(feature["name"], whitespace="drop")) or {}
            kind = (reference.get("Type") or "Crater").strip()
        if not feature.get("zh"):
            report["unnamed_features"].append(feature["name"])
        rows.append(
            {
                "English Name": feature["name"],
                "名称": feature.get("zh", ""),
                "Lat": round(feature["lat"], 5),
                "Lon": round(feature["lon"], 5),
                "Diameter": round(feature["diameter"], 3),
                "Type": kind,
                "Note": "",
            }
        )
    report["named"] = sum(1 for row in rows if row["名称"])
    report["coverage_percent"] = round(100.0 * report["named"] / max(1, len(rows)), 1)
    return rows, report


def write_rows(rows: Sequence[Dict[str, Any]], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=list(CATALOGUE_COLUMNS),
            )
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--features", type=Path, required=True,
                        help="searchresults.csv (the IAU export)")
    parser.add_argument("--translation", type=Path, action="append", required=True,
                        help="译名表，可给多次：**先给的优先**，后面的只补前面没有的"
                             "（例如 --translation 月面标记.xlsx --translation 月球地名.xlsx）")
    parser.add_argument("--spelling", type=Path,
                        help="catalogue with undamaged Unicode names, for repairs only")
    parser.add_argument("--out", type=Path, default=TARGET)
    args = parser.parse_args(argv)

    if not args.features.is_file():
        raise SystemExit(f"features not found: {args.features}")
    translation: List[Dict[str, Any]] = []
    for path in args.translation:
        if not path.is_file():
            raise SystemExit(f"translation not found: {path}")
        suffix = path.suffix.lower()
        if suffix in (".xlsx", ".xlsm"):
            rows = read_translation_xlsx(path)
        elif suffix == ".xls":
            raise SystemExit(
                f"{path.name} is a legacy .xls; convert it first:\n"
                "  soffice --headless --convert-to xlsx --outdir /tmp "
                f"'{path}'"
            )
        else:
            rows = read_translation_csv(path)
        for row in rows:
            row["source"] = path.name
        print(f"  table       : {path.name} -> {len(rows)} rows")
        translation.extend(rows)
        # Only a workbook has a 卫星坑 column; a CSV export carries its names
        # as ordinary rows and needs no second look.
        extra = satellite_name_rows(path) if path.suffix.lower() in (".xlsx", ".xlsm") else []
        if extra:
            for row in extra:
                row["source"] = f"{path.name}（卫星坑列）"
            print(f"  satellites  : {path.name} 的「卫星坑」列给出 {len(extra)} 个显式译名")
            translation.extend(extra)

    features, dropped = read_features(args.features)
    spelling = read_spelling(args.spelling)
    features, problems = repair_names(features, spelling)
    print(f"  features    : {len(features)} rows from {args.features.name}")
    if dropped:
        print(f"  dropped     : {len(dropped)} without a usable size")
    if spelling:
        fixed = sum(1 for f in features if f.get("repair") in ("unique", "position"))
        print(f"  spelling    : {fixed} names repaired against {args.spelling.name}, "
              f"{len(problems)} left damaged")
    if problems:
        for item in problems[:5]:
            print(f"    damaged   : {item['name']} (candidates {item['candidates']})")

    rows, report = merge(features, translation, spelling)
    write_rows(rows, args.out)

    print(f"  translation : {len(translation)} rows from "
          f"{', '.join(path.name for path in args.translation)}")
    for rule in ("exact", "wildcard", "renamed", "position", "shifted", "ambiguous",
                 "unmatched"):
        if report["rules"].get(rule):
            print(f"    by {rule:9s}: {report['rules'][rule]}")
    print(f"  named       : {report['named']} / {len(rows)} "
          f"({report['coverage_percent']}%)")
    if report["stale_rows"]:
        print(f"  stale coords: {len(report['stale_rows'])} 条译表坐标与 IAU 位置差 >0.5°"
              f"（名称仍按名称匹配）")
    if report["derived_names"]:
        print(f"  derived     : {len(report['derived_names'])} 项卫星坑沿用母坑译名+字母"
              f"（见 provenance）")
    if report["unnamed_features"]:
        print(f"  no 中文名   : {len(report['unnamed_features'])} 项，页面回落英文")
        print(f"    例: {', '.join(report['unnamed_features'][:6])}")
    if report["unmatched_rows"]:
        print(f"  unused rows : {len(report['unmatched_rows'])} 条译表行没有对应地貌")

    report["sources"] = {
        "features": args.features.name,
        "translation": [path.name for path in args.translation],
        "translation_precedence": "earlier files win",
        "spelling_reference": args.spelling.name if spelling else None,
    }
    report["dropped"] = dropped
    report["damaged_names"] = problems
    report["generated_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report["regenerate"] = (
        "python tools/build_landform_db.py --features searchresults.csv "
        "--translation 月面标记.xlsx --translation 月球地名.xlsx "
        "--spelling .cache/moonlib_s/nomenclature/iau-2026.csv"
    )
    PROVENANCE.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"  written     : {os.path.relpath(args.out)} ({args.out.stat().st_size / 1024:.0f} KB)")
    print(f"  provenance  : {os.path.relpath(PROVENANCE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
