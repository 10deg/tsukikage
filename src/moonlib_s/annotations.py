"""Names on a simulated Moon: which landforms are in frame, and where to write.

The simulator knows its own geometry exactly, so labelling is **projection, not
recognition**: every feature in the IAU gazetteer has a selenographic position,
every frame has a camera, and the pixel a named place lands on follows from the
two.  Nothing in this module looks at rendered pixels.

The projection is the inverse of the one the renderer raymarches with.  Pixel
``(i, j)`` of a frame is the ray

    direction ∝ forward + (i - (W-1)/2)·s·right + ((H-1)/2 - j)·s·up

so a surface point seen from the camera's own position lands back on the pixel
that ray came from.  Working through the camera *position* rather than treating
the view as orthographic is what keeps the outer third of the disk honest: at
45° from the sub-observer point the two differ by about a pixel on a 560 px
preview, and by several on a 3000 px export.

Two frames exist, because the page draws two kinds of picture:

``disk_frame``
    the whole Moon, orthographic along the observer direction, with the same
    ``disk_fraction`` and pixel scale :func:`moonlib_s.render_moon` used.
``region_frame``
    one telescope close-up: camera aimed at a selenographic target, rolled by
    the request's rotation, spanning the same ``span_km``.

Beyond position the planner decides *what is worth writing down*: a full-disk
preview has room for a few dozen names, so features are ranked by how big they
look, given a small bonus for the kinds that carry the most meaning per label
(maria, mountain ranges), and labels are pushed aside until they stop landing on
each other.  Text is measured with a character-class estimate rather than a font
(fonts are a presentation choice, and this module is a compute layer); the widths
are deliberately generous, because a page that leaves a little more air between
two labels looks right, while one that overlaps looks broken.
"""

from __future__ import annotations

import csv
import math
import re
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Iterator, Set

import numpy as np

from .config import package_data_dir
from .errors import MoonlibError, TargetDataError
from .features import load_features
from .instrument import ARCSEC_PER_RADIAN
from .models import MoonRenderSettings, MoonView, SurfaceFeature
from .moonview import _camera_basis, _rolled_basis, _target_vector
from .terrain import MOON_REFERENCE_RADIUS_KM, MoonTerrain

__all__ = [
    "NOMENCLATURE_FILE",
    "load_nomenclature",
    "load_chinese_names",
    "marker_radius_px",
    "outline_exit_distance",
    "annotation_catalogue",
    "FrameProjection",
    "disk_frame",
    "region_frame",
    "rim_outline",
    "Annotation",
    "AnnotationPlan",
    "plan_annotations",
    "annotate_frame",
    "LANGUAGES",
    "FEATURE_CLASSES",
    "CLASS_LABELS",
    "BASIN_MIN_DIAMETER_KM",
    "canonical_kind",
    "feature_class",
]

#: Full IAU gazetteer for the Moon, rebuilt by ``tools/fetch_nomenclature.py``.
NOMENCLATURE_FILE = "lunar_nomenclature.csv"

#: What a label may say: the official Chinese name, the IAU English one, or
#: both.  ``zh`` falls back to English for the few features that have no
#: translation rather than leaving them unnamed.
LANGUAGES = ("zh", "en", "both")

#: Kinds that earn a small label bonus *within their own family*.  Across
#: families the budget is shared round-robin instead, because a plain ranking by
#: apparent size gives a full-disk preview nothing but maria: they are the
#: largest things on the Moon by a wide margin, and there are twenty of them.
_KIND_BONUS = {
    "Mons": 8.0,
    "Vallis": 6.0,
    "Rima": 6.0,
    "Rupes": 5.0,
    "Dorsum": 4.0,
    "Catena": 4.0,
    "Promontorium": 4.0,
}

# ---------------------------------------------------------------------------
# classes: the families a reader sees, and can switch on and off
# ---------------------------------------------------------------------------
#: The categories a plan can be filtered by, in the order a page should show
#: them: biggest first, then the maria, then everything rarer than a crater.
#: ``basin`` and ``crater`` are size classes of the same IAU kind; the rest come
#: straight from the feature family the name and the gazetteer agree on.
FEATURE_CLASSES: Tuple[str, ...] = (
    # The order is the order the page's switches are drawn in, and a test
    # compares the two lists key by key.
    "basin",
    "mare",
    "crater",
    "lacus",
    "sinus",
    "palus",
    "rima",
    "vallis",
    "mons",
    "dome",
    "dorsum",
    "rupes",
    "catena",
    "promontorium",
    "statio",
    "pit",
    "pyroclastic",
    "imp",
    "impact",
    "albedo",
    "other",
)

#: Display names per class: ``(中文, English)``.  The Chinese names are the
#: categories of ``月面标记.xlsx`` -- the translation table's own sheet names --
#: with the one addition the table does not have, the size class that splits the
#: 环形坑 sheet into basins and craters.
CLASS_LABELS: Dict[str, Tuple[str, str]] = {
    "basin": ("大型撞击盆地", "Impact basin"),
    "mare": ("月海", "Mare"),
    "crater": ("环形坑", "Crater"),
    "lacus": ("月湖", "Lacus"),
    "sinus": ("月湾", "Sinus"),
    "palus": ("月沼", "Palus"),
    "rima": ("月溪", "Rima"),
    "vallis": ("月谷", "Vallis"),
    "mons": ("山脉", "Mons"),
    "dorsum": ("山脊", "Dorsum"),
    "rupes": ("峭壁", "Rupes"),
    "catena": ("坑链", "Catena"),
    "promontorium": ("海角", "Promontorium"),
    "statio": ("着陆点", "Statio"),
    "dome": ("穹丘 (Domes)", "Dome"),
    "pit": ("Lunar pits", "Lunar pit"),
    "pyroclastic": ("火成碎屑 (Pyroclastic)", "Pyroclastic"),
    "imp": ("不规则月海斑块 (IMP)", "Irregular mare patch"),
    "impact": ("撞击闪光 (NELIOTA)", "NELIOTA impact"),
    "albedo": ("反射率特征点", "Albedo feature"),
    "other": ("其他", "Other"),
}

#: Diameter at which a crater is called a basin.  The conventional floor in the
#: literature (Stuart-Alexander) is 300 km; below it a "basin" is just a large
#: crater, and above it a crater has the ring structure that makes one.
BASIN_MIN_DIAMETER_KM = 300.0

#: Which class a canonical kind belongs to.  ``Crater`` is split by size, so it
#: is absent here and handled in :func:`feature_class`.
_CLASS_BY_KIND = {
    # Crater used to be split by size here; the split is gone (see
    # feature_class) so it needs its own line.
    "Crater": "crater",
    "Mare": "mare",
    "Oceanus": "mare",
    "Lacus": "lacus",
    "Sinus": "sinus",
    "Palus": "palus",
    "Rima": "rima",
    "Vallis": "vallis",
    "Mons": "mons",
    "Dorsum": "dorsum",
    "Rupes": "rupes",
    "Catena": "catena",
    "Promontorium": "promontorium",
    "Statio": "statio",
    "Albedo Feature": "albedo",
    "Planitia": "mare",
    # The families the Virtual Moon Atlas keeps in tables of their own: without
    # these they all fell into 其他 and the class switches for them did nothing.
    "Basin": "basin",
    "Pit": "pit",
    "Pyroclastic": "pyroclastic",
    "IMP": "imp",
    "Irregular mare patch": "imp",
    "Rille": "rima",
    "Dome": "dome",
    "Impact": "impact",
    "Contemporary meteoritic impact": "impact",
    "Impact basin": "basin",
    "Historical site": "statio",
}

#: The first word of an IAU name *is* the feature family -- ``Mare Imbrium``,
#: ``Rimae Arzachel``, ``Montes Recti B``.  The gazetteer's own type column
#: disagrees for twenty near-side maria (the translation table files them under
#: 月海, a sheet the database builder did not know), so the name is what the
#: class is read from, with the type column as the fallback for the 8 700
#: craters that carry no such prefix.
_NAME_KINDS = {
    "mare": "Mare",
    "oceanus": "Oceanus",
    "lacus": "Lacus",
    "sinus": "Sinus",
    "palus": "Palus",
    "rima": "Rima",
    "rimae": "Rima",
    "vallis": "Vallis",
    "mons": "Mons",
    "montes": "Mons",
    "dorsum": "Dorsum",
    "dorsa": "Dorsum",
    "rupes": "Rupes",
    "catena": "Catena",
    "promontorium": "Promontorium",
    "statio": "Statio",
    "planitia": "Planitia",
    "pit": "Pit",
    "basin": "Basin",
}


#: A satellite feature: the name of its parent, a space, and one capital letter
#: ("Mons Bradley B", "Rima Hyginus A").  The letter names a *crater* on the
#: parent, whatever the parent is -- the IAU's own convention -- so the family a
#: satellite belongs to is the one its parent's family does not describe.
_SATELLITE_SUFFIX = re.compile(r"^.+\s[A-Z]$")


def canonical_kind(name_en: str, kind: str) -> str:
    """The kind a feature's own name implies, or the stored one.

    Only the first word is consulted, and only when it is a known descriptor and
    the row is not a lettered satellite: ``Montes Recti B`` is a 7 km crater on
    the range, not a mountain, and reading its first word as its family would
    have put it in 山脉 with a mountain's label bonus.  Everything else the rule
    touches is a repair -- the twenty near-side maria the shipped table files as
    ``Crater`` -- and no other row in the 9 048 disagrees with its own name.
    """
    name = str(name_en or "").strip()
    if str(kind or "") == "Basin":
        # The basin table is authoritative about basins: ``RUPES RECTA IMPACT
        # BASIN`` is a basin the atlas lists, even though its own name begins
        # with the word for a scarp.  Everything else is read from the name.
        return "Basin"
    if _SATELLITE_SUFFIX.match(name):
        return str(kind or "Feature")
    return _NAME_KINDS.get(name.split(" ")[0].casefold(), str(kind or "Feature"))


def feature_class(kind: str, diameter_km: float) -> str:
    """The category a page groups one feature under.

    ``kind`` must already be canonical (:func:`canonical_kind`), and the class
    comes from the kind alone.  A crater over 300 km used to be called a basin
    as well; the basins are now the ones the catalogue *names* as basins
    (``Impact basin`` rows from the Virtual Moon Atlas), so the size is no
    longer consulted -- two sources of truth for one word was one too many, and
    it put a hundred rings in 大型撞击盆地 that its own table does not list.
    """
    kind = str(kind or "Feature")
    return _CLASS_BY_KIND.get(kind, "other")

#: The families the label budget is split between, in the order the first round
#: of the round-robin takes them.
_FAMILY_ORDER = ("sites", "seas", "craters", "other")
_SEA_CLASSES = frozenset({"mare", "lacus", "sinus", "palus"})

#: The landing-site class.  Its *IAU* members are exempt from the label budget
#: *and* from the minimum-radius filter: every one of them in the frame is named
#: at every density.
#:
#: A landing site is a few kilometres across, so on a full disk it loses the
#: size ranking to every mare and every lettered crater; at the densities a
#: reader actually uses it was never named at all.  They are also the handful of
#: places on the Moon a reader is most likely to be looking for, and there are
#: only ten of them, so "all of them" is a bounded promise.
#:
#: The class also carries the atlas's **historical sites** -- Luna 2, Ranger 4,
#: the Apollo descent stages, the GRAIL twins, sixty-three of them -- which are
#: ``statio`` for the class switch and the colour but are *not* exempt: see
#: :func:`_family`.  Sixty-three extra always-on names would push every sea and
#: every mountain out of a thirty-label plan.
_SITE_CLASS = "statio"
#: The kind the IAU gives a landing site.
_SITE_KIND = "Statio"

#: What a *landing site* is called, as opposed to the landmarks inside one.
#:
#: The IAU files both under ``Statio, stationes``: 静海基地 and 天河基地 are the
#: places spacecraft came down, while 南丘, 烟雾山 and 北辐射线 are the named
#: mountains, craters and rays *within* the Apollo sites -- seventy of them, all
#: filed the same way.  The sites are named for themselves (``Statio …``, and the
#: atlas's ``… APOLLO`` / ``TAURUS LITTROW VALLEY`` / ``ST GEORGE``); exempting
#: the whole class from the budget put forty traverse stations on a twelve-label
#: plan and pushed every sea out of it.
#:
#: ``APOLLO 11 BASE`` and the rest of the atlas's historical sites deliberately do
#: **not** match: they are places rather than landmarks, but there are sixty-three
#: of them, and exempting them would be the same mistake with a different
#: justification.
_SITE_NAME = re.compile(r"^Statio\b| APOLLO$|^TAURUS LITTROW VALLEY$|^ST GEORGE$",
                        re.IGNORECASE)


def is_landing_site(feature: Any) -> bool:
    """Whether this feature is a place something landed, not a landmark in one."""
    return (str(getattr(feature, "kind", "")) == _SITE_KIND
            and bool(_SITE_NAME.search(str(getattr(feature, "name_en", "")))))
#: A ceiling anyway, for a catalogue that grows one day.
_SITE_LIMIT = 40

#: Bonus for a feature in the curated showpiece catalogue.  Apparent size alone
#: is a poor guide to what a lunar observer wants named: Tycho is a sixth the
#: apparent radius of Clavius, and on a full disk it is the first thing anyone
#: looks for.  The showpieces are the project's own answer to "what is worth
#: pointing at", so they outrank an equally sized neighbour without outranking
#: something several times larger.
_HIGHLIGHT_BONUS = 22.0

#: Marker discs are drawn this size regardless of a feature's real extent, and
#: the planner keeps labels off them; past a certain size a ray crater would
#: otherwise reserve a circle bigger than the label it is trying to place.
_MAX_MARKER_PX = 26.0

#: The font the layout constants above are quoted at.  Every length the planner
#: does not take from the frame is measured in label-heights, so that a plan for
#: the same frame at another font size is the *same plan*, scaled -- which is
#: what lets the page ask for one plan at the preview's size and another at
#: 10 000 px and show the same names in the same places.
_DEFAULT_FONT_PX = 13.0

#: Classes whose outline is worth drawing: the round ones.  A rille, a scarp or a
#: mountain chain has a diameter in the gazetteer too, but it is the length of
#: something that is not a circle, and drawing a circle round it would invent a
#: shape the Moon does not have.
_OUTLINE_CLASSES = frozenset({"basin", "crater", "mare", "lacus", "sinus", "palus"})


# ---------------------------------------------------------------------------
# the catalogue
# ---------------------------------------------------------------------------
def load_nomenclature(path: Optional[Any] = None) -> Tuple[SurfaceFeature, ...]:
    """Read the full IAU gazetteer (or a user-supplied table of the same shape).

    Raises
    ------
    TargetDataError
        When the file is missing or a row cannot be parsed.  Rebuilding it is
        ``python tools/fetch_nomenclature.py``.
    """
    source = Path(path) if path is not None else package_data_dir() / NOMENCLATURE_FILE
    if not source.is_file():
        raise TargetDataError(
            f"Named-landform catalogue not found: {source}. Rebuild it with "
            "`python tools/fetch_nomenclature.py`."
        )
    return load_features(source)


def load_chinese_names(path: Optional[Any] = None) -> Dict[str, str]:
    """The official 中文名 the catalogue carries, keyed by its English name.

    Names live in the same file as the geometry, because they come from the same
    merge: ``tools/build_landform_db.py`` joins the IAU export to the official
    translation table, and a name with no feature (or a feature with no name) is
    reported there rather than guessed at here.
    """
    source = Path(path) if path is not None else package_data_dir() / NOMENCLATURE_FILE
    if not source.is_file():
        return {}
    names: Dict[str, str] = {}
    with source.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            name = (row.get("English Name") or "").strip()
            chinese = (row.get("名称") or "").strip()
            if name and chinese:
                names[name] = chinese
    return names


def _name_key(name: str) -> str:
    """Fold a name for matching: accents, case and spacing do not count."""
    folded = unicodedata.normalize("NFKD", str(name or ""))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return "".join(folded.casefold().split())


def annotation_catalogue(
    path: Optional[Any] = None,
    *,
    showpieces: Optional[Sequence[SurfaceFeature]] = None,
) -> Tuple[SurfaceFeature, ...]:
    """Everything worth labelling: the gazetteer, with showpieces taking priority.

    The showpiece catalogue is merged in first so that its notes win over the
    gazetteer's bare English row for the same place, and so a small installation
    with only one of the two files still annotates something.  Showpiece kinds
    are folded onto the gazetteer's vocabulary ("Mountain" -> "Mons", "Rille" ->
    "Rima"), so that a caller filtering on ``kind`` gets the same meaning
    whichever file a name came from.

    Chinese names come from the database and **override** the showpiece
    catalogue's own translations, which were informal: the showpieces call
    Clavius 克拉维斯环形山, the official table calls it 克拉维斯, and the
    official name is the one the page should show.
    """
    primary = tuple(showpieces) if showpieces is not None else load_features()
    gazetteer = load_nomenclature(path)
    official = {
        _name_key(feature.name_en): feature.name_zh
        for feature in gazetteer
        if feature.name_zh
    }
    features: List[SurfaceFeature] = []
    showpiece_keys = set()
    for feature in primary:
        key = _name_key(feature.name_en)
        if key in showpiece_keys:
            continue
        showpiece_keys.add(key)
        translated = replace(feature, name_zh=official.get(key, feature.name_zh))
        features.append(_with_gazetteer_kind(translated))
    shown = {feature.name_en for feature in features}
    for feature in gazetteer:
        if feature.name_en in shown:
            continue
        if _name_key(feature.name_en) in showpiece_keys:
            # A showpiece already stands for this place under a name that folds
            # to the same key (Vallis Schröteri / Vallis Schroteri); its
            # informal Chinese name has just been replaced by the official one.
            continue
        # The gazetteer is *not* deduplicated against itself: folding removes
        # spaces, so a lettered crater collides with any base crater whose name
        # ends in that letter -- Mee S with Mees, Watt S with Watts -- and they
        # are different features that both belong in the list.
        shown.add(feature.name_en)
        features.append(_with_gazetteer_kind(feature))
    return tuple(features)


#: Showpiece-catalogue kinds that mean the same thing as a gazetteer category.
_KIND_SYNONYMS = {
    "Mountain": "Mons",
    "Valley": "Vallis",
    "Rille": "Rima",
    "Fault": "Rupes",
    "Ridge": "Dorsum",
    "Sea": "Mare",
}


def _with_gazetteer_kind(feature: SurfaceFeature) -> SurfaceFeature:
    """Fold a showpiece kind onto the gazetteer vocabulary, and repair the rest.

    The repair is not cosmetic: twenty near-side maria are filed as ``Crater``
    by the shipped table, which would put 雨海 in the crater class and give it a
    crater's outline rule.  The name decides.
    """
    synonym = _KIND_SYNONYMS.get(feature.kind, feature.kind)
    canonical = canonical_kind(feature.name_en, synonym)
    return feature if canonical == feature.kind else replace(feature, kind=canonical)


# ---------------------------------------------------------------------------
# frames
# ---------------------------------------------------------------------------
def _unit(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if norm < 1e-12:  # pragma: no cover - a zero camera axis is unusable
        raise MoonlibError("cannot build a frame from a zero-length axis.")
    return np.asarray(vector, dtype=np.float64) / norm


def surface_normals(lat_deg: Any, lon_deg: Any) -> np.ndarray:
    """Unit vectors of selenographic ``(lat, lon)`` points, in ``MOON_ME``."""
    lat = np.radians(np.asarray(lat_deg, dtype=np.float64))
    lon = np.radians(np.asarray(lon_deg, dtype=np.float64))
    cos_lat = np.cos(lat)
    return np.stack(
        (cos_lat * np.cos(lon), cos_lat * np.sin(lon), np.sin(lat)), axis=-1
    )


@dataclass(frozen=True)
class FrameProjection:
    """The camera of one simulated frame, as a mapping to frame pixels.

    ``origin`` is the observer's position in the ``MOON_ME`` frame in km, and
    ``forward``/``right``/``up`` are the camera axes; ``pixel_scale`` is the
    tangent of the angle one pixel subtends, which is the same number the
    renderer's ray grid is built from.
    """

    kind: str
    width: int
    height: int
    origin: np.ndarray
    forward: np.ndarray
    right: np.ndarray
    up: np.ndarray
    pixel_scale: float
    sun: np.ndarray
    disk_fraction: Optional[float] = None
    span_km: Optional[float] = None
    rotation_deg: float = 0.0
    target_lon_deg: Optional[float] = None
    target_lat_deg: Optional[float] = None

    @property
    def arcsec_per_pixel(self) -> float:
        return self.pixel_scale * ARCSEC_PER_RADIAN

    def ground_sample_km(self, distance_km: float) -> float:
        """Surface kilometres per pixel at a given distance from the camera."""
        return self.pixel_scale * float(distance_km)

    def project(
        self, lat_deg: Any, lon_deg: Any, elevation_km: Any = 0.0
    ) -> Dict[str, np.ndarray]:
        """Frame pixels of selenographic points, plus what is true of each.

        Returns ``x``/``y`` in frame pixels, ``facing`` (the point is on the
        near side), ``incidence_deg`` (0 = directly under the Sun, 90 = on the
        terminator) and ``distance_km`` from the camera to the point.

        **Visibility is a hemisphere test, not a depth test.**  The Moon is a
        sphere, so the direction from the camera to a point on the *far* side
        still lies inside the disk's angular silhouette -- the near surface is
        simply in the way.  Testing the depth along the camera axis would
        therefore put every far-side feature "inside the frame", which is how a
        first version of this module ended up labelling 9 000 places at once.
        A point is visible when its surface normal faces the observer, and the
        far-side coordinates returned here are the projection continued through
        the Moon: callers must respect ``facing``.
        """
        normal = surface_normals(lat_deg, lon_deg)
        radius = MOON_REFERENCE_RADIUS_KM + np.asarray(elevation_km, dtype=np.float64)
        return self._project(normal, radius)

    def project_directions(
        self, directions: Any, elevation_km: Any = 0.0
    ) -> Dict[str, np.ndarray]:
        """Frame pixels of unit surface directions, on the reference sphere.

        The point of taking directions rather than lon/lat is :func:`rim_outline`,
        which walks a circle *on* the sphere: converting each sample back into
        coordinates would be a needless arc tangent, and would lose the pole.

        ``elevation_km`` is the one thing a direction cannot say on its own.  It
        keeps the direction as the surface *normal* and puts the point at the
        reference sphere plus the elevation, which is how a rim is drawn where
        the picture drew it: on the height field, not on the sphere under it.
        """
        return self._project(
            np.asarray(directions, dtype=np.float64),
            MOON_REFERENCE_RADIUS_KM + np.asarray(elevation_km, dtype=np.float64),
        )

    def _project(self, normal: np.ndarray, radius: Any) -> Dict[str, np.ndarray]:
        """Shared body of :meth:`project` and :meth:`project_directions`."""
        point = normal * np.asarray(radius)[..., None]
        offset = point - self.origin
        distance = np.linalg.norm(offset, axis=-1)
        direction = offset / np.maximum(distance, 1e-12)[..., None]
        # Contractions are written as einsum rather than ``@``: on macOS the
        # matrix-multiply ufunc routes through Accelerate, which leaves floating
        # point flags set and makes NumPy warn about overflow in a product whose
        # values are all near 1.0.
        depth = np.einsum("ij,j->i", direction, self.forward)
        facing = np.einsum("ij,j->i", normal, _unit(self.origin)) > 0.0
        safe = np.where(depth > 0.0, depth, 1.0)
        across = np.einsum("ij,j->i", direction, self.right) / safe
        along = np.einsum("ij,j->i", direction, self.up) / safe
        x = 0.5 * (self.width - 1) + across / self.pixel_scale
        y = 0.5 * (self.height - 1) - along / self.pixel_scale
        incidence = np.degrees(
            np.arccos(np.clip(np.einsum("ij,j->i", normal, self.sun), -1.0, 1.0))
        )
        return {
            "x": x,
            "y": y,
            "facing": facing,
            "incidence_deg": incidence,
            "distance_km": distance,
        }

    def apparent_radius_px(self, diameter_km: Any, distance_km: Any) -> np.ndarray:
        """Pixels the radius of a feature of *diameter_km* covers.

        The mean radius: a feature away from the sub-observer point is
        foreshortened into an ellipse, and this is the radius the label layout
        needs rather than the shape.
        """
        angular = 0.5 * np.asarray(diameter_km, dtype=np.float64) / np.maximum(
            np.asarray(distance_km, dtype=np.float64), 1e-9
        )
        return angular / self.pixel_scale

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe description, for a page that has to draw this frame."""
        return {
            "kind": self.kind,
            "width": int(self.width),
            "height": int(self.height),
            "pixel_scale": float(self.pixel_scale),
            "arcsec_per_pixel": self.arcsec_per_pixel,
            "disk_fraction": self.disk_fraction,
            "span_km": self.span_km,
            "rotation_deg": self.rotation_deg,
            "target_lon_deg": self.target_lon_deg,
            "target_lat_deg": self.target_lat_deg,
            "sun_direction": [float(v) for v in self.sun],
            "observer_position_km": [float(v) for v in self.origin],
        }


def disk_frame(
    view: MoonView,
    width: int,
    height: int,
    disk_fraction: Optional[float] = None,
) -> FrameProjection:
    """Frame of :func:`moonlib_s.render_moon` at a given preview size."""
    if disk_fraction is None:
        disk_fraction = MoonRenderSettings().disk_fraction
    if not 0.05 <= float(disk_fraction) <= 1.0:
        raise MoonlibError("disk_fraction must lie in [0.05, 1.0].")
    if int(width) < 8 or int(height) < 8:
        raise MoonlibError("a frame needs an image of at least 8x8 pixels.")
    forward, right, up = _camera_basis(view)
    pixel_scale = 2.0 * math.tan(
        math.radians(0.5 * view.angular_diameter_deg)
    ) / (float(disk_fraction) * min(int(width), int(height)))
    return FrameProjection(
        kind="disk",
        width=int(width),
        height=int(height),
        origin=float(view.distance_km) * np.asarray(view.observer_direction, dtype=np.float64),
        forward=forward,
        right=right,
        up=up,
        pixel_scale=pixel_scale,
        sun=_unit(np.asarray(view.sun_direction, dtype=np.float64)),
        disk_fraction=float(disk_fraction),
    )


def region_frame(
    view: MoonView,
    lon_deg: float,
    lat_deg: float,
    span_km: float,
    width: int,
    height: int,
    rotation_deg: float = 0.0,
    terrain: Optional[MoonTerrain] = None,
) -> FrameProjection:
    """Frame of :func:`moonlib_s.render_region` for the same arguments.

    *terrain* is used only to place the target on the height field, exactly as
    the renderer does; a few hundred metres of elevation there is a ten-
    thousandth of a pixel of camera tilt, so the global pyramid is close enough
    for a frame whose window the renderer may have upgraded.
    """
    if int(width) < 8 or int(height) < 8:
        raise MoonlibError("a frame needs an image of at least 8x8 pixels.")
    if not float(span_km) > 0.0:
        raise MoonlibError("span_km must be positive.")
    origin = float(view.distance_km) * np.asarray(
        view.observer_direction, dtype=np.float64
    )
    target = _target_vector(float(lon_deg), float(lat_deg), terrain)
    to_target = target - origin
    distance = float(np.linalg.norm(to_target))
    if distance <= 0.0:  # pragma: no cover - the observer is on the surface
        raise MoonlibError("the camera is at the target; no frame is defined.")
    forward = to_target / distance
    pole = np.array([0.0, 0.0, 1.0])
    up = pole - float(np.dot(pole, forward)) * forward
    up = _unit(up)
    right = _unit(np.cross(forward, up))
    right, up = _rolled_basis(right, up, float(rotation_deg))
    pixel_scale = 2.0 * math.tan(math.atan(0.5 * float(span_km) / distance)) / min(
        int(width), int(height)
    )
    return FrameProjection(
        kind="region",
        width=int(width),
        height=int(height),
        origin=origin,
        forward=forward,
        right=right,
        up=up,
        pixel_scale=pixel_scale,
        sun=_unit(np.asarray(view.sun_direction, dtype=np.float64)),
        span_km=float(span_km),
        rotation_deg=float(rotation_deg),
        target_lon_deg=float(lon_deg),
        target_lat_deg=float(lat_deg),
    )


# ---------------------------------------------------------------------------
# the height field a name is standing on
# ---------------------------------------------------------------------------
#: The Moon's relief, summit to basin floor, in kilometres: +10.8 km at the
#: Selenean summit and -9.1 km in the floor of Antoniadi, both of which the
#: survey this library renders has measured.  It is the one number needed to
#: bound how far a label can move when it is lifted off the reference sphere,
#: and it is stated once here rather than derived from a raster scan a plan
#: cannot afford.
_MOON_RELIEF_KM = 11.0


def _terrain_level(
    terrain: MoonTerrain, frame: FrameProjection, level: Optional[int] = None
) -> int:
    """Which DEM level a plan or a rim should read its elevations from.

    ``None`` means the level the renderer itself would choose for this frame's
    footprint -- :meth:`RasterPyramid.level_for_ground_scale` on the ground the
    frame covers per pixel.  (A render that supersamples reads one level finer
    still; the smoothing that leaves is a few hundredths of a pixel.)  That rule
    is what keeps a full-disk plan affordable: at a 600 px preview one pixel
    already spans nearly 6 km, so the level it reads is 7.6 km per pixel and the
    few thousand lookups touch 2 MB of raster.  The base level is 0.24 km per
    pixel and 2 GB of raster, and a scattered read of eight thousand points from
    it is a second of page faults for a correction no picture could show.  A
    caller that wants the finest field anyway passes the level explicitly, and a
    close-up (where the frame's own footprint is finer than the pyramid) reaches
    level 0 by itself.
    """
    if level is not None:
        return int(level)
    distance = float(np.linalg.norm(frame.origin))
    return terrain.dem.level_for_ground_scale(frame.ground_sample_km(distance))


def _elevation_slack_px(frame: FrameProjection) -> float:
    """How far, in pixels, an elevation can move a point across the frame.

    This is what lets a plan sample the height field only where it matters.
    A point at radius ``c`` and angle ``theta`` from the camera axis lands
    ``c*sin(theta) / ((D + c*cos(theta)) * s)`` pixels from the frame centre
    (``D`` the camera distance, ``s`` the pixel scale), so lifting it by one
    kilometre moves it by ``D*sin(theta) / ((D + c*cos(theta))**2 * s)``.  On
    the near side ``cos(theta) > 0``, and the derivative is largest at the limb,
    where it is ``1/(D*s)``: the relief over one pixel's worth of distance.  A
    feature whose sphere pixel is inside the frame by less than this may still
    be in the picture once it is standing on the ground, and one further out
    than this cannot be.  The extra pixel covers the second-order term the
    derivative drops, and the coarse level's own smoothing of the ground.

    Frames of both kinds are covered: a close-up's ``D*s`` is the span of one
    pixel on the ground, so the slack there is metres of relief per pixel.
    """
    distance = float(np.linalg.norm(frame.origin))
    return _MOON_RELIEF_KM / max(distance * frame.pixel_scale, 1e-12) + 1.0


# ---------------------------------------------------------------------------
# what to label, and where to write it
# ---------------------------------------------------------------------------
def rim_outline(
    frame: FrameProjection,
    lat_deg: float,
    lon_deg: float,
    diameter_km: float,
    *,
    points: int = 48,
    terrain: Optional[MoonTerrain] = None,
    terrain_level: Optional[int] = None,
) -> Optional[Tuple[Tuple[float, float], ...]]:
    """Closed polygon of a feature's rim in frame pixels, or ``None``.

    The rim is walked as a **small circle on the sphere**: the centre direction
    is rotated by the feature's angular radius towards each of *points*
    azimuths, and every sample is projected through the frame's own camera.  On
    a sphere that is not a circle in the picture -- it opens into the ellipse
    foreshortening makes of it, and near the limb into a shape no ellipse
    describes -- which is exactly why the samples are projected instead of an
    ellipse being fitted.

    *terrain* moves the circle onto the height field: the direction of each
    sample is kept as the rim's own normal, and the sample is placed at the
    reference sphere plus the elevation there (see
    :meth:`FrameProjection.project_directions`).  A rim read off the sphere is
    drawn inside the limb the renderer draws by however high that ground
    happens to be -- a kilometre of relief is 0.16 px at 600 px and 0.65 px at
    2400 -- so a caller that has the terrain should say so.  *terrain_level*
    picks the DEM level, ``None`` for the one the frame's footprint asks for.

    Two things return ``None`` rather than a broken shape: a feature whose
    centre is on the far side, and one whose rim crosses the limb, where part of
    the circle is simply not there to draw.

    The outline is a *reference* outline: it says where the gazetteer puts the
    centre and how wide it says the feature is.  Irregular rims, flooded craters
    and basins whose walls have been buried are not circles, and this does not
    pretend to trace them.
    """
    center = surface_normals(lat_deg, lon_deg)
    if float(np.dot(center, _unit(frame.origin))) <= 0.0:
        return None
    if not diameter_km > 0.0 or points < 8:
        return None
    angular = 0.5 * float(diameter_km) / MOON_REFERENCE_RADIUS_KM
    # A local frame at the centre: east along the parallel, north along the
    # meridian.  At a pole the east direction is undefined, so it is taken from
    # the meridian instead -- any perpendicular will do, the circle is a circle.
    east = np.cross(np.array([0.0, 0.0, 1.0]), center)
    norm = float(np.linalg.norm(east))
    if norm < 1e-9:
        east = np.cross(np.array([0.0, 1.0, 0.0]), center)
        norm = float(np.linalg.norm(east))
    east = east / max(norm, 1e-12)
    north = np.cross(center, east)
    azimuth = np.linspace(0.0, 2.0 * math.pi, int(points), endpoint=False)
    directions = (
        math.cos(angular) * center[None, :]
        + math.sin(angular)
        * (
            np.cos(azimuth)[:, None] * east[None, :]
            + np.sin(azimuth)[:, None] * north[None, :]
        )
    )
    if terrain is None:
        projected = frame.project_directions(directions)
    else:
        # The circle above is a circle of *directions*; the ground along it is
        # what decides where each of those directions meets the surface.  The
        # samples go back to lon/lat only to be looked up, and the pole is no
        # trouble: the direction is the normal either way.
        level = _terrain_level(terrain, frame, terrain_level)
        heights = np.asarray(
            terrain.height_km(
                np.degrees(np.arctan2(directions[:, 1], directions[:, 0])),
                np.degrees(np.arcsin(np.clip(directions[:, 2], -1.0, 1.0))),
                level,
            ),
            dtype=np.float64,
        )
        projected = frame.project_directions(directions, heights)
    if not bool(np.all(projected["facing"])):
        return None
    xs = np.asarray(projected["x"], dtype=np.float64)
    ys = np.asarray(projected["y"], dtype=np.float64)
    if not (np.isfinite(xs).all() and np.isfinite(ys).all()):
        return None
    return tuple(
        (round(float(x), 1), round(float(y), 1)) for x, y in zip(xs, ys)
    )


@dataclass(frozen=True)
class Annotation:
    """One named feature inside a frame, with its marker and its label box."""

    name_en: str
    name_zh: str
    kind: str
    lat_deg: float
    lon_deg: float
    diameter_km: float
    x: float
    y: float
    radius_px: float
    incidence_deg: float
    illuminated: bool
    score: float
    label: str
    label_x: Optional[float]
    label_y: Optional[float]
    label_w: float
    label_h: float
    leader: bool
    outline: Optional[Tuple[Tuple[float, float], ...]] = None
    feature_class: str = "other"

    @property
    def labelled(self) -> bool:
        return self.label_x is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name_en": self.name_en,
            "name_zh": self.name_zh,
            "kind": self.kind,
            "class": self.feature_class,
            "lat_deg": self.lat_deg,
            "lon_deg": self.lon_deg,
            "diameter_km": self.diameter_km,
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "radius_px": round(self.radius_px, 2),
            "incidence_deg": round(self.incidence_deg, 2),
            "illuminated": self.illuminated,
            "score": round(self.score, 3),
            "label": self.label,
            "labelled": self.labelled,
            "label_x": None if self.label_x is None else round(self.label_x, 2),
            "label_y": None if self.label_y is None else round(self.label_y, 2),
            "label_w": round(self.label_w, 2),
            "label_h": round(self.label_h, 2),
            "leader": self.leader,
            "outline": (
                None if self.outline is None
                else [[x, y] for x, y in self.outline]
            ),
        }


@dataclass(frozen=True)
class AnnotationPlan:
    """A frame's labels plus the counts that explain what was left out."""

    frame: FrameProjection
    items: Tuple[Annotation, ...]
    catalogue_size: int
    in_frame: int
    unlit: int
    too_small: int
    over_budget: int
    unplaced: int
    font_px: float
    language: str = "zh"

    @property
    def labelled(self) -> int:
        return sum(1 for item in self.items if item.labelled)

    @property
    def outlined(self) -> int:
        return sum(1 for item in self.items if item.outline)

    def class_counts(self) -> Dict[str, int]:
        """How many placed items each class has, in :data:`FEATURE_CLASSES` order."""
        counts = {key: 0 for key in FEATURE_CLASSES}
        for item in self.items:
            counts[item.feature_class] = counts.get(item.feature_class, 0) + 1
        return counts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "frame": self.frame.to_dict(),
            "items": [item.to_dict() for item in self.items],
            "font_px": self.font_px,
            "language": self.language,
            "counts": {
                "catalogue": self.catalogue_size,
                "in_frame": self.in_frame,
                "unlit": self.unlit,
                "too_small": self.too_small,
                "over_budget": self.over_budget,
                "placed": len(self.items),
                "labelled": self.labelled,
                "outlined": self.outlined,
                "unplaced": self.unplaced,
                "classes": self.class_counts(),
            },
        }


#: How wide a character is, in ems, by class.  Deliberately generous: the label
#: boxes only have to be big enough that text does not collide, and an estimate
#: that is a little too wide costs a little air, while one that is too narrow
#: lets two labels overlap.
_CHAR_WIDTH = {"wide": 1.0, "digit": 0.58, "upper": 0.68, "lower": 0.55, "thin": 0.34}


def estimate_text_width(text: str, font_px: float) -> float:
    """Width of *text* in pixels at *font_px*, without a font file.

    Characters are classified rather than measured: CJK and full-width
    punctuation take a full em, digits and capitals a bit more than half, and
    the thin punctuation in a name like ``"Vallis Schröteri"`` a third.
    """
    total = 0.0
    for character in str(text):
        code = ord(character)
        if code < 0x20:
            continue
        if code >= 0x1100 and (
            code <= 0x115F
            or 0x2E80 <= code <= 0xA4CF
            or 0xAC00 <= code <= 0xD7A3
            or 0xF900 <= code <= 0xFAFF
            or 0xFE30 <= code <= 0xFE6F
            or 0xFF00 <= code <= 0xFF60
            or 0xFFE0 <= code <= 0xFFE6
        ):
            total += _CHAR_WIDTH["wide"]
        elif character.isdigit():
            total += _CHAR_WIDTH["digit"]
        elif character.isupper():
            total += _CHAR_WIDTH["upper"]
        elif character in " .,:;'\"|!i l":
            total += _CHAR_WIDTH["thin"]
        else:
            total += _CHAR_WIDTH["lower"]
    return total * float(font_px)


def _display_label(feature: SurfaceFeature, language: str) -> str:
    """What the page writes for one feature, in the language it asked for.

    ``zh`` falls back to English rather than drawing nothing: the official
    table covers 98.7% of the gazetteer, and an unnamed crater in a labelled
    picture reads as a bug.  ``both`` puts the official name first, which is
    the one a Chinese-language page is read for.
    """
    chinese, english = feature.name_zh, feature.name_en
    if language == "en" or not chinese:
        return english
    if language == "zh":
        return chinese
    return f"{chinese} {english}"


def _size_score(radius_px: float, unit: float = 1.0) -> float:
    """How much a feature's apparent size counts towards its label.

    Compressed logarithmically: Mare Imbrium is eleven times the apparent radius
    of Clavius, but it is not eleven times more worth naming, and a linear score
    would make the label budget a list of the same few maria every time.

    *unit* is the label-height scale (see :data:`_DEFAULT_FONT_PX`): the
    reference radius is twelve pixels *at the default font*, so the ranking of a
    frame does not depend on how many pixels it happens to be rendered at.
    """
    return 40.0 * math.log1p(max(float(radius_px), 0.0) / (12.0 * float(unit)))


def _family(kind: str, group: str = "") -> str:
    """Which share of the label budget a kind is drawn from.

    A few families rather than one per class: fifteen classes sharing a
    thirty-label budget would give a landing site the same number of names as
    the maria, and the round-robin exists to stop the *largest* things taking
    every slot, not to make every class equally loud.  Landing sites get their
    own family so that they can be drawn *first* -- see ``_SITE_SHARE``.
    """
    group = group or _CLASS_BY_KIND.get(kind, "")
    # Only the IAU's own landing sites get the sites family, and that family is
    # outside the budget.  The atlas's *historical* sites -- Luna 2, Ranger 4 and
    # the rest of the probes that hit the Moon -- are `statio` by class but they
    # are seventy of them, and exempting those emptied every other family out of
    # the plan.
    if kind == _SITE_KIND:
        return "sites"
    if group in _SEA_CLASSES:
        return "seas"
    if kind == "Crater":
        return "craters"
    return "other"


def _select_by_family(
    candidates: Sequence[Tuple[float, int, str]], budget: int
) -> Tuple[List[Tuple[float, int, str]], int]:
    """Take the best of each family in turn until *budget* labels are chosen.

    Returns the chosen ``(score, index, family)`` triples, best first within
    each family, and how many candidates were left over.
    """
    buckets: Dict[str, List[Tuple[float, int, str]]] = {}
    for item in candidates:
        buckets.setdefault(item[2], []).append(item)
    chosen: List[Tuple[float, int, str]] = []
    # Every landing site, before the budget is spent and outside it: they are the
    # smallest things here and the ones a reader is most likely to be looking
    # for, and the round-robin below would never reach them at a low density.
    sites = buckets.get("sites")
    while sites and len(chosen) < _SITE_LIMIT:
        chosen.append(sites.pop(0))
    while len(chosen) < budget:
        progressed = False
        for family in _FAMILY_ORDER:
            bucket = buckets.get(family)
            if bucket and len(chosen) < budget:
                chosen.append(bucket.pop(0))
                progressed = True
        if not progressed:
            break
    left_over = sum(len(bucket) for bucket in buckets.values())
    return chosen, left_over


def plan_annotations(
    frame: FrameProjection,
    features: Sequence[SurfaceFeature],
    *,
    max_labels: int = 30,
    min_radius_px: float = 2.5,
    min_diameter_km: float = 0.0,
    only_illuminated: bool = True,
    kinds: Optional[Iterable[str]] = None,
    classes: Optional[Iterable[str]] = None,
    font_px: float = 13.0,
    padding_px: Optional[float] = None,
    margin_px: Optional[float] = None,
    elevation_km: float = 0.0,
    terrain: Optional[MoonTerrain] = None,
    terrain_level: Optional[int] = None,
    highlight: Optional[Iterable[str]] = None,
    language: str = "zh",
    outline_min_radius_px: float = 9.0,
    outline_points: int = 48,
) -> AnnotationPlan:
    """Choose the labels for one frame and lay them out.

    Parameters
    ----------
    frame
        Camera of the frame being annotated.
    features
        Catalogue to choose from; usually :func:`annotation_catalogue`.
    max_labels
        Label budget.  Markers are still returned for features that lose their
        label to the budget or to the layout.
    min_radius_px
        Smallest apparent radius worth a marker, so a close-up of a mare floor
        does not fill up with the gazetteer's lettered craterlets.
    min_diameter_km
        Hard floor on published diameter, for callers that want a short list.
    only_illuminated
        Drop features on the night side of the terminator.
    kinds
        Restrict to these kinds (``{"Crater", "Mare", …}``); ``None`` keeps all.
    classes
        Restrict to these classes (:data:`FEATURE_CLASSES`); ``None`` keeps all.
        This is the filter a page puts a switch on, because it is the grouping a
        reader recognises: 大型撞击盆地, 月海, 月溪, …
    font_px
        Font size the layout assumes the page will draw with.  Returned in the
        plan so the two cannot disagree.
    elevation_km
        Height above the reference sphere to place *every* feature at, used
        only when *terrain* is ``None``: it is the historical single scalar the
        whole catalogue shares.
    terrain
        Height field to place the features on.  With it, each feature is
        projected at the elevation of the ground under it, which is where the
        renderer drew it -- reading the elevations off the reference sphere
        instead leaves a label on the sphere, up to two pixels from its own
        ground at 600 px and seven at 2400 -- and *elevation_km* is then not
        used.  ``None`` (the default) keeps the plan on the reference sphere, to
        the last bit of what it was before this parameter existed.
    terrain_level
        DEM level the elevations are read from; ``None`` asks
        :func:`_terrain_level` for the one this frame's footprint is drawn at,
        which is the cheap one to read as well.
    highlight
        Names to treat as showpieces (:data:`_HIGHLIGHT_BONUS`).  Matching
        ignores accents, case and spacing; see :func:`annotate_frame`.
    language
        One of :data:`LANGUAGES`.  The label text decides the layout, so this is
        a layout input, not a display detail: 克拉维斯 and Clavius are different
        widths, and a plan laid out for one is the wrong plan for the other.
    outline_min_radius_px
        Apparent radius at which a round feature also gets its rim drawn.  Below
        it the outline would be smaller than the marker it surrounds.
    outline_points
        Samples around one rim; 48 is smooth at any zoom the page offers.
    """
    if max_labels < 0:
        raise MoonlibError("max_labels cannot be negative.")
    if language not in LANGUAGES:
        raise MoonlibError(
            f"language must be one of {', '.join(LANGUAGES)}; got {language!r}."
        )
    # Everything below is in "label heights": the frame's own pixel count is an
    # implementation detail of whoever asked for the plan, so a plan at another
    # font size comes out as the same plan, scaled.
    unit = max(float(font_px), 6.0) / _DEFAULT_FONT_PX
    allowed = None if kinds is None else {str(kind) for kind in kinds}
    wanted = None if classes is None else {str(key) for key in classes}
    if wanted is not None:
        unknown = sorted(wanted - set(FEATURE_CLASSES))
        if unknown:
            raise MoonlibError(
                f"unknown landform class {unknown[0]!r}; expected one of "
                f"{', '.join(FEATURE_CLASSES)}."
            )
    highlighted = (
        frozenset() if highlight is None else {_name_key(name) for name in highlight}
    )
    projected = frame.project(
        [feature.lat_deg for feature in features],
        [feature.lon_deg for feature in features],
        elevation_km,
    )
    x = np.asarray(projected["x"], dtype=np.float64)
    y = np.asarray(projected["y"], dtype=np.float64)
    facing = np.asarray(projected["facing"])
    incidence = np.asarray(projected["incidence_deg"], dtype=np.float64)
    distance = np.asarray(projected["distance_km"], dtype=np.float64)

    if terrain is not None:
        # Two passes, because a full-disk plan carries ten thousand names and
        # the raster is 2 GB: project everything on the sphere first, then ask
        # the terrain only about the points that could still be in the picture
        # once they are standing on it.  That second set is the frame grown by
        # _elevation_slack_px -- the most a point can move -- so nothing that
        # the elevation could pull into frame is missed, and the far-side names,
        # a quarter of the catalogue, are never sampled at all.  The pixels that
        # come back are the ones this plan reports: a feature the ground pushes
        # over the limb is dropped by the same test that drops one that was
        # never in frame.
        latitude = np.array([feature.lat_deg for feature in features], dtype=np.float64)
        longitude = np.array([feature.lon_deg for feature in features], dtype=np.float64)
        slack = _elevation_slack_px(frame)
        near = (
            facing
            & (x >= -slack)
            & (x <= frame.width - 1 + slack)
            & (y >= -slack)
            & (y <= frame.height - 1 + slack)
        )
        lifted_index = np.nonzero(near)[0]
        if lifted_index.size:
            level = _terrain_level(terrain, frame, terrain_level)
            heights = np.asarray(
                terrain.height_km(
                    longitude[lifted_index], latitude[lifted_index], level
                ),
                dtype=np.float64,
            )
            lifted = frame.project(
                latitude[lifted_index], longitude[lifted_index], heights
            )
            x[lifted_index] = lifted["x"]
            y[lifted_index] = lifted["y"]
            facing[lifted_index] = lifted["facing"]
            incidence[lifted_index] = lifted["incidence_deg"]
            distance[lifted_index] = lifted["distance_km"]

    radius = frame.apparent_radius_px(
        np.array([feature.diameter_km for feature in features], dtype=np.float64), distance
    )

    inside = (
        facing
        & (x >= 0.0)
        & (x <= frame.width - 1)
        & (y >= 0.0)
        & (y <= frame.height - 1)
    )
    centre_x = 0.5 * (frame.width - 1)
    centre_y = 0.5 * (frame.height - 1)
    half_diagonal = max(1.0, math.hypot(centre_x, centre_y))

    candidates: List[Tuple[float, int, str]] = []
    groups: Dict[int, str] = {}
    unlit = too_small = in_frame = 0
    for index, feature in enumerate(features):
        if not inside[index]:
            continue
        in_frame += 1
        if allowed is not None and feature.kind not in allowed:
            continue
        group = feature_class(feature.kind, feature.diameter_km)
        if wanted is not None and group not in wanted:
            continue
        # Landing sites are exempt from the size floor: a 1 km site is a dot on
        # a full disk, and it is still the thing the reader is looking for.
        if not is_landing_site(feature) and (
            feature.diameter_km < min_diameter_km or radius[index] < min_radius_px
        ):
            too_small += 1
            continue
        luminous = float(incidence[index]) < 90.0
        if not luminous:
            unlit += 1
            if only_illuminated:
                continue
        centrality = 1.0 - min(
            1.0, math.hypot(float(x[index]) - centre_x, float(y[index]) - centre_y) / half_diagonal
        )
        score = (
            _size_score(float(radius[index]), unit)
            + _KIND_BONUS.get(feature.kind, 0.0)
            + (_HIGHLIGHT_BONUS if _name_key(feature.name_en) in highlighted else 0.0)
            + 6.0 * centrality
            + (0.0 if luminous else -8.0)
        )
        groups[index] = group
        family = _family(feature.kind, group)
        # Only the sites themselves are exempt: a landmark inside one is a name
        # like any other.  `_family` cannot tell them apart -- both are `Statio`
        # -- so the decision is made here, where the feature is in hand.
        if family == "sites" and not is_landing_site(feature):
            family = "other"
        candidates.append((score, index, family))

    candidates.sort(key=lambda item: (-item[0], item[1]))
    chosen, over_budget = _select_by_family(candidates, max_labels)
    # Sites were taken first and are not counted against the budget.
    over_budget = max(0, over_budget)
    # Landmarks first: the highlighted showpieces, and each family's leader.
    # They are what a reader looks for, so their captions reserve space before
    # the ordinary dots are allowed to claim any.
    important_indices = {candidates[0][1]} if candidates else set()
    seen_families = set()
    for _score, _index, family_name in candidates:
        if family_name not in seen_families:
            seen_families.add(family_name)
            important_indices.add(_index)
    if highlight:
        important_indices.update(
            index for index, feature in enumerate(features)
            if any(name in feature.name_en for name in highlight)
        )

    max_marker = _MAX_MARKER_PX * unit
    padding = 3.0 * unit if padding_px is None else float(padding_px)
    label_height = font_px * 1.25
    boxes: List[Tuple[float, float, float, float]] = []
    markers: List[Tuple[float, float, float, float]] = []
    for _score, index, _family_name in chosen:
        marker = min(float(radius[index]), max_marker) + unit
        markers.append(
            (
                float(x[index]) - marker,
                float(y[index]) - marker,
                2.0 * marker,
                2.0 * marker,
            )
        )

    # Every caption is arranged at once rather than one after another: the
    # collision test has to see the whole set, and the reflow can only free room
    # if it may move captions that are already down.  The budget has been spent
    # by now, so nothing is held back in reserve here.
    gap = 2.0 * unit          # proportional, so a plan scales with the frame
    wanted = []
    for order, (_score, index, _family_name) in enumerate(chosen):
        wanted.append({
            "key": index,
            "anchor": (float(x[index]), float(y[index])),
            "radius_px": min(float(radius[index]), max_marker),
            "width": estimate_text_width(_display_label(features[index], language), font_px)
            + 2.0 * padding,
            "height": label_height,
            "priority": order,
            "important": index in important_indices,
        })
    placements, _arranged_leaders, _omitted = arrange_labels(
        wanted, frame.width, frame.height, gap=gap)

    items: List[Annotation] = []
    unplaced = 0
    for score, index, _family_name in chosen:
        feature = features[index]
        text = _display_label(feature, language)
        width = estimate_text_width(text, font_px) + 2.0 * padding
        marker = min(float(radius[index]), max_marker)
        rect = placements.get(index)
        placement = None if rect is None else (float(rect[0]), float(rect[1]))
        if placement is None:
            unplaced += 1
            label_x = label_y = None
            leader = False
        else:
            label_x, label_y = placement
            boxes.append((label_x, label_y, width, label_height))
            # A leader is only worth drawing when the text had to move far
            # enough that the reader could not pair it with its marker.
            near_x = min(max(float(x[index]), label_x), label_x + width)
            near_y = min(max(float(y[index]), label_y), label_y + label_height)
            leader = (
                math.hypot(float(x[index]) - near_x, float(y[index]) - near_y)
                > marker + 4.0 * unit
            )
        outline = None
        if (
            groups.get(index) in _OUTLINE_CLASSES
            and float(radius[index]) >= outline_min_radius_px
        ):
            outline = rim_outline(
                frame,
                float(feature.lat_deg),
                float(feature.lon_deg),
                float(feature.diameter_km),
                points=int(outline_points),
                terrain=terrain,
                terrain_level=terrain_level,
            )
        items.append(
            Annotation(
                name_en=feature.name_en,
                name_zh=feature.name_zh,
                kind=feature.kind,
                lat_deg=float(feature.lat_deg),
                lon_deg=float(feature.lon_deg),
                diameter_km=float(feature.diameter_km),
                x=float(x[index]),
                y=float(y[index]),
                radius_px=float(radius[index]),
                incidence_deg=float(incidence[index]),
                illuminated=float(incidence[index]) < 90.0,
                score=float(score),
                label=text,
                label_x=label_x,
                label_y=label_y,
                label_w=width,
                label_h=label_height,
                leader=bool(leader),
                outline=outline,
                feature_class=groups.get(index, "other"),
            )
        )

    return AnnotationPlan(
        frame=frame,
        items=tuple(items),
        catalogue_size=len(features),
        in_frame=in_frame,
        unlit=unlit,
        too_small=too_small,
        over_budget=over_budget,
        unplaced=unplaced,
        font_px=float(font_px),
        language=language,
    )


#: Directions a label is tried in, as (across, along) in screen axes where
#: +x is right and +y is down.  Right first, then left: a name reads outwards
#: from its marker, and the eye finds it before it finds one placed on top.
_LABEL_DIRECTIONS = (
    (1.0, 0.0),
    (-1.0, 0.0),
    (0.0, -1.0),
    (0.0, 1.0),
    (0.7, -0.7),
    (-0.7, -0.7),
    (0.7, 0.7),
    (-0.7, 0.7),
)


def _place_label(
    x: float,
    y: float,
    marker_px: float,
    width: float,
    height: float,
    frame_width: int,
    frame_height: int,
    occupied: Sequence[Tuple[float, float, float, float]],
    margin_px: float,
    unit: float = 1.0,
) -> Optional[Tuple[float, float]]:
    """First free position for a label near ``(x, y)``, or ``None``.

    Candidate boxes start just outside the marker and are pushed further out
    along each direction until one fits, which is what lets a tight cluster of
    craters keep all of its names: the innermost label takes the near slot and
    the others step outwards.  *unit* is the label-height scale, so the gaps
    around a label grow with the type rather than staying 4 pixels wide on a
    10 000 px frame.
    """
    for step in range(4):
        gap = marker_px + (4.0 + step * (height + 3.0)) * unit
        for across, along in _LABEL_DIRECTIONS:
            if across > 0:
                left = x + gap
            elif across < 0:
                left = x - gap - width
            else:
                left = x - 0.5 * width
            if along > 0:
                top = y + gap
            elif along < 0:
                top = y - gap - height
            else:
                top = y - 0.5 * height
            left = min(max(left, margin_px), frame_width - margin_px - width)
            top = min(max(top, margin_px), frame_height - margin_px - height)
            box = (left, top, width, height)
            if _is_free(box, occupied):
                return left, top
    return None


def _is_free(
    box: Tuple[float, float, float, float],
    occupied: Sequence[Tuple[float, float, float, float]],
) -> bool:
    left, top, width, height = box
    right, bottom = left + width, top + height
    for other_left, other_top, other_width, other_height in occupied:
        if (
            left < other_left + other_width
            and other_left < right
            and top < other_top + other_height
            and other_top < bottom
        ):
            return False
    return True


def annotate_frame(
    frame: FrameProjection,
    features: Optional[Sequence[SurfaceFeature]] = None,
    **options: Any,
) -> AnnotationPlan:
    """Convenience wrapper: catalogue in, finished plan out.

    Unless the caller says otherwise, the curated showpiece catalogue is what
    counts as highlight-worthy, which is the only thing that puts Tycho on a
    full-disk label list ahead of crater rims that merely cover more pixels.
    """
    catalogue = features if features is not None else annotation_catalogue()
    if options.get("highlight") is None:
        options["highlight"] = [feature.name_en for feature in load_features()]
    return plan_annotations(frame, catalogue, **options)


# ---------------------------------------------------------------------------
# label placement: the pieces the layout is built from
# ---------------------------------------------------------------------------
#: Candidate directions, diagonals first: a caption set at 45 degrees tucks in
#: closer to its feature than one set straight up, and the cardinal directions
#: stay available for the crowded cases.
_LABEL_ANGLES = (45.0, -45.0, 135.0, -135.0, 0.0, 180.0, 90.0, -90.0,
                 22.5, -22.5, 157.5, -157.5, 67.5, -67.5, 112.5, -112.5)

#: Three local distances per direction: against the feature, one gap out, and a
#: last resort further out -- enough reach to escape a crowded limb, not enough
#: to fling a caption into empty space.
_LABEL_REACH = (0.0, 1.0, 2.5)
#: Important captions -- landmarks, the ones a reader looks for -- may also try
#: far positions when every compact one is taken.  Only they pay for it, and
#: only after the compact search has failed.
_LABEL_REACH_EXTENDED = (4.0, 7.0, 11.0)

#: An obstacle is the marker, not the whole feature.  A crater a hundred pixels
#: across would otherwise forbid captions everywhere inside it, which on a
#: whole-Moon view leaves no room around the most prominent features of all.
MARKER_HEIGHT_FRACTION = 0.45
MARKER_MIN_RADIUS_PX = 4.0


def marker_radius_px(radius_px: float, font_px: float) -> float:
    """Radius of the dot that stands for a feature, used as its obstacle size."""
    return min(float(radius_px), max(MARKER_MIN_RADIUS_PX, float(font_px) * MARKER_HEIGHT_FRACTION))


def outline_exit_distance(
    point: Tuple[float, float],
    angle_deg: float,
    outline: Sequence[Tuple[float, float]],
    fallback_radius_px: float,
) -> Optional[float]:
    """How far along *angle_deg* the rim of *outline* is, if it is in the way.

    A caption belongs outside the thing it names, and for a large feature the
    fixed radius is well inside the rim.  Casting the leader outward and taking
    the last crossing gives the distance at which the rim is cleared -- so a
    caption for a 200 px mare starts beyond it instead of on top of it.

    Returns ``None`` when the ray misses the outline (an occulted rim, or a
    feature whose outline does not close), leaving the caller to use the
    fallback.
    """
    if not outline or len(outline) < 2:
        return None
    radians = math.radians(angle_deg)
    dx, dy = math.cos(radians), math.sin(radians)
    ax, ay = float(point[0]), float(point[1])
    crossings: List[float] = []
    points = list(outline) + [outline[0]]
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        # Solve a + t*d = p1 + u*(p2 - p1) for t >= 0 and u in [0, 1].
        ex, ey = x2 - x1, y2 - y1
        denominator = dx * ey - dy * ex
        if abs(denominator) < 1e-12:
            continue
        t = ((x1 - ax) * ey - (y1 - ay) * ex) / denominator
        u = ((x1 - ax) * dy - (y1 - ay) * dx) / denominator
        if t >= 0.0 and -1e-9 <= u <= 1.0 + 1e-9:
            crossings.append(t)
    if not crossings:
        return None
    return max(crossings)


# ---------------------------------------------------------------------------
# the placement itself: candidates, an index, a score, and a bounded reflow
# ---------------------------------------------------------------------------
def _rects_intersect(a: Tuple[float, float, float, float],
                     b: Tuple[float, float, float, float], padding: float = 0.0) -> bool:
    return not (a[0] + a[2] + padding <= b[0] or b[0] + b[2] + padding <= a[0]
                or a[1] + a[3] + padding <= b[1] or b[1] + b[3] + padding <= a[1])


def _segment_hits_rect(line: Tuple[float, float, float, float],
                       rect: Tuple[float, float, float, float], padding: float = 0.0) -> bool:
    x, y, w, h = rect[0] - padding, rect[1] - padding, rect[2] + 2 * padding, rect[3] + 2 * padding
    x1, y1, x2, y2 = line
    if max(x1, x2) < x or min(x1, x2) > x + w or max(y1, y2) < y or min(y1, y2) > y + h:
        return False
    if x <= x1 <= x + w and y <= y1 <= y + h:
        return True
    return (outline_exit_distance((x1, y1), 0.0, [(x, y), (x + w, y), (x + w, y + h), (x, y + h)], 0.0)
            is not None and _segment_crosses_box(line, (x, y, w, h)))


def _segment_crosses_box(line: Tuple[float, float, float, float],
                         rect: Tuple[float, float, float, float]) -> bool:
    x, y, w, h = rect
    edges = [((x, y), (x + w, y)), ((x + w, y), (x + w, y + h)),
             ((x + w, y + h), (x, y + h)), ((x, y + h), (x, y))]
    return any(_segments_cross(line, (a[0], a[1], b[0], b[1])) for a, b in edges)


def _segments_cross(first: Tuple[float, float, float, float],
                    second: Tuple[float, float, float, float]) -> bool:
    def side(px, py, qx, qy, rx, ry):
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)
    d1 = side(first[0], first[1], first[2], first[3], second[0], second[1])
    d2 = side(first[0], first[1], first[2], first[3], second[2], second[3])
    d3 = side(second[0], second[1], second[2], second[3], first[0], first[1])
    d4 = side(second[0], second[1], second[2], second[3], first[2], first[3])
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


class _Grid:
    """Uniform grid for neighbourhood queries.

    The layout asks "what is near this candidate?" thousands of times; comparing
    every caption with every other one is what made a dense view slow.
    """

    def __init__(self, cell: float) -> None:
        self.cell = max(8.0, float(cell))
        self.buckets: Dict[Tuple[int, int], Dict[Any, Any]] = {}

    def _keys(self, rect: Tuple[float, float, float, float], padding: float = 0.0):
        x0 = int((rect[0] - padding) // self.cell)
        y0 = int((rect[1] - padding) // self.cell)
        x1 = int((rect[0] + rect[2] + padding) // self.cell)
        y1 = int((rect[1] + rect[3] + padding) // self.cell)
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                yield gx, gy

    def put(self, key: Any, rect: Tuple[float, float, float, float], value: Any = None) -> None:
        for cell in self._keys(rect):
            self.buckets.setdefault(cell, {})[key] = (rect, value)

    def remove(self, key: Any) -> None:
        for bucket in self.buckets.values():
            bucket.pop(key, None)

    def near(self, rect: Tuple[float, float, float, float], padding: float = 0.0,
             exclude: Any = None) -> List[Any]:
        found: Dict[Any, Any] = {}
        for cell in self._keys(rect, padding):
            for key, payload in self.buckets.get(cell, {}).items():
                if key != exclude:
                    found[key] = payload
        return list(found.values())


def candidate_rects(
    anchor: Tuple[float, float],
    width: float,
    height: float,
    radius_px: float,
    gap: float,
    outline: Optional[Sequence[Tuple[float, float]]] = None,
    reach: Sequence[float] = _LABEL_REACH,
) -> Iterator[Tuple[float, float, float, float]]:
    """Every place a caption for one feature may be tried, best guess first."""
    ax, ay = float(anchor[0]), float(anchor[1])
    for reach in reach:
        for angle in _LABEL_ANGLES:
            radians = math.radians(angle)
            dx, dy = math.cos(radians), math.sin(radians)
            rim = outline_exit_distance((ax, ay), angle, outline or (), radius_px)
            distance = (rim if rim is not None else radius_px) + gap * (1.0 + reach)
            yield (ax + distance * dx - (width if dx < -0.1 else width / 2.0 if abs(dx) <= 0.1 else 0.0),
                   ay + distance * dy - (height if dy < -0.1 else height / 2.0 if abs(dy) <= 0.1 else 0.0),
                   float(width), float(height))


def score_candidate(rect: Tuple[float, float, float, float],
                    line: Tuple[float, float, float, float],
                    others: Sequence[Tuple[float, float, float, float]],
                    markers: Sequence[Tuple[float, float, float, float]],
                    gap: float,
                    conflicts: Optional[int] = None) -> float:
    """Lower is better: conflict first, then a short leader, then clearance.

    The clearance reward is capped, so an empty corner cannot pull a caption
    away from the feature it names just because nothing else is there.
    """
    if conflicts is None:
        conflicts = sum(_rects_intersect(rect, other, gap * 0.65) for other in others)
        conflicts += sum(_rects_intersect(rect, marker, gap * 0.3) for marker in markers)
        conflicts += sum(_segment_hits_rect(line, other, 1.0) for other in others)
        conflicts += sum(_segment_hits_rect(line, marker) for marker in markers)
    length = math.hypot(line[2] - line[0], line[3] - line[1])
    clearance = min((_rect_distance(rect, other) for other in list(others) + list(markers)),
                    default=gap * 3.0)
    return conflicts * 1e6 + 0.45 * length - 0.8 * min(clearance, gap * 3.0)


def _rect_distance(a: Tuple[float, float, float, float],
                   b: Tuple[float, float, float, float]) -> float:
    dx = max(b[0] - (a[0] + a[2]), a[0] - (b[0] + b[2]), 0.0)
    dy = max(b[1] - (a[1] + a[3]), a[1] - (b[1] + b[3]), 0.0)
    return math.hypot(dx, dy)


def leader_line(anchor: Tuple[float, float],
                rect: Tuple[float, float, float, float]) -> Tuple[float, float, float, float]:
    """Shortest segment from the anchor to the caption box's edge."""
    ax, ay = anchor
    left, top, width, height = rect
    cx = min(max(ax, left), left + width)
    cy = min(max(ay, top), top + height)
    return (ax, ay, cx, cy)


def _line_rect(line: Tuple[float, float, float, float]) -> Tuple[float, float, float, float]:
    x = min(line[0], line[2])
    y = min(line[1], line[3])
    return (x, y, abs(line[2] - line[0]), abs(line[3] - line[1]))


def arrange_labels(
    wanted: Sequence[Dict[str, Any]],
    width: float,
    height: float,
    gap: float = 8.0,
    *,
    max_labels: Optional[int] = None,
) -> Tuple[Dict[Any, Any], Dict[Any, Any], Set[Any]]:
    """Lay captions out so that none of them collide, and say which ones fit.

    Each request is a mapping with ``key``, ``anchor`` (x, y), ``radius_px``,
    ``width``, ``height`` and optionally ``outline``, ``priority`` (lower goes
    first) and ``important``.  Returns ``(placements, leaders, omitted)``.

    Two tiers, and the order between them is the point.  A landmark is placed
    against an **empty** picture: while it is being arranged, the only markers
    that obstruct it are the other landmarks', because a single unnamed dot must
    not be able to veto the one caption a reader is looking for.  Ordinary
    captions are arranged afterwards, and a dot that ends up underneath a
    landmark's text or leader is dropped along with its label -- the landmark has
    already claimed that space, so the dot is what gives way.

    Within each tier the placement is greedy in priority order, then reflowed
    until nothing moves, and an important caption that finds no compact position
    may search further out before it is given up on.
    """
    ordered = sorted(wanted, key=lambda request: request.get("priority", 0))
    if max_labels is None:
        pool = list(ordered)
        reserve = list(ordered)
    else:
        pool = list(ordered)[: max(0, int(max_labels))]
        reserve = list(ordered)

    text: _Grid = _Grid(max(32.0, gap * 4.0))
    lines: _Grid = _Grid(max(32.0, gap * 4.0))
    marks: _Grid = _Grid(max(32.0, gap * 4.0))
    placements: Dict[Any, Any] = {}
    leaders: Dict[Any, Any] = {}
    registered: Dict[Any, Any] = {}          # markers currently acting as obstacles
    every_marker: Dict[Any, Any] = {}        # every marker, obstacle or not
    for request in pool:
        radius = marker_radius_px(request["radius_px"], request["height"])
        ax, ay = request["anchor"]
        every_marker[request["key"]] = (ax - radius, ay - radius, 2 * radius, 2 * radius)

    def register(key: Any) -> None:
        if key not in registered:
            registered[key] = every_marker[key]
            marks.put(key, every_marker[key], every_marker[key])

    def place(request: Dict[str, Any], rect) -> None:
        key = request["key"]
        line = leader_line(request["anchor"], rect)
        placements[key] = rect
        leaders[key] = line
        text.put(key, rect, rect)
        lines.put(key, _line_rect(line), line)

    def options(request: Dict[str, Any], extended: bool = False):
        """Scored candidates: conflict-free ones only, cheapest first."""
        reach = _LABEL_REACH_EXTENDED if extended else _LABEL_REACH
        for rect in candidate_rects(request["anchor"], request["width"], request["height"],
                                    request["radius_px"], gap, request.get("outline"), reach):
            x, y, box_w, box_h = rect
            if x < gap or y < gap or x + box_w > width - gap or y + box_h > height - gap:
                continue
            line = leader_line(request["anchor"], rect)
            lx, ly, lw, lh = _line_rect(line)
            left, top = min(x, lx), min(y, ly)
            neighbourhood = (left, top, max(x + box_w, lx + lw) - left,
                             max(y + box_h, ly + lh) - top)
            others = [item[0] for item in text.near(neighbourhood, gap * 3.0, request["key"])]
            other_lines = [item[1] for item in lines.near(neighbourhood, gap * 3.0, request["key"])]
            own = registered.get(request["key"])
            other_marks = [item[1] for item in marks.near(neighbourhood, gap * 3.0, request["key"])
                           if item[1] is not own]
            if any(_rects_intersect(rect, other, gap * 0.65) for other in others):
                continue
            if any(_rects_intersect(rect, marker, gap * 0.3) for marker in other_marks):
                continue
            if any(_segment_hits_rect(line, other, 1.0) for other in others):
                continue
            if any(_segment_hits_rect(other, rect, 1.0) or _segments_cross(line, other)
                   for other in other_lines):
                continue
            if any(_segment_hits_rect(line, marker) for marker in other_marks):
                continue
            yield (score_candidate(rect, line, others, other_marks, gap, conflicts=0), rect, line)

    def best(request: Dict[str, Any], extended: bool = False):
        found = list(options(request, extended))
        if found:
            return min(found, key=lambda item: item[0])
        # A landmark may look further out, once, after the compact search fails.
        if request.get("important") and not extended:
            return best(request, extended=True)
        return None

    def arrange_group(group: Sequence[Dict[str, Any]]) -> None:
        for _ in range(3):                   # placement, then at most two reflows
            moved = False
            for request in group:
                previous = placements.get(request["key"])
                found = best(request)
                if found is not None:
                    place(request, found[1])
                    moved = moved or previous != placements[request["key"]]
            if not moved:
                break

    def center_conflicts(marker) -> bool:
        """Is this centre already covered by a placed caption or its leader?"""
        # The grids hand back (rect, payload) pairs, so the box is item[0] and
        # a leader is item[1] -- reading the pair itself as a rectangle is how
        # this compared a float with a tuple.
        return (any(_rects_intersect(marker, item[0], gap * 0.3)
                    for item in text.near(marker, gap * 0.3))
                or any(_segment_hits_rect(item[1], marker)
                       for item in lines.near(marker, gap * 0.3)))

    fixed_keys = {request["key"] for request in pool if request.get("fixed")}
    protected = [request for request in pool
                 if request.get("important") or request.get("force")
                 or request["key"] in fixed_keys]
    for request in protected:
        register(request["key"])
        if request.get("fixed"):
            x, y = request["fixed"]
            place(request, (float(x), float(y), request["width"], request["height"]))
    arrange_group([request for request in protected if not request.get("fixed")])

    ordinary = []
    for request in pool:
        if request["key"] in {item["key"] for item in protected}:
            continue
        if center_conflicts(every_marker[request["key"]]):
            continue                          # the landmark got there first
        register(request["key"])
        ordinary.append(request)
    arrange_group(ordinary)

    if max_labels is not None:
        # Only the centres of captions that were actually placed obstruct the
        # refill; the reserve's own markers would forbid the space being filled.
        for key in [key for key in list(registered) if key not in placements]:
            marks.remove(key)
            registered.pop(key, None)
        for request in reserve:
            if len(placements) >= max_labels:
                break
            if request["key"] in placements:
                continue
            found = best(request)
            if found is not None:
                place(request, found[1])

    omitted = {request["key"] for request in reserve if request["key"] not in placements}
    return placements, leaders, omitted
