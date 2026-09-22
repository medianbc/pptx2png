# ==========================================
# yandex_disk.py — клиент Яндекс.Диска (v1.4)
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
    """
    Асинхронный клиент для REST API Яндекс.Диска.
    Использует ОБЩУЮ aiohttp.ClientSession (передаётся извне),
    чтобы не открывать новое TCP-соединение при каждом запросе.
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
        """
        ✅ FIX (Bug #2): Лёгкий запрос метаданных.
        Возвращает 'dir', 'file' или None (не существует).
        Не тянет _embedded — экономит трафик и время для больших папок.
        Бросает YandexDiskError при транзиентных сбоях (не 404).
        """
        url = f"{YANDEX_API_BASE}/resources"
        params = {
            "path": path,
            "fields": "type",  # только type — без _embedded
        }
        try:
            async with self.session.get(
                url, headers=self.headers, params=params, timeout=15
            ) as resp:
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
        """
        Полные метаданные ресурса с _embedded (список детей).
        Используется, когда действительно нужен список содержимого.
        """
        url = f"{YANDEX_API_BASE}/resources"
        params = {
            "path": path,
            "limit": limit,
            "offset": offset,
            "fields": "name,path,type,size,created,modified,_embedded",
        }
        try:
            async with self.session.get(
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
        """
        ✅ FIX (Bug #2): использует лёгкий resource_type.
        ✅ FIX (Bug #1): пробрасывает YandexDiskError при транзиентных сбоях,
        чтобы вызывающий мог отличить «папки нет» (False) от «не смог проверить» (raise).

        404 → False. Иначе проверяет type == "dir".
        """
        t = await self.resource_type(path)
        return t == "dir"

    # ---------- Создание папок ----------

    async def create_folder(self, path: str) -> bool:
        """
        Создаёт одну папку (без родителей).

        ✅ FIX (Bug #1): при 409 доверяем статусу — это надёжный сигнал
        «уже существует». Fallback GET используется только для логов
        и НЕ может превратить успех в неудачу при временной ошибке сети.

        409 от Яндекс API может означать:
        - DiskPathAlreadyExistsError
        - DiskResourceAlreadyExistsError
        - DiskPathPointsToExistentDirectoryError
        """
        url = f"{YANDEX_API_BASE}/resources"
        params = {"path": path}
        try:
            async with self.session.put(
                url, headers=self.headers, params=params, timeout=30
            ) as resp:
                # 201 = создано
                if resp.status == 201:
                    return True

                # 409 = «уже существует». Доверяем статусу.
                if resp.status == 409:
                    try:
                        body = await resp.text()
                        logging.info(
                            f"create_folder {path}: 409 (already exists). "
                            f"body={body[:200]}"
                        )
                    except Exception:
                        pass

                    # ✅ Fallback GET — только для логов и редкой диагностики.
                    # Что бы ни вернул/бросил GET — при 409 считаем папку существующей.
                    try:
                        exists = await self.folder_exists(path)
                        if not exists:
                            logging.warning(
                                f"create_folder {path}: PUT=409, но GET говорит 'нет'. "
                                f"Доверяем PUT — считаем папку существующей."
                            )
                    except Exception as e:
                        logging.warning(
                            f"create_folder {path}: PUT=409, fallback GET упал ({e}). "
                            f"Доверяем PUT — считаем папку существующей."
                        )
                    return True

                # Любая другая ошибка — логируем тело ответа
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
        Создаёт папку и всех родителей при необходимости.
        Сначала проверяет существование, только потом создаёт.

        ✅ FIX (Bug #1): если проверка существования падает с YandexDiskError —
        логируем и всё равно пробуем создать (не прерываем флоу).
        """
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
