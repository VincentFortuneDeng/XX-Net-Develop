"""launcher 有界运行快照、启动事实和受保护接口的离线测试。"""
import ast
import base64
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "Coding" / "XX-Net-5.16.7.1" / "code" / "default"
LAUNCHER = APP / "launcher"
NOARCH = APP / "lib" / "noarch"
sys.path.insert(0, str(NOARCH))
sys.path.insert(0, str(LAUNCHER))

import launcher_runtime_metrics as runtime_metrics
import runtime_state


class TargetDict(dict):
    def __missing__(self, key):
        raise AssertionError("缺少目标字段: %s" % key)


def target(value):
    if isinstance(value, dict):
        return TargetDict({key: target(item) for key, item in value.items()})
    if isinstance(value, list):
        return [target(item) for item in value]
    return value


def collect_snapshot(providers=None):
    return target(runtime_metrics.collect_snapshot(providers))


def state_snapshot():
    return target(runtime_state.snapshot())


class Collector:
    def _record(self, *args, **kwargs):
        pass

    debug = info = warn = warning = error = exception = _record


def namespace(name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class RuntimeSnapshotTest(unittest.TestCase):
    def setUp(self):
        importlib.reload(runtime_state)

    def test_schema_generation_and_safe_startup_classification(self):
        runtime_state.record_argv([
            "/private/should-not-be-stored/python", "-no_popup", "-no_systray",
            "--token=should-not-be-stored",
        ])
        first = collect_snapshot({})
        second = collect_snapshot({})

        self.assertEqual(first["schema_version"], 1)
        self.assertIsInstance(first["sampled_at"], float)
        self.assertGreaterEqual(first["collection_duration_seconds"], 0)
        self.assertRegex(first["process_generation"], r"^[0-9a-f]{32}$")
        self.assertEqual(first["process_generation"], second["process_generation"])
        self.assertNotEqual(first["process_generation"], str(os.getpid()))
        self.assertEqual(first["process"]["pid"], os.getpid())
        self.assertGreater(first["process"]["started_at"], 0)
        self.assertEqual(first["launcher"]["startup_mode"], "headless_service")
        self.assertTrue(first["launcher"]["no_popup"])
        self.assertTrue(first["launcher"]["no_systray"])
        serialized = json.dumps(first)
        self.assertNotIn("should-not-be-stored", serialized)
        for forbidden in ("argv", "cwd", "executable"):
            self.assertNotIn(forbidden, first)
            self.assertNotIn(forbidden, first["launcher"])

    def test_default_and_custom_startup_modes(self):
        runtime_state.record_argv([])
        runtime_state.record_restart_from_except(False)
        state = state_snapshot()
        self.assertEqual(state["startup_mode"], "default")
        self.assertFalse(state["no_popup"])
        self.assertFalse(state["no_systray"])
        self.assertFalse(state["restart_from_except"])

        runtime_state.record_argv(["-no_popup"])
        self.assertEqual(state_snapshot()["startup_mode"], "custom")

    def test_not_started_page_and_tray_use_fixed_enums(self):
        initial = state_snapshot()
        self.assertEqual(initial["startup_mode"], "not_started")
        self.assertEqual(initial["page_open_request"], "not_requested")
        self.assertEqual(initial["tray_backend"], "unknown")
        self.assertEqual(initial["tray_state"], "not_started")

        runtime_state.record_page_open(True)
        runtime_state.record_tray_backend("native")
        runtime_state.record_tray_started()
        state = state_snapshot()
        self.assertEqual(state["page_open_request"], "succeeded")
        self.assertEqual(state["tray_backend"], "native")
        self.assertEqual(state["tray_state"], "running")

        runtime_state.record_page_open(False)
        runtime_state.record_tray_backend("fallback")
        runtime_state.record_tray_returned()
        state = state_snapshot()
        self.assertEqual(state["page_open_request"], "failed")
        self.assertEqual(state["tray_backend"], "fallback")
        self.assertEqual(state["tray_state"], "returned")

        runtime_state.record_tray_disabled()
        self.assertEqual(state_snapshot()["tray_state"], "disabled")

    def test_module_error_is_isolated_and_exception_text_is_not_exported(self):
        secret = "provider-secret-must-not-leak"

        def fail():
            raise RuntimeError(secret)

        result = collect_snapshot({
            "good": lambda: {"value": 1},
            "bad": fail,
            "stopped": lambda: None,
        })

        self.assertEqual(result["modules"]["good"], {
            "state": "running", "collection": "ok", "metrics": {"value": 1},
        })
        self.assertEqual(result["modules"]["bad"], {
            "state": "unknown", "collection": "error", "metrics": None,
        })
        self.assertEqual(result["modules"]["stopped"], {
            "state": "not_running", "collection": "not_running", "metrics": None,
        })
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn("exception", result["modules"]["bad"])

    def test_blocked_provider_has_shared_deadline_and_single_inflight_thread(self):
        barrier = threading.Barrier(2)
        calls = []

        def blocked():
            calls.append(1)
            if not barrier.wait(timeout=3):
                raise TimeoutError("test barrier failed")
            return {"value": 1}

        snapshots = []
        for _ in range(3):
            started = time.monotonic()
            snapshot = collect_snapshot({"blocked": blocked})
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.8, "provider 阻塞不得拖死控制面")
            self.assertGreaterEqual(snapshot["collection_duration_seconds"], 0.15)
            snapshots.append(snapshot)

        for snapshot in snapshots:
            self.assertEqual(snapshot["modules"]["blocked"], {
                "state": "unknown", "collection": "unknown", "metrics": None,
            })
        self.assertEqual(len(calls), 1, "同一阻塞 provider 不得重复创建线程")
        workers = [thread for thread in threading.enumerate()
                   if thread.name == "runtime-metrics:blocked"]
        self.assertLessEqual(len(workers), 1)
        if calls:
            barrier.wait(timeout=1)

    def test_multiple_blocked_providers_share_one_aggregate_deadline(self):
        barrier = threading.Barrier(3)
        calls = []

        def blocked():
            calls.append(1)
            barrier.wait(timeout=3)
            return {"value": 1}

        started = time.monotonic()
        result = collect_snapshot({"one": blocked, "two": blocked})
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.65)
        self.assertEqual(result["modules"]["one"]["collection"], "unknown")
        self.assertEqual(result["modules"]["two"]["collection"], "unknown")
        if calls:
            barrier.wait(timeout=1)

    def test_snapshot_does_not_open_network_connections(self):
        with mock.patch.object(socket, "create_connection", side_effect=AssertionError("network")), \
                mock.patch.object(socket, "socket", side_effect=AssertionError("network")):
            result = collect_snapshot({"local": lambda: {"value": 2}})
        self.assertEqual(result["modules"]["local"]["metrics"], {"value": 2})

    def test_process_schema_and_real_darwin_resources(self):
        with mock.patch("subprocess.Popen", side_effect=AssertionError("不得启动 lsof")), \
                mock.patch("subprocess.run", side_effect=AssertionError("不得启动 lsof")):
            result = collect_snapshot({})
        process = result["process"]
        for key in (
            "threads", "rss_bytes", "cpu_time_seconds", "fd_count", "fd_soft_limit",
            "log_queue_length", "log_dropped_total", "log_write_failures_total",
            "log_throttled_total", "background_exceptions_total", "unknown_stops_total",
        ):
            self.assertIn(key, process)
        if sys.platform == "darwin":
            for key in ("threads", "rss_bytes", "cpu_time_seconds", "fd_count", "fd_soft_limit"):
                self.assertIsNotNone(process[key], key)
                self.assertGreater(process[key], 0, key)


class StartupIntegrationTest(unittest.TestCase):
    def setUp(self):
        importlib.reload(runtime_state)

    def test_real_main_records_default_page_request_and_tray_return(self):
        tree = ast.parse((LAUNCHER / "start.py").read_text(encoding="utf-8"))
        main_node = next(node for node in tree.body
                         if isinstance(node, ast.FunctionDef) and node.name == "main")
        module_tree = ast.Module(body=[main_node], type_ignores=[])
        ast.fix_missing_locations(module_tree)
        config = types.SimpleNamespace(
            show_compat_suggest=False, allow_remote_connect=0, no_mess_system=0,
            popup_webui=1, control_port=8085, show_systray=True,
        )
        module_init = types.SimpleNamespace(xargs={}, start_all_auto=mock.Mock())
        web_control = types.SimpleNamespace(confirm_xxnet_not_running=mock.Mock(), start=mock.Mock())
        update = types.SimpleNamespace(start=mock.Mock(), should_create_desktop_shortcut=True)
        download_modules = types.SimpleNamespace(start_download=mock.Mock())
        update_from_github = types.SimpleNamespace(cleanup=mock.Mock())
        sys_platform = types.SimpleNamespace(show_systray=mock.Mock())
        fake_sys = types.SimpleNamespace(platform="offline-test", argv=["start.py"])
        browser_open = mock.Mock(return_value=True)
        environment = {
            "__file__": str(LAUNCHER / "start.py"), "os": os, "sys": fake_sys,
            "time": time, "config": config, "module_init": module_init,
            "web_control": web_control, "update": update,
            "download_modules": download_modules, "update_from_github": update_from_github,
            "sys_platform": sys_platform, "global_var": types.SimpleNamespace(running=False),
            "has_desktop": True, "runtime_state": runtime_state, "xlog": Collector(),
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            environment["running_file"] = str(Path(temp_dir) / "Running.Lck")
            exec(compile(module_tree, str(LAUNCHER / "start.py"), "exec"), environment)
            with mock.patch.dict(sys.modules, {
                "post_update": namespace("post_update", check=mock.Mock()),
                "webbrowser": namespace("webbrowser", open=browser_open),
            }), mock.patch.object(os, "chdir"):
                environment["main"]()

        state = state_snapshot()
        self.assertEqual(state["startup_mode"], "default")
        self.assertFalse(state["restart_from_except"])
        self.assertEqual(state["page_open_request"], "succeeded")
        self.assertEqual(state["tray_state"], "returned")
        browser_open.assert_called_once_with("http://localhost:8085/")
        sys_platform.show_systray.assert_called_once_with()

    def test_darwin_tray_records_native_and_fallback_backends(self):
        stubs = {
            "xlog": namespace("xlog", getLogger=lambda *args, **kwargs: Collector()),
            "global_var": namespace("global_var", running=True),
        }
        with mock.patch.dict(sys.modules, stubs):
            platform_module = load_module("sys_platform_runtime_test", LAUNCHER / "sys_platform.py")

        native_tray = namespace("mac_tray", serve_forever=mock.Mock())
        with mock.patch.dict(sys.modules, {"mac_tray": native_tray}):
            platform_module.show_systray()
        state = state_snapshot()
        self.assertEqual(state["tray_backend"], "native")
        self.assertEqual(state["tray_state"], "running")
        native_tray.serve_forever.assert_called_once_with()

        importlib.reload(runtime_state)
        fallback_serve = mock.Mock()
        fallback_module = namespace("non_tray", sys_tray=types.SimpleNamespace(serve_forever=fallback_serve))
        with mock.patch.dict(sys.modules, {"mac_tray": None, "non_tray": fallback_module}):
            platform_module.show_systray()
        state = state_snapshot()
        self.assertEqual(state["tray_backend"], "fallback")
        self.assertEqual(state["tray_state"], "running")
        fallback_serve.assert_called_once_with()


class WebControlRuntimeMetricsTest(unittest.TestCase):
    def setUp(self):
        self.config = types.SimpleNamespace(
            allowed_refers=[], webui_auth=None, enable_x_tunnel=False,
            enable_gae_proxy=False, all_modules=[],
        )
        stubs = {
            "sys_platform": namespace("sys_platform", platform="mac", on_quit=mock.Mock()),
            "xlog": namespace("xlog", getLogger=lambda *args, **kwargs: Collector(), keep_log=mock.Mock()),
            "module_init": namespace("module_init", proc_handler={}),
            "config": namespace("config", config=self.config, valid_language=lambda value: value,
                                app_name="XX-Net"),
            "autorun": namespace("autorun"),
            "update": namespace("update"),
            "update_from_github": namespace("update_from_github", current_version=lambda: "test"),
            "simple_http_client": namespace("simple_http_client"),
            "simple_i18n": namespace("simple_i18n", SimpleI18N=type("SimpleI18N", (), {
                "add_translate": lambda self, *args: None,
            })),
            "env_info": namespace("env_info", data_path=tempfile.gettempdir()),
        }
        with mock.patch.dict(sys.modules, stubs):
            self.web = load_module("launcher_web_runtime_test", LAUNCHER / "web_control.py")
        self.web.runtime_metrics = types.SimpleNamespace(collect_snapshot=mock.Mock(return_value={
            "schema_version": 1, "safe": True,
        }))

    def handler(self, path="/runtime_metrics", address="127.0.0.1", headers=None):
        handler = self.web.Http_Handler.__new__(self.web.Http_Handler)
        handler.headers = headers or {}
        handler.path = path
        handler.client_address = (address, 12345)
        handler.command = "GET"
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        handler.res_headers = {}
        handler.send_response = mock.Mock()
        handler.send_file = mock.Mock()
        return handler

    def response_status(self, handler):
        self.assertTrue(handler.send_response.called, "目标路由没有产生响应")
        return handler.send_response.call_args.kwargs.get("status", 200)

    def test_local_metrics_returns_json_no_store_without_new_cors(self):
        handler = self.handler()
        handler.do_GET()
        handler.send_response.assert_called_once()
        args, kwargs = handler.send_response.call_args
        self.assertEqual(args[0], "application/json")
        self.assertEqual(json.loads(args[1]), {"schema_version": 1, "safe": True})
        self.assertEqual(kwargs["headers"], {"Cache-Control": "no-store"})
        self.assertEqual(handler.res_headers, {})

    def test_auth_failure_is_401_and_does_not_collect_metrics(self):
        self.config.webui_auth = {"tester": "dummy-password"}
        handler = self.handler()
        handler.do_GET()
        self.assertEqual(self.response_status(handler), 401)
        self.web.runtime_metrics.collect_snapshot.assert_not_called()

    def test_authorized_metrics_still_requires_loopback(self):
        self.config.webui_auth = {"tester": "dummy-password"}
        token = base64.b64encode(b"tester:dummy-password").decode("ascii")
        local = self.handler(headers={"Authorization": "Basic " + token})
        local.do_GET()
        self.assertEqual(self.response_status(local), 200)

        self.web.runtime_metrics.collect_snapshot.reset_mock()
        remote = self.handler(address="192.0.2.10", headers={"Authorization": "Basic " + token})
        remote.do_GET()
        self.assertEqual(self.response_status(remote), 403)
        self.web.runtime_metrics.collect_snapshot.assert_not_called()

    def test_ipv6_loopback_is_allowed(self):
        handler = self.handler(address="::1")
        handler.do_GET()
        self.assertEqual(self.response_status(handler), 200)

    def test_referer_boundary_runs_before_metrics(self):
        handler = self.handler(headers={"Referer": "https://example.invalid/", "Host": "localhost"})
        handler.do_GET()
        handler.send_response.assert_not_called()
        self.web.runtime_metrics.collect_snapshot.assert_not_called()

    def test_dashboard_uses_existing_static_file_path(self):
        handler = self.handler(path="/dashboard.html")
        with mock.patch.object(self.web.os.path, "isfile", return_value=True):
            handler.do_GET()
        handler.send_file.assert_called_once()
        self.assertTrue(handler.send_file.call_args.args[0].endswith("web_ui/dashboard.html"))
        self.assertEqual(handler.send_file.call_args.args[1], "text/html")


if __name__ == "__main__":
    unittest.main()
