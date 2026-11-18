"""The deck window and serialized, generation-aware engine integration."""
from __future__ import annotations

import inspect
import os
from pathlib import Path
import time
from concurrent.futures import Future
from PySide6.QtCore import QObject, Qt, QTimer, QSizeF
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QSplitter, QScrollArea, QStackedWidget, QMessageBox, QTextEdit, QPlainTextEdit, QLineEdit
from .host import DeckHost, EditContext
from .paths import AppPaths
from .runtime import Runtime
from .rpc import RpcClient, EngineError, EngineStopped
from .tasks import SerialQueue, spawn, resolved, failed, when_done
from .workers import run_blocking
from .models import render_dir, frame_size, unit_px, stop_box, char_styles, frame_box, table_cells, table_cell_at
from .actions import ActionRegistry
from .source_pane import SourceMixin
from .drawer import BottomDrawer, NotesMixin
from .setup_ui import SetupMixin, dependency_report
from . import prefs

from .project_controller import ProjectMixin
from .darkmode import DarkModeMixin
from .rulers import RulerMixin
from .toolbar import ToolbarMixin
from .navigator import NavigatorMixin
from .slide_tools import SlideToolsMixin, ArrangeMixin, HyperlinkMixin
from .formatbar import FormatBarMixin
from .background import BackgroundMixin
from .deck_themes import DeckThemesMixin
from .inspector import InspectorMixin
from .formula import FormulaMixin
from .find import FindMixin
from .clipboard import ClipboardMixin
from .feedback import FeedbackMixin
from .panegame import PaneOverlayMixin
from .presentation import PresentationMixin
from .script import ScriptMixin
from .extraction import ExtractMixin
from .plots import PlotMixin
from .biocartoon import BioCartoonMixin
from .biocartoon_library import BCLibraryMixin
from .latex_ui import LatexMixin
from .menus import MenusMixin


class DeckWindow(QMainWindow):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self._approved_close = False

    def closeEvent(self, event):
        if self._approved_close:
            event.accept()
            return
        event.ignore()
        self.controller.request_close()


class MainController(QObject, DeckHost, SourceMixin, NotesMixin, SetupMixin, ProjectMixin, DarkModeMixin,
                     RulerMixin, ToolbarMixin, NavigatorMixin, SlideToolsMixin, ArrangeMixin,
                     HyperlinkMixin, FormatBarMixin, BackgroundMixin, DeckThemesMixin, InspectorMixin,
                     FormulaMixin, FindMixin, ClipboardMixin, FeedbackMixin, PaneOverlayMixin,
                     PresentationMixin, ScriptMixin, ExtractMixin, PlotMixin, BioCartoonMixin,
                     BCLibraryMixin, LatexMixin, MenusMixin):
    def __init__(self, paths=None, runtime=None, settings=None, app=None):
        super().__init__()
        self.paths = paths or AppPaths.from_environment()
        self.settings = settings or prefs.open_settings(self.paths)
        self.app = app or QApplication.instance()
        self.runtime = runtime
        self.rpc = None
        self.workspace = None
        self.state = self.scene = self.scene_name = None
        self.stop_index = 0
        self._selection = None
        self._busy = 0
        self.busy_text = 'Rendering…'
        self.busy_progress = self.render_eta = None
        self.verifying = False
        self.shown_dir = None
        self.pending_deck = None
        self.last_changed = self.last_build_move = None
        self.pending_moves = {}
        self.pending_texts = {}
        self.nudge = {}
        self.shared_choice = True
        self.aspect_locked = True
        self.show_unused_slides = False
        self.show_pending = False
        self.player = self.presenter = self.animated_player = self.animation_preview = None
        self.preview_scene = None
        self.feedback = self.feedback_pane = None
        self.text_field = None
        self.table_cells = []
        self.source_draft = None
        self.renumbered = None
        self._queue = SerialQueue()
        self._connection_epoch = self._deck_epoch = self._scene_generation = 0
        self._preview_generations = {}
        self._pending_identity_refs = []
        self._table_edit_target = None
        self._closing = self._polling = False
        self._dirty_scan = False
        self._dirty = False
        self.window = DeckWindow(self)
        self.window.setWindowTitle('Manimble')
        self.window.resize(1500, 920)
        self.actions = ActionRegistry(self.window)
        self.build_ui()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(300)
        self._refresh_timer.timeout.connect(self._refresh_invalidated)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(2000)
        self._poll_timer.timeout.connect(self._poll)
        self._nudge_timer = QTimer(self)
        self._nudge_timer.setSingleShot(True)
        self._nudge_timer.setInterval(350)
        self._nudge_timer.timeout.connect(self.flush_nudges)

    @property
    def work_deck(self):
        return self.workspace.work if self.workspace else None

    @property
    def original_deck(self):
        return self.workspace.original if self.workspace else None

    @property
    def edit_context(self):
        return EditContext(self.scene_name, self.stop_index, Path(self.shown_dir).name if self.shown_dir else None)

    @property
    def submit_context(self):
        return self._queue.context

    @property
    def queued_work(self):
        return self._queue.pending

    @property
    def queue_tail(self):
        return self._queue.tail

    @property
    def edits_in_flight(self):
        return self.queued_work > 0 or self.verifying

    @property
    def obj_info(self):
        return (self.scene or {}).get('objects', {}).get(str(self.selection))

    @property
    def selection(self):
        return self._selection

    @selection.setter
    def selection(self, value):
        self._selection = value
        if hasattr(self, 'canvas'):
            self.canvas.selection = value
            self.canvas.viewport().update()
            self._feature('rebuild_inspector')
            self.highlight_source()
            self.actions.refresh()

    @property
    def busy(self):
        return self._busy

    @busy.setter
    def busy(self, value):
        self._busy = max(0, value)
        if hasattr(self, 'busy_pill'):
            self.update_busy_pill()

    def _feature(self, name, *args, **kwargs):
        """Dispatch a feature hook without masking missing integration methods."""
        return getattr(self, name)(*args, **kwargs)

    def build_ui(self):
        from .canvas import CanvasView
        from .widgets import Pill, SlideTable, GrabSplitView
        self.canvas = CanvasView()
        self.canvas.show_grid = prefs.get_bool('showGrid')
        self.canvas.snap_enabled = prefs.get_bool('snapObjects', True)
        self.canvas.on_select = lambda oid: setattr(self, 'selection', oid)
        for callback, method in [('on_move', 'commit_move'), ('on_resize', 'commit_resize'),
                                 ('on_wrap', 'commit_wrap'), ('on_endpoints', 'commit_endpoints'),
                                 ('on_edit', 'begin_edit'), ('on_nudge', 'nudge_object'),
                                 ('on_placeholder', 'begin_placeholder'), ('on_placeholder_delete', 'remove_placeholder'),
                                 ('on_many_edits', 'as_one_undo_step')]:
            setattr(self.canvas, callback, getattr(self, method))
        self.canvas.on_cell_tab = self.advance_cell_edit
        for callback, method in [('on_delete', 'delete_selected'), ('on_draw', 'draw_finished'),
                                 ('on_pen', 'pen_finished'), ('on_context_menu', 'canvas_menu'),
                                 ('on_guides_change', 'save_guides'), ('on_zoom_change', 'sync_zoom_popup'),
                                 ('on_draw_tool_change', 'sync_format_bar'), ('on_corner_action', 'rerun_plot'),
                                 ('on_corner_edit', 'edit_plot')]:
            setattr(self.canvas, callback, lambda *a, _m=method: self._feature(_m, *a))
        self.canvas.has_corner_action = lambda oid: self._feature('plot_of', oid) is not None
        self.canvas.on_icon_drop = lambda data, at: self._feature('biocartoon_dropped', data, at)
        self.toast_pill, self.busy_pill = Pill(), Pill()
        self.busy_pill.on_cancel = self.cancel_render
        self.table = SlideTable()
        self._feature('init_navigator', self.table)
        self.steps_bar = QWidget()
        self.steps_bar.setLayout(QHBoxLayout())
        self.steps_bar.layout().setContentsMargins(8, 4, 8, 4)
        stage = QWidget()
        stage_layout = QVBoxLayout(stage)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        from .rulers import RulerView
        self.stage_area = QWidget()
        grid = QGridLayout(self.stage_area)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(0)
        self.h_ruler = RulerView('horizontal', self.canvas)
        self.v_ruler = RulerView('vertical', self.canvas)
        self.ruler_corner = QWidget()
        self.ruler_corner.setFixedSize(RulerView.thickness, RulerView.thickness)
        grid.addWidget(self.ruler_corner, 0, 0)
        grid.addWidget(self.h_ruler, 0, 1)
        grid.addWidget(self.v_ruler, 1, 0)
        grid.addWidget(self.canvas, 1, 1)
        grid.setRowStretch(1, 1)
        grid.setColumnStretch(1, 1)
        self.canvas.on_geometry_change = self.redraw_rulers
        self.canvas.on_cursor_move = self.cursor_moved
        stage_layout.addWidget(self.busy_pill)
        stage_layout.addWidget(self.stage_area, 1)
        stage_layout.addWidget(self.steps_bar)
        stage_layout.addWidget(self.toast_pill)
        self.build_notes_pane()
        from .terminal import TerminalPanel
        self.terminal = TerminalPanel(controller=self)
        self.drawer = BottomDrawer(self.notes_box, self.build_notes_box, self.alt_text_box, self.terminal)
        self.engine_log = QPlainTextEdit()
        self.engine_log.setReadOnly(True)
        self.engine_log.setMaximumBlockCount(400)
        self.drawer.add_page('log', self.engine_log, 'Engine Log')
        self.drawer.on_hide = lambda: self.set_drawer_open(False)
        self.drawer.will_change_page = lambda _: self.commit_drawer()
        self.drawer.on_insert_image = lambda: self._feature('insert_notes_image')
        self.center_split = GrabSplitView(Qt.Orientation.Vertical, settings_key='manimble.drawer.sizes')
        self.center_split.collapses_last_pane = True
        self.center_split.addWidget(stage)
        self.center_split.addWidget(self.drawer)
        self.center_split.setSizes([660, 150])
        self.drawer.setVisible(prefs.get_bool('showNotes', True))
        self.inspector = QWidget()
        self.inspector.setLayout(QVBoxLayout())
        self.inspector_scroll = QScrollArea()
        self.inspector_scroll.setWidgetResizable(True)
        self.inspector_scroll.setWidget(self.inspector)
        self.inspector_host = QStackedWidget()
        self.inspector_host.addWidget(self.inspector_scroll)
        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(self.inspector_host)
        right.addWidget(self.make_source_pane())
        right.setSizes([350, 450])
        self.main_split = GrabSplitView(settings_key='manimble.split.sizes')
        self.main_split.collapses_edge_panes = True
        self.main_split.addWidget(self.table)
        self.main_split.addWidget(self.center_split)
        self.main_split.addWidget(right)
        self.main_split.setSizes(prefs.get_json('manimble.split.sizes', [220, 920, 360]))
        self.main_split.splitterMoved.connect(lambda *_: prefs.set('manimble.split.sizes', self.main_split.sizes()))
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        self.format_bar = self._feature('make_format_bar')
        if self.format_bar is not None:
            layout.addWidget(self.format_bar)
        layout.addWidget(self.main_split, 1)
        self.window.setCentralWidget(root)
        self._feature('init_dark_mode')
        self.apply_ruler_visibility()
        self._feature('init_find')
        self._feature('init_script')
        self._feature('init_latex')
        layout.insertWidget(1, self.latex_banner)
        self._feature('init_presentation')
        self._feature('rebuild_inspector')
        from . import menus
        menus.register_actions(self, self.actions)
        menus.build_menus(self, self.actions)
        self._canvas_context_commands = []
        for i, text in enumerate(('Show Credit…', 'Add to Local BioCartoon Library…', 'Upload to Online BioCartoon Library…')):
            self.actions.add(f'canvas_bio_{i}', text, lambda i=i: self._canvas_context_commands[i][1]())
        toolbar = self._feature('make_toolbar')
        if toolbar is not None:
            self.window.addToolBar(toolbar)
        self.toast_pill.hide()
        self.busy_pill.hide()

    def show_window(self):
        self.window.show()
        self.canvas.setFocus()
        screenshot = os.environ.get('MANIMBLE_SCREENSHOT')
        if screenshot:
            QTimer.singleShot(18000, lambda: self.window.grab().save(screenshot))

    def start(self):
        def begin():
            try:
                if self.runtime is None:
                    self.runtime = Runtime.discover(self.paths, self.settings)
                report, invalid, tools = yield run_blocking(dependency_report, self.runtime)
                if invalid:
                    self.show_setup(report)
                    return False
                self.launch()
                if tools:
                    self.toast('Some features need additional tools. See Manimble → Install Dependencies…', error=True)
                return True
            except Exception as exc:
                self.show_setup(str(exc))
                return False
        return spawn(begin(), 'start').future

    def launch(self, python=None):
        if python is not None:
            self.runtime = Runtime(str(python), 'saved', self.paths)
        self._connection_epoch += 1
        self.rpc = RpcClient(self.runtime, self.paths.logs / 'engine.log', self)
        self.rpc.event.connect(self.handle_event)
        self.rpc.stopped.connect(self.engine_stopped)
        self.rpc.start()
        self.rpc.log_line.connect(self.engine_log.appendPlainText)
        self._feature('init_project')
        self._poll_timer.start()
        if self.pending_deck is not None:
            pending, self.pending_deck = self.pending_deck, None
            self.open(pending)

    def call(self, cmd, args=None):
        if self.rpc is None:
            return failed(EngineStopped('The Manimble engine is not running'))
        return self.rpc.request(cmd, args or {})

    def submit(self, make):
        context = self.edit_context
        epoch = (self._connection_epoch, self._deck_epoch)
        def guarded():
            if epoch != (self._connection_epoch, self._deck_epoch):
                raise EngineStopped('The deck or engine changed before this edit could run; it was not replayed.')
            result = make()
            if inspect.isgenerator(result):
                return (yield from result)
            if isinstance(result, Future):
                return (yield result)
            return result
        future = self._queue.submit(guarded, context)
        when_done(future, self._report_failure)
        return future

    def _report_failure(self, future):
        if not future.cancelled() and future.exception():
            self.toast(str(future.exception()), error=True)

    def open(self, deck, confirmed=False):
        if self.rpc is None or not self.rpc.running:
            self.pending_deck = str(deck)
            self.busy_pill.show('Waiting for Manimble engine…', spinning=True, cancellable=False)
            return resolved(False)
        def opening():
            from .workspace import Workspace
            from .projects import RecentDecks
            if not confirmed:
                if self.edits_in_flight:
                    yield from self.finish_edits()
                if not (yield from self.confirm_close('opening another deck')):
                    return False
            self.stop_feature_workers()
            self.canvas.setFocus()
            self.busy += 1
            try:
                workspace = yield run_blocking(Workspace.open, deck, self.paths)
                old = self.workspace
                if self.terminal.has_started:
                    yield self.terminal.stop_all()
                self.workspace = workspace
                self._deck_epoch += 1
                if old:
                    yield run_blocking(old.discard)
                self.scene_name = self.scene = self.shown_dir = None
                self.selection = None
                self.source_draft = self.source_edit_base = self.source_edit_scene_name = None
                self.source_focused = False
                self.pending_moves.clear()
                self.pending_texts.clear()
                self._preview_generations.clear()
                self.canvas.stop = None
                for box in (self.notes_box, self.build_notes_box):
                    box.image_base = workspace.work
                feedback = self._feature('open_feedback', workspace)
                if inspect.isgenerator(feedback):
                    yield from feedback
                script = self._feature('open_script', workspace)
                if inspect.isgenerator(script):
                    yield from script
                yield from self.reopen_working_copy()
                prefs.set('lastDeck', str(deck))
                RecentDecks.note(deck, self.settings)
                self.update_document_state()
                latex = self._feature('check_latex_packages')
                if inspect.isgenerator(latex):
                    yield from latex
                return True
            except Exception as exc:
                self.alert('Could not open deck', str(exc))
                return False
            finally:
                self.busy -= 1
        return self._watch(spawn(opening(), 'open deck').future)

    def reopen_working_copy(self):
        from .workspace import Workspace
        if not self.workspace:
            return
        cache = yield run_blocking(Workspace.cache_dir, self.workspace.original, self.paths)
        self.busy += 1
        try:
            yield self.call('open', {'deck': str(self.workspace.work), 'cache': str(cache)})
            yield from self.reload_state()
            names = [row['name'] for row in self.navigator_scenes]
            if names:
                yield from self.select_scene(self.scene_name if self.scene_name in names else names[0])
            self._feature('load_guides')
        finally:
            self.busy -= 1

    def engine_stopped(self, code, log=''):
        self._connection_epoch += 1
        self._poll_timer.stop()
        self.verifying = False
        self.update_busy_pill()
        message = f'The engine exited with status {code}.\n\n{log}'
        if self.workspace:
            message += '\n\nUnsaved edits remain in this working copy. Restart keeps them, but engine undo history is lost. Unconfirmed edits are not replayed.'
        box = QMessageBox(QMessageBox.Icon.Critical, 'Manimble engine stopped', message, parent=self.window)
        restart = box.addButton('Restart Engine', QMessageBox.ButtonRole.AcceptRole) if self.workspace else None
        box.addButton(QMessageBox.StandardButton.Close)
        box.setDefaultButton(QMessageBox.StandardButton.Close)
        box.finished.connect(lambda _: self.restart_engine() if restart and box.clickedButton() is restart else None)
        self._engine_error = box
        box.open()

    def restart_engine(self):
        self.launch()
        self._watch(spawn(self.reopen_working_copy()).future)

    def _watch(self, future):
        when_done(future, self._report_failure)
        return future

    @property
    def navigator_scenes(self):
        state = self.state or {}
        return state.get('scenes', []) + (state.get('unusedScenes', []) if self.show_unused_slides else [])

    def _poll(self):
        if self.workspace and not self.busy and not self.queued_work and not self._polling and self.rpc and self.rpc.running:
            self._polling = True
            def poll():
                try:
                    yield from self.reload_state()
                finally:
                    self._polling = False
            self._watch(spawn(poll()).future)

    def _refresh_invalidated(self):
        if self.workspace and self.rpc and self.rpc.running:
            self._watch(spawn(self.reload_state()).future)
            self._feature('fetch_all_build_pages')
            if self.show_pending:
                self._feature('refresh_show_pages')

    def handle_event(self, event):
        if self.handle_presentation_event(event):
            return
        kind = event.get('event')
        if kind == 'render_progress':
            if event.get('name') != self.scene_name:
                return
            now = time.monotonic()
            if event.get('step') == 0:
                self.render_eta = (now, event.get('estimate', 3))
                self.busy_progress = None
            elif self.render_eta and event.get('fraction', 0) >= .05:
                start, total = self.render_eta
                self.render_eta = (start, .5 * total + .5 * (now - start) / event['fraction'])
            self.update_busy_pill()
            return
        if kind == 'edit_failed':
            self.last_build_move = None
            self.verifying = False
            self.toast(event.get('error', 'Edit failed') + ' (undone)', error=True)
            self._refresh_timer.start()
            return
        if kind == 'rendered':
            # Unversioned events invalidate state; their object ids never replace live state directly.
            self._refresh_timer.start()
            return
        handlers = {'present': 'handle_present_event', 'animation_preview': 'handle_animation_preview_event',
                    'export_movie': 'finish_scripted_movie', 'import': 'handle_import_event', 'export': 'handle_export_event'}
        handler = getattr(self, handlers.get(kind, ''), None)
        if handler and handler(event):
            return
        phase = event.get('phase')
        if phase in ('building', 'encoding'):
            done, total = event.get('done', 0), event.get('total', 0)
            self.busy_text = f"{kind.replace('_', ' ').title()} {done + 1}/{total}…"
            self.busy_progress = done / total if total else None
            self.busy_pill.show(self.busy_text, spinning=True, progress=self.busy_progress)
        elif phase == 'done':
            self.busy_pill.hide()
            self.toast(f"Completed: {event.get('path', kind)}")
            if kind == 'import' and event.get('deck'):
                self.app.open_path(event['deck'])
        elif phase == 'cancelled':
            self.busy_pill.hide()
            self.toast('Cancelled')
        elif event.get('error') or phase == 'failed':
            self.toast(event.get('error', f'{kind} failed'), error=True)
        self._refresh_timer.start()

    def reload_state(self):
        if not self.workspace:
            return
        epoch = (self._connection_epoch, self._deck_epoch)
        state = yield self.call('state', {'current': self.scene_name or ''})
        if epoch != (self._connection_epoch, self._deck_epoch):
            return
        old_rows = self.navigator_scenes
        self.state = state
        self._feature('reload_all_slide_rows')
        self.update_document_state()
        rows = self.navigator_scenes
        names = [r['name'] for r in rows]
        if self.scene_name and not self.busy and not self.edits_in_flight and self.source_draft is None:
            if self.scene_name not in names:
                old_names = [r['name'] for r in old_rows]
                index = old_names.index(self.scene_name) if self.scene_name in old_names else 0
                if rows:
                    yield from self.select_scene(rows[min(index, len(rows) - 1)]['name'])
                else:
                    self.scene_name = self.scene = self.shown_dir = None
                    self.selection = None
                    self.canvas.stop = None
                    self.render_source()
                return
        row = next((r for r in rows if r['name'] == self.scene_name), None)
        if row and row.get('status') == 'ready' and row.get('thumb') and self.shown_dir and not self.busy and not self.queued_work:
            if str(Path(row['thumb']).parent) != self.shown_dir:
                yield from self.load_scene(self.scene_name, True, True)
        self.actions.refresh()

    def select_scene(self, name):
        if name != self.scene_name:
            self.commit_drawer()
            if self.source_focused:
                self.canvas.setFocus()
            if self.source_draft is not None:
                yield from self._queue.drained()
                if self.source_draft is not None:
                    self.toast('Fix the Python code before changing slides', error=True)
                    self.source_view.setFocus()
                    return False
            self._feature('close_animation_preview')
            self.flush_nudges()
            self.canvas.overrides.clear()
            self.canvas.line_overrides.clear()
            self.canvas.select_group([])
            self._preview_generations = {key: generation + 1 for key, generation in self._preview_generations.items()}
        self.scene_name = name
        self.stop_index = 0
        self.selection = None
        self._feature('refresh_rings')
        yield from self.load_scene(name, False)
        return True

    def renumber_objects(self, name, scene):
        self.renumbered = None
        old, new = self.edit_context.seen, render_dir(scene)
        if self.scene_name != name or not old or not new or old == new:
            return
        prefix = name + ':'
        ids = set(self.canvas.selected_ids) | set(self.canvas.overrides) | set(self.canvas.line_overrides)
        if self.canvas.editing_text is not None:
            ids.add(self.canvas.editing_text)
        ids.update(int(k[len(prefix):]) for k in self.pending_texts if k.startswith(prefix))
        ids.update(int(k[len(prefix):]) for k in self.pending_moves if k.startswith(prefix))
        if not ids:
            return
        result = yield self.call('resolve_oids', {'scene': name, 'from': old, 'to': new, 'oids': sorted(ids)})
        mapping = result.get('map', {})
        def remap(oid):
            value = mapping.get(str(oid), oid)
            return int(value) if value is not None else None
        self.renumbered = remap
        self.canvas.renumber(remap(self.selection) if self.selection is not None else None, remap)
        self.selection = remap(self.selection) if self.selection is not None else None
        for table in (self.pending_texts, self.pending_moves):
            values, keys = {}, {}
            for key, value in list(table.items()):
                if key.startswith(prefix):
                    table.pop(key)
                    oid = remap(int(key[len(prefix):]))
                    keys[key] = prefix + str(oid) if oid is not None else None
                    if keys[key] is not None:
                        values[keys[key]] = value
            table.update(values)
            for reference in self._pending_identity_refs:
                if reference['table'] is table and reference['key'] in keys:
                    reference['key'] = keys[reference['key']]
        self._preview_generations = {(scene_name, remap(oid) if scene_name == name else oid): value
                                     for (scene_name, oid), value in self._preview_generations.items()
                                     if scene_name != name or remap(oid) is not None}

    def load_scene(self, name, keep_stop, keep_overrides=False):
        self._scene_generation += 1
        generation = self._scene_generation
        epoch = (self._connection_epoch, self._deck_epoch)
        self.busy += 1
        try:
            scene = yield self.call('scene', {'name': name})
            if name != self.scene_name or generation != self._scene_generation or epoch != (self._connection_epoch, self._deck_epoch):
                return
            yield from self.renumber_objects(name, scene)
            if name != self.scene_name or generation != self._scene_generation:
                return
            if scene.get('status') == 'cancelled':
                self.verifying = False
                self.toast('Cancelled')
                return
            if scene.get('status') != 'ready' or not scene.get('stops'):
                # Keep last-good canvas independently of failed source/scene diagnostics.
                self.scene = scene
                self.verifying = False
                self.render_source()
                self.show_source_error(scene.get('error', 'Slide failed to render'))
                self.toast(scene.get('error', 'Slide failed to render'), error=True)
                return
            self.scene = scene
            self.verifying = bool(scene.get('optimistic'))
            self.shown_dir = str(Path(scene['stops'][0]['png']).parent)
            self.stop_index = min(self.stop_index, len(scene['stops']) - 1) if keep_stop else 0
            if self.selection is not None and str(self.selection) not in scene.get('objects', {}):
                self.selection = None
            if not keep_overrides:
                self.canvas.overrides.clear()
                self.canvas.line_overrides.clear()
            self.notes_box.set_text(scene.get('notes') or '')
            self.alt_text_box.set_text(scene.get('altText') or '')
            self.apply_stop()
            self._feature('rebuild_steps')
            self._feature('refresh_build_rows')
            active = QApplication.focusWidget()
            if not active or not self.inspector.isAncestorOf(active):
                self._feature('rebuild_inspector')
            self.refresh_inspector_text()
            self.render_source()
        finally:
            self.busy -= 1

    def apply_stop(self):
        stops = (self.scene or {}).get('stops', [])
        if not stops:
            return
        self.stop_index = max(0, min(self.stop_index, len(stops) - 1))
        self.canvas.frame_size = QSizeF(*frame_size(self.scene))
        self.canvas.unit_px = unit_px(self.scene)
        self.canvas.scene_objects = self.scene.get('objects', {})
        self.canvas.placeholders = self.scene.get('placeholders', [])
        self.canvas.optimistic = self.verifying
        self.canvas.stop = stops[self.stop_index]
        self.canvas.selection = self.selection
        notes = self.scene.get('buildNotes') or []
        self.build_notes_box.set_text((notes[self.stop_index] or '') if self.stop_index < len(notes) else '')
        self.build_notes_for = (self.scene_name, self.stop_index)
        layer = getattr(self.canvas, 'feedback_layer', None)
        if layer:
            layer.scene, layer.build = self.scene_name, self.stop_index
            layer.rebuild()
        self.drawer.build_label.setText(f'build {self.stop_index + 1} of {len(stops)}' if len(stops) > 1 else '')
        self._feature('sync_script_page')
        self.canvas.viewport().update()

    def go_to_stop(self, i):
        self.commit_drawer()
        self.stop_index = i
        self.apply_stop()
        self._feature('rebuild_steps')
        self._feature('refresh_rings')

    def move_stop(self, delta):
        self.go_to_stop(self.stop_index + delta)

    def move_slide(self, delta):
        names = [r['name'] for r in self.navigator_scenes]
        if not names:
            return
        i = names.index(self.scene_name) if self.scene_name in names else 0
        self._watch(spawn(self.select_scene(names[max(0, min(i + delta, len(names) - 1))])).future)

    def as_one_undo_step(self, label, body):
        mark = []
        def begin():
            mark.append((yield self.call('undo_mark'))['mark'])
        def end():
            if mark:
                yield self.call('undo_merge', {'mark': mark[0], 'label': label})
                yield from self.reload_state()
        self.submit(begin)
        body()
        self.submit(end)

    def finish_edits(self):
        self.commit_drawer()
        self.flush_nudges()
        self.canvas.setFocus()
        if self.source_draft is not None and not self.queued_work:
            self.submit(self.commit_source_draft)
        yield from self._queue.drained()
        if self.source_draft is not None:
            raise EngineError('Fix the Python source before saving or closing. The draft remains editable.')
        if self.workspace:
            if self.feedback is not None:
                yield self.feedback.flush()
            yield self.call('settle')
            self.verifying = False
            if self.scene_name:
                yield from self.load_scene(self.scene_name, True, True)

    def run_edit(self, req):
        ctx = self.submit_context or self.edit_context
        args = dict(req)
        name = args.setdefault('scene', ctx.scene)
        if not name:
            return {'ok': False, 'error': 'No slide selected'}
        args.setdefault('stop', ctx.stop if name == ctx.scene else 0)
        if ctx.seen and name == ctx.scene:
            args.setdefault('seen', ctx.seen)
        if os.environ.get('MANIMBLE_SYNC_EDITS'):
            args['optimistic'] = False
        before = self.canvas.stop if name == self.scene_name else None
        oid = args.get('oid')
        self.busy += 1
        try:
            result = yield self.call('edit', args)
            if not result.get('ok'):
                raise EngineError(result.get('error', 'Edit failed'))
            self.last_changed = result.get('changed')
            self.last_build_move = (name, oid, args['stop']) if result.get('oneBuild') else None
            self.toast('No change' if result.get('message') == 'no change' else 'Saved · ' + result.get('message', ''))
        except Exception as exc:
            self.toast(str(exc), error=True)
            result = {'ok': False, 'error': str(exc)}
        finally:
            self.busy -= 1
        if name == self.scene_name:
            yield from self.load_scene(name, True, True)
            if oid is not None:
                now = self.renumbered(oid) if self.renumbered else oid
                if now is not None:
                    self.settle_preview(name, oid, now, result.get('ok', False), before)
        yield from self.reload_state()
        if result.get('pageNumber'):
            self._feature('offer_page_number_spread', name)
        return result

    def commit_move(self, oid, dx, dy):
        if not self.scene_name:
            return
        key = f'{self.scene_name}:{oid}'
        if key in self.pending_moves:
            x, y = self.pending_moves[key]
            self.pending_moves[key] = (x + dx, y + dy)
            return
        self.pending_moves[key] = (dx, dy)
        reference = {'table': self.pending_moves, 'key': key}
        self._pending_identity_refs.append(reference)
        def move():
            try:
                live_key = reference['key']
                delta = self.pending_moves.pop(live_key, None) if live_key is not None else None
                if delta is not None:
                    yield from self.run_edit({'op': 'move', 'oid': oid, 'dx': delta[0], 'dy': delta[1], 'build': True})
            finally:
                self._pending_identity_refs.remove(reference)
        self.submit(move)

    def settle_preview(self, scene, oid, now, ok, before):
        self.canvas.line_overrides.pop(now, None)
        live = f'{scene}:{now}' in self.pending_moves or self.canvas.is_dragging(now)
        a, b = stop_box(before, oid), stop_box(self.canvas.stop, now)
        if ok and live and a and b:
            self.canvas.settle(now, b.cx - a.cx, b.cy - a.cy)
        else:
            self.canvas.overrides.pop(now, None)
            if not ok:
                self.pending_moves.pop(f'{scene}:{oid}', None)
        self.canvas.viewport().update()

    def commit_endpoints(self, oid, e, anchors=None):
        req = dict(anchors or {}, op='endpoints', oid=oid, sx=e[0], sy=e[1], ex=e[2], ey=e[3])
        return self.submit(lambda: self.run_edit(req))

    def commit_resize(self, oid, ratio):
        return self.submit(lambda: self.run_edit({'op': 'resize', 'oid': oid, 'ratio': ratio}))

    def commit_wrap(self, oid, width, side):
        return self.submit(lambda: self.run_edit({'op': 'wrap', 'oid': oid, 'width': width, 'side': side}))

    def nudge_object(self, oid, dx, dy):
        from .canvas import Override
        x, y = self.nudge.get(oid, (0, 0))
        self.nudge[oid] = (x + dx, y + dy)
        override = self.canvas.overrides.setdefault(oid, Override())
        override.dx += dx
        override.dy += dy
        self.canvas.viewport().update()
        self._nudge_timer.start()

    def flush_nudges(self):
        self._nudge_timer.stop()
        all_moves, self.nudge = self.nudge, {}
        def send():
            for oid, (dx, dy) in sorted(all_moves.items()):
                self.commit_move(oid, dx, dy)
        if len(all_moves) > 1:
            self.as_one_undo_step(f'move {len(all_moves)} objects', send)
        else:
            send()

    def preview_sprite(self, oid, kind, value, size, color=None, font=None, weight=None, slant=None, styles=None):
        name = self.scene_name
        epoch = (self._connection_epoch, self._deck_epoch)
        key = (name, oid)
        generation = self._preview_generations.get(key, 0) + 1
        self._preview_generations[key] = generation
        args = dict(kind=kind, value=value, font_size=size)
        for k, v in dict(color=color, font=font, weight=weight, slant=slant, styles=styles).items():
            if v is not None:
                args[k] = v
        sprite = yield self.call('sprite', args)
        if sprite.get('error'):
            raise EngineError(sprite['error'])
        if not self.preview_is_current(key, generation, epoch):
            return False
        from .canvas import Override
        image = self.canvas.texture(sprite['path'])
        override = self.canvas.overrides.setdefault(oid, Override())
        override.replacement = (image, sprite['w'], sprite['h'])
        self.canvas.viewport().update()
        return True

    def preview_is_current(self, key, generation, epoch):
        return key[0] == self.scene_name and self._preview_generations.get(key) == generation and epoch == (self._connection_epoch, self._deck_epoch)

    def commit_text(self, oid, value, shared=True, styles=None, slide=None, seen=None, previewed=None):
        name = slide or self.scene_name
        obj = (self.scene or {}).get('objects', {}).get(str(oid)) if name == self.scene_name else None
        if not name:
            if previewed:
                previewed()
            return
        if obj and value == obj.get('value') and (styles is None or all(runs == char_styles(obj).get(k, []) for k, runs in styles.items())):
            if previewed:
                previewed()
            return
        key = f'{name}:{oid}'
        self.pending_texts[key] = value
        reference = {'table': self.pending_texts, 'key': key}
        self._pending_identity_refs.append(reference)
        def commit():
            if reference['key'] is None:
                self._pending_identity_refs.remove(reference)
                if previewed:
                    previewed()
                return
            try:
                if obj and name == self.scene_name:
                    yield from self.preview_sprite(oid, obj['kind'], value, obj.get('fontSize', 30), obj.get('color'), obj.get('font'), obj.get('weight'), obj.get('slant'), styles)
            except Exception as exc:
                self.toast(str(exc), error=True)
            finally:
                if previewed:
                    previewed()
            req = dict(op='text', oid=oid, value=value, shared=shared, scene=name)
            if styles is not None:
                req['styles'] = styles
            if seen:
                req['seen'] = seen
            yield from self.run_edit(req)
            live_key = reference['key']
            if live_key is not None and self.pending_texts.get(live_key) == value:
                self.pending_texts.pop(live_key, None)
            self._pending_identity_refs.remove(reference)
            self.refresh_inspector_text()
        return self.submit(commit)

    def begin_edit(self, oid, at=None):
        self.selection = oid
        obj = self.obj_info
        if not obj:
            return
        if self._feature('plot_of', oid) is not None:
            self._feature('edit_plot', oid)
        elif obj.get('kind') == 'tex':
            self._feature('open_latex', oid)
        elif obj.get('kind') == 'text':
            if not obj.get('textOk'):
                self.toast(obj.get('textNote', 'This text is generated by code'), error=True)
            else:
                self.begin_inline_text(oid, obj, at)
        elif obj.get('kind') == 'table':
            self.begin_cell_edit(oid, obj, at)

    def begin_inline_text(self, oid, obj, at=None):
        name, seen = self.scene_name, self.edit_context.seen
        current = self.pending_texts.get(f'{name}:{oid}', obj.get('value', ''))
        fmt = obj.get('format') or {}
        def done(value, release):
            if value is None:
                release()
            else:
                self.commit_text(oid, value, self.shared_choice, self.canvas.committed_styles, name, seen, release)
        return self.canvas.begin_text_edit(oid, text=current, family=obj.get('font'), bold=obj.get('weight') == 'BOLD',
            italic=obj.get('slant') == 'ITALIC', styles=char_styles(obj) if current == obj.get('value') else None,
            color=obj.get('color'), at=at, align=fmt.get('align', 'left'), valign=fmt.get('valign', 'top'),
            line_spacing=fmt.get('lineSpacing'), frame=frame_box(obj), changed=lambda value: self._mirror_text(value), done=done)

    def _mirror_text(self, value):
        if self.text_field is not None:
            self.text_field.set_text(value)

    def begin_cell_edit(self, oid, obj, at=None, *, cell=None):
        if oid in self.canvas.overrides or not self.canvas.stop:
            return False
        if cell is None:
            if at is None:
                cells = table_cells(self.canvas.stop, oid)
                cell = cells[0] if cells else None
            else:
                point = self.canvas.frame_point(at)
                cell = table_cell_at(self.canvas.stop, oid, point.x(), point.y())
        if cell is None:
            return False
        rows = self.table_draft() if self.table_grid is not None else self.table_rows(obj.get('value', ''))
        r, c = cell[:2]
        if r >= len(rows) or c >= len(rows[r]):
            return False
        ctx = self.edit_context
        self._table_edit_target = (ctx, oid, r, c)
        current = rows[r][c]
        def mirror(value):
            grid = self.table_grid
            if grid is not None and grid.item(r, c) is not None:
                grid.item(r, c).setText(value)
        def done(value, release):
            if value is None:
                mirror(current)
                release()
                return
            rows[r][c] = value
            def commit():
                try:
                    yield from self.run_edit({'op': 'table', 'oid': oid, 'rows': rows,
                                              'scene': ctx.scene, 'stop': ctx.stop, 'seen': ctx.seen})
                finally:
                    release()
            self.submit(commit)
        return self.canvas.begin_text_edit(oid, text=current, family=obj.get('font'), bold=False, italic=False,
            color=obj.get('color'), at=at, cell=cell, sizing='Cell' if not current else None, changed=mirror, done=done)

    def advance_cell_edit(self):
        target = self._table_edit_target
        backward = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
        if target is None:
            return
        ctx, oid, row, col = target
        def advance():
            yield from self._queue.drained()
            if self.scene_name != ctx.scene:
                return
            if ctx.seen != self.edit_context.seen:
                mapped = yield self.call('resolve_oids', {'scene': ctx.scene, 'from': ctx.seen,
                                                          'to': self.edit_context.seen, 'oids': [oid]})
                current_oid = mapped.get('map', {}).get(str(oid), oid)
            else:
                current_oid = oid
            if current_oid is None:
                return
            current_oid = int(current_oid)
            cells = table_cells(self.canvas.stop or {}, current_oid)
            index = next((i for i, cell in enumerate(cells) if cell[:2] == (row, col)), None)
            if index is not None and cells:
                cell = cells[(index + (-1 if backward else 1)) % len(cells)]
                obj = (self.scene or {}).get('objects', {}).get(str(current_oid))
                if obj:
                    self.begin_cell_edit(current_oid, obj, cell=cell)
        self._watch(spawn(advance()).future)

    def begin_placeholder(self, placeholder):
        name = self.scene_name
        if not name:
            return
        self.selection = None
        def done(value, release):
            text = '\n'.join(line for line in (value or '').splitlines() if line.strip(' •\t'))
            if not text:
                release()
                return
            def commit():
                try:
                    result = yield self.call('placeholder', {'scene': name, 'line': placeholder['line'], 'text': text})
                    if not result.get('ok'):
                        raise EngineError(result.get('error', 'Could not add text'))
                    yield from self.load_scene(name, True, True)
                    yield from self.reload_state()
                finally:
                    release()
            self.submit(commit)
        self.canvas.begin_text_edit(-1, text='• ' if placeholder.get('role') == 'body' else '',
            family=(self.scene or {}).get('font'), bold=False, italic=False,
            color=(self.scene or {}).get('textColor', '#FFFFFF'), at=None, placeholder=placeholder, changed=lambda _: None, done=done)

    def remove_placeholder(self, line):
        name = self.scene_name
        def remove():
            result = yield self.call('placeholder', {'scene': name, 'line': line, 'op': 'remove'})
            if not result.get('ok'):
                raise EngineError(result.get('error', 'Could not remove box'))
            yield from self.load_scene(name, True, True)
            yield from self.reload_state()
        self.submit(remove)

    def refresh_inspector_text(self):
        if self.text_field is not None and not self.text_field.hasFocus() and self.canvas.editing_text is None and self.obj_info:
            if f'{self.scene_name}:{self.selection}' not in self.pending_texts:
                self.text_field.set_text(self.obj_info.get('value', ''))

    def undo(self):
        return self.do_undo(False)

    def redo(self):
        return self.do_undo(True)

    def do_undo(self, redo):
        focus = QApplication.focusWidget()
        if isinstance(focus, (QTextEdit, QPlainTextEdit, QLineEdit)):
            available = focus.document().isRedoAvailable() if redo and hasattr(focus, 'document') else focus.document().isUndoAvailable() if hasattr(focus, 'document') else True
            if focus is not self.source_view or available or self.source_draft is not None:
                focus.redo() if redo else focus.undo()
                return
        if self.source_draft is not None:
            self.toast('Fix or undo the Python draft first', error=True)
            return
        def change():
            yield self.call('settle')
            result = yield self.call('redo' if redo else 'undo')
            if result.get('ok'):
                self.last_changed = None
                if self.scene_name:
                    yield from self.load_scene(self.scene_name, True)
                yield from self.reload_state()
                self.toast(('Redid ' if redo else 'Undid ') + result.get('label', ''))
            else:
                self.toast(result.get('error', 'Nothing to undo'), error=True)
        return self.submit(change)

    def select_all(self, *_):
        focus = QApplication.focusWidget()
        if isinstance(focus, (QTextEdit, QPlainTextEdit, QLineEdit)):
            focus.selectAll()
        else:
            self.canvas.select_all_objects()
            self.canvas.setFocus()

    def spread_last_move(self):
        def spread():
            if self.last_build_move and self.last_build_move[0] == self.scene_name:
                yield from self.run_edit({'op': 'spread', 'oid': self.last_build_move[1]})
        self.submit(spread)

    def save_asset(self, source, prefix=''):
        deck = self.work_deck
        if deck is None:
            return failed(EngineError('Open a deck before importing an image'))
        def save():
            import hashlib
            import tempfile
            from PIL import Image, ImageOps
            import pillow_heif
            pillow_heif.register_heif_opener()
            src = Path(source)
            data = src.read_bytes()
            digest = hashlib.sha256(data).hexdigest()[:16]
            suffix = '.png' if src.suffix.lower() in ('.heic', '.heif') else src.suffix.lower()
            target = deck / 'assets' / f'{prefix}{digest}{suffix}'
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                fd, temporary = tempfile.mkstemp(dir=target.parent, suffix=suffix)
                os.close(fd)
                try:
                    if suffix == '.png' and src.suffix.lower() in ('.heic', '.heif'):
                        with Image.open(src) as image:
                            ImageOps.exif_transpose(image).save(temporary, format='PNG')
                    else:
                        Path(temporary).write_bytes(data)
                    os.replace(temporary, target)
                finally:
                    Path(temporary).unlink(missing_ok=True)
            return target.relative_to(deck).as_posix()
        return run_blocking(save)

    def show_inspector_page(self, widget):
        if widget is None:
            self.inspector_host.setCurrentWidget(self.inspector_scroll)
        else:
            if self.inspector_host.indexOf(widget) < 0:
                self.inspector_host.addWidget(widget)
            self.inspector_host.setCurrentWidget(widget)

    def toggle_navigator(self):
        widget = self.main_split.widget(0)
        widget.setVisible(not widget.isVisible())

    def toggle_inspector(self):
        widget = self.main_split.widget(2)
        widget.setVisible(not widget.isVisible())

    def cursor_moved(self, point):
        self.h_ruler.cursor = self.v_ruler.cursor = point

    def canvas_menu(self):
        if not self.scene_name or self.player:
            return None
        if not self.obj_info:
            return self.actions.context_menu(['format_background'])
        items = []
        if self.obj_info.get('kind') in ('text', 'tex'):
            action = self.actions.action('hyperlink_pressed')
            link = (self.obj_info.get('format') or {}).get('link')
            action.setText('Edit Hyperlink…' if link else 'Add Hyperlink…')
            action.setToolTip(link or '')
            items.append('hyperlink_pressed')
        self._canvas_context_commands = self._feature('bio_cartoon_menu_items') or []
        items.extend(f'canvas_bio_{i}' for i in range(len(self._canvas_context_commands)))
        items.extend(['bring_to_front', 'bring_forward', 'send_backward', 'send_to_back',
                      'group_objects', 'ungroup_objects'])
        return self.actions.context_menu(items)

    def prev_slide(self):
        self.move_slide(-1)

    def next_slide(self):
        self.move_slide(1)

    def prev_stop(self):
        self.move_stop(-1)

    def next_stop(self):
        self.move_stop(1)

    def toggle_objects(self):
        self.canvas.show_objects = not self.canvas.show_objects

    @property
    def show_tips(self):
        return prefs.get_bool('showTips')

    def toggle_tips(self):
        prefs.set('showTips', not self.show_tips)
        if self.show_tips:
            self.toast('Tips on: short confirmations and hints appear here after each action')
        else:
            self.toast_pill.hide()

    def toggle_terminal_in_work_copy(self):
        self.terminal.set_start_in_work_copy(not prefs.get_bool('terminalInWorkCopy', True))

    def toast(self, text, error=False):
        if not error and not self.show_tips and not str(text).lower().startswith('cancelled'):
            return
        self.toast_pill.show(str(text), tint='#dd5050' if error else None)
        self.window.statusBar().showMessage(str(text), 12000 if error else 4500)
        self.schedule_toast_hide(8 if error else 2.6)

    def schedule_toast_hide(self, delay, *, generation=None):
        if generation is None:
            self._toast_generation = getattr(self, '_toast_generation', 0) + 1
            generation = self._toast_generation
        def hide():
            if generation != self._toast_generation:
                return
            if self.toast_pill.hovered:
                self.schedule_toast_hide(1.5, generation=generation)
            else:
                self.toast_pill.hide()
        QTimer.singleShot(round(delay * 1000), hide)

    def alert(self, title, text):
        box = QMessageBox(QMessageBox.Icon.Warning, title, text, QMessageBox.StandardButton.Ok, self.window)
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.open()
        self._alert = box

    def update_busy_pill(self):
        if self.busy or self.verifying:
            remaining = max(0, self.render_eta[1] - (time.monotonic() - self.render_eta[0])) if self.render_eta else None
            self.busy_pill.show(self.busy_text, spinning=True, progress=self.busy_progress,
                                remaining=remaining, cancellable=self.busy > 0)
        else:
            self.busy_pill.hide()
            self.busy_progress = self.render_eta = None

    def cancel_render(self):
        self.cancel_plot()
        self.busy_text = 'Cancelling…'
        self._watch(self.call('cancel'))

    def stop_feature_workers(self):
        for method in ('cancel_plot', 'cancel_extraction', 'cancel_script_movie', 'stop_script_preview',
                       'close_animation_preview'):
            self._feature(method)


    def request_close(self):
        if self._closing:
            return
        self._closing = True
        def close():
            try:
                approved = self.confirm_close('closing this window')
                if inspect.isgenerator(approved):
                    approved = yield from approved
                elif isinstance(approved, Future):
                    approved = yield approved
                if not approved:
                    return False
                self.stop_feature_workers()
                yield from self.finish()
                self._connection_epoch += 1
                self.window._approved_close = True
                self.window.close()
                if hasattr(self.app, 'window_closed'):
                    self.app.window_closed(self)
                return True
            except Exception as exc:
                self.toast(str(exc), error=True)
                return False
            finally:
                self._closing = False
        return spawn(close()).future
