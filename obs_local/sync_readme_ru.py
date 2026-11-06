#!/usr/bin/env python3
"""Validate that the English and Russian OBS README files stay structurally aligned.

Run:
    python -m obs_local.sync_readme_ru --check
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
    fence_marker: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if fence_marker is not None:
            marker_length = len(stripped) - len(
                stripped.lstrip(fence_marker[0])
            )
            if marker_length >= len(fence_marker) and not stripped[
                marker_length:
            ].strip():
                fence_marker = None
            continue
        if stripped.startswith(("```", "~~~")):
            marker = stripped[0]
            marker_length = len(stripped) - len(stripped.lstrip(marker))
            fence_marker = marker * marker_length
            continue
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
        print("✅ README files are structurally aligned.")
        return 0

    print("⚠️ README files are not aligned. Update the Russian version when the source README changes.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Check source and Russian README alignment.")
    parser.add_argument("--check", action="store_true", help="validate heading structure")
    args = parser.parse_args()

    if args.check:
        return check_sync()

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
