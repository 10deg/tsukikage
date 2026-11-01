# -*- mode: python ; coding: utf-8 -*-
"""One-file PyInstaller build for the browser-based Tsukikage service.

Run from the repository root (or use packaging/build_windows.bat)::

    python -m PyInstaller --noconfirm --clean \
        --distpath out/windows-service/dist \
        --workpath out/windows-service/build/pyinstaller \
        packaging/tsukikage.spec

The seven recent-mode SPICE kernels are *not* in the exe.  They are required
attachments in ``kernels/`` next to ``Tsukikage.exe``, together with the user's
DEM/colour rasters and anything the built-in DEM downloader fetches.  The exe
fails with an explicit filename list if the attachment directory is incomplete.
Qt/PySide6, timezonefinder, imageio-ffmpeg and the sample photos are excluded.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - injected by PyInstaller
WEB = ROOT / "web"
PACKAGING = ROOT / "packaging"
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

BUILD_DIR = Path(os.environ.get("TSUKIKAGE_BUILD_DIR", ROOT / "out" / "windows-service"))
VERSION_FILE = ROOT / "src" / "moonlib" / "_version.py"
VERSION = "1.6.4"
try:
    match = re.search(r'__version__\s*=\s*"([^"]+)"', VERSION_FILE.read_text(encoding="utf-8"))
    if match:
        VERSION = match.group(1)
except OSError:
    pass

datas = [
    (str(WEB / "server.py"), "web"),
    (str(WEB / "map_server.py"), "web"),
    (str(WEB / "static"), "web/static"),
    (str(WEB / "map"), "web/map"),
    (str(WEB / "settings" / "default.conf"), "web/settings"),
]

try:
    datas += collect_data_files("moonlib")
except Exception as error:  # pragma: no cover - build environment
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
except Exception as error:
    print("warning: moonlib submodules not collected: %s" % error)

binaries = []
try:
    binaries += collect_dynamic_libs("spiceypy")
except Exception:
    pass

# No Qt, no charts, no timezone database, no sample images.  Pillow's AVIF and
# WebP plugins are not used by the service (PNG/JPEG/TIFF only) and are among
# the larger optional plugins.
excludes = [
    "PySide6",
    "shiboken6",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "matplotlib",
    "timezonefinder",
    "h3",
    "imageio_ffmpeg",
    "scipy",
    "pandas",
    "IPython",
    "jupyter",
    "notebook",
    "pytest",
    "tkinter",
    "PIL.AvifImagePlugin",
    "PIL._avif",
    "PIL.WebPImagePlugin",
    "PIL._webp",
    "numpy.f2py",
    "numpy.distutils",
]

version_file = None
icon = str(PACKAGING / "tsukikage.ico")
if sys.platform == "win32":
    try:
        from PyInstaller.utils.win32.versioninfo import (
            FixedFileInfo,
            StringFileInfo,
            StringStruct,
            StringTable,
            VarFileInfo,
            VarStruct,
            VSVersionInfo,
        )

        numbers = [int(part) for part in re.findall(r"\d+", VERSION)][:4]
        while len(numbers) < 4:
            numbers.append(0)
        version_file = BUILD_DIR / "build-info" / "version_info.txt"
        version_file.parent.mkdir(parents=True, exist_ok=True)
        version_file.write_text(
            str(
                VSVersionInfo(
                    ffi=FixedFileInfo(
                        filevers=tuple(numbers),
                        prodvers=tuple(numbers),
                        mask=0x3F,
                        flags=0x0,
                        OS=0x40004,
                        fileType=0x1,
                        subtype=0x0,
                        date=(0, 0),
                    ),
                    kids=[
                        StringFileInfo(
                            [
                                StringTable(
                                    "080404b0",
                                    [
                                        StringStruct("CompanyName", "Tsukikage"),
                                        StringStruct("FileDescription", "Tsukikage 月面模拟服务"),
                                        StringStruct("FileVersion", VERSION),
                                        StringStruct("InternalName", "Tsukikage"),
                                        StringStruct("OriginalFilename", "Tsukikage.exe"),
                                        StringStruct("ProductName", "Tsukikage"),
                                        StringStruct("ProductVersion", VERSION),
                                    ],
                                )
                            ]
                        ),
                        VarFileInfo([VarStruct("Translation", [0x0804, 1200])]),
                    ],
                )
            ),
            encoding="utf-8",
        )
    except Exception as error:  # pragma: no cover - non-Windows or old PyInstaller
        print("warning: version resource not written: %s" % error)
        version_file = None
elif sys.platform == "darwin":
    icon = None

a = Analysis(
    [str(PACKAGING / "entry.py")],
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
    a.binaries,
    a.datas,
    [],
    name="Tsukikage",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
    version=str(version_file) if version_file is not None else None,
)

# The one-file bundle must never contain a raster or a SPICE kernel: both are
# attachments in the external kernels/ directory.
for _dest, source in datas:
    suffix = Path(source).suffix.lower()
    if suffix in {".tif", ".tiff", ".exr"}:
        raise SystemExit("lunar raster must not be bundled: %s" % source)
