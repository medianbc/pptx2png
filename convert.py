# convert.py — минимальная версия, использующая converter_engine
import sys

missing = []
try:
    import fitz
except ImportError:
    missing.append("pymupdf")
try:
    import pptx
except ImportError:
    missing.append("python-pptx")

if missing:
    print("❌ Ошибка: В системе отсутствуют необходимые библиотеки Python.")
    print("📋 Для их установки выполните команду в терминале:")
    print(f"\n    pip install {' '.join(missing)}\n")
    sys.exit(1)

import argparse
from pathlib import Path
from converter_engine import (
    make_dark_mode,
    pptx_to_pdf_crossplatform,
    pdf_to_png_fast,
    create_zip_stream,
)

def process_file(pptx_path, args):
    print(f"\n🚀 Обработка файла: {pptx_path.name}")

    base_dir = Path(args.output_dir or pptx_path.parent)
    file_output_dir = base_dir / f"{pptx_path.stem}_output"
    file_output_dir.mkdir(parents=True, exist_ok=True)

    current_pptx = pptx_path
    temp_dark_pptx = None
    zip_created = False

    try:
        if args.dark_mode:
            print(" 🎨 Применение темной темы (черный ...)")
            temp_dark_pptx = file_output_dir / f"temp_dark_{pptx_path.name}"
            make_dark_mode(pptx_path, temp_dark_pptx)
            current_pptx = temp_dark_pptx

        pdf_path = pptx_to_pdf_crossplatform(current_pptx, file_output_dir)
        print(" 📄 Конвертация в PDF успешна.")

        print(f" 🖼️ Конвертация в PNG (Качество: {args.quality})...")
        total_slides, generated_pngs = pdf_to_png_fast(
            pdf_path, file_output_dir, args.quality
        )
        print(f" ✅ Успешно создано слайдов: {total_slides}")

        if args.zip:
            zip_name = f"{pptx_path.stem}_output.zip"
            zip_path = base_dir / zip_name
            print(f" 📦 Архивирование слайдов в {zip_name}...")
            create_zip_stream(generated_pngs, zip_path)
            print(" 📦 Создание архива завершено.")
            zip_created = True

        if not args.keep_pdf and pdf_path.exists():
            pdf_path.unlink()
            print(" 🗑️ Временный PDF удален.")

        if temp_dark_pptx and temp_dark_pptx.exists():
            temp_dark_pptx.unlink()

        if args.clean:
            if zip_created:
                print(" 🧹 Удаление папки с несжатыми PNG для экономии места...")
                import shutil
                shutil.rmtree(file_output_dir)
                print(" 🧹 Папка успешно удалена. Оставлен только чистый ZIP-архив.")
            else:
                print(" ⚠️ Предупреждение: Ключ --clean работает только вместе с --zip!")

    except Exception as e:
        print(f" ❌ Ошибка при обработке {pptx_path.name}: {e}")


def main():
    parser = argparse.ArgumentParser(description="Универсальный конвертер PPTX -> PDF -> PNG.")
    parser.add_argument("-f", "--file", type=str, help="Путь к конкретному файлу PPTX.")
    parser.add_argument("-d", "--dir", type=str, default=".", help="Путь к папке с презентациями.")
    parser.add_argument("-o", "--output-dir", type=str, help="Папка для сохранения результатов.")
    parser.add_argument("-a", "--all", action="store_true", help="Обработать ВСЕ файлы pptx в папке.")
    parser.add_argument("--keep-pdf", action="store_true", help="Сохранить промежуточный PDF файл.")
    parser.add_argument("-q", "--quality", choices=["standard", "2k", "4k"], default="standard",
                        help="Качество выходных PNG.")
    parser.add_argument("--dark-mode", action="store_true", help="Включить темную тему.")
    parser.add_argument("--zip", action="store_true", help="Упаковать PNG в ZIP-архив.")
    parser.add_argument("--clean", action="store_true", help="Удалить папку с PNG после ZIP.")

    args = parser.parse_args()
    files_to_process = []

    if args.file:
        specific_file = Path(args.file).resolve()
        if specific_file.exists() and specific_file.suffix.lower() in ['.pptx', '.ppt']:
            files_to_process.append(specific_file)
        else:
            print(f"❌ Ошибка: Файл '{args.file}' не найден.")
            return
    else:
        target_dir = Path(args.dir).resolve()
        if not target_dir.exists():
            print(f"❌ Ошибка: Папка '{args.dir}' не существует.")
            return

        all_pptx = [f for f in target_dir.glob("*.[pP][pP][tT]*") if not f.name.startswith("~$")]

        if not all_pptx:
            print(f"❌ Ошибка: В папке '{target_dir}' не найдено презентаций.")
            return

        if args.all:
            files_to_process = all_pptx
        else:
            files_to_process = [all_pptx[0]]

    for pptx_file in files_to_process:
        process_file(pptx_file, args)

    print("\n🎉 Работа скрипта завершена!")


if __name__ == "__main__":
    main()
