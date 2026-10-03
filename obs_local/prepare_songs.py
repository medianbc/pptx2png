"""Prepare song PNG folders for OBS from the meeting program."""

from __future__ import annotations

import argparse
import asyncio
import configparser
import json
import logging
import re
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from xml.etree import ElementTree

import aiohttp

from pptx2png_core.converter_engine import convert_all_pngs
from pptx2png_core.yandex_disk import YandexDiskClient, get_nearest_sunday


BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
PUBLIC_DOWNLOAD_API = "https://cloud-api.yandex.net/v1/disk/public/resources/download"
DOCX_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_ZIP_PNG_BYTES = 512 * 1024 * 1024
MAX_ZIP_PNG_FILES = 1000
MAX_PROGRAM_DOCX_BYTES = 50 * 1024 * 1024
MAX_YANDEX_SEARCH_DEPTH = 3
LOGGER = logging.getLogger(__name__)


@dataclass
class SongResult:
    title: str
    folder: Path
    source: str | None = None
    png_count: int = 0
    status: str = "not_found"
    error: str | None = None


def _resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_DIR / path


def _load_yandex_token() -> str:
    for secrets_path in (PROJECT_DIR / "config.ini", BASE_DIR / "config.ini"):
        if not secrets_path.is_file():
            continue
        secrets = configparser.ConfigParser(interpolation=None)
        secrets.read(secrets_path, encoding="utf-8")
        token = secrets.get("YandexDisk", "token", fallback="").strip()
        if token:
            return token
    return ""


def load_config() -> dict[str, Any]:
    settings = configparser.ConfigParser(interpolation=None)
    settings_path = BASE_DIR / "settings.ini"
    if not settings.read(settings_path, encoding="utf-8"):
        raise RuntimeError(f"Не найден файл настроек: {settings_path}")

    if not settings.has_section("OBS Local paths"):
        raise RuntimeError("В settings.ini отсутствует секция [OBS Local paths]")
    song_settings = settings["Song preparation"] if settings.has_section(
        "Song preparation"
    ) else {}
    obs_settings = settings["OBS Local paths"]
    root_path = _resolve_path(obs_settings.get("root_path", "").strip())
    if not str(obs_settings.get("root_path", "")).strip():
        raise RuntimeError("В settings.ini не задан [OBS Local paths] root_path")

    quality = obs_settings.get("quality", "standard").strip().lower()
    if quality not in {"standard", "2k", "4k"}:
        raise RuntimeError("quality должен быть standard, 2k или 4k")

    def local_path(option: str, default: str) -> Path:
        value = song_settings.get(option, default).strip()
        return _resolve_path(value)

    return {
        "program_url": song_settings.get("program_url", "").strip(),
        "zip_dir": local_path("local_zip_dir", "./song_assets/zip"),
        "pptx_dir": local_path("local_pptx_dir", "./song_assets/pptx"),
        "yandex_zip_paths": _parse_remote_paths(
            song_settings.get("yandex_zip_paths", "")
        ),
        "yandex_pptx_paths": _parse_remote_paths(
            song_settings.get("yandex_pptx_paths", "")
        ),
        "output_dir": root_path / "Трансляция" / "Песни",
        "quality": quality,
        "yandex_token": _load_yandex_token(),
        "log_dir": _resolve_path(
            settings.get(
                "Logging",
                "log_dir",
                fallback="/dev/shm/pptx2png_tasks/obs_local/logs",
            ).strip()
        ),
    }


def setup_logging(log_dir: Path, console_debug: bool = False) -> None:
    """Configure rotating INFO and DEBUG logs, matching the bot's log layout."""
    log_dir.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    )
    info_handler = RotatingFileHandler(
        log_dir / "bot.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(formatter)
    root_logger.addHandler(info_handler)

    debug_handler = RotatingFileHandler(
        log_dir / "debug.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(formatter)
    root_logger.addHandler(debug_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG if console_debug else logging.INFO)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    logging.info("Логирование OBS song preparation настроено: %s", log_dir)


def _parse_remote_paths(value: str) -> list[str]:
    return [path.strip() for path in value.split("|") if path.strip()]


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    return "".join(
        text.text or "" for text in paragraph.findall(".//w:t", DOCX_NS)
    ).strip()


def _table_rows(table: ElementTree.Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in table.findall("./w:tr", DOCX_NS):
        cells = []
        for cell in row.findall("./w:tc", DOCX_NS):
            cells.append(
                " ".join(
                    part
                    for part in (
                        _paragraph_text(paragraph)
                        for paragraph in cell.findall(".//w:p", DOCX_NS)
                    )
                    if part
                ).strip()
            )
        rows.append(cells)
    return rows


def _normalize(value: str) -> str:
    return re.sub(r"[\W_]+", " ", value.casefold(), flags=re.UNICODE).strip()


def _parse_program_date(text: str) -> date | None:
    match = re.search(
        r"программа\s+собрания\s+на\s+"
        r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})"
        r"(?:\s*г\.?)?",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    try:
        day, month, year = map(int, match.groups())
        return date(year, month, day)
    except ValueError:
        return None


def extract_song_titles(docx_path: Path, target_date: date) -> list[str]:
    """Read song-title columns from tables in the exact dated program section."""
    try:
        with zipfile.ZipFile(docx_path) as document:
            xml_data = document.read("word/document.xml")
    except (OSError, KeyError, zipfile.BadZipFile) as error:
        raise RuntimeError(f"Не удалось прочитать DOCX: {error}") from error

    try:
        root = ElementTree.fromstring(xml_data)
    except ElementTree.ParseError as error:
        raise RuntimeError(f"Повреждён XML документа DOCX: {error}") from error

    body = root.find(".//w:body", DOCX_NS)
    if body is None:
        raise RuntimeError("В DOCX не найдено основное содержимое документа")

    blocks: list[tuple[str, Any]] = []
    matching_sections: list[int] = []
    for item in body:
        if item.tag == f"{{{DOCX_NS['w']}}}p":
            text = _paragraph_text(item)
            program_date = _parse_program_date(text)
            if program_date is not None:
                blocks.append(("program", program_date))
                if program_date == target_date:
                    matching_sections.append(len(blocks) - 1)
            else:
                blocks.append(("paragraph", text))
        elif item.tag == f"{{{DOCX_NS['w']}}}tbl":
            blocks.append(("table", _table_rows(item)))

    if not matching_sections:
        raise RuntimeError(
            f"В программе не найден блок на {target_date:%d.%m.%Y}"
        )
    if len(matching_sections) != 1:
        raise RuntimeError(
            f"В документе найдено несколько блоков на {target_date:%d.%m.%Y}"
        )

    start = matching_sections[0] + 1
    end = len(blocks)
    for index in range(start, len(blocks)):
        kind, value = blocks[index]
        if kind == "program":
            end = index
            break
        if kind == "paragraph":
            normalized = _normalize(value)
            if normalized.startswith(("архив", "шаблон", "краткая памятка")):
                end = index
                break

    songs: list[str] = []
    for kind, value in blocks[start:end]:
        if kind != "table":
            continue
        rows: list[list[str]] = value
        if not rows:
            continue
        header = [_normalize(cell) for cell in rows[0]]
        title_columns = [
            index
            for index, cell in enumerate(header)
            if "название песни" in cell
        ]
        if len(title_columns) != 1:
            continue
        title_column = title_columns[0]
        for row in rows[1:]:
            if title_column >= len(row):
                continue
            title = row[title_column].strip()
            if not title or not title.strip("?").strip() or not _normalize(title):
                continue
            songs.append(title)

    if not songs:
        raise RuntimeError(
            f"В блоке {target_date:%d.%m.%Y} не найдено названий песен"
        )
    return songs


def _safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" ._")
    return cleaned[:100] or "Без названия"


def _song_folder_name(index: int, title: str) -> str:
    return f"{index:02d} - {_safe_name(title)}"


def _natural_key(value: str) -> list[Any]:
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", value)
    ]


def _find_local_matches(root: Path, title: str, suffix: str) -> list[Path]:
    normalized_title = _normalize(title)
    if not normalized_title:
        return []
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
    matches = []
    for path in root.rglob("*"):
        if (
            path.is_symlink()
            or not path.is_file()
            or path.suffix.casefold() != suffix
        ):
            continue
        normalized_stem = _normalize(path.stem)
        if f" {normalized_title} " in f" {normalized_stem} ":
            matches.append(path)
    return sorted(matches, key=lambda path: str(path).casefold())


async def _find_yandex_matches(
    client: YandexDiskClient,
    roots: list[str],
    title: str,
    suffix: str,
) -> list[dict[str, Any]]:
    normalized_title = _normalize(title)
    if not normalized_title:
        return []
    matches: list[dict[str, Any]] = []
    visited: set[str] = set()

    async def visit(path: str, depth: int) -> None:
        if path in visited:
            return
        visited.add(path)
        for item in await client.list_folder(path):
            name = str(item.get("name", ""))
            item_path = str(item.get("path", ""))
            if item.get("type") == "dir" and depth < MAX_YANDEX_SEARCH_DEPTH:
                if item_path:
                    await visit(item_path, depth + 1)
                continue
            if (
                item.get("type") == "file"
                and bool(item_path)
                and Path(name).suffix.casefold() == suffix
                and f" {normalized_title} " in f" {_normalize(Path(name).stem)} "
            ):
                matches.append(item)

    for root in roots:
        await visit(root, 0)
    return matches


def _unique_match(matches: list[Any], title: str, source: str) -> Any | None:
    if len(matches) > 1:
        names = ", ".join(
            str(item.get("name", item)) if isinstance(item, dict) else item.name
            for item in matches
        )
        raise RuntimeError(
            f"Для песни «{title}» найдено несколько файлов в {source}: {names}"
        )
    return matches[0] if matches else None


def _copy_zip_pngs(zip_path: Path, output_dir: Path) -> int:
    with tempfile.TemporaryDirectory(prefix=".zip-stage-", dir=output_dir) as temp_name:
        staged = Path(temp_name)
        with zipfile.ZipFile(zip_path) as archive:
            entries = [
                entry
                for entry in archive.infolist()
                if not entry.is_dir()
                and Path(entry.filename).suffix.casefold() == ".png"
            ]
            entries.sort(key=lambda entry: _natural_key(entry.filename))
            total_size = sum(entry.file_size for entry in entries)
            if total_size > MAX_ZIP_PNG_BYTES:
                raise RuntimeError("Общий размер PNG в ZIP превышает лимит 512 МБ")
            if len(entries) > MAX_ZIP_PNG_FILES:
                raise RuntimeError("В ZIP найдено больше 1000 PNG-файлов")
            written = 0
            for entry in entries:
                with archive.open(entry) as source:
                    signature = source.read(len(PNG_SIGNATURE))
                    if signature != PNG_SIGNATURE:
                        continue
                    destination = staged / f"slide_{written + 1:03d}.png"
                    with destination.open("wb") as target:
                        target.write(signature)
                        shutil.copyfileobj(source, target)
                    written += 1
        if not written:
            raise RuntimeError("В ZIP не найдено корректных PNG-файлов")
        for staged_png in sorted(staged.glob("*.png")):
            shutil.move(str(staged_png), output_dir / staged_png.name)
    return written


def _normalize_program_public_key(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise RuntimeError(
            "Ссылка программы должна использовать HTTPS"
        )
    if parsed.hostname == "docs.yandex.ru":
        raise RuntimeError(
            "Ссылка docs.yandex.ru/view/d/... открывает viewer документа, "
            "но её ID не является public_key Яндекс.Диска. Вставьте публичную "
            "ссылку файла из «Поделиться» в формате "
            "https://disk.yandex.ru/d/<public-key>"
        )
    if parsed.hostname == "disk.yandex.ru" and re.fullmatch(
        r"/(?:d|i)/[A-Za-z0-9_-]+/?", parsed.path
    ):
        return url
    if parsed.hostname == "yadi.sk" and re.fullmatch(
        r"/d/[A-Za-z0-9_-]+/?", parsed.path
    ):
        return url
    raise RuntimeError(
        "Ссылка программы должна быть публичной HTTPS-ссылкой вида "
        "disk.yandex.ru/d/<public-key>, disk.yandex.ru/i/<public-key> "
        "или yadi.sk/d/<public-key>"
    )


def _sanitize_diagnostic(value: str, sensitive_url: str = "") -> str:
    if sensitive_url:
        value = value.replace(sensitive_url, "[public-link redacted]")
        value = value.replace(
            quote(sensitive_url, safe=""), "[public-link redacted]"
        )
    return re.sub(
        r"https?://[^\s\"'<>]+",
        "[url redacted]",
        value,
        flags=re.IGNORECASE,
    )[:2000]


async def download_program_docx(
    session: aiohttp.ClientSession, url: str, destination: Path
) -> None:
    public_key = _normalize_program_public_key(url)
    LOGGER.info(
        "Запрос ссылки на DOCX программы через публичный API Яндекс.Диска "
        "(host=%s)",
        urlparse(url).hostname,
    )
    parsed_program_url = urlparse(url)
    safe_path = re.sub(
        r"(/(?:d|i)/)[^/]+",
        r"\1[share-id redacted]",
        parsed_program_url.path,
    )
    LOGGER.debug(
        "Формат публичной ссылки программы: %s%s",
        parsed_program_url.hostname,
        safe_path,
    )
    try:
        async with session.get(
            PUBLIC_DOWNLOAD_API,
            params={"public_key": public_key},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            LOGGER.debug(
                "Публичный API Яндекс.Диска ответил HTTP %s", response.status
            )
            if response.status != 200:
                detail = await _response_error_detail(response, public_key)
                LOGGER.error(
                    "Не удалось получить ссылку на программу: HTTP %s; %s",
                    response.status,
                    detail,
                )
                raise RuntimeError(
                    "Яндекс.Диск не выдал ссылку на программу "
                    f"(HTTP {response.status}): {detail}. Проверьте, что "
                    "документ доступен по публичной ссылке."
                )
            data = await response.json()
    except aiohttp.ClientError as error:
        LOGGER.error(
            "Сетевая ошибка при запросе публичной программы (%s): %s",
            type(error).__name__,
            _sanitize_diagnostic(str(error), url),
        )
        raise

    download_url = data.get("href")
    parsed_download_url = urlparse(download_url or "")
    if (
        parsed_download_url.scheme != "https"
        or not parsed_download_url.hostname
        or not parsed_download_url.hostname.endswith(
            (".yandex.ru", ".yandex.net", ".yandexcloud.net")
        )
    ):
        LOGGER.error(
            "Публичный API вернул некорректный href (type=%s, keys=%s)",
            type(download_url).__name__,
            sorted(data.keys()) if isinstance(data, dict) else [],
        )
        raise RuntimeError("Яндекс.Диск вернул некорректную ссылку на скачивание")

    LOGGER.debug(
        "Получена временная ссылка для скачивания программы (host=%s)",
        parsed_download_url.hostname,
    )
    try:
        async with session.get(
            download_url, timeout=aiohttp.ClientTimeout(total=120)
        ) as response:
            LOGGER.debug("Скачивание DOCX ответило HTTP %s", response.status)
            if response.status != 200:
                detail = await _response_error_detail(response, url)
                LOGGER.error(
                    "Не удалось скачать DOCX программы: HTTP %s; %s",
                    response.status,
                    detail,
                )
                raise RuntimeError(
                    f"Не удалось скачать программу (HTTP {response.status}): "
                    f"{detail}"
                )
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_PROGRAM_DOCX_BYTES:
                raise RuntimeError("Размер DOCX превышает лимит 50 МБ")
            downloaded = 0
            with destination.open("wb") as output:
                async for chunk in response.content.iter_chunked(64 * 1024):
                    downloaded += len(chunk)
                    if downloaded > MAX_PROGRAM_DOCX_BYTES:
                        raise RuntimeError("Размер DOCX превышает лимит 50 МБ")
                    output.write(chunk)
            LOGGER.info("DOCX программы скачан (%s байт)", downloaded)
    except aiohttp.ClientError as error:
        LOGGER.error(
            "Сетевая ошибка при скачивании DOCX программы (%s): %s",
            type(error).__name__,
            _sanitize_diagnostic(str(error), url),
        )
        raise

    try:
        with zipfile.ZipFile(destination) as document:
            if "word/document.xml" not in document.namelist():
                raise RuntimeError(
                    "Скачанный файл не похож на DOCX: отсутствует "
                    "word/document.xml"
                )
            corrupt_member = document.testzip()
            if corrupt_member:
                raise RuntimeError(
                    f"В скачанном DOCX повреждена запись {corrupt_member}"
                )
    except RuntimeError:
        raise
    except (OSError, zipfile.BadZipFile) as error:
        raise RuntimeError(
            "Скачанный по публичной ссылке файл не является DOCX. "
            "Возможно, ссылка ведёт на изображение или Яндекс.Диск вернул "
            "страницу CAPTCHA вместо файла."
        ) from error


async def _response_error_detail(
    response: aiohttp.ClientResponse, shared_url: str
) -> str:
    """Read bounded API diagnostics while redacting public links and tokens."""
    try:
        body = (await response.text()).strip()
    except (aiohttp.ClientError, UnicodeDecodeError):
        LOGGER.exception("Не удалось прочитать тело HTTP-ошибки Яндекс.Диска")
        return "тело ответа прочитать не удалось"

    detail = body[:2000]
    try:
        payload = json.loads(body)
        if isinstance(payload, dict):
            fields = [
                payload.get(key)
                for key in ("error", "error_description", "description", "message")
                if payload.get(key)
            ]
            if fields:
                detail = "; ".join(str(value) for value in fields)[:2000]
    except json.JSONDecodeError:
        pass

    detail = _sanitize_diagnostic(detail, shared_url)
    return detail or "ответ не содержит описания ошибки"


async def _process_song(
    title: str,
    song_dir: Path,
    config: dict[str, Any],
    client: YandexDiskClient | None,
    temp_root: Path,
) -> SongResult:
    result = SongResult(title=title, folder=song_dir)
    song_dir.mkdir(parents=True)

    source_steps = [
        ("local ZIP", config["zip_dir"], ".zip"),
        ("local PPTX", config["pptx_dir"], ".pptx"),
    ]
    for label, root, suffix in source_steps:
        LOGGER.debug(
            "Поиск песни %r: источник=%s каталог=%s",
            title,
            label,
            root,
        )
        try:
            match = _unique_match(
                _find_local_matches(root, title, suffix), title, label
            )
        except RuntimeError as error:
            result.status = "ambiguous"
            result.error = str(error)
            return result
        if match is None:
            LOGGER.debug("Совпадений песни %r в %s не найдено", title, label)
            continue
        LOGGER.info("Для песни %r выбран локальный файл: %s", title, match.name)
        if suffix == ".zip":
            try:
                result.png_count = _copy_zip_pngs(match, song_dir)
            except (zipfile.BadZipFile, RuntimeError) as error:
                logging.warning("Не удалось использовать %s: %s", match, error)
                continue
        else:
            try:
                with tempfile.TemporaryDirectory(
                    prefix="song_render_", dir=temp_root
                ) as render_root:
                    png_paths, _ = await convert_all_pngs(
                        match, Path(render_root), config["quality"]
                    )
                    if not png_paths:
                        raise RuntimeError("Конвертер не создал PNG")
                    for png_index, png_path in enumerate(png_paths, 1):
                        shutil.copy2(
                            png_path, song_dir / f"slide_{png_index:03d}.png"
                        )
                    result.png_count = len(png_paths)
            except Exception as error:
                for child in song_dir.iterdir():
                    if child.is_file() or child.is_symlink():
                        child.unlink()
                    elif child.is_dir():
                        shutil.rmtree(child)
                result.status = "error"
                result.error = f"Ошибка конвертации {match.name}: {error}"
                return result
        result.status = "ready"
        result.source = str(match)
        return result

    remote_steps = [
        ("Yandex Disk ZIP", config["yandex_zip_paths"], ".zip"),
        ("Yandex Disk PPTX", config["yandex_pptx_paths"], ".pptx"),
    ]
    for label, roots, suffix in remote_steps:
        if not roots:
            continue
        LOGGER.debug(
            "Поиск песни %r: источник=%s, папок=%d",
            title,
            label,
            len(roots),
        )
        if client is None:
            result.status = "error"
            result.error = "Для поиска на Яндекс.Диске не задан токен"
            return result
        matches = await _find_yandex_matches(client, roots, title, suffix)
        try:
            match = _unique_match(matches, title, label)
        except RuntimeError as error:
            result.status = "ambiguous"
            result.error = str(error)
            return result
        if match is None:
            LOGGER.debug(
                "Совпадений песни %r в %s не найдено", title, label
            )
            continue
        LOGGER.info(
            "Для песни %r выбран файл на Яндекс.Диске: %s",
            title,
            match["name"],
        )

        with tempfile.TemporaryDirectory(prefix="song_download_") as download_root:
            source_path = Path(download_root) / _safe_name(match["name"])
            if not await client.download_file(match["path"], source_path):
                logging.warning(
                    "Не удалось скачать %s с Яндекс.Диска", match["name"]
                )
                continue
            if suffix == ".zip":
                try:
                    result.png_count = _copy_zip_pngs(source_path, song_dir)
                except (zipfile.BadZipFile, RuntimeError) as error:
                    logging.warning(
                        "Не удалось использовать %s: %s", match["name"], error
                    )
                    continue
            else:
                try:
                    with tempfile.TemporaryDirectory(
                        prefix="song_render_", dir=temp_root
                    ) as render_root:
                        png_paths, _ = await convert_all_pngs(
                            source_path, Path(render_root), config["quality"]
                        )
                        if not png_paths:
                            raise RuntimeError("Конвертер не создал PNG")
                        for png_index, png_path in enumerate(png_paths, 1):
                            shutil.copy2(
                                png_path, song_dir / f"slide_{png_index:03d}.png"
                            )
                        result.png_count = len(png_paths)
                except Exception as error:
                    for child in song_dir.iterdir():
                        if child.is_file() or child.is_symlink():
                            child.unlink()
                        elif child.is_dir():
                            shutil.rmtree(child)
                    result.status = "error"
                    result.error = (
                        f"Ошибка конвертации {match['name']}: {error}"
                    )
                    return result
        result.status = "ready"
        result.source = f"{label}: {match['path']}"
        return result

    result.status = "not_found"
    return result


def _publish_output(staging: Path, target: Path) -> Path | None:
    backup = None
    if target.exists():
        backup = target.parent / f".Песни-backup-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
        target.rename(backup)
    try:
        staging.rename(target)
    except OSError:
        if backup is not None and backup.exists():
            backup.rename(target)
        raise
    return backup


async def run(
    target_date: date,
    program_file: Path | None = None,
    program_url: str | None = None,
    config: dict[str, Any] | None = None,
) -> list[SongResult]:
    config = config or load_config()
    url = (program_url or config["program_url"]).strip()
    LOGGER.info("Запуск подготовки песен OBS на %s", target_date.isoformat())
    LOGGER.debug(
        "Конфигурация: local_zip=%s, local_pptx=%s, output=%s, "
        "yandex_zip_roots=%d, yandex_pptx_roots=%d",
        config["zip_dir"],
        config["pptx_dir"],
        config["output_dir"],
        len(config["yandex_zip_paths"]),
        len(config["yandex_pptx_paths"]),
    )
    if program_file is None and not url:
        raise RuntimeError(
            "Укажите --program-file или задайте [Song preparation] program_url"
        )
    if program_file is not None and not program_file.is_file():
        raise RuntimeError(f"Файл программы не найден: {program_file}")

    config["zip_dir"].mkdir(parents=True, exist_ok=True)
    config["pptx_dir"].mkdir(parents=True, exist_ok=True)
    target = config["output_dir"]
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix=".Песни-staging-", dir=target.parent
    ) as stage_root:
        staging = Path(stage_root) / "Песни"
        staging.mkdir()
        with tempfile.TemporaryDirectory(prefix="song_work_") as work_root:
            work_dir = Path(work_root)
            if program_file is None:
                async with aiohttp.ClientSession() as session:
                    program_file = work_dir / "program.docx"
                    await download_program_docx(session, url, program_file)

            LOGGER.info("Разбор программы: %s", program_file.name)
            songs = extract_song_titles(program_file, target_date)
            LOGGER.info(
                "В программе на %s найдено песен: %d",
                target_date.isoformat(),
                len(songs),
            )
            print(f"Программа на {target_date:%d.%m.%Y}; найдено песен: {len(songs)}")
            for index, title in enumerate(songs, 1):
                print(f"  {index:02d}. {title}")

            client = None
            if config["yandex_zip_paths"] or config["yandex_pptx_paths"]:
                if not config["yandex_token"]:
                    raise RuntimeError(
                        "Для поиска песен на Яндекс.Диске задайте "
                        "[YandexDisk] token в общем config.ini "
                        "в корне проекта (как для бота)"
                    )
                async with aiohttp.ClientSession() as session:
                    client = YandexDiskClient(config["yandex_token"], session)
                    results = await _process_songs(
                        songs, staging, config, client, work_dir
                    )
            else:
                results = await _process_songs(
                    songs, staging, config, None, work_dir
                )

        backup = _publish_output(staging, target)

        for result in results:
            result.folder = target / result.folder.name

    print(f"\nРезультаты сохранены: {target}")
    if backup:
        print(f"Предыдущий результат сохранён: {backup}")
    for index, result in enumerate(results, 1):
        if result.status == "ready":
            LOGGER.info(
                "Песня %02d %r: подготовлено PNG=%d, источник=%s",
                index,
                result.title,
                result.png_count,
                result.source,
            )
            print(
                f"  {index:02d}. {result.title}: {result.png_count} PNG "
                f"({result.source})"
            )
        elif result.error:
            LOGGER.error("Песня %02d %r: %s", index, result.title, result.error)
            print(f"  {index:02d}. {result.title}: ошибка — {result.error}")
        else:
            LOGGER.warning("Для песни %02d %r материал не найден", index, result.title)
            print(f"  {index:02d}. {result.title}: материал не найден")
    return results


async def _process_songs(
    songs: list[str],
    staging: Path,
    config: dict[str, Any],
    client: YandexDiskClient | None,
    temp_root: Path,
) -> list[SongResult]:
    results = []
    for index, title in enumerate(songs, 1):
        song_dir = staging / _song_folder_name(index, title)
        result = await _process_song(
            title, song_dir, config, client, temp_root
        )
        results.append(result)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Найти песенные материалы и подготовить папки PNG для OBS."
    )
    parser.add_argument(
        "--date",
        help="Дата программы YYYY-MM-DD; по умолчанию ближайшее воскресенье.",
    )
    parser.add_argument(
        "--program-file",
        type=Path,
        help="Локальный DOCX для тестового запуска; имеет приоритет над URL.",
    )
    parser.add_argument(
        "--program-url",
        help="Публичная ссылка на DOCX; если не задана, берётся из settings.ini.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        help="Каталог логов; по умолчанию берётся из секции [Logging].",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Показывать DEBUG-сообщения также в консоли.",
    )
    args = parser.parse_args()
    if args.date:
        try:
            selected_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError as error:
            parser.error(f"Неверная дата: {error}")
    else:
        selected_date = get_nearest_sunday().date()

    try:
        config = load_config()
        log_dir = args.log_dir or config["log_dir"]
        setup_logging(log_dir, console_debug=args.debug)
        LOGGER.info(
            "Начало локального запуска; подробный лог: %s",
            log_dir / "debug.log",
        )
        asyncio.run(
            run(
                selected_date,
                args.program_file,
                args.program_url,
                config=config,
            )
        )
    except Exception as error:
        if logging.getLogger().handlers:
            LOGGER.exception("Подготовка песен завершилась ошибкой")
        parser.exit(1, f"Ошибка: {error}\n")


if __name__ == "__main__":
    main()
