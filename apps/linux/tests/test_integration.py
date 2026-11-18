"""Real Qt/engine tests; every editable file lives in a disposable hello copy."""
from __future__ import annotations
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from PySide6.QtCore import QSettings
from PySide6.QtTest import QTest
from manimble_linux.app import ManimbleApp
from manimble_linux.controller import MainController
from manimble_linux.models import stop_box
from manimble_linux.paths import AppPaths
from manimble_linux.runtime import Runtime
from manimble_linux.tasks import spawn


class RealEditorIntegration(unittest.TestCase):
    @classmethod
    def wait(cls, future, timeout=180):
        end = time.monotonic() + timeout
        while not future.done() and time.monotonic() < end:
            QTest.qWait(20)
        if not future.done():
            raise AssertionError('Timed out waiting for real engine: ' + cls.controller.rpc.log_tail())
        return future.result()

    @classmethod
    def setUpClass(cls):
        os.environ['MANIMBLE_SYNC_EDITS'] = '1'
        root = Path('/var/tmp/manimble-core-integration')
        root.mkdir(exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(dir=root)
        cls.root = Path(cls.temporary.name)
        checkout = Path(__file__).resolve().parents[3]
        cls.original = cls.root / 'hello' / 'deck'
        shutil.copytree(checkout / 'examples' / 'hello' / 'deck', cls.original)
        cls.original_files = {p.relative_to(cls.original): p.read_bytes() for p in cls.original.rglob('*') if p.is_file()}
        cls.paths = AppPaths.from_environment({
            'HOME': str(cls.root), 'XDG_CONFIG_HOME': str(cls.root / 'config'),
            'XDG_DATA_HOME': str(cls.root / 'data'), 'XDG_CACHE_HOME': str(cls.root / 'cache'),
            'XDG_STATE_HOME': str(cls.root / 'state')})
        cls.app = ManimbleApp.instance() or ManimbleApp(['manimble-test'])
        cls.settings = QSettings(str(cls.paths.settings_file), QSettings.Format.IniFormat)
        cls.controller = MainController(cls.paths, Runtime.discover(cls.paths, cls.settings), cls.settings, cls.app)
        cls.controller.show_window()
        cls.controller.launch()
        if not cls.wait(cls.controller.open(str(cls.original), confirmed=True)):
            raise AssertionError('The copied hello deck did not open')
        cls.controller._poll_timer.stop()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'controller'):
            cls.wait(spawn(cls.controller.finish()).future)
            cls.controller.window._approved_close = True
            cls.controller.window.close()
        if hasattr(cls, 'temporary'):
            cls.temporary.cleanup()
        os.environ.pop('MANIMBLE_SYNC_EDITS', None)

    def setUp(self):
        self.c = self.controller
        self.c.canvas.setFocus()
        self.wait(spawn(self.c.select_scene('Shapes')).future)
        self.wait(self.c.call('clear_history'))

    def test_open_and_render_real_frame(self):
        self.assertEqual(self.c.scene['status'], 'ready')
        self.assertTrue(Path(self.c.canvas.stop['png']).is_file())
        self.assertNotEqual(self.c.work_deck, self.original)
        self.assertFalse(self.c.canvas.texture(self.c.canvas.stop['png']).isNull())
        self.wait(spawn(self.c.select_scene('Welcome')).future)
        self.assertEqual(self.c.scene['status'], 'ready')
        for rel, data in self.original_files.items():
            self.assertEqual((self.original / rel).read_bytes(), data)

    def test_move_undo_redo_real_source_and_frame(self):
        oid = next(int(key) for key, obj in self.c.scene['objects'].items() if obj.get('value') == 'Rectangle')
        before = stop_box(self.c.canvas.stop, oid)
        source = self.c.scene['source']['text']
        result = self.wait(self.c.submit(lambda: self.c.run_edit({'op': 'move', 'oid': oid, 'dx': 30, 'dy': 20, 'build': True})))
        self.assertTrue(result['ok'], result)
        moved = stop_box(self.c.canvas.stop, oid)
        self.assertAlmostEqual(moved.cx - before.cx, 30, delta=2)
        self.assertAlmostEqual(moved.cy - before.cy, 20, delta=2)
        self.assertNotEqual(self.c.scene['source']['text'], source)
        self.wait(self.c.do_undo(False))
        undone = stop_box(self.c.canvas.stop, oid)
        self.assertAlmostEqual(undone.cx, before.cx, delta=2)
        self.wait(self.c.do_undo(True))
        redone = stop_box(self.c.canvas.stop, oid)
        self.assertAlmostEqual(redone.cx, moved.cx, delta=2)

    def test_source_syntax_error_writes_nothing(self):
        self.c.begin_source_editing()
        base = dict(self.c.source_edit_base)
        source = self.c.work_deck / base['file']
        before = source.read_bytes()
        invalid = base['text'] + '\n    def broken(\n'
        self.c.source_view.setPlainText(invalid)
        self.assertEqual(self.c.source_draft, invalid)
        result = self.wait(spawn(self.c.commit_source_draft()).future)
        self.assertFalse(result)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(self.c.source_draft, invalid)
        self.assertTrue(self.c.source_error_message)
        self.c.source_draft = self.c.source_edit_base = self.c.source_edit_scene_name = None
        self.c.source_focused = False
        self.c.render_source()

    def test_stale_preview_is_rejected(self):
        oid = next(int(key) for key, obj in self.c.scene['objects'].items() if obj.get('kind') == 'text')
        self.c.canvas.overrides.pop(oid, None)
        task = spawn(self.c.preview_sprite(oid, 'text', 'A real preview', 30))
        # The task starts on the next event; advance exactly its first generator step,
        # then invalidate that live request as a newer drag/draft would.
        end = time.monotonic() + 5
        key = (self.c.scene_name, oid)
        while key not in self.c._preview_generations and time.monotonic() < end:
            QTest.qWait(1)
        generation = self.c._preview_generations[key]
        self.c._preview_generations[key] = generation + 1
        self.assertFalse(self.wait(task.future))
        self.assertNotIn(oid, self.c.canvas.overrides)
        epoch = (self.c._connection_epoch, self.c._deck_epoch)
        self.assertFalse(self.c.preview_is_current(key, generation, epoch))

    def test_toast_hover_and_stale_timer(self):
        self.c.toast('Older message', error=True)
        self.c.schedule_toast_hide(0.01)
        self.c.toast('Newer error', error=True)
        QTest.qWait(30)
        self.assertFalse(self.c.toast_pill.isHidden())
        self.assertEqual(self.c.toast_pill.label.text(), 'Newer error')
        self.c.toast_pill.hovered = True
        self.c.schedule_toast_hide(0.01)
        QTest.qWait(30)
        self.assertFalse(self.c.toast_pill.isHidden())
        self.c.toast_pill.hovered = False
        self.c.schedule_toast_hide(0.01)
        QTest.qWait(30)
        self.assertTrue(self.c.toast_pill.isHidden())

    def test_new_build_adds_one_stop_to_static_and_animated_slides(self):
        for name in ('Shapes', 'Welcome'):       # Shapes only add()s; Welcome plays FadeIn
            with self.subTest(scene=name):
                self.wait(spawn(self.c.select_scene(name)).future)
                self.c.go_to_stop(len(self.c.scene['stops']) - 1)
                before = len(self.c.scene['stops'])
                result = self.wait(self.c.new_build())
                self.assertTrue(result['ok'], result)
                self.assertEqual(len(self.c.scene['stops']), before + 1)
                self.wait(self.c.do_undo(False))
                self.assertEqual(len(self.c.scene['stops']), before)

    @unittest.skipUnless(shutil.which('latex'), 'Formula rendering needs the native latex tool')
    def test_formula_real_render(self):
        self.wait(spawn(self.c.select_scene('Formula')).future)
        self.assertEqual(self.c.scene['status'], 'ready')
        self.assertGreaterEqual(len(self.c.scene['stops']), 3)


if __name__ == '__main__':
    unittest.main()
