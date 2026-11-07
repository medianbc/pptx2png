"""Helpers for safely updating local OBS configuration files."""

from __future__ import annotations

import configparser
import os
import re
import stat
import sys
import tempfile
from pathlib import Path


def get_settings_path(base_dir: Path) -> Path:
    """Initialize an independent OS configuration from the legacy settings once."""
    platform = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
    path = base_dir / f"settings.{platform}.ini"
    legacy = base_dir / "settings.ini"
    if path.exists() or not legacy.is_file():
        return path
    settings = configparser.ConfigParser(interpolation=None)
    settings.read(legacy, encoding="utf-8")
    log_dir = settings.get("Logging", "log_dir", fallback="").strip()
    updates = {}
    if not log_dir or log_dir == "/dev/shm/pptx2png_tasks/obs_local/logs":
        updates = {"Logging": {"log_dir": str(default_log_dir())}}
    update_ini_file(path, updates, template_path=legacy)
    return path


def default_log_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "pptx2png" / "obs_local"
    if sys.platform == "win32":
        local_data = Path(
            os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        )
        return local_data / "pptx2png" / "obs_local" / "logs"
    return Path("/dev/shm/pptx2png_tasks/obs_local/logs")


def update_ini_file(
    path: Path,
    updates: dict[str, dict[str, str]],
    file_mode: int | None = None,
    *,
    template_path: Path | None = None,
) -> None:
    source = path if path.exists() or template_path is None else template_path
    newline = (
        "\r\n"
        if source.exists() and "\r\n" in source.read_text(encoding="utf-8")
        else "\n"
    )
    lines = source.read_text(encoding="utf-8").splitlines() if source.exists() else []
    output: list[str] = []
    written = {section: set() for section in updates}
    section_name = None

    def append_missing(section: str) -> None:
        for key, value in updates[section].items():
            if key.lower() not in written[section]:
                output.append(f"{key} = {value}")
                written[section].add(key.lower())

    for line in lines:
        section_match = configparser.ConfigParser.SECTCRE.match(line)
        if section_match:
            if section_name in updates:
                append_missing(section_name)
            section_name = section_match.group("header").strip()
            output.append(line)
            continue

        if section_name in updates:
            option_match = re.match(r"^(\s*)([^#;\s][^=:]*?)\s*[=:]", line)
            if option_match:
                key = option_match.group(2).strip()
                replacement = next(
                    (
                        value
                        for option, value in updates[section_name].items()
                        if option.lower() == key.lower()
                    ),
                    None,
                )
                if replacement is not None:
                    output.append(f"{option_match.group(1)}{key} = {replacement}")
                    written[section_name].add(key.lower())
                    continue
        output.append(line)

    if section_name in updates:
        append_missing(section_name)
    for section, options in updates.items():
        if any(key.lower() not in written[section] for key in options):
            if output and output[-1].strip():
                output.append("")
            output.append(f"[{section}]")
            append_missing(section)

    path.parent.mkdir(parents=True, exist_ok=True)
    if file_mode is None and path.exists():
        file_mode = stat.S_IMODE(path.stat().st_mode)
    temporary_path = None
    try:
        updated_text = newline.join(output) + newline
        validation = configparser.ConfigParser(interpolation=None)
        validation.read_string(updated_text, source=str(path))
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(updated_text)
        if file_mode is not None:
            os.chmod(temporary_path, file_mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
