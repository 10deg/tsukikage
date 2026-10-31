#!/usr/bin/env python3
"""Export basins, landing sites, domes and pyroclastic patches as a Markdown table.

The web client and the map service already read one catalogue
(``moonlib.annotation_catalogue()``); this file is the *human* view of the
classes whose Chinese names are worth reviewing by hand.  The 暂译名 column is
derived from the surrounding named features (a dome on Arago, the basin of Mare
Imbrium); copy a draft into 中文名 when it is accepted, then feed the table back
through ``tools/apply_feature_names_md.py``.

Usage::

    ./.venv/bin/python tools/export_feature_names_md.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import moonlib  # noqa: E402
from moonlib.annotations import CLASS_LABELS, feature_class  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provisional_names import Index, provisional_name  # noqa: E402

TARGET = ROOT / "docs" / "地貌中文名-大型撞击坑与着陆点.md"


def _cell(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    return text.replace("|", "\\|").replace("\n", " ")


def rows() -> List[Dict[str, Any]]:
    catalogue = moonlib.annotation_catalogue()
    index = Index(catalogue)
    selected: List[Dict[str, Any]] = []
    for feature in catalogue:
        group = feature_class(feature.kind, feature.diameter_km)
        if group not in {"basin", "statio", "dome", "pyroclastic"}:
            continue
        selected.append({
            "name_en": feature.name_en,
            "name_zh": feature.name_zh,
            "provisional": provisional_name(feature, catalogue, index),
            "lat": float(feature.lat_deg),
            "lon": float(feature.lon_deg),
            "diameter_km": float(feature.diameter_km),
            "kind": feature.kind,
            "class": group,
            "class_zh": CLASS_LABELS.get(group, (group, group))[0],
            "note": feature.note,
        })

    order = {"basin": 0, "statio": 1, "dome": 2, "pyroclastic": 3}
    selected.sort(key=lambda row: (
        order.get(row["class"], 9), -row["diameter_km"], row["name_en"]))
    return selected

def render(items: List[Dict[str, Any]]) -> str:
    lines = [
        "# 地貌中文名：撞击盆地、着陆点、穹丘与火成碎屑",
        "",
        "> 本表由 `tools/export_feature_names_md.py` 从 `src/moonlib/data/"
        "lunar_nomenclature.csv` 生成。网页客户端与月面地图服务读取的是同一份"
        "地貌库。",
        ">",
        "> 收录：大型撞击盆地（basin）、着陆点（statio）、穹丘（dome）、"
        "火成碎屑（pyroclastic）。",
        ">",
        "> **“暂译名”是按附近已有地貌推导的校对草稿**，尚未写回数据库。"
        "确认后把名字抄进“中文名”列，再运行 "
        "`./.venv/bin/python tools/apply_feature_names_md.py` 写回 CSV。"
        "英文名列是匹配键，不要改。",
        "",
        "| 分类 | 英文名 | 中文名 | 暂译名 | 纬度 | 经度 | 直径 km | 类型 | 备注 |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | --- | --- |",
    ]
    for row in items:
        lines.append(
            "| {class_zh} | {name_en} | {name_zh} | {provisional} | {lat:.4f} | "
            "{lon:.4f} | {diameter:.1f} | {kind} | {note} |".format(
                class_zh=_cell(row["class_zh"]),
                name_en=_cell(row["name_en"]),
                name_zh=_cell(row["name_zh"]),
                provisional=_cell(row.get("provisional")),
                lat=row["lat"],
                lon=row["lon"],
                diameter=row["diameter_km"],
                kind=_cell(row["kind"]),
                note=_cell(row["note"]),
            )
        )
    lines.append("")
    return "\n".join(lines)

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", type=Path, default=TARGET)
    args = parser.parse_args(argv)
    items = rows()
    args.target.parent.mkdir(parents=True, exist_ok=True)
    args.target.write_text(render(items), encoding="utf-8")
    print(f"wrote {args.target} ({len(items)} features)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
