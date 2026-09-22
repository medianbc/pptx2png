# ==========================================
# yandex_disk.py — клиент Яндекс.Диска (v1.8)
# ==========================================

import aiohttp
import json
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple, Callable
from datetime import datetime, timedelta


YANDEX_API_BASE = "https://cloud-api.yandex.net/v1/disk"


RUSSIAN_MONTHS = [
    "", "ЯНВАРЬ", "ФЕВРАЛЬ", "МАРТ", "АПРЕЛЬ", "МАЙ", "ИЮНЬ",
    "ИЮЛЬ", "АВГУСТ", "СЕНТЯБРЬ", "ОКТЯБРЬ", "НОЯБРЬ", "ДЕКАБРЬ"
]


# ==========================================
# КОНСТАНТЫ ОШИБОК ЯНДЕКС API
# ==========================================

YD_EXISTS_ERRORS = frozenset({
    "DiskPathAlreadyExistsError",
    "DiskResourceAlreadyExistsError",
    "DiskPathPointsToExistentDirectoryError",
})

YD_PARENT_NOT_FOUND_ERRORS = frozenset({
    "DiskPathDoesntExistsError",
})


# ==========================================
# УТИЛИТЫ ПУТЕЙ
# ==========================================

def strip_disk_prefix(path: str) -> str:
    """
    Убирает префикс 'disk:' из пути Яндекс.Диска.

    API возвращает пути вида 'disk:/folder/sub' в ответах,
    но НЕ принимает их в параметрах запросов — только '/folder/sub'.

    Применяется ко всем путям, которые приходят из ответов API,
    и защищает методы на входе, если пользователь случайно передал 'disk:'.
    """
    if isinstance(path, str) and path.startswith("disk:"):
        return path[len("disk:"):]
    return path


def normalize_resource_paths(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Рекурсивно нормализует пути в объекте ресурса Яндекс.Диска:

    - item["path"]                              → без 'disk:'
    - item["_embedded"]["path"]                 → без 'disk:'
    - item["_embedded"]["items"][*]["path"]     → без 'disk:' (рекурсивно)

    Возвращает тот же объект (мутирует на месте для экономии памяти).
    """
    if not isinstance(item, dict):
        return item

    if "path" in item and isinstance(item["path"], str):
        item["path"] = strip_disk_prefix(item["path"])

    embedded = item.get("_embedded")
    if isinstance(embedded, dict):
        if "path" in embedded and isinstance(embedded["path"], str):
            embedded["path"] = strip_disk_prefix(embedded["path"])

        items = embedded.get("items")
        if isinstance(items, list):
            for child in items:
                normalize_resource_paths(child)

    return item


# ==========================================
# ИСКЛЮЧЕНИЯ
# ==========================================

class YandexDiskError(Exception):
    """Общая ошибка обращения к API Яндекс.Диска."""
    pass


class YandexDiskNotFoundError(YandexDiskError):
    """Ресурс не найден (404)."""
    pass


# ==========================================
# КЛИЕНТ
# ==========================================

class YandexDiskClient:
    """
    Асинхронный клиент для REST API Яндекс.Диска.
    Использует ОБЩУЮ aiohttp.ClientSession (передаётся извне).
    """

    def __init__(self, token: str, http_session: aiohttp.ClientSession):
        self.token = token
        self.session = http_session
        self.headers = {"Authorization": f"OAuth {token}"}

    # ---------- Проверка доступности ----------

    async def check_access(self) -> Tuple[bool, Optional[str]]:
        if not self.token:
            return False, "Токен не задан"
        url = f"{YANDEX_API_BASE}/"
        try:
            async with self.session.get(
                url, headers=self.headers, timeout=15
            ) as resp:
                if resp.status == 200:
                    return True, None
                elif resp.status == 401:
                    return False, "Неверный токен (401)"
                elif resp.status == 403:
                    return False, "Доступ запрещён (403)"
                else:
                    return False, f"HTTP {resp.status}"
        except aiohttp.ClientError as e:
            return False, f"Ошибка сети: {e}"
        except Exception as e:
            return False, f"Неизвестная ошибка: {e}"

    # ---------- Метаданные ----------

    async def resource_type(self, path: str) -> Optional[str]:
        path = strip_disk_prefix(path)
        url = f"{YANDEX_API_BASE}/resources"
        params = {"path": path, "fields": "type"}
        logging.debug(f"[YD-API] GET /resources?path={path!r}&fields=type")
        try:
            async with self.session.get(
                url, headers=self.headers, params=params, timeout=15
            ) as resp:
                logging.debug(f"[YD-API] → {resp.status} (GET {path!r})")
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    raise YandexDiskError(f"HTTP {resp.status} для {path}")
                data = await resp.json()
                return data.get("type")
        except aiohttp.ClientError as e:
            raise YandexDiskError(f"Сеть: {e}")
        except YandexDiskError:
            raise
        except Exception as e:
            raise YandexDiskError(f"Неизвестная ошибка: {e}")

    async def get_resource(
        self,
        path: str,
        limit: int = 1000,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Полные метаданные с _embedded (для списка содержимого)."""
        path = strip_disk_prefix(path)
        url = f"{YANDEX_API_BASE}/resources"
        params = {
            "path": path,
            "limit": limit,
            "offset": offset,
            "fields": "name,path,type,size,created,modified,_embedded",
        }
        logging.debug(
            f"[YD-API] GET /resources?path={path!r}&limit={limit}&offset={offset}"
        )
        try:
            async with self.session.get(
                url, headers=self.headers, params=params, timeout=30
            ) as resp:
                logging.debug(
                    f"[YD-API] → {resp.status} (GET {path!r}, "
                    f"limit={limit}, offset={offset})"
                )
                if resp.status == 404:
                    raise YandexDiskNotFoundError(f"Не найдено: {path}")
                if resp.status != 200:
                    raise YandexDiskError(f"HTTP {resp.status} для {path}")
                data = await resp.json()
                normalize_resource_paths(data)
                return data
        except aiohttp.ClientError as e:
            raise YandexDiskError(f"Сеть: {e}")
        except (YandexDiskNotFoundError, YandexDiskError):
            raise
        except Exception as e:
            raise YandexDiskError(f"Неизвестная ошибка: {e}")

    async def list_folder(self, path: str, page_size: int = 200) -> List[Dict[str, Any]]:
        all_items: List[Dict[str, Any]] = []
        offset = 0

        while True:
            resource = await self.get_resource(path, limit=page_size, offset=offset)

            embedded = resource.get("_embedded", {})
            items = embedded.get("items", [])
            total = embedded.get("total", 0)

            if not items:
                break

            all_items.extend(items)
            offset += len(items)

            if offset >= total:
                break

            if len(items) < page_size:
                break

        return all_items

    async def folder_exists(self, path: str) -> bool:
        """Проверяет существование папки. 404 → False."""
        t = await self.resource_type(path)
        return t == "dir"

    async def find_child_folder(
        self,
        parent_path: str,
        predicate: Callable[[str], bool],
    ) -> Optional[Dict[str, Any]]:
        items = await self.list_folder(parent_path)
        for item in items:
            if item.get("type") != "dir":
                continue
            try:
                if predicate(item["name"]):
                    if isinstance(item.get("path"), str):
                        item["path"] = strip_disk_prefix(item["path"])
                    return item
            except Exception as e:
                logging.error(f"Ошибка предиката для '{item['name']}': {e}")
        return None

    # ---------- Скачивание / Загрузка ----------

    async def download_file(self, remote_path: str, destination: Path) -> bool:
        remote_path = strip_disk_prefix(remote_path)
        logging.debug(f"[YD-API] download {remote_path!r} → {destination.name}")
        url = f"{YANDEX_API_BASE}/resources/download"
        params = {"path": remote_path}
        try:
            async with self.session.get(
                url, headers=self.headers, params=params, timeout=30
            ) as resp:
                logging.debug(
                    f"[YD-API] download {remote_path!r} → {resp.status}"
                )
                if resp.status != 200:
                    logging.error(f"Yandex API download: HTTP {resp.status}")
                    return False
                data = await resp.json()
                href = data.get("href")
                if not href:
                    return False

            async with self.session.get(href, timeout=600) as file_resp:
                if file_resp.status != 200:
                    logging.error(f"Ошибка скачивания файла: HTTP {file_resp.status}")
                    return False
                with open(destination, "wb") as f:
                    async for chunk in file_resp.content.iter_chunked(64 * 1024):
                        f.write(chunk)
            return True
        except Exception as e:
            logging.error(f"Ошибка download_file: {e}", exc_info=True)
            return False

    async def upload_file(self, local_path: Path, remote_path: str,
                          overwrite: bool = True) -> bool:
        remote_path = strip_disk_prefix(remote_path)
        size = local_path.stat().st_size if local_path.exists() else 0
        logging.debug(
            f"[YD-API] upload {local_path.name} ({size} байт) → {remote_path!r}"
        )
        url = f"{YANDEX_API_BASE}/resources/upload"
        params = {"path": remote_path, "overwrite": str(overwrite).lower()}
        try:
            async with self.session.get(
                url, headers=self.headers, params=params, timeout=30
            ) as resp:
                logging.debug(
                    f"[YD-API] upload URL → {resp.status} ({local_path.name})"
                )
                if resp.status != 200:
                    logging.error(f"Yandex API upload URL: HTTP {resp.status}")
                    return False
                data = await resp.json()
                href = data.get("href")
                if not href:
                    return False

            with open(local_path, "rb") as f:
                async with self.session.put(href, data=f, timeout=600) as upload_resp:
                    logging.debug(
                        f"[YD-API] upload {local_path.name} → {upload_resp.status}"
                    )
                    if upload_resp.status not in (200, 201, 202):
                        logging.error(
                            f"Ошибка загрузки на Диск: HTTP {upload_resp.status}"
                        )
                        return False
            return True
        except Exception as e:
            logging.error(f"Ошибка upload_file: {e}", exc_info=True)
            return False

    # ---------- Создание папок ----------

    async def create_folder(self, path: str) -> bool:
        """
        Создаёт одну папку (без родителей).

        HTTP 409:
        - YD_EXISTS_ERRORS → True (папка уже есть)
        - YD_PARENT_NOT_FOUND_ERRORS → False (родителя нет)
        - неизвестный → fallback GET
        """
        path = strip_disk_prefix(path)
        url = f"{YANDEX_API_BASE}/resources"
        params = {"path": path}
        logging.debug(f"[YD-API] PUT /resources?path={path!r}")
        try:
            async with self.session.put(
                url, headers=self.headers, params=params, timeout=30
            ) as resp:
                logging.debug(f"[YD-API] PUT → {resp.status} ({path!r})")
                if resp.status == 201:
                    return True

                if resp.status == 409:
                    error_code = ""
                    try:
                        body = await resp.text()
                        logging.info(
                            f"create_folder {path}: 409 body={body[:300]}"
                        )
                        try:
                            data = json.loads(body)
                            error_code = data.get("error", "")
                        except json.JSONDecodeError:
                            pass
                    except Exception as e:
                        logging.warning(
                            f"create_folder {path}: не удалось прочитать 409: {e}"
                        )

                    if error_code in YD_EXISTS_ERRORS:
                        return True

                    if error_code in YD_PARENT_NOT_FOUND_ERRORS:
                        logging.error(
                            f"create_folder {path}: 409 {error_code} — "
                            f"родительская папка отсутствует"
                        )
                        return False

                    logging.warning(
                        f"create_folder {path}: неизвестный 409 "
                        f"error='{error_code}', проверяю через GET"
                    )
                    try:
                        return await self.folder_exists(path)
                    except Exception as e:
                        logging.error(
                            f"create_folder {path}: fallback GET failed: {e}"
                        )
                        return False

                try:
                    body = await resp.text()
                except Exception:
                    body = "<no body>"
                logging.error(
                    f"create_folder {path}: HTTP {resp.status} body={body[:300]}"
                )
                return False
        except Exception as e:
            logging.error(f"Ошибка create_folder: {e}", exc_info=True)
            return False

    async def ensure_folder(self, path: str) -> bool:
        """
        Создаёт папку и всех родителей.
        Для "/a/b/c" проверит/создаст /a, /a/b, /a/b/c.
        """
        path = strip_disk_prefix(path)
        parts = [p for p in path.strip("/").split("/") if p]
        for i in range(1, len(parts) + 1):
            current = "/" + "/".join(parts[:i])

            try:
                if await self.folder_exists(current):
                    continue
            except YandexDiskError as e:
                logging.warning(
                    f"ensure_folder: проверка {current} не удалась: {e}. "
                    f"Пробую создать."
                )

            if not await self.create_folder(current):
                logging.error(f"ensure_folder: не удалось создать {current}")
                return False
        return True


# ==========================================
# УТИЛИТЫ ДЛЯ ДАТ
# ==========================================

def get_nearest_sunday(reference_date: Optional[datetime] = None) -> datetime:
    """Ближайшее ПРЕДСТОЯЩЕЕ воскресенье."""
    if reference_date is None:
        reference_date = datetime.now()
    days_until_sunday = (6 - reference_date.weekday()) % 7
    return reference_date + timedelta(days=days_until_sunday)


def month_folder_name(date: datetime) -> str:
    return f"{date.month:02d} {RUSSIAN_MONTHS[date.month]} {date.year}"


def date_folder_variants(date: datetime) -> List[str]:
    return [
        date.strftime("%d.%m.%Y"),
        date.strftime("%d.%m.%y"),
        date.strftime("%Y-%m-%d"),
        date.strftime("%d-%m-%Y"),
        date.strftime("%d-%m-%y"),
        date.strftime("%d_%m_%Y"),
        date.strftime("%d_%m_%y"),
        f"{date.day}.{date.month}.{date.year}",
    ]


def date_folder_matches(name: str, date: datetime) -> bool:
    name_clean = name.strip()
    return any(name_clean == v for v in date_folder_variants(date))


def pptx_matches_date(filename: str, date: datetime) -> bool:
    variants = [
        date.strftime("%d.%m.%y"),
        date.strftime("%d.%m.%Y"),
        date.strftime("%d-%m-%y"),
        date.strftime("%d-%m-%Y"),
        date.strftime("%d_%m_%y"),
        date.strftime("%d_%m_%Y"),
        date.strftime("%Y-%m-%d"),
    ]
    return any(v in filename for v in variants)


# ==========================================
# РАЗРЕШЕНИЕ ПУТЕЙ
# ==========================================

async def resolve_sunday_paths(
    client: YandexDiskClient,
    base_path: str,
    sunday: datetime,
    source_folder: str = "Служение",
    target_folder: str = "Трансляция",
) -> Optional[Dict[str, str]]:
    """Находит пути для указанного воскресенья."""
    month_name = month_folder_name(sunday)

    def match_month(name: str) -> bool:
        return " ".join(name.upper().split()) == " ".join(month_name.upper().split())

    month = await client.find_child_folder(base_path, match_month)
    if not month:
        logging.warning(f"Месячная папка '{month_name}' не найдена в '{base_path}'")
        return None

    date_folder = await client.find_child_folder(
        month["path"],
        lambda name: date_folder_matches(name, sunday),
    )
    if not date_folder:
        logging.warning(f"Папка даты '{sunday:%d.%m.%Y}' не найдена в {month['path']}")
        return None

    month_path = strip_disk_prefix(month["path"])
    date_path = strip_disk_prefix(date_folder["path"])

    return {
        "month_folder": month_path,
        "date_folder": date_path,
        "source": f"{date_path}/{source_folder}",
        "target": f"{date_path}/{target_folder}",
    }


# ==========================================
# ПОИСК PPTX В СЛУЖЕНИИ
# ==========================================

async def find_pptx_in_source(
    client: YandexDiskClient,
    source_path: str,
    sunday: datetime,
) -> List[Dict[str, Any]]:
    source_path = strip_disk_prefix(source_path)
    if not await client.folder_exists(source_path):
        return []
    items = await client.list_folder(source_path)
    result = []
    for item in items:
        if item.get("type") != "file":
            continue
        name = item["name"]
        if not name.lower().endswith((".pptx", ".ppt")):
            continue
        if pptx_matches_date(name, sunday):
            result.append(item)
    return result