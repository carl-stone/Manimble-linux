"""JSON lines to and from the engine (``python -m manimble.rpc``), over a QProcess on the GUI thread.

Requests are ``{id, cmd, ...args}``; a reply ``{id, result}`` or ``{id, error}`` completes that request's future, and
objects with an ``event`` key are emitted as ``event`` signals, exactly as the engine sends them. stdout is decoded
incrementally as UTF-8 and split on newlines; stderr is the engine's log, kept in a bounded buffer (and appended to
the log file), never parsed. When the process ends, every waiting request fails with ``EngineStopped``.
"""

from __future__ import annotations

import codecs
import collections
import json
import logging
from concurrent.futures import Future
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal, Slot

from .runtime import Runtime

log = logging.getLogger("manimble.rpc")


class EngineError(Exception):
    """The engine answered a request with an error."""


class EngineStopped(EngineError):
    """The engine is not running (it crashed, could not start, or was stopped)."""


class RpcClient(QObject):
    event = Signal(dict)                 # an asynchronous engine event, as sent
    stopped = Signal(int, str)           # exit code, last lines of the log: the engine ended without stop()
    log_line = Signal(str)               # a line the engine wrote to stderr

    LOG_LINES = 400

    def __init__(self, runtime: Runtime, log_file: Path | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self.runtime = runtime
        self.log_file = log_file
        self.proc: QProcess | None = None
        self._next = 1
        self._pending: dict[int, Future] = {}
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._err_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buf = ""
        self._err_buf = ""
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=self.LOG_LINES)
        self._stopping = False
        self._stop_waiters: list[Future] = []
        self._log_fh = None

    # ----------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self.proc is not None:
            return
        p = QProcess(self)
        env = QProcessEnvironment()
        for k, v in self.runtime.engine_env().items():
            env.insert(k, v)
        p.setProcessEnvironment(env)
        p.setProgram(self.runtime.python)
        p.setArguments(self.runtime.engine_command()[1:])
        p.readyReadStandardOutput.connect(self._read_out)
        p.readyReadStandardError.connect(self._read_err)
        p.finished.connect(self._finished)
        p.errorOccurred.connect(self._error)
        if self.log_file is not None:
            try:
                self.log_file.parent.mkdir(parents=True, exist_ok=True)
                self._log_fh = open(self.log_file, "a", encoding="utf-8", buffering=1)
                self._log_fh.write(f"[app] starting engine with {self.runtime.python}\n")
            except OSError:
                self._log_fh = None
        self._stopping = False
        self.proc = p
        p.start()

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.state() != QProcess.ProcessState.NotRunning

    def pid(self) -> int | None:
        return int(self.proc.processId()) if self.running else None

    def stop(self, wait_ms: int = 5000) -> None:
        """Close the engine and wait for it (application shutdown only; it blocks the GUI thread).

        stdin EOF lets the engine shut its render processes down; then terminate, then kill."""
        p = self.proc
        if p is None:
            return
        self._stopping = True
        if p.state() != QProcess.ProcessState.NotRunning:
            p.closeWriteChannel()
            if not p.waitForFinished(wait_ms):
                p.terminate()
                if not p.waitForFinished(2000):
                    p.kill()
                    p.waitForFinished(1000)
        self._fail_all(EngineStopped("The Manimble engine stopped"))
        self.proc = None
        self._stopped_on_request()

    def stop_async(self, wait_ms: int = 5000) -> Future:
        """Close the engine without blocking: the same EOF → terminate → kill sequence, driven by timers.

        The future settles (with None) once the process has ended; ``start()`` may be called from then on."""
        fut: Future = Future()
        fut.set_running_or_notify_cancel()
        p = self.proc
        if p is None or p.state() == QProcess.ProcessState.NotRunning:
            self.stop()
            fut.set_result(None)
            return fut
        self._stopping = True
        self._stop_waiters.append(fut)
        p.closeWriteChannel()
        # timers owned by the process object: they die with it once it has finished
        QTimer.singleShot(wait_ms, p, lambda: p.state() != QProcess.ProcessState.NotRunning and p.terminate())
        QTimer.singleShot(wait_ms + 2000, p, lambda: p.state() != QProcess.ProcessState.NotRunning and p.kill())
        return fut

    # ----------------------------------------------------------------- requests

    def request(self, cmd: str, args: dict | None = None) -> Future:
        fut: Future = Future()
        fut.set_running_or_notify_cancel()
        if not self.running:
            fut.set_exception(EngineStopped("The Manimble engine is not running"))
            return fut
        rid = self._next
        self._next += 1
        req = dict(args or {})
        req["id"] = rid
        req["cmd"] = cmd
        try:
            line = json.dumps(req, ensure_ascii=False) + "\n"
        except (TypeError, ValueError) as exc:
            fut.set_exception(EngineError(f"cannot send {cmd}: {exc}"))
            return fut
        self._pending[rid] = fut
        if self.proc.write(line.encode("utf-8")) < 0:
            self._pending.pop(rid, None)
            fut.set_exception(EngineStopped("The Manimble engine stopped"))
        return fut

    # ----------------------------------------------------------------- reading

    @Slot()
    def _read_out(self) -> None:
        if self.proc is None:
            return
        self._buf += self._decoder.decode(bytes(self.proc.readAllStandardOutput()))
        while True:
            nl = self._buf.find("\n")
            if nl < 0:
                break
            line, self._buf = self._buf[:nl], self._buf[nl + 1:]
            if line.strip():
                self._handle(line)

    def _handle(self, line: str) -> None:
        try:
            obj = json.loads(line)
        except ValueError:
            self._note(f"[app] unreadable engine line: {line[:200]}")
            return
        if not isinstance(obj, dict):
            return
        if "event" in obj:
            self.event.emit(obj)
            return
        rid = obj.get("id")
        fut = self._pending.pop(rid, None) if isinstance(rid, int) else None
        if fut is None or fut.done():
            return
        if "error" in obj:
            fut.set_exception(EngineError(str(obj["error"])))
        else:
            res = obj.get("result")
            fut.set_result(res if res is not None else {})

    @Slot()
    def _read_err(self) -> None:
        if self.proc is None:
            return
        self._err_buf += self._err_decoder.decode(bytes(self.proc.readAllStandardError()))
        while True:
            nl = self._err_buf.find("\n")
            if nl < 0:
                break
            line, self._err_buf = self._err_buf[:nl], self._err_buf[nl + 1:]
            self._note(line)
            self.log_line.emit(line)
        if len(self._err_buf) > 65536:                     # a runaway line without a newline
            self._note(self._err_buf[:65536])
            self._err_buf = ""

    def _note(self, line: str) -> None:
        self.stderr_tail.append(line)
        if self._log_fh:
            try:
                self._log_fh.write(line + "\n")
            except OSError:
                pass

    def log_tail(self, lines: int = 12) -> str:
        return "\n".join(list(self.stderr_tail)[-lines:]).strip()

    # ----------------------------------------------------------------- ending

    def _fail_all(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for f in pending.values():
            if not f.done():
                f.set_exception(exc)

    @Slot(QProcess.ProcessError)
    def _error(self, err) -> None:
        if err == QProcess.ProcessError.FailedToStart and self.proc is not None:
            self._note(f"[app] the engine could not start: {self.proc.errorString()}")
            self._finished(-1, QProcess.ExitStatus.CrashExit)

    @Slot(int, QProcess.ExitStatus)
    def _finished(self, code: int, status) -> None:
        if self.proc is None:
            return
        self._read_out()
        self._read_err()
        self._fail_all(EngineStopped("The Manimble engine stopped"))
        stopping = self._stopping
        self.proc.deleteLater()
        self.proc = None
        if not stopping:
            self.stopped.emit(int(code), self.log_tail())
            return
        self._stopped_on_request()

    def _stopped_on_request(self) -> None:
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None
        waiters, self._stop_waiters = self._stop_waiters, []
        for f in waiters:
            if not f.done():
                f.set_result(None)


def describe(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__
