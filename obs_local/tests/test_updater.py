import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from obs_local import updater


class UpdateCheckTests(unittest.TestCase):
    def test_archive_version_is_compared_to_github_revision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / updater.ARCHIVE_MARKER).touch()
            (project_dir / updater.VERSION_FILE).write_text(
                json.dumps({"branch": "main", "revision": "a" * 40}),
                encoding="utf-8",
            )
            response = Mock()
            response.read.return_value = json.dumps(
                {"sha": "b" * 40}
            ).encode()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=None)

            with (
                patch.object(updater, "PROJECT_DIR", project_dir),
                patch.object(updater, "urlopen", return_value=response) as urlopen,
            ):
                result = updater.check_for_updates()

        self.assertTrue(result.available)
        self.assertEqual(result.local_revision, "a" * 40)
        self.assertEqual(result.remote_revision, "b" * 40)
        self.assertFalse(result.is_git_checkout)
        self.assertIn("/commits/main", urlopen.call_args.args[0].full_url)

    def test_unknown_legacy_archive_version_is_reported_as_update(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / updater.ARCHIVE_MARKER).touch()
            response = Mock()
            response.read.return_value = json.dumps(
                {"sha": "c" * 40}
            ).encode()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=None)

            with (
                patch.object(updater, "PROJECT_DIR", project_dir),
                patch.object(updater, "urlopen", return_value=response),
            ):
                result = updater.check_for_updates()

        self.assertTrue(result.available)
        self.assertIsNone(result.local_revision)


class ArchiveUpdateTests(unittest.TestCase):
    def test_archive_extraction_accepts_github_root_directory(self):
        archive_buffer = io.BytesIO()
        with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
            root = tarfile.TarInfo("repo-main/")
            root.type = tarfile.DIRTYPE
            archive.addfile(root)
            payload = b"module source"
            member = tarfile.TarInfo("repo-main/obs_local/updater.py")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        archive_buffer.seek(0)

        with tempfile.TemporaryDirectory() as temp_dir:
            response = Mock()
            response.read.return_value = archive_buffer.getvalue()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=None)
            with patch.object(updater, "urlopen", return_value=response):
                updater._download_archive("main", Path(temp_dir))

            self.assertEqual(
                (Path(temp_dir) / "obs_local" / "updater.py").read_bytes(),
                b"module source",
            )

    def test_update_preserves_local_data_and_installs_dependencies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "obs_local").mkdir()
            (project_dir / "obs_local" / "settings.ini").write_text(
                "local settings", encoding="utf-8"
            )
            (project_dir / "config.ini").write_text(
                "private token", encoding="utf-8"
            )
            (project_dir / "start_obs_local.sh").write_text(
                "local launcher", encoding="utf-8"
            )
            (project_dir / ".venv").mkdir()
            (project_dir / ".venv" / "keep").touch()
            info = updater.UpdateInfo(
                branch="main",
                local_revision="old",
                remote_revision="b" * 40,
                available=True,
                is_git_checkout=False,
            )

            def create_archive_source(_branch: str, destination: Path) -> None:
                (destination / "obs_local").mkdir()
                (destination / "obs_local" / "settings.ini").write_text(
                    "default settings", encoding="utf-8"
                )
                (destination / "obs_local" / "menu.py").write_text(
                    "new source", encoding="utf-8"
                )
                (destination / "new_module.py").write_text(
                    "new module", encoding="utf-8"
                )

            with (
                patch.object(updater, "PROJECT_DIR", project_dir),
                patch.object(updater, "_download_archive", side_effect=create_archive_source),
                patch.object(updater, "_install_python_dependencies") as install_deps,
            ):
                updater.install_update(info)

            self.assertEqual(
                (project_dir / "obs_local" / "settings.ini").read_text(
                    encoding="utf-8"
                ),
                "local settings",
            )
            self.assertEqual(
                (project_dir / "config.ini").read_text(encoding="utf-8"),
                "private token",
            )
            self.assertEqual(
                (project_dir / "start_obs_local.sh").read_text(encoding="utf-8"),
                "local launcher",
            )
            self.assertTrue((project_dir / ".venv" / "keep").is_file())
            self.assertEqual(
                (project_dir / "obs_local" / "menu.py").read_text(
                    encoding="utf-8"
                ),
                "new source",
            )
            version = json.loads(
                (project_dir / updater.VERSION_FILE).read_text(encoding="utf-8")
            )
            self.assertEqual(version, {"branch": "main", "revision": "b" * 40})
            install_deps.assert_called_once_with()

    def test_archive_extraction_rejects_parent_directory_paths(self):
        archive_buffer = io.BytesIO()
        with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
            payload = b"unsafe"
            member = tarfile.TarInfo("repo-main/../outside.txt")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        archive_buffer.seek(0)

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(updater, "urlopen") as urlopen:
                response = Mock()
                response.read.return_value = archive_buffer.getvalue()
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=None)
                urlopen.return_value = response
                with self.assertRaises(updater.UpdateError):
                    updater._download_archive("main", Path(temp_dir))


if __name__ == "__main__":
    unittest.main()
