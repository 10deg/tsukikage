#!/usr/bin/env python3
"""Export the landform database as one JSON file under ``out/``.

The database itself is a CSV (``src/moonlib_s/data/lunar_nomenclature.csv``)
because it is built and merged by scripts and diffed by hand.  A JSON copy
exists for the *other* half of the project: the labeler reads a translation
table keyed by the IAU English name, and so does anything else that wants
"what is this feature called, in Chinese, and where is it" without parsing a
CSV or opening the library.

The shape follows the file that convention was set by --
``MoonLabeler-test/moonlabeler/data/chinese_names.json``:

``source`` / ``retrieved`` / ``source_urls`` / ``matching``
    where the names came from and what the matching rule was, so a consumer can
    tell a translation from an inference.
``names``
    one entry per feature, keyed by the IAU English name, carrying ``zh`` (the
    name the page captions with), the authority's own wording ``term_zh`` and
    ``url`` where LPND has them, and -- the part that file has no room for --
    the geometry and the class the page sorts by.

Usage
-----
    /path/to/python3 tools/export_landforms.py            # write out/lunar_nomenclature.json
    /path/to/python3 tools/export_landforms.py --check    # compare, write nothing
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from moonlib_s.annotations import (  # noqa: E402
    FEATURE_CLASSES,
    canonical_kind,
    feature_class,
)

DB = ROOT / "src" / "moonlib_s" / "data" / "lunar_nomenclature.csv"
LPND = ROOT / "out" / "lunar_chinese_names.json"
TARGET = ROOT / "out" / "lunar_nomenclature.json"

HEADER = {
    "source": "moonlib_s 月球地貌数据库（IAU 行星命名表 + LPND 月球与行星地名库 + "
              "Virtual Moon Atlas 分类）",
    "source_urls": [
        "https://planetarynames.wr.usgs.gov/Page/MOON/target",
        "https://lpnd.bao.ac.cn/#/search?is=1",
        "https://www.ap-i.net/avl/en/start",
    ],
    "matching": (
        "Keyed by the IAU English name exactly as the gazetteer writes it.  "
        "``zh`` is the name the page captions with (the official translation "
        "where one exists, the English name where it does not); ``term_zh`` and "
        "``url`` are the authority's own wording and record, present only where "
        "LPND has them.  ``type`` is the IAU/USGS type, ``class`` is the page's "
        "own category key, and the coordinates are selenographic degrees with "
        "east positive."
    ),
}


def read_database(path: Path) -> List[Dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def read_lpnd(path: Path) -> Dict[str, Dict[str, Any]]:
    """The authority blurb and per-name records, or empty when it is absent.

    The file is an *input* to the database rather than part of it -- it lives in
    ``out/``, which is not version controlled -- so a machine that only has the
    package still exports a complete table, just without the term wording and
    the source URLs.
    """
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    names = payload.get("names")
    return names if isinstance(names, dict) else {}


def build(database: Path = DB, lpnd: Path = LPND) -> Dict[str, Any]:
    rows = read_database(database)
    authority = read_lpnd(lpnd)
    names: Dict[str, Dict[str, Any]] = {}
    classes: Counter = Counter()
    named = 0
    for row in rows:
        english = (row.get("English Name") or "").strip()
        if not english:
            continue
        chinese = (row.get("名称") or "").strip()
        diameter = float(row["Diameter"])
        kind = (row.get("Type") or "Other").strip()
        # The *canonical* kind, which is what the page sorts by: the stored
        # column files the twenty near-side maria as ``Crater``, and their names
        # are the repair (see annotations.canonical_kind).  An export that
        # repeated the column verbatim would put 雨海 in 环形坑.
        group = feature_class(canonical_kind(english, kind), diameter)
        # The authority's wording wins over what the database carries: the CSV
        # keeps one name per feature, and term_zh is the full phrase (阿贝坑)
        # rather than the caption stem (阿贝).
        record = authority.get(english) or {}
        entry: Dict[str, Any] = {
            "zh": chinese or english,
            "lat": round(float(row["Lat"]), 6),
            "lon": round(float(row["Lon"]), 6),
            "diameter_km": round(diameter, 4),
            "type": kind,
            "class": group,
        }
        for key in ("term", "term_zh", "url", "source"):
            if record.get(key):
                entry[key] = record[key]
        note = (row.get("Note") or "").strip()
        if note:
            entry["note"] = note
        names[english] = entry
        classes[group] += 1
        if chinese:
            named += 1
    return {
        **HEADER,
        "retrieved": date.today().isoformat(),
        "count": len(names),
        "named": named,
        "coverage_percent": round(100.0 * named / max(1, len(names)), 1),
        "classes": {key: classes[key] for key in FEATURE_CLASSES if classes.get(key)},
        "names": names,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=TARGET)
    parser.add_argument("--database", type=Path, default=DB)
    parser.add_argument("--lpnd", type=Path, default=LPND)
    parser.add_argument("--check", action="store_true",
                        help="report what would change, write nothing")
    args = parser.parse_args(argv)

    payload = build(args.database, args.lpnd)
    print(f"{payload['count']} features, {payload['named']} with a Chinese name "
          f"({payload['coverage_percent']}%)")
    print("classes:", payload["classes"])
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        current = args.target.read_text(encoding="utf-8") if args.target.is_file() else ""
        # `retrieved` moves with the calendar, so it is not a difference.
        strip = lambda blob: "\n".join(  # noqa: E731
            line for line in blob.splitlines() if '"retrieved"' not in line)
        same = strip(current) == strip(text)
        print(f"{args.target}: {'up to date' if same else 'would change'}")
        return 0 if same else 1
    args.target.parent.mkdir(parents=True, exist_ok=True)
    args.target.write_text(text, encoding="utf-8")
    print(f"wrote {args.target} ({args.target.stat().st_size / 1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
