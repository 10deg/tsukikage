"""Provisional Chinese names for features the gazetteer has not translated.

The domes, pyroclastic patches and many impact basins in the Virtual Moon Atlas
are named after a nearby IAU feature (``IMBRIUM IMPACT BASIN`` is the basin of
``Mare Imbrium``, ``DOME ARAGO 1`` is a dome on Arago).  This module follows
that convention first, then falls back to a fuzzy match against translated
names and finally to the nearest translated feature.  The result is a *review
draft*: no value here is written to the database by itself.
"""

from __future__ import annotations

import difflib
import math
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

TYPE_PREFIXES = (
    "mare", "oceanus", "lacus", "sinus", "palus", "rima", "rimae", "vallis",
    "montes", "mons", "dorsum", "dorsa", "rupes", "catena", "promontorium",
    "planitia", "basin", "impact basin", "crater", "rille",
)

DIRECTIONS = {
    "north": "北", "south": "南", "east": "东", "west": "西",
    "central": "中部", "center": "中部",
    "northern": "北部", "southern": "南部", "eastern": "东部",
    "western": "西部",
    "north east": "东北", "north west": "西北",
    "south east": "东南", "south west": "西南",
    "north north east": "北北东", "north north west": "北北西",
    "east north east": "东北东", "east south east": "东南东",
    "west north west": "西北西", "west south west": "西南西",
    "south south east": "南南东", "south south west": "南南西",
    "northeastern": "东北部", "northwestern": "西北部",
    "southeastern": "东南部", "southwestern": "西南部",
    "northeast": "东北部", "northwest": "西北部",
    "southeast": "东南部", "southwest": "西南部",
}

GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "omicron": "ο",
    "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ",
    "phi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
}

#: Mission and historical-site names that are not "nearby features"; these are
#: the accepted Chinese short forms rather than transliterations of an acronym.
SPECIAL_STATIO = {
    "bowen apollo": "鲍文（阿波罗）", "hess apollo": "赫斯（阿波罗）",
    "mount marylin": "玛丽琳山", "nansen apollo": "南森（阿波罗）",
    "sharp apollo": "夏普（阿波罗）", "st george": "圣乔治",
    "steno apollo": "斯泰诺（阿波罗）", "taurus littrow valley": "陶拉斯-利特罗谷",
    "luna 2": "月球2号", "ranger 4": "徘徊者4号", "ranger 6": "徘徊者6号",
    "ranger 7": "徘徊者7号", "ranger 8": "徘徊者8号", "ranger 9": "徘徊者9号",
    "luna 5": "月球5号", "luna 7": "月球7号", "luna 8": "月球8号",
    "luna 9": "月球9号", "surveyor 1": "勘测者1号",
    "lunar orbiter 1": "月球轨道器1号", "surveyor 2": "勘测者2号",
    "lunar orbiter 2": "月球轨道器2号", "luna 13": "月球13号",
    "lunar orbiter 3": "月球轨道器3号", "surveyor 3": "勘测者3号",
    "surveyor 4": "勘测者4号", "lunar orbiter 5": "月球轨道器5号",
    "surveyor 5": "勘测者5号", "surveyor 6": "勘测者6号",
    "surveyor 7": "勘测者7号", "luna 15": "月球15号",
    "apollo 11 base": "阿波罗11号基地", "apollo 12 base": "阿波罗12号基地",
    "apollo 12 lm": "阿波罗12号登月舱", "apollo 13 s iv b": "阿波罗13号S-IVB",
    "luna 16": "月球16号", "luna 17": "月球17号",
    "apollo 14 base": "阿波罗14号基地", "apollo 14 lm": "阿波罗14号登月舱",
    "apollo 14 s iv b": "阿波罗14号S-IVB",
    "apollo 15 base": "阿波罗15号基地", "apollo 15 lm": "阿波罗15号登月舱",
    "apollo 15 s iv b": "阿波罗15号S-IVB", "luna 18": "月球18号",
    "luna 20": "月球20号", "apollo 16 base": "阿波罗16号基地",
    "apollo 16 s iv b": "阿波罗16号S-IVB", "apollo 17 base": "阿波罗17号基地",
    "apollo 17 s iv b": "阿波罗17号S-IVB", "apollo 17 lm": "阿波罗17号登月舱",
    "luna 21": "月球21号", "luna 23": "月球23号", "luna 24": "月球24号",
    "hiten": "飞天号", "lunar prospector": "月球探勘者号", "smart 1": "智能1号",
    "grail a ebb": "圣杯号A（Ebb）", "grail b flow": "圣杯号B（Flow）",
    "ladee": "月球大气与粉尘环境探测器", "chang e 1": "嫦娥一号",
    "lcross": "月球坑观测与遥感卫星",
    "centaur a lcross": "半人马座A / 月球坑观测与遥感卫星",
    "chang e 3 yutu": "嫦娥三号 / 玉兔",
    "chandrayaan 1 moon impact probe": "月船一号 / 月球撞击探测器",
    "kaguya": "辉夜号", "chang e 4 yutu 2": "嫦娥四号 / 玉兔二号",
    "beresheet": "创世纪号", "longjiang 2": "龙江二号",
    "chandrayaan 2 vikram pragyan": "月船二号 / 维克拉姆 / 普拉吉安",
    "chang e 5": "嫦娥五号", "unknown": "未知",
}

SPECIAL_BASIN = {
    "near side megabasin": "近侧巨型",
    "south pole aitken": "南极-艾特肯",
    "von karman n": "冯·卡门N",
    "topo 22": "地形22号",
    "schickardt": "席卡尔德",
    "schwarzschildt": "史瓦西",
    "tsiolkovsky": "齐奥尔科夫斯基",
    "tsiolkovsky stark": "齐奥尔科夫斯基-斯塔克",
    "priestly": "普里斯特利",
    "winmock": "温洛克",
    "winlock": "温洛克",
    "moscoviense": "莫斯科海",
    "keeler": "基勒",
    "crisium": "危海",
    "orientale": "东方海",
    "imbrium": "雨海",
    "marginis": "边缘海",
    "fecunditatis": "丰富海",
    "nubium": "云海",
    "smythii": "史密斯海",
    "serenitatis": "澄海",
    "tranquillitatis": "静海",
    "humorum": "湿海",
    "humboldtianum": "洪堡海",
    "australe": "南海",
    "ingenii": "智海",
    "iridum": "虹湾",
    "rupes recta": "直壁",
    "aestatis": "夏湖",
    "autumni": "秋湖",
    "pacificus": "风暴洋",
    "apollo": "阿波罗",
    "deslandres": "德朗德尔",
    "lorentz": "洛伦兹",
    "poczobutt": "波佐布特",
}

SPECIAL_COMPONENT = {
    "chaplygin": "恰普雷金", "mandel shtam": "门德尔施塔姆",
    "mandelshtam": "门德尔施塔姆", "amundsen": "阿蒙森",
    "ganswindt": "甘斯温特", "balmer": "巴耳末", "kapteyn": "卡普坦",
    "cruger": "克鲁格", "sirsalis": "西尔萨利斯", "dirichlet": "狄利克雷",
    "jackson": "杰克逊", "coulomb": "库仑", "sarton": "萨尔顿",
    "kohlschutter": "科尔许特", "leonov": "列昂诺夫", "schiller": "席勒",
    "zuccius": "祖基", "flamsteed": "弗拉姆斯蒂德", "billy": "比伊",
    "jeans": "金斯", "priestly": "普里斯特利", "wegener": "韦格纳",
    "winmock": "温洛克",
    "winlock": "温洛克", "fitzgerald": "菲茨杰拉德", "poincare": "庞加莱",
    "sharonov": "沙罗诺夫", "freundlich": "弗罗因德利希", "landau": "朗道",
    "von karman": "冯·卡门", "leibnitz": "莱布尼茨", "milne": "米尔恩",
    "grimaldi": "格里马尔迪", "pasteur": "巴斯德", "mendel": "门德尔",
    "rydberg": "里德伯", "korolev": "科罗廖夫", "mendeleev": "门捷列夫",
    "planck": "普朗克", "hertzsprung": "赫茨普龙", "bel kovich": "别尔科维奇",
    "bailly": "巴伊", "d alembert": "达朗贝尔", "oppenheimer": "奥本海默",
    "clavius": "克拉维斯", "lavoisier": "拉瓦锡", "mairan": "迈兰",
    "cardanus": "卡丹", "herodotus": "希罗多德", "schrodinger": "薛定谔",
    "humboldt": "洪堡", "tsiolkovskiy": "齐奥尔科夫斯基",
    "antoniadi": "安东尼亚迪", "compton": "康普顿", "grissom": "格里索姆",
    "white": "怀特", "insularum": "岛海", "keeler": "基勒",
    "heaviside": "亥维赛", "lomonosov": "罗蒙诺索夫", "fleming": "弗莱明",
    "mutus": "穆图斯", "vlacq": "弗拉克", "pingre": "潘格雷",
    "hausen": "豪森", "sylvester": "西尔维斯特", "nansen": "南森",
    "stark": "斯塔克", "werner": "维尔纳", "airy": "艾里", "newton": "牛顿",
    "sikorsky": "西科尔斯基", "rittenhouse": "里滕豪斯", "apollo": "阿波罗",
    "gagarin": "加加林", "fermi": "费米", "galois": "伽罗瓦",
    "harkhebi": "哈克赫比", "janssen": "让森", "campbell": "坎贝尔",
}

SPECIAL_PYRO = {
    "taurus littrow": "托罗斯-利特罗峡谷",
    "pacificus": "风暴洋",
}

#: A basin's direction is a part of its name (危海东部盆地), not a compass
#: bearing (危海东盆地).
BASIN_DIRECTION = {
    "north": "北部", "south": "南部", "east": "东部", "west": "西部",
    "central": "中部", "northeast": "东北部", "northwest": "西北部",
    "southeast": "东南部", "southwest": "西南部",
    "north east": "东北部", "north west": "西北部",
    "south east": "东南部", "south west": "西南部",
}


def normalize(text: Any) -> str:
    """ASCII-fold, lowercase and collapse every non-alphanumeric run."""
    text = str(text)
    try:
        text = text.encode("latin1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _strip_satellite(zh: str) -> str:
    return re.sub(r"坑[A-Z]$", "", zh)


class Index:
    """Translated names and the aliases implied by their type prefixes."""

    def __init__(self, features: Sequence[Any]):
        self.features = [feature for feature in features
                         if getattr(feature, "name_zh", "")
                         and feature.name_zh != feature.name_en]
        self.exact: Dict[str, str] = {}
        self.alias: Dict[str, str] = {}
        for feature in self.features:
            n = normalize(feature.name_en)
            self.exact.setdefault(n, feature.name_zh)
            self.alias.setdefault(n, feature.name_zh)
            words = n.split()
            for prefix in TYPE_PREFIXES:
                pw = prefix.split()
                if words[:len(pw)] == pw and len(words) > len(pw):
                    self.alias.setdefault(" ".join(words[len(pw):]), feature.name_zh)
        self.keys = list(self.exact)

    def exact_name(self, text: str) -> Optional[str]:
        n = normalize(text)
        if n in self.alias:
            return self.alias[n]
        if n in self.exact:
            return self.exact[n]
        for prefix in TYPE_PREFIXES:
            pw = prefix.split()
            words = n.split()
            if words[:len(pw)] == pw and len(words) > len(pw):
                candidate = " ".join(words[len(pw):])
                if candidate in self.alias:
                    return self.alias[candidate]
        return None

    def fuzzy(self, text: str) -> Optional[str]:
        n = normalize(text)
        found = difflib.get_close_matches(n, self.keys, n=1, cutoff=0.72)
        return self.exact[found[0]] if found else None

    def match(self, text: str) -> Optional[str]:
        n = normalize(text)
        exact = self.exact_name(n)
        if exact:
            return exact
        words = n.split()
        # A one-to-three-letter/ Greek suffix is a satellite designation.
        for take in (2, 1):
            if len(words) <= take:
                continue
            base = " ".join(words[:-take])
            design = " ".join(words[-take:]).replace(" ", "")
            if not design.isalpha() or (len(design) > 3 and design not in GREEK):
                continue
            found = self.exact_name(base) or self.fuzzy(base)
            if found:
                if design in GREEK:
                    design = GREEK[design]
                else:
                    design = design.upper()
                found = _strip_satellite(found)
                return found + (
                    "" if found.endswith("坑" + design) else "坑" + design)
        words = n.split()
        for length in range(len(words) - 1, 0, -1):
            key = " ".join(words[:length])
            if key in self.exact:
                return self.exact[key] + "".join(words[length:]).upper()
        found = self.fuzzy(n)
        if found:
            return found if re.search(r"[a-z]$", n) else _strip_satellite(found)
        return None

    def nearest(self, lon: float, lat: float) -> Optional[Any]:
        best = None
        for feature in self.features:
            distance = math.hypot(
                (float(feature.lon_deg) - lon) * math.cos(math.radians(lat)),
                float(feature.lat_deg) - lat,
            )
            if best is None or distance < best[0]:
                best = (distance, feature)
        return best[1] if best else None


def _directions(text: str) -> Tuple[List[str], List[str]]:
    """Split a name into (base words, Chinese direction words)."""
    tokens = re.split(r"[\s-]+", normalize(text))
    rest: List[str] = []
    found: List[str] = []
    index = 0
    while index < len(tokens):
        picked = None
        for length in range(4, 0, -1):
            key = " ".join(tokens[index:index + length])
            if key in DIRECTIONS:
                picked = (length, DIRECTIONS[key])
                break
        if picked is None:
            rest.append(tokens[index])
            index += 1
        else:
            simple = {
                "东": "E", "东部": "E", "西": "W", "西部": "W",
                "南": "S", "南部": "S", "北": "N", "北部": "N",
                "中部": "C",
            }
            key = simple.get(picked[1])
            duplicate = key is not None and any(
                simple.get(value) == key for value in found)
            if not duplicate and picked[1] not in found:
                found.append(picked[1])
            index += picked[0]
    return rest, found


def _basin(feature: Any, index: Index) -> str:
    raw = re.sub(r"\s+IMPACT\s+BASIN$", "", feature.name_en.strip(), flags=re.I)
    key = normalize(raw)
    if key in SPECIAL_BASIN:
        return SPECIAL_BASIN[key] + "盆地"
    words = key.split()
    directions: List[str] = []
    while words and words[-1] in DIRECTIONS:
        word = words.pop()
        directions.insert(0, BASIN_DIRECTION.get(word, DIRECTIONS[word]))
    components = [part.strip() for part in re.split(r"-", raw) if part.strip()]
    if components:
        last = normalize(components[-1]).split()
        while last and last[-1] in DIRECTIONS:
            direction = last.pop()
            rendered_direction = BASIN_DIRECTION.get(direction, DIRECTIONS[direction])
            if rendered_direction not in directions:
                directions.insert(0, rendered_direction)
        components[-1] = " ".join(last)
    rendered: List[str] = []
    for component in components:
        ckey = normalize(component)
        found = (SPECIAL_BASIN.get(ckey) or SPECIAL_COMPONENT.get(ckey)
                 or index.match(component))
        if not found:
            found = "-".join(
                SPECIAL_COMPONENT.get(word) or index.match(word) or word.upper()
                for word in ckey.split()
            )
        rendered.append(found)
    return "-".join(rendered) + "".join(directions) + "盆地"


def _dome(feature: Any, index: Index) -> str:
    match = re.match(r"^DOME\s+(.*?)(?:\s+(\d+))?$", feature.name_en.strip())
    stem = match.group(1) if match else feature.name_en
    number = (match.group(2) or "") if match else ""
    found = index.match(stem)
    if found:
        return found + "穹丘" + number
    near = index.nearest(float(feature.lon_deg), float(feature.lat_deg))
    parent = near.name_zh if near else stem.upper()
    return parent + "穹丘" + number


def _pyroclastic(feature: Any, index: Index) -> str:
    quoted = re.search(r'"([^"]+)"', feature.name_en)
    name = quoted.group(1) if quoted else feature.name_en
    rest, directions = _directions(name)
    directions = [item[:-1] if item.endswith("部") else item for item in directions]
    number = ""
    while rest and rest[-1].isdigit():
        number = rest.pop() + number
    base = re.sub(r"^of\s+", "", " ".join(rest))
    base = re.sub(r"\s+basin$", "", base)
    found = SPECIAL_PYRO.get(base) or index.match(base)
    if not found:
        near = index.nearest(float(feature.lon_deg), float(feature.lat_deg))
        found = near.name_zh if near else base.upper()
    return f"火成碎屑“{found}{''.join(directions)}{number}”"


def provisional_name(feature: Any, features: Sequence[Any],
                     index: Optional[Index] = None) -> Optional[str]:
    """A review-draft Chinese name, or None when the feature is not in scope."""
    from moonlib.annotations import feature_class

    group = feature_class(feature.kind, feature.diameter_km)
    index = index or Index(features)
    if not feature.name_zh or feature.name_zh == feature.name_en:
        if group == "dome":
            return _dome(feature, index)
        if group == "pyroclastic":
            return _pyroclastic(feature, index)
        if group == "basin":
            return _basin(feature, index)
        if group == "statio":
            return SPECIAL_STATIO.get(normalize(feature.name_en))
    return None
