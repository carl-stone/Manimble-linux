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
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import time
from typing import Protocol

from PySide6.QtCore import (QObject, QPointF, QRectF, Qt, QTimer, QUrl,
                            QVariantAnimation, Signal, QEvent)
from PySide6.QtGui import (QColor, QDesktopServices, QImage, QImageReader, QPainter,
                           QPen, QKeyEvent)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox,
                               QMainWindow, QPushButton, QScrollArea, QSlider,
                               QSplitter, QTextEdit, QVBoxLayout, QWidget)

from . import prefs
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
        self.blacked_out = False
        self.placeholder = ""
        self.counter = ""
        self.fraction = 0.0
        self.show_progress = False
        self.on_key = self.on_click = self.over_link = None
        self.script_pointer = None
        self.setMinimumSize(80, 45)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    @property
    def slide_rect(self) -> QRectF:
        aspect = self.image.width() / self.image.height() if not self.image.isNull() else 16 / 9
        w = min(self.width(), self.height() * aspect)
        h = w / aspect
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def point(self, pos) -> QPointF | None:
        r = self.slide_rect
        if not r.contains(pos) or not r.width() or not r.height():
            return None
        return QPointF((pos.x() - r.x()) / r.width(), (pos.y() - r.y()) / r.height())

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.GlobalColor.black)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if not self.blacked_out:
            if not self.previous_image.isNull() and self.fade < 1:
                p.drawImage(self.slide_rect, self.previous_image)
                p.setOpacity(self.fade)
            if not self.image.isNull():
                p.drawImage(self.slide_rect, self.image)
            p.setOpacity(1)
            if self.placeholder:
                p.setPen(QColor("#AAAAAA"))
                f = p.font(); f.setPointSize(22); p.setFont(f)
                p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.placeholder)
            pointer = self.script_pointer
            if pointer is not None:
                r = self.slide_rect
                pos = QPointF(r.x() + pointer.x() * r.width(), r.y() + pointer.y() * r.height())
                p.setPen(QPen(QColor("white"), 2)); p.setBrush(QColor("#EF4444"))
                p.drawEllipse(pos, 7, 7)
        if self.show_progress:
            p.fillRect(QRectF(0, self.height() - 3, self.width() * self.fraction, 3), QColor("#8C9EFF"))
            p.setPen(QColor("#DDDDDD"))
            p.drawText(self.rect().adjusted(12, 10, -18, -15),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop, self.counter)

    def mousePressEvent(self, e):
        if self.on_click:
            self.on_click(self.point(e.position()), e.button() != Qt.MouseButton.RightButton)

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
            QTimer.singleShot(0, lambda: self.move_to(QApplication.primaryScreen()))

    @property
    def script_pointer(self):
        return self.show_view.script_pointer

    @script_pointer.setter
    def script_pointer(self, p):
        self.show_view.script_pointer = p
        self.show_view.update()

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
        self._counter_until = 0.0
        self._chrome_timer = QTimer(self)
        self._chrome_timer.setInterval(100)
        self._chrome_timer.timeout.connect(self._chrome)
        self._chrome_timer.start()

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
        self.window.show_view.placeholder = text
        self.window.show_view.update()
        if self.on_error:
            self.on_error(text)

    def _changed(self):
        self._counter_until = time.monotonic() + 1.8
        self._chrome()
        if self.on_change:
            self.on_change()

    def _chrome(self):
        v = self.window.show_view
        v.blacked_out = self.blacked_out
        v.show_progress = self.show_progress
        v.fraction = (self.index + 1) / max(1, self.page_count)
        if self.page_count:
            p = self.presenter_page(self.index)
            v.counter = (f"{p.slide + 1} / {self.slide_count}" +
                         (f"  ·  {p.build + 1}/{p.builds}" if p.builds > 1 else "")) if time.monotonic() < self._counter_until else ""
        v.update()

    def _fade_value(self, value):
        self.window.show_view.fade = float(value)
        self.window.show_view.update()

    def _set_frame(self, image, fade=False):
        v = self.window.show_view
        self._fade.stop()
        v.previous_image = self.frame if fade else QImage()
        self.frame = image
        v.image = image
        v.placeholder = ""
        v.fade = 0.0 if fade else 1.0
        if fade:
            self._fade.setDuration(250)
            self._fade.setStartValue(0.0); self._fade.setEndValue(1.0); self._fade.start()
        v.update()
        self.frame_changed.emit(image)

    def _load_still(self, path, fade=False):
        self._loaded_still_path = None
        generation = self._generation
        if not path:
            self.window.show_view.placeholder = f"Rendering slide {self.presenter_page(self.index).slide + 1}…"
            self.window.show_view.update()
            return
        if path in self._cache:
            self._set_frame(self._cache[path], fade)
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
                self._set_frame(image, fade)
                self._loaded_still_path = path
                self._changed()
            except Exception as exc:
                if generation == self._generation:
                    self._error(str(exc))
        spawn(load())

    def click(self, point, primary=True):
        if not primary:
            self.previous(); return
        url = self.link(point) if point else None
        if url:
            QDesktopServices.openUrl(url); return
        if point and point.y() >= .94 and self.sections:
            self.jump(min(len(self.sections) - 1, int(point.x() * len(self.sections)))); return
        self.next()

    def handle_key(self, e):
        if e.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier):
            return False
        k, t = e.key(), e.text().lower()
        if k in (Qt.Key.Key_Right, Qt.Key.Key_Down, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_PageDown):
            self.next()
        elif k in (Qt.Key.Key_Left, Qt.Key.Key_Up, Qt.Key.Key_Backspace, Qt.Key.Key_PageUp):
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
            self.blacked_out = not self.blacked_out; self._changed()
        elif t == "p":
            self.show_progress = not self.show_progress; self._changed()
        elif t == "n":
            current = self.presenter_page(self.index).slide
            target = next((i for i in range(self.index + 1, self.page_count) if self.presenter_page(i).slide > current), None)
            if target is not None:
                self.go(target)
        elif t in "123456789" and t:
            self.jump(int(t) - 1)
        elif t == "r":
            self.replay()
        elif t == "v" and isinstance(self, AnimatedPlayer):
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

    def replay(self):
        self.go(self.index)

    def exit(self):
        if self._exited:
            return
        self._exited = True
        self._generation += 1
        self._chrome_timer.stop()
        self._fade.stop()
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
        self.sections = []; self.scene_indices = []; self.extra_links = {}

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
        self.show(False)

    def update(self, slides, links, scenes, notes, build_notes):
        old = self.presenter_page(self.index) if self.pages else None
        fresh = self._pages(slides, links)
        if not fresh:
            self.exit(); return
        self.pages = fresh
        self.scenes, self.notes, self.build_notes = scenes, notes, build_notes
        self.reload_token += 1
        si = scenes.index(old.scene) if old and old.scene in scenes else min(old.slide if old else 0, len(slides) - 1)
        self.index = next((i for i, p in enumerate(fresh) if p.slide == si and old and p.build == old.build),
                          next((i for i, p in enumerate(fresh) if p.slide >= si), len(fresh) - 1))
        self.show(False)

    def show(self, animated=True):
        self._generation += 1
        self._load_still(self.pages[self.index].path, animated)
        self._changed()

    def go(self, i):
        if 0 <= i < len(self.pages):
            self.index = i; self.show(True)

    def next(self):
        self.exit() if self.index + 1 >= len(self.pages) else self.go(self.index + 1)

    def previous(self):
        self.go(self.index - 1)

    def jump(self, section):
        if not 0 <= section < len(self.sections):
            return
        target = self.sections[section]
        si = next((i for i, original in enumerate(self.scene_indices) if original >= target), None)
        if si is not None:
            self.go(next((i for i, p in enumerate(self.pages) if p.slide == si), self.index))

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
    def __init__(self, screen=None):
        super().__init__(screen)
        self.deck = PresentDeck()
        self.media_player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.media_player.setAudioOutput(self.audio_output)
        self.video_sink = None
        self.finished = False
        self.mode = "play"
        self._first_frame = False
        self._end_seek = False
        self.media_player.mediaStatusChanged.connect(self._status)
        self.media_player.errorOccurred.connect(lambda error, text: self._error(text))
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

    def play(self, deck, start_scene=0, screen=None, visible=True, own_space=False):
        self.deck = PresentDeck.from_record(deck)
        if not self.page_count:
            return
        if visible:
            self.window.present(screen, own_space)
        self.show(next((i for i, s in enumerate(self.deck.slides) if s.scene_index >= start_scene), 0), "play")

    def update(self, deck):
        old = self.current_slide
        self.deck = PresentDeck.from_record(deck)
        self.reload_token += 1
        if not self.page_count:
            self.exit(); return
        i = next((i for i, s in enumerate(self.deck.slides) if old and s.scene == old.scene and s.index == old.index),
                 next((i for i, s in enumerate(self.deck.slides) if old and s.number >= old.number), self.page_count - 1))
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
        self._end_seek = mode == "end"
        self.media_player.stop()
        self.media_player.setSource(QUrl())
        s = self.current_slide
        self.window.show_view.placeholder = f"Building slide {s.number + 1}…" if s.pending and not s.file else ""
        if s.kind == "image":
            self._load_still(s.file)
            self.finished = not s.pending
            self._changed(); return
        path = s.rev_file if mode == "reverse" else s.file
        if mode == "reverse" and not path:
            self.finished = True
            self._error("This build has no reverse clip")
            self._changed(); return
        if not path:
            self.finished = not s.pending
            self._changed(); return
        if self.video_sink:
            self.media_player.setVideoSink(None)
            self.video_sink.deleteLater()
        sink = QVideoSink(self)
        self.video_sink = sink
        sink.videoFrameChanged.connect(lambda f, g=generation: self._video_frame(f, g))
        self.media_player.setVideoSink(sink)
        rate = s.rev_rate if mode == "reverse" else s.rate
        self.media_player.setPlaybackRate(rate if rate > 0 else 1)
        self.media_player.setSource(QUrl.fromLocalFile(path))
        self.media_player.play()
        self._changed()

    def _video_frame(self, frame, generation):
        if generation != self._generation or self._exited or not frame.isValid():
            return
        if self._end_seek:
            duration = self.media_player.duration()
            if not duration or self.media_player.position() < duration - 90:
                return
            self._end_seek = False
            self.media_player.pause()
            self.finished = True
        image = frame.toImage()
        if image.isNull():
            return
        self._first_frame = True
        self._set_frame(_mapped(image))
        if self.finished:
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
        i = next((i for i, s in enumerate(self.deck.slides) if s.scene_index >= self.sections[section]), None)
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
    def set_frame(self, image):
        self.image = image
        self.update()


class SlideView(LiveVideoView):
    pass


class NotesTextView(QTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.on_focus = self.on_blur = self.on_images = None
        self.shows_hint = False
        self.setAcceptRichText(False)
        self.setPlaceholderText("No notes. Click here to type.")
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

    @property
    def notes_text(self):
        from .note_images import NoteImages
        return NoteImages.markup(self.document())

    def focusInEvent(self, e):
        super().focusInEvent(e)
        if self.on_focus:
            self.on_focus()

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
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
        self.on_key = self.on_close = None
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, obj, e):
        if e.type() == QEvent.Type.KeyPress and isinstance(obj, QWidget) and obj.window() is self:
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
    def __init__(self, number, parent=None):
        super().__init__(parent)
        self.image = SlideView(self)
        self.number = number
        self.on_click = None
        self.selected = False
        self.image.on_click = lambda p, primary: self.on_click() if self.on_click else None
        self.setFixedHeight(110)
        self.setMinimumWidth(140)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def resizeEvent(self, e):
        self.image.setGeometry(self.rect().adjusted(3, 3, -3, -3))
        super().resizeEvent(e)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setPen(QPen(QColor("#8C9EFF" if self.selected else "#444444"), 3 if self.selected else 1))
        p.drawRect(self.rect().adjusted(1, 1, -2, -2))
        p.setPen(QColor("white"))
        p.drawText(8, 16, str(self.number))


class SlideThumb(QWidget):
    def __init__(self, number, builds, parent=None):
        super().__init__(parent)
        self.on_click = None
        self.has_feedback = False
        self.selected = False
        layout = QVBoxLayout(self)
        self.label = QLabel(f"Slide {number}")
        layout.addWidget(self.label)
        self.main = BuildThumb(number)
        self.image = self.main.image
        self.main.on_click = lambda: self.on_click() if self.on_click else None
        layout.addWidget(self.main)
        self.builds = [BuildThumb(i + 1) for i in range(builds)] if builds > 1 else []
        for thumb in self.builds:
            row = QHBoxLayout()
            row.addSpacing(18); row.addWidget(thumb)
            layout.addLayout(row)


class PresenterController(QObject):
    def __init__(self, *, source=None, slides=None, links=(), scenes=(), notes=(),
                 build_notes=(), start_slide=0, deck=None, start_scene=0,
                 presenter_screen=None, audience_screen=None):
        super().__init__()
        self.presenter_screen = presenter_screen or QApplication.primaryScreen()
        self.audience_screen = audience_screen or self.presenter_screen
        self.shared_screen = audience_screen is None
        self.source = source or (AnimatedPlayer(self.audience_screen) if deck is not None
                                 else SlidePlayer(self.audience_screen))
        self.is_animated = isinstance(self.source, AnimatedPlayer)
        self.on_exit = self.on_gone = self.on_detach = self.on_save_notes = None
        self.finished = False
        self.feedback_mode = False
        self._feedback = None
        self._note_image_base = None
        self._editing = {}
        self._shown = {}
        self._thumbnail_cache = OrderedDict()
        self._pending_frames = set()
        self._sidebar_token = -1
        self._slide_range = []
        self.thumbs = []
        self.started = time.monotonic()
        self.accumulated = 0.0
        self.paused = False
        self.window = PresenterWindow()
        self.window.setWindowTitle("Manimble — Presenter View" + (" with Animations" if self.is_animated else ""))
        self.window.setMinimumSize(820, 560)
        self.window.on_key = self.key
        self.window.on_close = self.end_show
        self.window.setStyleSheet("QMainWindow, QWidget { background: #17191e; color: #eeeeee; }"
                                  "QPushButton, QComboBox { padding: 5px; background: #30343d; }"
                                  "QTextEdit { background: #24272e; font-size: 18px; }")
        self._build_content()
        self.source.on_change = self.refresh
        self.source.on_exit = self.finish
        self.source.frame_changed.connect(self.current.set_frame)
        self.current.image = self.source.frame
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
        self.window.setScreen(self.presenter_screen)
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

    def _button(self, text, action, layout):
        button = QPushButton(text)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(action)
        layout.addWidget(button)
        return button

    def _build_content(self):
        from .feedback import FeedbackLayer
        root = QWidget()
        self.window.setCentralWidget(root)
        layout = QVBoxLayout(root)
        tools = QHBoxLayout()
        self._button("End Show", self.end_show, tools)
        self._button("Swap Displays", self.swap_displays, tools)
        self._button("Use Slide Show", self.use_slide_show, tools)
        self.slide_notes_button = self._button("Slide Notes", self.toggle_slide_notes, tools)
        self.build_notes_button = self._button("Build Notes", self.toggle_build_notes, tools)
        self.feedback_button = self._button("Feedback Mode", self.toggle_feedback_mode, tools)
        self.feedback_list_button = self._button("Feedback List", self.show_feedback_list, tools)
        self.feedback_button.setEnabled(False)
        self.feedback_list_button.setEnabled(False)
        layout.addLayout(tools)
        timers = QHBoxLayout()
        self.elapsed = QLabel()
        self.clock = QLabel()
        self.elapsed.setStyleSheet("font-size: 25px")
        self.clock.setStyleSheet("font-size: 25px")
        timers.addWidget(self.elapsed)
        self.pause_button = self._button("Pause Timer", self.toggle_pause, timers)
        self._button("Reset Timer (R)", self.reset_timer, timers)
        timers.addStretch()
        timers.addWidget(QLabel("Audience:"))
        self.screen_choice = QComboBox()
        self.screen_choice.activated.connect(self.choose_screen)
        timers.addWidget(self.screen_choice)
        timers.addWidget(self.clock)
        layout.addLayout(timers)
        split = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(split, 1)
        self.sidebar = QScrollArea()
        self.sidebar.setWidgetResizable(True)
        self.sidebar.setMinimumWidth(160)
        split.addWidget(self.sidebar)
        main = QWidget()
        column = QVBoxLayout(main)
        images = QSplitter(Qt.Orientation.Horizontal)
        self.current = SlideView()
        self.current.on_click = self._current_click
        self.current.over_link = lambda p: bool(p and self.source.link(p))
        images.addWidget(self.current)
        self.feedback_layer = FeedbackLayer(parent=self.current)
        self.feedback_layer.slide_rect = lambda: self.current.slide_rect
        self.feedback_layer.on_add = self.mark_feedback
        self.current.installEventFilter(self)
        next_column = QWidget()
        nxt = QVBoxLayout(next_column)
        nxt.addWidget(QLabel("NEXT BUILD"))
        self.next_view = SlideView()
        self.next_view.on_click = lambda p, primary: self.source.next()
        nxt.addWidget(self.next_view, 1)
        images.addWidget(next_column)
        images.setSizes([600, 230])
        column.addWidget(images, 4)
        self.badge = QLabel()
        column.addWidget(self.badge)
        self.notes_row = QWidget()
        row = QHBoxLayout(self.notes_row)
        self.notes = NotesTextView()
        self.build_notes = NotesTextView()
        self.notes_columns = []
        for title, editor, is_build in (("SLIDE NOTES", self.notes, False), ("BUILD NOTES", self.build_notes, True)):
            panel = QWidget()
            box = QVBoxLayout(panel)
            box.setContentsMargins(0, 0, 0, 0)
            box.addWidget(QLabel(title)); box.addWidget(editor)
            editor.on_focus = lambda tv=editor: self.begin_notes(tv)
            editor.on_blur = lambda tv=editor, b=is_build: self.end_notes(tv, b)
            editor.on_images = lambda mime, tv=editor: self._paste_images(mime, tv)
            row.addWidget(panel)
            self.notes_columns.append(panel)
        column.addWidget(self.notes_row, 2)
        controls = QHBoxLayout()
        self._button("‹ Previous", self.go_back, controls)
        self.counter = QLabel()
        controls.addWidget(self.counter, 1)
        self._button("Next ›", self.advance, controls)
        column.addLayout(controls)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #ff8787")
        column.addWidget(self.error_label)
        column.addWidget(QLabel("←/→ build · Space pause/next · B blank · N slide · F feedback · Esc end · click notes to edit"))
        split.addWidget(main)
        split.setSizes([210, 850])
        self.apply_notes_visibility()

    def eventFilter(self, obj, e):
        if obj is self.current and e.type() == QEvent.Type.Resize:
            self.feedback_layer.setGeometry(self.current.rect())
            self.feedback_layer.relayout()
        return super().eventFilter(obj, e)

    def _place_presenter(self):
        r = self.presenter_screen.availableGeometry()
        if self.shared_screen:
            self.window.resize(max(820, int(r.width() * .75)), max(560, int(r.height() * .75)))
            self.window.move(r.center() - self.window.rect().center())
            self.window.showNormal()
        else:
            self.window.setGeometry(r)
            self.window.showFullScreen()

    def _screens_changed(self, *_):
        self.screen_choice.blockSignals(True)
        self.screen_choice.clear()
        for screen in QApplication.screens():
            self.screen_choice.addItem(screen.name(), screen)
        index = self.screen_choice.findData(self.audience_screen)
        self.screen_choice.setCurrentIndex(max(0, index))
        self.screen_choice.blockSignals(False)

    def _screen_removed(self, screen):
        if self.finished:
            return
        fallback = QApplication.primaryScreen()
        if screen is self.presenter_screen:
            self.presenter_screen = fallback
            self.window.setScreen(fallback)
        if screen is self.audience_screen:
            self.audience_screen = fallback
        self.shared_screen = self.presenter_screen is self.audience_screen
        self.source.window.windowed_presentation = self.shared_screen
        self.source.window.move_to(self.audience_screen)
        self._place_presenter()
        self._screens_changed()

    def choose_screen(self, index):
        screen = self.screen_choice.itemData(index)
        if screen:
            self.audience_screen = screen
            self.shared_screen = screen is self.presenter_screen
            self.source.window.windowed_presentation = self.shared_screen
            self.source.window.move_to(screen)
            self._place_presenter()

    def swap_displays(self):
        if self.presenter_screen is self.audience_screen:
            self._show_error("Swap Displays needs a second display.")
            return
        self.presenter_screen, self.audience_screen = self.audience_screen, self.presenter_screen
        self.source.window.move_to(self.audience_screen)
        self.window.showNormal()
        self.window.setScreen(self.presenter_screen)
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
        self._feedback = value
        self.feedback_layer.store = value
        self.feedback_button.setEnabled(value is not None)
        self.feedback_list_button.setEnabled(value is not None)
        self.feedback_changed()

    def _show_error(self, text):
        self.error_label.setText(text)

    def _current_click(self, point, primary):
        self.source.click(point, primary)

    def _paste_images(self, mime, tv):
        if not self.note_image_base:
            return
        from .note_images import NoteImages
        def paste():
            try:
                paths = yield from NoteImages.import_images(mime, self.note_image_base)
                if paths:
                    NoteImages.insert(paths, tv, self.note_image_base, min(480, tv.viewport().width() - 12))
                else:
                    QTextEdit.insertFromMimeData(tv, mime)
            except Exception as exc:
                self._show_error(str(exc))
        spawn(paste())

    def _show_notes(self, tv, text):
        if tv.hasFocus() or tv in self._editing or self._shown.get(tv) == text:
            return
        from .note_images import NoteImages
        document = NoteImages.attributed(text, self.note_image_base,
                                          max_width=min(480, max(120, tv.viewport().width() - 12)))
        document.setParent(tv)
        tv.setDocument(document)
        self._shown[tv] = text

    def begin_notes(self, tv):
        if not self.finished and self.source.page_count:
            self._editing[tv] = self.source.presenter_page(self.source.page_index)

    def end_notes(self, tv, is_build=False):
        page = self._editing.pop(tv, None)
        if page is None:
            return
        text = tv.notes_text
        old = page.build_notes if is_build else page.notes
        if text == old or not page.scene or not self.on_save_notes:
            return
        def saved(ok):
            if not ok:
                self._show_error(f"Could not save {page.scene}'s notes.")
                self._recover_notes(page, text, is_build)
                return
            i = next((i for i in range(self.source.page_count)
                      if self.source.presenter_page(i).scene == page.scene and
                      self.source.presenter_page(i).build == page.build), None)
            if i is not None:
                self.source.set_notes(text, i, is_build)
            self._shown[tv] = text
        self.on_save_notes(page.scene, page.build if is_build else None, text, saved)

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
        if key in self._pending_frames:
            return
        self._pending_frames.add(key)
        def load():
            try:
                image = yield run_blocking(_thumbnail, path, bool(page.clip))
                image = _mapped(image)
                self._thumbnail_cache[key] = image
                while len(self._thumbnail_cache) > 400:
                    self._thumbnail_cache.popitem(last=False)
                if not self.finished:
                    callback(image)
            except Exception as exc:
                if not self.finished:
                    self._show_error(str(exc))
            finally:
                self._pending_frames.discard(key)
                if not self.finished and key in self._thumbnail_cache:
                    QTimer.singleShot(0, self.refresh)
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
            column = QVBoxLayout(holder)
            self.thumbs = []
            for k, (first, last) in enumerate(ranges):
                thumb = SlideThumb(k + 1, last - first + 1)
                thumb.on_click = lambda i=first: self.source.go(i)
                for b, panel in enumerate(thumb.builds):
                    panel.on_click = lambda i=first + b: self.source.go(i)
                column.addWidget(thumb)
                self.thumbs.append(thumb)
            column.addStretch()
            self.sidebar.setWidget(holder)
            self.sidebar.verticalScrollBar().setValue(scroll)
        for thumb, (first, last) in zip(self.thumbs, ranges):
            for i in range(first, last + 1):
                def assign(image, t=thumb, idx=i, f=first, l=last):
                    if idx == l:
                        t.image.set_frame(image)
                    if t.builds:
                        t.builds[idx - f].image.set_frame(image)
                self._request_picture(self.source.presenter_page(i), assign)

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
        # Blanking is audience-only; presenter retains a dimmed live preview.
        self.current.image = self.source.frame
        self.current.update()
        self.badge.setText(("SCREEN BLANKED  ·  " if self.source.is_blacked_out else "") +
                           (self.source.playback_state or ""))
        self.counter.setText(f"Slide {page.slide + 1} of {self.source.slide_count}  ·  "
                             f"build {page.build + 1}/{page.builds}")
        self._show_notes(self.notes, page.notes)
        self._show_notes(self.build_notes, page.build_notes)
        nxt = self.source.page_index + 1
        if nxt < self.source.page_count:
            def assign(image, index=nxt):
                if self.source.page_index + 1 == index:
                    self.next_view.set_frame(image)
            self._request_picture(self.source.presenter_page(nxt), assign)
        else:
            self.next_view.set_frame(QImage())
        self.feedback_layer.scene = page.scene
        self.feedback_layer.build = page.build
        self.feedback_layer.setGeometry(self.current.rect())
        self.feedback_layer.relayout()
        for thumb, (first, last) in zip(self.thumbs, self._slide_range):
            selected = first <= self.source.page_index <= last
            thumb.selected = thumb.main.selected = selected
            thumb.main.update()
            for i, panel in enumerate(thumb.builds):
                panel.selected = selected and first + i == self.source.page_index
                panel.update()
        self.mark_feedback()

    def update_stills(self, slides, links, scenes, notes, build_notes):
        if isinstance(self.source, SlidePlayer):
            self.source.update(slides, links, scenes, notes, build_notes)

    def update_animated(self, deck):
        if isinstance(self.source, AnimatedPlayer):
            self.source.update(deck)

    @property
    def live_frame_ready(self):
        return not self.current.image.isNull()

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
        if not self.feedback:
            return
        for thumb, (first, _) in zip(self.thumbs, self._slide_range):
            scene = self.source.presenter_page(first).scene
            thumb.has_feedback = bool(scene and self.feedback.notes_for(scene))
            thumb.label.setText(f"Slide {self.source.presenter_page(first).slide + 1}" +
                                ("  ●" if thumb.has_feedback else ""))

    def feedback_changed(self):
        self.feedback_layer.rebuild()
        self.mark_feedback()

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
        self.feedback_button.setText("Feedback On" if self.feedback_mode else "Feedback Mode")

    def show_feedback_list(self):
        if self.feedback is None:
            return
        from .feedback import FeedbackListView
        dialog = QDialog(self.window)
        dialog.setWindowTitle("Presentation Feedback")
        layout = QVBoxLayout(dialog)
        view = FeedbackListView(self.feedback)
        names = {self.source.presenter_page(i).scene: self.source.presenter_page(i).slide + 1
                 for i in range(self.source.page_count)}
        view.slide_label = lambda scene: f"Slide {names[scene]}" if scene in names else f"Missing slide: {scene}"
        def go(note):
            scene = note.scene if hasattr(note, "scene") else note.get("scene")
            i = next((i for i in range(self.source.page_count)
                      if self.source.presenter_page(i).scene == scene), None)
            if i is not None:
                self.source.go(i)
        view.on_go = go
        view.on_close = dialog.close
        view.reload()
        layout.addWidget(view)
        dialog.resize(540, 500)
        dialog.show()
        self._feedback_dialog = dialog

    def toggle_slide_notes(self):
        prefs.set("presenterHideSlideNotes", not prefs.get_bool("presenterHideSlideNotes"))
        self.apply_notes_visibility()

    def toggle_build_notes(self):
        prefs.set("presenterHideBuildNotes", not prefs.get_bool("presenterHideBuildNotes"))
        self.apply_notes_visibility()

    def apply_notes_visibility(self):
        hidden = [prefs.get_bool("presenterHideSlideNotes"), prefs.get_bool("presenterHideBuildNotes")]
        for panel, tv, hide in zip(self.notes_columns, (self.notes, self.build_notes), hidden):
            if hide:
                tv.clearFocus()
            panel.setVisible(not hide)
        self.notes_row.setVisible(not all(hidden))

    def tick(self):
        elapsed = int(self.accumulated + (0 if self.paused else time.monotonic() - self.started))
        self.elapsed.setText(f"{elapsed // 3600}:{elapsed // 60 % 60:02}:{elapsed % 60:02}" if elapsed >= 3600
                             else f"{elapsed // 60:02}:{elapsed % 60:02}")
        self.clock.setText(datetime.now().strftime("%H:%M"))

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
        self.pause_button.setText("Resume Timer" if self.paused else "Pause Timer")
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
        self._end_editing()
        self.finished = True
        self.timer.stop()
        self.window.on_close = None
        self.window.hide()
        self.source.on_change = None
        self.source.frame_changed.disconnect(self.current.set_frame)
        self.source.window.windowed_presentation = False
        self.source.window.present(self.audience_screen)
        if self.on_detach:
            self.on_detach(self.source)

    def finish(self):
        if self.finished:
            return
        self._end_editing()
        self.finished = True
        self.timer.stop()
        self.window.on_close = None
        self.window.hide()
        if self.on_exit:
            self.on_exit()
        if self.on_gone:
            self.on_gone()


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


class PreviewClickArea(ShowView):
    pass


class AnimationPreviewView(QWidget):
    def __init__(self, scene, clips, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{scene} — Animation Preview")
        self.on_close = self.on_error = None
        self.clips = [AnimationClip.from_record(c) for c in clips]
        self.player = AnimatedPlayer()
        self.player.show_progress = False
        self.player.on_error = self._error
        self.player.on_change = self._changed
        self._advance = QTimer(self)
        self._advance.setSingleShot(True)
        self._advance.timeout.connect(self.next_build)
        self._ended_generation = -1
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(QLabel(scene), 1)
        close = QPushButton("Close")
        close.clicked.connect(self.close_preview)
        header.addWidget(close)
        layout.addLayout(header)
        self.video = self.player.window.show_view
        self.video.setParent(self)
        self.video.on_click = lambda p, primary: self.next_build() if primary else self.previous_build()
        self.video.on_key = self._key
        self.video.show_progress = False
        layout.addWidget(self.video, 1)
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderMoved.connect(self.player.seek)
        layout.addWidget(self.seek_slider)
        self.player.media_player.durationChanged.connect(lambda n: self.seek_slider.setRange(0, int(n)))
        self.player.media_player.positionChanged.connect(self._position)
        footer = QHBoxLayout()
        self.previous = QPushButton("Previous build")
        self.next = QPushButton("Next build")
        self.replay = QPushButton("Replay")
        self.pause = QPushButton("Pause / Resume")
        self.counter = QLabel()
        for button, action in ((self.previous, self.previous_build), (self.next, self.next_build),
                               (self.replay, self.replay_build), (self.pause, self.toggle_pause)):
            button.clicked.connect(action)
            footer.addWidget(button)
        footer.addWidget(self.counter)
        layout.addLayout(footer)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #ff8787")
        layout.addWidget(self.error_label)
        self.resize(900, 600)
        deck = PresentDeck([PresentSlide(scene, index=i, count=len(self.clips), kind=c.kind,
                                        file=str(c.url), rate=c.playback_rate)
                            for i, c in enumerate(self.clips)])
        self.player.play(deck, visible=False)
        self._changed()

    @property
    def index(self):
        return self.player.index

    def _position(self, n):
        if not self.seek_slider.isSliderDown():
            self.seek_slider.setValue(int(n))

    def _changed(self):
        if not self.clips:
            return
        self.counter.setText(f"Build {self.index + 1} of {len(self.clips)}" +
                             (" · End" if self.player.finished and self.index + 1 == len(self.clips) else ""))
        self.previous.setEnabled(self.index > 0)
        self.next.setEnabled(self.index + 1 < len(self.clips))
        self.video.show_progress = False
        if self.player.finished and self.index + 1 < len(self.clips) and self._ended_generation != self.player._generation:
            self._ended_generation = self.player._generation
            self._advance.start(1000 if self.clips[self.index].kind == "image" else 400)

    def _error(self, text):
        self.error_label.setText(text)
        if self.on_error:
            self.on_error(text)

    def play_build(self, index):
        self._advance.stop()
        if 0 <= index < len(self.clips):
            self.player.go(index)

    def previous_build(self):
        self.play_build(self.index - 1)

    def next_build(self):
        self.play_build(self.index + 1)

    def replay_build(self):
        self.play_build(self.index)

    def toggle_pause(self):
        self._advance.stop()
        if self.player.finished:
            self.player.replay()
        else:
            self.player.play_pause()

    def _key(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.close_preview()
        elif e.key() == Qt.Key.Key_Left:
            self.previous_build()
        elif e.key() == Qt.Key.Key_Right:
            self.next_build()
        elif e.key() == Qt.Key.Key_Space:
            self.toggle_pause()
        else:
            return False
        return True

    def keyPressEvent(self, e):
        if not self._key(e):
            super().keyPressEvent(e)

    def stop(self):
        self._advance.stop()
        self.player.on_change = None
        self.player.exit()

    def close_preview(self):
        self.stop()
        if self.on_close:
            self.on_close()
        else:
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
    """Read chapter metadata without importing executable presentation code."""
    if deck is None or not (deck / "present.json").is_file():
        return {}, None
    try:
        data = json.loads((deck / "present.json").read_text())
        return {"sections": [int(i) for i in data.get("sections", [])],
                "links": dict(data.get("links", {}))}, None
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
        self._script_auto_pending = False

    def _presentation_init(self):
        if not hasattr(self, "_presentation_generation"):
            self.init_presentation()

    @property
    def animation_height(self):
        h = prefs.get_int("animationHeight", 720)
        return h if h in (720, 1080, 1440, 2160) else 720

    def animation_resolution_chosen(self, value):
        h = value.data() if hasattr(value, "data") else value
        h = int(h)
        if h not in (720, 1080, 1440, 2160):
            raise ValueError("Animation resolution must be 720, 1080, 1440 or 2160")
        prefs.set("animationHeight", h)
        self.toast(f"Animations build at {'4K' if h == 2160 else str(h) + 'p'} from the next show")

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
        return bool(self.player or self.animated_player or self.presenter or self._present_requested)

    def _show_screen(self):
        return self.window.screen() or QApplication.primaryScreen()

    def present(self, from_current=False, with_presenter=False, auto=False):
        self._presentation_init()
        if self._show_open():
            return
        self.close_animation_preview()
        self._present_requested = True
        self.present_with_presenter = with_presenter
        self._script_auto_pending = auto
        order = [s["name"] for s in (self.state or {}).get("scenes", [])]
        self.present_from = order.index(self.scene_name) if from_current and self.scene_name in order else 0
        self._presentation_generation += 1
        generation = self._presentation_generation
        def prepare():
            try:
                if (yield from self.finish_edits()) is False:
                    self._present_requested = False
                    return
                self.busy_text = "Preparing presentation…"
                args = {"height": self.animation_height, "ahead": self.show_ahead}
                if from_current and self.scene_name:
                    args["current"] = self.scene_name
                reply = yield self.call("present", args)
                if generation != self._presentation_generation:
                    return
                if reply.get("ok") is False:
                    raise RuntimeError(reply.get("error") or "Could not start the presentation")
            except Exception as exc:
                if generation == self._presentation_generation:
                    self._present_requested = False
                    self._script_auto_pending = False
                    self.toast(str(exc), error=True)
        spawn(prepare())

    def _configure_show(self, source, presenter=None):
        source.on_error = lambda text: self.toast(text, error=True)
        if presenter:
            source.on_error = lambda text: (presenter._show_error(text), self.toast(text, error=True))
        def returned():
            self._presentation_generation += 1
            self._present_requested = False
            self.show_pending = False
            self.player = self.animated_player = self.presenter = None
            self.return_from_show(source)
        if presenter:
            presenter.on_exit = returned
            presenter.on_gone = lambda: self.window.show()
            presenter.on_save_notes = self.save_presenter_notes
            presenter.note_image_base = self.work_deck
            presenter.feedback = self.feedback
            def detached(src):
                self.presenter = None
                if isinstance(src, AnimatedPlayer):
                    self.animated_player = src
                else:
                    self.player = src
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
                self.toast("One display: audience and presenter use separate windows.")
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
        if from_current and self.scene_name:
            args["current"] = self.scene_name
        return (yield self.call("deck_pages", args))

    def refresh_show_pages(self):
        self._presentation_init()
        if not self.show_pending or not (self.player or self.presenter):
            self.show_pending = False
            return
        generation = self._presentation_generation
        def refresh():
            try:
                deck = yield self.call("deck_pages", {"ahead": 0})
                if generation != self._presentation_generation:
                    return
                s = self.still_pages(deck)
                self.show_pending = s.pending
                if self.player:
                    self.player.update(s.slides, s.links, s.scenes, s.notes, s.build_notes)
                if self.presenter:
                    self.presenter.update_stills(s.slides, s.links, s.scenes, s.notes, s.build_notes)
                for item in deck.get("slides", []):
                    if item.get("error"):
                        self.toast(f"Skipping {item['scene']}: {item['error']}", error=True)
            except Exception as exc:
                self.toast(str(exc), error=True)
        spawn(refresh())

    def play(self, from_current=False, auto=False, with_presenter=False):
        self._presentation_init()
        if self._show_open():
            return
        self.close_animation_preview()
        self._present_requested = True
        self._presentation_generation += 1
        generation = self._presentation_generation
        def prepare():
            try:
                if (yield from self.finish_edits()) is False:
                    return
                deck = yield from self.fetch_still_pages(from_current)
                extras, warning = yield run_blocking(_still_extras, self.work_deck)
                if generation != self._presentation_generation:
                    return
                if warning:
                    self.toast(warning, error=True)
                s = self.still_pages(deck)
                broken = [item for item in deck.get("slides", []) if item.get("error")]
                for item in broken:
                    self.toast(f"Skipping {item['scene']}: {item['error']}", error=True)
                if not s.slides:
                    self.toast("No slides could be rendered", error=True)
                    return
                here = self._show_screen()
                other = next((screen for screen in QApplication.screens() if screen is not here), None)
                source = SlidePlayer(other if with_presenter and other else here)
                source.scenes, source.notes, source.build_notes = s.scenes, s.notes, s.build_notes
                source.sections = extras.get("sections", [])
                source.extra_links = extras.get("links", {})
                order = [item["name"] for item in (self.state or {}).get("scenes", [])]
                source.scene_indices = [order.index(n) if n in order else i for i, n in enumerate(s.scenes)]
                source.play(s.slides, s.links, self.show_start(s.scenes) if from_current else 0,
                            visible=not with_presenter)
                if with_presenter:
                    p = PresenterController(source=source, presenter_screen=here, audience_screen=other)
                    self.presenter = p
                    self._configure_show(source, p)
                    if other is None:
                        self.toast("One display: audience and presenter use separate windows.")
                else:
                    self.player = source
                    self._configure_show(source)
                self.show_pending = s.pending
                if auto:
                    self.start_script_runner(source)
            except Exception as exc:
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
        if not self.scene_name or self.source_draft is not None:
            return
        self.close_animation_preview()
        self.preview_scene = self.scene_name
        name = self.preview_scene
        def prepare():
            try:
                if (yield from self.finish_edits()) is False:
                    self.preview_scene = None
                    return
                reply = yield self.call("animation_preview", {"scene": name, "height": self.animation_height})
                if reply.get("ok") is False:
                    raise RuntimeError(reply.get("error") or "Could not prepare animation preview")
            except Exception as exc:
                self.preview_scene = None
                self.toast(str(exc), error=True)
        spawn(prepare())

    def show_animation_preview(self, scene, clips):
        self.close_animation_preview()
        preview = AnimationPreviewView(scene, clips)
        preview.on_close = self.close_animation_preview
        preview.on_error = lambda text: self.toast(text, error=True)
        # A separate nonmodal tool window leaves the editor and its drafts intact.
        preview.setParent(self.window, Qt.WindowType.Window)
        self.animation_preview = preview
        preview.show()
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
        if phase == "building":
            self.busy_text = (f"Building animations {int(event.get('done', 0)) + 1}/"
                              f"{event.get('total', 0)}: {event.get('scene', '')}")
            self.toast(self.busy_text)
        elif phase in ("error", "cancelled"):
            if kind == "present":
                self._present_requested = False
                self._script_auto_pending = False
            else:
                self.preview_scene = None
            self.toast(event.get("error") or "Cancelled", error=phase == "error")
        elif kind == "animation_preview" and phase == "ready":
            name = event.get("scene")
            if name == self.preview_scene and name == self.scene_name:
                clips = event.get("clips") or []
                if clips:
                    self.show_animation_preview(name, clips)
                else:
                    self.toast("No playable animation clips", error=True)
        elif kind == "present" and phase in ("ready", "update"):
            deck = PresentDeck.from_record(event)
            if self.animated_player:
                self.animated_player.update(deck)
            elif self.presenter and self.presenter.is_animated:
                self.presenter.update_animated(deck)
            elif phase == "ready" and self._present_requested:
                self.start_animated(deck)
        elif phase == "failed":
            source = self.animated_player or (self.presenter.source if self.presenter and self.presenter.is_animated else None)
            if source:
                failed_scene = event.get("scene")
                remaining = [s for s in source.deck.slides if s.scene != failed_scene]
                numbers = {name: i for i, name in enumerate(dict.fromkeys(s.scene for s in remaining))}
                for slide in remaining:
                    slide.number = numbers[slide.scene]
                source.update(PresentDeck(remaining, source.deck.sections, source.deck.links))
            self.toast(f"Skipping {event.get('scene', '')}: {event.get('error', 'build failed')}", error=True)
        elif phase == "built":
            self.busy_text = "Rendering…"
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
