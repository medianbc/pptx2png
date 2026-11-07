"""Check for and install updates to the local OBS application."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


PROJECT_DIR = Path(__file__).resolve().parent.parent
REPOSITORY = "medianbc/pptx2png"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/commits"
ARCHIVE_URL = f"https://codeload.github.com/{REPOSITORY}/tar.gz/refs/heads"
VERSION_FILE = ".pptx2png-version.json"
MANIFEST_FILE = ".pptx2png-files.json"
ARCHIVE_MARKER = ".pptx2png-archive-install"
REQUEST_TIMEOUT = 8
PRESERVED_PATHS = {
    ".git",
    ".venv",
    "config.ini",
    "obs_local/config.ini",
    "obs_local/settings.ini",
    "obs_local/song_title_aliases.json",
    "obs_png",
    "prepare_obs_songs.sh",
    "song_assets",
    "start_obs_local.sh",
}


class UpdateError(RuntimeError):
    """Raised when an update cannot be checked or installed safely."""


@dataclass(frozen=True)
class UpdateInfo:
    branch: str
    local_revision: str | None
    remote_revision: str
    available: bool
    is_git_checkout: bool


def _run_git(*arguments: str, check: bool = True) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_DIR), *arguments],
            check=check,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", None) or str(error)
        raise UpdateError(f"Ошибка Git: {detail.strip()}") from error
    return result.stdout.strip()


def _is_git_checkout() -> bool:
    return (PROJECT_DIR / ".git").exists()


def _installed_version() -> tuple[str, str | None, bool]:
    if _is_git_checkout():
        branch = _run_git("branch", "--show-current")
        if not branch:
            raise UpdateError(
                "Нельзя автоматически обновить Git-копию с detached HEAD."
            )
        _run_git("check-ref-format", "--branch", branch)
        origin = _run_git("remote", "get-url", "origin")
        normalized_origin = origin.removesuffix(".git").rstrip("/")
        if normalized_origin not in {
            f"https://github.com/{REPOSITORY}",
            f"git@github.com:{REPOSITORY}",
            f"ssh://git@github.com/{REPOSITORY}",
        }:
            raise UpdateError("Git origin не указывает на официальный репозиторий.")
        return branch, _run_git("rev-parse", "HEAD"), True

    version_path = PROJECT_DIR / VERSION_FILE
    if version_path.is_file():
        try:
            version = json.loads(version_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            raise UpdateError(
                f"Файл версии повреждён: {version_path}. "
                "Обновление не выполнялось."
            )
        branch = version.get("branch") if isinstance(version, dict) else None
        revision = version.get("revision") if isinstance(version, dict) else None
        if (
            not isinstance(branch, str)
            or not branch
            or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch)
            or ".." in branch
            or branch.startswith(("-", "/"))
            or branch.endswith(("/", "."))
            or not isinstance(revision, str)
            or not re.fullmatch(r"[0-9a-fA-F]{40,64}", revision)
        ):
            raise UpdateError(
                f"Файл версии повреждён: {version_path}. "
                "Обновление не выполнялось."
            )
        return branch, revision, False
    if (PROJECT_DIR / ARCHIVE_MARKER).is_file():
        return "main", None, False
    raise UpdateError(
        "Не удалось определить установку проекта. "
        "Обновление доступно только для официальной Git- или архивной установки."
    )


def _fetch_remote_revision(branch: str) -> str:
    url = f"{API_URL}/{quote(branch, safe='')}"
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "pptx2png-obs-local-updater",
        },
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
        raise UpdateError(f"Не удалось проверить GitHub: {error}") from error
    revision = payload.get("sha") if isinstance(payload, dict) else None
    if not isinstance(revision, str) or not re.fullmatch(
        r"[0-9a-fA-F]{40,64}", revision
    ):
        raise UpdateError("GitHub вернул некорректный номер версии.")
    return revision


def check_for_updates() -> UpdateInfo:
    branch, local_revision, is_git_checkout = _installed_version()
    remote_revision = _fetch_remote_revision(branch)
    return UpdateInfo(
        branch=branch,
        local_revision=local_revision,
        remote_revision=remote_revision,
        available=local_revision != remote_revision,
        is_git_checkout=is_git_checkout,
    )


def install_update(info: UpdateInfo) -> None:
    try:
        if info.is_git_checkout:
            _update_git_checkout(info)
        else:
            _update_archive_install(info)
        _install_python_dependencies()
        if not info.is_git_checkout:
            _write_version(info.branch, info.remote_revision)
    except UpdateError:
        raise
    except OSError as error:
        raise UpdateError(f"Ошибка при установке обновления: {error}") from error


def _update_git_checkout(info: UpdateInfo) -> None:
    status = _run_git("status", "--porcelain", "--untracked-files=normal")
    stash_created = False
    if status:
        previous_stash = _run_git(
            "rev-parse", "--verify", "refs/stash", check=False
        )
        _run_git(
            "stash",
            "push",
            "--include-untracked",
            "-m",
            "pptx2png OBS update backup",
        )
        current_stash = _run_git(
            "rev-parse", "--verify", "refs/stash", check=False
        )
        stash_created = current_stash != previous_stash
        remaining_status = _run_git(
            "status", "--porcelain", "--untracked-files=normal"
        )
        if remaining_status:
            raise UpdateError(
                "Не удалось безопасно сохранить локальные изменения перед "
                "обновлением; рабочая копия не обновлена."
            )

    try:
        _run_git("fetch", "origin", info.branch)
        fetched_revision = _run_git("rev-parse", "FETCH_HEAD")
        if fetched_revision != info.remote_revision:
            raise UpdateError(
                "Ветка изменилась во время проверки. Повторите проверку "
                "обновлений."
            )
        ancestor = subprocess.run(
            [
                "git",
                "-C",
                str(PROJECT_DIR),
                "merge-base",
                "--is-ancestor",
                "HEAD",
                "FETCH_HEAD",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if ancestor.returncode != 0:
            raise UpdateError(
                "Локальная ветка содержит отдельные коммиты и не может быть "
                "обновлена автоматически без риска потери изменений."
            )
        _run_git("merge", "--ff-only", "FETCH_HEAD")
    except Exception:
        if stash_created:
            _restore_stash()
        raise

    if stash_created:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_DIR), "stash", "pop", "--index"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise UpdateError(
                "Исходники обновлены, но локальные изменения не удалось "
                "вернуть без конфликта. Резервная копия осталась в git stash; "
                "разрешите конфликт вручную. "
                f"{result.stderr.strip()}"
            )


def _restore_stash() -> None:
    result = subprocess.run(
        ["git", "-C", str(PROJECT_DIR), "stash", "pop", "--index"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise UpdateError(
            "Обновление остановлено; резервная копия локальных изменений "
            "осталась в git stash. Восстановите её вручную. "
            f"{result.stderr.strip()}"
        )


def _download_archive(branch: str, destination: Path) -> None:
    url = f"{ARCHIVE_URL}/{quote(branch, safe='')}"
    request = Request(url, headers={"User-Agent": "pptx2png-obs-local-updater"})
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            archive_data = response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise UpdateError(f"Не удалось скачать обновление с GitHub: {error}") from error

    try:
        with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz") as archive:
            members = archive.getmembers()
            if not members:
                raise UpdateError("GitHub вернул пустой архив обновления.")
            for member in members:
                parts = PurePosixPath(member.name).parts
                if len(parts) == 1 and member.isdir():
                    continue
                if (
                    len(parts) < 2
                    or member.issym()
                    or member.islnk()
                    or not (member.isdir() or member.isfile())
                ):
                    raise UpdateError("Архив содержит недопустимый путь или тип файла.")
                relative = Path(*parts[1:])
                if relative.is_absolute() or ".." in relative.parts:
                    raise UpdateError("Архив содержит путь за пределами проекта.")
                target = destination / relative
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise UpdateError("Не удалось прочитать файл из архива.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                os.chmod(target, member.mode & 0o755)
    except (tarfile.TarError, OSError) as error:
        raise UpdateError(f"Не удалось распаковать обновление: {error}") from error


def _update_archive_install(info: UpdateInfo) -> None:
    with tempfile.TemporaryDirectory(
        prefix=".pptx2png-update-", dir=PROJECT_DIR.parent
    ) as temp_dir:
        staging = Path(temp_dir) / "source"
        backup = Path(temp_dir) / "backup"
        staging.mkdir()
        backup.mkdir()
        _download_archive(info.branch, staging)

        new_files = _archive_source_files(staging)
        old_files = _read_installed_manifest()
        obsolete_files = old_files - new_files
        touched_files = new_files | obsolete_files
        _validate_update_targets(touched_files)
        for metadata_path in (PROJECT_DIR / MANIFEST_FILE, PROJECT_DIR / VERSION_FILE):
            if metadata_path.is_symlink() or (
                metadata_path.exists() and not metadata_path.is_file()
            ):
                raise UpdateError(
                    f"Обновление остановлено: некорректный файл "
                    f"метаданных ({metadata_path})."
                )
        backup_files = {
            path
            for path in touched_files | {MANIFEST_FILE}
            if (PROJECT_DIR / path).is_file()
        }
        changed_files: set[str] = set()
        try:
            for relative_name in backup_files:
                backup_path = backup / relative_name
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(PROJECT_DIR / relative_name, backup_path)

            for relative_name in sorted(new_files):
                source = staging / relative_name
                target = PROJECT_DIR / relative_name
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
                changed_files.add(relative_name)

            for relative_name in obsolete_files:
                target = PROJECT_DIR / relative_name
                if target.is_file():
                    target.unlink()
                    changed_files.add(relative_name)

            _write_json_atomically(
                PROJECT_DIR / MANIFEST_FILE,
                sorted(new_files),
            )
            changed_files.add(MANIFEST_FILE)
        except (OSError, UpdateError) as error:
            _rollback_archive_update(
                changed_files,
                backup_files,
                backup,
            )
            if isinstance(error, UpdateError):
                raise
            raise UpdateError(
                "Не удалось полностью установить архивное обновление; "
                "исходные файлы восстановлены."
            ) from error

    _write_version(info.branch, info.remote_revision)


def _archive_source_files(source_root: Path) -> set[str]:
    result: set[str] = set()
    for source in source_root.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(source_root)
        relative_name = relative.as_posix()
        if relative_name in PRESERVED_PATHS or relative_name in {
            VERSION_FILE,
            MANIFEST_FILE,
            ARCHIVE_MARKER,
        }:
            continue
        if any(
            part in {".git", ".venv", "song_assets", "obs_png"}
            for part in relative.parts
        ):
            continue
        result.add(relative_name)
    if not result:
        raise UpdateError("Архив не содержит файлов проекта.")
    return result


def _is_safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(value)
        and not path.is_absolute()
        and ".." not in path.parts
        and value not in PRESERVED_PATHS
        and value not in {VERSION_FILE, MANIFEST_FILE, ARCHIVE_MARKER}
    )


def _read_installed_manifest() -> set[str]:
    manifest_path = PROJECT_DIR / MANIFEST_FILE
    if not manifest_path.exists():
        return set()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise UpdateError(
            f"Файл списка установленных файлов повреждён: {manifest_path}"
        ) from error
    if not isinstance(manifest, list) or not all(
        isinstance(path, str) and _is_safe_relative_path(path)
        for path in manifest
    ):
        raise UpdateError(
            f"Файл списка установленных файлов повреждён: {manifest_path}"
        )
    return set(manifest)


def _validate_update_targets(relative_paths: set[str]) -> None:
    for relative_name in relative_paths:
        if not _is_safe_relative_path(relative_name):
            raise UpdateError("Недопустимый путь в архиве или списке файлов.")
        target = PROJECT_DIR / relative_name
        current = PROJECT_DIR
        for part in PurePosixPath(relative_name).parts:
            current = current / part
            if current.is_symlink():
                raise UpdateError(
                    f"Обновление остановлено: путь содержит символическую "
                    f"ссылку ({current})."
                )
        if target.exists() and not target.is_file():
            raise UpdateError(
                f"Обновление остановлено: вместо файла найден каталог "
                f"({target})."
            )


def _rollback_archive_update(
    touched_files: set[str],
    backup_files: set[str],
    backup_root: Path,
) -> None:
    failures: list[str] = []
    for relative_name in touched_files:
        target = PROJECT_DIR / relative_name
        backup = backup_root / relative_name
        try:
            if relative_name in backup_files and backup.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, target)
            elif target.is_file():
                target.unlink()
        except OSError as error:
            failures.append(f"{relative_name}: {error}")
    if failures:
        raise UpdateError(
            "Не удалось полностью откатить архивное обновление: "
            + "; ".join(failures)
        )


def _write_version(branch: str, revision: str) -> None:
    _write_json_atomically(
        PROJECT_DIR / VERSION_FILE,
        {"branch": branch, "revision": revision},
    )


def _write_json_atomically(path: Path, payload: object) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary_path.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, path)
    except OSError as error:
        raise UpdateError(
            f"Не удалось атомарно сохранить файл {path}: {error}"
        ) from error


def _install_python_dependencies() -> None:
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "-e",
        str(PROJECT_DIR / "core"),
        "-r",
        str(PROJECT_DIR / "obs_local" / "requirements.txt"),
    ]
    try:
        subprocess.run(command, cwd=PROJECT_DIR, check=True)
        subprocess.run(
            [sys.executable, "-m", "pip", "check"],
            cwd=PROJECT_DIR,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise UpdateError(
            "Исходники обновлены, но не удалось установить или проверить "
            "Python-зависимости. Подробности выше; повторно запустите "
            "установщик после устранения ошибки."
        ) from error
