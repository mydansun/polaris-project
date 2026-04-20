"""pytest config — make ``scripts/`` importable so tests can do
``from lib.X import Y`` without scripts/ being a real installed package."""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
