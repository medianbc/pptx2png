# Глубокий разбор PPTX2PNG — 4 ключевых модуля

**Дата:** 2026-10-02  
**Версия dev:** c2184bba  
**Язык:** Python (100% для этого анализа)

---

## 1. bot.py — Стартовая точка, конфиг, жизненный цикл

### Роль в системе
**bot.py** — главный скрипт, который:
1. Загружает конфиг из `config.ini` (токены) и `settings.ini` (параметры)
2. Инициализирует **Telegram Bot** (aiogram) и **Dispatcher**
3. Настраивает логирование на диск (`/dev/shm/pptx2png_tasks/{env}/logs/`)
4. Запускает **cleanup_loop** — фоновый процесс удаления старых задач
5. Подключает Yandex-модули (диск, состояние)

### Структура

```
setup_environment()        # Парсит конфиги, валидирует таймауты
    ↓
setup_logging()           # Создаёт 2 handler'а: bot.log (INFO+), debug.log (DEBUG+)
    ↓
create_bot_and_dispatcher()  # Инит Bot, Dispatcher, YandexDiskClient, UserManager
    ↓
main()                    # Запускает dispatcher.start_polling()
                          # + cleanup_loop как фоновый task
```

### Ключевые переменные и параметры

| Переменная | Источник | Назначение |
|-----------|----------|-----------|
| `env_name` | Имя папки скрипта (dev/test/prod) | Изоляция окружений |
| `shm_dir` | `/dev/shm/pptx2png_tasks/{env_name}` | Временные папки в RAM |
| `log_dir` | `{shm_dir}/logs/` | Логи |
| `bot_token` | `config.ini [Telegram]` | Токен бота |
| `admin_id` | `config.ini [Telegram]` | ID админа для запросов доступа |
| `yandex_token` | `config.ini [YandexDisk]` | OAuth-токен Диска |
| `prompt_timeout_sec` | `settings.ini [Timeouts]` | Таймаут ответа на промпт (дефолт 1800s) |
| `cleanup_interval_sec` | `settings.ini [Timeouts]` | Интерва�� проверки старых задач (дефолт 300s) |
| `cleanup_max_age_sec` | `settings.ini [Timeouts]` | Удалять папки старше (дефолт 7200s) |

### Пример конфига

```ini
# config.ini (в .gitignore)
[Telegram]
BOT_TOKEN = 123:ABC...
ADMIN_ID = 987654321

[YandexDisk]
token = y0_AgAA...

# settings.ini (можно в git)
[Paths]
shm_dir = /dev/shm/pptx2png_tasks
log_dir = /var/log/pptx2png

[YandexDisk]
base_path = /приложения/PPTX2PNG
source_folder = Служение
target_folder = Трансляция
pptx2png_folder = pptx2png
sermon_folder = проповедь - png
opening_folder = Место из Слова Божьего перед служением

[Timeouts]
prompt_timeout_sec = 1800
cleanup_interval_sec = 300
cleanup_max_age_sec = 7200

[SlideCategories]
sermon_keywords = проповед,проповедь
opening_keywords = начало,в начале
prayer_keywords = молитва,молиться
```

### Логирование

```python
# Два файла логов:
/dev/shm/pptx2png_tasks/dev/logs/bot.log      # INFO и выше, rotating (10 МБ × 5)
/dev/shm/pptx2png_tasks/dev/logs/debug.log    # DEBUG и выше, rotating (10 МБ × 3)

# Формат:
2026-10-02 14:32:45,123 - INFO - ✅ Бот успешно инициализирован и готов к работе
```

### cleanup_loop — фоновая очистка

```python
async def cleanup_loop(shm_dir, interval=300, max_age=7200):
    while True:
        await asyncio.sleep(interval)
        # Удаляет папки task_*, yd_task_*, старше max_age секунд
        # НЕ удаляет активные задачи (защита через task_lock_manager, yd_active_tasks)
```

**Почему это важно:**
- Без cleanup `/dev/shm` переполнится брошенными папками
- С cleanup все рабочие папки удаляются, но активные задачи сохраняются

### Инициализация Yandex

```python
yandex_state.config.client = YandexDiskClient(token, http_session)
yandex_state.config.base_path = "/приложения/PPTX2PNG"
yandex_state.config.sermon_keywords = ["проповед", "проповедь"]
```

Это позволяет `yandex_flow.py` получить доступ к сессиям и клиенту через **единый глобальный объект конфига**.

---

## 2. handlers.py — Telegram-роутер, обычная конвертация

### Роль в системе
**handlers.py** обрабатывает основной сценарий: пользователь загружает файл → выбирает слайды → получает ZIP+PDF. Этот модуль НЕ трогает Yandex (для Yandex есть `yandex_flow.py`).

### Главные компоненты

#### TaskLockManager — защита от дублей

```python
class TaskLockManager:
    """
    Предотвращает двойное нажатие кнопки "Конвертировать".
    Каждая задача имеет asyncio.Lock.
    """
    async def acquire(task_id: str) -> bool:
        if lock.locked():
            return False  # Задача уже обрабатывается
        await lock.acquire()
        return True
    
    async def release(task_id: str):
        lock.release()
        self._locks.pop(task_id, None)  # Bug #2: не копим мёртвые локи
```

#### TaskContext — контекстный менеджер для безопасной работы с задачей

```python
class TaskContext:
    """
    Гарантирует:
    1. Файл и папка задачи существуют
    2. Блокировка захвачена (двойное нажатие невозможно)
    3. Гарантированная очистка при выходе
    """
    async def __aenter__(self):
        # Проверяем сессию, папку, файл
        # Захватываем lock
        
    async def __aexit__(self):
        # Освобождаем lock
        # Удаляем сессию
        # Удаляем папку задачи
```

### Основные обработчики

#### 1. `/start` — приветствие

```python
@router.message(CommandStart())
async def cmd_start(message: types.Message, check_access, get_settings_keyboard):
    # Проверяем доступ (whitelist + админское одобрение)
    # Если Yandex настроен: показываем статус и /sunday
    # Выводим клавиатуру качества/PDF
```

#### 2. Загрузка файла → обработчик документов

```python
@router.message(F.document.file_name.lower().endswith(('.pptx', '.ppt')))
async def handle_pptx_document(message: types.Message):
    task_id = generate_task_id(chat_id, user_id, message_id)  # Уникальный ID
    task_dir = Path(SHM_DIR) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / ".owner").write_text(f"{user_id}:{chat_id}")  # Защита от чужих
    
    # Скачиваем файл из Telegram
    sessions[task_id] = {
        "user_id": user_id,
        "chat_id": chat_id,
        "task_dir": task_dir,
        "file_path": file_path,
        "awaiting_selection": True,
        "ranges": []
    }
    
    # Показываем кнопки: "Все слайды" / "Выбрать слайды"
```

#### 3. Выбор слайдов — текстовый ввод

```python
# Текст вроде "1, 3-5, 10-20"
@router.message(F.text & ~F.text.contains("http://"))
async def handle_text_input(message: types.Message):
    # Проверяем, есть ли активная сессия выбора
    if sessions[task_id]["awaiting_selection"]:
        ranges = parse_slides_ranges(message.text)  # Парсим диапазоны
        sessions[task_id]["ranges"] = ranges
        # Показываем превью и кнопку "Конвертировать"
```

#### 4. Конвертация — async-pipeline

```python
async def run_conversion(callback, task_id, SHM_DIR, user_mgr, ...):
    async with converter_semaphore:  # Максимум 2 конвертации одновременно
        async with TaskContext(task_id, callback, SHM_DIR) as ctx:
            # ctx.pptx_path — путь к файлу
            # ctx.task_dir — папка задачи
            
            cfg = user_mgr.get_user_config(user_id)
            
            if all_slides:
                # core_pipeline (utils.py) делает PPTX → PNG → ZIP
                expected_zip = await core_pipeline(pptx_path, message, user_id, user_mgr)
                
                # Отправляем архив пользователю
                await bot.send_document(chat_id, FSInputFile(expected_zip), ...)
            else:
                # Нарезаем на диапазоны
                all_pngs = await convert_all_pngs(pptx_path, temp_dir, cfg["quality"])
                # Для каждого диапазона создаём свой ZIP
```

### Сложность: управление состоянием

**sessions** — глобальный dict в памяти:
```python
sessions = {
    "task_<chat>_<user>_<msg>_<hex>": {
        "user_id": 12345,
        "chat_id": 67890,
        "task_dir": Path(...),
        "file_path": Path("/dev/shm/.../file.pptx"),
        "awaiting_selection": True/False,
        "ranges": [(1, 5), (10, 15)],
    },
    # Yandex-сессии (в yandex_state.sessions):
    "yd_<user>_<chat>": {...},      # Picker-сессия
    "yd_task_<user>_<hex>": {...},  # Задача конвертации
}
```

**Проблема:** при рестарте бота все сессии теряются. Решение: cleanup через 2 часа.

---

## 3. yandex_flow.py — Оркестрация /sunday

### Роль в системе
**yandex_flow.py** (в dev — это фасад для `yandex_flow_core.py`) обрабатывает сценарий **трансляции**:

1. Пользователь нажимает `/sunday`
2. Бот скачивает список PPTX с Диска на ближайшее воскресенье
3. Пользователь выбирает файл (picker)
4. Бот загружает файл, конвертирует в PNG, вытаскивает заметки спикера
5. Бот показывает промпт: "Вот заметки. Где проповедь?" (слайды 5-30?)
6. Пользователь вводит диапазон (или одну категорию)
7. Бот конвертирует слайды в PNG и загружает на Диск в правильные папки

### Архитектура в dev-ветке

```
yandex_flow.py (88 строк)
    ├─ Реэкспортирует из yandex_flow_core
    └─ Реэкспортирует router из yandex_flow_handlers

yandex_flow_core.py (~1500 строк)
    ├─ Утилиты: _classify_slide, _normalize_item_ranges, ...
    ├─ Промпты: _yd_render_sermon_prompt (показывает заметки + кнопки)
    └─ Пайплайн: _yd_convert_and_upload (конвертирует + выкладывает)

yandex_flow_handlers.py
    └─ router: обработчики callback_query для Yandex
```

### Ключевые функции

#### render_sermon_prompt — показывает промпт

```python
async def render_sermon_prompt(
    task_id: str,
    item: dict,  # {"file_name": "...", "total_slides": 186, "content": "..."}
    status_msg,
    reply_fn  # message.reply
):
    """
    Показывает заметки спикера и просит указать диапазон проповеди.
    
    Пример:
    
    📄 file.pptx (186 слайдов)
    
    📝 **Заметки спикера:**
    ```
    Слайды 1-5: Начало
    Слайды 6-50: Проповедь
    Слайды 51-186: Молитва
    ```
    
    🎯 **Укажите диапазон проповеди:**
    Ответьте: `6-50` или `6` (для одного слайда)
    
    [кнопки: Проповедь только | Слайды только | Оба архива]
    """
    
    # Ждёт ввода пользователя или нажатия кнопки
    # Таймаут: prompt_timeout_sec (1800s по умолчанию)
```

#### is_sermon_slide — классификация слайда

```python
def is_sermon_slide(item: dict, slide_idx: int) -> bool:
    """
    Проверяет, содержит ли слайд ключевые слова проповеди.
    
    Проверяет:
    1. Попал ли слайд в ручной диапазон item["ranges"]?
    2. Содержит ли заметка спикера слово "проповед"?
    
    Возвращает True, если это слайд проповеди.
    """
```

#### convert_and_upload — финальный пайплайн

```python
async def convert_and_upload(
    bot: Bot,
    task_id: str,
    status_msg,
):
    """
    1. Конвертирует файлы в PNG (каждому своё качество)
    2. Разбивает на категории: проповедь, начало, остальные
    3. Создаёт ZIP-архивы:
       - file_проповедь.zip → /Трансляция/.../проповедь - png/
       - file_слайды.zip  → /pptx2png/file/
    4. Загружает на Диск
    5. Присылает ссылки пользователю
    """
```

### Состояние Yandex-сессии

```python
pending = {
    "owner_user_id": 12345,
    "chat_id": 67890,
    "prepared": [
        {
            "file_name": "sunday_1.pptx",
            "file_path": "/dev/shm/.../sunday_1.pptx",
            "total_slides": 186,
            "content": "Слайды 1-5: Начало...",  # Заметки спикера
            "ranges": [(6, 50)],  # Диапазон проповеди (пользователь ввёл)
            "start": 6, "end": 50,
            "matches": [6, 7, 8, ..., 50],  # Слайды, попавшие в диапазон
            "convert_mode": "sermon",  # "sermon" | "all" | "other"
            "confirmed": True,
        },
        # ... больше файлов из этого воскресенья
    ],
    "awaiting_range_for_idx": 0,  # Ждём ввода для файла с индексом 0
    "prompt_timeout_task": <Task>,  # Watchdog таймаута
    "cancelled": False,
}
```

### Таймаут промпта — watchdog

```python
async def prompt_timeout_watchdog(
    task_id: str,
    timeout_sec: int,
    nonce: str,
):
    """
    Если пользователь не ответил на промпт за timeout_sec:
    1. Помечает текущий файл как пропущенный
    2. Движется к следующему файлу
    3. Или начинает конвертацию (если файлы закончились)
    """
    await asyncio.sleep(timeout_sec)
    # ... логика таймаута
```

---

## 4. converter_engine.py — PPTX → PDF → PNG

### Роль в системе
**converter_engine.py** отвечает за техническую часть: преобразование презентации в картинки.

### Pipeline

```
PPTX/PPT → [LibreOffice] → PDF → [PyMuPDF] → PNG[] → ZIP
```

### Ключевые функции

#### make_dark_mode — тёмный режим

```python
def make_dark_mode(pptx_path, temp_output_path):
    """
    Создаёт тёмную версию презентации:
    1. Чёрный фон (RGB 0,0,0)
    2. Белый текст (RGB 255,255,255)
    3. Удаляет большие картинки (>80% слайда)
    """
    prs = Presentation(pptx_path)
    for slide in prs.slides:
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = BLACK
        
        for shape in slide.shapes:
            if is_large_picture(shape):
                slide.shapes.remove(shape)
            if has_text:
                make_text_white(shape)
```

#### pptx_to_pdf_crossplatform — конвертация в PDF

```python
def pptx_to_pdf_crossplatform(pptx_path: Path, output_dir: Path) -> Path:
    """
    Вызывает LibreOffice headless:
    soffice --headless --convert-to pdf --outdir /tmp/ file.pptx
    
    Зачем LibreOffice?
    • Работает с PPTX-файлами любого качества
    • python-pptx может не открыть закрытый OOXML
    • Результат — PDF, который уже безопасно рендерить
    """
```

#### pdf_to_png_fast — рендер PNG с качеством

```python
def pdf_to_png_fast(pdf_path: Path, output_dir: Path, quality: str) -> Tuple[int, List[Path]]:
    """
    Использует PyMuPDF (fitz) для рендера PDF в PNG.
    
    quality:
    • "standard" → zoom = 2.0 (1920×1080 → экран)
    • "2k"       → zoom = 2560 / long_side (2560×1440+)
    • "4k"       → zoom = 3840 / long_side (4K)
    
    Возвращает: (186 слайдов, [slide_001.png, slide_002.png, ...])
    """
```

#### convert_all_pngs — высокоуровневая обёртка

```python
async def convert_all_pngs(pptx_path: Path, output_dir: Path, quality: str) -> Tuple[List[Path], Path]:
    """
    1. Если .ppt → конвертирует в .pptx через LibreOffice
    2. Dark mode (если нужен)
    3. PPTX → PDF
    4. PDF → PNG[]
    5. Удаляет PDF и temp-файлы
    6. Возвращает список PNG
    
    Запускается в asyncio.to_thread(), чтобы не блокировать event loop.
    """
    def _sync_convert():
        if pptx_path.suffix.lower() == '.ppt':
            pptx_converted = ppt_to_pptx_crossplatform(pptx_path, output_dir)
        else:
            pptx_converted = pptx_path
        
        temp_dark_pptx = output_dir / f"temp_dark_{pptx_converted.name}"
        make_dark_mode(pptx_converted, temp_dark_pptx)
        
        pdf_path = pptx_to_pdf_crossplatform(temp_dark_pptx, output_dir)
        total_slides, png_paths = pdf_to_png_fast(pdf_path, output_dir, quality)
        
        # Очистка
        pdf_path.unlink()
        temp_dark_pptx.unlink()
        # ❌ НЕ удаляем pptx_converted (нужен для заметок в yandex_flow)
        
        return png_paths, pptx_converted
    
    pngs, used_pptx = await asyncio.to_thread(_sync_convert)
    return pngs, used_pptx
```

#### count_slides_via_libreoffice — fallback для подсчёта слайдов

```python
def count_slides_via_libreoffice(pptx_path: Path, work_dir: Path) -> Optional[int]:
    """
    Если python-pptx не может открыть закрытый OOXML:
    1. LibreOffice конвертирует в PDF
    2. PyMuPDF считает страницы
    3. Результат — число слайдов
    """
```

### Качество и разрешение

```python
def get_resolution_multiplier(quality_str, page):
    rect = page.rect
    long_side = max(rect.width, rect.height)
    
    if quality_str == "2k":
        target = 2560
    elif quality_str == "4k":
        target = 3840
    else:
        return 2.0
    
    return target / long_side
```

**Пример:**
- Слайд 1920×1440 (16:9), quality="4k"
- long_side = 1920
- zoom = 3840 / 1920 = 2.0 → 3840×2880

### ZIP — функция create_zip_stream

```python
def create_zip_stream(file_paths: List[Path], output_path: Path, compress_level: int = 6) -> Path:
    """
    Создаёт архив:
    slide_001.png
    slide_002.png
    ...
    slide_186.png
    
    compress_level = 6 (medium), может быть 1-9
    """
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for fpath in file_paths:
            zf.write(fpath, arcname=fpath.name)  # Сохраняет только имя, без пути
```

---

## Взаимодействие модулей

```
bot.py
  ├─ Загружает config.ini + settings.ini
  ├─ Инит YandexDiskClient → yandex_state
  ├─ Инит UserManager
  ├─ Подключает handlers.router
  │   └─ handlers.py
  │       ├─ Обработчики /start, загрузка файлов, выбор слайдов
  │       ├─ run_conversion() → converter_engine.convert_all_pngs()
  │       ├─ Подключает yandex_router
  │       │   └─ yandex_flow.py (facade)
  │       │       └─ yandex_flow_core.py
  │       │           ├─ _yd_prepare_files() — скачивает с Диска, готовит
  │       │           ├─ _yd_render_sermon_prompt() — показывает промпт
  │       │           └─ _yd_convert_and_upload() — конвертирует + выкладывает
  │       │
  │       └─ converter_engine.py
  │           ├─ convert_all_pngs() → LibreOffice → PDF → PyMuPDF → PNG
  │           └─ make_dark_mode()
  │
  └─ cleanup_loop() — удаляет старые папки
```

---

## Типичные сценарии

### Сценарий 1: Обычная конвертация (handlers.py)

```
Пользователь: /start
Bot: Приветствие + кнопки качества

Пользователь: [загружает file.pptx]
handlers.handle_pptx_document():
  ├─ Создаёт task_id = "task_67890_12345_9876_abc123"
  ├─ task_dir = "/dev/shm/pptx2png_tasks/dev/task_67890_12345_9876_abc123"
  ├─ Скачивает файл
  ├─ sessions[task_id] = {..., awaiting_selection=True}
  └─ Показывает кнопки "Все слайды" / "Выбрать"

Пользователь: [нажимает "Все слайды"]
run_conversion(all_slides=True):
  ├─ Захватывает converter_semaphore (макс 2 одновременно)
  ├─ core_pipeline() (utils.py):
  │   ├─ convert_all_pngs() → 186 PNG
  │   └─ Создаёт ZIP, возвращает архив
  ├─ Отправляет ZIP пользователю
  └─ Удаляет task_dir

Пользователь: Получает "file.zip" с 186 PNG
```

### Сценарий 2: Yandex-трансляция (/sunday)

```
Пользователь: /sunday
handlers._cmd_sunday() → yandex_flow_handlers.handle_sunday_command():
  ├─ Скачивает /Служение/2026-10-05/ с Диска
  ├─ Находит 3 PPTX файла
  └─ Показывает picker (кнопки)

Пользователь: [выбирает sunday_1.pptx]
yandex_flow_handlers.handle_yd_pick_file():
  ├─ _yd_prepare_files() — скачивает, конвертирует, готовит заметки
  ├─ Извлекает текст из заметок спикера
  ├─ Определяет подозрительные слайды (keyword matching)
  └─ render_sermon_prompt() — показывает:
      """
      📄 sunday_1.pptx (186 слайдов)
      
      📝 Заметки спикера:
      Слайды 1-10: Начало богослужения
      Слайды 11-100: ПРОПОВЕДЬ о любви
      Слайды 101-186: Молитва и закрытие
      
      🎯 Укажите диапазон ПРОПОВЕДИ (или нажмите кнопку):
      """
      
      [кнопка: Проповедь только | Все слайды | Начало только]

Пользователь: [вводит "11-100"]
handlers.handle_text_input() → is_sermon_slide():
  ├─ Сохраняет ranges = [(11, 100)]
  ├─ Заполняет matches = [11, 12, ..., 100]
  └─ render_sermon_prompt() снова, но теперь с режимом выбора:
      [кнопка: Проповедь (11-100) | Остальные (1-10, 101-186) | Оба архива]

Пользователь: [нажимает "Оба архива"]
_yd_convert_and_upload():
  ├─ convert_all_pngs() — 186 PNG
  ├─ Разбивает на категории:
  │   ├─ sermon: слайды 11-100 (90 PNG)
  │   ├─ other: слайды 1-10, 101-186 (96 PNG)
  ├─ Создаёт ZIP:
  │   ├─ sunday_1_проповедь.zip (90 PNG)
  │   └─ sunday_1_слайды.zip (186 PNG)
  ├─ Загружает на Диск:
  │   ├─ /Трансляция/2026-10-05/.../проповедь - png/ ← проповедь.zip
  │   └─ /pptx2png/sunday_1/ ← слайды.zip
  └─ Присылает ссылки пользователю

Пользователь: Получает сообщение:
"""
✅ Файлы готовы!

📦 Проповедь (90 слайдов):
https://disk.yandex.ru/d/...

📦 Слайды (186 слайдов):
https://disk.yandex.ru/d/...
"""
```

---

## Проблемы и компромиссы

| Проблема | Почему | Решение |
|----------|--------|---------|
| sessions в памяти | Быстро, но теряются при рестарте | cleanup через 2 часа (не идеально) |
| Один процесс | Нет масштабирования | systemd supervisor / PM2 |
| Нет параллельной загрузки ZIP | Сложно координировать | Загружаем архивы по одному |
| dark_mode медленный | python-pptx модифицирует весь PPTX | Async wrapper (to_thread) |
| LibreOffice зависимость | Нужен системный пакет | apt-get install libreoffice (на Raspberry Pi медленно) |

---

## Заключение

**PPTX2PNG** — хорошо организованный монолит:

1. **bot.py** — загрузка конфига, инит, логирование, lifecycle
2. **handlers.py** — основная логика Telegram, обычная конвертация
3. **yandex_flow.py** — фасад для сценария трансляции (сложная оркестрация)
4. **converter_engine.py** — техническая часть: PPTX → PNG

**Сильные стороны:**
- Четкое разделение ответственности
- Защита от дублей (TaskLockManager, yd_active_tasks)
- Taймауты и cleanup (не теряет ресурсы)
- Асинхронность (не блокирует event loop)

**Слабые стороны:**
- sessions в памяти (нет persistence)
- Нет тестов
- Один процесс (нет горизонтального масштабирования)
- LibreOffice на Raspberry Pi медленный
