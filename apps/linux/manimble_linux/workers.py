"""Work that must stay off the GUI thread.

* ``run_blocking(fn, *args)``: filesystem copying/scanning and blocking HTTP, on a bounded pool of two threads. The
  returned future settles on that thread; ``yield`` it from a task (tasks.py) or use ``tasks.when_done`` to get back
  onto the GUI thread before touching widgets or state.
* ``WorkerProcess``: CPU-heavy work (PatchMatch fill, narrated movie composition, model inference) in a separate,
  cancellable Python process. The worker module calls ``worker_protocol.worker_main`` (stdlib only, so workers may run
  under an interpreter without Qt): it reads one JSON job from stdin and writes JSON lines to stdout:
  ``{"progress": x, "text": "..."}`` any number of times, then exactly one ``{"result": {...}}`` or
  ``{"error": "..."}``. Cancelling kills the process; the caller's generation token makes late results ignorable.
"""

from __future__ import annotations

import codecs
import json
import os
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from typing import Callable

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal, Slot

from . import paths

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="manimble-io")


def run_blocking(fn: Callable, *args, **kwargs) -> Future:
    return _pool.submit(fn, *args, **kwargs)


def shutdown() -> None:
    _pool.shutdown(wait=False, cancel_futures=True)


class WorkerCancelled(CancelledError):
    pass


class WorkerProcess(QObject):
    """One run of ``python -u -m manimble_linux.<module>`` with a JSON job on stdin."""

    progress = Signal(float, str)

    def __init__(self, python: str, module: str, job: dict, env: dict[str, str] | None = None,
                 parent: QObject | None = None):
        super().__init__(parent)
        self.python, self.module, self.job = python, module, job
        self.env = dict(os.environ if env is None else env)
        # the front end's package must be importable by the worker's Python
        pp = [str(paths.LINUX_DIR)] + [p for p in self.env.get("PYTHONPATH", "").split(":") if p]
        self.env["PYTHONPATH"] = ":".join(dict.fromkeys(pp))
        self.env["PYTHONUNBUFFERED"] = "1"
        self.env.pop("PYTHONHOME", None)
        self.future: Future = Future()
        self.future.set_running_or_notify_cancel()
        self.proc: QProcess | None = None
        self._buf = ""
        self._dec = codecs.getincrementaldecoder("utf-8")("replace")
        self.stderr = ""
        self._outcome: tuple[str, object] | None = None
        self._cancelled = False

    def start(self) -> Future:
        p = QProcess(self)
        env = QProcessEnvironment()
        for k, v in self.env.items():
            env.insert(k, v)
        p.setProcessEnvironment(env)
        p.setProgram(self.python)
        p.setArguments(["-u", "-m", f"manimble_linux.{self.module}"])
        p.readyReadStandardOutput.connect(self._out)
        p.readyReadStandardError.connect(self._err)
        p.finished.connect(self._done)
        p.errorOccurred.connect(self._error)
        self.proc = p
        p.start()
        p.write((json.dumps(self.job) + "\n").encode("utf-8"))
        p.closeWriteChannel()
        return self.future

    def cancel(self) -> None:
        self._cancelled = True
        if self.proc is not None and self.proc.state() != QProcess.ProcessState.NotRunning:
            self.proc.kill()
        elif not self.future.done():
            self.future.set_exception(WorkerCancelled("cancelled"))

    @Slot()
    def _out(self) -> None:
        self._buf += self._dec.decode(bytes(self.proc.readAllStandardOutput()))
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "progress" in msg:
                self.progress.emit(float(msg.get("progress") or 0), str(msg.get("text") or ""))
            elif "result" in msg:
                self._outcome = ("result", msg["result"])
            elif "error" in msg:
                self._outcome = ("error", str(msg["error"]))

    @Slot()
    def _err(self) -> None:
        self.stderr = (self.stderr + bytes(self.proc.readAllStandardError()).decode("utf-8", "replace"))[-20000:]

    @Slot(QProcess.ProcessError)
    def _error(self, err) -> None:
        if err == QProcess.ProcessError.FailedToStart and not self.future.done():
            self.future.set_exception(RuntimeError(f"could not start {self.python}: {self.proc.errorString()}"))

    @Slot(int, QProcess.ExitStatus)
    def _done(self, code, status) -> None:
        self._out()
        if self.future.done():
            return
        if self._cancelled:
            self.future.set_exception(WorkerCancelled("cancelled"))
        elif self._outcome and self._outcome[0] == "result":
            self.future.set_result(self._outcome[1])
        elif self._outcome:
            self.future.set_exception(RuntimeError(self._outcome[1]))
        else:
            tail = "\n".join(self.stderr.strip().splitlines()[-14:])
            self.future.set_exception(RuntimeError(f"{self.module} stopped (exit {code})" + (f":\n{tail}" if tail else "")))
