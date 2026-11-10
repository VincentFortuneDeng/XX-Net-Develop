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


class FailingFile:
    def write(self, data):
        raise OSError("injected disk full")

    def flush(self):
        pass

    def close(self):
        pass


class BoundedWriterTest(unittest.TestCase):
    def setUp(self):
        self.stderr_patch = mock.patch.object(xlog.sys, "stderr", new=io.StringIO())
        self.stderr_patch.start()
        self.addCleanup(self.stderr_patch.stop)

    def test_disk_failure_is_counted_and_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = xlog.Logger("disk-error", buffer_size=10, log_path=tmp, save_warning_log=True)
            logger.persistent_fd.close()
            logger.persistent_fd = FailingFile()

            logger.warn("accepted-before-disk-failure")
            logger.stop()

            self.assertGreaterEqual(logger.dropped_persistent_records, 1)
            self.assertIsNotNone(getattr(logger, "last_persistent_write_error", None))
            self.assertIn("persistent writer", xlog.sys.stderr.getvalue())

    def test_concurrent_stop_has_single_sentinel_and_always_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = xlog.Logger("double-stop", buffer_size=10, log_path=tmp, save_warning_log=True)
            started = threading.Event()
            release = threading.Event()
            logger.persistent_fd.close()
            logger.persistent_fd = SlowFile(started, release)
            first = threading.Thread(target=logger.warn, args=("pending",))
            first.start()
            self.assertTrue(started.wait(1))
            workers = [threading.Thread(target=logger.stop, daemon=True) for _ in range(2)]

            for worker in workers:
                worker.start()
            threading.Event().wait(0.05)
            release.set()
            first.join(1)
            for worker in workers:
                worker.join(1)

            self.assertFalse(any(worker.is_alive() for worker in workers))
            self.assertEqual(logger.write_queue.unfinished_tasks, 0)

    def test_reset_blocks_new_writes_until_pending_batch_is_reopened(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = xlog.Logger("reset-race", buffer_size=10, log_path=tmp, save_warning_log=True)
            started = threading.Event()
            release = threading.Event()
            logger.persistent_fd.close()
            logger.persistent_fd = SlowFile(started, release)
            reset_entered = threading.Event()
            original_flush = logger.flush_persistent_writes

            def mark_reset_entered():
                reset_entered.set()
                original_flush()

            logger.flush_persistent_writes = mark_reset_entered
            first = threading.Thread(target=logger.warn, args=("first",))
            first.start()
            self.assertTrue(started.wait(1))
            reset = threading.Thread(target=logger.reset_log_files)
            reset.start()
            self.assertTrue(reset_entered.wait(1))
            second = threading.Thread(target=logger.warn, args=("second",))
            second.start()
            second.join(0.05)
            blocked_during_reset = second.is_alive()
            release.set()
            first.join(1)
            reset.join(1)
            second.join(1)
            logger.stop()

            self.assertTrue(blocked_during_reset)
            self.assertIn("second", Path(tmp, "reset-race.log").read_text())

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

    def assert_registry_iteration_tolerates_new_logger(self, operation_name):
        original_registry = xlog.loggerDict
        original_default = xlog.default_log
        entered = threading.Event()
        release = threading.Event()

        class GatedLogger:
            def reset_log_files(self):
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("test did not release registry iteration")

            def keep_logs(self):
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("test did not release registry iteration")

        gated = GatedLogger()
        xlog.loggerDict = {"existing": gated}
        xlog.default_log = gated

        def restore_registry():
            release.set()
            xlog.loggerDict = original_registry
            xlog.default_log = original_default

        self.addCleanup(restore_registry)
        errors = []

        def run_operation():
            try:
                getattr(xlog, operation_name)()
            except Exception as error:
                errors.append(error)

        operation = threading.Thread(target=run_operation)
        operation.start()
        self.assertTrue(entered.wait(1))
        try:
            created = xlog.getLogger("new")
            self.assertIs(created, xlog.loggerDict["new"])
        finally:
            release.set()
        operation.join(1)
        self.assertFalse(operation.is_alive())
        self.assertEqual(errors, [])

    def test_reset_log_files_tolerates_concurrent_logger_creation(self):
        self.assert_registry_iteration_tolerates_new_logger("reset_log_files")

    def test_keep_log_tolerates_concurrent_logger_creation(self):
        self.assert_registry_iteration_tolerates_new_logger("keep_log")


if __name__ == "__main__":
    unittest.main()
