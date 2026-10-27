"""Tests for the scripts in scripts/: they import each other by module name."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
