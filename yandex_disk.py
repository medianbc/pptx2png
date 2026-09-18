# ==========================================
# yandex_disk.py — клиент Яндекс.Диска (v1.2)
# ==========================================

import aiohttp
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
    """Асинхронный клиент для REST API Яндекс.Диска."""

    def __init__(self, token: str):
        self.token = token
        self.headers = {"Authorization": f"OAuth {token}"}

    # ---------- Проверка доступности ----------

    async def check_access(self) -> Tuple[bool, Optional[str]]:
        if not self.token:
            return False, "Токен не задан"
        url = f"{YANDEX_API_BASE}/"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers, timeout=15) as resp:
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

    async def get_resource(
        self,
        path: str,
        limit: int = 1000,
        offset: int = 0,
    ) -> Dict[str, Any]:
        url = f"{YANDEX_API_BASE}/resources"
        params = {
            "path": path,
            "limit": limit,
            "offset": offset,
            "fields": "name,path,type,size,created,modified,_embedded",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, headers=self.headers, params=params, timeout=30
                ) as resp:
                    if resp.status == 404:
                        raise YandexDiskNotFoundError(f"Не найдено: {path}")
                    if resp.status != 200:
                        raise YandexDiskError(f"HTTP {resp.status} для {path}")
                    return await resp.json()
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
        try:
            resource = await self.get_resource(path)
        except YandexDiskNotFoundError:
            return False
        return resource.get("type") == "dir"

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
                    return item
            except Exception as e:
                logging.error(f"Ошибка предиката для '{item['name']}': {e}")
        return None

    # ---------- Скачивание / Загрузка ----------

    async def download_file(self, remote_path: str, destination: Path) -> bool:
        """
        Скачивает файл с Диска ПОТОКОВО (chunked),
        не загружая весь файл в память.
        """
        url = f"{YANDEX_API_BASE}/resources/download"
        params = {"path": remote_path}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, headers=self.headers, params=params, timeout=30
                ) as resp:
                    if resp.status != 200:
                        logging.error(f"Yandex API download: HTTP {resp.status}")
                        return False
                    data = await resp.json()
                    href = data.get("href")
                    if not href:
                        return False

                async with session.get(href, timeout=600) as file_resp:
                    if file_resp.status != 200:
                        logging.error(f"Ошибка скачивания файла: HTTP {file_resp.status}")
                        return False
                    # ✅ Потоковая запись блоками по 64 КБ
                    with open(destination, "wb") as f:
                        async for chunk in file_resp.content.iter_chunked(64 * 1024):
                            f.write(chunk)
                return True
        except Exception as e:
            logging.error(f"Ошибка download_file: {e}", exc_info=True)
            return False

    async def upload_file(self, local_path: Path, remote_path: str, overwrite: bool = True) -> bool:
        url = f"{YANDEX_API_BASE}/resources/upload"
        params = {"path": remote_path, "overwrite": str(overwrite).lower()}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, headers=self.headers, params=params, timeout=30
                ) as resp:
                    if resp.status != 200:
                        logging.error(f"Yandex API upload URL: HTTP {resp.status}")
                        return False
                    data = await resp.json()
                    href = data.get("href")
                    if not href:
                        return False

                with open(local_path, "rb") as f:
                    async with session.put(href, data=f, timeout=600) as upload_resp:
                        if upload_resp.status not in (200, 201, 202):
                            logging.error(f"Ошибка загрузки на Диск: HTTP {upload_resp.status}")
                            return False
                return True
        except Exception as e:
            logging.error(f"Ошибка upload_file: {e}", exc_info=True)
            return False

    # ---------- Создание папок ----------

    async def create_folder(self, path: str) -> bool:
        """
        Создаёт папку. Возвращает True если создана или уже существует.
        Разбирает 409 — отличает «уже существует» от других конфликтов.
        """
        url = f"{YANDEX_API_BASE}/resources"
        params = {"path": path}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.put(
                    url, headers=self.headers, params=params, timeout=30
                ) as resp:
                    if resp.status == 201:
                        return True
                    if resp.status == 409:
                        # ✅ Разбираем ошибку 409 — только "уже существует" = успех
                        try:
                            data = await resp.json()
                        except Exception:
                            logging.error(
                                f"create_folder {path}: 409 без JSON"
                            )
                            return False
                        err = data.get("error", "")
                        if err in (
                            "DiskPathAlreadyExistsError",
                            "DiskResourceAlreadyExistsError",
                        ):
                            return True
                        logging.error(
                            f"create_folder {path}: 409 error={err}"
                        )
                        return False
                    logging.error(
                        f"create_folder {path}: HTTP {resp.status}"
                    )
                    return False
        except Exception as e:
            logging.error(f"Ошибка create_folder: {e}")
            return False

    async def ensure_folder(self, path: str) -> bool:
        """
        Создаёт папку и все родительские при необходимости.
        Собирает путь корректно: /a, /a/b, /a/b/c.
        """
        parts = [p for p in path.strip("/").split("/") if p]
        for i in range(1, len(parts) + 1):
            current = "/" + "/".join(parts[:i])
            if not await self.create_folder(current):
                return False
        return True


# ==========================================
# УТИЛИТЫ ДЛЯ ДАТ
# ==========================================

def get_nearest_sunday(reference_date: Optional[datetime] = None) -> datetime:
    """
    Ближайшее ПРЕДСТОЯЩЕЕ воскресенье.
    - Если сегодня воскресенье — возвращает сегодня.
    - Иначе — следующее воскресенье.
    """
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

    return {
        "month_folder": month["path"],
        "date_folder": date_folder["path"],
        "source": f"{date_folder['path']}/{source_folder}",
        "target": f"{date_folder['path']}/{target_folder}",
    }


# ==========================================
# ПОИСК PPTX В СЛУЖЕНИИ
# ==========================================

async def find_pptx_in_source(
    client: YandexDiskClient,
    source_path: str,
    sunday: datetime,
) -> List[Dict[str, Any]]:
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
