import importlib.util
from pathlib import Path
import queue
import sys
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "Coding" / "XX-Net-5.16.7.1" / "code" / "default"
sys.path.insert(0, str(APP / "lib" / "noarch"))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


http_common = load_module("front_body_http_common", APP / "lib" / "noarch" / "front_base" / "http_common.py")
front_base = types.ModuleType("front_base")
front_base.__path__ = [str(APP / "lib" / "noarch" / "front_base")]
front_base.http_common = http_common
sys.modules["front_base"] = front_base
sys.modules["front_base.http_common"] = http_common
sys.modules["front_base.http1"] = types.SimpleNamespace(Http1Worker=object)
sys.modules["front_base.http2_connection"] = types.SimpleNamespace(Http2Worker=object, Stream=object)
http_dispatcher = load_module("front_base.http_dispatcher", APP / "lib" / "noarch" / "front_base" / "http_dispatcher.py")


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class SlowBodyQueue:
    def __init__(self, clock, chunks, cost):
        self.clock = clock
        self.chunks = list(chunks)
        self.cost = cost

    def get(self, timeout):
        if timeout < self.cost:
            self.clock.now += timeout
            raise queue.Empty()
        self.clock.now += self.cost
        return self.chunks.pop(0)


class FrontBodyDeadlineTest(unittest.TestCase):
    def test_body_chunks_share_original_absolute_deadline(self):
        clock = Clock()
        body_queue = SlowBodyQueue(clock, [b"aa", b"bb", b"cc"], 0.04)
        config = types.SimpleNamespace(show_state_debug=False)
        with mock.patch.object(http_common.time, "monotonic", side_effect=clock.monotonic):
            task = http_common.Task(None, config, "GET", "example", b"/", {}, b"", None, b"url", 0.1)
            task.content_length = 6
            task.body_queue = body_queue
            content = task.read_all()

        self.assertEqual(content, b"aabb")
        self.assertAlmostEqual(clock.now, 0.1)

    def test_dispatcher_overload_wait_never_exceeds_timeout(self):
        dispatcher = http_dispatcher.HttpsDispatcher.__new__(http_dispatcher.HttpsDispatcher)
        dispatcher.task_count = 1
        dispatcher.config = types.SimpleNamespace(max_task_num=0)
        dispatcher.logger = types.SimpleNamespace(warn=lambda *args: None)

        with mock.patch.object(http_dispatcher.time, "sleep") as sleep:
            result = dispatcher.request("GET", "example.com", b"/", {}, b"", timeout=0.2)

        self.assertIsNone(result)
        sleep.assert_called_once_with(0.2)

    def test_direct_front_does_not_replace_budget_or_sleep_past_it(self):
        client = types.ModuleType("direct_front_http_client")
        client.request = mock.Mock(side_effect=OSError("injected failure"))
        direct = load_module("direct_front_deadline", APP / "x_tunnel" / "local" / "direct_front.py")
        clock = Clock()
        with mock.patch.dict(sys.modules, {"simple_http_client": client}), \
                mock.patch.object(direct, "simple_http_client", client), \
                mock.patch.object(direct.time, "monotonic", side_effect=clock.monotonic), \
                mock.patch.object(direct.time, "sleep", side_effect=lambda seconds: setattr(clock, "now", clock.now + seconds)):
            result = direct.request("GET", "example.com", timeout=0.2)

        self.assertEqual(result[1], 602)
        self.assertEqual(client.request.call_args.kwargs["timeout"], 0.2)
        self.assertLessEqual(clock.now, 0.2)


if __name__ == "__main__":
    unittest.main()
