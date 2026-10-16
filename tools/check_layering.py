#!/usr/bin/env python3
"""Enforce the architectural rules that can be checked mechanically.

Run from the repository root::

    python tools/check_layering.py

Rules checked
-------------
1. **Kernel-pool ownership** — only ``kernels.py`` may call
   ``spice.furnsh`` / ``spice.kclear`` / ``spice.bodvrd``.  The kernel pool is a
   process-global resource; a second owner would make load order and constants
   unpredictable.
2. **No upward imports** — a module may only import from the layer below it
   (see ``docs/02-架构设计.md`` §1).  This catches the classic mistake of
   ``frames`` importing ``moon``.
3. **No presentation in the compute layers** — the L0–L2 modules may not import
   ``matplotlib`` and may not contain bare ``print`` calls.  Charts belong in
   ``plotting``, text in ``reporting``, and ``kernels.init_kernels(verbose=)``
   is the single, explicitly opted-into exception.
4. **Optional-dependency discipline** — ``timezonefinder`` and ``matplotlib``
   may only be imported inside ``timeutil`` / ``plotting``.

Exit code is 0 when everything passes, 1 otherwise.
"""

from __future__ import annotations

import ast
from typing import Dict, List, Set, Tuple

from _bootstrap import ROOT  # noqa: E402

PACKAGE = ROOT / "src" / "moonlib_s"

#: Layer index per module; a module may import any module with a *lower* index,
#: plus anything in the same "peer" set.
LAYERS: Dict[str, int] = {
    # L0
    "errors": 0,
    "config": 0,
    "numerics": 0,
    "models": 0,
    "kernels": 0,
    "timeutil": 0,
    "observer": 0,
    # L1
    "frames": 1,
    "illumination": 1,
    "sun": 1,
    "moon": 1,
    "riseset": 1,
    "terrain": 1,
    # L2
    "moonview": 2,
    "features": 2,
    "annotations": 2,
    "instrument": 2,
    "phases": 2,
    "crescent": 2,
    "libration": 2,
    "analemma": 2,
    # L3
    "service": 3,
    "cli": 3,
    # L4
    "reporting": 4,
    "plotting": 4,
    # Output formats: an animation is a sequence of frames written to a file,
    # and an HDR gain map is a file too -- both are presentation, not physics.
    "anim": 4,
    "encode": 4,
    "hdr": 4,
}

#: Peer imports that are allowed in addition to the strictly-lower-layer rule.
PEER_ALLOWED: Set[Tuple[str, str]] = {
    ("moon", "phases"),        # moon_report needs the phase name and moon age
    ("riseset", "kernels"),
    ("illumination", "observer"),
    ("phases", "illumination"),
    ("crescent", "phases"),
    ("cli", "reporting"),
    ("cli", "service"),
    ("cli", "plotting"),
    ("service", "plotting"),   # sky_chart_png lives here
}

KERNEL_POOL_FUNCTIONS = {"furnsh", "kclear", "bodvrd"}
POOL_OWNER = "kernels"

OPTIONAL_DEPENDENCIES = {
    "timezonefinder": {"timeutil"},
    "matplotlib": {"plotting", "_mpl"},
    # Pillow reads a *compressed* colour TIFF and writes the two JPEGs of an HDR
    # gain map; both uses are opt-in and both fail with a MoonlibError naming
    # the missing package.
    "PIL": {"terrain", "hdr"},
    # The MP4 encoder.  Only encode.py may look for it, so "there is no ffmpeg"
    # surfaces as one clear message instead of a crash somewhere else.
    "imageio_ffmpeg": {"encode"},
}

COMPUTE_LAYERS_MAX_INDEX = 2


def _imports(tree: ast.AST) -> List[str]:
    names: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.module:
                names.append(node.module)
            elif node.level:
                names.extend(alias.name for alias in node.names)
            elif node.module:
                names.append(node.module)
    return names


def _spice_calls(tree: ast.AST) -> List[str]:
    called: List[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "spice"
        ):
            called.append(node.func.attr)
    return called


def check() -> List[str]:
    problems: List[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE)
        if relative.name == "__init__.py" or relative.name == "__main__.py":
            continue
        if relative.parts[0] == "plotting":
            name = f"plotting.{relative.stem}"
            layer_key = "plotting"
        else:
            name = relative.stem
            layer_key = name

        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        own_layer = LAYERS.get(layer_key)

        # -- rule 1: kernel pool ownership ---------------------------------
        if name != POOL_OWNER:
            for function in _spice_calls(tree):
                if function in KERNEL_POOL_FUNCTIONS:
                    problems.append(
                        f"{relative}: calls spice.{function}(); only "
                        f"{POOL_OWNER}.py may touch the kernel pool"
                    )

        # -- rule 2: no upward imports -------------------------------------
        for imported in _imports(tree):
            target = imported.split(".")[0]
            if target not in LAYERS:
                continue
            if target == layer_key:
                continue
            target_layer = LAYERS[target]
            if own_layer is None:
                continue
            if target_layer > own_layer and (layer_key, target) not in PEER_ALLOWED:
                problems.append(
                    f"{relative}: imports {target!r} (layer {target_layer}) from "
                    f"layer {own_layer} — upward dependency"
                )

        # -- rule 3: no presentation in compute layers ---------------------
        if own_layer is not None and own_layer <= COMPUTE_LAYERS_MAX_INDEX:
            for imported in _imports(tree):
                if imported.split(".")[0] in {"matplotlib", "plotting", "reporting"}:
                    problems.append(
                        f"{relative}: compute layer imports {imported!r}"
                    )
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "print"
                ):
                    if not (name == "kernels" and node.lineno >= 250):
                        problems.append(
                            f"{relative}:{node.lineno}: print() in a compute module"
                        )

        # -- rule 4: optional dependency discipline ------------------------
        for imported in _imports(tree):
            root = imported.split(".")[0]
            allowed = OPTIONAL_DEPENDENCIES.get(root)
            if allowed is None:
                continue
            if name in allowed or layer_key in allowed:
                continue
            problems.append(
                f"{relative}: imports optional dependency {root!r}; "
                f"allowed only in {sorted(allowed)}"
            )

    return problems


def main() -> int:
    problems = check()
    if not problems:
        print("layering check: OK")
        return 0
    print(f"layering check: {len(problems)} problem(s)\n")
    for problem in problems:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
