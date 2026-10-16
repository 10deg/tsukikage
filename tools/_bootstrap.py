"""Shared plumbing for the developer tools: where the repository is, and the path.

Eleven of the twelve tools open with the same four lines -- find the repository
root from ``__file__``, insert ``src/`` (or ``web/``) on ``sys.path``, then import
:mod:`moonlib_s`.  Two names were in use for the same value (``ROOT`` and
``_REPO``) and two idioms for computing it (``parent.parent`` and ``parents[1]``),
and one tool skipped the constant altogether and inlined the whole path.

The tools are not part of the installed package, so this module is private to
them (leading underscore) and is imported by path, the same way the tools
themselves are run::

    from _bootstrap import ROOT, use_src

    moonlib_s = use_src()

``use_src`` returns the library so a tool can bind it to whatever name it likes.
It raises ``SystemExit`` with the interpreter to use when ``src/`` is there but
the library still cannot be imported -- the case a reader hits by running a tool
with the system Python instead of the project's.
"""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path
from typing import Any

#: The repository root: the directory holding ``src/``, ``web/``, ``kernels/``.
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WEB = ROOT / "web"


def interpreter_hint(script: Any = None) -> str:
    """The command line to suggest when the import fails."""
    name = Path(script or sys.argv[0]).name
    return f"    {ROOT}/.venv/bin/python {'tools/' if name else ''}{name}"


def use_src(script: Any = None) -> Any:
    """Put the checkout's ``src/`` on ``sys.path`` and return :mod:`moonlib_s`."""
    if SRC.is_dir():
        sys.path.insert(0, str(SRC))
    try:
        import moonlib_s
    except ImportError as error:  # pragma: no cover - environment dependent
        raise SystemExit(
            f"Cannot import moonlib_s ({error}).\n"
            f"Run this with the project interpreter:\n"
            f"{interpreter_hint(script)}"
        ) from error
    return moonlib_s


def use_web(script: Any = None) -> None:
    """Put the checkout's ``web/`` on ``sys.path`` so ``import server`` works."""
    if WEB.is_dir():
        sys.path.insert(0, str(WEB))


# ---------------------------------------------------------------------------
# shared catalogue conventions
# ---------------------------------------------------------------------------
#: The column order of every catalogue under ``src/moonlib_s/data``.
#:
#: Written out five times before this existed -- twice in ``fetch_nomenclature``
#: (the writer and its provenance block) and once each in ``fetch_lpnd_names``
#: and ``build_landform_db``, plus a copy in a test.  The readers in
#: :mod:`moonlib_s.features` and :mod:`moonlib_s.annotations` look the same keys up
#: by name, so a rename in one writer would have quietly emptied a column rather
#: than failing.
CATALOGUE_COLUMNS = ("English Name", "名称", "Lat", "Lon", "Diameter", "Type", "Note")


def normalise_lon(value: Any) -> float:
    """A longitude in -180..180.

    The IAU and USGS exports run 0..360 east; everything else in the project
    uses -180..180, and the conversion is idempotent for input that is already
    in range.  Two byte-identical copies of this lived in the catalogue builders.
    """
    return ((float(value) + 180.0) % 360.0) - 180.0


def fold(text: Any, *, whitespace: str = "collapse") -> str:
    """Comparison key for a proper name: accents and case do not count.

    ``whitespace`` says what spacing means, and it is a parameter rather than a
    detail of the implementation because the callers genuinely disagree.  There
    were three copies of this function in the tools, differing only here, and
    none of them said so:

    ``"drop"``
        Spacing is not part of the name.  The landform database uses this to
        find the IAU's own collisions -- "Mee S" and "Mees" are the same key,
        which is the whole point of the pass.
    ``"collapse"``
        A run of whitespace is one space and the ends are trimmed, so
        ``"  Mee  S "`` and ``"Mee S"`` are one name written two ways.  The
        database's spelling pass uses this: it has to tell "Mee S" from "Mees",
        so it cannot drop the space, but it should not care about the run.
    ``"keep"``
        Whitespace is left exactly as written.  The LPND matcher uses this,
        because it compares against catalogue names that were already
        normalised when the catalogue was built.
    """
    if whitespace not in ("drop", "collapse", "keep"):
        raise ValueError(f"whitespace must be 'drop', 'collapse' or 'keep', not {whitespace!r}")
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    kept = "".join(
        character for character in decomposed
        if not unicodedata.combining(character)
    )
    if whitespace == "drop":
        return "".join(c for c in kept if not c.isspace()).casefold()
    if whitespace == "collapse":
        return " ".join(kept.split()).casefold()
    return kept.casefold()


__all__ = [
    "ROOT", "SRC", "WEB",
    "use_src", "use_web", "interpreter_hint",
    "CATALOGUE_COLUMNS", "normalise_lon", "fold",
]
