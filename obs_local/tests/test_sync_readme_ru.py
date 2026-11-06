import tempfile
import unittest
from pathlib import Path

import sync_readme_ru
from obs_local import sync_readme_ru as obs_sync_readme_ru


class CollectHeadingsTests(unittest.TestCase):
    def test_ignores_headings_inside_fenced_code_blocks(self):
        content = (
            "# Actual heading\n"
            "```sh\n"
            "# Shell comment\n"
            "## Also not a heading\n"
            "```\n"
            "~~~md\n"
            "### Not a heading either\n"
            "~~~\n"
        )

        temp_dir = "/dev/shm" if Path("/dev/shm").is_dir() else None
        with tempfile.TemporaryDirectory(dir=temp_dir) as directory:
            readme = Path(directory) / "README.md"
            readme.write_text(content, encoding="utf-8")

            for module in (sync_readme_ru, obs_sync_readme_ru):
                with self.subTest(module=module.__name__):
                    self.assertEqual(
                        module.collect_headings(readme),
                        {1: 1, 2: 0, 3: 0, 4: 0},
                    )


if __name__ == "__main__":
    unittest.main()
