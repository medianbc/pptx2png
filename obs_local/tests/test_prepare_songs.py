import asyncio
import contextlib
import io
import logging
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch
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


class SongMatchingTests(unittest.TestCase):
    def test_match_quality_accepts_decomposed_unicode_in_either_title(self):
        for composed, decomposed in (
            ("Долг мой уплатил", "Долг мои\u0306 уплатил"),
            ("Всё прекрасно", "Все\u0308 прекрасно"),
        ):
            for filename, title in ((composed, decomposed), (decomposed, composed)):
                with self.subTest(filename=filename, title=title):
                    self.assertEqual(
                        prepare_songs._song_match_quality(filename, title), 2
                    )

    def test_local_search_matches_decomposed_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for suffix in (".zip", ".pptx"):
                candidate = root / f"Долг мои\u0306 уплатил{suffix}"
                candidate.touch()
                with self.subTest(suffix=suffix):
                    self.assertEqual(
                        prepare_songs._find_local_matches(
                            root, "Долг мой уплатил", suffix
                        ),
                        [candidate],
                    )

    def test_match_quality_accepts_strong_partial_title_and_yo_variant(self):
        self.assertEqual(
            prepare_songs._song_match_quality(
                "Бог всего творения", "Бог всего творения вечен"
            ),
            1,
        )
        self.assertEqual(
            prepare_songs._song_match_quality(
                "Все прекрасно",
                "Всё прекрасно",
            ),
            2,
        )

    def test_match_quality_rejects_weak_partial_title(self):
        self.assertEqual(
            prepare_songs._song_match_quality(
                "Бог всего", "Бог всего творения вечен"
            ),
            0,
        )

    def test_exact_local_match_takes_precedence_over_partial_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exact = root / "Бог всего творения.zip"
            partial = root / "Бог всего творенья - live.zip"
            exact.touch()
            partial.touch()

            matches = prepare_songs._find_local_matches(
                root, "Бог всего творения", ".zip"
            )

        self.assertEqual(matches, [exact])


class ConfigTests(unittest.TestCase):
    def test_priority_selects_only_its_cloud_and_keeps_legacy_configs_working(self):
        google_url = GoogleProgramDownloadTests.URL
        yandex_url = "https://disk.yandex.ru/d/synthetic-program"
        cases = (
            ("", yandex_url, "", "", yandex_url, "yandex"),
            ("", google_url, "", "", google_url, "google"),
            ("google", yandex_url, google_url, yandex_url, google_url, "google"),
            ("yandex", google_url, google_url, yandex_url, yandex_url, "yandex"),
            ("google", yandex_url, "", yandex_url, "", "google"),
            ("yandex", yandex_url, google_url, "", yandex_url, "yandex"),
        )
        for priority, legacy, google, yandex, expected, expected_cloud in cases:
            with self.subTest(priority=priority, legacy=legacy), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "settings.ini"
                text = (
                    "[OBS Local paths]\nroot_path = ./png\n"
                    "[Song preparation]\n"
                    f"program_url = {legacy}\n"
                    f"program_google_url = {google}\n"
                    f"program_yandex_url = {yandex}\n"
                )
                if priority:
                    text += f"priority_cloud_disk = {priority}\n"
                path.write_text(text, encoding="utf-8")
                with (
                    patch.object(prepare_songs, "get_settings_path", return_value=path),
                    patch.object(prepare_songs, "_load_yandex_token", return_value=""),
                ):
                    config = prepare_songs.load_config()
                self.assertEqual(config["program_url"], expected)
                self.assertEqual(config["priority_cloud_disk"], expected_cloud)
                self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_invalid_priority_and_wrong_cloud_url_are_reported(self):
        cases = (
            "priority_cloud_disk = other\n",
            "priority_cloud_disk =\n",
            "priority_cloud_disk = google\nprogram_google_url = https://disk.yandex.ru/d/program\n",
            "priority_cloud_disk = yandex\nprogram_yandex_url = https://docs.google.com/document/d/program/edit\n",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.ini"
            for options in cases:
                with self.subTest(options=options):
                    path.write_text(
                        "[OBS Local paths]\nroot_path = ./png\n[Song preparation]\n" + options,
                        encoding="utf-8",
                    )
                    with (
                        patch.object(prepare_songs, "get_settings_path", return_value=path),
                        patch.object(prepare_songs, "_load_yandex_token", return_value=""),
                    ):
                        config = prepare_songs.load_config()
                    with self.assertRaises(RuntimeError):
                        asyncio.run(prepare_songs.run(date(2026, 10, 4), config=config))

    def test_bad_saved_program_does_not_block_local_file_override_or_library(self):
        from obs_local import menu

        for priority in ("google", "invalid"):
            with self.subTest(priority=priority), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "settings.ini"
                path.write_text(
                    "[OBS Local paths]\nroot_path = " + str(root / "output") + "\n"
                    "[Song preparation]\n"
                    f"priority_cloud_disk = {priority}\n"
                    "program_url = https://[broken\n"
                    "program_google_url = https://[broken\n"
                    "yandex_zip_png_paths = /synthetic/zip\n",
                    encoding="utf-8",
                )
                with (
                    patch.object(prepare_songs, "get_settings_path", return_value=path),
                    patch.object(prepare_songs, "_load_yandex_token", return_value="synthetic-token"),
                ):
                    config = prepare_songs.load_config()
                program = root / "local.docx"
                _create_docx(program)
                with (
                    patch.object(prepare_songs, "_process_songs", new_callable=AsyncMock, return_value=[]),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    asyncio.run(prepare_songs.run(
                        date(2026, 10, 4), program_file=program, config=config,
                    ))
                    async def download(_session, _url, destination):
                        destination.write_bytes(program.read_bytes())
                    with patch.object(
                        prepare_songs, "download_program_docx",
                        new_callable=AsyncMock, side_effect=download,
                    ) as download_mock:
                        asyncio.run(prepare_songs.run(
                            date(2026, 10, 4), program_url=GoogleProgramDownloadTests.URL,
                            config=config,
                        ))
                    download_mock.assert_awaited_once()
                with (
                    patch.object(menu.prepare_songs, "_cache_remote_song_zips",
                                 new_callable=AsyncMock, return_value=(0, 0)) as cache,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    asyncio.run(menu._update_song_library(config))
                cache.assert_awaited_once()

    def test_template_defaults_to_google_priority(self):
        with (
            patch.object(prepare_songs, "get_settings_path", return_value=prepare_songs.BASE_DIR / "settings.ini"),
            patch.object(prepare_songs, "_load_yandex_token", return_value=""),
        ):
            config = prepare_songs.load_config()
        self.assertEqual(config["priority_cloud_disk"], "google")
        self.assertEqual(config["program_url_option"], "program_google_url")
        self.assertEqual(config["program_url"], "")

    def test_prompt_rejects_the_other_cloud_when_priority_is_set(self):
        inputs = Mock(side_effect=[
            "https://disk.yandex.ru/d/synthetic-program",
            GoogleProgramDownloadTests.URL,
        ])
        self.assertEqual(
            prepare_songs.ensure_program_url(
                {"program_url": "", "priority_cloud_disk": "google"}, input_fn=inputs,
            ),
            GoogleProgramDownloadTests.URL,
        )
        self.assertEqual(inputs.call_count, 2)

    def test_missing_program_url_is_prompted_and_validated_before_save(self):
        with tempfile.TemporaryDirectory(
            dir="/dev/shm" if Path("/dev/shm").is_dir() else None
        ) as directory:
            obs_dir = Path(directory) / "obs_local"
            obs_dir.mkdir()
            settings_path = obs_dir / "settings.ini"
            settings_path.write_text(
                "# Preserve this comment.\n"
                "[Song preparation]\n"
                "local_zip_dir = ./song_assets/zip\n",
                encoding="utf-8",
            )
            config = {"program_url": ""}
            answers = iter(
                [
                    "https://docs.yandex.ru/view/d/not-a-public-key",
                    "https://disk.yandex.ru/d/public-program",
                ]
            )

            with patch.object(prepare_songs, "BASE_DIR", obs_dir):
                result = prepare_songs.ensure_program_url(
                    config, input_fn=lambda _: next(answers)
                )

            saved = settings_path.read_text(encoding="utf-8")

        self.assertEqual(result, "https://disk.yandex.ru/d/public-program")
        self.assertEqual(config["program_url"], "")
        self.assertIn("# Preserve this comment.", saved)
        self.assertNotIn("program_url =", saved)

    def test_song_title_aliases_are_saved_and_loaded(self):
        with tempfile.TemporaryDirectory(
            dir="/dev/shm" if Path("/dev/shm").is_dir() else None
        ) as directory:
            aliases_path = Path(directory) / "song_title_aliases.json"
            aliases = {"песня вторая": "Другое название"}
            prepare_songs.save_song_title_aliases(aliases, aliases_path)

            self.assertEqual(
                prepare_songs.load_song_title_aliases(aliases_path), aliases
            )
            self.assertFalse(
                list(Path(directory).glob(".song_title_aliases.json.*"))
            )

    def test_macos_uses_user_log_directory_instead_of_linux_shm_default(self):
        with tempfile.TemporaryDirectory() as directory:
            project_dir = Path(directory)
            obs_dir = project_dir / "obs_local"
            obs_dir.mkdir()
            (obs_dir / "settings.ini").write_text(
                "[OBS Local paths]\n"
                "root_path = ./obs_png\n"
                "[Logging]\n"
                "log_dir = /dev/shm/pptx2png_tasks/obs_local/logs\n",
                encoding="utf-8",
            )

            with (
                patch.object(prepare_songs, "BASE_DIR", obs_dir),
                patch.object(prepare_songs, "PROJECT_DIR", project_dir),
                patch.object(prepare_songs.sys, "platform", "darwin"),
                patch.object(Path, "home", return_value=project_dir),
                self.assertNoLogs(level="WARNING"),
            ):
                config = prepare_songs.load_config()

        self.assertEqual(
            config["log_dir"],
            project_dir / "Library" / "Logs" / "pptx2png" / "obs_local",
        )

    def test_png_zip_paths_url_and_cache_flag_are_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            project_dir = Path(directory)
            obs_dir = project_dir / "obs_local"
            obs_dir.mkdir()
            (obs_dir / "settings.ini").write_text(
                "[OBS Local paths]\n"
                "root_path = ./obs_png\n"
                "[Song preparation]\n"
                "yandex_zip_png_paths = /remote/png\n"
                "yandex_zip_png_url = https://disk.yandex.ru/d/example\n"
                "cache_missing_yandex_song_zips = true\n",
                encoding="utf-8",
            )

            with (
                patch.object(prepare_songs, "BASE_DIR", obs_dir),
                patch.object(prepare_songs, "PROJECT_DIR", project_dir),
            ):
                config = prepare_songs.load_config()

        self.assertEqual(config["yandex_zip_png_paths"], ["/remote/png"])
        self.assertEqual(
            config["yandex_zip_png_url"],
            "https://disk.yandex.ru/d/example",
        )
        self.assertTrue(config["cache_missing_yandex_song_zips"])

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


class GoogleProgramDownloadTests(unittest.TestCase):
    URL = "https://docs.google.com/document/d/synthetic-document/edit?usp=sharing"

    def make_session(self, body=None, status=200, headers=None, error=None):
        response = MagicMock()
        response.status = status
        response.headers = headers or {}
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=None)

        async def chunks(_size):
            yield _minimal_docx_bytes() if body is None else body

        response.content.iter_chunked = chunks
        response.text = AsyncMock(return_value="Access denied")
        session = Mock()
        session.get = Mock(return_value=response, side_effect=error)
        return session

    def test_google_links_export_docx_without_yandex_api_or_token(self):
        urls = (
            self.URL,
            "https://docs.google.com/document/d/synthetic-document",
            "https://docs.google.com/document/d/synthetic-document/preview",
            "https://docs.google.com/document/d/synthetic-document/export?format=pdf",
        )
        for url in urls:
            with self.subTest(url=url), tempfile.TemporaryDirectory() as directory:
                session = self.make_session()
                destination = Path(directory) / "program.docx"
                asyncio.run(prepare_songs.download_program_docx(
                    session, url, destination,
                ))
                session.get.assert_called_once()
                args, kwargs = session.get.call_args
                self.assertEqual(
                    args[0],
                    "https://docs.google.com/document/d/synthetic-document/export?format=docx",
                )
                self.assertNotIn("headers", kwargs)
                self.assertNotIn("params", kwargs)
                self.assertEqual(destination.read_bytes(), _minimal_docx_bytes())

    def test_google_url_is_accepted_by_interactive_prompt(self):
        self.assertEqual(
            prepare_songs.ensure_program_url(
                {"program_url": ""}, input_fn=lambda _: self.URL,
            ),
            self.URL,
        )

    def test_invalid_google_addresses_are_rejected_before_network_access(self):
        urls = (
            "http://docs.google.com/document/d/example/edit",
            "https://docs.google.com.evil.test/document/d/example/edit",
            "https://docs.google.com/document/d//edit",
            "https://docs.google.com/document/d/example/edit/extra",
            "https://docs.google.com/document/u/0/d/example/edit",
            "https://docs.google.com/document/d/example%2Fother/edit",
            "https://user:password@docs.google.com/document/d/example/edit",
            "https://docs.google.com:8080/document/d/example/edit",
            "https://docs.google.com:invalid/document/d/example/edit",
        )
        with tempfile.TemporaryDirectory() as directory:
            for url in urls:
                with self.subTest(url=url):
                    session = self.make_session()
                    with self.assertRaises(RuntimeError):
                        asyncio.run(prepare_songs.download_program_docx(
                            session, url, Path(directory) / "program.docx",
                        ))
                    session.get.assert_not_called()

    def test_google_http_errors_explain_sharing_permissions(self):
        for status in (403, 404, 429):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                session = self.make_session(status=status)
                with self.assertRaisesRegex(RuntimeError, f"HTTP {status}") as caught:
                    asyncio.run(prepare_songs.download_program_docx(
                        session, self.URL, Path(directory) / "program.docx",
                    ))
                self.assertIn("Google Docs", str(caught.exception))
                self.assertNotIn("synthetic-document", str(caught.exception))

    def test_google_login_page_with_http_200_is_not_a_docx(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.make_session(body=b"<html>Sign in to Google</html>")
            with self.assertRaisesRegex(RuntimeError, "не является DOCX"):
                asyncio.run(prepare_songs.download_program_docx(
                    session, self.URL, Path(directory) / "program.docx",
                ))

    def test_google_download_enforces_header_and_stream_size_limits(self):
        for headers in ({}, {"Content-Length": "1000"}):
            with self.subTest(headers=headers), tempfile.TemporaryDirectory() as directory:
                session = self.make_session(body=b"x" * 11, headers=headers)
                with patch.object(prepare_songs, "MAX_PROGRAM_DOCX_BYTES", 10):
                    with self.assertRaisesRegex(RuntimeError, "превышает лимит"):
                        asyncio.run(prepare_songs.download_program_docx(
                            session, self.URL, Path(directory) / "program.docx",
                        ))

    def test_google_network_error_propagates(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.make_session(
                error=prepare_songs.aiohttp.ClientConnectionError("network unavailable"),
            )
            with self.assertRaises(prepare_songs.aiohttp.ClientConnectionError):
                asyncio.run(prepare_songs.download_program_docx(
                    session, self.URL, Path(directory) / "program.docx",
                ))

    def test_google_export_is_parsed_and_prompted_url_saved_after_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program = root / "synthetic.docx"
            _create_docx(program)
            config = {
                "program_url": "",
                "priority_cloud_disk": "google",
                "program_url_option": "program_google_url",
                "song_title_aliases_path": root / "aliases.json",
                "zip_dir": root / "zip",
                "pptx_dir": root / "pptx",
                "yandex_zip_png_paths": [],
                "yandex_pptx_paths": [],
                "output_dir": root / "output",
                "quality": "standard",
                "yandex_token": "",
            }
            session = self.make_session(body=program.read_bytes())
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=session)
            context.__aexit__ = AsyncMock(return_value=None)
            inputs = Mock(side_effect=[self.URL, "", ""])
            with (
                patch.object(prepare_songs.aiohttp, "ClientSession", return_value=context),
                patch.object(prepare_songs, "get_settings_path", return_value=root / "settings.ini"),
                patch.object(prepare_songs, "update_ini_file") as save,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                results = asyncio.run(prepare_songs.run(
                    date(2026, 10, 4), config=config, interactive=True, input_fn=inputs,
                ))
            self.assertEqual([result.title for result in results], ["Песня первая", "Песня вторая"])
            self.assertTrue(config["output_dir"].is_dir())
            save.assert_called_once_with(
                root / "settings.ini", {"Song preparation": {"program_google_url": self.URL}},
            )
            self.assertEqual(config["program_url"], self.URL)
            session.get.assert_called_once()

    def test_google_timeout_can_be_cancelled_without_replacing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "output"
            target.mkdir()
            marker = target / "keep.png"
            marker.write_bytes(PNG)
            config = {
                "program_url": self.URL,
                "priority_cloud_disk": "google",
                "program_yandex_url": "https://disk.yandex.ru/d/other-source",
                "zip_dir": root / "zip",
                "pptx_dir": root / "pptx",
                "yandex_zip_png_paths": [],
                "yandex_pptx_paths": [],
                "output_dir": target,
                "quality": "standard",
                "yandex_token": "",
            }
            session = self.make_session(error=asyncio.TimeoutError())
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=session)
            context.__aexit__ = AsyncMock(return_value=None)
            with (
                patch.object(prepare_songs.aiohttp, "ClientSession", return_value=context),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaises(prepare_songs.OperationCancelled):
                    asyncio.run(prepare_songs.run(
                        date(2026, 10, 4), config=config, interactive=True,
                        input_fn=lambda _: "",
                    ))
            self.assertEqual(marker.read_bytes(), PNG)
            session.get.assert_called_once()

    def test_google_failure_allows_local_docx_without_saving_failed_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program = root / "local.docx"
            _create_docx(program)
            config = {
                "program_url": "",
                "song_title_aliases_path": root / "aliases.json",
                "zip_dir": root / "zip",
                "pptx_dir": root / "pptx",
                "yandex_zip_png_paths": [],
                "yandex_pptx_paths": [],
                "output_dir": root / "output",
                "quality": "standard",
                "yandex_token": "",
            }
            session = self.make_session(status=403)
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=session)
            context.__aexit__ = AsyncMock(return_value=None)
            inputs = Mock(side_effect=[self.URL, "2", str(program), "", ""])
            with (
                patch.object(prepare_songs.aiohttp, "ClientSession", return_value=context),
                patch.object(prepare_songs, "update_ini_file") as save,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                results = asyncio.run(prepare_songs.run(
                    date(2026, 10, 4), config=config, interactive=True, input_fn=inputs,
                ))
            self.assertEqual(len(results), 2)
            session.get.assert_called_once()
            save.assert_not_called()
            self.assertEqual(config["program_url"], "")


class SongOutputTests(unittest.TestCase):
    def test_network_errors_have_actionable_user_message(self):
        self.assertIn(
            "Проверьте интернет-подключение",
            prepare_songs.format_user_error(
                prepare_songs.aiohttp.ClientConnectionError()
            ),
        )

    def test_timeout_errors_have_actionable_user_message(self):
        self.assertIn(
            "Истекло время ожидания",
            prepare_songs.format_user_error(TimeoutError()),
        )

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
            with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(
                io.StringIO()
            ) as console:
                log_dir = Path(directory)
                prepare_songs.setup_logging(log_dir)
                logging.info("info diagnostic test")
                logging.debug("debug diagnostic test")
                logging.warning("warning diagnostic test")
                for handler in root_logger.handlers:
                    handler.flush()

                self.assertIn("info diagnostic test", (log_dir / "obs_local.log").read_text(
                    encoding="utf-8"
                ))
                self.assertIn(
                    "debug diagnostic test",
                    (log_dir / "debug.log").read_text(encoding="utf-8"),
                )
                self.assertNotIn("info diagnostic test", console.getvalue())
                self.assertNotIn("debug diagnostic test", console.getvalue())
                self.assertIn("warning diagnostic test", console.getvalue())
                self.assertFalse((log_dir / "bot.log").exists())
        finally:
            for handler in root_logger.handlers[:]:
                root_logger.removeHandler(handler)
                handler.close()
            for handler in original_handlers:
                root_logger.addHandler(handler)
            root_logger.setLevel(original_level)

    def test_traceback_is_kept_in_debug_log_but_hidden_from_normal_console(self):
        root_logger = logging.getLogger()
        original_handlers = root_logger.handlers[:]
        original_level = root_logger.level
        try:
            with tempfile.TemporaryDirectory(
                dir="/dev/shm" if Path("/dev/shm").is_dir() else None
            ) as directory, contextlib.redirect_stderr(io.StringIO()) as console:
                prepare_songs.setup_logging(Path(directory))
                try:
                    raise RuntimeError("synthetic failure")
                except RuntimeError as error:
                    prepare_songs.log_user_error(
                        error, "Проверка понятного сообщения"
                    )
                for handler in root_logger.handlers:
                    handler.flush()

                console_output = console.getvalue()
                debug_output = (
                    Path(directory) / "debug.log"
                ).read_text(encoding="utf-8")

            self.assertNotIn("Traceback", console_output)
            self.assertIn("Проверка понятного сообщения", console_output)
            self.assertIn("Traceback", debug_output)
            self.assertIn("synthetic failure", debug_output)
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
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

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

    def test_public_program_rate_limit_has_actionable_error(self):
        public_url = "https://disk.yandex.ru/d/example_public_key"

        class FakeResponse:
            status = 429

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def text(self):
                return (
                    '{"error":"TooManyRequestsError",'
                    '"description":"Too Many Requests"}'
                )

        class FakeSession:
            def get(self, *_args, **_kwargs):
                return FakeResponse()

        async def execute():
            with self.assertRaisesRegex(
                prepare_songs.ProgramRateLimitError,
                "временно ограничил частоту запросов.*HTTP 429",
            ):
                await prepare_songs.download_program_docx(
                    FakeSession(), public_url, Path("program.docx")
                )

        asyncio.run(execute())

    def test_rate_limit_retries_only_on_enter_and_accepts_local_docx(self):
        with tempfile.TemporaryDirectory(
            dir="/dev/shm" if Path("/dev/shm").is_dir() else None
        ) as directory:
            root = Path(directory)
            local_program = root / "program.docx"
            _create_docx(local_program)
            invalid_program = root / "invalid.docx"
            invalid_program.write_text("not a DOCX", encoding="utf-8")
            missing_program = root / "missing.docx"
            config = {
                "program_url": "https://disk.yandex.ru/i/example_program",
                "song_title_aliases_path": root / "aliases.json",
                "zip_dir": root / "zip",
                "pptx_dir": root / "pptx",
                "yandex_zip_png_paths": [],
                "yandex_pptx_paths": [],
                "cache_missing_yandex_song_zips": False,
                "output_dir": root / "Трансляция" / "Песни",
                "quality": "standard",
                "yandex_token": "",
            }

            class FakeResponse:
                status = 429
                headers = {}

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_args):
                    return None

                async def text(self):
                    return (
                        '{"error":"TooManyRequestsError",'
                        '"description":"Too Many Requests"}'
                    )

            class FakeSession:
                def __init__(self):
                    self.get_calls = 0

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_args):
                    return None

                def get(self, *_args, **_kwargs):
                    self.get_calls += 1
                    return FakeResponse()

            session = FakeSession()
            inputs = iter(
                [
                    "1",
                    "2",
                    str(missing_program),
                    "2",
                    str(invalid_program),
                    "2",
                    str(local_program),
                    "",
                    "",
                ]
            )
            with patch.object(
                prepare_songs.aiohttp,
                "ClientSession",
                return_value=session,
            ):
                results = asyncio.run(
                    prepare_songs.run(
                        date(2026, 10, 4),
                        config=config,
                        interactive=True,
                        input_fn=lambda _prompt: next(inputs),
                    )
                )

        self.assertEqual(len(results), 2)
        self.assertEqual(session.get_calls, 2)

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

    def test_download_failures_offer_local_docx_without_another_request(self):
        failures = (
            prepare_songs.ProgramRateLimitError("HTTP 429"),
            RuntimeError("HTML CAPTCHA вместо DOCX"),
            prepare_songs.aiohttp.ClientConnectionError("connection failed"),
            asyncio.TimeoutError(),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                local_program = root / "local program.docx"
                _create_docx(local_program)
                config = {
                    "program_url": "https://disk.yandex.ru/i/example_program",
                    "song_title_aliases_path": root / "aliases.json",
                    "zip_dir": root / "zip",
                    "pptx_dir": root / "pptx",
                    "yandex_zip_png_paths": [],
                    "yandex_pptx_paths": [],
                    "cache_missing_yandex_song_zips": False,
                    "output_dir": root / "output",
                    "quality": "standard",
                    "yandex_token": "",
                }
                escaped_path = '"' + str(local_program).replace(
                    " ", r"\ " if prepare_songs.sys.platform != "win32" else " "
                ) + '" '
                inputs = iter(["invalid", "2", "", "2", str(root / "missing.docx"),
                               "2", escaped_path, "", ""])
                with patch.object(
                    prepare_songs, "download_program_docx",
                    new_callable=AsyncMock, side_effect=failure,
                ) as download:
                    results = asyncio.run(
                        prepare_songs.run(
                            date(2026, 10, 4), config=config, interactive=True,
                            input_fn=lambda _: next(inputs),
                        )
                    )
                self.assertEqual(len(results), 2)
                download.assert_awaited_once()

    def test_empty_recovery_choice_cancels_without_replacing_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir()
            (output / "keep.png").write_bytes(PNG)
            config = {
                "program_url": "https://disk.yandex.ru/i/example_program",
                "zip_dir": root / "zip",
                "pptx_dir": root / "pptx",
                "yandex_zip_png_paths": [],
                "yandex_pptx_paths": [],
                "output_dir": output,
                "quality": "standard",
                "yandex_token": "",
            }
            with patch.object(
                prepare_songs, "download_program_docx",
                new_callable=AsyncMock,
                side_effect=prepare_songs.ProgramRateLimitError("HTTP 429"),
            ) as download:
                with self.assertRaises(prepare_songs.OperationCancelled):
                    asyncio.run(
                        prepare_songs.run(
                            date(2026, 10, 4), config=config, interactive=True,
                            input_fn=lambda _: "",
                        )
                    )
            download.assert_awaited_once()
            self.assertEqual((output / "keep.png").read_bytes(), PNG)

    def test_new_program_link_failure_can_cancel_with_three_or_eof(self):
        for choice in ("3", EOFError()):
            with self.subTest(choice=choice), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = {
                    "program_url": "", "zip_dir": root / "zip",
                    "pptx_dir": root / "pptx", "output_dir": root / "output",
                    "yandex_zip_png_paths": [], "yandex_pptx_paths": [],
                    "quality": "standard", "yandex_token": "",
                }
                answers = Mock(side_effect=[
                    "https://disk.yandex.ru/d/new-program", choice,
                ])
                with (
                    patch.object(prepare_songs, "download_program_docx",
                                 new_callable=AsyncMock,
                                 side_effect=RuntimeError("invalid DOCX")) as download,
                    contextlib.redirect_stdout(io.StringIO()) as output,
                ):
                    with self.assertRaises(prepare_songs.OperationCancelled):
                        asyncio.run(prepare_songs.run(
                            date(2026, 10, 4), config=config,
                            interactive=True, input_fn=answers,
                        ))
                self.assertIn(
                    "1. Повторить запрос\n"
                    "2. Ввести путь к локальному файлу DOCX\n"
                    "3. Отменить, вернуться назад",
                    output.getvalue(),
                )
                download.assert_awaited_once()
                self.assertEqual(config["program_url"], "")
                self.assertFalse(config["output_dir"].exists())

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

    def test_remote_zip_and_pptx_materials_are_cached_as_local_png_zips(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            zip_dir = root / "local_zip"
            zip_dir.mkdir()
            remote_zip = root / "remote.zip"
            with zipfile.ZipFile(remote_zip, "w") as archive:
                archive.writestr("slides/original.png", PNG)
            remote_pptx = root / "remote.pptx"
            remote_pptx.write_bytes(b"pptx")

            class FakeClient:
                def __init__(self):
                    self.downloads = []

                async def list_folder(self, path):
                    if path == "/remote/zip":
                        return [
                            {
                                "type": "file",
                                "name": "Song from ZIP.zip",
                                "path": "/remote/zip/Song from ZIP.zip",
                            }
                        ]
                    if path == "/remote/pptx":
                        return [
                            {
                                "type": "file",
                                "name": "Song from PPTX.pptx",
                                "path": "/remote/pptx/Song from PPTX.pptx",
                            }
                        ]
                    return []

                async def download_file(self, remote_path, destination):
                    self.downloads.append(remote_path)
                    source = remote_zip if remote_path.endswith(".zip") else remote_pptx
                    destination.write_bytes(source.read_bytes())
                    return True

            async def fake_convert(_source, render_dir, _quality):
                png = render_dir / "slide.png"
                png.write_bytes(PNG)
                return [png], None

            client = FakeClient()
            config = {
                "zip_dir": zip_dir,
                "yandex_zip_png_paths": ["/remote/zip"],
                "yandex_pptx_paths": ["/remote/pptx"],
                "quality": "standard",
            }
            with patch.object(
                prepare_songs, "convert_all_pngs", side_effect=fake_convert
            ):
                cached, failed = asyncio.run(
                    prepare_songs._cache_remote_song_zips(client, config, root)
                )
                repeated_cached, repeated_failed = asyncio.run(
                    prepare_songs._cache_remote_song_zips(client, config, root)
                )

            self.assertEqual((cached, failed), (2, 0))
            self.assertEqual((repeated_cached, repeated_failed), (0, 0))
            self.assertEqual(len(client.downloads), 2)
            self.assertTrue(
                prepare_songs._zip_has_valid_pngs(zip_dir / "Song from ZIP.zip")
            )
            self.assertTrue(
                prepare_songs._zip_has_valid_pngs(zip_dir / "Song from PPTX.zip")
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
                "song_title_aliases_path": root / "song_title_aliases.json",
                "zip_dir": zip_dir,
                "pptx_dir": pptx_dir,
                "yandex_zip_png_paths": [],
                "yandex_pptx_paths": [],
                "cache_missing_yandex_song_zips": False,
                "output_dir": output_dir,
                "quality": "standard",
                "yandex_token": "",
            }

            with patch.object(prepare_songs, "load_config", return_value=config):
                results = asyncio.run(
                    prepare_songs.run(date(2026, 10, 4), program_path)
                )

            first_song = output_dir / "01 - Песня первая"
            second_song = output_dir / "02 - Песня вторая (не найдено)"
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
                "song_title_aliases_path": root / "song_title_aliases.json",
                "zip_dir": zip_dir,
                "pptx_dir": pptx_dir,
                "yandex_zip_png_paths": [],
                "yandex_pptx_paths": [],
                "cache_missing_yandex_song_zips": False,
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
            self.assertTrue(prepare_songs._zip_has_valid_pngs(zip_dir / "Песня первая.zip"))

    def test_missing_song_can_be_found_and_remembered_by_alternative_title(self):
        with tempfile.TemporaryDirectory(
            dir="/dev/shm" if Path("/dev/shm").is_dir() else None
        ) as directory:
            root = Path(directory)
            program_path = root / "program.docx"
            _create_docx(program_path)
            zip_dir = root / "zip"
            pptx_dir = root / "pptx"
            zip_dir.mkdir()
            pptx_dir.mkdir()
            with zipfile.ZipFile(zip_dir / "Песня первая.zip", "w") as archive:
                archive.writestr("slide_1.png", PNG)
            with zipfile.ZipFile(zip_dir / "Alt title.zip", "w") as archive:
                archive.writestr("slide_1.png", PNG)
            aliases_path = root / "song_title_aliases.json"
            output_dir = root / "Трансляция" / "Песни"
            config = {
                "program_url": "",
                "song_title_aliases_path": aliases_path,
                "zip_dir": zip_dir,
                "pptx_dir": pptx_dir,
                "yandex_zip_png_paths": [],
                "yandex_pptx_paths": [],
                "cache_missing_yandex_song_zips": False,
                "output_dir": output_dir,
                "quality": "standard",
                "yandex_token": "",
            }

            first_answers = iter(["Alt title"])
            first_results = asyncio.run(
                prepare_songs.run(
                    date(2026, 10, 4),
                    program_path,
                    config=config,
                    interactive=True,
                    input_fn=lambda _: next(first_answers),
                )
            )
            first_song_dir = output_dir / "02 - Песня вторая"
            self.assertTrue(
                (first_song_dir / "slide_001.png").is_file(),
                f"Output contents: {list(output_dir.iterdir())}",
            )
            self.assertTrue(aliases_path.is_file())

            second_results = asyncio.run(
                prepare_songs.run(
                    date(2026, 10, 4),
                    program_path,
                    config=config,
                    interactive=True,
                    input_fn=Mock(
                        side_effect=AssertionError("unexpected alias prompt")
                    ),
                )
            )
            saved_aliases = prepare_songs.load_song_title_aliases(aliases_path)

        self.assertEqual(first_results[1].status, "ready")
        self.assertEqual(first_results[1].title, "Песня вторая")
        self.assertEqual(
            saved_aliases, {"песня вторая": "Alt title"}
        )
        self.assertEqual(second_results[1].status, "ready")
        self.assertEqual(second_results[1].title, "Песня вторая")

    def test_legacy_alias_for_decomposed_title_still_finds_material(self):
        for title in ("Долг мои\u0306 уплатил", "Долг мой уплатил"):
            with self.subTest(title=title), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                zip_dir = root / "zip"
                zip_dir.mkdir()
                archive_path = zip_dir / "Alt title.zip"
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr("slide_1.png", PNG)
                aliases_path = root / "aliases.json"
                aliases_path.write_text(
                    '{"долг мои уплатил": "Alt title"}', encoding="utf-8"
                )
                aliases = prepare_songs.load_song_title_aliases(aliases_path)
                staging = root / "output"
                staging.mkdir()
                results = asyncio.run(prepare_songs._process_songs(
                    [title], staging,
                    {"zip_dir": zip_dir, "pptx_dir": root / "pptx",
                     "quality": "standard"},
                    None, root, aliases,
                ))
                self.assertEqual(results[0].status, "ready")
                self.assertEqual(results[0].source, str(archive_path))
                self.assertEqual(results[0].title, title)

    def test_nfc_alias_takes_precedence_over_legacy_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "output"
            staging.mkdir()
            aliases = {"долг мой уплатил": "Current", "долг мои уплатил": "Legacy"}
            with patch.object(
                prepare_songs, "_process_song", new_callable=AsyncMock,
                return_value=prepare_songs.SongResult(
                    "Current", staging / "song", status="ready"
                ),
            ) as process:
                asyncio.run(prepare_songs._process_songs(
                    ["Долг мои\u0306 уплатил"], staging, {}, None, root, aliases
                ))
            self.assertEqual(process.await_args.args[0], "Current")

    def test_saved_alias_falls_back_to_original_title_if_alias_is_missing(self):
        with tempfile.TemporaryDirectory(
            dir="/dev/shm" if Path("/dev/shm").is_dir() else None
        ) as directory:
            root = Path(directory)
            program_path = root / "program.docx"
            _create_docx(program_path)
            zip_dir = root / "zip"
            pptx_dir = root / "pptx"
            zip_dir.mkdir()
            pptx_dir.mkdir()
            with zipfile.ZipFile(zip_dir / "Песня вторая.zip", "w") as archive:
                archive.writestr("slide_1.png", PNG)
            aliases_path = root / "song_title_aliases.json"
            prepare_songs.save_song_title_aliases(
                {"песня вторая": "Old alias"}, aliases_path
            )
            config = {
                "program_url": "",
                "song_title_aliases_path": aliases_path,
                "zip_dir": zip_dir,
                "pptx_dir": pptx_dir,
                "yandex_zip_png_paths": [],
                "yandex_pptx_paths": [],
                "cache_missing_yandex_song_zips": False,
                "output_dir": root / "Трансляция" / "Песни",
                "quality": "standard",
                "yandex_token": "",
            }

            results = asyncio.run(
                prepare_songs.run(date(2026, 10, 4), program_path, config=config)
            )

        self.assertEqual(results[1].status, "ready")
        self.assertEqual(results[1].source, str(zip_dir / "Песня вторая.zip"))

    def test_prompted_program_url_is_saved_only_after_docx_validation(self):
        with tempfile.TemporaryDirectory(
            dir="/dev/shm" if Path("/dev/shm").is_dir() else None
        ) as directory:
            root = Path(directory)
            obs_dir = root / "obs_local"
            obs_dir.mkdir()
            settings_path = obs_dir / "settings.ini"
            settings_path.write_text(
                "[OBS Local paths]\n"
                "root_path = ./obs_png\n"
                "[Song preparation]\n"
                "program_url =\n",
                encoding="utf-8",
            )
            program_path = root / "program.docx"
            _create_docx(program_path)
            config = {
                "program_url": "",
                "song_title_aliases_path": root / "aliases.json",
                "zip_dir": root / "zip",
                "pptx_dir": root / "pptx",
                "yandex_zip_png_paths": [],
                "yandex_pptx_paths": [],
                "cache_missing_yandex_song_zips": False,
                "output_dir": root / "Трансляция" / "Песни",
                "quality": "standard",
                "yandex_token": "",
            }
            inputs = iter(
                [
                    "https://disk.yandex.ru/d/not-a-docx",
                    "1",
                    "",
                    "",
                ]
            )

            download_attempts = 0

            async def download_docx(_session, _url, destination):
                nonlocal download_attempts
                download_attempts += 1
                if download_attempts == 1:
                    destination.write_bytes(b"invalid")
                    raise RuntimeError("Скачанный файл не похож на DOCX")
                _create_docx(destination)

            with (
                patch.object(prepare_songs, "BASE_DIR", obs_dir),
                patch.object(
                    prepare_songs,
                    "download_program_docx",
                    side_effect=download_docx,
                ),
            ):
                results = asyncio.run(
                    prepare_songs.run(
                        date(2026, 10, 4),
                        config=config,
                        interactive=True,
                        input_fn=lambda _: next(inputs),
                    )
                )
                saved_settings = prepare_songs.get_settings_path(obs_dir).read_text(encoding="utf-8")

        self.assertEqual(config["program_url"], "https://disk.yandex.ru/d/not-a-docx")
        self.assertIn(
            "program_url = https://disk.yandex.ru/d/not-a-docx",
            saved_settings,
        )
        self.assertEqual(len(results), 2)
        self.assertEqual(download_attempts, 2)

    def test_local_program_file_keeps_interactive_retry_prompt_enabled(self):
        config = {"log_dir": Path("/dev/shm")}
        with (
            patch(
                "sys.argv",
                [
                    "prepare_songs",
                    "--program-file",
                    "/dev/shm/program.docx",
                ],
            ),
            patch("sys.stdin", MagicMock(isatty=Mock(return_value=True))),
            patch.object(prepare_songs, "load_config", return_value=config),
            patch.object(prepare_songs, "setup_logging"),
            patch.object(prepare_songs, "run", new_callable=AsyncMock) as run,
        ):
            prepare_songs.main()

        self.assertTrue(run.await_args.kwargs["interactive"])

if __name__ == "__main__":
    unittest.main()
