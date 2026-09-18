# ==========================================
# yandex_disk.py — клиент Яндекс.Диска (исправлен)
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
    ) -> Optional[Dict[str, Any]]:
        """Получить метаданные файла/папки."""
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
                        return None
                    if resp.status != 200:
                        logging.error(f"get_resource {path}: HTTP {resp.status}")
                        return None
                    return await resp.json()
        except Exception as e:
            logging.error(f"Ошибка get_resource: {e}")
            return None

    async def list_folder(self, path: str, page_size: int = 200) -> List[Dict[str, Any]]:
        """
        Список содержимого папки с ПОЛНОЙ пагинацией.
        Проходит все страницы, пока не соберёт все элементы.
        """
        all_items: List[Dict[str, Any]] = []
        offset = 0

        while True:
            resource = await self.get_resource(path, limit=page_size, offset=offset)
            if not resource:
                break

            embedded = resource.get("_embedded", {})
            items = embedded.get("items", [])
            total = embedded.get("total", 0)

            if not items:
                break

            all_items.extend(items)
            offset += len(items)

            # Собрали всё — выходим
            if offset >= total:
                break

            # Защита от бесконечного цикла
            if len(items) < page_size:
                break

        return all_items

    async def folder_exists(self, path: str) -> bool:
        resource = await self.get_resource(path)
        return resource is not None and resource.get("type") == "dir"

    async def find_child_folder(
        self,
        parent_path: str,
        predicate: Callable[[str], bool],
    ) -> Optional[Dict[str, Any]]:
        """
        Найти дочернюю папку по предикату.
        ⚠️ Предикат должен быть СИНХРОННЫМ (def, не async def).
        """
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


# ==========================================
# УТИЛИТЫ ДЛЯ ДАТ
# ==========================================

def get_nearest_sunday(reference_date: Optional[datetime] = None) -> datetime:
    """
    Ближайшее ПРЕДСТОЯЩЕЕ воскресенье.
    - Если сегодня воскресенье — возвращает сегодня.
    - Иначе — следующее воскресенье (в будущем).
    """
    if reference_date is None:
        reference_date = datetime.now()

    # weekday(): Пн=0, Вт=1, ..., Вс=6
    days_until_sunday = (6 - reference_date.weekday()) % 7
    return reference_date + timedelta(days=days_until_sunday)


def month_folder_name(date: datetime) -> str:
    """Имя месячной папки: '09 СЕНТЯБРЬ 2026'."""
    return f"{date.month:02d} {RUSSIAN_MONTHS[date.month]} {date.year}"


def date_folder_variants(date: datetime) -> List[str]:
    """Возможные имена папки даты (только цифровые форматы)."""
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
    """
    Находит пути для указанного воскресенья.
    Возвращает словарь {month_folder, date_folder, source, target} или None.
    """
    month_name = month_folder_name(sunday)

    # ⚠️ ВАЖНО: предикат должен быть СИНХРОННЫМ (def, не async def)
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
    """Находит все pptx в папке 'Служение' с датой воскресенья в имени."""
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

