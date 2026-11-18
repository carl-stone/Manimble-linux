"""Qt-independent checks for the shared engine's platform boundaries."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from manimble import beamer_import, export, importer, latex_packages, theme
from manimble.core import Manimble
from manimble.srcedit import wrap_soft


LINUX = sys.platform.startswith("linux")


class EngineGuardsTest(unittest.TestCase):
    def test_exact_lookup_rejects_nonexistent_family(self):
        self.assertIsNone(importer._font("Manimble Definitely Missing Font 973821", False, False))

    def test_fontconfig_match_caches_file_and_face_index(self):
        importer._fontconfig_match.cache_clear()
        self.addCleanup(importer._fontconfig_match.cache_clear)
        reply = SimpleNamespace(returncode=0, stdout="Requested Font,Alias\n/fonts/collection.ttc\n3\n", stderr="")
        with patch.object(importer.subprocess, "run", return_value=reply) as run:
            for _ in range(2):
                self.assertEqual(importer._fontconfig_match("Requested Font", True, True, False),
                                 ("/fonts/collection.ttc", 3))
            self.assertEqual(run.call_count, 1)
            args = run.call_args.args[0]
            self.assertEqual(args[0], "fc-match")
            self.assertIn(":weight=bold:slant=italic", args[-1])
            self.assertIsNone(importer._fontconfig_match("Absent Font", False, False, False))
            self.assertEqual(importer._fontconfig_match("Absent Font", False, False, True),
                             ("/fonts/collection.ttc", 3))

    def test_missing_fontconfig_has_dependency_diagnostic(self):
        importer._fontconfig_match.cache_clear()
        self.addCleanup(importer._fontconfig_match.cache_clear)
        with patch.object(importer.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(RuntimeError, "fc-match.*fontconfig"):
                importer._fontconfig_match("Missing tool test", False, False, True)

    @unittest.skipUnless(LINUX, "Linux uses Fontconfig/Pango substitution")
    def test_linux_fallback_loads_dejavu_and_substitutes_missing_font(self):
        # Use matplotlib's real bundled DejaVu faces without depending on the host's native font packages.
        import matplotlib
        from xml.sax.saxutils import escape
        with tempfile.TemporaryDirectory() as tmp:
            fonts = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
            config = Path(tmp) / "fonts.conf"
            config.write_text("<fontconfig><dir>" + escape(str(fonts)) + "</dir><cachedir>" +
                              escape(tmp) + "</cachedir></fontconfig>", encoding="utf-8")
            importer._font_cache.clear()
            importer._fontconfig_match.cache_clear()
            self.addCleanup(importer._font_cache.clear)
            self.addCleanup(importer._fontconfig_match.cache_clear)
            with patch.dict("os.environ", {"FONTCONFIG_FILE": str(config)}):
                self.assertEqual(importer.DEFAULT_FONT, "DejaVu Sans")
                for bold, italic in ((False, False), (True, False), (False, True), (True, True)):
                    face = importer._font_or_fallback("DejaVu Sans", bold, italic)
                    self.assertIn("DejaVu Sans", face.getname()[0])
                missing = "Manimble Definitely Missing Font 973821"
                face = importer._font_or_fallback(missing, False, False)
                self.assertIsNotNone(face)
                match = importer._fontconfig_match(missing, False, False, True)
                self.assertEqual(Path(face.path), Path(match[0]))
                self.assertIsNone(importer._font(missing, False, False))

    @unittest.skipUnless(LINUX, "Linux wrap measurement must follow Fontconfig substitution")
    def test_wrapped_uses_substituted_font_measurements(self):
        obj = {"font": "Manimble Definitely Missing Font 973821", "value": "iiii WWWW"}
        face = importer._font_or_fallback(obj["font"], False, False)
        measure = lambda value: face.getlength(value.rstrip()) / 1000
        expected = wrap_soft("iiii WWWW iii WWWW", 1.5 * measure(obj["value"]) / 4, measure)
        with patch.object(importer, "_font_or_fallback", return_value=face) as fallback, \
                patch.object(importer, "_font", side_effect=AssertionError("exact lookup used")):
            actual = Manimble._wrapped(None, obj, "iiii WWWW iii WWWW", 1.5, 4)
        self.assertEqual(actual, expected)
        fallback.assert_called_once_with(obj["font"], False, False)

    @unittest.skipUnless(LINUX, "Linux substitutes unavailable bundled Mac theme fonts")
    def test_theme_substitution_preserves_files_and_installed_original(self):
        expected = {"midnight": "DejaVu Sans", "paper": "DejaVu Sans", "slate": "DejaVu Sans",
                    "ember": "DejaVu Sans", "chalkboard": "DejaVu Sans", "parchment": "DejaVu Serif",
                    "terminal": "DejaVu Sans Mono"}
        for name, fallback in expected.items():
            path = Path(theme.DIR) / (name + ".py")
            original = path.read_bytes()
            with self.subTest(theme=name), patch.object(importer, "_font", return_value=None):
                self.assertEqual(theme.load(name)["FONT"], fallback)
            self.assertEqual(path.read_bytes(), original)
        with patch.object(importer, "_font", return_value=object()):
            self.assertEqual(theme.load("terminal")["FONT"], "Menlo")
            self.assertEqual(theme.load("parchment")["FONT"], "Palatino")

    @unittest.skipUnless(sys.platform == "darwin", "Darwin keeps bundled theme font names")
    def test_darwin_theme_fonts_are_unchanged(self):
        with patch.object(importer, "_font", side_effect=AssertionError("Linux-only lookup")):
            self.assertEqual(theme.load("terminal")["FONT"], "Menlo")
            self.assertEqual(theme.load("parchment")["FONT"], "Palatino")

    @unittest.skipUnless(LINUX, "Linux Beamer fallback fonts")
    def test_beamer_fallbacks_and_monospace_measurement(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj = object.__new__(beamer_import.BeamerImport)
            obj.sans = None
            obj.assets = Path(tmp)
            obj.warnings = []
            obj.tex_fonts = lambda files: {}
            obj.setup_fonts()
            self.assertEqual(obj.font, "DejaVu Sans")
            self.assertEqual(obj.mono, "DejaVu Sans Mono")
            obj.pil, obj.widths = {}, {}
            with patch.object(importer, "_font_or_fallback", wraps=importer._font_or_fallback) as lookup:
                self.assertGreater(obj.measure("Wiii", {"tt": True, "size": 12}), 0)
            lookup.assert_called_once_with("DejaVu Sans Mono", False, False)

    def test_beamer_font_existence_does_not_accept_substitution(self):
        obj = object.__new__(beamer_import.BeamerImport)
        obj.warnings = []
        obj.tex_fonts = lambda files: {}
        with patch.object(beamer_import, "pango_fonts", return_value=[]):
            self.assertEqual(obj.resolve_conditionals(
                r"\IfFontExistsTF{Manimble Definitely Missing Font 973821}{yes}{no}"), "no")

    @unittest.skipIf(sys.platform == "darwin", "Apple conversions remain supported on macOS")
    def test_keynote_guards_do_not_launch_apple_commands(self):
        with patch.object(subprocess, "run") as run, patch.object(subprocess, "Popen") as popen:
            for result in (importer.keynote_to_pptx("input.key", "out.pptx"),
                           importer.keynote_to_pptx("input.ppt", "out.pptx"),
                           export.pptx_to_keynote("input.pptx", "out.key")):
                self.assertFalse(result["ok"])
                self.assertIn("macOS", result["error"])
                self.assertIn(".pptx", result["error"])
            run.assert_not_called()
            popen.assert_not_called()

    @unittest.skipIf(sys.platform == "darwin", "Darwin retains its administrator consent flow")
    def test_protected_tex_never_launches_tlmgr_or_osascript(self):
        tools = {"tlmgr": "/opt/tex/bin/tlmgr", "kpsewhich": "/opt/tex/bin/kpsewhich"}
        with patch.object(latex_packages, "find_tool", side_effect=tools.get), \
                patch.object(latex_packages.os, "access", return_value=False), \
                patch.object(subprocess, "run", return_value=SimpleNamespace(stdout="/protected/tex\n")) as run, \
                patch.object(subprocess, "Popen") as popen:
            result = latex_packages.install(["dsfont.sty"])
            self.assertFalse(result["ok"])
            self.assertIn("apt install", result["error"])
            self.assertIn("pacman -S", result["error"])
            self.assertEqual(result["missing"], ["dsfont.sty"])
            run.assert_called_once_with([tools["kpsewhich"], "-var-value", "TEXMFROOT"],
                                        capture_output=True, text=True)
            popen.assert_not_called()
        with patch.object(latex_packages, "find_tool", side_effect=lambda name: "/usr/bin/tlmgr" if name == "tlmgr" else None), \
                patch.object(subprocess, "run") as run:
            self.assertFalse(latex_packages.install(["dsfont.sty"])["ok"])
            run.assert_not_called()

    @unittest.skipIf(sys.platform == "darwin", "User TeX install path on non-Darwin")
    def test_writable_user_tex_keeps_consented_install_flow(self):
        tools = {"tlmgr": "/home/user/tex/bin/tlmgr", "kpsewhich": "/home/user/tex/bin/kpsewhich"}
        with patch.object(latex_packages, "find_tool", side_effect=tools.get), \
                patch.object(latex_packages.os, "access", return_value=True), \
                patch.object(latex_packages, "package_of", return_value="doublestroke"), \
                patch.object(latex_packages, "absent", return_value=[]), \
                patch.object(subprocess, "run", side_effect=[SimpleNamespace(stdout="/home/user/tex\n"),
                                                             SimpleNamespace(returncode=0)]) as run:
            result = latex_packages.install(["dsfont.sty"])
            self.assertTrue(result["ok"])
            self.assertEqual(result["installed"], ["doublestroke"])
            self.assertEqual(run.call_args.args[0][:2], ["/bin/sh", "-c"])
            self.assertIn("install doublestroke", run.call_args.args[0][2])

    def test_build_media_slot_zero_preserves_platform_contract(self):
        obj = SimpleNamespace(cache_dir=Path("/private/cache"))
        expected = "/private/cache/media/build-0" if LINUX else "../media"
        self.assertEqual(Manimble._build_media(obj, 0), expected)
        self.assertEqual(Manimble._build_media(obj, 1), "/private/cache/media/build-1")


class ProjectFormatPortabilityTest(unittest.TestCase):
    def test_source_only_project_preserves_unicode_and_relative_layout(self):
        import json
        import zipfile
        from manimble_linux.paths import AppPaths
        from manimble_linux.projects import PortableProject, create_new_deck, deck_url
        from manimble_linux.workspace import Workspace
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            paths = AppPaths.from_environment({"HOME": str(base / "home")})
            deck = create_new_deck(base / "A project with spaces", 'Unicode 😀 and "quotes"')
            workspace = Workspace.open(deck, paths)
            archive = base / "portable.manimble"
            PortableProject.create_archive(workspace, [], archive)
            with zipfile.ZipFile(archive) as packed:
                names = packed.namelist()
                manifest_name = next(name for name in names if name.endswith("/manimble-project.json"))
                manifest = json.loads(packed.read(manifest_name))
                self.assertEqual(manifest["version"], 1)
                self.assertEqual(manifest["deckPath"], "A project with spaces/deck")
                self.assertTrue(all("\\" not in name and not name.startswith("/") for name in names))
                self.assertFalse(any(name.endswith("/AGENTS.md") for name in names))
            folder = base / "portable-folder"
            PortableProject.create_folder(workspace, [], folder)
            self.assertEqual((deck_url(folder) / "slides.py").read_text(), (deck / "slides.py").read_text())
            workspace.discard()

    def test_new_deck_and_manifest_checks_do_not_import_qt(self):
        from manimble_linux import projects, workspace
        result = subprocess.run(
            [sys.executable, "-c", "import sys; from manimble_linux import projects, workspace; assert not any(n.startswith('PySide6') for n in sys.modules)"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        source = projects.new_deck_files('a\nb\\c"d')
        compile(source["slides.py"], "slides.py", "exec")
        self.assertEqual(source["order.txt"], "Title\n")
        self.assertIn("pixel_width = 1280", source["manim.cfg"])
