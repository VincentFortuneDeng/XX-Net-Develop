import importlib.util
from pathlib import Path
import queue
import socket
import sys
import threading
import time
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
http2_stream = load_module("front_base.http2_stream", APP / "lib" / "noarch" / "front_base" / "http2_stream.py")
http1 = load_module("front_base.http1", APP / "lib" / "noarch" / "front_base" / "http1.py")


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def time(self):
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


class FakeH1Response:
    def __init__(self, chunks, content_length=None, clock=None, cost=0):
        self.status = 200
        self.headers = {}
        self.content_length = content_length
        self.chunked = False
        self.chunks = list(chunks)
        self.clock = clock
        self.cost = cost

    def begin(self, timeout):
        pass

    def read(self, timeout):
        if self.clock is not None:
            if timeout < self.cost:
                self.clock.now += timeout
                raise socket.timeout("body read deadline")
            self.clock.now += self.cost
        return self.chunks.pop(0) if self.chunks else b""


def run_h1_response(response, timeout=1):
    logger = types.SimpleNamespace(
        debug=lambda *args: None, warn=lambda *args: None)
    config = types.SimpleNamespace(show_state_debug=False, http2_show_debug=False)
    task = http_common.Task(
        logger, config, b"GET", "example", b"/", {}, b"",
        queue.Queue(), b"url", timeout)
    worker = http1.Http1Worker.__new__(http1.Http1Worker)
    worker.logger = logger
    worker.config = config
    worker.ip_str = "test"
    worker.ssl_sock = types.SimpleNamespace(
        send=lambda data: len(data), getsockname=lambda: None,
        received_size=0, ip_str="test")
    worker.record_active = lambda *args: None
    worker.get_host = lambda host: host
    worker.get_trace = lambda: "trace"
    worker.retry_task_cb = mock.Mock()
    worker.close = mock.Mock()
    worker.ip_manager = types.SimpleNamespace(recheck_ip=mock.Mock())
    worker.idle_cb = mock.Mock()
    worker.transfered_size = 0
    worker.processed_tasks = 0
    worker.accept_task = False
    worker.task = task
    with mock.patch.object(http1.simple_http_client, "Response", return_value=response):
        worker.request_task(task)
    return worker, task


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

    def test_known_length_read_all_is_complete_before_worker_finish(self):
        config = types.SimpleNamespace(show_state_debug=False)
        task = http_common.Task(
            None, config, "GET", "example", b"/", {}, b"", None, b"url", 1)
        task.content_length = 3
        task.put_data(b"abc")

        self.assertEqual(task.read_all(), b"abc")
        self.assertTrue(task.response_complete)
        self.assertFalse(task.finished)

    def test_dispatcher_overload_wait_never_exceeds_timeout(self):
        dispatcher = http_dispatcher.HttpsDispatcher.__new__(http_dispatcher.HttpsDispatcher)
        dispatcher.task_count = 1
        dispatcher.config = types.SimpleNamespace(max_task_num=0)
        dispatcher.logger = types.SimpleNamespace(warn=lambda *args: None)

        with mock.patch.object(http_dispatcher.time, "sleep") as sleep:
            result = dispatcher.request("GET", "example.com", b"/", {}, b"", timeout=0.2)

        self.assertIsNone(result)
        sleep.assert_called_once_with(0.2)

    def test_timeout_log_preserves_xtunnel_path_and_trace(self):
        class CaptureLogger:
            def __init__(self):
                self.warnings = []

            def warn(self, fmt, *args):
                self.warnings.append(fmt % args if args else fmt)

            def debug(self, *args):
                pass

        logger = CaptureLogger()
        dispatcher = http_dispatcher.HttpsDispatcher.__new__(http_dispatcher.HttpsDispatcher)
        dispatcher.task_count = 0
        dispatcher.task_count_lock = threading.Lock()
        dispatcher.config = types.SimpleNamespace(max_task_num=10, show_state_debug=False)
        dispatcher.logger = logger
        dispatcher.request_queue = queue.Queue()
        dispatcher.retry_log_throttle = types.SimpleNamespace(allow=lambda key: (True, 0))
        dispatcher.trigger_create_worker_cv = types.SimpleNamespace(notify=lambda: None)
        dispatcher.fail_num = 0
        dispatcher.continue_fail_num = 0

        created_tasks = []
        real_task = http_dispatcher.http_common.Task

        def capture_task(*args):
            task = real_task(*args)
            created_tasks.append(task)
            return task

        class TimeoutQueue:
            def get(self, timeout):
                created_tasks[0].set_state("h1_req_sent")
                raise queue.Empty()

        now = 0.0

        def next_time():
            nonlocal now
            now += 0.001
            return now

        with mock.patch.object(http_dispatcher.http_common, "Task", side_effect=capture_task), \
                mock.patch.object(http_dispatcher, "Queue", TimeoutQueue), \
                mock.patch.object(http_common.time, "time", side_effect=next_time):
            result = dispatcher.request(
                "POST", "ny3.xx-net.org", b"/",
                {b"X-Path": b"/data?tid=634&session_id=test-session-secret", b"X-Test": b"test-sensitive-marker"},
                b"payload", timeout=1,
            )

        self.assertIsNone(result)
        self.assertEqual(len(logger.warnings), 1)
        warning = logger.warnings[0]
        self.assertIn("/data?tid=634", warning)
        self.assertIn("h1_req_sent", warning)
        self.assertIn("get_response", warning)
        self.assertIn("session_id=[redacted]", warning)
        self.assertNotIn("test-session-secret", warning)
        self.assertNotIn("test-sensitive-marker", warning)

    def test_close_all_workers_includes_busy_workers(self):
        dispatcher = http_dispatcher.HttpsDispatcher.__new__(http_dispatcher.HttpsDispatcher)
        busy = types.SimpleNamespace(accept_task=False, close=mock.Mock())
        idle = types.SimpleNamespace(accept_task=True, close=mock.Mock())
        dispatcher.workers = [busy, idle]
        dispatcher.h1_num = 1
        dispatcher.h2_num = 1

        dispatcher.close_all_worker("stop")

        busy.close.assert_called_once_with("stop")
        idle.close.assert_called_once_with("stop")
        self.assertEqual(dispatcher.workers, [])
        self.assertEqual((dispatcher.h1_num, dispatcher.h2_num), (0, 0))

    def test_expired_queued_task_is_not_sent_to_worker(self):
        dispatcher = http_dispatcher.HttpsDispatcher.__new__(http_dispatcher.HttpsDispatcher)
        dispatcher.running = True
        dispatcher.request_queue = queue.Queue()
        dispatcher.logger = types.SimpleNamespace(
            debug=lambda *args: None, warn=lambda *args: None,
            exception=lambda *args: None)
        dispatcher.ping_speed_ip_str_last_active = {}
        dispatcher.wait_a_worker_cv = types.SimpleNamespace(notify=lambda: None)
        dispatcher.trigger_create_worker_cv = types.SimpleNamespace(notify=lambda: None)
        worker = types.SimpleNamespace(
            ip_str="test", request=mock.Mock(), get_score=lambda: 0,
            last_request_time=0)
        dispatcher.get_worker = lambda: worker
        config = types.SimpleNamespace(show_state_debug=False)
        task = http_common.Task(
            dispatcher.logger, config, "POST", "example", b"/data", {}, b"payload",
            queue.Queue(), b"url", 0.01)
        task.deadline = time.monotonic() - 1
        dispatcher.request_queue.put(task)
        dispatcher.request_queue.put(None)

        dispatcher.dispatcher()

        worker.request.assert_not_called()
        self.assertTrue(task.finished)

    def test_h2_end_stream_is_complete_but_failure_is_not(self):
        logger = types.SimpleNamespace(
            debug=lambda *args: None, warn=lambda *args: None,
            error=lambda *args: None)
        config = types.SimpleNamespace(show_state_debug=False)

        def make_stream(reason):
            task = http_common.Task(
                logger, config, "GET", "example", b"/", {}, b"",
                queue.Queue(), b"url", 1)
            task.responsed = True
            stream = http2_stream.Stream.__new__(http2_stream.Stream)
            stream.task = task
            stream.state = http2_stream.STATE_OPEN
            stream.stream_id = 1
            stream.connection = types.SimpleNamespace(retry_task_cb=mock.Mock())
            stream._close_cb = lambda *args: None
            stream.close(reason)
            return task

        self.assertTrue(make_stream("end stream").response_complete)
        self.assertFalse(make_stream("recv fail").response_complete)

    def test_h1_complete_and_truncated_bodies_have_distinct_completion(self):
        worker, complete = run_h1_response(FakeH1Response([b"abc", b""]))
        self.assertTrue(complete.response_complete)
        worker.idle_cb.assert_called_once_with()

        worker, truncated = run_h1_response(FakeH1Response([b"abc", b""], content_length=10))
        self.assertFalse(truncated.response_complete)
        worker.close.assert_called_once_with("down fail")
        worker.idle_cb.assert_not_called()

    def test_h1_body_reads_share_original_absolute_deadline(self):
        clock = Clock()
        response = FakeH1Response([b"aa", b"bb", b"cc"], clock=clock, cost=0.04)
        with mock.patch.object(http1.time, "time", side_effect=clock.time), \
                mock.patch.object(http1.time, "monotonic", side_effect=clock.monotonic):
            worker, task = run_h1_response(response, timeout=0.1)

        self.assertLessEqual(clock.now, 0.100001)
        self.assertEqual(task.body_len, 4)
        self.assertFalse(task.response_complete)
        worker.close.assert_called_once_with("read fail")
        worker.idle_cb.assert_not_called()

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
