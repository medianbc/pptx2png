import asyncio
import io
import logging
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from obs_local import prepare_songs


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PNG = prepare_songs.PNG_SIGNATURE + b"test-png"


def _minimal_docx_bytes() -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as document:
        document.writestr("word/document.xml", b"<document/>")
    return data.getvalue()


def _paragraph(text: str) -> ElementTree.Element:
    paragraph = ElementTree.Element(f"{{{W_NS}}}p")
    run = ElementTree.SubElement(paragraph, f"{{{W_NS}}}r")
    value = ElementTree.SubElement(run, f"{{{W_NS}}}t")
    value.text = text
    return paragraph


def _table(rows: list[list[str]]) -> ElementTree.Element:
    table = ElementTree.Element(f"{{{W_NS}}}tbl")
    for row_values in rows:
        row = ElementTree.SubElement(table, f"{{{W_NS}}}tr")
        for text in row_values:
            cell = ElementTree.SubElement(row, f"{{{W_NS}}}tc")
            cell.append(_paragraph(text))
    return table


def _create_docx(path: Path) -> None:
    document = ElementTree.Element(f"{{{W_NS}}}document")
    body = ElementTree.SubElement(document, f"{{{W_NS}}}body")
    body.append(_paragraph("Программа собрания на 04.10.2026 г."))
    body.append(
        _table(
            [
                ["п.", "кто говорит (вступление, молитва)", "название песни"],
                ["7", "Николай", "Песня первая"],
                ["12", "Анна", "Песня вторая"],
                ["13", "", "?????"],
            ]
        )
    )
    body.append(_paragraph("АРХИВ"))
    body.append(_paragraph("Программа собрания на 27.09.2026 г."))
    body.append(
        _table(
            [
                ["п.", "кто говорит", "название песни"],
                ["1", "", "Старая песня"],
            ]
        )
    )

    with zipfile.ZipFile(path, "w") as docx:
        docx.writestr(
            "word/document.xml",
            ElementTree.tostring(document, encoding="utf-8", xml_declaration=True),
        )


class ExtractSongTitlesTests(unittest.TestCase):
    def test_extracts_only_title_column_in_document_order_for_exact_date(self):
        with tempfile.TemporaryDirectory() as directory:
            docx_path = Path(directory) / "program.docx"
            _create_docx(docx_path)

            songs = prepare_songs.extract_song_titles(
                docx_path, date(2026, 10, 4)
            )

        self.assertEqual(songs, ["Песня первая", "Песня вторая"])

    def test_missing_date_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            docx_path = Path(directory) / "program.docx"
            _create_docx(docx_path)

            with self.assertRaisesRegex(RuntimeError, "не найден блок"):
                prepare_songs.extract_song_titles(
                    docx_path, date(2026, 10, 11)
                )


class ConfigTests(unittest.TestCase):
    def test_shared_project_token_is_used_before_obs_local_config(self):
        with tempfile.TemporaryDirectory() as directory:
            project_dir = Path(directory)
            obs_dir = project_dir / "obs_local"
            obs_dir.mkdir()
            (obs_dir / "settings.ini").write_text(
                "[OBS Local paths]\nroot_path = ./obs_png\n",
                encoding="utf-8",
            )
            (project_dir / "config.ini").write_text(
                "[YandexDisk]\ntoken = shared-token\n",
                encoding="utf-8",
            )
            (obs_dir / "config.ini").write_text(
                "[YandexDisk]\n",
                encoding="utf-8",
            )

            with (
                patch.object(prepare_songs, "BASE_DIR", obs_dir),
                patch.object(prepare_songs, "PROJECT_DIR", project_dir),
            ):
                config = prepare_songs.load_config()

        self.assertEqual(config["yandex_token"], "shared-token")

    def test_obs_local_token_is_used_as_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            project_dir = Path(directory)
            obs_dir = project_dir / "obs_local"
            obs_dir.mkdir()
            (obs_dir / "settings.ini").write_text(
                "[OBS Local paths]\nroot_path = ./obs_png\n",
                encoding="utf-8",
            )
            (obs_dir / "config.ini").write_text(
                "[YandexDisk]\ntoken = local-token\n",
                encoding="utf-8",
            )

            with (
                patch.object(prepare_songs, "BASE_DIR", obs_dir),
                patch.object(prepare_songs, "PROJECT_DIR", project_dir),
            ):
                config = prepare_songs.load_config()

        self.assertEqual(config["yandex_token"], "local-token")


class SongOutputTests(unittest.TestCase):
    def test_docs_view_link_is_rejected_as_not_a_disk_public_key(self):
        docs_url = (
            "https://docs.yandex.ru/view/d/"
            "GJamJ029JyWOsKXfRK5ZRSPegnqahzm72s0qoIz-cKg6SjhCN3pHZXhVQQ"
        )

        with self.assertRaisesRegex(
            RuntimeError, "не является public_key Яндекс.Диска"
        ):
            prepare_songs._normalize_program_public_key(docs_url)

    def test_disk_public_link_is_preserved(self):
        disk_url = "https://disk.yandex.ru/d/example_public_key"

        self.assertEqual(
            prepare_songs._normalize_program_public_key(disk_url),
            disk_url,
        )

    def test_disk_image_style_public_link_is_preserved(self):
        disk_url = "https://disk.yandex.ru/i/S4CRCWdrzxYmVg"

        self.assertEqual(
            prepare_songs._normalize_program_public_key(disk_url),
            disk_url,
        )

    def test_unsupported_yandex_url_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "публичной HTTPS-ссылкой"):
            prepare_songs._normalize_program_public_key(
                "https://yandex.ru/some/other/page"
            )

    def test_logging_creates_info_and_debug_files(self):
        root_logger = logging.getLogger()
        original_handlers = root_logger.handlers[:]
        original_level = root_logger.level
        try:
            with tempfile.TemporaryDirectory() as directory:
                log_dir = Path(directory)
                prepare_songs.setup_logging(log_dir)
                logging.info("info diagnostic test")
                logging.debug("debug diagnostic test")
                for handler in root_logger.handlers:
                    handler.flush()

                self.assertIn("info diagnostic test", (log_dir / "bot.log").read_text(
                    encoding="utf-8"
                ))
                self.assertIn(
                    "debug diagnostic test",
                    (log_dir / "debug.log").read_text(encoding="utf-8"),
                )
        finally:
            for handler in root_logger.handlers[:]:
                root_logger.removeHandler(handler)
                handler.close()
            for handler in original_handlers:
                root_logger.addHandler(handler)
            root_logger.setLevel(original_level)

    def test_public_program_http_error_includes_api_diagnostics_without_link(self):
        public_url = "https://disk.yandex.ru/d/example_public_key"

        class FakeResponse:
            status = 400

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def text(self):
                return (
                    '{"error":"DiskNotFoundError",'
                    '"description":"Public resource not found: '
                    + public_url
                    + '"}'
                )

        class FakeSession:
            def get(self, *_args, **_kwargs):
                return FakeResponse()

        async def execute():
            with self.assertLogs("obs_local.prepare_songs", level="DEBUG") as logs:
                with self.assertRaisesRegex(
                    RuntimeError, "HTTP 400.*DiskNotFoundError"
                ) as error:
                    await prepare_songs.download_program_docx(
                        FakeSession(), public_url, Path("program.docx")
                    )
            self.assertNotIn(public_url, str(error.exception))
            self.assertNotIn(public_url, "\n".join(logs.output))
            self.assertIn("Public resource not found", "\n".join(logs.output))
            self.assertIn("disk.yandex.ru/d/[share-id redacted]", "\n".join(logs.output))

        asyncio.run(execute())

    def test_download_rejects_docs_view_link_before_api_call(self):
        docs_url = (
            "https://docs.yandex.ru/view/d/"
            "GJamJ029JyWOsKXfRK5ZRSPegnqahzm72s0qoIz-cKg6SjhCN3pHZXhVQQ"
        )

        class FakeSession:
            def get(self, *_args, **_kwargs):
                raise AssertionError("API must not be called for docs viewer URL")

        async def execute():
            session = FakeSession()
            with tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / "program.docx"
                with self.assertRaisesRegex(
                    RuntimeError, "публичную ссылку файла"
                ):
                    await prepare_songs.download_program_docx(
                        session, docs_url, destination
                    )

        asyncio.run(execute())

    def test_download_passes_disk_public_link_to_api(self):
        public_url = "https://disk.yandex.ru/d/example_public_key"

        class FakeContent:
            async def iter_chunked(self, _size):
                yield _minimal_docx_bytes()

        class FakeResponse:
            def __init__(self, payload=None):
                self.status = 200
                self.payload = payload
                self.headers = {}
                self.content = FakeContent()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def json(self):
                return self.payload

        class FakeSession:
            def __init__(self):
                self.calls = []
                self.responses = [
                    FakeResponse(
                        {"href": "https://downloader.disk.yandex.ru/file"}
                    ),
                    FakeResponse(),
                ]

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return self.responses.pop(0)

        async def execute():
            session = FakeSession()
            with tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / "program.docx"
                await prepare_songs.download_program_docx(
                    session, public_url, destination
                )

                self.assertEqual(
                    session.calls[0][1]["params"]["public_key"], public_url
                )
                self.assertEqual(destination.read_bytes(), _minimal_docx_bytes())

        asyncio.run(execute())

    def test_download_passes_disk_i_link_to_api(self):
        public_url = "https://disk.yandex.ru/i/S4CRCWdrzxYmVg"

        class FakeContent:
            async def iter_chunked(self, _size):
                yield _minimal_docx_bytes()

        class FakeResponse:
            def __init__(self, payload=None):
                self.status = 200
                self.payload = payload
                self.headers = {}
                self.content = FakeContent()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def json(self):
                return self.payload

        class FakeSession:
            def __init__(self):
                self.calls = []
                self.responses = [
                    FakeResponse(
                        {"href": "https://downloader.disk.yandex.ru/file"}
                    ),
                    FakeResponse(),
                ]

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return self.responses.pop(0)

        async def execute():
            session = FakeSession()
            with tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / "program.docx"
                await prepare_songs.download_program_docx(
                    session, public_url, destination
                )
                self.assertEqual(
                    session.calls[0][1]["params"]["public_key"], public_url
                )

        asyncio.run(execute())

    def test_download_rejects_non_docx_response(self):
        public_url = "https://disk.yandex.ru/i/example_image"

        class FakeContent:
            async def iter_chunked(self, _size):
                yield b"<html>captcha page</html>"

        class FakeResponse:
            def __init__(self, payload=None):
                self.status = 200
                self.payload = payload
                self.headers = {}
                self.content = FakeContent()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def json(self):
                return self.payload

        class FakeSession:
            def __init__(self):
                self.responses = [
                    FakeResponse(
                        {"href": "https://downloader.disk.yandex.ru/file"}
                    ),
                    FakeResponse(),
                ]

            def get(self, *_args, **_kwargs):
                return self.responses.pop(0)

        async def execute():
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(RuntimeError, "не является DOCX"):
                    await prepare_songs.download_program_docx(
                        FakeSession(),
                        public_url,
                        Path(directory) / "program.docx",
                    )

        asyncio.run(execute())

    def test_zip_pngs_are_written_in_natural_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "song.zip"
            output_dir = root / "output"
            output_dir.mkdir()
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("slides/slide_10.png", PNG)
                archive.writestr("slides/slide_2.png", PNG)

            count = prepare_songs._copy_zip_pngs(archive_path, output_dir)

            self.assertEqual(count, 2)
            self.assertEqual(
                sorted(path.name for path in output_dir.glob("*.png")),
                ["slide_001.png", "slide_002.png"],
            )

    def test_run_publishes_song_folders_and_preserves_previous_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program_path = root / "program.docx"
            _create_docx(program_path)
            zip_dir = root / "zip"
            pptx_dir = root / "pptx"
            zip_dir.mkdir()
            pptx_dir.mkdir()
            with zipfile.ZipFile(zip_dir / "Песня первая.zip", "w") as archive:
                archive.writestr("slide_1.png", PNG)
            output_dir = root / "Трансляция" / "Песни"
            output_dir.mkdir(parents=True)
            (output_dir / "old-result.txt").write_text("preserve", encoding="utf-8")
            config = {
                "program_url": "",
                "zip_dir": zip_dir,
                "pptx_dir": pptx_dir,
                "yandex_zip_paths": [],
                "yandex_pptx_paths": [],
                "output_dir": output_dir,
                "quality": "standard",
                "yandex_token": "",
            }

            with patch.object(prepare_songs, "load_config", return_value=config):
                results = asyncio.run(
                    prepare_songs.run(date(2026, 10, 4), program_path)
                )

            first_song = output_dir / "01 - Песня первая"
            second_song = output_dir / "02 - Песня вторая"
            self.assertEqual(results[0].status, "ready")
            self.assertEqual(results[0].png_count, 1)
            self.assertEqual(results[1].status, "not_found")
            self.assertTrue((first_song / "slide_001.png").is_file())
            self.assertTrue(second_song.is_dir())
            self.assertEqual(list(second_song.iterdir()), [])

            backups = list(output_dir.parent.glob(".Песни-backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(
                (backups[0] / "old-result.txt").read_text(encoding="utf-8"),
                "preserve",
            )

    def test_corrupt_zip_falls_back_to_local_pptx(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program_path = root / "program.docx"
            _create_docx(program_path)
            zip_dir = root / "zip"
            pptx_dir = root / "pptx"
            zip_dir.mkdir()
            pptx_dir.mkdir()
            (zip_dir / "Песня первая.zip").write_bytes(b"not a zip")
            pptx_path = pptx_dir / "Песня первая.pptx"
            pptx_path.write_bytes(b"mock pptx")
            output_dir = root / "Трансляция" / "Песни"
            config = {
                "program_url": "",
                "zip_dir": zip_dir,
                "pptx_dir": pptx_dir,
                "yandex_zip_paths": [],
                "yandex_pptx_paths": [],
                "output_dir": output_dir,
                "quality": "standard",
                "yandex_token": "",
            }

            async def fake_convert(_source, render_dir, _quality):
                png = render_dir / "slide_1.png"
                png.write_bytes(PNG)
                return [png], pptx_path

            with (
                patch.object(prepare_songs, "load_config", return_value=config),
                patch.object(
                    prepare_songs, "convert_all_pngs", side_effect=fake_convert
                ),
            ):
                results = asyncio.run(
                    prepare_songs.run(date(2026, 10, 4), program_path)
                )

            self.assertEqual(results[0].status, "ready")
            self.assertEqual(results[0].source, str(pptx_path))
            self.assertTrue(
                (output_dir / "01 - Песня первая" / "slide_001.png").is_file()
            )


if __name__ == "__main__":
    unittest.main()
