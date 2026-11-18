"""Qt-independent checks for the shared engine's platform boundaries."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from manimble import beamer_import, export, importer, latex_packages, theme
from manimble.core import Manimble
from manimble.srcedit import Project, wrap_soft


LINUX = sys.platform.startswith("linux")


class EngineGuardsTest(unittest.TestCase):
    def test_exact_lookup_rejects_nonexistent_family(self):
        self.assertIsNone(importer._font("Manimble Definitely Missing Font 973821", False, False))

    def test_fontconfig_match_caches_file_and_face_index(self):
        importer._matched_fontconfig_face.cache_clear()
        self.addCleanup(importer._matched_fontconfig_face.cache_clear)
        reply = SimpleNamespace(returncode=0, stdout=f"Requested Font,Alias\n{__file__}\n3\n", stderr="")
        with patch.object(importer.subprocess, "run", return_value=reply) as run:
            for _ in range(2):
                match = importer._fontconfig_match("Requested Font", True, True, False)
                self.assertEqual(match.families, ("Requested Font", "Alias"))
                self.assertEqual((match.file, match.index), (__file__, 3))
            self.assertEqual(run.call_count, 1)
            args = run.call_args.args[0]
            self.assertEqual(args[0], "fc-match")
            self.assertIn(":weight=bold:slant=italic", args[-1])
            self.assertIsNone(importer._fontconfig_match("Absent Font", False, False, False))
            match = importer._fontconfig_match("Absent Font", False, False, True)
            self.assertEqual((match.file, match.index), (__file__, 3))
            self.assertEqual(run.call_count, 2)  # exact and substituted lookups share the reported face

    def test_missing_fontconfig_has_dependency_diagnostic(self):
        importer._matched_fontconfig_face.cache_clear()
        self.addCleanup(importer._matched_fontconfig_face.cache_clear)
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
            with patch.dict("os.environ", {"FONTCONFIG_FILE": str(config)}):
                self.assertEqual(importer.DEFAULT_FONT, "DejaVu Sans")
                for stem, bold, italic in (("DejaVuSans", False, False), ("DejaVuSans-Bold", True, False),
                                           ("DejaVuSans-Oblique", False, True), ("DejaVuSans-BoldOblique", True, True)):
                    face = importer._font_or_fallback("DejaVu Sans", bold, italic)
                    self.assertEqual(Path(face.path).stem, stem)
                missing = "Manimble Definitely Missing Font 973821"
                face = importer._font_or_fallback(missing, False, False)
                self.assertIsNotNone(face)
                match = importer._fontconfig_match(missing, False, False, True)
                self.assertEqual((face.path, face.index), (match.file, match.index))
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
        import matplotlib
        from xml.sax.saxutils import escape

        resources = Path(__file__).resolve().parents[1] / "resources" / "fonts"
        dejavu = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
        expected = {"midnight": "DejaVu Sans", "paper": "DejaVu Sans", "slate": "Nunito Sans",
                    "ember": "DejaVu Sans", "chalkboard": "Comic Neue", "parchment": "DejaVu Serif",
                    "terminal": "DejaVu Sans Mono"}
        with tempfile.TemporaryDirectory(prefix="manimble fonts & ") as tmp:
            root = Path(tmp)

            def config(name, rules):
                path = root / (name + ".conf")
                path.write_text(f"<fontconfig><cachedir>{escape(str(root / name))}</cachedir>{rules}</fontconfig>",
                                encoding="utf-8")
                return {**os.environ, "FONTCONFIG_FILE": str(path)}

            dirs = (dejavu, resources / "comicneue", resources / "nunitosans")
            rules = "".join(f"<dir>{escape(str(directory))}</dir>" for directory in dirs)
            rules += f"<include>{escape(str(resources / 'theme-aliases.conf'))}</include>"
            absent = config("absent", rules)
            # Append the Mac name to a real face's reported family list without using any Apple font bytes.
            installed = config("installed", rules + """
                <match target="scan">
                  <test name="family"><string>Comic Neue</string></test>
                  <edit name="family" mode="append" binding="strong"><string>Chalkboard SE</string></edit>
                </match>""")
            empty = config("empty", "<reset-dirs/>")

            for name, fallback in expected.items():
                with self.subTest(theme=name):
                    path = Path(theme.DIR) / (name + ".py")
                    original = path.read_bytes()
                    self.assertEqual(theme.load(name, env=absent)["FONT"], fallback)
                    self.assertEqual(path.read_bytes(), original)

            source = b'THEME = "Chalkboard"\nFONT = "Chalkboard SE"\nOTHER_FONT = "DejaVu Sans"\n'
            common = root / "common.py"
            common.write_bytes(source)
            deck = SimpleNamespace(deck=tmp, project=Project(tmp))
            with patch.dict(os.environ, {"FONTCONFIG_FILE": absent["FONTCONFIG_FILE"]}):
                self.assertEqual(theme.load("chalkboard", env=installed)["FONT"], "Chalkboard SE")
                self.assertEqual(importer.resolve_font_family("chalkboard se", env=installed), "chalkboard se")
                self.assertEqual(theme.load("chalkboard", env=empty)["FONT"], "Chalkboard SE")
                self.assertEqual(os.environ["FONTCONFIG_FILE"], absent["FONTCONFIG_FILE"])
                self.assertEqual(theme.load("chalkboard")["FONT"], "Comic Neue")
                self.assertEqual(theme.apply(deck, {"op": "get", "theme": "chalkboard"})["theme"], "Chalkboard")

                family = "Chalkboard SE"
                self.assertIsNone(importer._font(family, True, True))
                comic = importer._font_or_fallback(family, True, True)
                self.assertEqual(Path(comic.path).name, "ComicNeue-BoldItalic.ttf")
                self.assertEqual(importer.resolve_font_family("DejaVu Sans"), "DejaVu Sans")

                nunito = importer._font_or_fallback("Avenir Next", True, True)
                match = importer._fontconfig_match("Avenir Next", True, True, True)
                self.assertEqual((nunito.path, nunito.index), (match.file, match.index))
                self.assertEqual(nunito.getname(), ("Nunito Sans", "Bold Italic"))

            with patch.dict(os.environ, {"FONTCONFIG_FILE": installed["FONTCONFIG_FILE"]}):
                self.assertEqual(importer._font(family, True, True).path, comic.path)
            self.assertEqual(common.read_bytes(), source)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin keeps bundled theme font names")
    def test_darwin_theme_fonts_are_unchanged(self):
        with patch.object(importer, "resolve_font_family", side_effect=AssertionError("Linux-only lookup")):
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
        slot0 = Manimble._build_media(obj, 0)
        slot1 = Path(Manimble._build_media(obj, 1))
        self.assertEqual(slot1.name, "build-1")
        if LINUX:
            self.assertEqual(Path(slot0), slot1.parent / "build-0")
            self.assertEqual(slot1.parent.parent, obj.cache_dir / "media")
        else:
            self.assertEqual(slot0, "../media")
            self.assertEqual(slot1.parent, obj.cache_dir / "media")


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
