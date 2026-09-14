"""When the app is allowed to shut itself down.

The app quits once its last window closes, so it doesn't sit in the background
forever. It used to decide that from a single signal — one goodbye, one stale
timestamp, or the browser process exiting — and every one of those lies:

  * open the app twice and closing either window took the other down with it,
    which is what put "Lost connection to Kling Studio" on a window that was
    still sitting there open;
  * a reload closes the page and opens it again, which looked like a goodbye;
  * the browser process exits on its own when it hands the window to a copy of
    itself that was already running.

So the only question now is how many windows are checking in, and the answer has
to stay zero for a stretch before anything shuts down.
"""

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app  # noqa: E402


class FakeCore:
    """Core's window bookkeeping, with the batch question answerable by the test."""

    def __init__(self, busy=False):
        self.lock = threading.RLock()
        self.windows = {}
        self.busy = busy

    window_here = app.Core.window_here
    window_closed = app.Core.window_closed
    live_windows = app.Core.live_windows

    def active_runs(self):
        return ["a_batch"] if self.busy else []


class FakeServer:
    def __init__(self, busy=False):
        self.core = FakeCore(busy)
        self.stopped = threading.Event()

    def shutdown(self):
        self.stopped.set()


class ShutdownTest(unittest.TestCase):
    """Real threads, with every wait divided by the same number.

    The ratios are what matter and they have to be the real ones. A heartbeat is
    slower than the grace periods it is racing — 15 s against 5 s in the app — so a
    test whose windows ping faster than the app waits can never see the bug that put
    "Lost connection" on an open window, and will pass against the broken code.
    """

    SCALE = 1 / 40
    TICK, PING = 2 * SCALE, 15 * SCALE
    EMPTY, TIMEOUT, OPEN = 20 * SCALE, 75 * SCALE, 45 * SCALE

    def setUp(self):
        self.was = (app.MONITOR_TICK, app.EMPTY_GRACE, app.OPEN_GRACE, app.WINDOW_TIMEOUT)
        app.MONITOR_TICK, app.EMPTY_GRACE = self.TICK, self.EMPTY
        app.OPEN_GRACE, app.WINDOW_TIMEOUT = self.OPEN, self.TIMEOUT

        def restore():
            (app.MONITOR_TICK, app.EMPTY_GRACE,
             app.OPEN_GRACE, app.WINDOW_TIMEOUT) = self.was
        self.addCleanup(restore)

    def heartbeat(self, core, wid):
        """a window that stays open, checking in at the app's own pace"""
        stop = threading.Event()

        def beat():
            while not stop.is_set():
                core.window_here(wid)
                time.sleep(self.PING)

        t = threading.Thread(target=beat, daemon=True)
        t.start()
        self.addCleanup(lambda: (stop.set(), t.join(timeout=2)))

    def start(self, busy=False):
        server = FakeServer(busy)
        threading.Thread(target=app.monitor, args=(server,), daemon=True).start()
        return server

    def test_closing_one_of_two_windows_leaves_the_other_one_working(self):
        """The one that stays open is mid-heartbeat when the other one closes.

        Pinned to the worst moment on purpose. A window checks in every 15 s, so most
        of the time it is simply quiet, and the old rule read that quiet plus a single
        goodbye as "they have all gone". Letting a thread free-run here would hide the
        bug behind whether a heartbeat happened to land inside the grace period.
        """
        server = self.start()
        server.core.window_here("window-A")
        server.core.window_here("window-B")
        server.core.window_closed("window-B")     # the user closes the second window
        # A says nothing for a while — longer than any grace, but well inside the
        # timeout that decides a window has actually gone
        self.assertFalse(server.stopped.wait(self.PING * 2),
                         "the app quit while a window was still open")
        server.core.window_here("window-A")       # and A is still there, as it always was
        self.assertEqual(server.core.live_windows(), 1)

    def test_closing_the_last_window_does_shut_the_app_down(self):
        server = self.start()
        server.core.window_here("only-window")
        time.sleep(self.TICK)
        server.core.window_closed("only-window")
        self.assertTrue(server.stopped.wait(self.EMPTY * 6),
                        "the app kept running with no window left")

    def test_a_reload_is_not_a_goodbye(self):
        """Reloading fires pagehide and comes back with a new id; that must not count."""
        server = self.start()
        server.core.window_here("before-reload")
        time.sleep(self.TICK)
        server.core.window_closed("before-reload")   # pagehide
        time.sleep(self.EMPTY / 2)                   # the page is loading
        self.heartbeat(server.core, "after-reload")  # and it's back, under a new id
        self.assertFalse(server.stopped.wait(self.TIMEOUT * 1.5),
                         "a reload took the app down with it")

    def test_a_window_that_stops_checking_in_is_given_up_on(self):
        server = self.start()
        server.core.window_here("frozen-window")
        self.assertTrue(server.stopped.wait((self.TIMEOUT + self.EMPTY) * 3),
                        "a window that went silent kept the app alive")

    def test_a_window_that_never_appears_does_not_hold_the_app_open(self):
        server = self.start()                        # the browser never launched
        self.assertTrue(server.stopped.wait((self.OPEN + self.EMPTY) * 3))

    def test_a_running_batch_keeps_the_app_alive_with_no_window_at_all(self):
        server = self.start(busy=True)
        server.core.window_here("gone")
        time.sleep(self.TICK)
        server.core.window_closed("gone")
        self.assertFalse(server.stopped.wait((self.OPEN + self.EMPTY) * 2),
                         "the app quit in the middle of a batch")


class CountingTest(unittest.TestCase):
    def setUp(self):
        self.core = FakeCore()

    def test_windows_are_counted_not_just_noticed(self):
        self.core.window_here("a")
        self.core.window_here("b")
        self.assertEqual(self.core.live_windows(), 2)
        self.core.window_closed("a")
        self.assertEqual(self.core.live_windows(), 1)

    def test_the_same_window_saying_hello_twice_is_still_one_window(self):
        self.core.window_here("a")
        self.core.window_here("a")
        self.assertEqual(self.core.live_windows(), 1)

    def test_a_goodbye_from_a_window_that_was_never_here_changes_nothing(self):
        self.core.window_here("a")
        self.core.window_closed("someone-else")
        self.assertEqual(self.core.live_windows(), 1)

    def test_a_silent_window_is_forgotten_after_the_timeout(self):
        self.core.window_here("a")
        self.core.windows["a"] = time.time() - (app.WINDOW_TIMEOUT + 1)
        self.assertEqual(self.core.live_windows(), 0)

    def test_a_page_too_old_to_name_itself_still_counts_as_a_window(self):
        class H:
            query = {}
        self.assertEqual(app.window_id(H()), "window")

    def test_a_window_id_is_taken_as_given_but_kept_short(self):
        class H:
            query = {"w": ["  abc  "]}
        self.assertEqual(app.window_id(H()), "abc")

        class Long:
            query = {"w": ["x" * 500]}
        self.assertEqual(len(app.window_id(Long())), 64)


if __name__ == "__main__":
    unittest.main()
