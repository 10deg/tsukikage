# -*- mode: python ; coding: utf-8 -*-
"""Onedir Python backend for the native macOS app.

The Swift shell in ``macapp/`` launches the executable with a data directory, a
random port and a port file.  This spec deliberately produces no .app and no
browser UI: it is the same minimal service, with the bundled SPICE kernels and
without Qt/Chromium/timezonefinder/imageio-ffmpeg/sample photos.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - injected by PyInstaller
WEB = ROOT / "web"
KERNELS = ROOT / "kernels"
PACKAGING = ROOT / "packaging"
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from moonlib.config import BASE_KERNELS  # noqa: E402

datas = [
    (str(WEB / "server.py"), "web"),
    (str(WEB / "map_server.py"), "web"),
    (str(WEB / "static"), "web/static"),
    (str(WEB / "map"), "web/map"),
    (str(WEB / "settings" / "default.conf"), "web/settings"),
]
for name in tuple(BASE_KERNELS):
    source = KERNELS / name
    if not source.is_file():
        raise SystemExit("bundled SPICE kernel is missing: %s" % source)
    datas.append((str(source), "kernels"))
try:
    datas += collect_data_files("moonlib")
except Exception as error:  # pragma: no cover
    print("warning: moonlib data not collected: %s" % error)

hiddenimports = [
    "moonlib.plotting",
    "moonlib.online",
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.ImageFont",
    "PIL.ImageOps",
    "PIL.ImageFile",
    "PIL.TiffImagePlugin",
    "PIL.JpegImagePlugin",
    "PIL.PngImagePlugin",
]
try:
    hiddenimports += collect_submodules("moonlib")
except Exception as error:  # pragma: no cover
    print("warning: moonlib submodules not collected: %s" % error)

binaries = []
try:
    binaries += collect_dynamic_libs("spiceypy")
except Exception:
    pass

excludes = [
    "PySide6", "shiboken6", "PyQt5", "PyQt6", "PySide2",
    "matplotlib", "timezonefinder", "h3", "imageio_ffmpeg",
    "scipy", "pandas", "IPython", "jupyter", "notebook", "pytest", "tkinter",
    "PIL.AvifImagePlugin", "PIL._avif", "PIL.WebPImagePlugin", "PIL._webp",
    "numpy.f2py", "numpy.distutils",
]

a = Analysis(
    [str(PACKAGING / "entry_backend_macos.py")],
    pathex=[str(ROOT), str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TsukikageBackend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="TsukikageBackend",
)

for _dest, source in datas:
    suffix = Path(source).suffix.lower()
    if suffix in {".tif", ".tiff", ".exr"}:
        raise SystemExit("lunar raster must not be bundled: %s" % source)
