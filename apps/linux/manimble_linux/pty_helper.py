"""Single-threaded PTY owner. Never import Qt here: only this process may fork.

The parent exchanges newline-delimited JSON; terminal bytes in either direction
are base64, so neither JSON framing nor partial UTF-8 can corrupt a stream.
"""
from __future__ import annotations

import argparse
import base64
import errno
import json
import os
from pathlib import Path
import pty
import selectors
import shutil
import signal
import sys
import termios
import time


def login_shell(env: dict[str, str]) -> str:
    shell = env.get("SHELL", "")
    found = shutil.which(shell, path=env.get("PATH")) if shell else None
    return found if found and os.access(found, os.X_OK) else "/bin/sh"


def processes(session: int) -> list[tuple[int, int, str]]:
    result = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            stat = (entry / "stat").read_text()
            tail = stat[stat.rfind(")") + 2:].split()
            if int(tail[3]) != session or tail[0] == "Z":
                continue
            raw = (entry / "cmdline").read_bytes().replace(b"\0", b" ").strip()
            name = raw.decode("utf-8", "replace") or stat[stat.find("(") + 1:stat.rfind(")")]
            result.append((int(entry.name), int(tail[2]), name))
        except (OSError, ValueError, IndexError):
            continue
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--original")
    parser.add_argument("--repository", action="store_true")
    args = parser.parse_args()
    env = dict(os.environ)
    env.update(TERM="xterm-256color", COLORTERM="truecolor")
    env.setdefault("MANIMBLE_DECK", args.cwd)
    if args.original:
        env["MANIMBLE_ORIGINAL_DECK"] = args.original
    shell = login_shell(env)
    directory = Path(args.cwd)
    if args.repository:
        directory = next((parent for parent in (directory, *directory.parents)
                          if (parent / ".git").exists()), directory.parent)
    pid, master = pty.fork()
    if pid == 0:
        try:
            os.chdir(directory)
            os.execve(shell, ["-" + Path(shell).name, "-i"], env)
        except OSError as exc:
            os.write(2, (str(exc) + "\r\n").encode())
            os._exit(127)

    selector = selectors.DefaultSelector()
    for fd in (0, 1, master):
        os.set_blocking(fd, False)
    selector.register(0, selectors.EVENT_READ)
    selector.register(master, selectors.EVENT_READ)
    incoming = bytearray()
    outgoing = bytearray()
    pending = bytearray()
    shutdown_at = None
    shutdown_groups: set[int] = set()
    last_status = 0.0
    child_status = None
    master_open = True
    interrupted = False

    def stop_signal(_sig, _frame):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGTERM, stop_signal)
    signal.signal(signal.SIGHUP, stop_signal)

    def emit(value):
        outgoing.extend(json.dumps(value, ensure_ascii=True).encode() + b"\n")
        try:
            selector.modify(1, selectors.EVENT_WRITE)
        except KeyError:
            selector.register(1, selectors.EVENT_WRITE)

    def send_group(group, sig):
        if group > 0 and group != os.getpgrp():
            try:
                os.killpg(group, sig)
            except ProcessLookupError:
                pass

    def begin_shutdown():
        nonlocal shutdown_at
        if shutdown_at is not None:
            return
        shutdown_at = time.monotonic()
        shutdown_groups.update(row[1] for row in processes(pid))
        shutdown_groups.add(pid)
        for group in shutdown_groups:
            send_group(group, signal.SIGHUP)
            send_group(group, signal.SIGCONT)
            send_group(group, signal.SIGTERM)

    emit({"event": "started", "pid": pid, "shell": shell})
    try:
        while True:
            now = time.monotonic()
            if interrupted:
                begin_shutdown()
            if shutdown_at is not None and now - shutdown_at >= 0.8:
                shutdown_groups.update(row[1] for row in processes(pid))
                for group in shutdown_groups:
                    send_group(group, signal.SIGKILL)
            if now - last_status >= 0.25 and master_open:
                last_status = now
                try:
                    foreground = os.tcgetpgrp(master)
                except OSError:
                    foreground = 0
                rows = processes(pid)
                commands = [name for proc, group, name in rows if proc != pid]
                label = next((name for proc, group, name in rows if group == foreground and proc != pid), None)
                emit({"event": "status", "foreground": label, "commands": commands})
            if child_status is None:
                waited, status = os.waitpid(pid, os.WNOHANG)
                if waited:
                    child_status = os.waitstatus_to_exitcode(status)
            if child_status is not None and not master_open and not outgoing:
                break
            for key, flags in selector.select(0.05):
                fd = key.fd
                if fd == 1:
                    try:
                        n = os.write(1, outgoing)
                        del outgoing[:n]
                    except BlockingIOError:
                        pass
                    except BrokenPipeError:
                        begin_shutdown()
                        outgoing.clear()
                    if not outgoing:
                        selector.unregister(1)
                elif fd == 0:
                    chunk = os.read(0, 65536)
                    if not chunk:
                        selector.unregister(0)
                        begin_shutdown()
                        continue
                    incoming.extend(chunk)
                    while b"\n" in incoming:
                        line, _, rest = incoming.partition(b"\n")
                        incoming[:] = rest
                        try:
                            msg = json.loads(line)
                            op = msg.get("op")
                            if op == "input" and master_open:
                                pending.extend(base64.b64decode(msg["data"], validate=True))
                                selector.modify(master, selectors.EVENT_READ | selectors.EVENT_WRITE)
                            elif op == "resize" and master_open:
                                rows, cols = int(msg["rows"]), int(msg["cols"])
                                if 1 <= rows <= 10000 and 1 <= cols <= 10000:
                                    termios.tcsetwinsize(master, (rows, cols))
                                    send_group(os.tcgetpgrp(master), signal.SIGWINCH)
                            elif op == "signal" and master_open:
                                sig = {"INT": signal.SIGINT, "TSTP": signal.SIGTSTP}.get(msg.get("name"))
                                if sig:
                                    send_group(os.tcgetpgrp(master), sig)
                            elif op == "stop":
                                begin_shutdown()
                        except (ValueError, KeyError, TypeError, OSError) as exc:
                            emit({"event": "error", "message": str(exc)})
                elif fd == master:
                    if flags & selectors.EVENT_WRITE and pending:
                        try:
                            n = os.write(master, pending)
                            del pending[:n]
                        except BlockingIOError:
                            pass
                        except OSError:
                            pending.clear()
                        if not pending:
                            selector.modify(master, selectors.EVENT_READ)
                    if flags & selectors.EVENT_READ:
                        try:
                            chunk = os.read(master, 65536)
                        except BlockingIOError:
                            continue
                        except OSError as exc:
                            if exc.errno != errno.EIO:
                                emit({"event": "error", "message": str(exc)})
                            chunk = b""
                        if chunk:
                            emit({"event": "output", "data": base64.b64encode(chunk).decode("ascii")})
                        else:
                            selector.unregister(master)
                            os.close(master)
                            master_open = False
            if child_status is not None and not master_open:
                # Queue the exit record after the final PTY bytes have drained.
                if child_status != "sent":
                    emit({"event": "exit", "code": child_status})
                    child_status = "sent"
    finally:
        for group in shutdown_groups | {pid}:
            send_group(group, signal.SIGKILL)
        selector.close()
        if master_open:
            os.close(master)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
