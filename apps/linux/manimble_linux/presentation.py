"""Native still/animated shows, presenter view and animation preview.

``SlidePlayer`` and ``AnimatedPlayer`` implement the snake-case PresenterSource and
ScriptShow protocols. ScriptShow uses ``window``, ``on_exit``, ``page_count``,
``page_index``, ``script_page`` (scene/build tuple), ``script_page_ready``,
``script_slide_rect`` (window-local logical pixels), ``next`` and ``exit``.
The window's ``show_view`` is the overlay parent for a script pointer. Animated
shows have exactly one QMediaPlayer/QVideoSink; all live views paint its shared
QImage. Thumbnail extraction is background, independent of live playback.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime
import json
from pathlib import Path
import time
from typing import Protocol

from PySide6.QtCore import (QEasingCurve, QEvent, QObject, QPointF, QRectF, QSignalBlocker, QSize,
                            Qt, QTimer, QUrl, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QImage, QImageReader, QPainter,
                           QPainterPath, QPalette, QPen)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox,
                               QMainWindow, QPushButton, QScrollArea, QSizePolicy, QSlider,
                               QTextEdit, QToolButton, QVBoxLayout, QWidget)

from . import prefs
from .icons import template_icon
from .style import Theme
from .tasks import spawn
from .workers import run_blocking


def _at(items, index, default=None):
    return items[index] if 0 <= index < len(items) else default


def _mapped(image: QImage) -> QImage:
    from .darkmode import DarkMode
    return DarkMode.apply(image)


def _picture(path: str, width: int = 0) -> QImage:
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    if width and size.width() > width:
        reader.setScaledSize(size.scaled(width, width, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        raise OSError(f"Could not open image {path}: {reader.errorString()}")
    return image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)


def _thumbnail(path: str, video: bool = False) -> QImage:
    if not video:
        return _picture(path, 480)
    import av
    with av.open(path) as container:
        stream = container.streams.video[0]
        if stream.duration:
            container.seek(max(0, stream.duration - int(2 / stream.time_base)), stream=stream)
        last = None
        for frame in container.decode(stream):
            last = frame
        if last is None:
            raise OSError(f"No video frames in {path}")
        image = last.to_image()
        image.thumbnail((480, 270))
        data = image.convert("RGBA").tobytes()
        return QImage(data, image.width, image.height, image.width * 4,
                      QImage.Format.Format_RGBA8888).copy()


@dataclass(frozen=True)
class LinkRegion:
    x0: float
    x1: float
    y0: float
    y1: float
    url: str

    @classmethod
    def from_record(cls, record):
        return record if isinstance(record, cls) else cls(*record)

    @staticmethod
    def url_at(p: QPointF, regions) -> QUrl | None:
        for item in reversed(regions):
            r = LinkRegion.from_record(item)
            if r.x0 <= p.x() <= r.x1 and r.y0 <= p.y() <= r.y1:
                url = QUrl(r.url)
                return url if url.isValid() else None
        return None


@dataclass
class PresentSlide:
    scene: str
    scene_index: int = 0
    number: int = 0
    index: int = 0
    count: int = 1
    kind: str = "video"
    file: str | None = None
    rev_file: str | None = None
    loop: bool = False
    auto_next: bool = False
    rate: float = 1.0
    rev_rate: float = 1.0
    notes: str = ""
    build_notes: str = ""
    pending: bool = False

    @classmethod
    def from_record(cls, r):
        if isinstance(r, cls):
            return r
        return cls(scene=r["scene"], scene_index=r.get("sceneIndex", 0), number=r.get("number", 0),
                   index=r.get("index", 0), count=r.get("count", 1), kind=r.get("kind", "video"),
                   file=r.get("file"), rev_file=r.get("revFile"), loop=bool(r.get("loop")),
                   auto_next=bool(r.get("autoNext")), rate=float(r.get("rate") or 1),
                   rev_rate=float(r.get("revRate") or 1), notes=r.get("notes") or "",
                   build_notes=r.get("buildNotes") or "", pending=bool(r.get("pending")))


@dataclass
class PresentDeck:
    slides: list[PresentSlide] = field(default_factory=list)
    sections: list[int] = field(default_factory=list)
    links: dict = field(default_factory=dict)
    LinkRegion = LinkRegion

    @classmethod
    def from_record(cls, r):
        if isinstance(r, cls):
            return r
        return cls([PresentSlide.from_record(s) for s in r.get("slides", [])],
                   list(r.get("sections", [])), dict(r.get("links", {})))


@dataclass
class PresenterPage:
    slide: int
    build: int
    builds: int
    scene: str | None
    notes: str = ""
    build_notes: str = ""
    still: str | None = None
    clip: str | None = None


class PresenterSource(Protocol):
    window: "PlayerWindow"
    on_change: object
    on_exit: object
    page_count: int
    page_index: int
    slide_count: int
    reload_token: int
    is_blacked_out: bool
    def presenter_page(self, i: int) -> PresenterPage: ...
    def next(self): ...
    def previous(self): ...
    def go(self, i: int): ...
    def link(self, p: QPointF) -> QUrl | None: ...
    def exit(self): ...
    def handle_key(self, e: QKeyEvent) -> bool: ...
    def set_notes(self, text: str, for_page: int, build: bool): ...


class ShowView(QWidget):
    """Letterboxed slide surface; feedback/editor decorations never enter here."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.image = QImage()
        self.previous_image = QImage()
        self.fade = 1.0
        self.content_opacity = 1.0
        self.placeholder = ""
        self.counter = ""
        self.counter_opacity = 0.0
        self.fraction = 0.0
        self.show_progress = False
        self.on_key = self.on_click = self.over_link = None
        self.hit_rect = None
        self.setMinimumSize(80, 45)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    @property
    def slide_rect(self) -> QRectF:
        return self.image_rect(self.image)

    def image_rect(self, image):
        aspect = image.width() / image.height() if not image.isNull() else 16 / 9
        w = min(self.width(), self.height() * aspect)
        h = w / aspect
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def point(self, pos) -> QPointF | None:
        r = self.hit_rect() if self.hit_rect else self.slide_rect
        if not r.contains(pos) or not r.width() or not r.height():
            return None
        return QPointF((pos.x() - r.x()) / r.width(), (pos.y() - r.y()) / r.height())

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.GlobalColor.black)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.paint_content(p)
        if self.show_progress:
            p.fillRect(QRectF(0, self.height() - 3, self.width() * self.fraction, 3), Theme.accent)
            if self.counter_opacity > 0:
                p.setOpacity(self.counter_opacity)
                p.setPen(QColor.fromRgbF(1, 1, 1, .8))
                p.setFont(Theme.tabular(13))
                p.drawText(QRectF(0, self.height() - 32, self.width() - 16, 18),
                           Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self.counter)

    def paint_content(self, painter):
        if self.content_opacity <= 0:
            return
        painter.setOpacity(self.content_opacity)
        previous = not self.previous_image.isNull() and self.fade < 1
        if previous:
            rect = self.image_rect(self.previous_image)
            overlap = not self.image.isNull() and rect == self.slide_rect
            painter.setOpacity(self.content_opacity * (1 if overlap else 1 - self.fade))
            painter.drawImage(rect, self.previous_image)
        if not self.image.isNull():
            painter.setOpacity(self.content_opacity * self.fade)
            painter.drawImage(self.slide_rect, self.image)
        painter.setOpacity(self.content_opacity)
        if self.placeholder:
            painter.setPen(QColor.fromRgbF(1, 1, 1, .55))
            painter.setFont(Theme.font(26))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.placeholder)
        painter.setOpacity(1)

    def mousePressEvent(self, e):
        if self.on_click and e.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            point = self.point(e.position())
            primary = e.button() == Qt.MouseButton.LeftButton
            self.on_click(point, primary)

    def mouseMoveEvent(self, e):
        hit = self.over_link and self.over_link(self.point(e.position()))
        self.setCursor(Qt.CursorShape.PointingHandCursor if hit else Qt.CursorShape.ArrowCursor)

    def keyPressEvent(self, e):
        if not self.on_key or not self.on_key(e):
            super().keyPressEvent(e)


class PlayerWindow(QWidget):
    def __init__(self, screen=None):
        super().__init__()
        self.setWindowTitle("Manimble — Slide Show")
        self.show_view = ShowView(self)
        self.show_view.setCursor(Qt.CursorShape.BlankCursor)
        self.on_key = self.on_click = self.on_mouse = self.over_link = self.on_close = None
        self._dismissing = False
        self.show_view.on_key = lambda e: self.on_key(e) if self.on_key else False
        self.show_view.on_click = self._click
        self.show_view.over_link = lambda p: self.over_link(p) if self.over_link and p else False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.resize(960, 540)
        self.windowed_presentation = False
        self._screen = screen
        QApplication.instance().screenRemoved.connect(self._screen_removed)

    def _click(self, p, primary):
        if self.on_mouse:
            self.on_mouse(p, primary)
        elif self.on_click:
            self.on_click(primary)

    def resizeEvent(self, e):
        self.show_view.setGeometry(self.rect())
        super().resizeEvent(e)

    def present(self, screen=None, own_space=False):
        self._screen = screen or self._screen or QApplication.primaryScreen()
        self.setScreen(self._screen)
        if self.windowed_presentation:
            r = self._screen.availableGeometry()
            self.resize(max(640, int(r.width() * .6)), max(360, int(r.height() * .6)))
            self.move(r.center() - self.rect().center())
            self.showNormal()
        else:
            self.setGeometry(self._screen.geometry())
            self.showFullScreen()
        self.show_view.setFocus()

    def move_to(self, screen, done=None):
        self.showNormal()
        self.present(screen)
        if done:
            QTimer.singleShot(0, done)

    def when_settled(self, work):
        QTimer.singleShot(0, work)

    def _screen_removed(self, screen):
        if screen is self._screen and self.isVisible():
            QTimer.singleShot(0, lambda: self.move_to(QApplication.primaryScreen())
                              if screen is self._screen and self.isVisible() else None)

    def dismiss(self):
        self._dismissing = True
        self.hide()

    def closeEvent(self, e):
        if not self._dismissing and self.on_close:
            self.on_close()
        e.accept()

    def keyPressEvent(self, e):
        if not self.on_key or not self.on_key(e):
            super().keyPressEvent(e)


def _animate(animation, end, duration, start):
    animation.stop()
    animation.setStartValue(start)
    animation.setEndValue(end)
    animation.setDuration(duration)
    animation.start()


class _Show(QObject):
    frame_changed = Signal(QImage)

    def __init__(self, screen=None):
        super().__init__()
        self.window = PlayerWindow(screen)
        self.window.on_key = self.handle_key
        self.window.on_mouse = self.click
        self.window.over_link = lambda p: self.link(p) is not None
        self.window.on_close = self.exit
        self.on_change = self.on_exit = self.on_error = None
        self.index = 0
        self.reload_token = 0
        self.blacked_out = False
        self.show_progress = True
        self.frame = QImage()
        self._generation = 0
        self._exited = False
        self._cache = OrderedDict()
        self._fade = QVariantAnimation(self)
        self._fade.valueChanged.connect(self._fade_value)
        self._fade.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._fade.finished.connect(self._clear_previous_frame)
        self._blank_fade = QVariantAnimation(self)
        self._blank_fade.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._blank_fade.valueChanged.connect(self._blank_value)
        self._progress = QVariantAnimation(self)
        self._progress.valueChanged.connect(self._progress_value)
        self._counter_fade = QVariantAnimation(self)
        self._counter_fade.valueChanged.connect(self._counter_value)
        self._counter_timer = QTimer(self)
        self._counter_timer.setSingleShot(True)
        self._counter_timer.timeout.connect(lambda: _animate(self._counter_fade, 0., 600, 1.))

    @property
    def page_index(self):
        return self.index

    @property
    def is_blacked_out(self):
        return self.blacked_out

    @property
    def slide_rect_on_screen(self):
        return self.window.show_view.slide_rect

    @property
    def script_slide_rect(self):
        return self.slide_rect_on_screen

    @property
    def script_page(self):
        if not 0 <= self.index < self.page_count:
            return None
        p = self.presenter_page(self.index)
        return (p.scene, p.build) if p.scene else None

    @property
    def live_player(self):
        return None

    @property
    def playback_state(self):
        return None

    def _error(self, text):
        if self.on_error:
            self.on_error(text)

    def _changed(self, reveal=False):
        v = self.window.show_view
        v.show_progress = self.show_progress
        if self.show_progress and self.page_count:
            p = self.presenter_page(self.index)
            v.counter = (f"{p.slide + 1} / {self.slide_count}" +
                         (f"  ·  {p.build + 1}/{p.builds}" if p.builds > 1 else ""))
            fraction = (self.index + 1) / self.page_count
            if self._progress.endValue() != fraction:
                _animate(self._progress, fraction, 250, v.fraction)
            if reveal:
                self._counter_fade.stop()
                v.counter_opacity = 1.
                self._counter_timer.start(1800)
        else:
            self._progress.stop()
            self._counter_timer.stop()
            self._counter_fade.stop()
            self._progress.setEndValue(0.)
            v.fraction = v.counter_opacity = 0.
        v.update()
        if self.on_change:
            self.on_change()

    def _blank_value(self, value):
        self.window.show_view.content_opacity = value
        self.window.show_view.update()

    def _progress_value(self, value):
        self.window.show_view.fraction = value
        self.window.show_view.update()

    def _counter_value(self, value):
        self.window.show_view.counter_opacity = value
        self.window.show_view.update()

    def _clear_previous_frame(self):
        self.window.show_view.previous_image = QImage()

    def _fade_value(self, value):
        self.window.show_view.fade = float(value)
        self.window.show_view.update()

    def _set_frame(self, image, fade_ms=0):
        v = self.window.show_view
        self._fade.stop()
        v.previous_image = self.frame if fade_ms else QImage()
        self.frame = image
        v.image = image
        v.placeholder = ""
        v.fade = 0.0 if fade_ms else 1.0
        if fade_ms:
            _animate(self._fade, 1., fade_ms, 0.)
        v.update()
        self.frame_changed.emit(image)

    def _load_still(self, path, fade_ms=0, *, pending="", preview=False):
        self._loaded_still_path = None
        generation = self._generation
        if not path:
            self._set_frame(QImage())
            self.window.show_view.placeholder = pending
            return
        if path in self._cache:
            self._set_frame(self._cache[path], fade_ms)
            self._loaded_still_path = path
            return
        def load():
            try:
                image = yield run_blocking(_picture, path)
                if generation != self._generation or self._exited:
                    return
                image = _mapped(image)
                self._cache[path] = image
                while len(self._cache) > 12:
                    self._cache.popitem(last=False)
                self._set_frame(image, fade_ms)
                self._loaded_still_path = path
                self._changed()
            except Exception as exc:
                if generation == self._generation:
                    self._set_frame(QImage())
                    detail = f"Could not open animation image: {Path(path).name}\n{exc}" if preview else str(exc)
                    self._error(detail)
                    self._changed()
        spawn(load())

    def click(self, point, primary=True):
        if not primary:
            self.previous(); return
        url = self.link(point) if point else None
        if url:
            QDesktopServices.openUrl(url); return
        self.next()

    def handle_key(self, e):
        if e.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier):
            return False
        k, t = e.key(), e.text().lower()
        if k in (Qt.Key.Key_Right, Qt.Key.Key_Down, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_PageDown):
            self.next()
        elif k in (Qt.Key.Key_Left, Qt.Key.Key_Up, Qt.Key.Key_Backspace, Qt.Key.Key_Delete, Qt.Key.Key_PageUp):
            self.previous()
        elif k == Qt.Key.Key_Home:
            self.go(0)
        elif k == Qt.Key.Key_End:
            self.go_end()
        elif k == Qt.Key.Key_Space:
            self.play_pause()
        elif k == Qt.Key.Key_F11:
            if self.window.isFullScreen():
                self.window.showNormal()
            else:
                self.window.showFullScreen()
        elif k == Qt.Key.Key_Escape or t == "q":
            self.exit()
        elif t in ("b", "."):
            self.blacked_out = not self.blacked_out
            view = self.window.show_view
            _animate(self._blank_fade, 0. if self.blacked_out else 1.,
                     250 if isinstance(self, AnimatedPlayer) else 200, view.content_opacity)
            self._changed(reveal=True)
        elif t == "p":
            self.show_progress = not self.show_progress
            self._changed(reveal=True)
        elif t == "n":
            current = self.presenter_page(self.index).slide
            target = next((i for i in range(self.index + 1, self.page_count) if self.presenter_page(i).slide > current), None)
            if target is not None:
                self.go(target)
        elif isinstance(self, AnimatedPlayer) and t and t in "123456789":
            self.jump(int(t) - 1)
        elif isinstance(self, AnimatedPlayer) and t == "r":
            self.replay()
        elif isinstance(self, AnimatedPlayer) and t == "v":
            self.reverse()
        else:
            return False
        e.accept()
        return True

    def go_end(self):
        self.go(self.page_count - 1)

    def set_script_paused(self, paused):
        """Stills have no media clock; the script scheduler owns their timing."""
        self._script_paused = bool(paused)

    def play_pause(self):
        self.next()

    def exit(self):
        if self._exited:
            return
        self._exited = True
        self._generation += 1
        self._counter_timer.stop()
        for animation in (self._fade, self._blank_fade, self._progress, self._counter_fade):
            animation.stop()
        self.window.dismiss()
        if self.on_exit:
            self.on_exit()


@dataclass
class _Page:
    slide: int
    build: int
    builds: int
    path: str
    links: list = field(default_factory=list)


class SlidePlayer(_Show):
    def __init__(self, screen=None):
        super().__init__(screen)
        self.pages = []
        self.scenes = []; self.notes = []; self.build_notes = []
        self.scene_indices = []; self.extra_links = {}

    @property
    def page_count(self):
        return len(self.pages)

    @property
    def slide_count(self):
        return len({p.slide for p in self.pages})

    @property
    def script_page_ready(self):
        return bool(self.pages and self.pages[self.index].path and not self.frame.isNull()
                    and getattr(self, "_loaded_still_path", None) == self.pages[self.index].path)

    def _pages(self, slides, links):
        return [_Page(si, bi, len(builds), path, _at(_at(links, si, []), bi, []))
                for si, builds in enumerate(slides) for bi, path in enumerate(builds)]

    def play(self, slides, links=(), start_slide=0, screen=None, visible=True, own_space=False):
        self.pages = self._pages(slides, links)
        if not self.pages:
            return
        self.index = next((i for i, p in enumerate(self.pages) if p.slide >= start_slide), 0)
        if visible:
            self.window.present(screen, own_space)
        self.show()

    def update(self, slides, links, scenes, notes, build_notes):
        old = self.presenter_page(self.index) if self.pages else None
        fresh = self._pages(slides, links)
        if not fresh:
            return
        self.pages = fresh
        self.scenes, self.notes, self.build_notes = scenes, notes, build_notes
        self.reload_token += 1
        si = scenes.index(old.scene) if old and old.scene in scenes else min(old.slide if old else 0, len(slides) - 1)
        self.index = next((i for i, p in enumerate(fresh) if p.slide == si and old and p.build == old.build),
                          next((i for i, p in enumerate(fresh) if p.slide >= si), len(fresh) - 1))
        new = self.presenter_page(self.index)
        if old and old.scene == new.scene and old.build == new.build and old.still == new.still:
            self._changed()
        else:
            self.show(200 if old and not old.still else 0)

    def show(self, fade_ms=0):
        self._generation += 1
        page = self.pages[self.index]
        self._load_still(page.path, fade_ms, pending=f"Rendering slide {page.slide + 1}…")
        self._changed(reveal=True)

    def go(self, i):
        if 0 <= i < len(self.pages) and i != self.index:
            slide_change = self.pages[i].slide != self.pages[self.index].slide
            self.index = i
            self.show(350 if slide_change else 200)

    def next(self):
        self.exit() if self.index + 1 >= len(self.pages) else self.go(self.index + 1)

    def previous(self):
        self.go(self.index - 1)

    def link(self, p):
        if not p or not self.pages or self.blacked_out:
            return None
        page = self.pages[self.index]
        original = _at(self.scene_indices, page.slide, page.slide)
        regions = self.extra_links.get(str(original), []) + list(page.links)
        return LinkRegion.url_at(p, regions)

    def presenter_page(self, i):
        p = self.pages[i]
        return PresenterPage(p.slide, p.build, p.builds, _at(self.scenes, p.slide),
                             _at(self.notes, p.slide, ""), _at(_at(self.build_notes, p.slide, []), p.build, ""), p.path)

    def set_notes(self, text, for_page, build):
        p = self.pages[for_page]
        target = self.build_notes if build else self.notes
        while len(target) <= p.slide:
            target.append([] if build else "")
        if build:
            while len(target[p.slide]) <= p.build:
                target[p.slide].append("")
            target[p.slide][p.build] = text
        else:
            target[p.slide] = text


class AnimatedPlayer(_Show):
    def __init__(self, screen=None, *, preview=False):
        super().__init__(screen)
        self.deck = PresentDeck()
        self.preview = preview
        self.failed = False
        self.window.show_view.hit_rect = self._hit_rect
        self.media_player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.media_player.setAudioOutput(self.audio_output)
        self.video_sink = None
        self.finished = False
        self.mode = "play"
        self._first_frame = False
        self._end_seek = False
        self.media_player.mediaStatusChanged.connect(self._status)
        self.media_player.errorOccurred.connect(self._media_error)
        self.media_player.playbackStateChanged.connect(lambda _: self._changed())

    @property
    def sections(self):
        return self.deck.sections

    @property
    def page_count(self):
        return len(self.deck.slides)

    @property
    def slide_count(self):
        return len({s.number for s in self.deck.slides})

    @property
    def current_slide(self):
        return _at(self.deck.slides, self.index)

    @property
    def is_settled(self):
        s = self.current_slide
        if s and s.kind == "image":
            return self.finished and getattr(self, "_loaded_still_path", None) == s.file
        return self.finished or bool(s and s.loop and self._first_frame)

    @property
    def script_page_ready(self):
        return self.is_settled and not bool(self.current_slide and self.current_slide.pending)

    @property
    def live_player(self):
        return self.media_player if self.current_slide and self.current_slide.kind == "video" else None

    @property
    def playback_state(self):
        if not self.live_player:
            return None
        if self.finished:
            return "Build done"
        return "Playing" if self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState else "Paused"

    def _hit_rect(self):
        view = self.window.show_view
        return view.slide_rect if self.live_player and self._first_frame else QRectF(view.rect())

    def click(self, point, primary=True):
        if primary and point is not None and self.sections and point.y() >= .94 and not self.link(point):
            self.jump(min(len(self.sections) - 1, int(point.x() * len(self.sections))))
        else:
            super().click(point, primary)

    def play(self, deck, start_scene=0, screen=None, visible=True, own_space=False):
        self.deck = PresentDeck.from_record(deck)
        if not self.page_count:
            return
        if visible:
            self.window.present(screen, own_space)
        self.show(next((i for i, s in enumerate(self.deck.slides) if s.scene_index >= start_scene), 0), "play")

    def update(self, deck):
        old = self.current_slide
        fresh = PresentDeck.from_record(deck)
        if not fresh.slides:
            return
        self.deck = fresh
        self.reload_token += 1
        same_scene = next((i for i, s in enumerate(fresh.slides) if old and s.scene == old.scene), None)
        i = next((i for i, s in enumerate(fresh.slides) if old and s.scene == old.scene and s.index == old.index),
                 same_scene if same_scene is not None else
                 next((i for i, s in enumerate(fresh.slides) if old and s.number >= old.number), self.page_count - 1))
        new = self.deck.slides[i]
        self.index = i
        if old and (old.scene != new.scene or old.index != new.index or
                    (old.pending and (not new.pending or old.file != new.file))):
            self.show(i, "end")
        else:
            self._changed()

    def show(self, i, mode="play"):
        if not 0 <= i < self.page_count:
            return
        self._generation += 1
        generation = self._generation
        self.index, self.mode, self.finished = i, mode, False
        self._first_frame = False
        self.failed = False
        self._end_seek = mode == "end"
        self.media_player.stop()
        self.media_player.setSource(QUrl())
        s = self.current_slide
        pending = f"Building slide {s.number + 1}…" if s.pending and not s.file else ""
        if self.video_sink is not None:
            self.media_player.setVideoSink(None)
            self.video_sink.deleteLater()
            self.video_sink = None
        if s.kind == "image":
            self._load_still(s.file, pending=pending, preview=self.preview)
            self.finished = True
            self._changed(reveal=True)
            return
        self.window.show_view.placeholder = pending
        path = (s.rev_file or s.file) if mode == "reverse" else s.file
        if not path:
            self.finished = not s.pending
            self._changed(reveal=True)
            return
        if self.preview:
            self._set_frame(QImage())
        sink = QVideoSink(self)
        self.video_sink = sink
        sink.videoFrameChanged.connect(lambda f, g=generation: self._video_frame(f, g))
        self.media_player.setVideoSink(sink)
        rate = s.rev_rate if mode == "reverse" else s.rate
        self.media_player.setPlaybackRate(rate if rate > 0 else 1)
        self.media_player.setSource(QUrl.fromLocalFile(path))
        self.media_player.play()
        self._changed(reveal=True)

    def _video_frame(self, frame, generation):
        if generation != self._generation or self._exited or not frame.isValid():
            return
        image = frame.toImage()
        if image.isNull():
            return
        if self._end_seek:
            duration = self.media_player.duration()
            if not duration or self.media_player.position() < duration - 90:
                return
            self._end_seek = False
            self.finished = True
            self.media_player.pause()
        self._first_frame = True
        self._set_frame(_mapped(image))
        if self.finished:
            self._changed()

    def _media_error(self, _error, text):
        if self._exited or not self.live_player or self.media_player.source().isEmpty():
            return
        self.failed = self.finished = True
        self._end_seek = False
        self._error(text or self.media_player.errorString() or "Could not play this animation clip")
        self._changed()

    def _status(self, status):
        if self._exited or not self.current_slide or self.current_slide.kind != "video":
            return
        if status == QMediaPlayer.MediaStatus.LoadedMedia and self._end_seek:
            self.media_player.setPosition(max(0, self.media_player.duration() - 45))
        elif status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self._end_seek:
                self._end_seek = False
            s = self.current_slide
            if self.mode == "play" and s.loop:
                self.media_player.setPosition(0); self.media_player.play()
            elif self.mode == "play" and s.auto_next and self.index + 1 < self.page_count:
                self.show(self.index + 1)
            else:
                self.finished = True; self._changed()

    def next(self):
        if self.index + 1 < self.page_count:
            self.show(self.index + 1)
        elif self.finished:
            self.exit()

    def previous(self):
        self.show(self.index - 1, "end")

    def go(self, i):
        self.show(i)

    def go_end(self):
        self.show(self.page_count - 1, "end")

    def replay(self):
        self.show(self.index)

    def reverse(self):
        self.show(self.index, "reverse")

    def seek(self, milliseconds):
        self.finished = False
        self.media_player.setPosition(max(0, min(int(milliseconds), self.media_player.duration())))

    def set_script_paused(self, paused):
        if paused:
            self._script_was_playing = self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
            self.media_player.pause()
        elif getattr(self, "_script_was_playing", False):
            self._script_was_playing = False
            self.media_player.play()

    def play_pause(self):
        if self.finished:
            self.next(); return
        if self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.media_player.pause()
        else:
            self.media_player.play()
        self._changed()

    def jump(self, section):
        if not 0 <= section < len(self.sections):
            return
        i = next((i for i, s in enumerate(self.deck.slides) if s.scene_index == self.sections[section]), None)
        if i is not None:
            self.show(i)

    def link(self, p):
        s = self.current_slide
        return LinkRegion.url_at(p, self.deck.links.get(str(s.scene_index), [])) if s and p and not self.blacked_out else None

    def presenter_page(self, i):
        s = self.deck.slides[i]
        return PresenterPage(s.number, s.index, s.count, s.scene, s.notes, s.build_notes,
                             s.file if s.kind == "image" else None, s.file if s.kind == "video" else None)

    def set_notes(self, text, for_page, build):
        s = self.deck.slides[for_page]
        if build:
            s.build_notes = text
        else:
            for other in self.deck.slides:
                if other.scene == s.scene:
                    other.notes = text

    @property
    def debug_state(self):
        s = self.current_slide
        return (f"page={self.index + 1}/{self.page_count} {s.scene if s else '-'} "
                f"file={self.media_player.source().fileName()} t={self.media_player.position()} "
                f"state={self.playback_state} finished={self.finished} frame={self._first_frame}")

    def exit(self):
        self.media_player.stop()
        self.media_player.setSource(QUrl())
        super().exit()


class LiveVideoView(ShowView):
    """A paint-only mirror. It never owns a decoder."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.blanked = False
        self.badge = ""
        self.preferred_size = QSize(80, 45)
        self.setMinimumSize(0, 0)

    def sizeHint(self):
        return self.preferred_size

    def set_frame(self, image):
        self.image = image
        self.update()

    def paint_content(self, painter):
        super().paint_content(painter)
        if self.blanked:
            painter.setPen(QColor.fromRgbF(1, 1, 1, .6))
            painter.setFont(Theme.font(20, QFont.Weight.Bold))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "SCREEN BLANKED")
        if self.badge:
            painter.setFont(Theme.font(11, QFont.Weight.DemiBold))
            metrics = painter.fontMetrics()
            rect = QRectF(10, 10, metrics.horizontalAdvance(self.badge), metrics.height() + 4)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor.fromRgbF(0, 0, 0, .55))
            painter.drawRoundedRect(rect, 4, 4)
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.badge)


class NotesTextView(QTextEdit):
    def __init__(self, hint, parent=None):
        super().__init__(parent)
        self.on_focus = self.on_blur = self.on_images = None
        self.setAcceptRichText(False)
        self.setFont(Theme.font(20))
        self.document().setDocumentMargin(12)
        self.setStyleSheet("QTextEdit { background: #1e1e1e; border: 0; border-radius: 6px; }")
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        # QTextEdit paints its placeholder inside the empty document's one-line frame.
        self._hint_label = QLabel(hint, self.viewport())
        self._hint_label.setWordWrap(True)
        self._hint_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._hint_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._hint_label.setStyleSheet("color: rgba(255, 255, 255, 89); background: transparent;")
        self.textChanged.connect(self._sync_hint)
        self._sync_hint()

    def _sync_hint(self):
        self._hint_label.setVisible(not self.hasFocus() and self.document().isEmpty())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        margin = int(self.document().documentMargin())
        self._hint_label.setGeometry(self.viewport().rect().adjusted(margin, margin, -margin, -margin))

    @property
    def notes_text(self):
        from .note_images import NoteImages
        return NoteImages.markup(self.document())

    def focusInEvent(self, e):
        super().focusInEvent(e)
        self._sync_hint()
        if self.on_focus:
            self.on_focus()

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self._sync_hint()
        if self.on_blur:
            self.on_blur()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape or (e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                                          and e.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.clearFocus()
            return
        super().keyPressEvent(e)

    def insertFromMimeData(self, source):
        if self.on_images and (source.hasImage() or source.hasUrls()):
            self.on_images(source)
        else:
            super().insertFromMimeData(source)


class PresenterWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.on_key = self.on_close = self.on_press = None
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, obj, e):
        if isinstance(obj, QWidget) and obj.window() is self and e.type() == QEvent.Type.MouseButtonPress:
            if self.on_press:
                self.on_press(obj)
        elif e.type() == QEvent.Type.KeyPress and isinstance(obj, QWidget) and obj.window() is self:
            focused = QApplication.focusWidget()
            if isinstance(focused, QTextEdit) and focused.window() is self:
                return False
            if self.on_key and self.on_key(e):
                return True
        return super().eventFilter(obj, e)

    def keyPressEvent(self, e):
        if not self.on_key or not self.on_key(e):
            super().keyPressEvent(e)

    def closeEvent(self, e):
        if self.on_close:
            self.on_close()
        e.accept()


class BuildThumb(QWidget):
    def __init__(self, number, *, build=True):
        super().__init__()
        self.image = QImage()
        self.number = number
        self.build = build
        self.frame_key = None
        self.on_click = None
        self.selected = False
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        label = f"{'Build' if build else 'Slide'} {number}"
        self.setAccessibleName(label)
        if build:
            self.setToolTip(label)
        self.flag = None
        if not build:
            self.flag = QWidget(self)
            self.flag.setFixedSize(10, 10)
            self.flag.setStyleSheet("background: #ff2e29; border: 1.5px solid white; border-radius: 5px;")
            self.flag.setToolTip("Has feedback")
            self.flag.setAccessibleName("Has feedback")
            self.flag.hide()

    def heightForWidth(self, width):
        return round(width * 9 / 16)

    def sizeHint(self):
        return QSize(190, self.heightForWidth(190))

    def set_frame(self, image):
        self.image = image
        self.update()

    def resizeEvent(self, event):
        if self.flag is not None:
            self.flag.move(self.width() - 14, 4)
        super().resizeEvent(event)

    def mousePressEvent(self, event):
        if self.on_click and event.button() == Qt.MouseButton.LeftButton:
            self.on_click()
            event.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        bounds = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(bounds, 5, 5)
        p.fillPath(path, Qt.GlobalColor.black)
        p.setClipPath(path)
        if not self.image.isNull():
            aspect = self.image.width() / self.image.height()
            w = min(bounds.width(), bounds.height() * aspect)
            h = w / aspect
            p.setOpacity(.72 if self.build and not self.selected else 1.)
            p.drawImage(QRectF((bounds.width() - w) / 2, (bounds.height() - h) / 2, w, h), self.image)
            p.setOpacity(1)
        p.setClipping(False)
        width = (2 if self.build else 2.5) if self.selected else 1
        p.setPen(QPen(Theme.accent if self.selected else QColor.fromRgbF(1, 1, 1, .18 if self.build else .12), width))
        p.setBrush(Qt.BrushStyle.NoBrush)
        d = width / 2
        p.drawRoundedRect(bounds.adjusted(d, d, -d, -d), 5 - d, 5 - d)
        if self.build:
            pill = QRectF(4, 3, 14, 12)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor.fromRgbF(0, 0, 0, .55))
            p.drawRoundedRect(pill, 3, 3)
            p.setPen(Qt.GlobalColor.white)
            p.setFont(Theme.tabular(9, QFont.Weight.Bold))
            p.drawText(pill, Qt.AlignmentFlag.AlignCenter, str(self.number))


class SlideThumb(QWidget):
    def __init__(self, number, builds, parent=None):
        super().__init__(parent)
        self.number = number
        self.on_click = None
        self.selected = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 6, 12, 6)
        layout.setSpacing(6)
        self.main = BuildThumb(number, build=False)
        self.main.on_click = lambda: self.on_click() if self.on_click else None
        layout.addWidget(self.main)
        self.builds = [BuildThumb(i + 1) for i in range(builds)] if builds > 1 else []
        for thumb in self.builds:
            row = QHBoxLayout()
            row.addSpacing(16)
            row.addWidget(thumb)
            layout.addLayout(row)

    def mousePressEvent(self, event):
        if self.on_click and event.button() == Qt.MouseButton.LeftButton:
            self.on_click()
            event.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setPen(Theme.accent if self.selected else QColor.fromRgbF(1, 1, 1, .5))
        p.setFont(Theme.tabular(11, QFont.Weight.Medium))
        p.drawText(QRectF(2, 8, 20, p.fontMetrics().height()), Qt.AlignmentFlag.AlignRight, str(self.number))


class PresenterProgress(QWidget):
    def __init__(self):
        super().__init__()
        self.fraction = 0.
        self.setFixedSize(220, 3)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor.fromRgbF(1, 1, 1, .15))
        p.drawRoundedRect(QRectF(self.rect()), 1.5, 1.5)
        p.setBrush(Theme.accent)
        p.drawRoundedRect(QRectF(0, 0, self.width() * self.fraction, 3), 1.5, 1.5)


def _dark_button(button, text, symbol, tip, *, size=18, active=False):
    tint = Theme.accent if active else QColor.fromRgbF(1, 1, 1, .85)
    palette = button.palette()
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        palette.setColor(group, QPalette.ColorRole.ButtonText, tint)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#666666"))
    button.setPalette(palette)
    button.setText(text)
    button.setAccessibleName(text)
    button.setToolTip(tip)
    button.setIcon(template_icon(symbol, button))
    button.setIconSize(QSize(size, size))


def _dark_palette(widget):
    palette = widget.palette()
    for role, color in ((QPalette.ColorRole.Window, QColor("#121212")),
                        (QPalette.ColorRole.WindowText, QColor("#d9d9d9")),
                        (QPalette.ColorRole.Base, QColor("#1e1e1e")),
                        (QPalette.ColorRole.AlternateBase, QColor("#242424")),
                        (QPalette.ColorRole.Text, QColor("#f2f2f2")),
                        (QPalette.ColorRole.PlaceholderText, QColor.fromRgbF(1, 1, 1, .35)),
                        (QPalette.ColorRole.Button, QColor("#292929")),
                        (QPalette.ColorRole.ButtonText, QColor("#d9d9d9")),
                        (QPalette.ColorRole.Highlight, QColor("#a7480e")),
                        (QPalette.ColorRole.HighlightedText, QColor("white"))):
        palette.setColor(role, color)
    widget.setPalette(palette)


def _chrome_label(text="", size=12, *, opacity=.85, weight=QFont.Weight.Normal):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setFont(Theme.font(size, weight))
    palette = label.palette()
    palette.setColor(QPalette.ColorRole.WindowText, QColor.fromRgbF(1, 1, 1, opacity))
    label.setPalette(palette)
    return label


class PresenterController(QObject):
    def __init__(self, *, source=None, slides=None, links=(), scenes=(), notes=(),
                 build_notes=(), start_slide=0, deck=None, start_scene=0,
                 presenter_screen=None, audience_screen=None):
        super().__init__()
        self.presenter_screen = presenter_screen or QApplication.primaryScreen()
        self.audience_screen = audience_screen or self.presenter_screen
        self.shared_screen = self.audience_screen is self.presenter_screen
        self.source = source or (AnimatedPlayer(self.audience_screen) if deck is not None
                                 else SlidePlayer(self.audience_screen))
        self.is_animated = isinstance(self.source, AnimatedPlayer)
        self.on_exit = self.on_detach = self.on_save_notes = None
        self.finished = False
        self.feedback_mode = False
        self._feedback = None
        self._feedback_dialog = self._feedback_list = None
        self._note_image_base = None
        self._editing = {}
        self._note_imports = set()
        self._note_saves = {}
        self._shown = {}
        self._thumbnail_cache = OrderedDict()
        self._pending_frames = {}
        self._sidebar_token = -1
        self._scrolled_to_page = None
        self._slide_range = []
        self.thumbs = []
        self.started = time.monotonic()
        self.accumulated = 0.0
        self.paused = False
        self.window = PresenterWindow()
        self.window.setWindowTitle("Presenter View with Animations" if self.is_animated else "Presenter View")
        self.window.on_key = self.key
        self.window.on_close = self.end_show
        self.window.on_press = self._clicked_outside_feedback
        _dark_palette(self.window)
        self._build_content()
        self.source.on_change = self.refresh
        self.source.on_exit = self.finish
        self.source.frame_changed.connect(self._mirror_frame)
        self._mirror_frame(self.source.frame)
        self.source.on_error = self._show_error
        if source is None:
            if deck is not None:
                self.source.play(deck, start_scene, self.audience_screen, visible=False)
            else:
                self.source.scenes = list(scenes)
                self.source.notes = list(notes)
                self.source.build_notes = list(build_notes)
                self.source.play(slides or [], links, start_slide, self.audience_screen, visible=False)
        self.source.window.windowed_presentation = self.shared_screen
        self.source.window.present(self.audience_screen)
        self._place_presenter()
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        QApplication.instance().screenRemoved.connect(self._screen_removed)
        QApplication.instance().screenAdded.connect(self._screens_changed)
        self._screens_changed()
        self.refresh()
        self.tick()

    def _button(self, text, action, layout, *, symbol=None, tip=None, size=18, above=False):
        if symbol:
            button = QToolButton()
            button.setAutoRaise(True)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon if above
                                      else Qt.ToolButtonStyle.ToolButtonIconOnly)
            _dark_button(button, text, symbol, tip or text, size=size)
        else:
            button = QPushButton(text)
        button.setFont(Theme.font(12))
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(action)
        layout.addWidget(button)
        return button

    def _build_content(self):
        from .feedback import FeedbackLayer
        root = QWidget()
        root.setAutoFillBackground(True)
        self.window.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(10)
        self.tools = tools = QHBoxLayout()
        tools.setContentsMargins(20, 0, 20, 0)
        tools.setSpacing(22)
        self._button("End Show", self.end_show, tools, symbol="xmark.circle.fill", above=True)
        self.swap_button = self._button("Swap Displays", self.swap_displays, tools,
                                        symbol="arrow.left.arrow.right", above=True)
        self._button("Use Slide Show", self.use_slide_show, tools, symbol="play.rectangle", above=True,
                     tip="Close Presenter View and keep the show running")
        self.slide_notes_button = self._button("Hide Slide Notes", self.toggle_slide_notes, tools,
                                              symbol="note.text", above=True)
        self.build_notes_button = self._button("Hide Build Notes", self.toggle_build_notes, tools,
                                              symbol="list.bullet.rectangle", above=True)
        self.feedback_button = self._button("Feedback Mode", self.toggle_feedback_mode, tools,
                                            symbol="text.bubble", above=True,
                                            tip="Click the slide here to type red feedback notes on it (F). "
                                                "The audience never sees them; the editor shows them afterwards.")
        self.feedback_button.setEnabled(False)
        tools.addStretch()
        layout.addLayout(tools)

        timers = QHBoxLayout()
        timers.setContentsMargins(24, 0, 24, 0)
        timers.setSpacing(14)
        self.elapsed = _chrome_label(opacity=1)
        self.elapsed.setFont(Theme.tabular(26))
        self.clock = _chrome_label()
        self.clock.setFont(Theme.tabular(26))
        timers.addWidget(self.elapsed)
        self.pause_button = self._button("Pause", self.toggle_pause, timers, symbol="pause.fill",
                                         tip="Pause the timer", size=16)
        self._button("Reset the timer", self.reset_timer, timers, symbol="arrow.counterclockwise", size=16)
        timers.addStretch()
        self.feedback_list_button = self._button("Feedback List", self.show_feedback_list, timers)
        self.feedback_list_button.setEnabled(False)
        self.feedback_list_button.setToolTip("Review the private feedback notes from this presentation")
        timers.addWidget(_chrome_label("Audience:"))
        self.screen_choice = QComboBox()
        self.screen_choice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.screen_choice.setMinimumContentsLength(7)
        self.screen_choice.setAccessibleName("Audience display")
        self.screen_choice.activated.connect(self.choose_screen)
        timers.addWidget(self.screen_choice)
        timers.addWidget(self.clock)
        layout.addLayout(timers)

        body = QHBoxLayout()
        body.setSpacing(0)
        layout.addLayout(body, 1)
        self.sidebar = QScrollArea()
        self.sidebar.setObjectName("presenterSidebar")
        self.sidebar.setStyleSheet("#presenterSidebar, #presenterSidebarContent { background: #1a1a1a; border: 0; }")
        self.sidebar.setWidgetResizable(True)
        self.sidebar.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar.setFixedWidth(250)
        self.sidebar.setAccessibleName("Slides and builds")
        body.addWidget(self.sidebar)
        self.main = QWidget()
        body.addWidget(self.main, 1)
        column = QVBoxLayout(self.main)
        column.setContentsMargins(24, 0, 24, 14)
        column.setSpacing(0)
        self.current = LiveVideoView()
        self.current.setAccessibleName("Current slide")
        self.current.on_click = self._current_click
        self.current.over_link = lambda p: bool(p is not None and self.source.link(p))
        column.addWidget(self.current, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.feedback_layer = FeedbackLayer(parent=self.current)
        self.feedback_layer.slide_rect = lambda: self.current.slide_rect
        self.feedback_layer.fallback_responder = self.current
        self.feedback_layer.on_add = self.mark_feedback
        self.notes_row = QWidget()
        row = QHBoxLayout(self.notes_row)
        row.setContentsMargins(0, 14, 0, 12)
        row.setSpacing(20)
        self.notes = NotesTextView("No notes for this slide. Click here to type them.")
        self.build_notes = NotesTextView("No notes for this build. Click here to type them.")
        self.notes_columns = []
        for title, editor, is_build in (("SLIDE NOTES", self.notes, False), ("BUILD NOTES", self.build_notes, True)):
            panel = QWidget()
            box = QVBoxLayout(panel)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(6)
            box.addWidget(_chrome_label(title, 11, opacity=.45, weight=QFont.Weight.DemiBold))
            box.addWidget(editor, 1)
            editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
            editor.setAccessibleName(title.title())
            editor.on_focus = lambda tv=editor: self.begin_notes(tv)
            editor.on_blur = lambda tv=editor, b=is_build: self.end_notes(tv, b)
            editor.on_images = lambda mime, tv=editor: self._paste_images(mime, tv)
            row.addWidget(panel, 1)
            self.notes_columns.append(panel)
        column.addWidget(self.notes_row, 1)
        column.addStretch()

        self.error_label = _chrome_label()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #ff8787")
        self.error_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.error_label.hide()
        column.addWidget(self.error_label)
        self.footer = QWidget()
        foot = QHBoxLayout(self.footer)
        foot.setContentsMargins(0, 0, 0, 0)
        foot.addStretch(1)
        self.navigation = QWidget()
        controls = QHBoxLayout(self.navigation)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(18)
        self._button("Previous", self.go_back, controls, symbol="chevron.left", size=20)
        self.counter = _chrome_label(size=16)
        controls.addWidget(self.counter)
        self._button("Next", self.advance, controls, symbol="chevron.right", size=20)
        foot.addWidget(self.navigation)
        foot.addStretch(1)
        self.hint_text = ("→ next · ← back · space pause · V reverse · 1–9 chapter · B blank · N next slide · "
                          "F feedback · esc end · click notes to edit" if self.is_animated else
                          "→ / space next · ← back · B blank · N next slide · F feedback · esc end · click notes to edit")
        self.hint = _chrome_label(size=11, opacity=.4)
        self.hint.setParent(self.footer)
        self.hint.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.hint.setToolTip(self.hint_text)
        column.addWidget(self.footer)
        column.addSpacing(6)
        self.progress = PresenterProgress()
        column.addWidget(self.progress, alignment=Qt.AlignmentFlag.AlignHCenter)
        for obj in (root, self.main, self.footer, self.navigation):
            obj.installEventFilter(self)
        self.apply_notes_visibility()

    def eventFilter(self, obj, e):
        if e.type() == QEvent.Type.Resize:
            if obj is self.main:
                self._fit_picture()
            elif obj in (self.footer, self.navigation):
                QTimer.singleShot(0, self._layout_hint)
            elif obj is self.window.centralWidget():
                self.tools.setSpacing(min(22, max(4, (obj.width() - 640) // 5)))
        return super().eventFilter(obj, e)

    def _layout_hint(self):
        left = self.navigation.geometry().right() + 16
        room = max(0, self.footer.width() - left)
        self.hint.setGeometry(left, 0, room, self.footer.height())
        self.hint.setText(self.hint.fontMetrics().elidedText(self.hint_text, Qt.TextElideMode.ElideRight, room))

    def _fit_picture(self):
        notes_visible = not self.notes_row.isHidden()
        available = self.main.height() - 23 - self.footer.sizeHint().height()
        if not self.error_label.isHidden():
            available -= self.error_label.heightForWidth(max(1, self.main.width() - 48))
        if notes_visible:
            available -= min(176, max(68, available // 2))
            available = min(available, round(self.window.height() * .6))
        height = max(1, min(available, round((self.main.width() - 48) * 9 / 16)))
        size = QSize(round(height * 16 / 9), height)
        if size != self.current.preferred_size:
            self.current.preferred_size = size
            self.current.setMaximumSize(size)
            self.current.updateGeometry()

    def _mirror_frame(self, image):
        was = self.current.slide_rect
        self.current.set_frame(image)
        if self.current.slide_rect != was:
            self.feedback_layer.relayout()

    def _clicked_outside_feedback(self, target):
        if self.feedback_layer.is_editing and target is not self.feedback_layer and not self.feedback_layer.isAncestorOf(target):
            self.feedback_layer.end_editing()

    def _place_presenter(self):
        r = self.presenter_screen.availableGeometry()
        min_w, min_h = min(820, r.width()), min(560, r.height())
        self.window.setMinimumSize(min_w, min_h)
        self.window.setScreen(self.presenter_screen)
        if self.shared_screen:
            self.window.showNormal()
            self.window.resize(max(min_w, min(round(r.width() * .68), r.width() - 48)),
                               max(min_h, min(round(r.height() * .68), r.height() - 48)))
            self.window.move(r.center() - self.window.rect().center())
        else:
            self.source.window.when_settled(self._fill_presenter)

    def _fill_presenter(self):
        if not self.finished and not self.shared_screen:
            self.window.setGeometry(self.presenter_screen.geometry())
            self.window.showFullScreen()

    def _screens_changed(self, *_):
        self.screen_choice.blockSignals(True)
        self.screen_choice.clear()
        for screen in QApplication.screens():
            self.screen_choice.addItem(screen.name(), screen)
        index = self.screen_choice.findData(self.audience_screen)
        self.screen_choice.setCurrentIndex(max(0, index))
        self.screen_choice.blockSignals(False)
        self.swap_button.setEnabled(not self.shared_screen)
        self.swap_button.setToolTip("Needs a second display" if self.shared_screen else
                                    "Show the slides on this display and the notes on the other")

    def _screen_removed(self, screen):
        if self.finished:
            return
        fallback = QApplication.primaryScreen()
        if screen is self.presenter_screen:
            self.presenter_screen = fallback
        if screen is self.audience_screen:
            self.audience_screen = fallback
        self.shared_screen = self.presenter_screen is self.audience_screen
        self.source.window.windowed_presentation = self.shared_screen
        self.source.window.move_to(self.audience_screen)
        self._place_presenter()
        self._screens_changed()

    def choose_screen(self, index):
        screen = self.screen_choice.itemData(index)
        if screen and screen is not self.audience_screen:
            self.audience_screen = screen
            self.shared_screen = screen is self.presenter_screen
            self.source.window.windowed_presentation = self.shared_screen
            self.source.window.move_to(screen)
            self._place_presenter()
            self._screens_changed()

    def swap_displays(self):
        if self.shared_screen:
            return
        self.presenter_screen, self.audience_screen = self.audience_screen, self.presenter_screen
        self.source.window.move_to(self.audience_screen)
        self.window.showNormal()
        self._place_presenter()
        self._screens_changed()

    @property
    def note_image_base(self):
        return self._note_image_base

    @note_image_base.setter
    def note_image_base(self, value):
        self._note_image_base = Path(value) if value else None
        self._shown.clear()
        self.refresh()

    @property
    def feedback(self):
        return self._feedback

    @feedback.setter
    def feedback(self, value):
        if self.feedback_mode and value is None:
            self.toggle_feedback_mode()
        self._feedback = value
        self.feedback_layer.store = value
        self.feedback_button.setEnabled(value is not None)
        self.feedback_list_button.setEnabled(value is not None)
        self.feedback_changed()

    def _show_error(self, text):
        self.error_label.setText(text)
        self.error_label.setVisible(bool(text))
        self._fit_picture()

    def _current_click(self, point, primary):
        if not primary:
            self.go_back()
        elif (url := self.source.link(point)) is not None:
            QDesktopServices.openUrl(url)
        else:
            self.advance()

    def _paste_images(self, mime, tv):
        if tv not in self._editing:
            self.begin_notes(tv)
        if tv not in self._editing or tv in self._note_imports or not self.note_image_base:
            return
        from .note_images import NoteImages
        base = self.note_image_base
        self._note_imports.add(tv)
        tv.setReadOnly(True)
        def paste():
            try:
                paths = yield from NoteImages.import_images(mime, base)
                if paths:
                    NoteImages.insert(paths, tv, base, min(480, max(120, tv.viewport().width() - 24)))
                else:
                    tv.setReadOnly(False)
                    QTextEdit.insertFromMimeData(tv, mime)
            except Exception as exc:
                self._show_error(str(exc))
            finally:
                self._note_imports.remove(tv)
                tv.setReadOnly(False)
                if not tv.hasFocus() or self.finished:
                    self.end_notes(tv, tv is self.build_notes)
                    self.refresh()
        spawn(paste())

    def _show_notes(self, tv, text):
        if tv.hasFocus() or tv in self._editing or self._shown.get(tv) == text:
            return
        from .note_images import NoteImages
        document = NoteImages.attributed(text, self.note_image_base,
                                         max_width=min(480, max(120, tv.viewport().width() - 24)))
        document.setDefaultFont(tv.font())
        document.setDocumentMargin(12)
        document.setParent(tv)
        tv.setDocument(document)
        self._shown[tv] = text

    def begin_notes(self, tv):
        if not self.finished and self.source.page_count and tv not in self._editing:
            page = self.source.presenter_page(self.source.page_index)
            self._editing[tv] = replace(page, notes=self._note_text(page, False), build_notes=self._note_text(page, True))

    def _note_text(self, page, build):
        pending = self._note_saves.get((page.scene, page.build if build else None))
        return pending[1] if pending else (page.build_notes if build else page.notes)

    def end_notes(self, tv, is_build=False):
        if tv in self._note_imports:
            return
        page = self._editing.pop(tv, None)
        if page is None:
            return
        text = tv.notes_text
        self._shown[tv] = text
        old = page.build_notes if is_build else page.notes
        if text == old or not page.scene or not self.on_save_notes:
            return
        key = (page.scene, page.build if is_build else None)
        draft = (object(), text)
        self._note_saves[key] = draft

        def saved(ok):
            if self._note_saves.get(key) is draft:
                del self._note_saves[key]
            if ok:
                for i in range(self.source.page_count):
                    current = self.source.presenter_page(i)
                    if current.scene == page.scene and (not is_build or current.build == page.build):
                        self.source.set_notes(text, i, is_build)
                        break
            else:
                self._show_error(f"Could not save {page.scene}'s notes.")
                self._recover_notes(page, text, is_build)
            QTimer.singleShot(0, self.refresh)

        self.on_save_notes(*key, text, saved)

    def _recover_notes(self, page, text, is_build):
        # A save can finish after navigation or show exit. Keep its captured
        # draft in an independent window, never in a different page's editor.
        dialog = _NotesRecoveryDialog(page.scene, text)
        recoveries = getattr(self, "_note_recoveries", None)
        if recoveries is None:
            self._note_recoveries = recoveries = []
        recoveries.append(dialog)
        def retry():
            dialog.retry.setEnabled(False)
            def saved(ok):
                dialog.retry.setEnabled(True)
                if ok:
                    dialog.saved = True
                    dialog.accept()
                else:
                    dialog.message.setText("Saving failed. Your draft is still here; retry or copy it.")
            self.on_save_notes(page.scene, page.build if is_build else None, dialog.editor.toPlainText(), saved)
        dialog.retry.clicked.connect(retry)
        def finished(_):
            recoveries.remove(dialog)
            dialog.deleteLater()
        dialog.finished.connect(finished)
        dialog.show()

    def _request_picture(self, page, callback):
        path = page.still or page.clip
        if not path:
            return
        key = (path, bool(page.clip))
        if key in self._thumbnail_cache:
            callback(self._thumbnail_cache[key])
            return
        receivers = self._pending_frames.setdefault(key, [])
        receivers.append(callback)
        if len(receivers) > 1:
            return
        def load():
            try:
                image = yield run_blocking(_thumbnail, path, bool(page.clip))
                image = _mapped(image)
                self._thumbnail_cache[key] = image
                while len(self._thumbnail_cache) > 400:
                    self._thumbnail_cache.popitem(last=False)
                if not self.finished:
                    for receive in receivers:
                        receive(image)
            except Exception as exc:
                if not self.finished:
                    self._show_error(str(exc))
            finally:
                self._pending_frames.pop(key, None)
        spawn(load())

    def build_sidebar(self):
        ranges = []
        for i in range(self.source.page_count):
            page = self.source.presenter_page(i)
            if ranges and self.source.presenter_page(ranges[-1][0]).slide == page.slide:
                ranges[-1] = (ranges[-1][0], i)
            else:
                ranges.append((i, i))
        if ranges != self._slide_range or not self.thumbs:
            scroll = self.sidebar.verticalScrollBar().value()
            self._slide_range = ranges
            holder = QWidget()
            holder.setObjectName("presenterSidebarContent")
            holder.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
            column = QVBoxLayout(holder)
            column.setContentsMargins(0, 6, 0, 12)
            column.setSpacing(4)
            self.thumbs = []
            for first, last in ranges:
                thumb = SlideThumb(self.source.presenter_page(first).slide + 1, last - first + 1)
                thumb.on_click = lambda i=first: self.source.go(i)
                for b, panel in enumerate(thumb.builds):
                    panel.on_click = lambda i=first + b: self.source.go(i)
                column.addWidget(thumb)
                self.thumbs.append(thumb)
            column.addStretch()
            self.sidebar.setWidget(holder)
            self.sidebar.verticalScrollBar().setValue(scroll)
        for thumb, (first, last) in zip(self.thumbs, ranges):
            self._load_thumb(thumb, thumb.main, last)
            for i, panel in enumerate(thumb.builds, first):
                self._load_thumb(thumb, panel, i)

    def _load_thumb(self, thumb, panel, index):
        page = self.source.presenter_page(index)
        key = (page.still, page.clip)
        if key == panel.frame_key:
            return
        panel.frame_key = key
        panel.set_frame(QImage())

        def assign(image):
            if thumb in self.thumbs and panel.frame_key == key:
                panel.set_frame(image)

        self._request_picture(page, assign)

    def refresh(self):
        if self.finished or not self.source.page_count:
            return
        if self.source.reload_token != self._sidebar_token:
            self._sidebar_token = self.source.reload_token
            self.build_sidebar()
        page = self.source.presenter_page(self.source.page_index)
        for tv, captured in list(self._editing.items()):
            if captured.scene != page.scene or captured.build != page.build:
                tv.clearFocus()
        self.current.blanked = self.source.is_blacked_out
        self.current.content_opacity = .25 if self.current.blanked else 1.
        state = self.source.playback_state
        glyph = {"Playing": "▶︎", "Paused": "❙❙", "Build done": "■"}.get(state)
        self.current.badge = f"  {glyph}  {state.upper()}  " if glyph else ""
        self.current.placeholder = "" if self.current.blanked else self.source.window.show_view.placeholder
        self.current.update()
        self.counter.setText(f"Slide {page.slide + 1} of {self.source.slide_count}" +
                             (f"  ·  build {page.build + 1}/{page.builds}" if page.builds > 1 else ""))
        self.progress.fraction = (self.source.page_index + 1) / self.source.page_count
        self.progress.update()
        self._show_notes(self.notes, self._note_text(page, False))
        self._show_notes(self.build_notes, self._note_text(page, True))
        self.feedback_layer.scene = page.scene
        self.feedback_layer.build = page.build
        target = None
        for thumb, (first, last) in zip(self.thumbs, self._slide_range):
            selected = first <= self.source.page_index <= last
            if selected != thumb.selected:
                thumb.selected = thumb.main.selected = selected
                thumb.update()
                thumb.main.update()
            for i, panel in enumerate(thumb.builds):
                panel_selected = selected and first + i == self.source.page_index
                if panel_selected != panel.selected:
                    panel.selected = panel_selected
                    panel.update()
            if selected:
                target = _at(thumb.builds, self.source.page_index - first, thumb.main)
        if target and self._scrolled_to_page != self.source.page_index:
            self._scrolled_to_page = self.source.page_index
            QTimer.singleShot(0, lambda: self._follow_thumb(target))
        self.mark_feedback()

    def _follow_thumb(self, target):
        if not self.finished and any(target is thumb.main or target in thumb.builds for thumb in self.thumbs):
            self.sidebar.ensureWidgetVisible(target, 0, 24)

    @property
    def live_frame_ready(self):
        return self.is_animated and bool(self.source.live_player and self.source._first_frame)

    @property
    def notes_image_count(self):
        count = 0
        block = self.notes.document().begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                if it.fragment().charFormat().isImageFormat():
                    count += 1
                it += 1
            block = block.next()
        return count

    def mark_feedback(self):
        for thumb, (first, _) in zip(self.thumbs, self._slide_range):
            scene = self.source.presenter_page(first).scene
            thumb.main.flag.setVisible(bool(self.feedback is not None and scene and self.feedback.notes_for(scene)))

    def feedback_changed(self):
        if self.finished:
            return
        self.feedback_layer.rebuild()
        self.mark_feedback()
        if self._feedback_list is not None:
            QTimer.singleShot(0, self._reload_feedback_list)

    def _reload_feedback_list(self):
        if self._feedback_list is not None:
            self._feedback_list.reload()

    def set_feedback_mode(self, on):
        if bool(on) != self.feedback_mode:
            self.toggle_feedback_mode()

    def toggle_feedback_mode(self):
        if self.feedback is None:
            return
        self.feedback_mode = not self.feedback_mode
        self.feedback_layer.placing = self.feedback_mode
        if not self.feedback_mode:
            self.feedback_layer.end_editing()
        _dark_button(self.feedback_button, "Feedback On" if self.feedback_mode else "Feedback Mode",
                     "text.bubble", self.feedback_button.toolTip(), active=self.feedback_mode)
        tip = "Click to add a feedback note" if self.feedback_mode else ""
        self.current.setToolTip(tip)
        self.feedback_layer.setToolTip(tip)

    def show_feedback_list(self):
        if self.feedback is None:
            return
        if self._feedback_dialog is not None:
            self._feedback_dialog.raise_()
            self._feedback_dialog.activateWindow()
            return
        from .feedback import FeedbackListView
        dialog = QDialog(self.window)
        dialog.setWindowTitle("Presentation Feedback")
        layout = QVBoxLayout(dialog)
        view = FeedbackListView(self.feedback)
        view.slide_label = self._feedback_scene_label

        def go(note):
            pages = [(i, self.source.presenter_page(i)) for i in range(self.source.page_count)]
            items = [(i, page) for i, page in pages if page.scene == note.scene]
            if not items:
                self._show_error("That slide no longer exists")
                return
            i = next((i for i, page in items if page.build == note.build), items[0][0])
            if i != self.source.page_index:
                self.source.go(i)
            self.window.activateWindow()
            self.feedback_layer.focus(note.id)
        view.on_go = go
        view.on_close = dialog.close
        view.reload()
        layout.addWidget(view)
        dialog.resize(540, 500)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(self._close_feedback_list)
        dialog.show()
        self._feedback_dialog, self._feedback_list = dialog, view

    def _close_feedback_list(self):
        self._feedback_dialog = self._feedback_list = None

    def _feedback_scene_label(self, scene):
        for i in range(self.source.page_count):
            page = self.source.presenter_page(i)
            if page.scene == scene:
                return f"Slide {page.slide + 1} · {scene}"
        return f"Missing slide · {scene}"

    def toggle_slide_notes(self):
        prefs.set("presenterHideSlideNotes", not prefs.get_bool("presenterHideSlideNotes"))
        self.apply_notes_visibility()

    def toggle_build_notes(self):
        prefs.set("presenterHideBuildNotes", not prefs.get_bool("presenterHideBuildNotes"))
        self.apply_notes_visibility()

    def apply_notes_visibility(self):
        hidden = [prefs.get_bool("presenterHideSlideNotes"), prefs.get_bool("presenterHideBuildNotes")]
        for panel, tv, hide, button, what, symbol in zip(
                self.notes_columns, (self.notes, self.build_notes), hidden,
                (self.slide_notes_button, self.build_notes_button), ("Slide", "Build"),
                ("note.text", "list.bullet.rectangle")):
            if hide:
                tv.clearFocus()
            panel.setVisible(not hide)
            _dark_button(button, f"{'Show' if hide else 'Hide'} {what} Notes", symbol,
                         f"Show the {what.lower()} notes under the slide" if hide else
                         f"Hide the {what.lower()} notes; the other panel, or the slide, takes their room", active=hide)
        self.notes_row.setVisible(not all(hidden))
        self._fit_picture()

    def tick(self):
        elapsed = int(self.accumulated + (0 if self.paused else time.monotonic() - self.started))
        self.elapsed.setText(f"{elapsed // 3600}:{elapsed // 60 % 60:02}:{elapsed % 60:02}" if elapsed >= 3600
                             else f"{elapsed // 60:02}:{elapsed % 60:02}")
        self.clock.setText(datetime.now().strftime("%-I:%M %p"))

    def reset_timer(self):
        self.accumulated = 0.0
        self.started = time.monotonic()
        self.tick()

    def toggle_pause(self):
        if self.paused:
            self.started = time.monotonic()
        else:
            self.accumulated += time.monotonic() - self.started
        self.paused = not self.paused
        _dark_button(self.pause_button, "Resume" if self.paused else "Pause",
                     "play.fill" if self.paused else "pause.fill",
                     "Resume the timer" if self.paused else "Pause the timer", size=16)
        self.tick()

    def advance(self):
        self.source.next()

    def go_back(self):
        self.source.previous()

    def end_show(self):
        self.source.exit()

    def key(self, e):
        if not e.modifiers() and e.text().lower() == "r":
            self.reset_timer(); return True
        if not e.modifiers() and e.text().lower() == "f":
            self.toggle_feedback_mode(); return True
        return self.source.window.on_key(e)

    def _end_editing(self):
        self.notes.clearFocus(); self.build_notes.clearFocus()
        self.feedback_layer.end_editing()

    def use_slide_show(self):
        if self.finished:
            return
        self._dismiss_presenter()
        self.source.window.windowed_presentation = False
        self.source.window.present(self.audience_screen)
        if self.on_detach:
            self.on_detach(self.source)

    def finish(self):
        if self.finished:
            return
        self._dismiss_presenter()
        if self.on_exit:
            self.on_exit()

    def _dismiss_presenter(self):
        self._end_editing()
        self.finished = True
        self.timer.stop()
        if self._feedback_dialog is not None:
            self._feedback_dialog.close()
        self.window.on_close = self.window.on_key = self.window.on_press = None
        self.window.hide()
        self.source.on_change = None
        self.source.frame_changed.disconnect(self._mirror_frame)
        app = QApplication.instance()
        app.removeEventFilter(self.window)
        app.screenRemoved.disconnect(self._screen_removed)
        app.screenAdded.disconnect(self._screens_changed)


@dataclass
class AnimationClip:
    url: Path
    kind: str = "video"
    playback_rate: float = 1.0

    @classmethod
    def from_record(cls, item):
        if isinstance(item, cls):
            return item
        return cls(Path(item["path"]), item.get("kind", "video"), float(item.get("playbackRate") or 1))


class AnimationPreviewView(QWidget):
    navigation_keys = (Qt.Key.Key_Escape, Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Space)

    def __init__(self, scene, clips, parent=None):
        super().__init__(parent)
        self.setAccessibleName(f"{scene} — Animation Preview")
        self.setObjectName("animationPreview")
        self.setStyleSheet("#animationPreview { background: black; }")
        _dark_palette(self)
        self.on_close = self.on_error = None
        self.clips = [AnimationClip.from_record(c) for c in clips]
        self.player = AnimatedPlayer(preview=True)
        self.player.show_progress = False
        self.player.on_error = self._error
        self.player.on_change = self._changed
        self._stopped = False
        self._visible_generation = self._ended_generation = -1
        self._advance = QTimer(self)
        self._advance.setSingleShot(True)
        self._advance.timeout.connect(self.next_build)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 12)
        layout.setSpacing(8)
        heading = QWidget()
        heading.setFixedHeight(30)
        header = QHBoxLayout(heading)
        header.setContentsMargins(16, 0, 16, 0)
        header.setSpacing(12)
        header.addWidget(_chrome_label(scene, 14, opacity=1, weight=QFont.Weight.Bold), 1)
        close = QPushButton("Close")
        close.clicked.connect(self.close_preview)
        header.addWidget(close)
        layout.addWidget(heading)

        self.video = self.player.window.show_view
        self.video.setParent(self)
        self.video.on_click = self.video.on_key = None
        self.video.setCursor(Qt.CursorShape.ArrowCursor)
        self.video.setAccessibleName("Animation preview image")
        layout.addWidget(self.video, 1)
        overlay = QVBoxLayout(self.video)
        overlay.setContentsMargins(12, 8, 12, 12)
        self.error_label = _chrome_label()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #ff8787; background: #161616;")
        self.error_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.error_label.hide()
        overlay.addWidget(self.error_label)
        overlay.addStretch()
        self.media_controls = QWidget()
        self.media_controls.setObjectName("animationMediaControls")
        self.media_controls.setStyleSheet("#animationMediaControls { background: rgba(20, 20, 20, 220); border-radius: 6px; }")
        bar = QHBoxLayout(self.media_controls)
        bar.setContentsMargins(6, 2, 8, 2)
        self.pause = QToolButton()
        self.pause.setAutoRaise(True)
        self.pause.clicked.connect(self.toggle_pause)
        bar.addWidget(self.pause)
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setAccessibleName("Playback position")
        self.seek_slider.setRange(0, 0)
        self.seek_slider.valueChanged.connect(self._seek)
        bar.addWidget(self.seek_slider, 1)
        overlay.addWidget(self.media_controls)
        self.media_controls.hide()
        self.player.media_player.durationChanged.connect(self._duration)
        self.player.media_player.positionChanged.connect(self._position)

        footer = QWidget()
        footer.setFixedHeight(32)
        row = QHBoxLayout(footer)
        row.setContentsMargins(12, 0, 12, 0)
        row.setSpacing(12)
        row.addStretch(1)
        self.previous = QPushButton("Previous build")
        self.counter = _chrome_label(opacity=.65)
        self.next = QPushButton("Next build")
        self.replay = QPushButton("Replay")
        for widget in (self.previous, self.counter, self.next, self.replay):
            widget.setFont(Theme.font(12))
            row.addWidget(widget)
        for button, action in ((self.previous, self.previous_build), (self.next, self.next_build),
                               (self.replay, self.replay_build)):
            button.clicked.connect(action)
        row.addStretch(1)
        layout.addWidget(footer)
        QApplication.instance().installEventFilter(self)
        self._deck = PresentDeck([PresentSlide(scene, index=i, count=len(self.clips), kind=c.kind,
                                              file=str(c.url), rate=c.playback_rate)
                                 for i, c in enumerate(self.clips)])
        QTimer.singleShot(0, self._start)

    def _start(self):
        if not self._stopped:
            self.player.play(self._deck, visible=False)

    @property
    def index(self):
        return self.player.index

    def _duration(self, n):
        with QSignalBlocker(self.seek_slider):
            self.seek_slider.setRange(0, int(n))

    def _position(self, n):
        if not self.seek_slider.isSliderDown():
            with QSignalBlocker(self.seek_slider):
                self.seek_slider.setValue(int(n))

    def _seek(self, n):
        if self.player.live_player and not self.player.failed:
            self._advance.stop()
            self._ended_generation = -1
            self.player.seek(n)
            self._changed()

    def _prepare_generation(self):
        if self.player._generation != self._visible_generation:
            self._visible_generation = self.player._generation
            self.error_label.hide()
            self.error_label.clear()

    def _changed(self):
        if not self.clips:
            return
        self._prepare_generation()
        clip = self.clips[self.index]
        video = clip.kind == "video"
        last = self.index + 1 == len(self.clips)
        complete = self.player.finished and not self.player.failed
        self.counter.setText(f"Build {self.index + 1} of {len(self.clips)}" +
                             (" · End" if video and complete and last else ""))
        self.previous.setEnabled(self.index > 0)
        self.next.setEnabled(not last)
        self.media_controls.setVisible(video)
        self.media_controls.setEnabled(not self.player.failed)
        if video:
            playing = self.player.playback_state == "Playing"
            _dark_button(self.pause, "Pause" if playing else "Play",
                         "pause.fill" if playing else "play.fill",
                         "Pause" if playing else "Play", size=16)
        ready = video or self.player._loaded_still_path == str(clip.url) or bool(self.error_label.text())
        if complete and ready and not last and self._ended_generation != self._visible_generation:
            self._ended_generation = self._visible_generation
            self._advance.start(400 if video else 1000)

    def _error(self, text):
        self._prepare_generation()
        self.error_label.setText(text)
        self.error_label.show()
        if self.on_error:
            self.on_error(text)

    def play_build(self, index):
        if not self._stopped and 0 <= index < len(self.clips):
            self._advance.stop()
            self.player.go(index)
            self.video.setFocus()

    def previous_build(self):
        self.play_build(self.index - 1)

    def next_build(self):
        self.play_build(self.index + 1)

    def replay_build(self):
        self.play_build(self.index)

    def toggle_pause(self):
        media = self.player.live_player
        if media is None or self.player.failed or media.source().isEmpty():
            return
        if self.player.finished:
            if not self._advance.isActive():
                self.replay_build()
        else:
            self.player.play_pause()

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.setGeometry(obj.rect())
        if obj is self.video and event.type() in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress):
            advances = self.video.slide_rect.contains(event.position()) and event.position().y() < self.video.height() - 60
            if event.type() == QEvent.Type.MouseMove:
                self.video.setCursor(Qt.CursorShape.PointingHandCursor if advances else Qt.CursorShape.ArrowCursor)
            elif advances and event.button() == Qt.MouseButton.LeftButton:
                self.next_build()
                self.video.setFocus()
                return True
        if (event.type() in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress) and
                isinstance(obj, QWidget) and (obj is self or self.isAncestorOf(obj)) and
                event.key() in self.navigation_keys and not event.modifiers() &
                (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier)):
            if event.type() == QEvent.Type.KeyPress:
                {Qt.Key.Key_Escape: self.close_preview, Qt.Key.Key_Left: self.previous_build,
                 Qt.Key.Key_Right: self.next_build, Qt.Key.Key_Space: self.toggle_pause}[event.key()]()
            event.accept()
            return True
        return super().eventFilter(obj, event)

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        QApplication.instance().removeEventFilter(self)
        self._advance.stop()
        self.player.on_change = self.player.on_error = None
        self.player.exit()

    def close_preview(self):
        self.close()

    def closeEvent(self, e):
        self.stop()
        if self.on_close:
            self.on_close()
        e.accept()


@dataclass
class StillPages:
    slides: list
    links: list
    scenes: list[str]
    notes: list[str]
    build_notes: list
    pending: bool


def _still_extras(deck: Path | None):
    """Read authored link regions without importing executable presentation code."""
    if deck is None or not (deck / "present.json").is_file():
        return {}, None
    try:
        data = json.loads((deck / "present.json").read_text())
        return dict(data.get("links", {})), None
    except (OSError, ValueError, TypeError) as exc:
        return {}, f"Could not read present.json: {exc}"


class PresentationMixin:
    show_ahead = 3

    def init_presentation(self):
        self.player = None
        self.animated_player = None
        self.presenter = None
        self.animation_preview = None
        self.preview_scene = None
        self.show_pending = False
        self.pending_show_refresh = False
        self.present_with_presenter = False
        self.present_from = 0
        self._present_requested = False
        self._presentation_generation = 0
        self._playback_build = None
        self._playback_busy = False
        self._script_auto_pending = False

    def _presentation_init(self):
        if not hasattr(self, "_presentation_generation"):
            self.init_presentation()

    @property
    def animation_height(self):
        h = prefs.get_int("animationHeight", 720)
        return h if h in (720, 1080, 1440, 2160) else 720

    def animation_resolution_chosen(self, value):
        self._presentation_init()
        if self._show_open() or self.preview_scene is not None or self._playback_build:
            return
        h = value.data() if hasattr(value, "data") else value
        h = int(h)
        if h not in (720, 1080, 1440, 2160):
            raise ValueError("Animation resolution must be 720, 1080, 1440 or 2160")
        prefs.set("animationHeight", h)
        title = "4K" if h == 2160 else f"{h}p"
        self.toast("Animations build at 720p" if h == 720 else
                   f"Animations build at {title} from the next show (slides rebuild once)")

    @property
    def show_animations(self):
        return prefs.get_bool("playShowAnimations", False)

    def toggle_show_animations(self):
        prefs.set("playShowAnimations", not self.show_animations)
        self.toast("Play shows animations" if self.show_animations else "Play shows still slides")
        self.actions.refresh()

    def play_menu_from_beginning(self):
        self.present_from_beginning() if self.show_animations else self.play_from_beginning()

    def play_menu_from_current(self):
        self.present_from_current() if self.show_animations else self.play_from_current()

    def play_menu_presenter(self):
        self.presenter_view_animated() if self.show_animations else self.presenter_view()

    def present_from_current(self):
        self.present(True)

    def present_from_beginning(self):
        self.present(False)

    def presenter_view_animated(self):
        self.present(True, with_presenter=True)

    def play_from_beginning(self):
        self.play(False)

    def play_from_current(self):
        self.play(True)

    def presenter_view(self):
        self.play(True, with_presenter=True)

    def _show_open(self):
        self._check_playback_build()
        return bool(self.player or self.animated_player or self.presenter or self._present_requested)

    def _show_screen(self):
        return self.window.screen() or QApplication.primaryScreen()

    def _can_play(self, from_current=False):
        return (self.workspace is not None and self.window.isVisible() and not self._closing and
                bool(self.scene_name if from_current else (self.state or {}).get("scenes")))

    def _check_playback_build(self):
        job = self._playback_build
        if job is not None and (job[1] is not self.workspace or job[2] is not self.rpc or
                                not self.rpc or not self.rpc.running):
            self._finish_playback_build(abandon=True)

    def _finish_playback_build(self, *, abandon=False):
        if abandon and self._playback_build is not None:
            if self._playback_build[0] == "present":
                self._present_requested = self._script_auto_pending = False
            else:
                self.preview_scene = None
        self._playback_build = None
        if self._playback_busy:
            self._playback_busy = False
            self.busy_text = "Rendering…"
            self.busy -= 1
            return
        self.update_busy_pill()

    def _animation_status(self, text, progress=None):
        if self._playback_build is not None:
            self.busy_text = text
            self.busy_progress = progress
            if not self._playback_busy:
                self._playback_busy = True
                self.busy += 1
            else:
                self.update_busy_pill()
        else:
            self.busy_pill.show(text, spinning=True, progress=progress, cancellable=True)

    def _show_display_hint(self):
        self.toast("One display: the audience and presenter use separate windows. “Use Slide Show” switches to full screen.")

    def present(self, from_current=False, with_presenter=False, auto=False):
        self._presentation_init()
        if self._show_open() or self._playback_build or not self._can_play(from_current):
            return
        self.close_animation_preview()
        self._present_requested = True
        self.present_with_presenter = with_presenter
        self._script_auto_pending = auto
        order = [s["name"] for s in (self.state or {}).get("scenes", [])]
        self.present_from = order.index(self.scene_name) if from_current and self.scene_name in order else 0
        self._presentation_generation += 1
        job = self._playback_build = ("present", self.workspace, self.rpc)
        current = self.scene_name if from_current else _at(order, 0)
        def prepare():
            try:
                if (yield from self.finish_edits()) is False or not self._can_play(from_current):
                    if self._playback_build is job:
                        self._finish_playback_build(abandon=True)
                    return
                if self._playback_build is not job or self.workspace is not job[1] or self.rpc is not job[2]:
                    return
                self._animation_status("Preparing presentation…")
                args = {"height": self.animation_height, "ahead": self.show_ahead}
                if current:
                    args["current"] = current
                reply = yield self.call("present", args)
                if self._playback_build is not job:
                    return
                if not reply.get("ok"):
                    self._finish_playback_build(abandon=True)
                    self.toast(reply.get("error") or "Could not start the presentation", error=True)
            except Exception:
                if self._playback_build is job:
                    self._finish_playback_build(abandon=True)
                    self.toast("Could not start the presentation", error=True)
        spawn(prepare())

    def _configure_show(self, source, presenter=None):
        source.on_error = lambda text: self.toast(text, error=True)
        deck = self.workspace
        if presenter:
            source.on_error = lambda text: (presenter._show_error(text), self.toast(text, error=True))
        def returned():
            self._presentation_generation += 1
            self._present_requested = False
            self.show_pending = False
            self.update_busy_pill()
            self.player = self.animated_player = self.presenter = None
            if self.workspace is deck and not self._closing:
                self.return_from_show(source)
        if presenter:
            presenter.on_exit = returned
            def save_notes(scene, build, text, done):
                if self.workspace is deck:
                    self.save_presenter_notes(scene, build, text, done)
                else:
                    done(False)
            presenter.on_save_notes = save_notes
            presenter.note_image_base = self.work_deck
            presenter.feedback = self.feedback
            def detached(src):
                self.presenter = None
                if isinstance(src, AnimatedPlayer):
                    self.animated_player = src
                else:
                    self.player = src
                src.on_error = lambda text: self.toast(text, error=True)
                src.on_exit = returned
            presenter.on_detach = detached
        else:
            source.on_exit = returned

    def start_animated(self, deck):
        self._presentation_init()
        if self.player or self.animated_player or self.presenter or not self._present_requested:
            return
        deck = PresentDeck.from_record(deck)
        if not deck.slides:
            self._present_requested = False
            self._script_auto_pending = False
            self.toast("No built slides to play", error=True)
            return
        self._present_requested = False
        if self.present_with_presenter:
            here = self._show_screen()
            other = next((s for s in QApplication.screens() if s is not here), None)
            p = PresenterController(deck=deck, start_scene=self.present_from,
                                    presenter_screen=here, audience_screen=other)
            self.presenter = p
            source = p.source
            self._configure_show(source, p)
            if other is None:
                self._show_display_hint()
        else:
            source = AnimatedPlayer(self._show_screen())
            self.animated_player = source
            self._configure_show(source)
            source.play(deck, self.present_from)
        if self._script_auto_pending:
            self._script_auto_pending = False
            self.start_script_runner(source)

    def still_pages(self, deck):
        playable = [s for s in deck.get("slides", [])
                    if not s.get("error") and (s.get("pages") or s.get("pending"))]
        return StillPages([s.get("pages") or [""] * max(1, int(s.get("builds") or 1)) for s in playable],
                          [s.get("links") or [] for s in playable], [s["scene"] for s in playable],
                          [s.get("notes") or "" for s in playable],
                          [s.get("buildNotes") or [] for s in playable],
                          any(s.get("pending") for s in playable))

    def fetch_still_pages(self, from_current=False):
        args = {"ahead": self.show_ahead}
        current = self.scene_name if from_current else next((s["name"] for s in (self.state or {}).get("scenes", [])), None)
        if current:
            args["current"] = current
        return (yield self.call("deck_pages", args))

    def refresh_show_pages(self):
        self._presentation_init()
        source = self.player or (self.presenter.source if self.presenter and not self.presenter.is_animated else None)
        if not self.show_pending or source is None:
            self.show_pending = False
            return
        generation = self._presentation_generation
        workspace = self.workspace
        def refresh():
            try:
                deck = yield self.call("deck_pages", {"ahead": 0})
                if generation != self._presentation_generation or self.workspace is not workspace:
                    return
                if "slides" not in deck:
                    self.toast(deck.get("error") or "Could not prepare the slideshow", error=True)
                    return
                s = self.still_pages(deck)
                self.show_pending = s.pending
                self._set_still_scene_indices(source, s.scenes)
                source.update(s.slides, s.links, s.scenes, s.notes, s.build_notes)
            except Exception as exc:
                if generation == self._presentation_generation and self.workspace is workspace:
                    self.toast(str(exc), error=True)
        spawn(refresh())

    def _set_still_scene_indices(self, source, scenes):
        order = {item["name"]: i for i, item in enumerate((self.state or {}).get("scenes", []))}
        source.scene_indices = [order.get(name, i) for i, name in enumerate(scenes)]

    def play(self, from_current=False, auto=False, with_presenter=False):
        self._presentation_init()
        if self._show_open() or self._playback_build or not self._can_play(from_current):
            return
        self.close_animation_preview()
        self._present_requested = True
        self._presentation_generation += 1
        generation = self._presentation_generation
        workspace = self.workspace
        def prepare():
            try:
                if ((yield from self.finish_edits()) is False or self.workspace is not workspace or
                        not self._can_play(from_current)):
                    return
                previous_busy_text = self.busy_text
                self.busy_text = "Preparing presenter view…" if with_presenter else "Preparing slideshow…"
                self.busy += 1
                try:
                    deck = yield from self.fetch_still_pages(from_current)
                except Exception:
                    if self.workspace is workspace and not self._closing:
                        self.toast("Could not prepare the slideshow", error=True)
                    return
                finally:
                    cancelled = self._cancelling
                    self.busy_text = previous_busy_text
                    self.busy -= 1
                if cancelled:
                    self.toast("cancelled")
                    return
                if generation != self._presentation_generation or self.workspace is not workspace or not self._can_play(from_current):
                    return
                if "slides" not in deck:
                    self.toast(deck.get("error") or "Could not prepare the slideshow", error=True)
                    return
                extras, warning = yield run_blocking(_still_extras, workspace.work)
                if generation != self._presentation_generation or self.workspace is not workspace or not self._can_play(from_current):
                    return
                if warning:
                    self.toast(warning, error=True)
                s = self.still_pages(deck)
                broken = [item["scene"] for item in deck["slides"] if item.get("error")]
                if broken:
                    self.toast("Skipping slides that fail to render: " + ", ".join(broken), error=True)
                if not s.slides:
                    return
                here = self._show_screen()
                other = next((screen for screen in QApplication.screens() if screen is not here), None)
                source = SlidePlayer(other if with_presenter and other else here)
                source.scenes, source.notes, source.build_notes = s.scenes, s.notes, s.build_notes
                source.extra_links = extras
                self._set_still_scene_indices(source, s.scenes)
                source.play(s.slides, s.links, self.show_start(s.scenes) if from_current else 0,
                            visible=not with_presenter)
                if with_presenter:
                    p = PresenterController(source=source, presenter_screen=here, audience_screen=other)
                    self.presenter = p
                    self._configure_show(source, p)
                    if other is None:
                        self._show_display_hint()
                else:
                    self.player = source
                    self._configure_show(source)
                self.show_pending = s.pending
                if auto:
                    self.start_script_runner(source)
            except Exception as exc:
                if self.workspace is workspace and not self._closing:
                    self.toast(str(exc), error=True)
            finally:
                if generation == self._presentation_generation:
                    self._present_requested = False
        spawn(prepare())

    def show_start(self, shown):
        if self.scene_name in shown:
            return shown.index(self.scene_name)
        order = [s["name"] for s in (self.state or {}).get("scenes", [])]
        if self.scene_name in order:
            for name in order[order.index(self.scene_name):]:
                if name in shown:
                    return shown.index(name)
        return 0

    def return_from_show(self, show):
        self.window.show()
        if not 0 <= show.page_index < show.page_count:
            return
        page = show.presenter_page(show.page_index)
        if not page.scene or page.scene not in [s["name"] for s in (self.state or {}).get("scenes", [])]:
            return
        def restore():
            if page.scene != self.scene_name:
                yield from self.select_scene(page.scene)
            if self.scene_name == page.scene:
                self.go_to_stop(min(page.build, max(0, len((self.scene or {}).get("stops") or []) - 1)))
        spawn(restore())

    def play_current_slide_animations(self):
        self._presentation_init()
        if (self._show_open() or self._playback_build or self.preview_scene or
                not self._can_play(True) or self.source_draft is not None):
            return
        name = self.preview_scene = self.scene_name
        job = self._playback_build = ("animation_preview", self.workspace, self.rpc)
        def prepare():
            try:
                if ((yield from self.finish_edits()) is False or not self._can_play(True) or
                        self._playback_build is not job or self.workspace is not job[1] or
                        name != self.preview_scene or name != self.scene_name or self.source_draft is not None):
                    if self._playback_build is job:
                        self._finish_playback_build(abandon=True)
                    return
                self._animation_status(f"Preparing {name}’s animations…")
                reply = yield self.call("animation_preview", {"scene": name, "height": self.animation_height})
                if self._playback_build is job and not reply.get("ok"):
                    self._finish_playback_build(abandon=True)
                    self.toast(reply.get("error") or "Could not play this slide", error=True)
            except Exception:
                if self._playback_build is job:
                    self._finish_playback_build(abandon=True)
                    self.toast("Could not play this slide", error=True)
        spawn(prepare())

    def show_animation_preview(self, scene, clips):
        self.close_animation_preview()
        preview = AnimationPreviewView(scene, clips, self.stage_area)
        preview.on_close = self.close_animation_preview
        preview.on_error = lambda text: self.toast(text, error=True)
        self.preview_scene = scene
        preview.setGeometry(self.stage_area.rect())
        self.animation_preview = preview
        preview.show()
        preview.raise_()
        preview.video.setFocus()

    def close_animation_preview(self):
        preview = self.animation_preview
        self.preview_scene = None
        if preview:
            self.animation_preview = None
            preview.on_close = None
            preview.stop()
            preview.hide()
            preview.deleteLater()
            if self.window.isVisible() and not self._closing:
                self.canvas.setFocus()

    def handle_presentation_event(self, event):
        self._presentation_init()
        kind, phase = event.get("event"), event.get("phase")
        if kind == "rendered":
            if self.show_pending and not self.pending_show_refresh:
                self.pending_show_refresh = True
                def refresh():
                    self.pending_show_refresh = False
                    self.refresh_show_pages()
                QTimer.singleShot(300, refresh)
            return False
        if kind not in ("present", "animation_preview"):
            return False
        self._check_playback_build()
        job = self._playback_build
        preparing = job is not None and job[0] == kind
        source = self.animated_player or (self.presenter.source if self.presenter and self.presenter.is_animated else None)
        is_preview = kind == "animation_preview"
        if not preparing and (is_preview or source is None):
            return True
        if phase == "building":
            if (is_preview and self.preview_scene != event.get("scene")) or self._cancelling:
                return True
            done, total = int(event.get("done", 0)), int(event.get("total", 0))
            steps = f"{done + 1}/{total}"
            if is_preview:
                title = f"Building animations {steps}…"
            elif (only := event.get("onlyScene")):
                title = f"Building {only}’s animations {steps}"
            else:
                title = f"Building animations {steps}: {event.get('scene', '')}"
            self._animation_status(title, done / total if total > 0 else None)
        elif phase in ("error", "cancelled"):
            active = not is_preview or self.preview_scene is not None
            if preparing:
                self._finish_playback_build(abandon=True)
            else:
                self.update_busy_pill()
            if active:
                if phase == "cancelled":
                    self.toast("cancelled")
                else:
                    fallback = "Animation preview failed" if is_preview else "presentation failed"
                    self.toast(event.get("error") or fallback, error=True)
        elif is_preview and phase == "ready":
            name = event.get("scene")
            wanted = name and name == self.preview_scene == self.scene_name and not self._cancelling
            self._finish_playback_build(abandon=True)
            if wanted and self._can_play(True) and self.source_draft is None and not self._show_open():
                clips = event.get("clips") or []
                if clips:
                    self.show_animation_preview(name, clips)
                else:
                    self.toast("No playable animation clips", error=True)
        elif not is_preview and phase in ("ready", "update"):
            if source:
                source.update(event)
            elif phase == "ready" and preparing:
                self._finish_playback_build(abandon=not self._can_play() or self._cancelling)
                if self._present_requested:
                    self.start_animated(event)
        elif not is_preview and phase == "failed":
            failed = event.get("scene", "")
            if source:
                remaining = [s for s in source.deck.slides if s.scene != failed]
                numbers = {name: i for i, name in enumerate(dict.fromkeys(s.scene for s in remaining))}
                slides = [replace(s, number=numbers[s.scene]) for s in remaining]
                source.update(PresentDeck(slides, source.deck.sections, source.deck.links))
            self.toast(f"Skipping a slide that fails to build: {failed}", error=True)
        elif phase == "built" and not preparing:
            self.update_busy_pill()
        return True


class _NotesRecoveryDialog(QDialog):
    def __init__(self, scene, text):
        super().__init__()
        self.saved = False
        self.setWindowTitle(f"Unsaved presenter notes — {scene}")
        layout = QVBoxLayout(self)
        self.message = QLabel("Saving failed. Your draft is retained here; retry or copy it.")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.editor = QTextEdit()
        self.editor.setPlainText(text)
        layout.addWidget(self.editor)
        row = QHBoxLayout()
        self.retry = QPushButton("Retry Saving")
        row.addWidget(self.retry)
        discard = QPushButton("Close")
        discard.clicked.connect(self.reject)
        row.addWidget(discard)
        layout.addLayout(row)
        self.resize(600, 400)

    def _can_close(self):
        return self.saved or QMessageBox.warning(
            self, "Discard unsaved notes?", "These presenter notes have not been saved.",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Discard

    def reject(self):
        if self._can_close():
            super().reject()

    def closeEvent(self, event):
        if self._can_close():
            event.accept()
        else:
            event.ignore()
