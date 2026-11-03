#!/usr/bin/env python3
"""O 阶段统一日志正文、持久化与 ERROR 上下文的离线测试。"""
import io
import json
import os
import re
import sys
import tempfile
import threading
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
            logger.stop()
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
        for logger in self.loggers:
            logger.flush_persistent_writes()
        path = os.path.join(self.temp_dir.name, name + ".log")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as log_file:
            return log_file.read()

    def read_bytes(self, name):
        for logger in self.loggers:
            logger.flush_persistent_writes()
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
        logger.flush_persistent_writes()
        self.assertTrue(os.path.exists(os.path.join(self.temp_dir.name, "mod.log.1")))
        self.assertFalse(os.path.exists(os.path.join(self.temp_dir.name, "mod.log.1.2")))
        self.assertLessEqual(os.path.getsize(os.path.join(self.temp_dir.name, "mod.log")), 220)

    def test_u7_utf8_bytes_drive_single_generation_rolling(self):
        logger = self.make_logger(startup=10)
        logger.persistent_file_max_size = 180
        for index in range(8):
            logger.error("错误 %d %s", index, "界" * 20)
        logger.flush_persistent_writes()
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
    def test_c1_error_context_contains_recent_debug_info_but_not_warning(self):
        logger = self.make_logger(startup=10)
        logger.debug("debug clue")
        logger.warning("warning not context")
        logger.info("info clue")
        logger.error("failed")
        match = re.search(r"\[ERROR\] ERROR_CONTEXT_BEGIN\n(.*?)\[ERROR\] ERROR_CONTEXT_END\n",
                          self.read_log(), re.S)
        self.assertIsNotNone(match)
        context = match.group(1)
        self.assertIn("debug clue", context)
        self.assertIn("info clue", context)
        self.assertNotIn("warning not context", context)
        self.assertNotIn("ERROR_CONTEXT", "".join(json.loads(logger.get_last_lines(20)).values()))

    def test_c2_context_is_limited_to_40_complete_entries_and_16_kib(self):
        logger = self.make_logger(startup=100, buffer_size=200)
        for index in range(60):
            logger.info("clue %02d %s", index, "界" * 100)
        logger.error("failed")
        context = re.search(r"ERROR_CONTEXT_BEGIN\n(.*?)ERROR_CONTEXT_END\n",
                            self.read_log(), re.S).group(1)
        self.assertEqual(len(re.findall(r" - \[INFO\] clue ", context)), 40)
        self.assertNotIn("clue 19", context)
        self.assertIn("clue 20", context)
        self.assertIn("clue 59", context)
        block = re.search(r"[^\n]*ERROR_CONTEXT_BEGIN\n.*?ERROR_CONTEXT_END\n",
                          self.read_log(), re.S).group(0)
        self.assertLessEqual(len(block.encode("utf-8")), 16 * 1024)
        self.assertNotIn("bytes omitted", block)

    def test_c3_same_template_cools_context_without_suppressing_error(self):
        logger = self.make_logger(startup=10)
        logger.info("clue")
        logger.error("same template %s", "one")
        logger.error("same template %s", "two")
        content = self.read_log()
        self.assertEqual(content.count("ERROR_CONTEXT_BEGIN"), 1)
        self.assertEqual(content.count("[ERROR] same template one"), 1)
        self.assertEqual(content.count("[ERROR] same template two"), 1)

    def test_c4_different_templates_have_independent_cooldowns(self):
        logger = self.make_logger(startup=10)
        logger.info("clue")
        logger.error("first template")
        logger.error("second template")
        self.assertEqual(self.read_log().count("ERROR_CONTEXT_BEGIN"), 2)

    def test_c5_exception_has_one_context_block_and_one_stack(self):
        logger = self.make_logger(startup=10)
        logger.info("exception clue")
        try:
            raise ValueError("exception boom")
        except ValueError:
            logger.exception("operation failed")
        content = self.read_log()
        self.assertEqual(content.count("ERROR_CONTEXT_BEGIN"), 1)
        self.assertEqual(content.count("Traceback (most recent call last)"), 1)
        self.assertEqual(content.count("ValueError: exception boom"), 1)

    def test_c6_critical_uses_the_same_context_contract(self):
        logger = self.make_logger(startup=10)
        logger.debug("critical clue")
        logger.critical("critical failure")
        content = self.read_log()
        self.assertEqual(content.count("ERROR_CONTEXT_BEGIN"), 1)
        self.assertIn("critical clue", content)
        self.assertIn("[CRITICAL] critical failure", content)

    def test_c7_multiline_context_and_traceback_remain_complete(self):
        logger = self.make_logger(startup=10)
        logger.info("context first\ncontext second")
        try:
            raise RuntimeError("multiline boom")
        except RuntimeError:
            logger.exception("failed")
        content = self.read_log()
        self.assertEqual(content.count("ERROR_CONTEXT_BEGIN"), 1)
        self.assertIn("context first\ncontext second", content)
        self.assertIn('raise RuntimeError("multiline boom")\nRuntimeError: multiline boom', content)

    def test_c8_concurrent_blocks_do_not_interleave_or_renumber_memory(self):
        logger = self.make_logger(startup=200, buffer_size=200)
        logger.info("shared clue")
        workers = [threading.Thread(target=logger.error, args=("failure template %d" % index,))
                   for index in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        content = self.read_log()
        self.assertEqual(content.count("ERROR_CONTEXT_BEGIN"), 8)
        self.assertEqual(content.count("ERROR_CONTEXT_END"), 8)
        for block in re.findall(r"ERROR_CONTEXT_BEGIN\n(.*?)ERROR_CONTEXT_END\n", content, re.S):
            self.assertNotIn("ERROR_CONTEXT_BEGIN", block)
        records = json.loads(logger.get_last_lines(200))
        self.assertEqual(sorted(map(int, records)), list(range(1, 10)))

    def test_c9_small_file_rolls_whole_context_and_keeps_error(self):
        logger = self.make_logger(startup=100, buffer_size=100)
        logger.persistent_file_max_size = 500
        for index in range(10):
            logger.info("rolling clue %d %s", index, "x" * 50)
        logger.error("rolling error")
        logger.flush_persistent_writes()
        paths = [os.path.join(self.temp_dir.name, "mod.log"),
                 os.path.join(self.temp_dir.name, "mod.log.1")]
        contents = []
        for path in paths:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as log_file:
                    contents.append(log_file.read())
        self.assertTrue(contents)
        self.assertIn("rolling error", "".join(contents))
        self.assertEqual("".join(contents).count("ERROR_CONTEXT_BEGIN"), 1)
        self.assertEqual("".join(contents).count("ERROR_CONTEXT_END"), 1)
        for content in contents:
            self.assertEqual(content.count("ERROR_CONTEXT_BEGIN"),
                             content.count("ERROR_CONTEXT_END"))
        self.assertLessEqual(max(map(len, [content.encode("utf-8") for content in contents])), 500)
    def test_c10_exception_body_survives_context_and_stack_rolling(self):
        logger = self.make_logger(startup=0, buffer_size=100)
        logger.persistent_file_max_size = 500
        for index in range(10):
            logger.info("rolling clue %d %s", index, "x" * 50)
        logger.info("persistent filler %s", "f" * 300, persist=True)
        try:
            raise ValueError("small file exception " + "x" * 600)
        except ValueError:
            logger.exception("exception body must survive")
        logger.flush_persistent_writes()
        contents = []
        for suffix in (".1", ""):
            path = os.path.join(self.temp_dir.name, "mod.log" + suffix)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as log_file:
                    contents.append(log_file.read())
        combined = "".join(contents)
        self.assertIn("exception body must survive", combined)
        self.assertIn("Traceback (most recent call last)", combined)

    def test_u9_initial_open_failure_degrades_to_memory_and_can_recover(self):
        missing = os.path.join(self.temp_dir.name, "missing", "logs")
        logger = xlog.Logger("recover", buffer_size=20, log_path=missing,
                             save_start_log=0, save_persistent_log=True)
        self.loggers.append(logger)
        self.assertIsNone(logger.persistent_fd)
        logger.info("memory survives open failure")
        self.assertIn("memory survives open failure",
                      "".join(json.loads(logger.get_last_lines(20)).values()))
        os.makedirs(missing)
        logger.reset_log_files()
        logger.error("persistent write recovered")
        logger.flush_persistent_writes()
        with open(os.path.join(missing, "recover.log"), encoding="utf-8") as log_file:
            self.assertIn("persistent write recovered", log_file.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
