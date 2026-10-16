"""Shared scaffolding for the examples: bootstrap, repository root, output paths.

Every example used to carry its own copy of the same twenty lines -- a
``sys.path`` patch, an explanatory message, its own ``ROOT``, and a
``path.relative_to(ROOT)`` call that raised ``ValueError`` the moment someone
passed an ``--out`` outside the repository.  That is one behaviour written ten
times, and it drifted: examples 01-07 shared a byte-identical bootstrap while
08-10 had a second, shorter variant.

What lives here
---------------
``moonlib_s``
    Imported after ``src/`` has been added to ``sys.path``, so a checkout works
    without ``pip install -e .``.  Examples import it from here:
    ``from _cli import moonlib_s``.
``ROOT``
    The repository root -- the directory holding ``src/``, ``out/`` and
    ``.cache/``.
``shown(path)``
    A path for a human to read: relative to the repository when it is inside,
    absolute when it is not.  This is the fix for the ``ValueError`` above.
``add_output_argument(parser, kind)``
    The ``--out`` / ``--outdir`` option, whose default differs between the
    plotting examples and the simulation ones but whose help text did not.

This module is private to the examples (leading underscore) and is not part of
the installed package: the examples only ever call the public :mod:`moonlib_s`
API, and this file holds no lunar logic at all -- just the plumbing that lets a
script in a checkout find the library and describe where it wrote its output.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]

#: Re-exported for the examples: this is the module's whole purpose.
__all__ = ["ROOT", "moonlib_s", "shown", "add_output_argument"]


def shown(path) -> str:
    """*path* relative to the repository, or absolute when it is outside it.

    ``Path.relative_to`` raises rather than falling back, so presenting a path
    the user chose -- ``--out /tmp/frame.png`` -- used to abort the script after
    it had already done all the work.  Defined before the import below, which is
    the first thing that needs it.
    """
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(ROOT))
    except ValueError:
        return str(resolved)


# Running a script by path puts *its own* directory on sys.path, not the
# repository root, so the checkout's `src/` has to be added explicitly.
if (ROOT / "src" / "moonlib_s").is_dir():
    sys.path.insert(0, str(ROOT / "src"))

try:
    import moonlib_s
except ImportError as error:  # pragma: no cover - environment dependent
    raise SystemExit(
        f"Cannot import moonlib_s ({error}).\n"
        f"Run this script with the project interpreter, which has numpy, "
        f"spiceypy and matplotlib installed:\n"
        f"    {ROOT}/.venv/bin/python {shown(sys.argv[0])}"
    ) from error


def add_output_argument(parser: argparse.ArgumentParser, *, kind: str = "file",
                        default: Optional[str] = "out") -> None:
    """Add the example's output option: ``--out FILE`` or ``--outdir DIR``.

    The default is the relative path ``"out"``, not ``ROOT / "out"``.  The
    plotting examples settled on that first and the simulation ones did not, so
    running ``08_region_closeup.py`` from anywhere wrote into the checkout --
    leaving untracked pictures behind, and making the examples behave
    differently depending on which one you ran.  Relative to the working
    directory is what a reader expects from a demo.

    Pass ``default=None`` for an example that names its own file inside ``out/``
    and so has to be able to tell "the user chose a name" from "the user said
    nothing"; :func:`None` is passed through rather than being replaced by the
    usual string, which would make that distinction impossible.
    """
    name = "--outdir" if kind == "dir" else "--out"
    parser.add_argument(
        name,
        default=default,
        help="输出目录（默认 out/）" if kind == "dir" else "输出文件（默认 out/<名称>.png）",
    )
