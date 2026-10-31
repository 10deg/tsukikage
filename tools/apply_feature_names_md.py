#!/usr/bin/env python3
"""Write the Chinese names from the editable Markdown table back to the database.

The table is produced by ``tools/export_feature_names_md.py``.  Only the
中文名 column is read (copy a 暂译名 draft there when it is accepted); the
English name is the matching key.  Rows left untouched, or with an empty
Chinese name, are skipped.

Usage::

    ./.venv/bin/python tools/apply_feature_names_md.py
    ./.venv/bin/python tools/apply_feature_names_md.py --check
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "src" / "moonlib" / "data" / "lunar_nomenclature.csv"
TABLE = ROOT / "docs" / "地貌中文名-大型撞击坑与着陆点.md"


def read_table(path: Path) -> Dict[str, str]:
    names: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("| ---"):
            continue
        cells = [cell.strip().replace("\\|", "|") for cell in line.strip("|").split("|")]
        if len(cells) < 3 or cells[0] == "分类":
            continue
        english, chinese = cells[1].strip(), cells[2].strip()
        if english and chinese:
            names[english] = chinese
    return names


def apply(path: Path, table: Path, check: bool) -> int:
    names = read_table(table)
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if "English Name" not in fieldnames or "名称" not in fieldnames:
        raise SystemExit(f"{path}: missing English Name / 名称 columns")
    changed = 0
    for row in rows:
        english = (row.get("English Name") or "").strip()
        wanted = names.get(english)
        if not wanted:
            continue
        if (row.get("名称") or "").strip() != wanted:
            row["名称"] = wanted
            changed += 1
    print(f"{len(names)} rows in the table, {changed} names would change")
    if check or not changed:
        return 0 if not check or not changed else 1
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)
    print(f"wrote {path}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--table", type=Path, default=TABLE)
    parser.add_argument("--database", type=Path, default=DB)
    parser.add_argument("--check", action="store_true", help="report, write nothing")
    args = parser.parse_args(argv)
    if not args.table.is_file():
        raise SystemExit(f"{args.table}: not found; run export_feature_names_md.py first")
    return apply(args.database, args.table, args.check)


if __name__ == "__main__":
    raise SystemExit(main())
