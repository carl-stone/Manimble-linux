import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from manimble_linux.paths import AppPaths
from manimble_linux.workspace import SaveConflict, Workspace, WorkspaceError

ROOT = Path(__file__).resolve().parents[3]


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="manimble-projects-", dir="/var/tmp")
        self.base = Path(self.temp.name)
        self.paths = AppPaths.from_environment({"HOME": str(self.base / "home")})
        self.deck = self.base / "project" / "deck"
        shutil.copytree(ROOT / "examples" / "hello" / "deck", self.deck)
        (self.deck / "package").mkdir()
        (self.deck / "package" / "helper.py").write_text("x = 1\n")
        (self.deck.parent / "assets").mkdir()
        (self.deck.parent / "assets" / "image.txt").write_text("asset")
        (self.deck / "CLAUDE.md").write_text("authored instructions")
        self.ws = Workspace.open(self.deck, self.paths)

    def tearDown(self):
        self.temp.cleanup()

    def test_edits_additions_deletions_isolated_until_save(self):
        original = (self.deck / "slides.py").read_bytes()
        (self.ws.work / "slides.py").write_bytes(original + b"\n# local\n")
        (self.ws.work / "new.py").write_text("x=2")
        (self.ws.work / "config.py").unlink()
        (self.ws.work / "package" / "helper.py").write_text("x=3")
        self.assertEqual((self.deck / "slides.py").read_bytes(), original)
        self.assertTrue((self.deck / "config.py").exists())
        self.assertFalse((self.deck / "new.py").exists())
        self.assertEqual(self.ws.changed_files(), ["config.py", "new.py", "package/helper.py", "slides.py"])
        self.ws.save()
        self.assertEqual((self.deck / "new.py").read_text(), "x=2")
        self.assertFalse((self.deck / "config.py").exists())
        self.assertEqual(self.ws.changed_files(), [])

    def test_conflicts_only_for_locally_changed_files(self):
        (self.ws.work / "config.py").write_text("local")
        (self.deck / "slides.py").write_text("unrelated external edit")
        self.assertEqual(self.ws.conflict_files(), [])
        self.ws.save()
        self.assertEqual((self.deck / "slides.py").read_text(), "unrelated external edit")
        (self.ws.work / "config.py").write_text("next local")
        (self.deck / "config.py").write_text("competing external edit")
        with self.assertRaises(SaveConflict):
            self.ws.save()
        self.assertEqual((self.deck / "config.py").read_text(), "competing external edit")
        self.ws.save(overwrite_conflicts=True)
        self.assertEqual((self.deck / "config.py").read_text(), "next local")

    def test_revert_uses_saved_baseline_not_external_disk(self):
        (self.ws.work / "config.py").write_text("saved")
        self.ws.save()
        (self.deck / "config.py").write_text("external")
        (self.ws.work / "config.py").unlink()
        (self.ws.work / "extra.py").write_text("extra")
        self.ws.revert()
        self.assertEqual((self.ws.work / "config.py").read_text(), "saved")
        self.assertFalse((self.ws.work / "extra.py").exists())
        self.assertEqual((self.deck / "config.py").read_text(), "external")
        self.assertFalse(self.ws.is_modified)

    def test_missing_original_retains_work_and_distinct_roots(self):
        second = Workspace.open(self.deck, self.paths)
        self.assertNotEqual(second.root, self.ws.root)
        self.ws.discard()
        self.assertTrue(second.work.exists())
        (second.work / "config.py").write_text("unsaved")
        shutil.rmtree(self.deck)
        with self.assertRaisesRegex(WorkspaceError, "Save Project Copy"):
            second.save()
        self.assertTrue(second.work.exists())
        second.discard()

    def test_agents_generated_output_and_link_targets_not_saved(self):
        self.assertEqual((self.ws.work / "CLAUDE.md").read_text(), "authored instructions")
        self.assertEqual(self.ws.own_files, {"AGENTS.md", "GEMINI.md"})
        for name in self.ws.own_files:
            (self.ws.work / name).write_text("changed generated instruction")
        for name in ("slides", "media", "__pycache__", ".manimble"):
            folder = self.ws.work / name
            folder.mkdir(exist_ok=True)
            (folder / "generated.py").write_text("generated")
        (self.ws.work / "temp.manimble.tmp").write_text("temporary")
        (self.ws.work / "asset-link").symlink_to(self.deck.parent / "assets" / "image.txt")
        self.assertFalse(self.ws.is_modified)
        self.assertTrue(self.ws.work.parent.joinpath("assets").is_symlink())

    def test_partial_save_keeps_failed_file_dirty(self):
        from manimble_linux import workspace
        for name in ("config.py", "slides.py"):
            (self.ws.work / name).write_text("changed " + name)
        actual = workspace.atomic_copy
        def copying(src, dst):
            if dst == self.deck / "slides.py":
                raise OSError("injected full destination")
            return actual(src, dst)
        with patch.object(workspace, "atomic_copy", side_effect=copying):
            with self.assertRaisesRegex(OSError, "full destination"):
                self.ws.save()
        self.assertEqual(self.ws.changed_files(), ["slides.py"])
        self.assertEqual((self.deck / "config.py").read_text(), "changed config.py")

    def test_changed_original_parent_symlink_never_writes_target(self):
        (self.ws.work / "package" / "helper.py").write_text("local")
        shutil.rmtree(self.deck / "package")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "helper.py").write_text("external")
        (self.deck / "package").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(WorkspaceError):
            self.ws.save(overwrite_conflicts=True)
        self.assertEqual((outside / "helper.py").read_text(), "external")


if __name__ == "__main__":
    unittest.main()
