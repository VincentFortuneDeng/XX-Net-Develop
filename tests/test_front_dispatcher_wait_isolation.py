import ast
import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "Coding" / "XX-Net-5.16.7.1" / "code" / "default"
LIB = APP / "lib" / "noarch"
sys.path.insert(0, str(LIB))


class Collector:
    def warn(self, *args):
        pass


class Throttle:
    def reset(self):
        pass

    def flush(self):
        return 0, 0


def namespace(name, path=None, **attributes):
    module = types.ModuleType(name)
    if path is not None:
        module.__path__ = [str(path)]
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


namespace("x_tunnel", APP / "x_tunnel")
namespace("x_tunnel.local", APP / "x_tunnel" / "local")
global_var = namespace(
    "x_tunnel.local.global_var",
    config=types.SimpleNamespace(api_server="api.example"),
    server_host="",
    stat={"timeout_roundtrip": 0},
    running=False,
)
namespace("env_info", data_path=tempfile.gettempdir())
namespace("xlog", getLogger=lambda *args, **kwargs: Collector(), LogThrottle=lambda seconds: Throttle())

SPEC = importlib.util.spec_from_file_location(
    "x_tunnel.local.front_dispatcher",
    APP / "x_tunnel" / "local" / "front_dispatcher.py",
)
front_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = front_module
SPEC.loader.exec_module(front_module)


class FakeDispatcher:
    def __init__(self, score=1, started=None):
        self.score = score
        self.started = started

    def get_score(self):
        if self.started is not None:
            self.started.set()
        return self.score


class FakeFront:
    def __init__(self, name, dispatcher):
        self.name = name
        self.dispatcher = dispatcher
        self.config = types.SimpleNamespace(show_state_debug=0)
        self.stopped = False

    def get_dispatcher(self, host=None):
        return self.dispatcher

    def stop(self):
        self.stopped = True


class FrontWaitIsolationTest(unittest.TestCase):
    def setUp(self):
        front_module.all_fronts = []
        front_module.light_fronts = []
        front_module.session_fronts = []
        front_module.cloudflare_front = None
        global_var.server_host = ""
        global_var.stat["timeout_roundtrip"] = 0

    def tearDown(self):
        front_module.all_fronts = []
        front_module.light_fronts = []
        front_module.session_fronts = []
        front_module.cloudflare_front = None

    def test_one_waiting_front_does_not_block_another_ready_front(self):
        started = threading.Event()
        waiting = FakeFront("waiting", FakeDispatcher(score=0, started=started))
        ready = FakeFront("ready", FakeDispatcher(score=1))
        front_module.session_fronts = [waiting]
        front_module.light_fronts = [ready]
        result = []
        waiter = threading.Thread(target=lambda: result.append(front_module.get_front("session", 0.5)))
        waiter.start()
        self.assertTrue(started.wait(0.1))

        begin = time.monotonic()
        selected = front_module.get_front("dns.xx-net.org", 0.05)
        elapsed = time.monotonic() - begin

        waiter.join(1)
        self.assertIs(selected, ready)
        self.assertLess(elapsed, 0.2)
        self.assertEqual(result, [None])

    def test_unavailable_fronts_honor_timeout_and_accounting(self):
        front_module.session_fronts = [FakeFront("waiting", FakeDispatcher(score=0))]

        begin = time.monotonic()
        selected = front_module.get_front("session", 0.03)
        elapsed = time.monotonic() - begin

        self.assertIsNone(selected)
        self.assertLess(elapsed, 0.2)
        self.assertEqual(global_var.stat["timeout_roundtrip"], 5)

    def test_front_recovers_without_new_caller(self):
        dispatcher = FakeDispatcher(score=0)
        front = FakeFront("recover", dispatcher)
        front_module.session_fronts = [front]
        timer = threading.Timer(0.05, setattr, args=(dispatcher, "score", 2))
        timer.start()
        self.addCleanup(timer.join)

        selected = front_module.get_front("session", 0.3)

        self.assertIs(selected, front)

    def test_light_and_session_front_registries_stay_separate(self):
        light = FakeFront("light", FakeDispatcher(score=1))
        session = FakeFront("session", FakeDispatcher(score=1))
        front_module.light_fronts = [light]
        front_module.session_fronts = [session]

        self.assertIs(front_module.get_front("dns.xx-net.org", 0.01), light)
        self.assertIs(front_module.get_front("session", 0.01), session)

    def test_stop_is_not_blocked_by_a_waiting_caller(self):
        started = threading.Event()
        waiting = FakeFront("waiting", FakeDispatcher(score=0, started=started))
        front_module.all_fronts = [waiting]
        front_module.session_fronts = [waiting]
        waiter = threading.Thread(target=lambda: front_module.get_front("session", 0.5))
        waiter.start()
        self.assertTrue(started.wait(0.1))

        begin = time.monotonic()
        front_module.stop()
        elapsed = time.monotonic() - begin

        self.assertLess(elapsed, 0.2)
        self.assertTrue(waiting.stopped)
        self.assertEqual(front_module.session_fronts, [])
        waiter.join(1)
        self.assertFalse(waiter.is_alive())

    def test_init_and_stop_clear_disabled_front_publications(self):
        class DisabledConfig:
            enable_gae_proxy = False
            enable_cloudflare = False
            enable_cloudfront = False
            enable_seley = False
            enable_tls_relay = False
            enable_direct = False
            api_server = "api.example"

        global_var.config = DisabledConfig()
        for name in ("cloudflare_front", "cloudfront_front", "seley_front", "tls_relay_front"):
            setattr(global_var, name, object())
        front_module.init()

        for name in ("cloudflare_front", "cloudfront_front", "seley_front", "tls_relay_front"):
            self.assertIsNone(getattr(global_var, name))

        for name in ("cloudflare_front", "cloudfront_front", "seley_front", "tls_relay_front"):
            setattr(global_var, name, object())
        front_module.stop()
        for name in ("cloudflare_front", "cloudfront_front", "seley_front", "tls_relay_front"):
            self.assertIsNone(getattr(global_var, name))

    def test_statistic_thread_stops_when_front_registry_is_replaced(self):
        sleep_calls = []
        statistic_calls = []

        class Dispatcher:
            def statistic(self):
                statistic_calls.append(1)
                front_module.all_fronts = []

        front = types.SimpleNamespace(
            get_dispatcher=lambda: Dispatcher())
        front_module.all_fronts = [front]
        global_var.running = True

        def fake_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) == 2:
                global_var.running = False

        try:
            with mock.patch.object(front_module.time, "sleep", side_effect=fake_sleep):
                front_module.front_staticstic_thread()
        finally:
            global_var.running = False

        self.assertEqual(statistic_calls, [1])
        self.assertEqual(sleep_calls, [3])

    def test_client_enables_running_before_starting_front_statistics(self):
        client_path = APP / "x_tunnel" / "local" / "client.py"
        tree = ast.parse(client_path.read_text(encoding="utf-8"), filename=str(client_path))
        start_node = next(node for node in tree.body
                          if isinstance(node, ast.FunctionDef) and node.name == "start")
        running_line = None
        front_init_line = None
        for node in ast.walk(start_node):
            if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Attribute) and target.attr == "running" and
                    isinstance(node.value, ast.Constant) and node.value.value is True
                    for target in node.targets):
                running_line = node.lineno
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and \
                    isinstance(node.value.func, ast.Attribute) and \
                    isinstance(node.value.func.value, ast.Name) and \
                    node.value.func.value.id == "front_dispatcher" and \
                    node.value.func.attr == "init":
                front_init_line = node.lineno

        self.assertIsNotNone(running_line)
        self.assertIsNotNone(front_init_line)
        self.assertLess(running_line, front_init_line)


if __name__ == "__main__":
    unittest.main()
