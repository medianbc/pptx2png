# ==========================================
# structure.py — работа с template.yaml
# ==========================================

import logging
from pathlib import Path
from typing import Dict, Any, Optional

import yaml

from yandex_disk import YandexDiskClient, YandexDiskError


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

    return data["structure"]


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

    async def _create_recursive(parent_path: str, node: Dict[str, Any]) -> bool:
        for name, children in node.items():
            folder_path = f"{parent_path}/{name}"
            try:
                ok = await client.ensure_folder(folder_path)
                if not ok:
                    logging.error(f"Не удалось создать папку: {folder_path}")
                    return False
                # Рекурсия в подпапки
                if isinstance(children, dict) and children:
                    if not await _create_recursive(folder_path, children):
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
# ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ — БЕЗОПАСНОЕ ИМЯ ПАПКИ
# ==========================================

def safe_folder_name(filename: str) -> str:
    """
    Преобразует имя файла в безопасное имя папки.
    Пример: 'Служение 20.09.26.pptx' → 'Служение_20.09.26'
    """
    import re
    name = Path(filename).stem
    name = re.sub(r'[^\w\s.-]', '', name)
    name = re.sub(r'\s+', '_', name).strip('_')
    if not name:
        name = "presentation"
    return name[:80]
