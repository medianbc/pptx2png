"""Compatibility wrapper for Yandex Disk client logic."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from yandex_disk import *  # noqa: F401,F403
