import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname
from unittest.mock import patch

from pptx2png_core.converter_engine import (
    _find_libreoffice,
    _install_macos_fonts,
    _macos_font_dirs,
    _run_libreoffice_conversion,
    _soffice_fallback_args,
)


def _write_fake_output(command, filename, content=b"pdf"):
    output_dir = Path(command[command.index("--outdir") + 1])
    (output_dir / filename).write_bytes(content)


class MacOSFontProfileTests(unittest.TestCase):
    def test_reads_additional_font_directories_from_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first"
            second = Path(directory) / "second"
            first.mkdir()
            second.mkdir()
            with patch.dict(
                os.environ,
                {"PPTX2PNG_FONT_DIRS": f"{first}{os.pathsep}{second}"},
            ):
                directories = _macos_font_dirs()

            self.assertEqual(directories[-2:], [first, second])

    def test_unavailable_extra_font_directory_triggers_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commands = []

            def fake_run(command, **_kwargs):
                commands.append(command)
                _write_fake_output(command, "slides.pdf")

            with (
                patch("pptx2png_core.converter_engine.sys.platform", "darwin"),
                patch.dict(os.environ, {"PPTX2PNG_FONT_DIRS": str(root / "missing")}),
                patch(
                    "pptx2png_core.converter_engine.subprocess.run",
                    side_effect=fake_run,
                ),
                self.assertLogs(level="WARNING"),
            ):
                _run_libreoffice_conversion(
                    "soffice", root / "slides.pptx", root, "pdf"
                )

            self.assertEqual(len(commands), 1)
            self.assertIn("--invisible", commands[0])

    def test_copies_supported_font_files_into_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "source"
            source_dir.mkdir()
            (source_dir / "Kraskario.ttf").write_bytes(b"font")
            (source_dir / "readme.txt").write_text("not a font", encoding="utf-8")

            with patch(
                "pptx2png_core.converter_engine._macos_font_dirs",
                return_value=[source_dir],
            ):
                copied = _install_macos_fonts(root / "profile")

            self.assertEqual(copied, 1)
            self.assertEqual(
                (root / "profile/user/fonts/Kraskario.ttf").read_bytes(),
                b"font",
            )

    def test_preserves_fonts_with_matching_filenames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dirs = [root / "one", root / "two"]
            for source, content in zip(source_dirs, (b"first", b"second")):
                source.mkdir()
                (source / "Regular.ttf").write_bytes(content)

            with patch(
                "pptx2png_core.converter_engine._macos_font_dirs",
                return_value=source_dirs,
            ):
                copied = _install_macos_fonts(root / "profile")

            fonts = list((root / "profile/user/fonts").glob("*.ttf"))
            self.assertEqual(copied, 2)
            self.assertEqual(
                {font.read_bytes() for font in fonts}, {b"first", b"second"}
            )

    def test_copies_font_contents_without_protected_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "source"
            source_dir.mkdir()
            (source_dir / "Protected.ttf").write_bytes(b"font")
            with (
                patch(
                    "pptx2png_core.converter_engine._macos_font_dirs",
                    return_value=[source_dir],
                ),
                patch.object(shutil, "copystat", side_effect=PermissionError),
            ):
                copied = _install_macos_fonts(root / "profile")

            self.assertEqual(copied, 1)
            self.assertEqual(
                (root / "profile/user/fonts/Protected.ttf").read_bytes(), b"font"
            )

    def test_unreadable_font_triggers_invisible_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "fonts"
            source_dir.mkdir()
            (source_dir / "Locked.ttf").write_bytes(b"locked")
            (source_dir / "Readable.ttf").write_bytes(b"readable")
            output_dir = root / "output"
            output_dir.mkdir()
            real_copy = shutil.copyfile
            commands = []

            def copy_font(source, destination):
                if Path(source).name == "Locked.ttf":
                    raise PermissionError("font is protected")
                return real_copy(source, destination)

            def fake_run(command, **_kwargs):
                commands.append(command)
                _write_fake_output(command, "slides.pdf")

            with (
                patch("pptx2png_core.converter_engine.sys.platform", "darwin"),
                patch(
                    "pptx2png_core.converter_engine._macos_font_dirs",
                    return_value=[source_dir],
                ),
                patch(
                    "pptx2png_core.converter_engine.shutil.copyfile",
                    side_effect=copy_font,
                ),
                patch(
                    "pptx2png_core.converter_engine.subprocess.run",
                    side_effect=fake_run,
                ),
                self.assertLogs(level="WARNING"),
            ):
                result = _run_libreoffice_conversion(
                    "soffice", root / "slides.pptx", output_dir, "pdf"
                )

            self.assertEqual(result, output_dir / "slides.pdf")
            self.assertEqual(len(commands), 1)
            self.assertIn("--invisible", commands[0])

    def test_uses_isolated_profile_with_copied_fonts_on_macos(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "fonts"
            source_dir.mkdir()
            (source_dir / "Kraskario.ttf").write_bytes(b"font")
            output_dir = root / "output"
            output_dir.mkdir()

            def fake_run(command, **_kwargs):
                profile_arg = next(
                    arg for arg in command if arg.startswith("-env:UserInstallation=")
                )
                uri = profile_arg.split("=", 1)[1]
                profile_path = Path(url2pathname(urlparse(uri).path))
                self.assertTrue(
                    (profile_path / "user/fonts/Kraskario.ttf").is_file()
                )
                self.assertIn("--headless", command)
                _write_fake_output(command, "slides.pdf")

            with (
                patch("pptx2png_core.converter_engine.sys.platform", "darwin"),
                patch(
                    "pptx2png_core.converter_engine._macos_font_dirs",
                    return_value=[source_dir],
                ),
                patch(
                    "pptx2png_core.converter_engine.subprocess.run",
                    side_effect=fake_run,
                ),
            ):
                result = _run_libreoffice_conversion(
                    "soffice", root / "slides.pptx", output_dir, "pdf"
                )

            self.assertEqual(result, output_dir / "slides.pdf")

    def test_retries_with_invisible_mode_if_profile_conversion_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "fonts"
            source_dir.mkdir()
            (source_dir / "Kraskario.ttf").write_bytes(b"font")
            output_dir = root / "output"
            output_dir.mkdir()
            commands = []

            def fake_run(command, **_kwargs):
                commands.append(command)
                if len(commands) == 1:
                    raise subprocess.CalledProcessError(1, command)
                _write_fake_output(command, "slides.pdf")

            with (
                patch("pptx2png_core.converter_engine.sys.platform", "darwin"),
                patch(
                    "pptx2png_core.converter_engine._macos_font_dirs",
                    return_value=[source_dir],
                ),
                patch(
                    "pptx2png_core.converter_engine.subprocess.run",
                    side_effect=fake_run,
                ),
                self.assertLogs(level="WARNING"),
            ):
                result = _run_libreoffice_conversion(
                    "soffice", root / "slides.pptx", output_dir, "pdf"
                )

            self.assertEqual(result, output_dir / "slides.pdf")
            self.assertIn("--headless", commands[0])
            self.assertTrue(
                any(arg.startswith("-env:UserInstallation=") for arg in commands[0])
            )
            self.assertIn("--invisible", commands[1])
            self.assertNotIn("--headless", commands[1])

    def test_non_macos_conversion_keeps_headless_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "output"
            output_dir.mkdir()
            commands = []

            def fake_run(command, **_kwargs):
                commands.append(command)
                _write_fake_output(command, "slides.pdf")

            with (
                patch("pptx2png_core.converter_engine.sys.platform", "linux"),
                patch(
                    "pptx2png_core.converter_engine.subprocess.run",
                    side_effect=fake_run,
                ),
            ):
                _run_libreoffice_conversion(
                    "soffice", root / "slides.pptx", output_dir, "pdf"
                )

            self.assertIn("--headless", commands[0])
            self.assertFalse(
                any(arg.startswith("-env:UserInstallation=") for arg in commands[0])
            )

    def test_missing_new_output_does_not_return_stale_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "slides.pdf"
            output.write_bytes(b"previous output")

            with (
                patch("pptx2png_core.converter_engine.sys.platform", "linux"),
                patch("pptx2png_core.converter_engine.subprocess.run"),
                self.assertRaises(FileNotFoundError),
            ):
                _run_libreoffice_conversion(
                    "soffice", root / "slides.pptx", root, "pdf"
                )

            self.assertEqual(output.read_bytes(), b"previous output")

    def test_creates_missing_output_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "new" / "nested"

            def fake_run(command, **_kwargs):
                _write_fake_output(command, "slides.pdf")

            with (
                patch("pptx2png_core.converter_engine.sys.platform", "linux"),
                patch(
                    "pptx2png_core.converter_engine.subprocess.run",
                    side_effect=fake_run,
                ),
            ):
                output = _run_libreoffice_conversion(
                    "soffice", root / "slides.pptx", output_dir, "pdf"
                )

            self.assertEqual(output.read_bytes(), b"pdf")

    def test_missing_headless_output_retries_without_using_stale_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "fonts"
            source_dir.mkdir()
            (source_dir / "font.ttf").write_bytes(b"font")
            output = root / "slides.pdf"
            output.write_bytes(b"previous output")
            commands = []

            def fake_run(command, **_kwargs):
                commands.append(command)
                if len(commands) == 2:
                    _write_fake_output(command, "slides.pdf", b"new output")

            with (
                patch("pptx2png_core.converter_engine.sys.platform", "darwin"),
                patch(
                    "pptx2png_core.converter_engine._macos_font_dirs",
                    return_value=[source_dir],
                ),
                patch(
                    "pptx2png_core.converter_engine.subprocess.run",
                    side_effect=fake_run,
                ),
                self.assertLogs(level="WARNING"),
            ):
                _run_libreoffice_conversion(
                    "soffice", root / "slides.pptx", root, "pdf"
                )

            self.assertEqual(len(commands), 2)
            self.assertIn("--headless", commands[0])
            self.assertIn("--invisible", commands[1])
            self.assertEqual(output.read_bytes(), b"new output")

    def test_ppt_conversion_produces_pptx(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commands = []

            def fake_run(command, **_kwargs):
                commands.append(command)
                _write_fake_output(command, "slides.pptx", b"converted")

            with (
                patch("pptx2png_core.converter_engine.sys.platform", "linux"),
                patch(
                    "pptx2png_core.converter_engine.subprocess.run",
                    side_effect=fake_run,
                ),
            ):
                output = _run_libreoffice_conversion(
                    "soffice", root / "slides.ppt", root, "pptx"
                )

            self.assertEqual(output.read_bytes(), b"converted")
            self.assertEqual(commands[0][commands[0].index("--convert-to") + 1], "pptx")

    def test_does_not_overwrite_source_presentation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "slides.pptx"
            source.write_bytes(b"original")
            with (
                patch("pptx2png_core.converter_engine.sys.platform", "linux"),
                patch("pptx2png_core.converter_engine.subprocess.run") as run,
                self.assertRaises(ValueError),
            ):
                _run_libreoffice_conversion("soffice", source, root, "pptx")

            self.assertEqual(source.read_bytes(), b"original")
            run.assert_not_called()


class SofficeFallbackModeTests(unittest.TestCase):
    def test_macos_fallback_uses_invisible_mode(self):
        with patch("pptx2png_core.converter_engine.sys.platform", "darwin"):
            args = _soffice_fallback_args()
        self.assertIn("--invisible", args)
        self.assertNotIn("--headless", args)

    def test_other_platforms_keep_headless_fallback(self):
        with patch("pptx2png_core.converter_engine.sys.platform", "linux"):
            self.assertEqual(_soffice_fallback_args(), ["--headless"])

class LibreOfficeDiscoveryTests(unittest.TestCase):
    def test_uses_configured_executable_path(self):
        with tempfile.TemporaryDirectory(prefix="pptx2png-libreoffice-") as directory:
            executable = Path(directory) / "Libre Office" / "soffice"
            executable.parent.mkdir()
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)

            with patch.dict(
                os.environ,
                {"PPTX2PNG_LIBREOFFICE_PATH": str(executable)},
            ):
                self.assertEqual(_find_libreoffice(), str(executable))

    def test_rejects_configured_path_that_is_not_executable(self):
        with tempfile.TemporaryDirectory(prefix="pptx2png-libreoffice-") as directory:
            executable = Path(directory) / "soffice"
            executable.write_text("not executable", encoding="utf-8")
            executable.chmod(0o644)

            with (
                patch.dict(
                    os.environ,
                    {"PPTX2PNG_LIBREOFFICE_PATH": str(executable)},
                ),
                self.assertRaisesRegex(FileNotFoundError, str(executable)),
            ):
                _find_libreoffice()


if __name__ == "__main__":
    unittest.main()
