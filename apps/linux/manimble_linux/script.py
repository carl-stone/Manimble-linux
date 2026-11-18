"""Portable script.json, deterministic motion, and the editor/show script scheduler.

The data and motion portion deliberately has no Qt imports. Widgets are loaded
lazily; movie workers and portable-project tests can use the same mathematics.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from enum import Enum
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Protocol
import uuid


@dataclass
class ScriptStep:
    class Kind(str, Enum):
        speak = "speak"
        move = "move"
        pause = "pause"

    id: str
    scene: str
    build: int = 0
    kind: str = "speak"
    text: str = ""
    seconds: float = 1.0
    path: list[list[float]] = field(default_factory=list)
    speed: float = 0.5
    noise: float = 0.0

    speeds = [("Slow", 0.25), ("Medium", 0.5), ("Fast", 1.0), ("Very fast", 2.0)]

    @classmethod
    def from_dict(cls, row):
        step = cls(**{k: v for k, v in row.items() if k in cls.__dataclass_fields__})
        if step.kind not in ("speak", "move", "pause"):
            raise ValueError(f"Unknown script step kind: {step.kind}")
        if not isinstance(step.id, str) or not isinstance(step.scene, str):
            raise ValueError("Script steps require string id and scene")
        if not isinstance(step.build, int) or step.build < 0:
            raise ValueError("Script build must be a nonnegative integer")
        if not isinstance(step.text, str):
            raise ValueError("Script speech must be text")
        for value in (step.seconds, step.speed, step.noise):
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("Script timing and noise must be finite numbers")
        if step.seconds < 0 or not isinstance(step.path, list):
            raise ValueError("Invalid script duration or path")
        for point in step.path:
            if not isinstance(point, list) or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in point):
                raise ValueError("Script path requires finite coordinate arrays")
        return step

    @property
    def points(self):
        return [(p[0], p[1]) for p in self.path if len(p) >= 2]


class ScriptStore:
    def __init__(self, url, original_has_file=False):
        self.url = Path(url)
        self.keep_empty_file = original_has_file
        self.steps: list[ScriptStep] = []
        self.voice = None
        self.rate = 0.5
        self.hold = 3.0
        self.on_change = None
        if self.url.exists():
            data = self.url.read_bytes()
            if data.strip():
                content = json.loads(data)
                if content.get("version") != 1:
                    raise ValueError("Unsupported script.json version")
                self.steps = [ScriptStep.from_dict(row) for row in content["steps"]]
                self.voice = content.get("voice")
                self.rate = content.get("rate") if content.get("rate") is not None else 0.5
                self.hold = content.get("hold") if content.get("hold") is not None else 3.0
                if self.voice is not None and not isinstance(self.voice, str):
                    raise ValueError("Script voice must be an identifier string")
                if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in (self.rate, self.hold)):
                    raise ValueError("Invalid script rate or hold")

    def save(self):
        if not self.steps and self.voice is None and self.rate == 0.5 and self.hold == 3 and not self.keep_empty_file:
            self.url.unlink(missing_ok=True)
        else:
            content = {"version": 1, "steps": [asdict(s) for s in self.steps], "rate": self.rate, "hold": self.hold}
            if self.voice is not None:
                content["voice"] = self.voice
            data = (json.dumps(content, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
            self.url.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".script-", suffix=".json", dir=self.url.parent)
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(name, self.url)
            finally:
                Path(name).unlink(missing_ok=True)
        if self.on_change:
            self.on_change()

    @property
    def is_empty(self):
        return not self.steps

    def steps_for(self, scene, build):
        return [s for s in self.steps if s.scene == scene and s.build == build]

    def step(self, id):
        return next((s for s in self.steps if s.id == id), None)

    def add(self, kind, scene, build=0, after=None):
        s = ScriptStep(str(uuid.uuid4()).upper(), scene, build, kind)
        if kind == "move":
            previous = [p for p in self.steps_for(scene, build) if p.kind == "move"]
            if previous:
                s.speed, s.noise = previous[-1].speed, previous[-1].noise
        indices = [i for i, p in enumerate(self.steps) if p.id == after]
        if not indices:
            indices = [i for i, p in enumerate(self.steps) if p.scene == scene and p.build == build]
        self.steps.insert(indices[-1] + 1 if indices else len(self.steps), s)
        self.save()
        return s

    def update(self, id, change=None, **values):
        s = self.step(id)
        if s is None:
            return
        changed = deepcopy(s)
        if change:
            change(changed)
        for key, value in values.items():
            if key not in ScriptStep.__dataclass_fields__:
                raise ValueError(f"Unknown script field: {key}")
            setattr(changed, key, value)
        changed = ScriptStep.from_dict(asdict(changed))
        if changed != s:
            self.steps[self.steps.index(s)] = changed
            self.save()

    def remove(self, id):
        s = self.step(id)
        if s is not None:
            self.steps.remove(s)
            self.save()

    def shift(self, id, by):
        s = self.step(id)
        if s is None:
            return
        mine = self.steps_for(s.scene, s.build)
        pos = mine.index(s) + by
        if 0 <= pos < len(mine):
            a, b = self.steps.index(s), self.steps.index(mine[pos])
            self.steps[a], self.steps[b] = self.steps[b], self.steps[a]
            self.save()

    def remove_all(self):
        if self.steps:
            self.steps.clear()
            self.save()

    def set_voice(self, value):
        if value != self.voice:
            self.voice = value
            self.save()

    def set_rate(self, value):
        if value != self.rate:
            self.rate = value
            self.save()

    def set_hold(self, value):
        if value != self.hold:
            self.hold = value
            self.save()

    def pointer(self, before):
        step = self.step(before)
        point = None
        if step:
            for s in self.steps_for(step.scene, step.build):
                if s.id == before:
                    break
                if s.kind == "move" and s.points:
                    point = s.points[-1]
        return point


def _xy(p):
    return (p.x(), p.y()) if hasattr(p, "x") else (p[0], p[1])


def _size(size):
    return (size.width(), size.height()) if hasattr(size, "width") else size


class ScriptMotion:
    def __init__(self, points, size=(16, 9), speed=0.5, noise=0, seed=""):
        self.points = [_xy(p) for p in points]
        w, h = _size(size)
        self.aspect = h / w if w > 0 else 9 / 16
        self.lengths = [0.0]
        for a, b in zip(self.points, self.points[1:]):
            self.lengths.append(self.lengths[-1] + math.hypot(b[0] - a[0], (b[1] - a[1]) * self.aspect))
        self.duration = max(0.2, self.length / max(0.02, speed)) if self.length > 0 else 0.0
        self.noise = noise
        self.hash = self.seed_hash(seed)
        self.phases = [((self.hash >> (k * 16)) & 0xFFFF) / 65536 * 2 * math.pi for k in range(4)]

    @staticmethod
    def seed_hash(seed):
        value = 1469598103934665603
        for byte in seed.encode("utf-8"):
            value = ((value ^ byte) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
        return value

    @property
    def length(self):
        return self.lengths[-1]

    def point(self, at):
        if self.duration <= 0 or len(self.points) <= 1:
            return self.points[-1] if self.points else (0, 0)
        u = min(1, max(0, at / self.duration))
        eased = u - math.sin(2 * math.pi * u) / (2 * math.pi)
        distance = eased * self.length
        i = 1
        while i < len(self.lengths) - 1 and self.lengths[i] < distance:
            i += 1
        segment = self.lengths[i] - self.lengths[i - 1]
        f = (distance - self.lengths[i - 1]) / segment if segment > 0 else 1
        a, b = self.points[i - 1], self.points[i]
        x, y = a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
        if self.noise > 0:
            amp = self.noise * 0.012 * math.sin(math.pi * u)
            nx = 0.6 * math.sin(2 * math.pi * 1.3 * at + self.phases[0]) + 0.4 * math.sin(2 * math.pi * 3.1 * at + self.phases[1])
            ny = 0.6 * math.sin(2 * math.pi * 1.7 * at + self.phases[2]) + 0.4 * math.sin(2 * math.pi * 2.6 * at + self.phases[3])
            x += amp * nx
            y += amp * ny / self.aspect
        return x, y


def script_pointer_anchor(point, rect):
    x, y, width, height = rect
    px, py = _xy(point)
    return x + width * px, y + height * py


@lru_cache(maxsize=16)
def script_pointer_image(height, device_scale=1.0):
    """The tip-anchored Swift arrow, rasterized once per size and device scale.

    Keep this independent of Qt for the movie worker's engine interpreter.
    """
    from PIL import Image, ImageDraw, ImageFilter

    k = max(14, height * 0.045) / 20
    stroke = max(1, 1.1 * k)
    inset = math.ceil((7 + stroke / 2) * device_scale)
    width = math.ceil(12 * k * device_scale) + 2 * inset
    height = math.ceil(20 * k * device_scale) + 2 * inset
    samples = 4
    unit = device_scale * samples
    origin = inset * samples
    dimensions = (width * samples, height * samples)
    points = [(origin + x * k * unit, origin + y * k * unit)
              for x, y in ((0, 0), (0, 17), (4, 13.2), (7, 19.6), (9.4, 18.6), (6.5, 12.2), (12, 12.2))]
    arrow = Image.new("RGBA", dimensions)
    draw = ImageDraw.Draw(arrow)
    draw.polygon(points, fill="white")
    draw.line(points + points[:1], fill="black", width=round(stroke * unit))
    radius = stroke * unit / 2
    for x, y in points:
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill="black")

    alpha = Image.new("L", dimensions)
    offset = round(unit)
    alpha.paste(arrow.getchannel("A"), (offset, offset))
    alpha = alpha.filter(ImageFilter.GaussianBlur(2 * unit)).point(lambda value: round(value * .35))
    sprite = Image.new("RGBA", dimensions)
    sprite.putalpha(alpha)
    sprite.alpha_composite(arrow)
    return sprite.resize((width, height), Image.Resampling.LANCZOS), inset


@lru_cache(maxsize=16)
def _qt_script_pointer_image(height, device_scale):
    from PySide6.QtGui import QImage

    sprite, inset = script_pointer_image(height, device_scale)
    image = QImage(sprite.tobytes(), sprite.width, sprite.height, sprite.width * 4,
                   QImage.Format.Format_RGBA8888).copy()
    image.setDevicePixelRatio(device_scale)
    return image, inset / device_scale


def paint_script_pointer(painter, point, rect):
    """Draw the arrow over a fitted QRectF, with the tip at normalized (x, y)."""
    if point is None or rect.isEmpty():
        return
    from PySide6.QtCore import QPointF

    image, inset = _qt_script_pointer_image(rect.height(), painter.device().devicePixelRatioF())
    x, y = script_pointer_anchor(point, (rect.x(), rect.y(), rect.width(), rect.height()))
    painter.drawImage(QPointF(x - inset, y - inset), image)


class ScriptClock:
    """A pauseable monotonic clock, including time spent awaiting synthesized audio."""
    def __init__(self, monotonic=time.monotonic):
        self.monotonic = monotonic
        self._last = monotonic()
        self._elapsed = 0.0
        self._paused = False

    @property
    def elapsed(self):
        now = self.monotonic()
        if not self._paused:
            self._elapsed += max(0, now - self._last)
        self._last = now
        return self._elapsed

    @property
    def paused(self):
        return self._paused

    @paused.setter
    def paused(self, value):
        self.elapsed
        self._paused = bool(value)


class ScriptPerformer:
    def __init__(self, voices=None, monotonic=time.monotonic, sleeper=None):
        self.voices = voices
        self.sleeper = sleeper
        self.voice = None
        self.rate = 0.5
        self.pointer = None
        self.draw = lambda p: None
        self.slide_size = lambda: (16, 9)
        self.interrupt = lambda: False
        self.clock = ScriptClock(monotonic)
        self.stopped = False
        self.player = None
        self.audio_output = None

    @property
    def paused(self):
        return self.clock.paused

    @paused.setter
    def paused(self, value):
        self.clock.paused = value
        if self.player:
            self.player.pause() if value else self.player.play()

    @property
    def halted(self):
        return self.stopped or self.interrupt()

    def stop(self):
        if self.voices is not None and hasattr(self.voices, "cancel"):
            self.voices.cancel()
        self.stopped = True
        if self.player:
            self.player.stop()

    def frame(self):
        if self.sleeper is not None:
            return self.sleeper(1 / 60)
        from .tasks import sleep
        return sleep(1 / 60)

    def wait(self, seconds):
        until = self.clock.elapsed + max(0, seconds)
        while not self.halted and (self.paused or self.clock.elapsed < until):
            yield self.frame()

    def perform(self, step):
        if self.halted:
            return
        if step.kind == "pause":
            yield from self.wait(step.seconds)
        elif step.kind == "move":
            yield from self.move(step)
        else:
            yield from self.speak(step.text)

    def move(self, step):
        if not step.points:
            return
        track = ScriptMotion([self.pointer or (0.5, 0.5)] + step.points, self.slide_size(), step.speed, step.noise, step.id)
        start = self.clock.elapsed
        self.pointer = track.points[0]
        self.draw(self.pointer)
        while not self.halted:
            t = self.clock.elapsed - start
            if not self.paused:
                self.pointer = track.point(min(t, track.duration))
                self.draw(self.pointer)
            if t >= track.duration and not self.paused:
                break
            yield self.frame()
        if not self.halted:
            self.pointer = step.points[-1]
            self.draw(self.pointer)

    def speak(self, text):
        if not text.strip():
            return
        if self.voices is None:
            raise RuntimeError("No narration service was supplied")
        from PySide6.QtCore import QUrl
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        from .tasks import sleep, spawn
        synthesis = spawn(self.voices.speech(text, self.voice, self.rate), "Prepare narration")
        while not synthesis.future.done():
            if self.halted:
                self.voices.cancel()
                synthesis.cancel()
                return
            yield sleep(1 / 60)
        if self.halted:
            return
        audio = synthesis.future.result()
        if audio is None:
            return
        path, duration = audio
        player = QMediaPlayer()
        output = QAudioOutput(player)
        player.setAudioOutput(output)
        self.player, self.audio_output = player, output
        player.setSource(QUrl.fromLocalFile(str(path)))
        if not self.paused:
            player.play()
        try:
            while not self.halted:
                if player.error() != QMediaPlayer.Error.NoError:
                    raise RuntimeError(player.errorString())
                if player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia:
                    break
                yield sleep(1 / 60)
        finally:
            player.stop()
            player.deleteLater()
            if self.player is player:
                self.player = None
                self.audio_output = None


class ScriptShow(Protocol):
    window: object
    page_count: int
    page_index: int
    script_page: tuple[str, int] | None
    script_page_ready: bool
    script_slide_rect: object
    on_exit: object

    def next(self): ...
    def exit(self): ...


class ScriptRunner:
    def __init__(self, store, show, voices=None, monotonic=time.monotonic, sleeper=None):
        self.store, self.show = store, show
        self.performer = ScriptPerformer(voices, monotonic, sleeper)
        self.performer.voice, self.performer.rate = store.voice, store.rate
        self.on_end = None
        self.task = None
        self.layer = None
        self._ended = False
        self._old_exit = None

    def start(self):
        from .tasks import spawn, when_done
        from PySide6.QtCore import Qt
        self._old_key = self.show.window.on_key

        def key(event):
            if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_K):
                if not event.isAutoRepeat():
                    self.toggle_pause()
                return
            if self._old_key:
                self._old_key(event)

        self._key = key
        self.show.window.on_key = key
        layer_class = __getattr__("ScriptLayer")
        self.layer = layer_class(self.show.window)
        self.layer.slide_rect = lambda: self.show.script_slide_rect
        self.layer.show()
        self.layer.raise_()
        self.performer.draw = self.layer.show_pointer
        self.performer.slide_size = lambda: self.show.script_slide_rect.size()
        self._old_exit = self.show.on_exit
        self.show.on_exit = self._exit
        self.task = spawn(self.run(), "Scripted presentation")

        def finished(future):
            if not future.cancelled() and future.exception() is not None:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(self.show.window, "Scripted presentation", str(future.exception()))
            self.stop()
        when_done(self.task.future, finished)

    def _exit(self):
        old = self._old_exit
        self.stop()
        if old:
            old()

    def stop(self):
        if self._ended:
            return
        self._ended = True
        self.performer.stop()
        if self.task:
            self.task.cancel()
        if self.layer:
            self.layer.hide()
            self.layer.deleteLater()
            self.layer = None
        if hasattr(self, "_key") and self.show.window.on_key is self._key:
            self.show.window.on_key = self._old_key
        if self.show.on_exit == self._exit:
            self.show.on_exit = self._old_exit
        if self.on_end:
            callback, self.on_end = self.on_end, None
            callback()

    def toggle_pause(self):
        self.performer.paused = not self.performer.paused
        if hasattr(self.show, "set_script_paused"):
            self.show.set_script_paused(self.performer.paused)
        if self.layer:
            self.layer.paused = self.performer.paused
            self.layer.update()

    def run(self):
        sleep = self.performer.sleeper
        if sleep is None:
            from .tasks import sleep
        last_scene = None
        while not self.performer.stopped:
            while self.performer.paused or not self.show.script_page_ready:
                if self.performer.stopped:
                    return
                yield sleep(0.05)
            page_index = self.show.page_index
            page = self.show.script_page
            if page is None:
                return
            scene, build = page
            if scene != last_scene:
                self.performer.draw(None)
                last_scene = scene
            self.performer.interrupt = lambda: self.show.page_index != page_index
            steps = self.store.steps_for(scene, build)
            if not steps:
                yield from self.performer.wait(self.store.hold)
            for step in steps:
                yield from self.performer.perform(step)
                if self.performer.halted:
                    break
            if self.performer.stopped:
                return
            if self.show.page_index != page_index:
                continue
            yield from self.performer.wait(0.4)
            if self.performer.stopped:
                return
            if self.show.page_index != page_index:
                continue
            if page_index >= self.show.page_count - 1:
                self.show.exit()
                return
            self.show.next()
            while not self.performer.stopped and self.show.page_index == page_index:
                yield sleep(0.03)


@dataclass
class ScriptState:
    store: ScriptStore | None = None
    pane: object = None
    layer: object = None
    preview: ScriptPerformer | None = None
    preview_task: object = None
    runner: ScriptRunner | None = None
    movie_export: object = None
    movie_active: bool = False
    movie_cancelled: bool = False
    movie_done: object = None


class ScriptMixin:
    def init_script(self):
        state = self._script_state()
        if state.store is None and self.workspace:
            self.open_script(self.workspace)

    def _script_state(self):
        if not hasattr(self, "script"):
            self.script = ScriptState()
        return self.script

    def open_script(self, ws):
        state = self._script_state()
        self.hide_script_pane()
        if state.runner:
            state.runner.stop()
        if state.pane:
            self.inspector_host.removeWidget(state.pane)
            state.pane.dispose()
            state.pane.deleteLater()
            state.pane = None
        state.store = ScriptStore(ws.work / "script.json", (ws.original / "script.json").exists())
        state.store.on_change = self.script_changed
        if state.layer is None:
            layer = __getattr__("ScriptLayer")(self.canvas)
            layer.slide_rect = lambda: self.canvas.slide_rect
            layer.hide()
            layer.on_captured = self._script_captured
            state.layer = layer
        self.sync_script_button()

    def _script_captured(self, id, points):
        self.script.store.update(id, path=[list(p) for p in points])
        self.set_script_capture(None)
        step = self.script.store.step(id)
        if step:
            self.play_script_step(step)

    def script_changed(self):
        if hasattr(self, "update_document_state"):
            self.update_document_state()
        self.sync_script_button()
        if self.script.pane:
            self.script.pane.reload()
        self.sync_script_layer()

    def sync_script_button(self):
        from PySide6.QtCore import QSignalBlocker

        state = self._script_state()
        button = getattr(getattr(self, "format_bar", None), "script", None)
        if button is not None:
            with QSignalBlocker(button):
                button.setEnabled(state.store is not None)
                button.setChecked(self.script_pane_is_open())

    def script_pane_is_open(self):
        pane = self._script_state().pane
        return pane is not None and self.inspector_host.currentWidget() is pane

    def _script_visibility_changed(self, visible):
        if visible:
            self.sync_script_page()
        else:
            self.stop_script_preview()
            self.set_script_capture(None)
            self.sync_script_layer()

    def sync_script_page(self):
        state = self._script_state()
        pane = state.pane
        if pane is None or pane.isHidden():
            return
        if (pane.scene, pane.build) != (self.scene_name, self.stop_index):
            self.stop_script_preview()
            self.set_script_capture(None)
        builds = len((self.scene or {}).get("stops", []))
        label = self.feedback_slide_label(self.scene_name) if self.scene_name else "No slide"
        if builds > 1:
            label += f" · build {self.stop_index + 1} of {builds}"
        pane.show_page(self.scene_name, self.stop_index, label)
        self.sync_script_layer()

    def sync_script_layer(self):
        state = self._script_state()
        if state.layer:
            opened = self.script_pane_is_open() and state.pane.isVisible()
            state.layer.setVisible(opened)
            state.layer.steps = state.store.steps_for(self.scene_name, self.stop_index) if opened and state.store else []
            state.layer.selected = state.pane.selected if opened else None
            state.layer.update()
            state.layer.raise_()

    def view_script(self, sender=None):
        state = self._script_state()
        if self.script_pane_is_open():
            self.hide_script_pane()
            return
        if state.store is None:
            return
        if state.pane is None:
            pane = __getattr__("ScriptPane")(state.store)
            pane.on_close = self.hide_script_pane
            pane.on_visibility_changed = self._script_visibility_changed
            pane.on_play = self.play_script_step
            pane.on_play_all = self.play_script_build
            pane.on_stop_playing = self.stop_script_preview
            pane.on_capture = self.set_script_capture
            pane.on_select = self._script_selected
            pane.on_error = lambda text: self.alert("Script", text)
            pane.canvas_view = self.canvas
            state.pane = pane
        self.show_inspector_page(state.pane)
        self.sync_script_page()

    def _script_selected(self, id):
        if self.script.layer:
            self.script.layer.selected = id
            self.script.layer.update()

    def hide_script_pane(self):
        self.stop_script_preview()
        self.set_script_capture(None)
        if self.script_pane_is_open():
            self.show_inspector_page(None)
        self.sync_script_layer()

    def set_script_capture(self, id):
        state = self._script_state()
        if state.pane:
            state.pane.set_capturing(id)
        if state.layer:
            state.layer.set_capture(id)
        if id:
            self.stop_script_preview()

    def _script_voices(self):
        from .voice import VoiceManager
        return VoiceManager(self.paths.voice_cache, self.window, self.runtime.child_env(),
                            lambda text: self.toast(text, error=True))

    def make_preview(self):
        state = self._script_state()
        if state.store is None or state.layer is None:
            return None
        self.stop_script_preview()
        performer = ScriptPerformer(self._script_voices())
        performer.voice, performer.rate = state.store.voice, state.store.rate
        performer.draw = state.layer.show_pointer
        performer.slide_size = lambda: state.layer.slide_rect().size()
        state.preview = performer
        return performer

    def play_script_step(self, step):
        performer = self.make_preview()
        if performer is None:
            return
        performer.pointer = self.script.store.pointer(step.id)
        self._preview_steps(performer, [step], linger=step.kind == "move")

    def play_script_build(self):
        if not self.scene_name or not self._script_state().store:
            return
        steps = self.script.store.steps_for(self.scene_name, self.stop_index)
        if not steps:
            return
        performer = self.make_preview()
        if performer:
            self._preview_steps(performer, steps, linger=True)

    def _preview_steps(self, performer, steps, *, linger):
        from .tasks import spawn
        state = self.script
        if state.pane:
            state.pane.set_playing(True)

        def play():
            try:
                for step in steps:
                    if performer.stopped:
                        break
                    if state.pane:
                        state.pane.select(step.id, False)
                    yield from performer.perform(step)
                if linger and not performer.stopped:
                    yield from performer.wait(0.6)
            except Exception as exc:
                self.alert("Script preview", str(exc))
            finally:
                self.end_script_preview(performer)
        state.preview_task = spawn(play(), "Script preview")

    def end_script_preview(self, performer):
        state = self._script_state()
        if state.preview is performer:
            performer.stop()
            state.preview, state.preview_task = None, None
            if state.layer:
                state.layer.show_pointer(None)
            if state.pane:
                state.pane.set_playing(False)

    def stop_script_preview(self):
        state = self._script_state()
        if state.preview:
            performer = state.preview
            performer.stop()
            if state.preview_task:
                state.preview_task.cancel()
            self.end_script_preview(performer)

    def auto_present_from_beginning(self):
        self.auto_present(False)

    def auto_present_from_current(self):
        self.auto_present(True)

    def auto_present(self, from_current=False):
        self.hide_script_pane()
        if self._script_state().store is None:
            return
        if self.script.store.is_empty:
            self.toast(f"No script yet: each build stays up {self.script.store.hold:g} s "
                       "(Script, top right, adds words and pointer moves)")
        if self.show_animations:
            self.present(from_current=from_current, auto=True)
        else:
            self.play(from_current=from_current, auto=True)

    def start_script_runner(self, show):
        state = self._script_state()
        if state.store is None:
            return
        if state.runner:
            state.runner.stop()
        runner = ScriptRunner(state.store, show, self._script_voices())
        state.runner = runner
        runner.on_end = lambda: setattr(state, "runner", None) if state.runner is runner else None
        runner.start()

    def _script_movie_progress(self, fraction, text):
        if not self.script.movie_cancelled:
            self.busy_text = text
            self.busy_progress = fraction
            self.update_busy_pill()

    def handle_script_movie_event(self, event):
        state = self._script_state()
        done = state.movie_done
        if done is None:
            return False
        phase = event.get("phase")
        if not done.done():
            if phase == "done" and event.get("path") == str(state.movie_export.base):
                done.set_result(event)
            elif phase == "cancelled":
                done.set_result(None)
            elif phase in ("error", "failed"):
                done.set_exception(RuntimeError(event.get("error") or "movie export failed"))
            elif phase in ("building", "encoding"):
                i, total = event.get("done", 0), event.get("total", 0)
                title = "Encoding movie" if phase == "encoding" else "Building animations"
                self._script_movie_progress(i / total if total else None,
                                            f"{title} {i + 1}/{total}: {event.get('scene', '')}")
        return True

    def export_scripted_movie(self, request, *, store, scenes, to):
        from concurrent.futures import Future
        from .models import frame_size
        from .script_movie import ScriptMovieExport
        from .voice import VoiceCancelled
        from .workers import run_blocking

        state = self._script_state()
        if state.movie_active:
            self.toast("A movie is already being exported", error=True)
            return
        state.movie_active = True
        state.movie_cancelled = False
        rpc = self.rpc
        export = None
        error_prefix = "Could not prepare the narration: "

        def registered(prepared):
            state.movie_export = prepared
            prepared.on_progress = self._script_movie_progress
            if state.movie_cancelled:
                prepared.cancel()

        def stopped(code, text):
            done = state.movie_done
            if done is not None and not done.done():
                done.set_exception(RuntimeError(text or f"Engine stopped ({code})"))

        rpc.stopped.connect(stopped)
        try:
            with self._project_busy("Preparing the narration…"):
                height = request.get("height", 720)
                width, frame_height = frame_size(self.scene)
                export = yield from ScriptMovieExport.prepare(store, scenes, (round(height * 16 / 9), height),
                    width / frame_height, request.get("fps", 30), to, self._script_voices(),
                    lambda i, n: self._script_movie_progress((i - 1) / n, f"Preparing the narration {i}/{n}"),
                    on_created=registered)
                if state.movie_cancelled:
                    self.toast("cancelled")
                    return
                error_prefix = ""
                self._script_movie_progress(None, "Exporting movie…")
                state.movie_done = Future()
                reply = yield self.call("export_movie", {**request, "path": str(export.base),
                    "holds": export.holds, "hold": export.unscripted, "scenes": scenes})
                if reply.get("ok") is not True:
                    raise RuntimeError(reply.get("error") or "could not start the movie export")
                completed = yield state.movie_done
                state.movie_done = None
                if completed is None or state.movie_cancelled:
                    self.toast("cancelled")
                    return
                error_prefix = "Could not write the movie: "
                self._script_movie_progress(None, "Adding the narration and pointer…")
                return (yield from export.finish(completed["segments"], self.runtime.python, self.runtime.child_env()))
        except VoiceCancelled:
            self.toast("cancelled")
        except Exception as exc:
            if state.movie_cancelled:
                self.toast("cancelled")
            else:
                self.toast(f"{error_prefix}{exc}", error=True)
        finally:
            rpc.stopped.disconnect(stopped)
            state.movie_done = None
            state.movie_export = None
            state.movie_active = False
            if export is not None:
                run_blocking(export.discard)

    def cancel_script_movie(self):
        state = self._script_state()
        if not state.movie_active:
            return False
        if not state.movie_cancelled:
            state.movie_cancelled = True
            self._cancelling = True
            self.busy_progress = None
            self.update_busy_pill()
            if state.movie_done is not None:
                if not state.movie_done.done():
                    self.call("cancel")
            elif state.movie_export:
                state.movie_export.cancel()
        return True


def __getattr__(name):
    if name not in ("ScriptLayer", "ScriptPane"):
        raise AttributeError(name)
    if "ScriptLayer" not in globals():
        _load_widgets()
    return globals()[name]


def _load_widgets():
    from PySide6.QtCore import QEvent, QLocale, QPointF, QRectF, QSignalBlocker, QSize, Qt
    from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPalette, QPen, QPolygonF, QRegion, QValidator
    from PySide6.QtWidgets import (QAbstractButton, QAbstractSlider, QAbstractSpinBox, QApplication,
        QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QScrollArea,
        QSizePolicy, QSlider, QToolButton, QVBoxLayout, QWidget)
    from .icons import template_icon
    from .style import Theme, watch_appearance
    from .tasks import spawn
    from .voice import CloudVoice, CloudVoiceAccount, CloudVoiceService, NativeVoice
    from .workers import run_blocking
    from .paths import AppPaths

    class ScriptLayer(QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.slide_rect = lambda: QRectF()
            self.steps = []
            self.selected = None
            self.capturing = None
            self.on_captured = None
            self.pointer = None
            self.trace = []
            self.paused = False
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            if parent:
                self.setGeometry(parent.rect())
                parent.installEventFilter(self)

        def eventFilter(self, watched, event):
            if watched is self.parentWidget() and event.type() == QEvent.Type.Resize:
                self.setGeometry(watched.rect())
                self._mask()
            return False

        def _mask(self):
            if self.capturing:
                self.setMask(QRegion(self.slide_rect().toRect()))
            else:
                self.clearMask()

        def set_capture(self, id):
            self.capturing = id
            self.trace = []
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, id is None)
            self.setCursor(Qt.CursorShape.CrossCursor if id else Qt.CursorShape.ArrowCursor)
            self._mask()
            self.update()

        def show_pointer(self, point):
            self.pointer = point
            self.update()

        def _view(self, point, rect):
            x, y = _xy(point)
            return QPointF(rect.x() + rect.width() * x, rect.y() + rect.height() * y)

        def _slide(self, point):
            r = self.slide_rect()
            return (min(1, max(0, (point.x() - r.x()) / r.width())),
                    min(1, max(0, (point.y() - r.y()) / r.height())))

        def paintEvent(self, event):
            r = self.slide_rect()
            if r.width() <= 0 or r.height() <= 0:
                return
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setFont(Theme.font(10, QFont.Weight.Bold))
            previous = None
            number = 0
            for step in self.steps:
                if step.kind != "move":
                    continue
                number += 1
                points = step.points
                if not points:
                    continue
                origin, previous = previous or points[0], points[-1]
                if step.id not in (self.selected, self.capturing):
                    continue
                path = QPainterPath(self._view(origin, r))
                for p in points:
                    path.lineTo(self._view(p, r))
                selected = step.id == self.selected
                accent = QColor(Theme.accent)
                accent.setAlphaF(1.0 if selected else .6)
                width = 2.5 if selected else 1.5
                pen = QPen(accent, width, Qt.PenStyle.DashLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
                pen.setDashPattern([5 / width, 4 / width])  # Qt dash lengths are in units of pen width.
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(path)
                end = self._view(points[-1], r)
                painter.setBrush(accent)
                painter.setPen(Qt.PenStyle.NoPen)
                radius = 9 if selected else 8
                painter.drawEllipse(end, radius, radius)
                painter.setPen(QColor("white"))
                painter.drawText(QRectF(end.x() - radius, end.y() - radius, radius * 2, radius * 2),
                                 Qt.AlignmentFlag.AlignCenter, str(number))
            if self.capturing:
                accent = QColor(Theme.accent)
                accent.setAlphaF(.9)
                painter.setPen(QPen(accent, 3))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(r.adjusted(1.5, 1.5, -1.5, -1.5))
                if len(self.trace) > 1:
                    painter.setPen(QPen(accent, 2.5, Qt.PenStyle.SolidLine,
                                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                    painter.drawPolyline(QPolygonF([self._view(p, r) for p in self.trace]))
                hint = "  Click to set where the pointer goes, or drag to trace its track  "
                painter.setFont(Theme.font(12, QFont.Weight.Medium))
                metrics = painter.fontMetrics()
                hint = metrics.elidedText(hint, Qt.TextElideMode.ElideRight, max(0, int(r.width() - 16)))
                width = metrics.horizontalAdvance(hint)
                box = QRectF(r.center().x() - width / 2, r.top() + 8, width, metrics.height())
                painter.fillRect(box, accent)
                painter.setPen(Qt.GlobalColor.white)
                painter.drawText(box, Qt.AlignmentFlag.AlignCenter, hint)
            paint_script_pointer(painter, self.pointer, r)
            if self.paused:
                box = QRectF(16, 18, 230, 26)
                painter.setBrush(QColor(0, 0, 0, 153))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(box, 6, 6)
                painter.setPen(Qt.GlobalColor.white)
                painter.setFont(Theme.font(14))
                painter.drawText(box, Qt.AlignmentFlag.AlignCenter, "❚❚  Paused · space to resume")
            painter.end()

        def mousePressEvent(self, event):
            if (event.button() == Qt.MouseButton.LeftButton and self.capturing
                    and self.slide_rect().contains(event.position())):
                self.trace = [self._slide(event.position())]
                self.update()
                event.accept()
            else:
                event.ignore()

        def mouseMoveEvent(self, event):
            if not self.trace:
                return
            r = self.slide_rect()
            p, last = self._slide(event.position()), self.trace[-1]
            if math.hypot((p[0] - last[0]) * r.width(), (p[1] - last[1]) * r.height()) >= 3:
                self.trace.append(p)
                self.update()

        def mouseReleaseEvent(self, event):
            if not self.capturing or not self.trace:
                return
            r, first = self.slide_rect(), self.trace[0]
            span = max(math.hypot((p[0] - first[0]) * r.width(), (p[1] - first[1]) * r.height()) for p in self.trace)
            points = self.thin([first] if span < 6 else self.trace)
            self.trace = []
            if self.on_captured:
                self.on_captured(self.capturing, points)
            self.update()

        @staticmethod
        def thin(points, tolerance=0.002):
            points = [_xy(p) for p in points]
            out = points[:1]
            for i in range(1, len(points) - 1):
                a, b, c = out[-1], points[i], points[i + 1]
                length = math.hypot(c[0] - a[0], c[1] - a[1])
                distance = (abs((c[0] - a[0]) * (a[1] - b[1]) - (a[0] - b[0]) * (c[1] - a[1])) / length
                            if length > 0 else math.hypot(b[0] - a[0], b[1] - a[1]))
                if distance > tolerance:
                    out.append(b)
            if len(points) > 1:
                out.append(points[-1])
            # Swift's rounded() breaks half-way ties away from zero.
            return [tuple(math.copysign(math.floor(abs(v) * 10000 + 0.5), v) / 10000 for v in p) for p in out]

    step_symbols = {"speak": "text.bubble", "move": "cursorarrow.motionlines", "pause": "pause.circle"}

    def ink(widget, color, role=QPalette.ColorRole.WindowText):
        palette = widget.palette()
        palette.setColor(role, color)
        widget.setPalette(palette)

    def label(text, size=11, weight=QFont.Weight.Normal, *, shrink=False):
        widget = QLabel(text)
        widget.setTextFormat(Qt.TextFormat.PlainText)
        widget.setFont(Theme.font(size, weight))
        if shrink:
            widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        return widget

    def button(text, symbol, tip, callback):
        widget = QToolButton()
        widget.setText(text)
        widget.setAccessibleName(text or tip)
        widget.setToolTip(tip)
        widget.setFont(Theme.font(11))
        if symbol:
            widget.setIcon(template_icon(symbol, widget))
            widget.setIconSize(QSize(12, 12))
        if text:
            widget.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon if symbol
                                      else Qt.ToolButtonStyle.ToolButtonTextOnly)
        else:
            widget.setFixedWidth(18)
            widget.setAutoRaise(True)
        widget.clicked.connect(lambda _checked=False: callback())
        return widget

    def horizontal(spacing, *items, stretch=False):
        row = QHBoxLayout()
        row.setSpacing(spacing)
        for item in items:
            if isinstance(item, QWidget):
                row.addWidget(item)
            else:
                row.addLayout(item)
        if stretch:
            row.addStretch()
        return row

    class Symbol(QWidget):
        def __init__(self, name, size):
            super().__init__()
            self.setFixedSize(size, size)
            self.icon = template_icon(name, self, tint=Theme.accent)

        def paintEvent(self, _):
            with QPainter(self) as painter:
                self.icon.paint(painter, self.rect())

    class Hairline(QWidget):
        def __init__(self):
            super().__init__()
            self.setFixedHeight(1)

        def paintEvent(self, _):
            with QPainter(self) as painter:
                painter.fillRect(self.rect(), Theme.separator())

    class SecondsField(QDoubleSpinBox):
        def textFromValue(self, value):
            return self.locale().toString(value, "g", 6)

        def validate(self, text, position):
            value, ok = self.locale().toDouble(text)
            if ok and math.isfinite(value):
                return QValidator.State.Acceptable, text, position
            return super().validate(text, position)

        def valueFromText(self, text):
            value, ok = self.locale().toDouble(text)
            return min(600, max(0, value)) if ok and math.isfinite(value) else self.value()

    class ScriptRow(QWidget):
        def __init__(self, pane, step, index, move_number):
            super().__init__()
            self.pane = pane
            self.id = step.id
            self.kind = step.kind
            self.selected = False
            self.speech = self.seconds = self.capture_button = self.summary = None
            self.secondary_labels = []
            self.has_target = False
            layout = QVBoxLayout(self)
            layout.setContentsMargins(8, 7, 8, 8)
            layout.setSpacing(5)
            self.heading = label("", 11, QFont.Weight.DemiBold)
            self.renumber(index, move_number)
            head = horizontal(5, Symbol(step_symbols[self.kind], 14))
            head.addWidget(self.heading, 1)
            for symbol, tip, action in (
                ("play.fill", "Play this step on the slide", lambda: pane.select(self.id, True)),
                ("chevron.up", "Earlier", lambda: pane.shift(self.id, -1)),
                ("chevron.down", "Later", lambda: pane.shift(self.id, 1)),
                ("trash", "Delete this step", lambda: pane.remove(self.id)),
            ):
                head.addWidget(button("", symbol, tip, action))
            layout.addLayout(head)
            if self.kind == "speak":
                self.speech = QPlainTextEdit(step.text)
                self.speech.setFont(Theme.font(12))
                self.speech.setAccessibleName("Words to speak…")
                self.speech.setPlaceholderText("Words to speak…")
                self.speech.setFixedHeight(max(56, 3 * self.speech.fontMetrics().lineSpacing() + 8))
                self.speech.textChanged.connect(lambda: self.change(text=self.speech.toPlainText()))
                layout.addWidget(self.speech)
            elif self.kind == "pause":
                self.seconds = SecondsField()
                self.seconds.setFont(Theme.font(11))
                self.seconds.setAlignment(Qt.AlignmentFlag.AlignRight)
                self.seconds.setAccessibleName("Wait (seconds)")
                self.seconds.setDecimals(6)
                self.seconds.setRange(0, 600)
                self.seconds.setSingleStep(1)
                self.seconds.setKeyboardTracking(False)
                # 44 for the text field, plus the native integrated stepper.
                self.seconds.setFixedWidth(max(58, self.seconds.fontMetrics().horizontalAdvance("000.00") + 22))
                self.seconds.valueChanged.connect(lambda value: self.change(seconds=value))
                layout.addLayout(horizontal(4, self.secondary("Wait"), self.seconds,
                                            self.secondary("seconds"), stretch=True))
            else:
                self.capture_button = button("Set on Slide", "scope",
                    "Click on the slide for where the pointer goes, or drag on it to trace the track it follows",
                    self.toggle_capture)
                self.capture_button.setCheckable(True)
                self.summary = label("", shrink=True)
                target = horizontal(8, self.capture_button)
                target.addWidget(self.summary, 1)
                layout.addLayout(target)
                self.speed = QComboBox()
                self.speed.setFont(Theme.font(11))
                self.speed.setAccessibleName("Speed")
                self.speed.setToolTip("How fast the pointer travels")
                for title, value in ScriptStep.speeds:
                    self.speed.addItem(title, value)
                self.speed.activated.connect(lambda index: self.change(speed=self.speed.itemData(index)))
                self.noise = QSlider(Qt.Orientation.Horizontal)
                self.noise.setRange(0, 100)
                self.noise.setFixedWidth(70)
                self.noise.setAccessibleName("Wobble")
                self.noise.setToolTip("Wobble: a little noise in the movement, like a hand on a mouse")
                self.noise.valueChanged.connect(lambda value: self.change(noise=value / 100))
                layout.addLayout(horizontal(5, self.secondary("Speed"), self.speed,
                                            self.secondary("Wobble"), self.noise, stretch=True))
            self.refresh(step)
            self.set_capturing(pane.capturing == self.id)
            self.refresh_appearance()

        def secondary(self, text):
            widget = label(text)
            self.secondary_labels.append(widget)
            return widget

        def change(self, **fields):
            self.pane._change(lambda: self.pane.store.update(self.id, **fields))

        def renumber(self, index, move_number):
            title = {"speak": "Speak", "move": f"Mouse {move_number}", "pause": "Pause"}
            self.heading.setText(f"{index}. {title[self.kind]}")

        def refresh(self, step):
            if self.speech is not None:
                if not self.speech.hasFocus() and self.speech.toPlainText() != step.text:
                    with QSignalBlocker(self.speech):
                        self.speech.setPlainText(step.text)
            elif self.seconds is not None:
                if not self.seconds.hasFocus():
                    with QSignalBlocker(self.seconds):
                        self.seconds.setValue(step.seconds)
            else:
                points = step.points
                self.has_target = bool(points)
                if not points:
                    text = "no target yet"
                elif len(points) == 1:
                    x, y = points[0]
                    text = f"to {x * 100:.0f}%, {y * 100:.0f}%"
                else:
                    text = f"traced track, {len(points)} points"
                self.summary.setText(text)
                self.refresh_summary_ink()
                self.speed.setCurrentIndex(min(range(self.speed.count()),
                    key=lambda index: abs(self.speed.itemData(index) - step.speed)))
                with QSignalBlocker(self.noise):
                    self.noise.setValue(round(step.noise * 100))

        def toggle_capture(self):
            self.pane.select(self.id)
            if self.pane.on_capture:
                self.pane.on_capture(None if self.pane.capturing == self.id else self.id)

        def set_capturing(self, capturing):
            if self.capture_button is not None:
                title = "Setting on Slide…" if capturing else "Set on Slide"
                self.capture_button.setText(title)
                self.capture_button.setAccessibleName(title)
                self.capture_button.setChecked(capturing)

        def refresh_summary_ink(self):
            if self.summary is not None:
                ink(self.summary, Theme.secondary_ink() if self.has_target else Theme.warning_ink())

        def refresh_appearance(self):
            for widget in self.secondary_labels:
                ink(widget, Theme.secondary_ink())
            self.refresh_summary_ink()
            self.update()

        def set_selected(self, selected):
            if self.selected != selected:
                self.selected = selected
                self.update()

        def paintEvent(self, _):
            box = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
            neutral = self.palette().color(QPalette.ColorRole.Base)
            neutral.setAlphaF(.6)
            with QPainter(self) as painter:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.setPen(QPen(Theme.accent if self.selected else Theme.separator(), 1))
                painter.setBrush(Theme.accent_soft if self.selected else neutral)
                painter.drawRoundedRect(box, 6, 6)

        def mousePressEvent(self, event):
            if event.button() == Qt.MouseButton.LeftButton:
                self.pane.end_field_edit()
                self.pane.select(self.id, self.kind != "speak")
                event.accept()
            else:
                super().mousePressEvent(event)

    class ScriptPane(QWidget):
        def __init__(self, store, parent=None):
            super().__init__(parent)
            self.store = store
            self.scene, self.build, self.selected, self.capturing = None, 0, None, None
            self.playing = False
            self.voice_tasks = []
            self.on_close = self.on_play = self.on_play_all = self.on_stop_playing = None
            self.on_capture = self.on_select = self.on_visibility_changed = None
            self.on_error = self.show_error
            self.rows = {}
            self.empty = None
            self.native_voices = []
            self._voices = {service: service.builtin_voices for service in CloudVoiceService}
            self._connected = dict.fromkeys(CloudVoiceService, False)
            self.secondary_labels = []
            self.setAutoFillBackground(True)
            # The alternate inspector page must not change an existing splitter position.
            self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

            root = QVBoxLayout(self)
            root.setContentsMargins(0, 14, 0, 12)
            root.setSpacing(0)
            titles = QVBoxLayout()
            titles.setSpacing(0)
            titles.addWidget(label("Script", 14, QFont.Weight.DemiBold))
            self.subtitle = label("words to speak and pointer moves", shrink=True)
            self.subtitle.setFont(Theme.mono(10))
            titles.addWidget(self.subtitle)
            head = horizontal(9, Symbol("scroll", 15))
            head.addLayout(titles, 1)
            done = button("Done", None, "Back to the edit panel", lambda: self.on_close() if self.on_close else None)
            done.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            head.addWidget(done)
            self.inset(root, head)
            root.addSpacing(10)
            self.inset(root, Hairline())
            root.addSpacing(10)
            self.where = self.secondary("NO SLIDE", 10, QFont.Weight.DemiBold, shrink=True)
            self.inset(root, self.where)
            root.addSpacing(8)
            adds = [
                ("Speak", "speak", "Add words to be read out by text to speech"),
                ("Mouse", "move", "Add a pointer move: click on the slide for where it goes, or drag to trace its track"),
                ("Pause", "pause", "Add a pause"),
            ]
            self.inset(root, horizontal(6, *(button(title, step_symbols[kind], tip, lambda k=kind: self.add(k))
                                             for title, kind, tip in adds), stretch=True), 12)
            root.addSpacing(8)

            self.scroll = QScrollArea()
            self.scroll.setFrameShape(QFrame.Shape.NoFrame)
            self.scroll.setWidgetResizable(True)
            self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.scroll.viewport().setAutoFillBackground(False)
            column_widget = QWidget()
            self.column = QVBoxLayout(column_widget)
            self.column.setContentsMargins(8, 0, 8, 8)
            self.column.setAlignment(Qt.AlignmentFlag.AlignTop)
            self.column.setSpacing(6)
            self.scroll.setWidget(column_widget)
            column_widget.setAutoFillBackground(False)
            root.addWidget(self.scroll, 1)
            root.addSpacing(4)
            self.inset(root, Hairline())
            root.addSpacing(8)

            foot = QVBoxLayout()
            foot.setSpacing(6)
            self.play_all = button("Play Build", "play.fill", "Play every step of this build on the slide",
                                   self.play_all_pressed)
            foot.addWidget(self.play_all, alignment=Qt.AlignmentFlag.AlignLeft)
            self.voice_popup = QComboBox()
            self.voice_popup.setFont(Theme.font(11))
            self.voice_popup.setIconSize(QSize(12, 12))
            self.voice_popup.setMinimumWidth(150)
            self.voice_popup.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            self.voice_popup.setAccessibleName("Voice")
            self.voice_popup.setToolTip("The voice that reads the script (eSpeak NG supplies Linux voices; "
                                        "voices marked ✦ come from an online service and need an account)")
            self.voice_popup.activated.connect(self.voice_chosen)
            self.account_button = button("Connect…", "sparkles", "", self.connect_account)
            self.account_button.setAutoRaise(True)
            foot.addLayout(horizontal(6, self.secondary("Voice"), self.voice_popup, self.account_button, stretch=True))
            self.rate_slider = QSlider(Qt.Orientation.Horizontal)
            self.rate_slider.setRange(30, 70)
            self.rate_slider.setValue(round(store.rate * 100))
            self.rate_slider.setFixedWidth(140)
            self.rate_slider.setAccessibleName("Speaking speed")
            self.rate_slider.setToolTip("Speaking rate")
            self.rate_slider.valueChanged.connect(lambda value: self._change(lambda: store.set_rate(value / 100)))
            foot.addLayout(horizontal(6, self.secondary("Speaking speed"), self.rate_slider, stretch=True))
            self.hold_field = QLineEdit(f"{store.hold:g}")
            self.hold_field.setFont(Theme.font(11))
            self.hold_field.setAlignment(Qt.AlignmentFlag.AlignRight)
            self.hold_field.setFixedWidth(max(36, self.hold_field.fontMetrics().horizontalAdvance("00.00") + 8))
            self.hold_field.setAccessibleName("Builds with no script stay up")
            self.hold_field.setToolTip("How long a build with no script stays up in a scripted presentation")
            self.hold_field.editingFinished.connect(self.hold_changed)
            foot.addLayout(horizontal(4, self.secondary("Builds with no script stay up"), self.hold_field,
                                      self.secondary("s"), stretch=True))
            self.error = label("")
            self.error.setWordWrap(True)
            self.error.hide()
            foot.addWidget(self.error)
            self.inset(root, foot)

            self.reload()
            self.load_saved_voices()
            self.refresh_appearance()
            watch_appearance(self.refresh_appearance)
            self.voice_tasks.extend([
                spawn(self.load_voices(), "List Linux voices"),
                spawn(self.load_accounts(), "Read narration account status"),
            ])
            QApplication.instance().installEventFilter(self)

        def dispose(self):
            for task in self.voice_tasks:
                task.cancel()
            self.voice_tasks.clear()
            QApplication.instance().removeEventFilter(self)

        @staticmethod
        def inset(root, content, inset=14):
            row = horizontal(0, content)
            row.setContentsMargins(inset, 0, inset, 0)
            root.addLayout(row)

        def secondary(self, *args, **kwargs):
            widget = label(*args, **kwargs)
            self.secondary_labels.append(widget)
            return widget

        def refresh_appearance(self):
            for widget in self.secondary_labels:
                ink(widget, Theme.secondary_ink())
            ink(self.subtitle, Theme.tertiary_ink())
            ink(self.error, Theme.warning_ink())
            if self.empty is not None:
                ink(self.empty, Theme.secondary_ink())
            for row in self.rows.values():
                row.refresh_appearance()
            self.update_account_button()
            self.update()

        def show_error(self, text):
            self.error.setText(text)
            self.error.setVisible(bool(text))

        def showEvent(self, event):
            super().showEvent(event)
            if self.on_visibility_changed:
                self.on_visibility_changed(True)

        def hideEvent(self, event):
            super().hideEvent(event)
            if self.on_visibility_changed:
                self.on_visibility_changed(False)

        def hold_changed(self):
            value, ok = QLocale().toDouble(self.hold_field.text())
            if not ok:
                value, ok = QLocale.c().toDouble(self.hold_field.text())
            value = min(60, max(0, value)) if ok and math.isfinite(value) else 0
            self.hold_field.setText(f"{value:g}")
            self._change(lambda: self.store.set_hold(value))

        def end_field_edit(self):
            focused = self.window().focusWidget()
            if focused is not None and self.isAncestorOf(focused):
                focused.clearFocus()

        def eventFilter(self, watched, event):
            kind = event.type()
            if (kind not in (QEvent.Type.MouseButtonPress, QEvent.Type.FocusIn)
                    or not self.isVisible() or not isinstance(watched, QWidget)):
                return False
            view = watched
            canvas = getattr(self, "canvas_view", None)
            hit_control = False
            while view is not None:
                if isinstance(view, ScriptRow):
                    if kind == QEvent.Type.FocusIn and view.pane is self:
                        if watched is view.speech or (view.seconds is not None and
                                (watched is view.seconds or view.seconds.isAncestorOf(watched))):
                            self.select(view.id)
                    return False
                if isinstance(view, ScriptLayer):
                    return False
                hit_control |= isinstance(view, (QAbstractButton, QAbstractSlider, QAbstractSpinBox,
                                                QComboBox, QLineEdit, QPlainTextEdit))
                if view is self or view is canvas:
                    if (kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton
                            and (view is canvas or not hit_control)):
                        self.end_field_edit()
                        if view is canvas and self.capturing and self.on_capture:
                            self.on_capture(None)
                        if self.selected is not None:
                            self.select(None)
                    return False
                view = view.parentWidget()
            return False

        def _change(self, fn):
            try:
                return fn()
            except Exception as exc:
                self.on_error(str(exc))
                return None

        def load_voices(self):
            try:
                self.native_voices = yield run_blocking(NativeVoice(AppPaths.from_environment().voice_cache).voices)
                self.fill_voices()
            except Exception as exc:
                self.show_error(str(exc))

        @staticmethod
        def account_connected(service):
            return service.has_saved_key

        def load_accounts(self):
            for service in CloudVoiceService:
                self._connected[service] = yield run_blocking(self.account_connected, service)
            self.fill_voices()

        def load_saved_voices(self):
            from . import prefs
            for service in CloudVoiceService:
                saved = prefs.get_json("cloudVoices." + service.value, [])
                if isinstance(saved, list) and saved:
                    self._voices[service] = [tuple(v) for v in saved if isinstance(v, list) and len(v) == 2]
            self.fill_voices()

        def fill_voices(self):
            choice = self.voice_popup
            saved = self.store.voice
            cloud = CloudVoice.parse(saved)
            language = QLocale.system().bcp47Name().split("-")[0].lower()
            with QSignalBlocker(choice):
                choice.clear()
                choice.addItem("System Voice", None)
                native = sorted(self.native_voices, key=lambda pair: pair[1].casefold())
                native.sort(key=lambda pair: pair[0] != saved and
                            not pair[1].rsplit(" · ", 1)[-1].lower().startswith(language))
                for identifier, title in native:
                    choice.addItem(title, identifier)
                if saved and cloud is None and choice.findData(saved) < 0:
                    choice.addItem(saved + " (saved voice)", saved)
                sparkle = template_icon("sparkles", choice)
                for service in CloudVoiceService:
                    choice.insertSeparator(choice.count())
                    title = service.name + ("" if self._connected[service] else " · needs an account")
                    choice.addItem(title)
                    choice.model().item(choice.count() - 1).setEnabled(False)
                    voices = self._voices[service]
                    if cloud is not None and cloud.service is service and not any(v[0] == cloud.voice for v in voices):
                        voices = [*voices, (cloud.voice, cloud.voice)]
                    for identifier, title in voices:
                        choice.addItem(sparkle, f"{title} · {service.name}", f"cloud:{service.value}:{identifier}")
                        choice.setItemData(choice.count() - 1, "Needs an account", Qt.ItemDataRole.AccessibleDescriptionRole)
                choice.setCurrentIndex(max(0, choice.findData(saved)) if saved else 0)
            self.update_account_button()

        def update_account_button(self):
            cloud = CloudVoice.parse(self.store.voice)
            account = self.account_button
            account.setVisible(cloud is not None)
            if cloud is None:
                return
            service = cloud.service
            connected = self._connected[service]
            title = "Connected" if connected else "Connect…"
            font = Theme.font(11)
            font.setUnderline(not connected)
            account.setFont(font)
            account.setText(title)
            account.setAccessibleName(title)
            color = Theme.secondary_ink() if connected else account.palette().color(QPalette.ColorRole.Link)
            ink(account, color, QPalette.ColorRole.ButtonText)
            account.setIcon(template_icon("checkmark.circle" if connected else "sparkles", account))
            account.setToolTip(f"{service.name} is connected. Click to change the key or disconnect." if connected else
                               f"Sign in to {service.name} with an API key so this voice can read the script")

        def voice_chosen(self, index):
            self._change(lambda: self.store.set_voice(self.voice_popup.itemData(index)))
            self.update_account_button()

        def connect_account(self):
            cloud = CloudVoice.parse(self.store.voice)
            if cloud is None:
                return
            service = cloud.service

            def connect():
                try:
                    voices = yield from CloudVoiceAccount.connect(service, self)
                    if voices is None:
                        return
                    self._voices[service] = voices
                    self._connected[service] = yield run_blocking(self.account_connected, service)
                    from . import prefs
                    prefs.set("cloudVoices." + service.value, voices)
                    self.fill_voices()
                except Exception as exc:
                    self.on_error(str(exc))
            self.voice_tasks[:] = [task for task in self.voice_tasks if not task.future.done()]
            self.voice_tasks.append(spawn(connect(), "Connect narration account"))

        def show_page(self, scene, build, label):
            self.where.setText(label.upper())
            if (self.scene, self.build) != (scene, build):
                self.end_field_edit()
                self.scene, self.build, self.selected = scene, build, None
                self.reload()

        def add(self, kind):
            if not self.scene:
                return
            step = self._change(lambda: self.store.add(kind, self.scene, self.build, self.selected))
            if step:
                self.reload()
                self.select(step.id)
                row = self.rows[step.id]
                self.column.activate()
                self.scroll.ensureWidgetVisible(row, xmargin=0, ymargin=8)
                if kind == "move" and self.on_capture:
                    self.on_capture(step.id)
                if row.speech is not None:
                    row.speech.setFocus()

        def add_speak(self):
            self.add("speak")

        def add_move(self):
            self.add("move")

        def add_pause(self):
            self.add("pause")

        def set_capturing(self, id):
            self.capturing = id
            for row in self.rows.values():
                row.set_capturing(row.id == id)

        def set_playing(self, playing):
            self.playing = playing
            title = "Stop" if playing else "Play Build"
            self.play_all.setText(title)
            self.play_all.setAccessibleName(title)
            self.play_all.setIcon(template_icon("stop.fill" if playing else "play.fill", self.play_all))

        def play_all_pressed(self):
            callback = self.on_stop_playing if self.playing else self.on_play_all
            if callback:
                callback()

        def select(self, id, play=False):
            self.selected = id
            for row in self.rows.values():
                row.set_selected(row.id == id)
            if self.on_select:
                self.on_select(id)
            if play and id and self.on_play:
                step = self.store.step(id)
                if step:
                    self.on_play(step)

        def remove(self, id):
            if self.capturing == id and self.on_capture:
                self.on_capture(None)
            if self.selected == id:
                self.select(None)
            self._change(lambda: self.store.remove(id))
            self.reload()

        def shift(self, id, by):
            self._change(lambda: self.store.shift(id, by))
            self.reload()

        def reload(self):
            steps = self.store.steps_for(self.scene, self.build) if self.scene else []
            if self.column.count() and list(self.rows) == [step.id for step in steps]:
                for step in steps:
                    self.rows[step.id].refresh(step)
                return
            old_rows, self.rows = self.rows, {}
            removed = [self.empty] if self.empty is not None else []
            self.empty = None
            while self.column.count():
                self.column.takeAt(0)
            if not steps:
                self.empty = label("No script for this build. Add words to speak, pointer moves and pauses; "
                                   "Play ▸ Scripted Presentation plays them build by build.", 12)
                self.empty.setWordWrap(True)
                self.empty.setContentsMargins(6, 6, 8, 0)
                ink(self.empty, Theme.secondary_ink())
                self.column.addWidget(self.empty)
            move_number = 0
            for index, step in enumerate(steps, 1):
                move_number += step.kind == "move"
                row = old_rows.pop(step.id, None)
                if row is None:
                    row = ScriptRow(self, step, index, move_number)
                else:
                    row.renumber(index, move_number)
                    row.refresh(step)
                self.rows[step.id] = row
                self.column.addWidget(row)
            for widget in (*removed, *old_rows.values()):
                widget.hide()
                widget.deleteLater()
            self.select(self.selected if self.selected in self.rows else None)

    globals().update(ScriptLayer=ScriptLayer, ScriptPane=ScriptPane)
