import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import package_research as package


class ExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "source"
        self.parent = self.root / "output/packages"
        for name in package.REQUIRED:
            self.write_source(name, f"source {name}\n")

    def write_source(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")
        return path

    def test_allowlist_and_repeatable_archive(self):
        exclusions = (
            "papers/original.pdf",
            "library/pages/p001.txt",
            "library/search.sqlite3",
            "notes/notes.md",
            "output/reports/latest.md",
            "discovery/state.json",
            "discovery/runs/results.json",
            ".env",
            ".codex/auth.json",
            ".codex/sessions/private.jsonl",
            ".agents/skills/paper-reader/private.md",
            ".agents/skills/unrelated/SKILL.md",
            "scripts/__pycache__/papers.pyc",
        )
        for name in exclusions:
            self.write_source(name, "excluded")
        self.write_source("discovery/weekly-prompt.txt", "portable prompt")
        self.write_source(".codex/agents/other.toml", "agent configuration")

        tree, archive, skipped = package.export(self.root)
        names = {p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file()}
        expected = set(package.REQUIRED) | {
            "README.md",
            ".gitignore",
            package.MARKER,
            "discovery/weekly-prompt.txt",
            ".codex/agents/other.toml",
        }
        self.assertEqual(names, expected)
        self.assertEqual(skipped, [])
        self.assertEqual((tree / "README.md").read_bytes(), (tree / "docs/PACKAGE_README.md").read_bytes())
        with ZipFile(archive) as zip_file:
            self.assertEqual(zip_file.namelist(), [f"{package.NAME}/{name}" for name in sorted(expected)])
            for name in expected:
                self.assertEqual(zip_file.read(f"{package.NAME}/{name}"), (tree / name).read_bytes())
        first_zip = archive.read_bytes()
        package.export(self.root)
        self.assertEqual(archive.read_bytes(), first_zip)

    def test_rejects_symlink_to_a_private_file_or_directory(self):
        private_dir = self.root.parent / "private"
        private_dir.mkdir()
        private_file = private_dir / "evidence.md"
        private_file.write_text("private", encoding="utf-8")
        source = self.root / ".agents/skills/paper-reader/references/evidence.md"
        source.unlink()
        source.symlink_to(private_file)
        with self.assertRaisesRegex(ValueError, "Symlink is not allowed"):
            package.export(self.root)
        source.unlink()
        source.parent.rmdir()
        source.parent.symlink_to(private_dir, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symlink is not allowed"):
            package.export(self.root)
        self.assertFalse(self.parent.exists())

    def test_missing_sources_and_machine_paths_fail_before_creating_output(self):
        source = self.root / "AGENTS.md"
        source.unlink()
        with self.assertRaisesRegex(ValueError, "Missing required source file: AGENTS.md"):
            package.export(self.root)
        self.write_source("AGENTS.md", f"Private root: {self.root}")
        with self.assertRaisesRegex(ValueError, "Machine-specific absolute path in source file: AGENTS.md"):
            package.export(self.root)
        self.assertFalse(self.parent.exists())

    def test_absent_weekly_prompt_is_reported(self):
        _, _, skipped = package.export(self.root)
        self.assertEqual(skipped, ["discovery/weekly-prompt.txt"])

    def test_protects_unmanaged_and_edited_exports(self):
        tree = self.parent / package.NAME
        tree.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "unmanaged directory"):
            package.export(self.root)
        tree.rmdir()
        archive = self.parent / f"{package.NAME}.zip"
        archive.write_bytes(b"unmanaged")
        with self.assertRaisesRegex(ValueError, "unmanaged ZIP"):
            package.export(self.root)
        self.assertEqual(archive.read_bytes(), b"unmanaged")
        archive.unlink()

        package.export(self.root)
        (tree / ".git").mkdir()
        with self.assertRaisesRegex(ValueError, "export containing .git"):
            package.export(self.root)
        self.assertTrue((tree / ".git").exists())
        (tree / ".git").rmdir()
        (tree / "README.md").write_text("user edit", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "edited export"):
            package.export(self.root)
        self.assertEqual((tree / "README.md").read_text(encoding="utf-8"), "user edit")

    def test_restores_previous_tree_if_archive_replacement_fails(self):
        tree, archive, _ = package.export(self.root)
        previous_tree = (tree / "AGENTS.md").read_bytes()
        previous_archive = archive.read_bytes()
        self.write_source("AGENTS.md", "updated rules")
        with patch.object(package.os, "replace", side_effect=PermissionError("test failure")):
            with self.assertRaises(PermissionError):
                package.export(self.root)
        self.assertEqual((tree / "AGENTS.md").read_bytes(), previous_tree)
        self.assertEqual(archive.read_bytes(), previous_archive)

    def test_concurrent_publisher_cannot_replace_existing_export(self):
        tree, archive, _ = package.export(self.root)
        previous_archive = archive.read_bytes()
        lock = self.parent / f".{package.NAME}.lock"
        lock.mkdir()
        self.write_source("AGENTS.md", "updated rules")
        with self.assertRaisesRegex(ValueError, "already in progress"):
            package.export(self.root)
        self.assertTrue(lock.is_dir())
        self.assertNotEqual((tree / "AGENTS.md").read_text(encoding="utf-8"), "updated rules")
        self.assertEqual(archive.read_bytes(), previous_archive)


if __name__ == "__main__":
    unittest.main()
