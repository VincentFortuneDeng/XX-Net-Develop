import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
XLOG_PATH = ROOT / "Coding" / "XX-Net-5.16.7.1" / "code" / "default" / "lib" / "noarch" / "xlog.py"
sys.path.insert(0, str(XLOG_PATH.parent))
SPEC = importlib.util.spec_from_file_location("xlog_bounded_writer", XLOG_PATH)
xlog = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = xlog
SPEC.loader.exec_module(xlog)


class SlowFile:
    def __init__(self, started, release):
        self.started = started
        self.release = release
        self.data = bytearray()
        self.flushes = 0

    def write(self, data):
        self.started.set()
        if not self.release.wait(2):
            raise TimeoutError("test did not release slow write")
        self.data.extend(data)
        return len(data)

    def flush(self):
        self.flushes += 1

    def close(self):
        pass


class BoundedWriterTest(unittest.TestCase):
    def setUp(self):
        self.stderr_patch = mock.patch.object(xlog.sys, "stderr", new=io.StringIO())
        self.stderr_patch.start()
        self.addCleanup(self.stderr_patch.stop)

    def test_slow_disk_does_not_block_business_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = xlog.Logger("slow", buffer_size=100, log_path=tmp, save_warning_log=True)
            started = threading.Event()
            release = threading.Event()
            logger.persistent_fd.close()
            logger.persistent_fd = SlowFile(started, release)
            first = threading.Thread(target=logger.warn, args=("first",))
            first.start()
            self.assertTrue(started.wait(1))

            begin = time.monotonic()
            second = threading.Thread(target=logger.warn, args=("second",))
            second.start()
            second.join(0.2)
            blocked = second.is_alive()
            release.set()
            first.join(1)
            second.join(1)
            if hasattr(logger, "stop"):
                logger.stop()

            self.assertFalse(blocked)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())

    def test_queue_full_is_bounded_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = xlog.Logger("bounded", buffer_size=10, log_path=tmp, save_warning_log=True)
            started = threading.Event()
            release = threading.Event()
            logger.persistent_fd.close()
            slow_file = SlowFile(started, release)
            logger.persistent_fd = slow_file
            first = threading.Thread(target=logger.warn, args=("blocking",))
            first.start()
            self.assertTrue(started.wait(1))
            for index in range(logger.write_queue_maxsize + 20):
                logger.warn("queued-%d", index)
            release.set()
            first.join(1)
            logger.stop()

            self.assertEqual(logger.write_queue.qsize(), 0)
            self.assertGreater(logger.dropped_persistent_records, 0)
            self.assertIn("dropped", slow_file.data.decode("utf-8"))

    def test_stop_preserves_order_context_and_seals_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = xlog.Logger("ordered", buffer_size=100, log_path=tmp, save_warning_log=True)
            logger.debug("before-error")
            logger.error("actual-error")
            logger.warn("after-error")
            logger.stop()
            logger.warn("after-stop")

            content = Path(tmp, "ordered.log").read_text()
            self.assertIn("ERROR_CONTEXT_BEGIN", content)
            self.assertIn("before-error", content)
            self.assertIn("actual-error", content)
            self.assertIn("ERROR_CONTEXT_END", content)
            self.assertLess(content.index("ERROR_CONTEXT_BEGIN"), content.index("actual-error"))
            self.assertLess(content.index("actual-error"), content.index("after-error"))
            self.assertNotIn("after-stop", content)

    def test_reset_flushes_pending_records_before_reopening(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = xlog.Logger("reset", buffer_size=10, log_path=tmp, save_warning_log=True)
            logger.warn("pending-before-reset")
            logger.reset_log_files()

            self.assertIn("pending-before-reset", Path(tmp, "reset.log").read_text())
            logger.stop()


if __name__ == "__main__":
    unittest.main()
