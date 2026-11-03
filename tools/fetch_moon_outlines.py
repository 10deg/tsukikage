#!/usr/bin/env python3
"""下载月球全部 IAU 已批准地名的矢量轮廓：存一份原始存档，再派生一份带排字标准的绘图文件。

数据来源
--------
USGS 行星命名网站（planetarynames.wr.usgs.gov）的每个地物页面（如
``/Feature/16118``）都内嵌一段 WKT 轮廓，供页面上的地图绘制。地图组件
CartoCosmos 本身不使用这些内嵌 WKT，而是请求同一个机构的 WFS 服务：

    https://planetarymaps.usgs.gov/cgi-bin/mapserv?map=/maps/earth/moon_nomen_wfs.map

其中的 ``MOON_POLY`` 图层就是 WKT 的矢量数据源：每个要素带
``feature_id``（与网页 URL 一致）以及 GML 几何。WFS 只收录 IAU 已
批准/已通过的正式地名，天然满足“不抓未通过条目”的要求。

分类逻辑
--------
官网总览页 ``/Page/MOON/target`` 按 feature type 列出了全部类别（例如
“Crater, craters”“Mare, maria”），正是要保留的原始分类。本脚本：

1. 爬取总览页，按页面顺序提取 (type_id, 类型名) 分类清单；
2. 按官方分类逐类向 WFS 的 ``MOON_POLY`` 面图层请求要素（每页
   ``--maxfeatures`` 条，默认 2000；大分类自动分页），每两个请求之间
   至少间隔 ``--delay`` 秒（默认 3）；
3. 再从 ``MOON_POINT`` 点图层取一遍属性，得到“全部已批准要素”清单；
4. 对缺少面轮廓的要素（多为 Rima/Rupes/Vallis 等线状地物），逐个读取
   详情页内嵌的 WKT（``MULTILINESTRING`` 等）；
5. 写两份文件：
   * ``out/moon_outlines_raw.json`` —— 原始存档：按分类分组、源坐标原样、
     不抽稀、不做任何几何变换；
   * ``out/moon_outlines.geojson`` —— 绘图用：扁平 ``FeatureCollection``，
     0.25 km 抽稀，每个要素附 ``label``（圆/走向线/内部中心线/点），
     供地图服务直接绘制与排字。

礼貌与可靠
----------
* 串行、限速：请求之间保留固定间隔，失败指数退避重试（最多 4 次）；
* 只对缺轮廓的要素回退抓页面，不逐页扫全部九千个；
* 网页与 WFS 响应会缓存到 ``.cache/moonlib/nomen_outlines/``，重复运行不
  会再次请求（除非 ``--force``）；
* 调排字规则时用 ``--build-draw``，从存档离线重建，完全不联网。
"""

from __future__ import annotations

import argparse
import binascii
import json
import math
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from _bootstrap import ROOT  # noqa: E402

CACHE_DIR = ROOT / ".cache" / "moonlib" / "nomen_outlines"

TARGET_PAGE = "https://planetarynames.wr.usgs.gov/Page/MOON/target"
WFS_HOST = "https://planetarymaps.usgs.gov"
WFS_PATH = "/cgi-bin/mapserv"
WFS_MAP = "/maps/earth/moon_nomen_wfs.map"
WFS_LAYER = "MOON_POLY"
POINT_LAYER = "MOON_POINT"
OUT_DEFAULT = ROOT / "out" / "moon_outlines.geojson"
RAW_DEFAULT = ROOT / "out" / "moon_outlines_raw.json"

USER_AGENT = (
    "moonlib-outline-fetch/0.1 "
    "(USGS/IAU planetary nomenclature outline archive; sequential polite requests)"
)

GML_NS = "http://www.opengis.net/gml"


def gml(tag: str) -> str:
    """把标签名变成带 GML 命名空间的限定名，用于 ElementTree 查找。"""
    return "{%s}%s" % (GML_NS, tag)


# ---------------------------------------------------------------------------
# 网络请求（带缓存、间隔、重试）
# ---------------------------------------------------------------------------
class PoliteFetcher:
    """串行抓取器：请求之间保持固定间隔，失败时指数退避重试。"""

    def __init__(self, delay: float = 3.0, timeout: float = 180.0,
                 retries: int = 4, force: bool = False) -> None:
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.force = force
        self._last = None
        self.requests = 0

    def _wait(self) -> None:
        if self._last is not None:
            remaining = self.delay - (time.monotonic() - self._last)
            if remaining > 0:
                time.sleep(remaining)

    def get(self, url: str) -> bytes:
        if CACHE_DIR is not None:
            cached = CACHE_DIR / (sha1(url.encode("utf-8")).hexdigest() + ".bin")
            if cached.is_file() and not self.force:
                return cached.read_bytes()

        for attempt in range(self.retries + 1):
            self._wait()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    body = response.read()
                self.requests += 1
                self._last = time.monotonic()
                if CACHE_DIR is not None:
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    cached.write_bytes(body)
                return body
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                if attempt == self.retries:
                    raise SystemExit(
                        f"抓取失败（已重试 {self.retries} 次）: {url}\n{error}")
                backoff = 5 * 2 ** attempt
                print(f"  临时错误，{backoff} 秒后重试: {error}", file=sys.stderr)
                time.sleep(backoff)
        raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# GML 解析
# ---------------------------------------------------------------------------
def decode_wkb_point(text: str) -> Tuple[float, float]:
    """把 WKB 点（十六进制）解析成 (经度, 纬度)。"""
    raw = binascii.unhexlify(text.strip())
    if len(raw) < 21:
        raise ValueError("WKB 点太短")
    endian = "<" if raw[0] == 1 else ">"
    gtype = struct.unpack(endian + "I", raw[1:5])[0]
    if gtype % 1000 != 1:
        raise ValueError(f"不是点要素, type={gtype}")
    x, y = struct.unpack(endian + "dd", raw[5:21])
    return x, y


def poslist_to_points(elem: ET.Element) -> List[Tuple[float, float]]:
    """在 elem 子树里找 gml:posList，解析成 [(纬度, 经度), ...]（保持源顺序）。"""
    points: List[Tuple[float, float]] = []
    for pl in elem.iter(gml("posList")):
        nums = [float(v) for v in (pl.text or "").split()]
        points.extend((nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2))
    return points


def geometry_from(geom_elem: ET.Element) -> Dict[str, Any]:
    """把 <ms:msGeometry> 子树转成 GeoJSON 风格的几何对象。"""
    polygons, lines = [], []
    for poly in geom_elem.iter(gml("Polygon")):
        rings = []
        for ring in poly.iter(gml("LinearRing")):
            pts = poslist_to_points(ring)
            if pts:
                # GeoJSON 坐标次序为 [经度, 纬度]
                rings.append([[lon, lat] for lat, lon in pts])
        if rings:
            polygons.append(rings)
    for ls in geom_elem.iter(gml("LineString")):
        pts = poslist_to_points(ls)
        if pts:
            lines.append([[lon, lat] for lat, lon in pts])
    if polygons:
        return {
            "type": "Polygon" if len(polygons) == 1 else "MultiPolygon",
            "coordinates": polygons[0] if len(polygons) == 1 else polygons,
        }
    if lines:
        return {
            "type": "LineString" if len(lines) == 1 else "MultiLineString",
            "coordinates": lines[0] if len(lines) == 1 else lines,
        }
    return {"type": None, "coordinates": None}


def parse_records(body: bytes) -> List[Dict[str, Any]]:
    """解析 WFS 的 GML 响应，逐条产出要素记录。"""
    root = ET.fromstring(body)
    records: List[Dict[str, Any]] = []
    for member in root.iter(gml("featureMember")):
        feature = list(member)[0]
        rec: Dict[str, Any] = {}
        feature_id = feature.get(gml("id"))
        if feature_id:
            rec["feature_id"] = feature_id
        for child in feature:
            name = child.tag.split("}", 1)[-1]
            if name == "boundedBy":
                continue
            if name == "msGeometry":
                rec["geometry"] = geometry_from(child)
            elif name == "center_point":
                try:
                    x, y = decode_wkb_point(child.text or "")
                    rec["center"] = {"lon": x, "lat": y}
                except ValueError:
                    pass
            else:
                rec[name] = (child.text or "").strip()
        if rec:
            records.append(rec)
    return records


# ---------------------------------------------------------------------------
# 详情页内嵌 WKT（线状地物不在 MOON_POLY 里，回退到这里）
# ---------------------------------------------------------------------------
#: One geometry row: the control network's id is in the row class.
WKT_ROW_RE = re.compile(
    r'<tr class="wkt wkt-(?P<id>\d+)"[^>]*>\s*<th[^>]*>[^<]*</th>\s*'
    r'<td class="wkt_string[^"]*">\s*(?P<kind>[A-Z]+)\s*(?P<body>\(.*?\))\s*</td>',
    re.S,
)
WKT_JS_RE = re.compile(r"drawWkt\s+([A-Z]+)\s*(\(.*?\))'", re.S)
#: The page's control-network picker; the selected one is what its map draws.
CONTROL_NET_RE = re.compile(
    r'<select[^>]*id="selected_control_net"[^>]*>(.*?)</select>', re.S
)
OPTION_RE = re.compile(r'<option[^>]*value="(?P<id>\d+)"[^>]*>', re.S)


def _wkt_groups(text: str) -> List[str]:
    """把 WKT 的括号体拆成顶层括号组（结果不含外层括号）。"""
    groups: List[str] = []
    depth, start = 0, None
    for index, char in enumerate(text):
        if char == "(":
            if depth == 0:
                start = index + 1
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0 and start is not None:
                groups.append(text[start:index])
                start = None
    return groups


def _wkt_coords(text: str) -> List[List[float]]:
    """'lon lat, lon lat' -> [[lon, lat], ...]，并把 0..360 归一到 -180..180。"""
    points: List[List[float]] = []
    for pair in text.strip().strip("()").split(","):
        parts = pair.split()
        if len(parts) < 2:
            continue
        lon = ((float(parts[0]) + 180.0) % 360.0) - 180.0
        points.append([lon, float(parts[1])])
    return points


def _strip_outer(text: str) -> str:
    """去掉 WKT 最外层的一对括号。"""
    text = text.strip()
    if text.startswith("(") and text.endswith(")"):
        return text[1:-1]
    return text


def wkt_to_geometry(kind: str, body: str) -> Dict[str, Any]:
    """把 WKT 文本转成 GeoJSON 风格几何（经度在前）。"""
    inner = _strip_outer(body)
    if kind == "LINESTRING":
        return {"type": "LineString", "coordinates": _wkt_coords(inner)}
    if kind == "MULTILINESTRING":
        return {"type": "MultiLineString",
                "coordinates": [_wkt_coords(group) for group in _wkt_groups(inner)]}
    if kind == "POLYGON":
        return {"type": "Polygon",
                "coordinates": [_wkt_coords(group) for group in _wkt_groups(inner)]}
    if kind == "MULTIPOLYGON":
        return {"type": "MultiPolygon",
                "coordinates": [[_wkt_coords(ring) for ring in _wkt_groups(polygon)]
                                for polygon in _wkt_groups(inner)]}
    if kind == "POINT":
        points = _wkt_coords(inner)
        if not points:
            raise ValueError("空点")
        return {"type": "Point", "coordinates": points[0]}
    raise ValueError(f"不支持的 WKT 类型: {kind}")


def selected_control_net(html: str) -> Optional[str]:
    """The control network the page shows by default ("LOLA 2011" on 5040)."""
    match = CONTROL_NET_RE.search(html)
    if not match:
        return None
    fallback = None
    for option in OPTION_RE.finditer(match.group(1)):
        fallback = fallback or option.group("id")
        if re.search(r"\bselected\b", option.group(0)):
            return option.group("id")
    return fallback


def wkt_from_page(html: str) -> Optional[Dict[str, Any]]:
    """The geometry the page's own map draws.

    A feature page lists one WKT per control network (UNKNOWN, LOLA 2011,
    ULCN 2005, ...) and its map draws the *selected* one.  Taking the first
    cell instead reads the UNKNOWN row, which is often just a bounding box --
    that is how Rima Ariadaeus arrived as a 7.9 deg x 3.0 deg rectangle.
    """
    rows = {match.group("id"): (match.group("kind"), match.group("body"))
            for match in WKT_ROW_RE.finditer(html)}
    chosen = rows.get(selected_control_net(html) or "")
    if chosen is not None:
        try:
            return wkt_to_geometry(*chosen)
        except ValueError:
            pass
    for kind, body in rows.values():
        try:
            return wkt_to_geometry(kind, body)
        except ValueError:
            continue
    for match in WKT_JS_RE.finditer(html):
        try:
            return wkt_to_geometry(match.group(1), match.group(2))
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# 官方分类（总览页）
# ---------------------------------------------------------------------------
def categories_from_page(html: str) -> List[Tuple[str, str]]:
    """解析总览页，返回 (type_id, 类型名)，保持页面顺序。"""
    link_re = re.compile(
        r'href="/SearchResults\?Target=16_Moon&(?:amp;)?Feature%20Type='
        r"(?P<id>\d+)_[^\"]+?\"[^>]*title=\"(?P<name>[^\"]*)\""
    )
    seen: List[Tuple[str, str]] = []
    for match in link_re.finditer(html):
        pair = (match.group("id"), urllib.parse.unquote(match.group("name")))
        if pair not in seen:
            seen.append(pair)
    return seen


# ---------------------------------------------------------------------------
# 组织输出
# ---------------------------------------------------------------------------
def make_document(
    records: Sequence[Dict[str, Any]],
    categories: Sequence[Tuple[str, str]],
    warnings: Sequence[str],
    meta: Dict[str, Any],
) -> Dict[str, Any]:
    by_type: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        cat = rec.get("type") or "未分类"
        if cat not in by_type:
            by_type[cat] = {"type": cat, "count": 0, "features": []}
        by_type[cat]["features"].append(rec)
        by_type[cat]["count"] = len(by_type[cat]["features"])

    groups = []
    total = 0
    for _, cat_name in categories:
        group = by_type.pop(cat_name, None)
        if group is None:
            group = {"type": cat_name, "count": 0, "features": []}
        group["features"].sort(key=lambda r: (
            r.get("feature", ""), str(r.get("feature_id", ""))))
        total += group["count"]
        groups.append(group)

    for cat, group in sorted(by_type.items()):
        group["type"] = f"{cat}（仅存在于 WFS，总览页未列出）"
        group["features"].sort(key=lambda r: (
            r.get("feature", ""), str(r.get("feature_id", ""))))
        total += group["count"]
        groups.append(group)
        warnings.append(f"追加了总览页未列出的分类: {cat!r} 共 {group['count']} 个")

    merged_meta = dict(meta)
    merged_meta["fetched_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "meta": merged_meta,
        "warnings": list(warnings),
        "totals": {"features": total, "classifications": len(groups)},
        "classifications": groups,
    }


def _simplify(points: List[List[float]], tolerance: float) -> List[List[float]]:
    """Douglas-Peucker，按经度乘 cos(纬度) 修正后的平面距离。"""
    if tolerance <= 0 or len(points) < 3:
        return points
    mean_lat = sum(p[1] for p in points) / len(points)
    scale = math.cos(math.radians(mean_lat)) or 1.0
    pts = [(p[0] * scale, p[1]) for p in points]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = pts[i]
        bx, by = pts[j]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        best, index = -1.0, -1
        for k in range(i + 1, j):
            px, py = pts[k]
            distance = (math.hypot(px - ax, py - ay) if norm == 0
                        else abs(dy * px - dx * py + bx * ay - by * ax) / norm)
            if distance > best:
                best, index = distance, k
        if best > tolerance:
            keep[index] = True
            stack.append((i, index))
            stack.append((index, j))
    return [p for p, flag in zip(points, keep) if flag]


def simplify_geometry(geom: Dict[str, Any], tolerance: float) -> Dict[str, Any]:
    """对 Polygon / MultiPolygon / LineString 做抽稀，环保持闭合。"""
    kind = geom.get("type")
    coords = geom.get("coordinates")
    if not coords or tolerance <= 0:
        return geom
    if kind == "LineString":
        return {"type": kind, "coordinates": _simplify(coords, tolerance)}
    if kind == "MultiLineString":
        return {"type": kind, "coordinates": [_simplify(line, tolerance) for line in coords]}
    if kind == "Polygon":
        return {"type": kind, "coordinates": [_ring(ring, tolerance) for ring in coords]}
    if kind == "MultiPolygon":
        return {"type": kind,
                "coordinates": [[_ring(ring, tolerance) for ring in polygon] for polygon in coords]}
    return geom


def _ring(ring: List[List[float]], tolerance: float) -> List[List[float]]:
    simplified = _simplify(ring, tolerance)
    if len(simplified) < 4:  # 退化后不足一个三角形，保留原环
        return ring
    if simplified[0] != simplified[-1]:
        simplified.append(simplified[0])
    return simplified


def _round_ring(points: Any, digits: int = 5) -> Any:
    if isinstance(points, list) and points and isinstance(points[0], (int, float)):
        return [round(points[0], digits), round(points[1], digits)]
    return [_round_ring(item, digits) for item in points]


# ---------------------------------------------------------------------------
# 排字几何（label）：轮廓是用来“画边界”的，排字需要的是走向
#
# 源轮廓直接拿来排字有三个毛病：环形坑用几十个顶点画一个近似的圆，浪费性能；
# 矩形兜底的月溪没有走向；长条形地物没有可以沿着排的路径。这里按分类从
# 原始轮廓推出 label（绘制与排字标准），geometry 仍原样保留，供填充使用。
# ---------------------------------------------------------------------------
MOON_RADIUS_KM = 1737.4
KM_PER_DEG = 2.0 * math.pi * MOON_RADIUS_KM / 360.0   # 30.32 km / 纬度度

MIN_LABEL_KM = 12.0          # 短于这个长度的路径不值得沿着排字
SHARP_TURN_DEG = 55.0        # 转角超过它视为弯折过急，需要断成连续段
ELONGATED_ASPECT = 1.6       # 长短轴比超过它才认为有明确走向
RECT_FILL_RATIO = 0.97       # 面积 / 包围盒面积，用来识别“只给了矩形范围”
LABEL_SIMPLIFY_KM = 0.25     # 排字路径的抽稀容差

CRATER_TYPES = {"Crater, craters", "Satellite Feature"}
LINE_TYPES = {"Rima, rimae", "Vallis, valles", "Rupes, rupēs", "Catena, catenae"}
RIDGE_TYPES = {"Mons, montes", "Dorsum, dorsa"}


def _iter_points(coords: Any):
    if isinstance(coords, list) and coords and isinstance(coords[0], (int, float)):
        yield coords
    else:
        for item in coords or []:
            yield from _iter_points(item)


def _center_of(points: Sequence[Sequence[float]]) -> Tuple[float, float]:
    return (sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points))


def _km_scale(lat: float) -> float:
    """该纬度上 1 经度对应多少 km（极小值保护，避免极点除零）。"""
    return KM_PER_DEG * max(0.05, math.cos(math.radians(lat)))


def _to_local(points, lon0: float, lat0: float):
    kx = _km_scale(lat0)
    return [((p[0] - lon0) * kx, (p[1] - lat0) * KM_PER_DEG) for p in points]


def _to_lonlat(x: float, y: float, lon0: float, lat0: float) -> List[float]:
    return [lon0 + x / _km_scale(lat0), lat0 + y / KM_PER_DEG]


def _main_axis(points, lon0: float, lat0: float):
    """点集的主轴（按边长加权的 PCA）：中心、方向、长、宽，单位 km。

    直接对顶点做 PCA 会被重复顶点和疏密不均带偏（矩形能偏出 2°），
    所以用每条边的中点、按边长加权。
    """
    pts = _to_local(points, lon0, lat0)
    samples = []
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        length = math.hypot(x2 - x1, y2 - y1)
        if length > 1e-9:
            samples.append(((x1 + x2) / 2.0, (y1 + y2) / 2.0, length))
    if not samples:
        samples = [(p[0], p[1], 1.0) for p in pts]
    wsum = sum(s[2] for s in samples) or 1.0
    mx = sum(s[0] * s[2] for s in samples) / wsum
    my = sum(s[1] * s[2] for s in samples) / wsum
    sxx = sum(s[2] * (s[0] - mx) ** 2 for s in samples) / wsum
    syy = sum(s[2] * (s[1] - my) ** 2 for s in samples) / wsum
    sxy = sum(s[2] * (s[0] - mx) * (s[1] - my) for s in samples) / wsum
    theta = 0.5 * math.atan2(2.0 * sxy, sxx - syy)
    ux, uy = math.cos(theta), math.sin(theta)
    along = [(p[0] - mx) * ux + (p[1] - my) * uy for p in pts]
    across = [-(p[0] - mx) * uy + (p[1] - my) * ux for p in pts]
    return mx, my, ux, uy, max(along) - min(along), max(across) - min(across)


def _fill_ratio(ring, lon0: float, lat0: float) -> float:
    """环面积 / 包围盒面积：接近 1 说明这个“轮廓”只是个矩形范围。"""
    pts = _to_local(ring, lon0, lat0)
    area = 0.0
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        area += x1 * y2 - x2 * y1
    area = abs(area) / 2.0
    box = ((max(p[0] for p in pts) - min(p[0] for p in pts)) *
           (max(p[1] for p in pts) - min(p[1] for p in pts)))
    return area / box if box > 0 else 0.0


def _fitted_circle_km(ring, lon0: float, lat0: float) -> float:
    """轮廓内接的等效圆半径（km），作为没有官方直径时的兜底。"""
    pts = _to_local(ring, lon0, lat0)
    mx = sum(p[0] for p in pts) / len(pts)
    my = sum(p[1] for p in pts) / len(pts)
    return sum(math.hypot(p[0] - mx, p[1] - my) for p in pts) / len(pts)


def _turn_degrees(a, b, c) -> float:
    kx = _km_scale(b[1])
    v1 = ((b[0] - a[0]) * kx, (b[1] - a[1]) * KM_PER_DEG)
    v2 = ((c[0] - b[0]) * kx, (c[1] - b[1]) * KM_PER_DEG)
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if not n1 or not n2:
        return 0.0
    cosang = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
    return math.degrees(math.acos(cosang))


def _split_sharp(line) -> List[List[List[float]]]:
    """在弯折过急的顶点处断开，返回若干连续段。"""
    if len(line) < 3:
        return [line]
    chains, current = [], [line[0]]
    for i in range(1, len(line) - 1):
        current.append(line[i])
        if _turn_degrees(line[i - 1], line[i], line[i + 1]) > SHARP_TURN_DEG:
            if len(current) >= 2:
                chains.append(current)
            current = [line[i]]
    current.append(line[-1])
    if len(current) >= 2:
        chains.append(current)
    return chains or [line]


def _path_length_km(path, lat: float) -> float:
    kx = _km_scale(lat)
    return sum(math.hypot((path[i + 1][0] - path[i][0]) * kx,
                          (path[i + 1][1] - path[i][1]) * KM_PER_DEG)
               for i in range(len(path) - 1))


def _chaikin(path, iterations: int = 1):
    """角点切割，把折线磨成适合沿走向排字的平滑路径。"""
    for _ in range(iterations):
        if len(path) < 3:
            break
        out = [list(path[0])]
        for i in range(len(path) - 1):
            p, q = path[i], path[i + 1]
            out.append([0.75 * p[0] + 0.25 * q[0], 0.75 * p[1] + 0.25 * q[1]])
            out.append([0.25 * p[0] + 0.75 * q[0], 0.25 * p[1] + 0.75 * q[1]])
        out.append(list(path[-1]))
        path = out
    return path


def _bearing(path, lat: float) -> float:
    """路径的整体走向（度）。文字方向无正反，归一到 (-90, 90]。"""
    kx = _km_scale(lat)
    cx = cy = 0.0
    for i in range(len(path) - 1):
        dx = (path[i + 1][0] - path[i][0]) * kx
        dy = (path[i + 1][1] - path[i][1]) * KM_PER_DEG
        length = math.hypot(dx, dy)
        if length <= 0:
            continue
        angle = math.atan2(dy, dx) * 2.0
        cx += length * math.cos(angle)
        cy += length * math.sin(angle)
    if cx == 0.0 and cy == 0.0:
        return 0.0
    deg = math.degrees(math.atan2(cy, cx)) / 2.0
    return round((deg + 90.0) % 180.0 - 90.0, 2)


def _cross_section(rot_ring, x: float):
    """主轴坐标系里竖直线 x 与环的交点，取最宽一段的中点。"""
    ys = []
    for i in range(len(rot_ring) - 1):
        x1, y1 = rot_ring[i]
        x2, y2 = rot_ring[i + 1]
        if (x1 - x) * (x2 - x) > 0 or x1 == x2:
            continue
        t = (x - x1) / (x2 - x1)
        ys.append(y1 + t * (y2 - y1))
    ys.sort()
    best = (None, None)
    for i in range(0, len(ys) - 1, 2):
        span = ys[i + 1] - ys[i]
        if best[0] is None or span > best[0]:
            best = (span, (ys[i] + ys[i + 1]) / 2.0)
    return best


def _interior_centerline(ring, lon0: float, lat0: float, samples: int = 9):
    """沿主轴在区域内部取样，得到一条居中的排字路径（山脉用）。"""
    mx, my, ux, uy, length, _ = _main_axis(ring, lon0, lat0)
    if length <= 0:
        return []
    kx = _km_scale(lat0)
    rot_ring = []
    for p in ring:
        x = (p[0] - lon0) * kx - mx
        y = (p[1] - lat0) * KM_PER_DEG - my
        rot_ring.append((x * ux + y * uy, -x * uy + y * ux))
    x0 = min(p[0] for p in rot_ring)
    x1 = max(p[0] for p in rot_ring)
    path = []
    for i in range(samples):
        x = x0 + (x1 - x0) * i / (samples - 1)
        span, ymid = _cross_section(rot_ring, x)
        if span is None or span < 0.05:
            continue
        path.append(_to_lonlat(mx + x * ux - ymid * uy,
                               my + x * uy + ymid * ux, lon0, lat0))
    return path


#: Two pieces whose ends are closer than this are one rille: the WKT splits a
#: long rille where it is interrupted, and drawing only the longest fragment
#: makes a 100 km rille look like a 60 km scratch.
JOIN_GAP_KM = 25.0


def _endpoint_distance(a, b, lat: float) -> float:
    kx = _km_scale(lat)
    return math.hypot((a[0] - b[0]) * kx, (a[1] - b[1]) * KM_PER_DEG)


def _merge_chains(chains, lat: float, max_gap_km: float = JOIN_GAP_KM):
    """Join polylines whose ends nearly touch, longest-first by repeated merge."""
    routes = [list(chain) for chain in chains if len(chain) >= 2]
    while len(routes) > 1:
        best = None
        for i in range(len(routes)):
            for j in range(i + 1, len(routes)):
                a, b = routes[i], routes[j]
                for a_tail, a_pt in ((True, a[-1]), (False, a[0])):
                    for b_head, b_pt in ((True, b[0]), (False, b[-1])):
                        gap = _endpoint_distance(a_pt, b_pt, lat)
                        if gap <= max_gap_km and (best is None or gap < best[0]):
                            best = (gap, i, j, a_tail, b_head)
        if best is None:
            break
        _, i, j, a_tail, b_head = best
        head = routes[i] if a_tail else routes[i][::-1]
        tail = routes[j] if b_head else routes[j][::-1]
        routes[i] = head + tail
        routes.pop(j)
    return routes


def _line_label(lines, lon0: float, lat0: float) -> Dict[str, Any]:
    """月溪 / 山脊 / 月谷：沿最长的一条连续段平滑排字。"""
    chains = []
    for line in lines:
        chains.extend(_split_sharp(line))
    routes = _merge_chains(chains, lat0)
    best = None
    for route in routes:
        length = _path_length_km(route, lat0)
        if best is None or length > best[0]:
            best = (length, route)
    if best is None:
        return {"kind": "point", "center": [round(lon0, 5), round(lat0, 5)],
                "reason": "empty"}
    length, chain = best
    total = sum(_path_length_km(line, lat0) for line in lines)
    longest = max((_path_length_km(route, lat0) for route in routes), default=length)
    if length < MIN_LABEL_KM:
        return {"kind": "point", "center": [round(lon0, 5), round(lat0, 5)],
                "reason": "too-short", "length_km": round(length, 1)}
    path = _chaikin(_simplify(chain, LABEL_SIMPLIFY_KM / KM_PER_DEG), 1)
    label = {
        "kind": "line",
        "path": _round_ring(path),
        "rotation_deg": _bearing(path, lat0),
        "length_km": round(_path_length_km(path, lat0), 1),
    }
    if abs(length - label["length_km"]) > 1.0:
        label["source_length_km"] = round(length, 1)
    if len(routes) > 1 or length < longest * 0.95:
        label["split"] = True           # 仍有分叉/断口：只取了其中最长的一条
        label["piece_count"] = len(lines)
        label["total_km"] = round(total, 1)
    return label


def build_label(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """按分类给出该要素的绘制与排字标准。"""
    geom = rec.get("geometry") or {}
    gtype, coords = geom.get("type"), geom.get("coordinates")
    if not gtype or not coords:
        return None
    category = rec.get("type") or ""

    if gtype == "Point":
        return {"kind": "point",
                "center": [round(float(coords[0]), 5), round(float(coords[1]), 5)]}

    if gtype in ("LineString", "MultiLineString"):
        lines = [coords] if gtype == "LineString" else [ln for ln in coords if ln]
        points = [p for line in lines for p in line]
        if not points:
            return None
        return _line_label(lines, *_center_of(points))

    rings = coords if gtype == "Polygon" else [poly[0] for poly in coords if poly]
    if not rings or not rings[0]:
        return None
    outer = rings[0]
    lon0, lat0 = _center_of(outer)
    official = rec.get("center") or {}
    if "lon" in official and "lat" in official:
        # 官方中心点比顶点均值更可靠（顶点疏密会把均值拉偏）
        lon0, lat0 = float(official["lon"]), float(official["lat"])

    # 环形坑（含卫星坑）：矢量圆，半径优先取官方直径，省掉几十个顶点。
    if category in CRATER_TYPES:
        diameter = _as_float(rec.get("diameter"))
        if diameter and diameter > 0:
            radius_km, radius_source = diameter / 2.0, "diameter"
        else:
            radius_km, radius_source = _fitted_circle_km(outer, lon0, lat0), "outline"
        return {
            "kind": "circle",
            "center": [round(lon0, 5), round(lat0, 5)],
            "radius_km": round(radius_km, 3),
            "radius_deg": round(radius_km / KM_PER_DEG, 6),
            "radius_deg_lon": round(radius_km / _km_scale(lat0), 6),
            "radius_source": radius_source,
        }

    mx, my, ux, uy, length, width = _main_axis(outer, lon0, lat0)
    aspect = length / width if width > 1e-6 else 999.0
    rectangle = _fill_ratio(outer, lon0, lat0) >= RECT_FILL_RATIO and aspect >= ELONGATED_ASPECT

    # 只给了矩形包围盒的要素（如 Rima Ariadaeus）：源数据里没有轨迹，不能
    # 拿长轴编一条直线冒充轮廓——那会在图上画出一条横贯的“尺子”。只保留
    # 中心与走向提示，交给普通点标注。
    if rectangle:
        half = length / 2.0 * 0.92
        axis = [_to_lonlat(mx - half * ux, my - half * uy, lon0, lat0),
                _to_lonlat(mx + half * ux, my + half * uy, lon0, lat0)]
        return {
            "kind": "point",
            "center": [round(lon0, 5), round(lat0, 5)],
            "rotation_deg": round(_bearing(axis, lat0), 2),
            "aspect": round(aspect, 2),
            "bbox_only": True,
        }

    # 月溪/月谷/崖的面积轮廓：按长轴造一条直线定走向。
    if category in LINE_TYPES:
        half = length / 2.0 * 0.92
        path = [_to_lonlat(mx - half * ux, my - half * uy, lon0, lat0),
                _to_lonlat(mx + half * ux, my + half * uy, lon0, lat0)]
        return {
            "kind": "line",
            "path": _round_ring(path),
            "rotation_deg": round(_bearing(path, lat0), 2),
            "length_km": round(length, 1),
            "reason": "area-outline",
        }

    # 山脉/山脊，或任何狭长区域：从边界取主走向，生成区域内部的排字路径。
    if category in RIDGE_TYPES or aspect >= ELONGATED_ASPECT:
        path = _interior_centerline(outer, lon0, lat0)
        if len(path) >= 2:
            path = _chaikin(_simplify(path, LABEL_SIMPLIFY_KM / KM_PER_DEG), 1)
            return {
                "kind": "centerline",
                "path": _round_ring(path),
                "rotation_deg": _bearing(path, lat0),
                "length_km": round(_path_length_km(path, lat0), 1),
                "aspect": round(aspect, 2),
            }

    # 其余（月海、湖、湾等块状区域）保持中心点排字。
    return {"kind": "point", "center": [round(lon0, 5), round(lat0, 5)],
            "aspect": round(aspect, 2)}


def to_geojson(records: Sequence[Dict[str, Any]],
               categories: Sequence[Tuple[str, str]],
               meta: Dict[str, Any],
               simplify_km: float = 0.0,
               digits: int = 5,
               labels: bool = False) -> Dict[str, Any]:
    """把要素写成 GeoJSON FeatureCollection；labels=True 时附带绘制/排字标准。"""
    type_ids = {name: type_id for type_id, name in categories}
    tolerance = simplify_km / KM_PER_DEG if simplify_km > 0 else 0.0
    features = []
    counts: Dict[str, int] = {}
    label_kinds: Dict[str, int] = {}
    total_vertices = 0
    for rec in sorted(records, key=lambda r: (r.get("type", ""), r.get("feature", ""))):
        original = rec.get("geometry") or {}
        label = build_label(rec) if labels else None
        geometry = simplify_geometry(original, tolerance)
        coords = geometry.get("coordinates")
        if coords:
            coords = _round_ring(coords, digits)
            total_vertices += _count_vertices(coords)
        name = rec.get("type", "")
        counts[name] = counts.get(name, 0) + 1
        properties: Dict[str, Any] = {
            "id": str(rec.get("feature_id", "")),
            "name": rec.get("feature", ""),
            "clean_name": rec.get("clean_feature", ""),
            "type": name,
            "type_id": type_ids.get(name),
            "code": rec.get("code", ""),
            "diameter_km": _as_float(rec.get("diameter")),
            "source": rec.get("geometry_source"),
        }
        if labels:
            if label is not None:
                properties["label"] = label
                label_kinds[label["kind"]] = label_kinds.get(label["kind"], 0) + 1
            else:
                label_kinds["none"] = label_kinds.get("none", 0) + 1
        features.append({
            "type": "Feature",
            "properties": properties,
            "geometry": {"type": geometry.get("type"), "coordinates": coords},
        })
    classes = [{"type_id": type_id, "type": name, "count": counts.get(name, 0)}
               for type_id, name in categories]
    merged = dict(meta)
    merged["fetched_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    merged["coordinate_note"] = "与源数据一致，未做半径校正"
    if labels:
        merged["simplify_km"] = simplify_km
        merged["digits"] = digits
        merged["label_rule"] = (
            "label 是绘制与排字标准：circle=中心+半径；line=沿走向的路径；"
            "centerline=区域内部主走向路径；point=退回普通点标注"
        )
    totals: Dict[str, Any] = {"features": len(features), "vertices": total_vertices}
    if labels:
        totals["labels"] = label_kinds
    return {
        "type": "FeatureCollection",
        "meta": merged,
        "classes": classes,
        "totals": totals,
        "features": features,
    }


def _count_vertices(coords: Any) -> int:
    if isinstance(coords, list) and coords and isinstance(coords[0], (int, float)):
        return 1
    return sum(_count_vertices(item) for item in coords)


def _as_float(value: Any) -> Optional[float]:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def wfs_url(layer: str, type_name: Optional[str], start: int, count: int,
            properties: Optional[Sequence[str]] = None) -> str:
    query = {
        "map": WFS_MAP,
        "SERVICE": "WFS",
        "VERSION": "1.1.0",
        "REQUEST": "GetFeature",
        "TYPENAME": layer,
        "MAXFEATURES": str(count),
        "STARTINDEX": str(start),
    }
    if type_name is not None:
        query["FILTER"] = (
            "<Filter><PropertyIsEqualTo><PropertyName>type</PropertyName>"
            f"<Literal>{type_name}</Literal></PropertyIsEqualTo></Filter>"
        )
    if properties:
        query["PROPERTYNAME"] = ",".join(properties)
    return WFS_HOST + WFS_PATH + "?" + urllib.parse.urlencode(query)


def wfs_hits_url(layer: str, type_name: Optional[str] = None) -> str:
    """只要总数的请求（resultType=hits），用于核对是否抓全。"""
    query = {
        "map": WFS_MAP,
        "SERVICE": "WFS",
        "VERSION": "1.1.0",
        "REQUEST": "GetFeature",
        "TYPENAME": layer,
        "RESULTTYPE": "hits",
    }
    if type_name is not None:
        query["FILTER"] = (
            "<Filter><PropertyIsEqualTo><PropertyName>type</PropertyName>"
            f"<Literal>{type_name}</Literal></PropertyIsEqualTo></Filter>"
        )
    return WFS_HOST + WFS_PATH + "?" + urllib.parse.urlencode(query)


def reported_total(body: bytes) -> Optional[int]:
    """从 hits 响应里读出 numberOfFeatures。"""
    match = re.search(rb'numberOfFeatures="(\d+)"', body)
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--out", type=Path, default=OUT_DEFAULT,
                        help="绘图用 GeoJSON（默认 out/moon_outlines.geojson）")
    parser.add_argument("--raw-out", type=Path, default=RAW_DEFAULT,
                        help="原始存档 JSON（默认 out/moon_outlines_raw.json）")
    parser.add_argument("--simplify", type=float, default=0.25,
                        help="绘图文件的抽稀容差 km（默认 0.25；0=不抽稀）")
    parser.add_argument("--build-draw", action="store_true",
                        help="不联网，只从 --raw-out 重新生成绘图文件（调整排字规则时用）")
    parser.add_argument("--delay", type=float, default=3.0, help="请求间间隔秒数（默认 3.0）")
    parser.add_argument("--maxfeatures", type=int, default=2000, help="WFS 每页请求数（默认 2000）")
    parser.add_argument("--maxfetch", type=int, default=0, help="最多处理的要素数（0=全部，用于试跑）")
    parser.add_argument("--force", action="store_true", help="忽略本地缓存重新抓取")
    parser.add_argument("--check", action="store_true", help="只校验已有 JSON 并打印统计")
    return parser


def _write_json(path: Path, doc: Dict[str, Any]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")),
                         encoding="utf-8")
    temporary.replace(path)
    return path.stat().st_size


def _records_from_raw(path: Path):
    """从原始存档读回 (records, categories)，供 --build-draw 离线重建。"""
    doc = json.loads(path.read_text(encoding="utf-8"))
    categories = [(str(g.get("type_id") or ""), g["type"]) for g in doc["classifications"]]
    records: Dict[str, Dict[str, Any]] = {}
    for group in doc["classifications"]:
        for rec in group["features"]:
            records[str(rec.get("feature_id"))] = rec
    return records, categories, doc


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.check:
        if not args.out.is_file():
            print(f"找不到 {args.out}", file=sys.stderr)
            return 1
        doc = json.loads(args.out.read_text(encoding="utf-8"))
        if "classifications" in doc:
            classes = doc["classifications"]
            totals = sum(g["count"] for g in classes)
        else:
            classes = doc.get("classes", [])
            totals = len(doc.get("features", []))
        print("分类统计:")
        for group in classes:
            print(f"  {group['type']!r:46s} {group['count']}")
        print("总计:", totals)
        counts = doc.get("totals", {})
        if counts.get("vertices"):
            print("顶点数:", counts["vertices"])
        if counts.get("labels"):
            print("标注类型:", counts["labels"])
        for warn in doc.get("warnings", []):
            print("警告:", warn)
        return 0

    warnings: List[str] = []
    if args.build_draw:
        if not args.raw_out.is_file():
            raise SystemExit(f"找不到原始存档 {args.raw_out}；先运行一次完整抓取。")
        records, categories, raw_doc = _records_from_raw(args.raw_out)
        meta = dict(raw_doc.get("meta") or {})
        meta["requests"] = 0
        warnings = list(raw_doc.get("warnings") or [])
        print(f"[build] 从 {args.raw_out} 读回 {len(records)} 个要素，不联网")
    else:
        fetcher = PoliteFetcher(delay=args.delay, force=args.force)

        print(f"[1/4] 读取官方分类 {TARGET_PAGE}")
        page = fetcher.get(TARGET_PAGE).decode("utf-8", "replace")
        categories = categories_from_page(page)
        if not categories:
            raise SystemExit("没有从总览页解析出任何分类，网站结构可能变了")
        print("      分类 %d 个: " % len(categories) + ", ".join(name for _, name in categories))

        # --- 第一步：WFS 面图层 ---------------------------------------------
        print(f"[2/4] 从 {WFS_LAYER} 读取有轮廓的要素（每页 {args.maxfeatures} 条）")
        total = reported_total(fetcher.get(wfs_hits_url(WFS_LAYER)))
        print(f"      WFS 报告要素数: {total if total is not None else '未知'}")
        records: Dict[str, Dict[str, Any]] = {}
        for _, type_name in categories:
            start = 0
            while True:
                body = fetcher.get(wfs_url(WFS_LAYER, type_name, start, args.maxfeatures))
                batch = parse_records(body)
                if not batch:
                    break
                for rec in batch:
                    fid = str(rec.get("feature_id") or "")
                    if fid:
                        rec["geometry_source"] = "wfs"
                        records[fid] = rec
                start += len(batch)
                if len(batch) < args.maxfeatures:
                    break
            print(f"      {type_name!r:44s} 面要素 "
                  f"{sum(1 for r in records.values() if r.get('type') == type_name)}")

        # --- 第二步：MOON_POINT 给出“全部已批准要素”清单 --------------------
        print(f"[3/4] 从 {POINT_LAYER} 读取全部要素清单（仅属性，无几何）")
        index: Dict[str, Dict[str, Any]] = {}
        fields = ["feature_id", "feature", "clean_feature", "type",
                  "diameter", "code", "controlnet"]
        for _, type_name in categories:
            start = 0
            while True:
                body = fetcher.get(wfs_url(POINT_LAYER, type_name, start,
                                           args.maxfeatures, fields))
                batch = parse_records(body)
                if not batch:
                    break
                for rec in batch:
                    fid = str(rec.get("feature_id") or "")
                    if fid:
                        index[fid] = rec
                start += len(batch)
                if len(batch) < args.maxfeatures:
                    break

        missing = [fid for fid in index if fid not in records]
        print(f"      已批准要素 {len(index)} 个；已有面轮廓 {len(records)} 个；缺 {len(missing)} 个")

        # --- 第三步：缺轮廓的要素逐个读详情页的 WKT --------------------------
        print(f"[4/4] 为 {len(missing)} 个要素读取详情页 WKT（线状地物多在这里）")
        page_filled = 0
        still_missing = 0
        for position, fid in enumerate(missing, 1):
            url = f"https://planetarynames.wr.usgs.gov/Feature/{fid}"
            html = fetcher.get(url).decode("utf-8", "replace")
            geometry = wkt_from_page(html)
            rec = dict(index[fid])
            if geometry is not None:
                rec["geometry"] = geometry
                rec["geometry_source"] = "page"
                page_filled += 1
            else:
                rec["geometry"] = {"type": None, "coordinates": None}
                rec["geometry_source"] = None
                still_missing += 1
            records[fid] = rec
            if position % 25 == 0 or position == len(missing):
                print(f"      进度 {position}/{len(missing)}，补齐 {page_filled}，仍无 {still_missing}")

        if still_missing:
            warnings.append(f"{still_missing} 个要素在 WFS 和图页里都没有几何")
        if args.maxfetch:
            records = dict(list(records.items())[: args.maxfetch])

        meta = {
            "project": "moonland",
            "target": "Moon",
            "source": {
                "classification_page": TARGET_PAGE,
                "wfs": WFS_HOST + WFS_PATH,
                "layers": {"polygons": WFS_LAYER, "points": POINT_LAYER},
                "fallback": "https://planetarynames.wr.usgs.gov/Feature/<id> 内嵌 WKT",
                "licence": "IAU / USGS Planetary Nomenclature（美国联邦政府作品，公有领域）",
                "coordinate_order": "坐标为 [经度, 纬度]，单位度；经度东经为正，范围 -180..180",
            },
            "requests": fetcher.requests,
        }
        meta["coordinate_note"] = "源数据原样：未抽稀、未做半径校正"

        raw_doc = make_document(records.values(), categories, warnings, meta)
        raw_doc["warnings"] = list(warnings)
        size = _write_json(args.raw_out, raw_doc)
        print(f"\n[存档] 已写入 {args.raw_out} ({size / 1e6:.1f} MB，未抽稀)")

    draw = to_geojson(records.values(), categories, meta, args.simplify, labels=True)
    size = _write_json(args.out, draw)

    print(f"[绘图] 已写入 {args.out} ({size / 1e6:.1f} MB，抽稀 {args.simplify} km)")
    print("      标注类型:", draw["totals"].get("labels"))
    print("      顶点数:", draw["totals"]["vertices"])
    for warn in warnings:
        print("警告:", warn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
