#!/usr/bin/env python3
"""Rebuild the named-landform catalogue used for annotation overlays.

The annotation layer needs *every* named feature, not the few dozen showpieces
in ``lunar_features.csv``: a close-up of an arbitrary area should be able to say
what is in it.  This tool turns the authoritative USGS / IAU gazetteer into the
same CSV shape the rest of ``moonlib`` already reads, so
:func:`moonlib.load_nomenclature` needs no special parser.

Source
------
``MOON_nomenclature_center_pts.zip`` from the USGS Astrogeology
``asc-planetarynames-data`` bucket -- the point edition of the
[IAU Planetary Nomenclature](https://planetarynames.wr.usgs.gov/GIS_Downloads)
gazetteer for the Moon.  It is a shapefile whose attributes live in a dBase
``.dbf``; that file is read here directly (about forty lines of ``struct``)
rather than through a shapefile library, because the coordinates this needs are
in the attribute table and adding a dependency for them would be silly.

The zip is cached under ``.cache/moonlib/nomenclature/`` and never committed:
only the derived CSV is, and it is small enough to travel with the package.

Columns written (identical to ``lunar_features.csv``)
-----------------------------------------------------
``English Name,名称,Lat,Lon,Diameter,Type,Note``

``名称`` is left empty: this tool fetches the IAU gazetteer only.  The shipped
database is built from the *user's* two tables by ``tools/build_landform_db.py``,
which is also where the Chinese names come from; this script exists to refresh
the undamaged spellings that repair depends on.

Usage
-----
    ./.venv/bin/python tools/fetch_nomenclature.py                  # download + rebuild
    ./.venv/bin/python tools/fetch_nomenclature.py --zip moon.zip   # from a local copy
    ./.venv/bin/python tools/fetch_nomenclature.py --check          # verify the CSV only
"""

from __future__ import annotations

import argparse
import os
import json
import struct
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from _bootstrap import CATALOGUE_COLUMNS, ROOT, normalise_lon, use_src  # noqa: E402

moonlib = use_src()

import moonlib  # noqa: E402

CATALOGUE_URL = (
    "https://asc-planetarynames-data.s3.us-west-2.amazonaws.com/"
    "MOON_nomenclature_center_pts.zip"
)
#: Derived file, read by :func:`moonlib.load_nomenclature`.
TARGET = ROOT / "src" / "moonlib" / "data" / "lunar_nomenclature.csv"
PROVENANCE = TARGET.with_suffix(".provenance.json")
USER_AGENT = "moonlib-nomenclature/1.0 (offline lunar simulator)"

#: Gazetteer types that name a *place* worth annotating, mapped to the short
#: kind the renderer and the page display.  Anything absent keeps the first word
#: of its USGS type, so a new IAU category shows up instead of being dropped.
KIND_NAMES = {
    "Crater": "Crater",
    "Satellite Feature": "Crater",
    "Astronaut-named features": "Crater",
    "Mare": "Mare",
    "Oceanus": "Oceanus",
    "Lacus": "Lacus",
    "Sinus": "Sinus",
    "Palus": "Palus",
    "Mons": "Mons",
    "Montes": "Mons",
    "Rima": "Rima",
    "Rupes": "Rupes",
    "Vallis": "Vallis",
    "Dorsum": "Dorsum",
    "Catena": "Catena",
    "Promontorium": "Promontorium",
}


# ---------------------------------------------------------------------------
# dBase III attribute table
# ---------------------------------------------------------------------------
class DbfError(ValueError):
    """The ``.dbf`` is not the attribute table this tool understands."""


def read_dbf_fields(raw: bytes) -> List[Dict[str, Any]]:
    """Field descriptors of a dBase file: name, type, length, decimals."""
    if len(raw) < 32:
        raise DbfError("the .dbf is shorter than its own header.")
    fields: List[Dict[str, Any]] = []
    position = 32
    while position + 32 <= len(raw) and raw[position] != 0x0D:
        descriptor = raw[position:position + 32]
        name = descriptor[:11].split(b"\x00")[0].decode("ascii", "replace")
        fields.append(
            {
                "name": name,
                "type": chr(descriptor[11]),
                "length": descriptor[16],
                "decimals": descriptor[17],
            }
        )
        position += 32
    if not fields:
        raise DbfError("the .dbf declares no fields.")
    return fields


def read_dbf_records(raw: bytes, encoding: str = "utf-8") -> Iterable[Dict[str, str]]:
    """Every live record of a dBase file, decoded to stripped strings."""
    fields = read_dbf_fields(raw)
    record_count = struct.unpack("<I", raw[4:8])[0]
    header_bytes = struct.unpack("<H", raw[8:10])[0]
    record_bytes = struct.unpack("<H", raw[10:12])[0]
    position = header_bytes
    for _ in range(record_count):
        chunk = raw[position:position + record_bytes]
        position += record_bytes
        if len(chunk) < record_bytes:  # pragma: no cover - truncated file
            break
        if chunk[:1] == b"*":  # deleted
            continue
        offset = 1
        record: Dict[str, str] = {}
        for field in fields:
            cell = chunk[offset:offset + field["length"]]
            offset += field["length"]
            record[field["name"]] = cell.decode(encoding, "replace").strip()
        yield record


def dbf_from_zip(archive: Path) -> bytes:
    """The Moon gazetteer's ``.dbf``, out of the downloaded zip."""
    import zipfile

    with zipfile.ZipFile(archive) as bundle:
        names = [n for n in bundle.namelist() if n.lower().endswith(".dbf")]
        if not names:
            raise DbfError(f"{archive.name} holds no .dbf.")
        return bundle.read(names[0])


# ---------------------------------------------------------------------------
# turning records into the shipped CSV
# ---------------------------------------------------------------------------
def short_kind(type_text: str) -> str:
    """``"Mare, maria"`` -> ``"Mare"``; unknown categories keep their first word."""
    head = str(type_text or "").split(",")[0].strip()
    return KIND_NAMES.get(head, head or "Feature")


def gazetteer_rows(raw: bytes, encoding: str = "utf-8") -> List[Dict[str, Any]]:
    """Usable rows of the gazetteer, in CSV column order.

    Rows without a positive diameter are dropped: every annotation draws a size
    and a position for its feature, and a zero diameter would put a full-size
    label on something the gazetteer itself does not measure.
    """
    rows: List[Dict[str, Any]] = []
    skipped = 0
    for record in read_dbf_records(raw, encoding):
        name = (record.get("name") or "").strip()
        try:
            lat = float(record.get("center_lat") or "")
            lon = normalise_lon(record.get("center_lon") or "")
            diameter = float(record.get("diameter") or "")
        except ValueError:
            skipped += 1
            continue
        if not name or not (-90.0 <= lat <= 90.0) or diameter <= 0.0:
            skipped += 1
            continue
        rows.append(
            {
                "English Name": name,
                "名称": "",
                "Lat": round(lat, 5),
                "Lon": round(lon, 5),
                "Diameter": round(diameter, 3),
                "Type": short_kind(record.get("type", "")),
                "Note": "",
            }
        )
    if not rows:
        raise DbfError("no usable rows in the gazetteer.")
    if skipped:
        print(f"  skipped    : {skipped} rows without a usable position/size")
    rows.sort(key=lambda row: row["English Name"])
    return rows


def write_catalogue(
    rows: Sequence[Dict[str, Any]],
    target: Path,
    source: str = CATALOGUE_URL,
) -> None:
    """Write the CSV and its provenance file."""
    import csv

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

    PROVENANCE.parent.mkdir(parents=True, exist_ok=True)
    PROVENANCE.write_text(
        json.dumps(
            {
                "file": target.name,
                "source": source,
                "fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "licence": "IAU / USGS Planetary Nomenclature: public domain (US Government work).",
                "columns": list(CATALOGUE_COLUMNS),
                "rows": len(rows),
                "chinese_names": "not in this file; see tools/build_landform_db.py",
                "regenerate": "python tools/fetch_nomenclature.py",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# download and command line
# ---------------------------------------------------------------------------
def download(url: str, destination: Path, force: bool = False) -> Path:
    destination = Path(destination)
    if destination.is_file() and not force:
        print(f"  cached     : {destination}")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    print(f"  downloading: {url}")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=120) as response:
            total = int(response.headers.get("Content-Length") or 0)
            written = 0
            with temporary.open("wb") as handle:
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    handle.write(chunk)
                    written += len(chunk)
                    if total:
                        print(f"\r  progress   : {written / 1e6:7.1f} / {total / 1e6:.1f} MB",
                              end="", flush=True)
            print()
    except Exception as error:  # network is the one thing that can fail here
        temporary.unlink(missing_ok=True)
        raise SystemExit(f"download failed: {type(error).__name__}: {error}")
    temporary.replace(destination)
    return destination


def check(target: Path = TARGET) -> int:
    """Independently re-read the derived CSV the way the library will."""
    if not target.is_file():
        print(f"missing    : {target}")
        return 1
    features = moonlib.load_nomenclature(target)
    kinds: Dict[str, int] = {}
    for feature in features:
        kinds[feature.kind] = kinds.get(feature.kind, 0) + 1
    print(f"  file       : {os.path.relpath(target)}")
    print(f"  features   : {len(features)}")
    print(f"  with 中文  : {sum(1 for f in features if f.name_zh)}")
    print("  kinds      : " + ", ".join(
        f"{kind} {count}" for kind, count in sorted(kinds.items(), key=lambda kv: -kv[1])
    ))
    biggest = max(features, key=lambda f: f.diameter_km)
    print(f"  largest    : {biggest.name_en} {biggest.diameter_km:.0f} km")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--zip", type=Path, help="local copy of the USGS zip")
    parser.add_argument("--url", default=CATALOGUE_URL, help="gazetteer zip to download")
    parser.add_argument("--out", type=Path, default=TARGET, help="CSV to write")
    parser.add_argument("--force", action="store_true", help="re-download the zip")
    parser.add_argument("--check", action="store_true", help="verify the CSV and stop")
    args = parser.parse_args(argv)

    if args.check:
        return check(args.out)

    archive = args.zip or download(
        args.url, moonlib.cache_dir() / "nomenclature" / Path(args.url).name, force=args.force
    )
    raw = dbf_from_zip(archive)
    rows = gazetteer_rows(raw)
    print(f"  gazetteer  : {len(rows)} features with a position and a size")
    write_catalogue(rows, args.out, source=args.url)
    print(f"  written    : {os.path.relpath(args.out)} ({args.out.stat().st_size / 1024:.0f} KB)")
    print(f"  provenance : {os.path.relpath(PROVENANCE)}")
    print("  next       : python tools/build_landform_db.py --features <iau.csv> "
          "--translation <月球地名.xlsx> --spelling <this file>")
    return check(args.out)


if __name__ == "__main__":
    raise SystemExit(main())
