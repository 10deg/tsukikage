"""PyInstaller entry point for the browser service."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if (ROOT / "service").is_dir():
    sys.path.insert(0, str(ROOT))

from service.app import main  # noqa: E402

raise SystemExit(main())
