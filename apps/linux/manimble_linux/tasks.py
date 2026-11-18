"""Continuations on the GUI thread, without a second event loop.

A *task* is a generator function run step by step on the Qt GUI thread. It ``yield``s a
``concurrent.futures.Future`` (an RPC reply, a worker result, ``sleep(...)``) or another ``Task``; when that settles,
the generator is resumed with its result, or the exception is thrown into it at the ``yield``. The Swift app's
``async``/``await`` code ports to this one-to-one::

    def load(self):
        sc = yield self.rpc.request("scene", {"name": name})
        ...
    spawn(self.load())

Futures may complete on any thread: resumption is always posted to the GUI thread through a queued Qt signal.
``SerialQueue`` runs submitted tasks strictly one after another (the controller's edit queue).
"""

from __future__ import annotations

import logging
import threading
import types
from concurrent.futures import CancelledError, Future
from typing import Any, Callable, Generator

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, QTimer, Signal, Slot

log = logging.getLogger("manimble.tasks")


class _Poster(QObject):
    posted = Signal(object)

    def __init__(self):
        super().__init__()
        # queued even on the GUI thread: a direct call would run the callback inside whatever emitted it
        self.posted.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @Slot(object)
    def _run(self, fn):
        try:
            fn()
        except Exception:                                    # noqa: BLE001
            log.exception("posted callback failed")


_poster: _Poster | None = None
_poster_lock = threading.Lock()


def _get_poster() -> _Poster:
    global _poster
    with _poster_lock:
        if _poster is None:
            app = QCoreApplication.instance()
            if app is None:
                raise RuntimeError("tasks need a QApplication")
            p = _Poster()
            p.moveToThread(app.thread())
            _poster = p
        return _poster


def on_gui_thread() -> bool:
    app = QCoreApplication.instance()
    return app is not None and QThread.currentThread() is app.thread()


def post(fn: Callable[[], Any]) -> None:
    """Run ``fn`` on the GUI thread, after the current event (queued even when called from the GUI thread)."""
    _get_poster().posted.emit(fn)


def when_done(fut: Future, fn: Callable[[Future], Any]) -> None:
    """``fn(fut)`` on the GUI thread once ``fut`` settles."""
    fut.add_done_callback(lambda f: post(lambda: fn(f)))


def resolved(value: Any = None) -> Future:
    f: Future = Future()
    f.set_result(value)
    return f


def failed(exc: BaseException) -> Future:
    f: Future = Future()
    f.set_exception(exc)
    return f


def sleep(seconds: float) -> Future:
    """A future that completes after ``seconds`` on the GUI thread's timer."""
    f: Future = Future()
    QTimer.singleShot(max(0, int(seconds * 1000)), lambda: f.done() or f.set_result(None))
    return f


class Task:
    """A generator stepped on the GUI thread; ``future`` settles with its return value or exception."""

    def __init__(self, gen: Generator, name: str = ""):
        if not isinstance(gen, types.GeneratorType):
            raise TypeError(f"Task needs a generator, got {type(gen).__name__}")
        self.gen = gen
        self.name = name or getattr(gen, "__qualname__", "task")
        self.future: Future = Future()
        self._cancelled = False

    def start(self) -> "Task":
        post(lambda: self._step(None, None))
        return self

    def cancel(self) -> None:
        """Stop at the next resumption: the generator is closed (``finally`` blocks run)."""
        self._cancelled = True

    def _step(self, value, exc) -> None:
        while True:
            if self._cancelled:
                self.gen.close()
                if not self.future.done():
                    self.future.cancel()
                    self.future.set_running_or_notify_cancel()
                return
            try:
                awaited = self.gen.throw(exc) if exc is not None else self.gen.send(value)
            except StopIteration as stop:
                if not self.future.done():
                    self.future.set_result(stop.value)
                return
            except BaseException as e:                           # noqa: BLE001
                if not isinstance(e, CancelledError):
                    log.exception("task %s failed", self.name)
                if not self.future.done():
                    self.future.set_exception(e)
                return
            if isinstance(awaited, Task):
                awaited = awaited.future
            if awaited is None:
                awaited = resolved(None)
            if not isinstance(awaited, Future):
                value, exc = None, TypeError(f"task {self.name} yielded {awaited!r}, not a Future or Task")
                continue
            if awaited.done():
                value, exc = _outcome(awaited)
                continue
            when_done(awaited, self._resume)
            return

    def _resume(self, fut: Future) -> None:
        value, exc = _outcome(fut)
        self._step(value, exc)


def _outcome(fut: Future):
    if fut.cancelled():
        return None, CancelledError()
    e = fut.exception()
    return (None, e) if e is not None else (fut.result(), None)


def spawn(gen: Generator, name: str = "") -> Task:
    """Start a task on the GUI thread (its first step runs after the current event)."""
    return Task(gen, name).start()


def wait_quietly(fut: Future):
    """A task step that waits for ``fut`` and ignores how it ended: ``yield from wait_quietly(f)``."""
    try:
        yield fut
    except BaseException:                                       # noqa: BLE001
        pass


class SerialQueue:
    """Runs submitted tasks one after another, each with the context captured when it was submitted.

    ``context`` is read while the task runs (``queue.context``), like MainController.submitContext in the Swift app.
    """

    def __init__(self):
        self._tail: Future = resolved(None)
        self.pending = 0
        self.context: Any = None

    def submit(self, make: Callable[[], Generator], context: Any = None) -> Future:
        prev = self._tail
        self.pending += 1

        def run():
            yield from wait_quietly(prev)
            self.context = context
            try:
                result = yield spawn(make())
                return result
            finally:
                self.context = None
                self.pending -= 1

        task = spawn(run(), "submit")
        self._tail = task.future
        return task.future

    @property
    def tail(self) -> Future:
        return self._tail

    def drained(self) -> Generator:
        """``yield from queue.drained()``: until every task submitted so far has finished."""
        while self.pending > 0:
            yield from wait_quietly(self._tail)
