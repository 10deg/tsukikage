#!/usr/bin/env python3
"""Fold the out/ catalogue into our landform database.

The three files in ``out/`` are the *catalogue* the database is being matched
against:

``01_Named_formations_EN.csv`` (Virtual Moon Atlas)
    Named formations with the IAU type, sub-type, coordinates and diameter.
    The authority for how a feature is *classified* -- except that its own
    ``IAU_FEATURE_TYPE`` column wins where the two disagree, because that column
    *is* the IAU's answer and the atlas's ``TYPE`` is its opinion.  The atlas
    files Reiner Gamma -- a swirl, and the textbook albedo feature -- as a
    ``Crater``; its IAU column says ``Albedo Feature``, which is what the
    database keeps.
``03…12`` (Virtual Moon Atlas)
    The other families the atlas keeps in their own tables: historical sites,
    domes, pyroclastic formations, NELIOTA impacts, impact basins, lunar pits,
    IMPs and sinuous rilles.  They carry no Chinese names; they are here for the
    types.
``named_cn.xlsx`` (the project's own compilation)
    Two sheets of Chinese names: 地貌 (named formations) and 卫星坑 (satellite
    features), keyed by the English name and carrying the atlas's columns.
``lunar_chinese_names.json`` (LPND 月球与行星地名库)
    Chinese names and the full Chinese *term* (``维萨里坑`` rather than
    ``维萨里``) for the same features, with the occasional NADC dictionary
    reference.

What it does **not** do is take the workbook's Chinese names.  Measured against
the shipped table (which is LPND's own ``zh`` field) the workbook differs on
7015 of 8774 features, and the differences are not spelling preferences:
``Schorr`` comes out as 薛定谔 and ``Quetelet`` as 伦琴, which are other
craters' names.  The sheet is misaligned by a row or two in places, so its
names are reported as *suspect* and never applied.  Its satellite names are a
different case -- ``阿贝 H`` against our ``阿贝坑H`` -- and are reported as a
style difference rather than an error.

So the merge takes the atlas's **classification** (type, sub-type, IAU type)
and its **coverage** (what the atlas has that we do not), and leaves the names
to LPND, which our table already matches exactly.

Usage
-----
    /path/to/python3 tools/merge_out_catalogue.py            # report only
    /path/to/python3 tools/merge_out_catalogue.py --write    # rewrite the CSV
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "src" / "moonlib_s" / "data"
OUT = ROOT / "out"
DB = DATA / "lunar_nomenclature.csv"
PROVENANCE = DATA / "lunar_nomenclature.provenance.json"

#: The atlas's type vocabulary mapped onto ours.
#:
#: Ours is what the page colours and filters on (``LAND_CLASSES`` in app.js and
#: ``_CLASS_BY_KIND`` in annotations.py), so the mapping has to land on a class
#: that exists rather than inventing one per atlas row.
TYPE_MAP = {
    "crater": "Crater",
    "crater, craters": "Crater",
    "satellite feature": "Crater",
    "mare": "Mare",
    "oceanus": "Mare",
    "lacus": "Lacus",
    "sinus": "Sinus",
    "palus": "Palus",
    "mons": "Mons",
    "mountain": "Mons",
    "dome": "Dome",
    "dorsum": "Dorsum",
    "rima": "Rima",
    "rille": "Rima",
    "sinuous rille": "Rima",
    "vallis": "Vallis",
    "valley": "Vallis",
    "catena": "Catena",
    "rupes": "Rupes",
    "promontorium": "Promontorium",
    "statio": "Statio",
    "landing site": "Statio",
    "landing site name": "Statio",
    "craters chain": "Catena",
    "historical site": "Statio",
    "albedo feature": "Albedo Feature",
    "impact basin": "Basin",
    "basin": "Basin",
    "pyroclastic formation": "Pyroclastic",
    "pyroclastic area": "Pyroclastic",
    "irregular mare patch": "IMP",
    "contemporary meteoritic impact": "Impact",
    "lunar pit": "Pit",
    "pit": "Pit",
    "imp": "IMP",
    "other": "Other",
}


def fold(text: str) -> str:
    """Casefold, strip accents and collapse spacing: the key names are matched on."""
    value = unicodedata.normalize("NFKD", str(text or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[\s_]+", " ", value).strip().casefold()
    return value


#: Nominal diameters for the two families whose tables carry no size at all.
#:
#: The NELIOTA rows are impact *flashes* -- there is no crater to measure -- and
#: the pyroclastic rows are diffuse deposits the atlas never measured.  Both are
#: worth naming and neither can be projected without a number, so they get one
#: and the Note says it is nominal.  Small on purpose: a diameter here decides
#: the size of the marker and whether a rim circle is drawn, and inventing a
#: 60 km circle around a deposit nobody has measured would be a lie with a
#: drawing attached.
NOMINAL_KM = {
    # A NELIOTA row is a flash, not a crater: the value is a *marker* size and
    # nothing more.  1 km is about the smallest that is still a dot rather than
    # a sub-pixel speck on a 100 km-wide frame.
    "impact": 1.0,
    # Diffuse deposits, never measured by the atlas.  12 km is a 3 px dot on the
    # same frame; the outline rule does not draw a rim for them.
    "pyroclastic": 12.0,
    # The atlas's historical sites -- Luna 2, Ranger 4, the Apollo descent
    # stages, the GRAIL twins.  What the row records is *where something came
    # down*, and a place something came down at is a point: the atlas publishes
    # a position and no extent, and the IAU's own Statio rows carry 0.01 km for
    # the same reason.  1 km is a marker and nothing else, and the class does
    # not draw a rim circle, so no invented shape reaches the picture.  It also
    # keeps these seventy off the full disk's label floor -- they are only
    # named at the scales at which a landing site is actually a *place* -- while
    # `_family` in annotations.py keeps them out of the landing-site exemption,
    # which is what stops them emptying every other family out of the plan.
    "statio": 1.0,
}


def number(*candidates: Any) -> Optional[float]:
    """The first candidate that is actually a number.

    The atlas fills a cell with prose when it has no value -- ``Not IAU
    approved``, ``No dimension found``, ``??`` -- and those are *truthy*, so a
    plain ``or`` chain hands them to ``float()`` and the row is lost.  The
    numeric columns (``LATI_N``, ``LONGI_N``, ``LENGTH_KM``) come first here,
    and the prose is skipped wherever it appears.
    """
    for candidate in candidates:
        text = str(candidate if candidate is not None else "").strip()
        if not text or text[:3].casefold() in ("not", "non", "no ", "no-") \
                or not text[0].isdigit() and text[0] not in "+-.":
            continue
        try:
            return float(text.replace(",", "."))
        except ValueError:
            continue
    return None


def classify(*candidates: Any) -> Optional[str]:
    """The first candidate the vocabulary knows, tried in the order given.

    The callers pass ``IAU_FEATURE_TYPE`` first: where the atlas's type column
    and the IAU's disagree, the IAU's is the classification and the atlas's is a
    second opinion (see the module docstring on Reiner Gamma).
    """
    for candidate in candidates:
        key = fold(candidate)
        if key in TYPE_MAP:
            return TYPE_MAP[key]
    return None


def read_vma(path: Path) -> Dict[str, Dict[str, str]]:
    """One atlas table, keyed by folded feature name."""
    if not path.exists():
        return {}
    rows: Dict[str, Dict[str, str]] = {}
    with path.open(encoding="utf-8", errors="replace", newline="") as handle:
        for row in csv.DictReader(handle, delimiter=";"):
            name = (row.get("NAME") or "").strip()
            if not name:
                continue
            rows[fold(name)] = row
    return rows


def read_xlsx_names(path: Path) -> Dict[str, str]:
    """English name -> Chinese name, from the project's own compilation."""
    if not path.exists():
        return {}
    import openpyxl  # only needed for the workbook

    names: Dict[str, str] = {}
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet, name_key, zh_key in (("地貌", "Description", "Formation name"),
                                        ("卫星坑", "NAME", "Formation name")):
            if sheet not in book.sheetnames:
                continue
            rows = book[sheet].iter_rows(values_only=True)
            header = [str(cell or "").strip() for cell in next(rows, ())]
            try:
                en_at = header.index(name_key)
                zh_at = header.index(zh_key)
            except ValueError:  # pragma: no cover - the sheets are as documented
                continue
            for row in rows:
                if row is None or len(row) <= max(en_at, zh_at):
                    continue
                english, chinese = row[en_at], row[zh_at]
                if not english or not chinese:
                    continue
                key = fold(english)
                # The named formations sheet has the Chinese name first and the
                # English in `Description`; both are plain strings.
                names.setdefault(key, str(chinese).strip())
    finally:
        book.close()
    return names


def read_lpnd(path: Path) -> Dict[str, Dict[str, str]]:
    """The LPND block of ``lunar_chinese_names.json``: name -> {zh, term_zh, …}."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    names = payload.get("names") if isinstance(payload, dict) else None
    if not isinstance(names, dict):  # pragma: no cover - the file is as documented
        return {}
    return {fold(key): value for key, value in names.items() if isinstance(value, dict)}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the CSV")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)

    ours: List[Dict[str, str]] = []
    with DB.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        ours = list(reader)
    # A row with no size or no position cannot be projected onto the Moon.  The
    # loader refuses it, so it is dropped here -- from *any* source, including an
    # earlier run of this tool -- and reported.
    usable: List[Dict[str, str]] = []
    pruned: List[Tuple[str, str]] = []
    for row in ours:
        size = number(row.get("Diameter"))
        lat = number(row.get("Lat"))
        lon = number(row.get("Lon"))
        if lat is None or lon is None:
            pruned.append((row.get("English Name", ""), "no position"))
            continue
        if size is None or size <= 0.0:
            pruned.append((row.get("English Name", ""), "no diameter"))
            continue
        usable.append(row)
    ours = usable
    print(f"our database: {len(ours)} rows, columns {fieldnames}")
    if pruned:
        print(f"pruned rows without a position or size: {len(pruned)} "
              f"(e.g. {[name for name, _ in pruned[:5]]})")

    named = read_vma(args.out / "01_Named_formations_EN.csv")
    atlas = {"named": named}
    # The filenames are the atlas's own, spaces and all: an underscore here
    # silently loads nothing, which is how 大型撞击盆地 went missing once.
    #
    # ``12_Sinuous rilles_EN.csv`` is deliberately *not* in this list.  Its 210
    # rows are the atlas's own survey of rille *segments* -- ``HURWITZ RILLE 109
    # TOTAL``, ``HURWITZ RILLE 114 BRANCH B`` -- and those labels belong to that
    # drawing rather than to the Moon: nobody named them, nobody can search for
    # them, and 210 of them in the 月溪 class crowded out the 113 rimae the IAU
    # does name.  Those 113 are already here, classified from the same atlas's
    # named-formations table; dropping this one only loses the segments.
    for name in ("03_Historical_sites_EN.csv", "04_Domes_EN.csv",
                 "05_Pyroclastic_formations_EN.csv", "08_NELIOTA impacts_EN.csv",
                 "09_Impact basins_EN.csv", "10_Lunar pits_EN.csv",
                 "11_IMP_EN.csv"):
        table = read_vma(args.out / name)
        if not table:
            raise SystemExit(f"atlas table {name!r} is missing or unreadable in {args.out}")
        atlas[name] = table
    print("atlas tables:", {k: len(v) for k, v in atlas.items()})

    zh = read_xlsx_names(args.out / "named_cn.xlsx")
    print(f"chinese names from named_cn.xlsx: {len(zh)}")
    lpnd = read_lpnd(args.out / "lunar_chinese_names.json")
    print(f"chinese names from LPND: {len(lpnd)}")

    changed_name = 0
    filled_name = 0
    changed_type = 0
    suspect_names: List[Tuple[str, str, str]] = []
    style_names: List[Tuple[str, str, str]] = []
    type_counts: Counter = Counter()
    unmatched_zh: List[str] = []
    for row in ours:
        key = fold(row.get("English Name", ""))
        old_name = (row.get("名称") or "").strip()
        # LPND is the authority for the name; our table already agrees with it
        # everywhere, so this is a check rather than a rewrite.
        official = (lpnd.get(key) or {}).get("zh", "").strip()
        if official and official != old_name:
            row["名称"] = official
            changed_name += 1
        elif not old_name and official:
            row["名称"] = official
            filled_name += 1
        elif not old_name:
            unmatched_zh.append(row.get("English Name", ""))

        book = (zh.get(key) or "").strip()
        if book and old_name and book != old_name:
            # Same stem, different spacing: 阿贝 H against 阿贝坑H.  That is a
            # style difference and worth listing separately from the ones that
            # name a different crater altogether (Schorr -> 薛定谔).
            stem = re.sub(r"[\s\-—]", "", book)[:2]
            if stem and stem in old_name:
                if len(style_names) < 6:
                    style_names.append((row.get("English Name", ""), old_name, book))
            elif len(suspect_names) < 20:
                suspect_names.append((row.get("English Name", ""), old_name, book))

        # Classification: the atlas's own type, then its IAU type, then ours.
        source = named.get(key)
        for table in atlas.values():
            if source is None:
                source = table.get(key)
        new_type = None
        if source is not None:
            new_type = classify(source.get("IAU_FEATURE_TYPE"), source.get("TYPE"),
                                source.get("SUBTYPE"))
        if new_type and new_type != (row.get("Type") or "").strip():
            row["Type"] = new_type
            changed_type += 1
        type_counts[row.get("Type", "")] += 1

    # What the atlas has that we do not, and which is therefore *added*: the
    # atlas is the catalogue of record, and a feature with no Chinese name yet
    # is shown under its English name rather than left out.
    we_have = {fold(row.get("English Name", "")) for row in ours}
    added: Counter = Counter()
    extra: Dict[str, int] = {}
    skipped: List[Tuple[str, str]] = []
    for label, table in atlas.items():
        for key, source in table.items():
            if key in we_have:
                continue
            english = (source.get("NAME") or "").strip()
            if not english:
                continue
            kind = classify(source.get("IAU_FEATURE_TYPE"), source.get("TYPE"),
                            source.get("SUBTYPE")) or "Other"
            # The numeric columns first: the IAU_* ones say "Not IAU approved"
            # for every family the IAU has not named, which is most of these.
            lat = number(source.get("LATI_N"), source.get("IAU_CENTER_LATITUDE"))
            lon = number(source.get("LONGI_N"), source.get("IAU_CENTER_LONGITUDE"))
            size = number(source.get("IAU_DIAMETER"), source.get("LENGTH_KM"),
                          source.get("WIDE_KM"))
            if lat is None or lon is None:
                skipped.append((english, "no position in the atlas"))
                continue
            nominal = False
            if size is None or size <= 0.0:
                size = NOMINAL_KM.get(kind.casefold())
                nominal = size is not None
                if size is None:
                    skipped.append((english, "no diameter in the atlas"))
                    continue
            zh = (lpnd.get(key) or {}).get("zh", "").strip()
            ours.append({
                "English Name": english,
                # The English name stands in until a translation exists; the
                # Note is what says so, because the page shows 名称 verbatim.
                "名称": zh or english,
                "Lat": f"{lat:.6f}",
                "Lon": f"{lon:.6f}",
                "Diameter": f"{size:.4f}",
                "Type": kind,
                "Note": (("标称直径 %.1f km（原表无尺寸）· " % size if nominal else "")
                         + ("Virtual Moon Atlas / " + label.replace("_EN.csv", "")
                            if zh else "待译名（Virtual Moon Atlas / "
                            + label.replace("_EN.csv", "") + "）")),
            })
            we_have.add(key)
            added[kind] += 1
            extra[label] = extra.get(label, 0) + 1

    print()
    print(f"names corrected to LPND: {changed_name}")
    print(f"names filled from LPND:  {filled_name}")
    print(f"classes re-assigned:     {changed_type}")
    print(f"still without a name:    {len(unmatched_zh)} {unmatched_zh[:8]}")
    print("classes now:", type_counts.most_common())
    if suspect_names:
        print("\nworkbook names that name a *different* feature (never applied):")
        for english, mine, book in suspect_names[:8]:
            print(f"  {english:28s} ours={mine!r:22s} workbook={book!r}")
    if style_names:
        print("\nworkbook names that differ only in spacing:")
        for english, mine, book in style_names[:3]:
            print(f"  {english:28s} ours={mine!r:22s} workbook={book!r}")
    if not added:
        # Nothing new this run: report what the table already carries, so the
        # provenance does not lose the record on a re-run.
        for row in ours:
            if "Virtual Moon Atlas" in (row.get("Note") or ""):
                added[row.get("Type", "Other")] += 1
    print("\nadded from the atlas:", dict(added), "total", sum(added.values()))
    print("by table:", extra)
    if skipped:
        print(f"skipped rows without a position or size: {len(skipped)} "
              f"(e.g. {[name for name, _ in skipped[:5]]})")

    if args.write:
        with DB.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(ours)
        provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
        provenance["features"] = len(ours)
        provenance["named"] = sum(1 for r in ours if (r.get("名称") or "").strip())
        provenance["coverage_percent"] = round(
            100.0 * provenance["named"] / max(1, len(ours)), 1)
        provenance["sources"].update({
            "catalogue": [
                "out/01_Named_formations_EN.csv",
                "out/03_Historical_sites_EN.csv",
                "out/04_Domes_EN.csv",
                "out/05_Pyroclastic_formations_EN.csv",
                "out/08_NELIOTA_impacts_EN.csv",
                "out/09_Impact_basins_EN.csv",
                "out/10_Lunar_pits_EN.csv",
                "out/11_IMP_EN.csv",
                "out/named_cn.xlsx",
                "out/lunar_chinese_names.json",
            ],
            "catalogue_note": (
                "Virtual Moon Atlas (https://www.ap-i.net/avl/en/start) tables, "
                "the project's own named_cn.xlsx compilation, and Chinese names "
                "from LPND 月球与行星地名库 (NADC)."
            ),
        })
        provenance["catalogue_merge"] = {
            "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "names_filled": filled_name,
            "names_changed": changed_name,
            "classes_reassigned": changed_type,
            "classes": dict(type_counts),
            "workbook_names_rejected":
                "reported, never applied: the workbook names a different feature "
                "in places and disagrees with LPND on 7015 of 8774 rows",
            "atlas_added": dict(added),
            "atlas_added_by_table": extra or provenance.get(
                "catalogue_merge", {}).get("atlas_added_by_table", {}),
            "atlas_skipped": [{"name": name, "why": why} for name, why in skipped],
            "atlas_added_note": (
                "Features the atlas has and the IAU export did not.  They carry "
                "their English name in the name column until a translation "
                "exists, and say so in the Note."
            ),
            "workbook_name_note": (
                "named_cn.xlsx disagrees with LPND on 7015 of 8774 features and is "
                "misaligned in places (Schorr -> 薛定谔, Quetelet -> 伦琴).  Its names "
                "are treated as suspect and are not applied; see the tool's docstring."
            ),
            "regenerate": "python3 tools/merge_out_catalogue.py --write",
        }
        PROVENANCE.write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {DB} and {PROVENANCE.name}")
    else:
        print("\n(report only: pass --write to rewrite the database)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
