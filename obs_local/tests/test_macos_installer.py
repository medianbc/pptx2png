import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "install_obs_local_macos.sh"
PATH_COMMANDS = (
    "basename",
    "cat",
    "chmod",
    "cp",
    "curl",
    "dirname",
    "mkdir",
    "mv",
    "pwd",
    "rm",
    "tar",
)


class MacOSInstallerDependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp_context = tempfile.TemporaryDirectory(
            prefix="pptx2png-installer-test-",
        )
        self.addCleanup(self.temp_context.cleanup)
        self.root = Path(self.temp_context.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.brew_prefix = self.root / "homebrew"
        self.python_prefix = self.brew_prefix / "opt" / "python"
        self.soffice_path = (
            self.root
            / "Applications"
            / "LibreOffice.app"
            / "Contents"
            / "MacOS"
            / "soffice"
        )
        self.brew_log = self.root / "brew.log"
        self.python_log = self.root / "python.log"
        self.installed_python_template = self.root / "installed-python"

        for name in PATH_COMMANDS:
            path = shutil.which(name)
            if path:
                (self.bin_dir / name).symlink_to(path)

        self._write_executable(
            self.installed_python_template,
            """#!/bin/sh
if [ "$1" = "-c" ]; then
    printf 'check:installed-python\\n' >> "$PYTHON_LOG"
    exit 0
fi
exit 0
""",
        )
        self._write_executable(
            self.bin_dir / "uname",
            "#!/bin/sh\nprintf 'Darwin\\n'\n",
        )
        self._write_executable(
            self.bin_dir / "brew",
            f"""#!/bin/sh
printf '%s\\n' "$*" >> "{self.brew_log}"
case "$*" in
    "--prefix") printf '%s\\n' "{self.brew_prefix}" ;;
    "--prefix python") printf '%s\\n' "{self.python_prefix}" ;;
    "install python")
        mkdir -p "{self.python_prefix}/libexec/bin"
        cp "{self.installed_python_template}" "{self.python_prefix}/libexec/bin/python3"
        chmod +x "{self.python_prefix}/libexec/bin/python3"
        ;;
    "install --cask libreoffice")
        mkdir -p "{self.soffice_path.parent}"
        cat > "{self.soffice_path}" <<'SOFFICE'
#!/bin/sh
exit 0
SOFFICE
        chmod +x "{self.soffice_path}"
        ;;
    *) exit 99 ;;
esac
""",
        )

        self.install_dir = self.home / "pptx2png"
        self.install_dir.mkdir()
        (self.install_dir / ".pptx2png-archive-install").touch()
        (self.install_dir / "config.ini").write_text(
            "[YandexDisk]\ntoken =\n",
            encoding="utf-8",
        )
        self._write_executable(
            self.install_dir / ".venv" / "bin" / "python",
            "#!/bin/sh\nexit 0\n",
        )

    def _write_executable(self, path: Path, contents: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)

    def _add_python(self, path: Path, *, compatible: bool, label: str) -> None:
        exit_code = 0 if compatible else 1
        self._write_executable(
            path,
            f"""#!/bin/sh
if [ "$1" = "-c" ]; then
    printf 'check:{label}\\n' >> "$PYTHON_LOG"
    exit {exit_code}
fi
exit 0
""",
        )

    def _run_installer(self) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.update(
            {
                "CI": "true",
                "HOME": str(self.home),
                "PATH": str(self.bin_dir),
                "PYTHON_LOG": str(self.python_log),
                "PPTX2PNG_LIBREOFFICE_PATH": str(self.soffice_path),
            }
        )
        return subprocess.run(
            ["/bin/bash", str(INSTALLER), str(self.install_dir)],
            capture_output=True,
            check=False,
            env=env,
            text=True,
        )

    def _brew_installs(self) -> list[str]:
        if not self.brew_log.exists():
            return []
        return [
            line
            for line in self.brew_log.read_text(encoding="utf-8").splitlines()
            if line.startswith("install ")
        ]

    def test_prefers_compatible_homebrew_python_over_shadowed_old_python(self):
        self._add_python(
            self.bin_dir / "python3",
            compatible=False,
            label="shadowed-old-python",
        )
        self._write_executable(
            self.soffice_path,
            "#!/bin/sh\nexit 0\n",
        )

        result = self._run_installer()

        self.assertEqual(
            result.returncode,
            0,
            result.stdout
            + result.stderr
            + (
                self.brew_log.read_text(encoding="utf-8")
                if self.brew_log.exists()
                else ""
            ),
        )
        self.assertEqual(self._brew_installs(), ["install python"])
        probes = self.python_log.read_text(encoding="utf-8").splitlines()
        self.assertIn("check:shadowed-old-python", probes)
        self.assertIn("check:installed-python", probes)

    def test_reuses_existing_python_and_soffice_without_homebrew_install(self):
        python_path = self.python_prefix / "libexec" / "bin" / "python3"
        self._add_python(python_path, compatible=True, label="existing-python")
        self._write_executable(
            self.soffice_path,
            "#!/bin/sh\nexit 0\n",
        )

        result = self._run_installer()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"Using compatible Python: {python_path}", result.stdout)
        self.assertIn(
            f"Using existing LibreOffice: {self.soffice_path}",
            result.stdout,
        )
        self.assertEqual(self._brew_installs(), [])

    def test_installs_only_missing_libreoffice(self):
        self._add_python(
            self.python_prefix / "libexec" / "bin" / "python3",
            compatible=True,
            label="existing-python",
        )

        result = self._run_installer()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._brew_installs(), ["install --cask libreoffice"])
        self.assertIn(
            f"Using compatible Python: {self.python_prefix}/libexec/bin/python3",
            result.stdout,
        )
        self.assertIn(
            "LibreOffice was not found; installing it with Homebrew",
            result.stdout,
        )
        self.assertTrue(self.soffice_path.is_file())


if __name__ == "__main__":
    unittest.main()
