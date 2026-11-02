#!/usr/bin/env python3
"""O 阶段日志按需视图与七模块导出走线的离线测试。"""
import datetime
import io
import json
import os
import re
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
CODE = os.path.join(ROOT, "Coding", "XX-Net-5.16.7.1", "code", "default")
XLOG_PATH = os.path.join(CODE, "lib", "noarch", "xlog.py")
xlog = types.ModuleType("xlog_views_under_test")
with open(XLOG_PATH, encoding="utf-8") as source:
    exec(compile(source.read(), XLOG_PATH, "exec"), xlog.__dict__)

HANDLERS = [
    "launcher/web_control.py",
    "gae_proxy/local/web_control.py",
    "smart_router/local/web_control.py",
    "x_tunnel/local/web_control.py",
    "x_tunnel/local/cloudflare_front/web_control.py",
    "x_tunnel/local/seley_front/web_control.py",
    "x_tunnel/local/tls_relay_front/web_control.py",
]


def record(level, message, when=None):
    when = when or datetime.datetime.now()
    return "%s - [%s] %s\n" % (when.strftime("%Y-%m-%d %H:%M:%S.%f")[:23], level, message)


class LogViewExportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="xlog_views_")
        self.loggers = []
        self.original_stderr = sys.stderr
        sys.stderr = io.StringIO()

    def tearDown(self):
        sys.stderr = self.original_stderr
        for logger in self.loggers:
            if logger.persistent_fd:
                logger.persistent_fd.close()
        self.temp_dir.cleanup()

    def make_logger(self, startup=2, persistent=True):
        logger = xlog.Logger("views", buffer_size=100, log_path=self.temp_dir.name,
                             save_start_log=startup, save_persistent_log=persistent)
        self.loggers.append(logger)
        return logger

    def write_raw(self, content, suffix=""):
        with open(os.path.join(self.temp_dir.name, "views.log" + suffix), "wb") as target:
            target.write(content)

    def test_v1_startup_view_contains_only_current_verbose_period(self):
        logger = self.make_logger(startup=2)
        logger.debug("startup debug")
        logger.info("startup info")
        logger.info("later important", persist=True)
        logger.warning("later warning")
        exported = logger.export_view("startup", 10000)
        self.assertIn("startup debug", exported)
        self.assertIn("startup info", exported)
        self.assertNotIn("later important", exported)
        self.assertNotIn("later warning", exported)

    def test_v2_warning_view_keeps_whole_error_context(self):
        logger = self.make_logger(startup=10)
        logger.info("ordinary info")
        logger.debug("debug clue")
        logger.warning("warning event")
        logger.error("error event")
        exported = logger.export_view("warning", 10000)
        outside_context = re.sub(r"[^\n]*ERROR_CONTEXT_BEGIN\n.*?ERROR_CONTEXT_END\n", "", exported, flags=re.S)
        self.assertNotIn("ordinary info", outside_context)
        self.assertIn("warning event", exported)
        self.assertIn("error event", exported)
        self.assertIn("debug clue", exported)
        self.assertEqual(exported.count("ERROR_CONTEXT_BEGIN"), 1)
        self.assertEqual(exported.count("ERROR_CONTEXT_END"), 1)

    def test_v3_view_limit_keeps_latest_complete_records(self):
        logger = self.make_logger()
        records = [record("WARNING", "old " + "x" * 100),
                   record("ERROR", "middle"),
                   record("CRITICAL", "latest")]
        self.write_raw("".join(records).encode("utf-8"))
        limit = len(records[2].encode("utf-8"))
        exported = logger.export_view("warning", limit)
        self.assertEqual(exported, records[2])

    def test_v4_invalid_view_and_limits_return_empty_and_warn(self):
        logger = self.make_logger()
        for view, limit in (("invalid", 100), ("warning", "abc"),
                            ("warning", 0), ("startup", -1)):
            self.assertEqual(logger.export_view(view, limit), "")
        memory = "".join(json.loads(logger.get_last_lines(20)).values())
        self.assertEqual(memory.count("[WARNING] invalid log export"), 4)

    def test_v5_backup_precedes_main_file(self):
        logger = self.make_logger()
        now = datetime.datetime.now()
        old = record("WARNING", "from backup", now - datetime.timedelta(seconds=2))
        new = record("WARNING", "from main", now)
        self.write_raw(old.encode("utf-8"), ".1")
        self.write_raw(new.encode("utf-8"))
        exported = logger.export_view("warning", 10000)
        self.assertLess(exported.index("from backup"), exported.index("from main"))

    def test_v6_corrupt_records_and_partial_tail_are_skipped(self):
        logger = self.make_logger()
        good_old = record("WARNING", "good old").encode("utf-8")
        corrupt = b"2026-11-02 12:00:00.000 - [ERROR] bad \xff\n"
        good_new = record("ERROR", "good new").encode("utf-8")
        partial = record("WARNING", "partial").encode("utf-8")[:-1]
        self.write_raw(good_old + corrupt + good_new + partial)
        exported = logger.export_view("warning", 10000)
        self.assertIn("good old", exported)
        self.assertIn("good new", exported)
        self.assertNotIn("bad", exported)
        self.assertNotIn("partial", exported)

    def test_v7_multiline_warning_and_context_are_not_split_by_limit(self):
        logger = self.make_logger(startup=10)
        logger.info("clue")
        logger.error("multiline\nstack line")
        exported = logger.export_view("warning", 10000)
        self.assertIn("multiline\nstack line", exported)
        self.assertIn("clue", exported)
        self.assertNotIn("bytes omitted", exported)

    def test_v8_logger_without_persistent_file_returns_empty(self):
        logger = self.make_logger(persistent=False)
        logger.warning("memory only")
        self.assertEqual(logger.export_view("warning", 10000), "")

    def test_v9_all_handlers_export_with_defaults_and_keep_memory_branches(self):
        for relative in HANDLERS:
            with self.subTest(handler=relative):
                with open(os.path.join(CODE, relative), encoding="utf-8") as source:
                    text = source.read()
                handler = re.search(r"    def req_log_handler\(self\):\n(.*?)(?=\n    def |\Z)",
                                    text, re.S).group(1)
                self.assertIn('cmd == "export"', handler)
                self.assertIn("256 * 1024", handler)
                self.assertIn("512 * 1024", handler)
                self.assertIn("export_view(", handler)
                self.assertIn('cmd == "get_last"', handler)
                self.assertIn('cmd == "get_new"', handler)
                self.assertIn("get_last_lines(max_line", handler)
                self.assertIn("get_new_lines(last_no", handler)


if __name__ == "__main__":
    unittest.main(verbosity=2)
