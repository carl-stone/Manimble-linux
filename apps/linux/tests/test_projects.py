import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from manimble_linux.paths import AppPaths
from manimble_linux.projects import (PortableProject, PortableProjectError, create_new_deck, deck_url,
                                     new_deck_files, selected_movie_scenes, unpack_archive)
from manimble_linux.runtime import Runtime
from manimble_linux.workspace import Workspace

ROOT = Path(__file__).resolve().parents[3]


class ProjectsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="manimble-packages-", dir="/var/tmp")
        self.base = Path(self.temp.name)
        self.paths = AppPaths.from_environment({"HOME": str(self.base / "home")})
        self.deck = self.base / "source" / "project" / "deck"
        shutil.copytree(ROOT / "examples" / "hello" / "deck", self.deck)
        (self.deck.parent / "assets").mkdir()
        (self.deck.parent / "assets" / "asset.txt").write_text("asset")
        self.ws = Workspace.open(self.deck, self.paths)
        self.runtime = Runtime(sys.executable, "self", self.paths)

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args):
        result = subprocess.run(self.runtime.manimbler_command(*map(str, args)), env=self.runtime.child_env(), capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_source_only_with_and_without_order_extracts(self):
        for ordered in (True, False):
            with self.subTest(order=ordered):
                if not ordered:
                    (self.ws.work / "order.txt").unlink()
                archive = self.base / f"source-{ordered}.manimble"
                PortableProject.create_archive(self.ws, [], archive)
                destination = self.base / f"extract-{ordered}"
                extracted = unpack_archive(archive, destination, self.runtime)
                self.assertTrue((extracted / "slides.py").is_file())
                self.assertEqual((extracted / "order.txt").exists(), ordered)
                self.assertFalse((extracted / "AGENTS.md").exists())
                self.assertFalse((extracted / "slides").exists())
                with self.assertRaises(FileExistsError):
                    unpack_archive(archive, destination, self.runtime)

    def test_copy_includes_unsaved_work_without_advancing_baseline(self):
        (self.ws.work / "config.py").write_text("unsaved")
        before = self.ws.changed_files()
        target = self.base / "copy"
        PortableProject.create_folder(self.ws, [str(self.deck / "config.py")], target)
        self.assertEqual((deck_url(target) / "config.py").read_text(), "unsaved")
        self.assertEqual(self.ws.changed_files(), before)
        self.assertNotEqual((self.deck / "config.py").read_text(), "unsaved")
        self.assertEqual((deck_url(target).parent / "assets" / "asset.txt").read_text(), "asset")
        self.assertFalse(any(p.is_symlink() for p in target.rglob("*")))

    def test_rejects_existing_inside_source_outside_and_loops(self):
        target = self.base / "existing"
        target.mkdir()
        with self.assertRaises(FileExistsError):
            PortableProject.create_folder(self.ws, [], target)
        for target in (self.ws.work / "copy", self.deck / "copy", self.deck.parent / "assets" / "copy"):
            with self.assertRaises(PortableProjectError):
                PortableProject.create_archive(self.ws, [], target)
        outside = self.base / "private.txt"
        outside.write_text("private")
        with self.assertRaisesRegex(PortableProjectError, "outside"):
            PortableProject.create_folder(self.ws, [str(outside)], self.base / "outside-copy")
        (self.ws.work / "loop").symlink_to(self.ws.work, target_is_directory=True)
        with self.assertRaisesRegex(PortableProjectError, "loop"):
            PortableProject.create_folder(self.ws, [], self.base / "loop-copy")
        self.assertFalse((self.base / "loop-copy").exists())

    def test_failed_archive_does_not_replace_previous_success(self):
        target = self.base / "existing.manimble"
        PortableProject.create_archive(self.ws, [], target)
        before = target.read_bytes()
        with self.assertRaises(FileExistsError):
            PortableProject.create_archive(self.ws, [], target)
        with patch.object(PortableProject, "_materialize", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                PortableProject.create_archive(self.ws, [], target, overwrite=True)
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse(list(self.base.glob(".manimble-package-*")))

    def test_media_materialization_deduplicates_paths_and_preserves_fields(self):
        media = self.base / "external-cache"
        (media / "a").mkdir(parents=True)
        (media / "b").mkdir()
        (media / "a" / "clip.mp4").write_bytes(b"forward")
        (media / "b" / "clip.mp4").write_bytes(b"reverse")
        slides = self.ws.work / "slides"
        slides.mkdir()
        original = {"resolution": [1280, 720], "slides": [
            {"type": "video", "file": str(media / "a" / "clip.mp4"), "rev_file": str(media / "b" / "clip.mp4"),
             "src": "/old/missing.mp4", "notes": "notes", "playback_rate": 1.5, "reversed_playback_rate": 0.7, "loop": True},
            {"type": "image", "file": str(media / "a" / "clip.mp4"), "src": "/old/image.png", "auto_next": True}]}
        (slides / "Welcome.json").write_text(json.dumps(original))
        target = self.base / "media-copy"
        PortableProject.create_folder(self.ws, [], target)
        copied_deck = deck_url(target)
        copied = json.loads((copied_deck / "slides" / "Welcome.json").read_text())
        first, second = copied["slides"]
        self.assertIsNone(first["src"])
        self.assertIsNone(second["src"])
        self.assertEqual(first["file"], second["file"])
        self.assertNotEqual(first["file"], first["rev_file"])
        self.assertEqual((copied_deck / first["rev_file"]).read_bytes(), b"reverse")
        for key in ("type", "notes", "playback_rate", "reversed_playback_rate", "loop"):
            self.assertEqual(first[key], original["slides"][0][key])
        self.assertEqual(copied["resolution"], original["resolution"])

    def test_new_deck_quotes_title_and_preserves_unrelated_files(self):
        project = self.base / "new project"
        project.mkdir()
        (project / "unrelated.txt").write_text("keep")
        (project / "assets").mkdir()
        (project / "assets" / "old.txt").write_text("keep")
        title = 'A "title"\\with\nnewlines 😀'
        deck = create_new_deck(project, title, "paper")
        compile((deck / "slides.py").read_text(), str(deck / "slides.py"), "exec")
        self.assertIn(json.dumps(title, ensure_ascii=False), (deck / "slides.py").read_text())
        self.assertEqual((project / "assets" / "old.txt").read_text(), "keep")
        with self.assertRaises(FileExistsError):
            create_new_deck(project)
        self.assertEqual((project / "unrelated.txt").read_text(), "keep")

    def test_manifest_and_movie_selection_validation(self):
        project = self.base / "invalid-project"
        project.mkdir()
        for path in ("../outside", "/absolute", "x\\deck", "C:/deck"):
            (project / "manimble-project.json").write_text(json.dumps({"format": "manimble-project", "version": 1, "deckPath": path}))
            with self.assertRaises(PortableProjectError):
                deck_url(project)
        scenes = [{"name": "Title"}, {"name": "Hidden", "hidden": True}, {"name": "Summary"}]
        self.assertEqual(selected_movie_scenes(scenes, "3-1, summary;1"), ["Title", "Summary"])
        with self.assertRaisesRegex(ValueError, "hidden"):
            selected_movie_scenes(scenes, "2")
        with self.assertRaisesRegex(ValueError, "No such"):
            selected_movie_scenes(scenes, "400, missing")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "real media conversion requires FFmpeg")
    def test_real_hello_build_package_relocate_extract_and_convert(self):
        from manimble.core import Manimble
        # Formula genuinely requires unavailable LaTeX; exercise real hello text/geometry instead.
        (self.ws.work / "order.txt").write_text("Welcome\nShapes\n")
        engine = Manimble(str(self.ws.work), sys.executable, str(self.base / "render-cache"))
        try:
            for scene in ("Welcome", "Shapes"):
                result = engine.api_scene(scene)
                self.assertEqual(result.get("status"), "ready", result.get("error"))
            report = engine.dependency_report()
            self.assertEqual(report["scannedScenes"], report["totalScenes"])
            self.assertFalse(report["failures"], report["failures"])
            self.assertTrue(engine._build_animation_slides(engine.visible_scenes(), "export_movie"))
            # Include a real rendered still alongside the forward/reverse clips.
            manifest = self.ws.work / "slides" / "Shapes.json"
            data = json.loads(manifest.read_text())
            frame = engine.api_scene("Shapes")["stops"][-1]["png"]
            data["slides"].append({"type": "image", "file": frame, "rev_file": frame, "src": frame, "notes": "still", "playback_rate": 1.0})
            manifest.write_text(json.dumps(data))
            archive = self.base / "rendered.manimble"
            PortableProject.create_archive(self.ws, report["dependencies"], archive)
        finally:
            engine.close()
        moved = self.base / "relocated"
        moved.mkdir()
        archive.rename(moved / archive.name)
        self.ws.discard()
        shutil.rmtree(self.deck.parent)
        unpack_archive(moved / archive.name, moved / "unpacked", self.runtime)
        self.cli("--check", moved / archive.name)
        for kind in ("mp4", "pdf", "html"):
            output = moved / ("converted." + kind)
            self.cli("--convert", moved / archive.name, "--to", kind, "--output", output, "--hold", "0", "--fps", "24")
            self.assertTrue(output.exists())
            if output.is_file():
                self.assertGreater(output.stat().st_size, 0)


class ProjectQtTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_pdf_preserves_slide_aspect_and_existing_file_on_failure(self):
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QColor, QImage
        from PySide6.QtPdf import QPdfDocument
        from manimble_linux.project_controller import write_pdf
        with tempfile.TemporaryDirectory(prefix="manimble-pdf-", dir="/var/tmp") as temporary:
            base = Path(temporary)
            image = QImage(QSize(640, 360), QImage.Format.Format_ARGB32)
            image.fill(QColor("red"))
            self.assertTrue(image.save(str(base / "frame.png")))
            output = base / "slides.pdf"
            write_pdf([base / "frame.png", base / "frame.png"], output)
            doc = QPdfDocument()
            self.assertEqual(doc.load(str(output)), QPdfDocument.Error.None_)
            self.assertEqual(doc.pageCount(), 2)
            size = doc.pagePointSize(0)
            self.assertAlmostEqual(size.width() / size.height(), 16 / 9, places=2)
            doc.close()
            before = output.read_bytes()
            with self.assertRaises(OSError):
                write_pdf([base / "missing.png"], output)
            self.assertEqual(output.read_bytes(), before)

    def test_movie_options_defaults_and_script_disables_hold(self):
        from manimble_linux.project_controller import MovieExportOptions
        options = MovieExportOptions([{"name": "Title"}, {"name": "Summary"}], "Summary", has_script=True)
        self.assertEqual((options.height, options.frame_rate, options.hold_seconds), (720, 30, 2))
        options.choose_current_slide()
        self.assertEqual(options.selected_scenes(), ["Summary"])
        options.script.setChecked(True)
        self.assertTrue(options.with_script)
        self.assertFalse(options.hold.isEnabled())
        options.close()
        unavailable = MovieExportOptions([{"name": "Title"}], has_script=False)
        unavailable.script.setChecked(True)
        self.assertFalse(unavailable.with_script)
        unavailable.close()


if __name__ == "__main__":
    unittest.main()
