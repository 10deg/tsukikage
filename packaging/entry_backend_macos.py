"""Entry point for the macOS app's bundled Python backend.

This is a plain console executable (not a .app) launched by the Swift shell.
It never opens a browser or an AppleScript dialog; the shell owns the window.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["TSUKIKAGE_FORCE_CONSOLE"] = "1"

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if (ROOT / "service").is_dir():
    sys.path.insert(0, str(ROOT))

from service.app import main  # noqa: E402

raise SystemExit(main())
