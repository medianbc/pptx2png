import io
import json
import subprocess
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
    def test_manifest_cannot_claim_preserved_secret_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            secret_path = project_dir / "config.ini"
            secret_path.write_text("token = keep\n", encoding="utf-8")
            (project_dir / updater.MANIFEST_FILE).write_text(
                json.dumps(["config.ini"]), encoding="utf-8"
            )

            with patch.object(updater, "PROJECT_DIR", project_dir):
                with self.assertRaises(updater.UpdateError):
                    updater._read_installed_manifest()

            self.assertEqual(secret_path.read_text(encoding="utf-8"), "token = keep\n")

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
            (project_dir / "removed_module.py").write_text(
                "obsolete", encoding="utf-8"
            )
            (project_dir / updater.MANIFEST_FILE).write_text(
                json.dumps(["removed_module.py"]), encoding="utf-8"
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
            self.assertFalse((project_dir / "removed_module.py").exists())
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
            manifest = json.loads(
                (project_dir / updater.MANIFEST_FILE).read_text(encoding="utf-8")
            )
            self.assertEqual(set(manifest), {"obs_local/menu.py", "new_module.py"})
            install_deps.assert_called_once_with()

    def test_failed_archive_replacement_restores_original_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "a.py").write_text("old a", encoding="utf-8")
            (project_dir / "z.py").write_text("old z", encoding="utf-8")
            info = updater.UpdateInfo(
                branch="main",
                local_revision="a" * 40,
                remote_revision="b" * 40,
                available=True,
                is_git_checkout=False,
            )

            def create_archive_source(_branch: str, destination: Path) -> None:
                (destination / "a.py").write_text("new a", encoding="utf-8")
                (destination / "z.py").write_text("new z", encoding="utf-8")

            real_replace = updater.os.replace

            def fail_on_second_source(source, target):
                if Path(source).name == "z.py":
                    raise OSError("simulated disk failure")
                return real_replace(source, target)

            with (
                patch.object(updater, "PROJECT_DIR", project_dir),
                patch.object(updater, "_download_archive", side_effect=create_archive_source),
                patch.object(updater.os, "replace", side_effect=fail_on_second_source),
            ):
                with self.assertRaisesRegex(updater.UpdateError, "восстановлены"):
                    updater._update_archive_install(info)

            self.assertEqual((project_dir / "a.py").read_text(), "old a")
            self.assertEqual((project_dir / "z.py").read_text(), "old z")

    def test_dependency_failure_does_not_mark_archive_revision_current(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / updater.VERSION_FILE).write_text(
                json.dumps({"branch": "main", "revision": "a" * 40}),
                encoding="utf-8",
            )
            info = updater.UpdateInfo(
                branch="main",
                local_revision="a" * 40,
                remote_revision="b" * 40,
                available=True,
                is_git_checkout=False,
            )

            def create_archive_source(_branch: str, destination: Path) -> None:
                (destination / "new_module.py").write_text(
                    "new source", encoding="utf-8"
                )

            with (
                patch.object(updater, "PROJECT_DIR", project_dir),
                patch.object(updater, "_download_archive", side_effect=create_archive_source),
                patch.object(
                    updater,
                    "_install_python_dependencies",
                    side_effect=updater.UpdateError("dependency failure"),
                ),
            ):
                with self.assertRaisesRegex(updater.UpdateError, "dependency failure"):
                    updater.install_update(info)

            version = json.loads(
                (project_dir / updater.VERSION_FILE).read_text(encoding="utf-8")
            )
            self.assertEqual(version["revision"], "a" * 40)

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


class GitCheckoutUpdateTests(unittest.TestCase):
    def _git(self, directory: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(directory), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def _create_checkout(self, root: Path) -> tuple[Path, Path, Path]:
        origin = root / "origin.git"
        seed = root / "seed"
        checkout = root / "checkout"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(origin)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "init", "--initial-branch=main", str(seed)],
            check=True,
            capture_output=True,
        )
        self._git(seed, "config", "user.name", "Updater Tests")
        self._git(seed, "config", "user.email", "updater-tests@example.invalid")
        (seed / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git(seed, "add", "tracked.txt")
        self._git(seed, "commit", "-m", "base")
        self._git(seed, "remote", "add", "origin", str(origin))
        self._git(seed, "push", "-u", "origin", "main")
        subprocess.run(
            [
                "git",
                "clone",
                "--single-branch",
                "--branch",
                "main",
                str(origin),
                str(checkout),
            ],
            check=True,
            capture_output=True,
        )
        self._git(checkout, "config", "user.name", "Updater Tests")
        self._git(checkout, "config", "user.email", "updater-tests@example.invalid")
        return origin, seed, checkout

    def _push_remote_change(self, seed: Path, content: str) -> str:
        (seed / "remote.txt").write_text(content, encoding="utf-8")
        self._git(seed, "add", "remote.txt")
        self._git(seed, "commit", "-m", "remote update")
        self._git(seed, "push", "origin", "main")
        return self._git(seed, "rev-parse", "HEAD")

    def _apply_update(self, checkout: Path, branch: str, revision: str) -> None:
        info = updater.UpdateInfo(
            branch=branch,
            local_revision=self._git(checkout, "rev-parse", "HEAD"),
            remote_revision=revision,
            available=True,
            is_git_checkout=True,
        )
        with patch.object(updater, "PROJECT_DIR", checkout):
            updater._update_git_checkout(info)

    def test_clean_checkout_fast_forwards(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, seed, checkout = self._create_checkout(Path(temp_dir))
            remote_revision = self._push_remote_change(seed, "remote\n")

            self._apply_update(checkout, "main", remote_revision)

            self.assertEqual(self._git(checkout, "rev-parse", "HEAD"), remote_revision)
            self.assertEqual((checkout / "remote.txt").read_text(), "remote\n")

    def test_fetch_head_updates_branch_without_remote_tracking_ref(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, seed, checkout = self._create_checkout(Path(temp_dir))
            self._git(seed, "switch", "-c", "release")
            (seed / "release.txt").write_text("release\n", encoding="utf-8")
            self._git(seed, "add", "release.txt")
            self._git(seed, "commit", "-m", "release update")
            self._git(seed, "push", "origin", "release")
            self._git(checkout, "switch", "-c", "release")
            remote_revision = self._git(seed, "rev-parse", "HEAD")
            self.assertNotEqual(
                subprocess.run(
                ["git", "-C", str(checkout), "show-ref", "--verify", "refs/remotes/origin/release"],
                check=False,
                capture_output=True,
                ).returncode,
                0,
            )

            self._apply_update(checkout, "release", remote_revision)

            self.assertEqual(self._git(checkout, "rev-parse", "HEAD"), remote_revision)
            self.assertEqual((checkout / "release.txt").read_text(), "release\n")

    def test_dirty_worktree_and_staged_selection_are_restored(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, seed, checkout = self._create_checkout(Path(temp_dir))
            remote_revision = self._push_remote_change(seed, "remote\n")
            (checkout / "tracked.txt").write_text("staged\n", encoding="utf-8")
            self._git(checkout, "add", "tracked.txt")
            (checkout / "tracked.txt").write_text("unstaged\n", encoding="utf-8")

            self._apply_update(checkout, "main", remote_revision)

            self.assertEqual(
                self._git(checkout, "show", ":tracked.txt"),
                "staged",
            )
            self.assertEqual((checkout / "tracked.txt").read_text(), "unstaged\n")
            self.assertIn("MM tracked.txt", self._git(checkout, "status", "--short"))
            self.assertEqual(self._git(checkout, "stash", "list"), "")

    def test_diverged_local_commit_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, seed, checkout = self._create_checkout(Path(temp_dir))
            (checkout / "local.txt").write_text("local\n", encoding="utf-8")
            self._git(checkout, "add", "local.txt")
            self._git(checkout, "commit", "-m", "local-only")
            local_revision = self._git(checkout, "rev-parse", "HEAD")
            remote_revision = self._push_remote_change(seed, "remote\n")

            with self.assertRaisesRegex(updater.UpdateError, "отдельные коммиты"):
                self._apply_update(checkout, "main", remote_revision)

            self.assertEqual(self._git(checkout, "rev-parse", "HEAD"), local_revision)
            self.assertFalse((checkout / "remote.txt").exists())

    def test_fetch_failure_restores_staged_and_worktree_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, _, checkout = self._create_checkout(root)
            (checkout / "tracked.txt").write_text("staged\n", encoding="utf-8")
            self._git(checkout, "add", "tracked.txt")
            (checkout / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
            self._git(checkout, "remote", "set-url", "origin", str(root / "missing.git"))
            revision = self._git(checkout, "rev-parse", "HEAD")

            with self.assertRaises(updater.UpdateError):
                self._apply_update(checkout, "main", revision)

            self.assertEqual(self._git(checkout, "show", ":tracked.txt"), "staged")
            self.assertEqual((checkout / "tracked.txt").read_text(), "unstaged\n")
            self.assertIn("MM tracked.txt", self._git(checkout, "status", "--short"))

    def test_stash_conflict_keeps_backup_in_stash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, seed, checkout = self._create_checkout(Path(temp_dir))
            (checkout / "tracked.txt").write_text("local edit\n", encoding="utf-8")
            (seed / "tracked.txt").write_text("remote edit\n", encoding="utf-8")
            self._git(seed, "add", "tracked.txt")
            self._git(seed, "commit", "-m", "conflicting remote edit")
            self._git(seed, "push", "origin", "main")
            remote_revision = self._git(seed, "rev-parse", "HEAD")

            with self.assertRaisesRegex(updater.UpdateError, "без конфликта"):
                self._apply_update(checkout, "main", remote_revision)

            self.assertIn("stash@{0}", self._git(checkout, "stash", "list"))
            self.assertEqual(self._git(checkout, "rev-parse", "HEAD"), remote_revision)


if __name__ == "__main__":
    unittest.main()
