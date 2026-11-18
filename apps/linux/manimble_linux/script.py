"""Portable script.json, deterministic motion, and the editor/show script scheduler.

The data and motion portion deliberately has no Qt imports. Widgets are loaded
lazily; movie workers and portable-project tests can use the same mathematics.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from enum import Enum
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


class ScriptPointer:
    vertices = [(0, 0), (0, 17), (4, 13.2), (7, 19.6), (9.4, 18.6), (6.5, 12.2), (12, 12.2)]

    def __init__(self):
        self.point = None
        self.rect = None

    def place(self, point, rect):
        self.point, self.rect = point, rect

    @classmethod
    def polygon(cls, point, rect):
        x, y, w, h = rect
        k = max(14, h * 0.045) / 20
        px, py = _xy(point)
        return [(x + w * px + vx * k, y + h * py + vy * k) for vx, vy in cls.vertices]

    def paint(self, painter):
        if self.point is None or self.rect is None or self.rect.width() <= 0:
            return
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QColor, QPen, QPolygonF
        r = self.rect
        polygon = QPolygonF([QPointF(*p) for p in self.polygon(self.point, (r.x(), r.y(), r.width(), r.height()))])
        painter.save()
        painter.setPen(QPen(QColor("black"), max(1, 1.1 * max(14, r.height() * 0.045) / 20)))
        painter.setBrush(QColor("white"))
        painter.drawPolygon(polygon)
        painter.restore()


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
    auto_pending: bool = False
    movie_export: object = None


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
        self.stop_script_preview()
        if state.runner:
            state.runner.stop()
        if state.pane:
            state.pane.hide()
            index = self.drawer.tabs.indexOf(state.pane)
            if index >= 0:
                self.drawer.tabs.removeTab(index)
            self.drawer.pages.pop("script", None)
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
        if hasattr(self, "actions"):
            self.actions.refresh()

    def _script_visibility_changed(self):
        state = self._script_state()
        if state.pane and state.pane.isVisible():
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
        label = self.scene_name or "No slide"
        if builds > 1:
            label += f" · build {self.stop_index + 1} of {builds}"
        pane.show_page(self.scene_name, self.stop_index, label)
        self.sync_script_layer()

    def sync_script_layer(self):
        state = self._script_state()
        if state.layer:
            opened = state.pane is not None and state.pane.isVisible()
            state.layer.setVisible(opened)
            state.layer.steps = state.store.steps_for(self.scene_name, self.stop_index) if opened and state.store else []
            state.layer.selected = state.pane.selected if opened else None
            state.layer.update()
            state.layer.raise_()

    def view_script(self, sender=None):
        state = self._script_state()
        if state.pane and not state.pane.isHidden():
            self.hide_script_pane()
            return
        if state.store is None:
            return
        if state.pane is None:
            pane = __getattr__("ScriptPane")(state.store, self.window)
            pane.on_close = self.hide_script_pane
            pane.on_visibility_changed = self._script_visibility_changed
            pane.on_play = self.play_script_step
            pane.on_play_all = self.play_script_build
            pane.on_stop_playing = self.stop_script_preview
            pane.on_capture = self.set_script_capture
            pane.on_select = self._script_selected
            pane.on_error = lambda text: self.alert("Script", text)
            pane.settings = self.settings
            pane.load_saved_voices()
            pane.canvas_view = self.canvas
            state.pane = pane
            self.drawer.add_page("script", pane, "Script")
        self.drawer.show_page("script")
        state.pane.show()
        self.sync_script_page()

    def _script_selected(self, id):
        if self.script.layer:
            self.script.layer.selected = id
            self.script.layer.update()

    def hide_script_pane(self):
        state = self._script_state()
        self.stop_script_preview()
        self.set_script_capture(None)
        if state.pane:
            state.pane.hide()
            self.drawer.hide()
        self.sync_script_layer()

    def set_script_capture(self, id):
        state = self._script_state()
        if state.pane:
            state.pane.capturing = id
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
        self._preview_steps(performer, [step])

    def play_script_build(self):
        if not self.scene_name or not self._script_state().store:
            return
        steps = self.script.store.steps_for(self.scene_name, self.stop_index)
        performer = self.make_preview()
        if performer:
            self._preview_steps(performer, steps)

    def _preview_steps(self, performer, steps):
        from .tasks import spawn
        state = self.script
        if state.pane:
            state.pane.playing = True
            state.pane.play_all.setText("Stop")

        def play():
            try:
                for step in steps:
                    if performer.stopped:
                        break
                    if state.pane:
                        state.pane.select(step.id, False)
                    yield from performer.perform(step)
                if not performer.stopped:
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
                state.pane.playing = False
                state.pane.play_all.setText("Play Build")

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
            self.toast(f"No script yet: each build stays up {self.script.store.hold:g} s")
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

    def _register_script_movie(self, export):
        state = self._script_state()
        state.movie_export = export
        export.on_progress = self._script_movie_progress
        if getattr(state, "movie_cancelled", False):
            export.cancel()

    def _script_movie_progress(self, fraction, text):
        state = self._script_state()
        dialog = getattr(state, "movie_progress", None)
        if dialog:
            dialog.setLabelText(text)
            if fraction is not None:
                dialog.setRange(0, 100)
                dialog.setValue(round(fraction * 100))
        self.toast(text)

    def prepare_script_movie(self, destination, scenes, height=720, fps=30):
        from .models import frame_size
        from .script_movie import ScriptMovieExport
        state = self._script_state()
        if state.store is None or state.store.is_empty:
            raise ValueError("This deck has no script")
        width, frame_height = frame_size(self.scene)
        export = yield from ScriptMovieExport.prepare(state.store, scenes, (round(height * 16 / 9), height),
            width / frame_height, fps, destination, self._script_voices(),
            lambda done, total: self._script_movie_progress(None, f"Preparing narration {done}/{total}"),
            on_created=self._register_script_movie)
        state.movie_export = export
        return export

    def finish_script_movie(self, segments):
        state = self._script_state()
        export = state.movie_export
        if export is None:
            return
        try:
            return (yield from export.finish(segments, self.runtime.python, self.runtime.child_env()))
        finally:
            if state.movie_export is export:
                state.movie_export = None

    def export_scripted_movie(self, request, *, store, scenes, to):
        from concurrent.futures import Future
        from .workers import run_blocking
        from PySide6.QtWidgets import QProgressDialog
        state = self._script_state()
        if getattr(state, "movie_active", False):
            raise RuntimeError("A scripted movie export is already running")
        state.movie_active = True
        state.movie_cancelled = False
        dialog = QProgressDialog("Preparing narration…", "Cancel", 0, 0, self.window)
        dialog.setWindowTitle("Export Movie with Script")
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.canceled.connect(self.cancel_script_movie)
        state.movie_progress = dialog
        dialog.show()
        export = None
        done = Future()
        finishing = False

        def event(message):
            if message.get("event") != "export_movie" or done.done():
                return
            phase = message.get("phase")
            if phase == "done" and export and message.get("path") == str(export.base):
                done.set_result(message)
            elif phase in ("error", "failed"):
                done.set_exception(RuntimeError(message.get("error", "Movie rendering failed")))
            elif phase == "cancelled":
                done.set_exception(RuntimeError("Movie export cancelled"))
            elif phase:
                self._script_movie_progress(None, "Movie: " + str(phase))

        def stopped(code, text):
            if not done.done():
                done.set_exception(RuntimeError(text or f"Engine stopped ({code})"))

        try:
            export = yield from self.prepare_script_movie(to, scenes, request.get("height", 720), request.get("fps", 30))
            self.rpc.event.connect(event)
            self.rpc.stopped.connect(stopped)
            state.movie_done = done
            reply = yield self.call("export_movie", {**request, "path": str(export.base),
                "holds": export.holds, "hold": export.unscripted, "scenes": scenes})
            if reply.get("ok") is False:
                raise RuntimeError(reply.get("error", "Movie export failed"))
            completed = yield done
            if export.cancelled:
                raise RuntimeError("Movie export cancelled")
            finishing = True
            return (yield from self.finish_script_movie(completed["segments"]))
        finally:
            try:
                self.rpc.event.disconnect(event)
                self.rpc.stopped.disconnect(stopped)
            except RuntimeError:
                pass
            state.movie_done = None
            state.movie_export = None
            state.movie_active = False
            state.movie_progress = None
            dialog.close()
            dialog.deleteLater()
            if export and not finishing:
                run_blocking(export.discard)

    def cancel_script_movie(self):
        state = self._script_state()
        state.movie_cancelled = True
        if state.movie_export:
            if getattr(state, "movie_done", None) is not None and not state.movie_done.done():
                state.movie_done.set_exception(RuntimeError("Movie export cancelled"))
                self.call("cancel")
            state.movie_export.cancel()
            state.movie_export = None


def __getattr__(name):
    if name not in ("ScriptLayer", "ScriptPane"):
        raise AttributeError(name)
    if "ScriptLayer" not in globals():
        _load_widgets()
    return globals()[name]


def _load_widgets():
    from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF, QRegion
    from PySide6.QtWidgets import (QApplication, QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout,
        QLabel, QPlainTextEdit, QPushButton, QScrollArea, QSlider, QVBoxLayout, QWidget)
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
            self.pointer = ScriptPointer()
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
            self.pointer.place(point, self.slide_rect())
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
            accent = QColor("#65A9FF")
            previous = None
            number = 0
            for step in self.steps:
                if step.kind != "move":
                    continue
                number += 1
                if not step.points:
                    continue
                points = step.points
                origin, previous = previous or points[0], points[-1]
                if step.id not in (self.selected, self.capturing):
                    continue
                path = QPainterPath(self._view(origin, r))
                for p in points:
                    path.lineTo(self._view(p, r))
                pen = QPen(accent, 2.5 if step.id == self.selected else 1.5, Qt.PenStyle.DashLine)
                pen.setDashPattern([5, 4])
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(path)
                end = self._view(points[-1], r)
                painter.setBrush(accent)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(end, 9, 9)
                painter.setPen(QColor("white"))
                painter.drawText(QRectF(end.x() - 9, end.y() - 9, 18, 18), Qt.AlignmentFlag.AlignCenter, str(number))
            if self.capturing:
                painter.setPen(QPen(accent, 3))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(r.adjusted(1.5, 1.5, -1.5, -1.5))
                if len(self.trace) > 1:
                    painter.drawPolyline(QPolygonF([self._view(p, r) for p in self.trace]))
                painter.drawText(r.adjusted(8, 8, -8, -8), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter,
                                 "Click for a target, or drag to trace the pointer track")
            self.pointer.rect = r
            self.pointer.paint(painter)
            if self.paused:
                painter.fillRect(QRectF(16, 16, 250, 30), QColor(0, 0, 0, 180))
                painter.setPen(QColor("white"))
                painter.drawText(QRectF(16, 16, 250, 30), Qt.AlignmentFlag.AlignCenter, "Paused · Space to resume")
            painter.end()

        def mousePressEvent(self, event):
            if self.capturing and self.slide_rect().contains(event.position()):
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

    class ScriptPane(QWidget):
        def __init__(self, store, parent=None):
            super().__init__(parent)
            self.store = store
            self.scene, self.build, self.selected, self.capturing = None, 0, None, None
            self.playing = False
            self.settings = None
            self.on_close = self.on_play = self.on_play_all = self.on_stop_playing = None
            self.on_capture = self.on_select = None
            self.on_visibility_changed = None
            self.on_error = lambda text: self.error.setText(text)
            self.rows = {}
            self._voices = {s: s.builtin_voices for s in CloudVoiceService}
            root = QVBoxLayout(self)
            head = QHBoxLayout()
            head.addWidget(QLabel("Script · words and pointer moves"), 1)
            self._button(head, "Done", lambda: self.on_close() if self.on_close else None)
            root.addLayout(head)
            self.where = QLabel("No slide")
            root.addWidget(self.where)
            buttons = QHBoxLayout()
            for title, kind in (("Speak", "speak"), ("Mouse", "move"), ("Pause", "pause")):
                self._button(buttons, title, lambda checked=False, k=kind: self.add(k))
            root.addLayout(buttons)
            self.scroll = QScrollArea()
            self.scroll.setWidgetResizable(True)
            self.column_widget = QWidget()
            self.column = QVBoxLayout(self.column_widget)
            self.column.setAlignment(Qt.AlignmentFlag.AlignTop)
            self.scroll.setWidget(self.column_widget)
            root.addWidget(self.scroll, 1)
            foot = QHBoxLayout()
            self.play_all = self._button(foot, "Play Build", self.play_all_pressed)
            self.voice_popup = QComboBox()
            self.voice_popup.currentIndexChanged.connect(self.voice_chosen)
            foot.addWidget(self.voice_popup, 1)
            self.account_button = self._button(foot, "Account…", self.connect_account)
            root.addLayout(foot)
            timing = QHBoxLayout()
            timing.addWidget(QLabel("Speaking speed"))
            self.rate_slider = QSlider(Qt.Orientation.Horizontal)
            self.rate_slider.setRange(30, 70)
            self.rate_slider.setValue(round(store.rate * 100))
            self.rate_slider.valueChanged.connect(self.rate_changed)
            timing.addWidget(self.rate_slider)
            timing.addWidget(QLabel("Empty build hold (s)"))
            self.hold_field = QDoubleSpinBox()
            self.hold_field.setRange(0, 60)
            self.hold_field.setValue(store.hold)
            self.hold_field.valueChanged.connect(self.hold_changed)
            timing.addWidget(self.hold_field)
            root.addLayout(timing)
            self.error = QLabel()
            self.error.setWordWrap(True)
            self.error.setStyleSheet("color: #e8a046")
            root.addWidget(self.error)
            self.native_voices = []
            self.fill_voices()
            spawn(self.load_voices(), "List Linux voices")
            QApplication.instance().installEventFilter(self)

        def showEvent(self, event):
            super().showEvent(event)
            if self.on_visibility_changed:
                self.on_visibility_changed()

        def hideEvent(self, event):
            super().hideEvent(event)
            if self.on_visibility_changed:
                self.on_visibility_changed()

        def rate_changed(self, value=None):
            value = self.rate_slider.value() if value is None else value
            self._change(lambda: self.store.set_rate(value / 100))

        def hold_changed(self, value=None):
            value = self.hold_field.value() if value is None else value
            self._change(lambda: self.store.set_hold(min(60, max(0, value))))

        def eventFilter(self, watched, event):
            if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.FocusIn) and self.isVisible() and isinstance(watched, QWidget):
                row_id = next((id for id, row in self.rows.items()
                               if row[0] is watched or row[0].isAncestorOf(watched)), None)
                in_row = row_id is not None
                if row_id:
                    step = self.store.step(row_id)
                    play = (event.type() == QEvent.Type.MouseButtonPress
                            and (watched is self.rows[row_id][0] or isinstance(watched, QLabel))
                            and step is not None and step.kind != "speak")
                    self.select(row_id, play)
                canvas = getattr(self, "canvas_view", None)
                in_canvas = canvas is not None and (watched is canvas or canvas.isAncestorOf(watched))
                if event.type() == QEvent.Type.MouseButtonPress and not in_row and (watched is self.column_widget or in_canvas and not isinstance(watched, ScriptLayer)):
                    self.select(None, False)
                    if self.on_capture:
                        self.on_capture(None)
            return False

        def _button(self, layout, title, callback):
            button = QPushButton(title)
            button.clicked.connect(callback)
            layout.addWidget(button)
            return button

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
                self.error.setText(str(exc))

        def load_saved_voices(self):
            from . import prefs
            for service in CloudVoiceService:
                saved = prefs.get_json("cloudVoices." + service.value, [])
                if isinstance(saved, list) and saved:
                    self._voices[service] = [tuple(v) for v in saved if isinstance(v, list) and len(v) == 2]
            self.fill_voices()

        def fill_voices(self):
            self.voice_popup.blockSignals(True)
            self.voice_popup.clear()
            self.voice_popup.addItem("System Voice", None)
            for identifier, title in self.native_voices:
                self.voice_popup.addItem(title, identifier)
            for service in CloudVoiceService:
                for identifier, title in self._voices[service]:
                    self.voice_popup.addItem(f"✦ {title} · {service.name}", f"cloud:{service.value}:{identifier}")
            selected = self.voice_popup.findData(self.store.voice)
            if selected < 0 and self.store.voice:
                self.voice_popup.addItem(self.store.voice + " (saved voice)", self.store.voice)
                selected = self.voice_popup.count() - 1
            self.voice_popup.setCurrentIndex(max(0, selected))
            self.voice_popup.blockSignals(False)
            self.account_button.setVisible(CloudVoice.parse(self.store.voice) is not None)

        def voice_chosen(self, index):
            self._change(lambda: self.store.set_voice(self.voice_popup.itemData(index)))
            self.account_button.setVisible(CloudVoice.parse(self.store.voice) is not None)

        def connect_account(self):
            cloud = CloudVoice.parse(self.store.voice)
            if cloud is None:
                return

            def connect():
                try:
                    voices = yield from CloudVoiceAccount.connect(cloud.service, self)
                    if voices:
                        self._voices[cloud.service] = voices
                        if self.settings:
                            from . import prefs
                            prefs.set("cloudVoices." + cloud.service.value, voices)
                        self.fill_voices()
                except Exception as exc:
                    self.on_error(str(exc))
            spawn(connect(), "Connect narration account")

        def show_page(self, scene, build, label):
            self.where.setText(label)
            if (self.scene, self.build) != (scene, build):
                self.scene, self.build, self.selected = scene, build, None
                self.reload()

        def add(self, kind):
            if not self.scene:
                return
            step = self._change(lambda: self.store.add(kind, self.scene, self.build, self.selected))
            if step:
                self.reload()
                self.select(step.id, False)
                if kind == "move" and self.on_capture:
                    self.on_capture(step.id)
                if kind == "speak":
                    self.rows[step.id][1].setFocus()

        def add_speak(self):
            self.add("speak")

        def add_move(self):
            self.add("move")

        def add_pause(self):
            self.add("pause")

        def play_all_pressed(self):
            callback = self.on_stop_playing if self.playing else self.on_play_all
            if callback:
                callback()

        def select(self, id, play=False):
            self.selected = id
            for key, (row, control) in self.rows.items():
                row.setStyleSheet("QFrame { border: 1px solid #65A9FF; }" if key == id else "")
            if self.on_select:
                self.on_select(id)
            if play and id and self.on_play:
                self.on_play(self.store.step(id))

        def remove(self, id):
            if self.capturing == id and self.on_capture:
                self.on_capture(None)
            if self.selected == id:
                self.select(None, False)
            self._change(lambda: self.store.remove(id))
            self.reload()

        def shift(self, id, by):
            self._change(lambda: self.store.shift(id, by))
            self.reload()

        def reload(self):
            steps = self.store.steps_for(self.scene, self.build) if self.scene else []
            if self.rows and list(self.rows) == [s.id for s in steps]:
                for step in steps:
                    control = self.rows[step.id][1]
                    if control.hasFocus():
                        continue
                    if step.kind == "speak" and control.toPlainText() != step.text:
                        control.blockSignals(True)
                        control.setPlainText(step.text)
                        control.blockSignals(False)
                    elif step.kind == "move":
                        control.setText("No target yet" if not step.points else f"Track · {len(step.points)} points")
                return
            while self.column.count():
                item = self.column.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            self.rows = {}
            if not steps:
                label = QLabel("No script for this build. Add speech, pointer moves, or pauses.")
                label.setWordWrap(True)
                self.column.addWidget(label)
            for i, step in enumerate(steps):
                row = QFrame()
                layout = QVBoxLayout(row)
                head = QHBoxLayout()
                head.addWidget(QLabel(f"{i + 1}. {step.kind.capitalize()}"), 1)
                self._button(head, "Play", lambda checked=False, id=step.id: self.select(id, True))
                self._button(head, "↑", lambda checked=False, id=step.id: self.shift(id, -1))
                self._button(head, "↓", lambda checked=False, id=step.id: self.shift(id, 1))
                self._button(head, "Delete", lambda checked=False, id=step.id: self.remove(id))
                layout.addLayout(head)
                if step.kind == "speak":
                    control = QPlainTextEdit(step.text)
                    control.setPlaceholderText("Words to speak…")
                    control.setMaximumHeight(120)
                    control.textChanged.connect(lambda id=step.id, c=control: self._change(lambda: self.store.update(id, text=c.toPlainText())))
                    layout.addWidget(control)
                elif step.kind == "pause":
                    control = QDoubleSpinBox()
                    control.setRange(0, 600)
                    control.setSuffix(" seconds")
                    control.setValue(step.seconds)
                    control.valueChanged.connect(lambda v, id=step.id: self._change(lambda: self.store.update(id, seconds=v)))
                    layout.addWidget(control)
                else:
                    cap = QHBoxLayout()
                    self._button(cap, "Set on Slide", lambda checked=False, id=step.id: self.on_capture(None if self.capturing == id else id) if self.on_capture else None)
                    control = QLabel("No target yet" if not step.points else f"Track · {len(step.points)} points")
                    cap.addWidget(control)
                    layout.addLayout(cap)
                    motion = QHBoxLayout()
                    motion.addWidget(QLabel("Speed"))
                    speed = QComboBox()
                    for title, value in ScriptStep.speeds:
                        speed.addItem(title, value)
                    speed.setCurrentIndex(min(range(len(ScriptStep.speeds)), key=lambda k: abs(ScriptStep.speeds[k][1] - step.speed)))
                    speed.currentIndexChanged.connect(lambda index, id=step.id, c=speed: self._change(lambda: self.store.update(id, speed=c.itemData(index))))
                    motion.addWidget(speed)
                    motion.addWidget(QLabel("Wobble"))
                    noise = QSlider(Qt.Orientation.Horizontal)
                    noise.setRange(0, 100)
                    noise.setValue(round(step.noise * 100))
                    noise.valueChanged.connect(lambda v, id=step.id: self._change(lambda: self.store.update(id, noise=v / 100)))
                    motion.addWidget(noise)
                    layout.addLayout(motion)
                self.column.addWidget(row)
                self.rows[step.id] = (row, control)
            self.select(self.selected if self.selected in self.rows else None, False)

    globals().update(ScriptLayer=ScriptLayer, ScriptPane=ScriptPane)
