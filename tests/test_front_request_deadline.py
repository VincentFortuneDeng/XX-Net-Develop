import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "Coding" / "XX-Net-5.16.7.1" / "code" / "default"
sys.path.insert(0, str(APP / "lib" / "noarch"))


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
)
namespace("env_info", data_path=tempfile.gettempdir())


class Logger:
    def warn(self, *args):
        pass


class Throttle:
    def allow(self, key):
        return True, 0


namespace("xlog", getLogger=lambda *args, **kwargs: Logger(), LogThrottle=lambda seconds: Throttle())

SPEC = importlib.util.spec_from_file_location(
    "x_tunnel.local.front_dispatcher",
    APP / "x_tunnel" / "local" / "front_dispatcher.py",
)
front_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = front_module
SPEC.loader.exec_module(front_module)


class Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Front:
    def __init__(self, clock, results):
        self.clock = clock
        self.results = list(results)
        self.timeouts = []

    def request(self, method, host, path, headers, data, timeout):
        self.timeouts.append(timeout)
        cost, status = self.results.pop(0)
        self.clock.sleep(min(cost, timeout))
        return "content", status, types.SimpleNamespace(headers={})


class FrontRequestDeadlineTest(unittest.TestCase):
    def run_request(self, timeout, results, select_cost=0.0, sleep_cost=None):
        clock = Clock()
        front = Front(clock, results)
        select_timeouts = []

        def get_front(host, remaining):
            select_timeouts.append(remaining)
            clock.sleep(select_cost)
            return front

        sleep_side_effect = clock.sleep if sleep_cost is None else lambda seconds: clock.sleep(min(seconds, sleep_cost))
        with mock.patch.object(front_module.time, "time", side_effect=clock.time), \
                mock.patch.object(front_module.time, "monotonic", side_effect=clock.time), \
                mock.patch.object(front_module.time, "sleep", side_effect=sleep_side_effect), \
                mock.patch.object(front_module, "get_front", side_effect=get_front):
            result = front_module.request("GET", "example.com", timeout=timeout)

        return result, clock.now, select_timeouts, front.timeouts

    def test_selection_and_request_share_one_deadline(self):
        result, elapsed, select_timeouts, request_timeouts = self.run_request(
            0.1, [(0.06, 200)], select_cost=0.06,
        )

        self.assertEqual(result[1], 200)
        self.assertLessEqual(elapsed, 0.100001)
        self.assertEqual(select_timeouts, [0.1])
        self.assertLessEqual(request_timeouts[0], 0.040001)

    def test_retry_success_still_respects_total_deadline(self):
        result, elapsed, select_timeouts, request_timeouts = self.run_request(
            0.1, [(0.02, 500), (0.01, 200)], select_cost=0.01, sleep_cost=0.02,
        )

        self.assertEqual(result[1], 200)
        self.assertLessEqual(elapsed, 0.100001)
        self.assertTrue(all(value <= 0.1 for value in select_timeouts + request_timeouts))

    def test_zero_timeout_starts_no_suboperation(self):
        with mock.patch.object(front_module, "get_front") as get_front:
            result = front_module.request("GET", "example.com", timeout=0)

        self.assertEqual(result[1], 603)
        get_front.assert_not_called()

    def test_accepted_statuses_return_without_retry(self):
        for status in (200, 521, 400, 404):
            with self.subTest(status=status):
                result, elapsed, select_timeouts, request_timeouts = self.run_request(
                    0.2, [(0.01, status)],
                )
                self.assertEqual(result[1], status)
                self.assertEqual(len(request_timeouts), 1)


if __name__ == "__main__":
    unittest.main()
