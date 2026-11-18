"""GUI-thread continuations: posted callbacks never run inside the code that posted them."""

import time
import unittest
from concurrent.futures import Future

from PySide6.QtCore import QCoreApplication

from manimble_linux import tasks

def pump_until(done, seconds=2.0):
    """Run the event loop until ``done()`` (a single processEvents may first handle unrelated pending events)."""
    deadline = time.monotonic() + seconds
    while not done() and time.monotonic() < deadline:
        QCoreApplication.processEvents()


class PostTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_post_from_gui_thread_waits_for_event_loop(self):
        ran = []
        tasks.post(lambda: ran.append("posted"))
        ran.append("caller continued")
        pump_until(lambda: len(ran) == 2)
        self.assertEqual(ran, ["caller continued", "posted"])

    def test_future_settled_on_gui_thread_resumes_after_settler_returns(self):
        # e.g. RpcClient settling stop_async() inside its QProcess.finished handler: the waiting task must not
        # restart the engine before that handler has finished clearing the old process
        fut: Future = Future()
        order = []
        tasks.when_done(fut, lambda f: order.append(("resumed", f.result())))
        fut.set_result(1)
        order.append("settler finished")
        self.assertEqual(order, ["settler finished"])
        pump_until(lambda: len(order) == 2)
        self.assertEqual(order, ["settler finished", ("resumed", 1)])


if __name__ == "__main__":
    unittest.main()
