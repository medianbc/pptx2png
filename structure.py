# ==========================================
# structure.py — работа с template.yaml
# ==========================================

import logging
import re
from pathlib import Path
from typing import Dict, Any, Optional

import yaml

from yandex_disk import YandexDiskClient, YandexDiskError


# ==========================================
# ВАЛИДАЦИЯ СТРУКТУРЫ
# ==========================================

def _validate_structure(node: Any, path: str = "structure", visited: Optional[set] = None) -> bool:
    """
    Рекурсивно проверяет структуру: dict[str, dict | None].
    Защита от циклических ссылок (YAML anchors).
    """
    if visited is None:
        visited = set()

    if not isinstance(node, dict):
        logging.error(
            f"Ошибка в template.yaml: '{path}' должен быть словарём, "
            f"получено {type(node).__name__}"
        )
        return False

    # ✅ Защита от рекурсии
    node_id = id(node)
    if node_id in visited:
        logging.error(
            f"Ошибка в template.yaml: обнаружена циклическая ссылка в '{path}'"
        )
        return False
    visited.add(node_id)

    try:
        for key, value in node.items():
            if not isinstance(key, str):
                logging.error(
                    f"Ошибка в template.yaml: ключ '{key}' в '{path}' должен быть строкой"
                )
                return False

            if value is None:
                continue

            if isinstance(value, dict):
                if not _validate_structure(value, f"{path}.{key}", visited):
                    return False
            else:
                logging.error(
                    f"Ошибка в template.yaml: значение '{path}.{key}' "
                    f"должно быть словарём или пустым, получено {type(value).__name__}"
                )
                return False
        return True
    finally:
        visited.discard(node_id)


# ==========================================
# ЗАГРУЗКА ШАБЛОНА
# ==========================================

def load_template(path: Path) -> Optional[Dict[str, Any]]:
    """
    Загружает template.yaml.
    Возвращает словарь структуры или None при ошибке.
    """
    if not path.exists():
        logging.error(f"Файл шаблона не найден: {path}")
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        logging.error(f"Ошибка парсинга YAML {path}: {e}")
        return None
    except Exception as e:
        logging.error(f"Ошибка чтения {path}: {e}")
        return None

    if not isinstance(data, dict) or "structure" not in data:
        logging.error(f"Неверный формат template.yaml: нет ключа 'structure'")
        return None

    structure = data["structure"]

    # ✅ Проверяем, что structure — словарь и все вложенные значения корректны
    if not _validate_structure(structure):
        logging.error("Неверная структура template.yaml — см. логи выше")
        return None

    return structure


# ==========================================
# СОЗДАНИЕ СТРУКТУРЫ НА ЯНДЕКС.ДИСКЕ
# ==========================================

async def create_structure(
    client: YandexDiskClient,
    base_target_path: str,
    structure: Dict[str, Any],
) -> bool:
    """
    Рекурсивно создаёт структуру папок внутри base_target_path.
    Возвращает True при успехе.
    """
    if not structure:
        return True

    # ✅ Защита от невалидного входа
    if not isinstance(structure, dict):
        logging.error(
            f"create_structure: structure должен быть словарём, "
            f"получено {type(structure).__name__}"
        )
        return False

    async def _create_recursive(parent_path: str, node: Dict[str, Any]) -> bool:
        if not isinstance(node, dict):
            logging.error(
                f"create_structure: узел '{parent_path}' должен быть словарём, "
                f"получено {type(node).__name__}"
            )
            return False

        for name, children in node.items():
            if not isinstance(name, str):
                logging.error(
                    f"create_structure: имя папки должно быть строкой, "
                    f"получено {type(name).__name__}"
                )
                return False

            folder_path = f"{parent_path}/{name}"
            try:
                ok = await client.ensure_folder(folder_path)
                if not ok:
                    logging.error(f"Не удалось создать папку: {folder_path}")
                    return False

                if children is None:
                    continue
                if isinstance(children, dict) and children:
                    if not await _create_recursive(folder_path, children):
                        return False
                elif not isinstance(children, dict):
                    logging.error(
                        f"create_structure: '{folder_path}' содержит невалидное "
                        f"значение {type(children).__name__}"
                    )
                    return False
            except YandexDiskError as e:
                logging.error(f"Ошибка API при создании {folder_path}: {e}")
                return False
            except Exception as e:
                logging.error(f"Ошибка при создании {folder_path}: {e}")
                return False
        return True

    return await _create_recursive(base_target_path, structure)


# ==========================================
# БЕЗОПАСНОЕ ИМЯ ПАПКИ
# ==========================================

def safe_folder_name(filename: str) -> str:
    """
    Преобразует имя файла в безопасное имя папки.
    Пример: 'Служение 20.09.26.pptx' → 'Служение_20.09.26'
    """
    name = Path(filename).stem
    name = re.sub(r'[^\w\s.-]', '', name)
    name = re.sub(r'\s+', '_', name).strip('_')
    if not name:
        name = "presentation"
    return name[:80]
