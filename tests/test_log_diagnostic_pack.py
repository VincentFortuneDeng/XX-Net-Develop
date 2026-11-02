#!/usr/bin/env python3
"""O 阶段诊断日志包收集、优先级与预算的离线测试。"""
import io
import inspect
import json
import os
import sys
import tempfile
import types
import unittest
import zipfile

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_PATH = os.path.join(ROOT, "Coding", "XX-Net-5.16.7.1", "code", "default",
                           "x_tunnel", "local", "upload_logs.py")

http_stub = types.ModuleType("simple_http_client")
http_stub.request = lambda *args, **kwargs: None
sys.modules["simple_http_client"] = http_stub
env_stub = types.ModuleType("env_info")
env_stub.data_path = tempfile.mkdtemp(prefix="xlog_pack_initial_")
sys.modules["env_info"] = env_stub
utils_stub = types.ModuleType("utils")
utils_stub.to_bytes = lambda value: value.encode("utf-8") if isinstance(value, str) else value
utils_stub.to_str = lambda value: value.decode("utf-8") if isinstance(value, bytes) else value
sys.modules["utils"] = utils_stub


class LoggerStub(object):
    def __init__(self):
        self.warnings = []
        self.errors = []

    def warn(self, fmt, *args):
        self.warnings.append(fmt % args)

    def exception(self, fmt, *args):
        self.errors.append(fmt % args)

    def debug(self, fmt, *args):
        pass


logger_stub = LoggerStub()
xlog_stub = types.ModuleType("xlog")
xlog_stub.getLogger = lambda name: logger_stub
xlog_stub.reset_log_files = lambda: None
sys.modules["xlog"] = xlog_stub
global_stub = types.ModuleType("x_tunnel.local.global_var")
global_stub.running = False
sys.modules["x_tunnel"] = types.ModuleType("x_tunnel")
sys.modules["x_tunnel.local"] = types.ModuleType("x_tunnel.local")
sys.modules["x_tunnel.local.global_var"] = global_stub

upload = types.ModuleType("upload_logs_under_test")
upload.__file__ = UPLOAD_PATH
with open(UPLOAD_PATH, encoding="utf-8") as source:
    exec(compile(source.read(), UPLOAD_PATH, "exec"), upload.__dict__)
REAL_COLLECT_LOG_VIEWS = upload.collect_log_views


class Response(object):
    def __init__(self, text):
        self.text = text


class DiagnosticPackTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="xlog_pack_")
        upload.data_path = self.temp_dir.name
        env_stub.data_path = self.temp_dir.name
        logger_stub.warnings = []
        logger_stub.errors = []
        self.requests = []
        self.flags = {"gae_proxy_enable": 1, "x_tunnel_enable": 1, "smart_router_enable": 1}
        upload.collect_debug_and_log = lambda: None
        upload.collect_log_views = lambda: {}

    def tearDown(self):
        http_stub.request = lambda *args, **kwargs: None
        self.temp_dir.cleanup()

    def install_http(self, failure=None):
        def request(method, url, timeout):
            self.requests.append((method, url, timeout))
            if "/config?" in url:
                return Response(json.dumps(self.flags))
            if failure and failure in url:
                raise TimeoutError("stub timeout")
            return Response("view:" + url)
        http_stub.request = request

    def create_file(self, relative, content):
        path = os.path.join(self.temp_dir.name, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(path, mode) as target:
            target.write(content)
        return path

    def pack(self, budget=10000, legacy=False, views=None):
        upload.collect_log_views = lambda: views or {}
        raw = upload.pack_logs(max_size=budget, include_legacy=legacy)
        self.assertIsNotNone(raw)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def test_p1_enabled_modules_export_their_own_bounded_views(self):
        upload.collect_log_views = REAL_COLLECT_LOG_VIEWS
        self.install_http()
        views = upload.collect_log_views()
        self.assertEqual(len(views), 14)
        self.assertEqual(len([url for _, url, _ in self.requests if "cmd=export" in url]), 14)
        for name in ("launcher", "smart_router", "gae_proxy", "x_tunnel",
                     "cloudflare_front", "seley_front", "tls_relay"):
            self.assertIn("views/%s_startup.log" % name, views)
            self.assertIn("views/%s_warning.log" % name, views)
        self.assertIn("max_bytes=262144", views["views/launcher_startup.log"].decode())
        self.assertIn("max_bytes=524288", views["views/launcher_warning.log"].decode())

    def test_p2_disabled_modules_have_zero_view_requests(self):
        self.flags.update(gae_proxy_enable=0, x_tunnel_enable=0, smart_router_enable=0)
        upload.collect_log_views = REAL_COLLECT_LOG_VIEWS
        self.install_http()
        views = upload.collect_log_views()
        self.assertEqual(set(views), {"views/launcher_startup.log", "views/launcher_warning.log"})
        exported = [url for _, url, _ in self.requests if "cmd=export" in url]
        self.assertEqual(len(exported), 2)
        self.assertFalse(any("/module/" in url for url in exported))

    def test_p3_timeout_warns_and_omits_only_that_view(self):
        upload.collect_log_views = REAL_COLLECT_LOG_VIEWS
        self.install_http(failure="cloudflare_front/log?cmd=export&view=warning")
        views = upload.collect_log_views()
        self.assertNotIn("views/cloudflare_front_warning.log", views)
        self.assertIn("views/cloudflare_front_startup.log", views)
        self.assertEqual(len(views), 13)
        self.assertEqual(len(logger_stub.warnings), 1)

    def test_p4_state_memory_and_views_precede_main_and_backup(self):
        self.create_file("launcher/config.json", "1234567890")
        self.create_file("downloads/launcher_log.txt", "abcdefghij")
        self.create_file("launcher/launcher.log", "ABCDEFGHIJ")
        self.create_file("launcher/launcher.log.1", "KLMNOPQRST")
        entries = self.pack(35, views={"views/launcher_warning.log": b"VIEWVIEWVV"})
        self.assertIn("launcher/config.json", entries)
        self.assertIn("downloads/launcher_log.txt", entries)
        self.assertIn("views/launcher_warning.log", entries)
        self.assertNotIn("launcher/launcher.log", entries)
        self.assertNotIn("launcher/launcher.log.1", entries)

    def test_p5_legacy_files_are_excluded_by_default(self):
        self.create_file("launcher/start_log_launcher_old.log", "old")
        self.create_file("launcher/launcher_warning.log", "old warning")
        self.create_file("launcher/launcher_warning.log.1", "older warning")
        self.create_file("launcher/launcher.log", "authoritative")
        entries = self.pack()
        self.assertEqual(set(entries), {"launcher/launcher.log"})

    def test_p6_explicit_legacy_is_lower_priority_than_unified_main(self):
        self.create_file("launcher/config.json", "1234567890")
        self.create_file("launcher/launcher.log", "ABCDEFGHIJ")
        self.create_file("launcher/launcher_warning.log", "KLMNOPQRST")
        entries = self.pack(25, legacy=True)
        self.assertIn("launcher/config.json", entries)
        self.assertIn("launcher/launcher.log", entries)
        self.assertNotIn("launcher/launcher_warning.log", entries)

    def test_p7_budget_uses_uncompressed_sizes_and_default_is_10_mib(self):
        self.assertEqual(inspect.signature(upload.pack_logs).parameters["max_size"].default,
                         10 * 1024 * 1024)
        self.assertEqual(inspect.signature(upload.pack_logs).parameters["include_legacy"].default, False)
        self.create_file("launcher/launcher.log", b"x" * 100)
        entries = self.pack(20)
        self.assertEqual(entries, {})

    def test_p8_oversized_file_is_skipped_and_smaller_candidate_still_packed(self):
        self.create_file("launcher/launcher.log", b"x" * 100)
        self.create_file("smart_router/smart_router.log", b"small")
        entries = self.pack(20)
        self.assertEqual(set(entries), {"smart_router/smart_router.log"})
        self.assertEqual(entries["smart_router/smart_router.log"], b"small")

    def test_p9_client_password_remains_masked(self):
        self.create_file("x_tunnel/client.json", json.dumps(
            {"login_account": "user", "login_password": "secret", "other": 1}))
        entries = self.pack()
        config = json.loads(entries["x_tunnel/client.json"])
        self.assertNotIn("login_password", config)
        self.assertNotIn(b"secret", entries["x_tunnel/client.json"])
        self.assertEqual(config["login_account"], "user")

    def test_p10_zip_has_no_duplicate_or_legacy_warning_copies(self):
        self.create_file("launcher/launcher.log", "main")
        self.create_file("launcher/launcher_warning.log", "warning copy")
        self.create_file("launcher/start_log_launcher_old.log", "startup copy")
        entries = self.pack(views={"views/launcher_warning.log": b"filtered warning"})
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries["launcher/launcher.log"], b"main")
        self.assertEqual(entries["views/launcher_warning.log"], b"filtered warning")


if __name__ == "__main__":
    unittest.main(verbosity=2)
