import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pptx2png_core.converter_engine import _find_libreoffice


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
