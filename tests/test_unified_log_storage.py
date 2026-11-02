#!/usr/bin/env python3
"""O 阶段统一日志正文、持久化与 ERROR 上下文的离线测试。"""
import io
import json
import os
import sys
import tempfile
import types
import unittest

sys.dont_write_bytecode = True

utils_stub = types.ModuleType("utils")
utils_stub.bytes2str_only = lambda args: args
utils_stub.to_str = lambda value: value
sys.modules["utils"] = utils_stub
six_stub = types.ModuleType("six")
six_stub.string_types = str
sys.modules["six"] = six_stub

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
XLOG_PATH = os.path.join(
    ROOT, "Coding", "XX-Net-5.16.7.1", "code", "default", "lib", "noarch", "xlog.py"
)
xlog = types.ModuleType("xlog_unified_under_test")
with open(XLOG_PATH, encoding="utf-8") as source:
    exec(compile(source.read(), XLOG_PATH, "exec"), xlog.__dict__)


class UnifiedLogStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="xlog_unified_")
        self.loggers = []
        self.original_stderr = sys.stderr
        sys.stderr = io.StringIO()
        xlog.full_log = False

    def tearDown(self):
        sys.stderr = self.original_stderr
        for logger in self.loggers:
            for fd_name in ("persistent_fd", "log_fd"):
                fd = getattr(logger, fd_name, None)
                if fd:
                    fd.close()
        xlog.full_log = False
        self.temp_dir.cleanup()

    def make_logger(self, name="mod", startup=2, **kwargs):
        kwargs.setdefault("buffer_size", 100)
        kwargs.setdefault("log_path", self.temp_dir.name)
        kwargs.setdefault("save_start_log", startup)
        kwargs.setdefault("save_persistent_log", True)
        logger = xlog.Logger(name, **kwargs)
        self.loggers.append(logger)
        return logger

    def read_log(self, name="mod"):
        path = os.path.join(self.temp_dir.name, name + ".log")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as log_file:
            return log_file.read()

    def read_bytes(self, name):
        with open(os.path.join(self.temp_dir.name, name + ".log"), "rb") as log_file:
            return log_file.read()

    def test_u1_startup_budget_persists_all_levels_in_one_file(self):
        logger = self.make_logger(startup=4)
        logger.debug("debug event")
        logger.info("info event")
        logger.warning("warning event")
        logger.error("error event")
        content = self.read_log()
        for message in ("debug event", "info event", "warning event", "error event"):
            self.assertIn(message, content)
        self.assertFalse([name for name in os.listdir(self.temp_dir.name)
                          if name.startswith("start_log_") or "_warning.log" in name])

    def test_u2_after_startup_normal_debug_and_info_stay_in_memory(self):
        logger = self.make_logger(startup=2)
        logger.info("startup one")
        logger.info("startup two")
        logger.debug("memory debug")
        logger.info("memory info")
        content = self.read_log()
        self.assertNotIn("memory debug", content)
        self.assertNotIn("memory info", content)
        visible = json.loads(logger.get_last_lines(10))
        self.assertIn("memory debug", "".join(visible.values()))
        self.assertIn("memory info", "".join(visible.values()))

    def test_u3_explicit_persist_survives_startup_budget(self):
        logger = self.make_logger(startup=1)
        logger.info("startup")
        logger.debug("important debug", persist=True)
        logger.info("important info", persist=True)
        content = self.read_log()
        self.assertIn("important debug", content)
        self.assertIn("important info", content)

    def test_u4_warning_and_above_always_persist(self):
        logger = self.make_logger(startup=1)
        logger.info("startup")
        logger.warning("warning persists")
        logger.error("error persists")
        logger.critical("critical persists")
        content = self.read_log()
        for message in ("warning persists", "error persists", "critical persists"):
            self.assertIn(message, content)

    def test_u5_reset_reopens_without_deleting_or_renumbering(self):
        logger = self.make_logger(startup=1)
        logger.error("before reset")
        before = json.loads(logger.get_last_lines(10))
        logger.reset_log_files()
        logger.error("after reset")
        after = json.loads(logger.get_last_lines(10))
        content = self.read_log()
        self.assertIn("before reset", content)
        self.assertIn("after reset", content)
        self.assertEqual(list(map(int, after)), list(map(int, before)) + [len(before) + 1])

    def test_u6_keep_and_full_log_remain_bounded(self):
        logger = self.make_logger(startup=1)
        logger.persistent_file_max_size = 220
        logger.keep_log = True
        xlog.full_log = True
        for index in range(12):
            logger.info("detailed %d %s", index, "x" * 60)
        self.assertTrue(os.path.exists(os.path.join(self.temp_dir.name, "mod.log.1")))
        self.assertFalse(os.path.exists(os.path.join(self.temp_dir.name, "mod.log.1.2")))
        self.assertLessEqual(os.path.getsize(os.path.join(self.temp_dir.name, "mod.log")), 220)

    def test_u7_utf8_bytes_drive_single_generation_rolling(self):
        logger = self.make_logger(startup=10)
        logger.persistent_file_max_size = 180
        for index in range(8):
            logger.error("错误 %d %s", index, "界" * 20)
        main_path = os.path.join(self.temp_dir.name, "mod.log")
        self.assertTrue(os.path.exists(main_path + ".1"))
        self.assertFalse(os.path.exists(main_path + ".1.2"))
        self.assertLessEqual(os.path.getsize(main_path), 180)

    def test_u8_oversized_records_keep_utf8_ends_and_respect_caps(self):
        logger = self.make_logger(startup=10)
        logger.error("HEAD%sTAIL", "界" * 30000)
        raw = self.read_bytes("mod")
        self.assertLessEqual(len(raw), 64 * 1024)
        text = raw.decode("utf-8")
        self.assertIn("HEAD", text)
        self.assertIn("TAIL", text)
        self.assertIn("bytes omitted", text)

        tiny = self.make_logger(name="tiny", startup=10)
        tiny.persistent_file_max_size = 200
        tiny.error("BEGIN%sEND", "x" * 1000)
        tiny_raw = self.read_bytes("tiny")
        self.assertLessEqual(len(tiny_raw), 200)
        self.assertIn(b"BEGIN", tiny_raw)
        self.assertIn(b"END", tiny_raw)
        self.assertIn(b"bytes omitted", tiny_raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
