"""The worker side of ``workers.WorkerProcess``, standard library only.

Worker modules run as ``python -u -m manimble_linux.<module>``, possibly under an interpreter without Qt (the optional
extraction environment), so this module must never import PySide6 or anything that does.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Callable


def worker_main(handler: Callable[[dict, Callable[[float, str], None]], dict]) -> None:
    """``if __name__ == "__main__": worker_main(run)`` with ``run(job, progress) -> result``."""
    out = sys.stdout

    def emit(obj):
        out.write(json.dumps(obj) + "\n")
        out.flush()

    def progress(x: float, text: str = "") -> None:
        emit({"progress": x, "text": text})

    sys.stdout = sys.stderr                     # stray prints must not corrupt the protocol
    try:
        job = json.loads(sys.stdin.readline())
        emit({"result": handler(job, progress)})
    except Exception as exc:                    # noqa: BLE001
        traceback.print_exc()
        emit({"error": str(exc) or type(exc).__name__})
