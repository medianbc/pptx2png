#!/usr/bin/env python3
"""Check that the English and Russian root README files stay structurally aligned.

Usage:
    python sync_readme_ru.py --check
"""

from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "README.md"
TARGET = ROOT / "README.RU.md"


def collect_headings(path: Path) -> dict[int, int]:
    if not path.exists():
        raise FileNotFoundError(f"Missing file: {path}")

    counts = {1: 0, 2: 0, 3: 0, 4: 0}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        level = len(stripped) - len(stripped.lstrip("#"))
        if 1 <= level <= 4:
            counts[level] += 1
    return counts


def check_sync() -> int:
    src_counts = collect_headings(SOURCE)
    ru_counts = collect_headings(TARGET)

    ok = src_counts == ru_counts
    print(f"README.md: {src_counts}")
    print(f"README.RU.md: {ru_counts}")

    if ok:
        print("✅ Root README files are structurally aligned.")
        return 0

    print("⚠️ Root README files are not aligned. Update the Russian version when the source README changes.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Check root README alignment.")
    parser.add_argument("--check", action="store_true", help="validate heading structure")
    args = parser.parse_args()

    if args.check:
        return check_sync()

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
