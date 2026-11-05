"""The plugin's tests import its source tree directly; nothing here needs OrcaSlicer or the BamDude backend."""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
