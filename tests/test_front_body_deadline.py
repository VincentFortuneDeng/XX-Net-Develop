import importlib.util
import gc
from pathlib import Path
import queue
import socket
import ssl
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
sys.modules["front_base.openssl_wrap"] = types.SimpleNamespace(SSLConnection=object)
connect_manager = load_module("front_base.connect_manager", APP / "lib" / "noarch" / "front_base" / "connect_manager.py")
http_dispatcher = load_module("front_base.http_dispatcher", APP / "lib" / "noarch" / "front_base" / "http_dispatcher.py")
http2_stream = load_module("front_base.http2_stream", APP / "lib" / "noarch" / "front_base" / "http2_stream.py")
http1 = load_module("front_base.http1", APP / "lib" / "noarch" / "front_base" / "http1.py")
http2_connection = load_module("front_base.http2_connection", APP / "lib" / "noarch" / "front_base" / "http2_connection.py")


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
    def __init__(self, chunks, content_length=None, clock=None, cost=0,
                 eof_on_empty=True, complete_chunked=True):
        self.status = 200
        self.headers = {}
        self.content_length = content_length
        self.chunked = False
        self.chunked_complete = False
        self.eof = False
        self.eof_on_empty = eof_on_empty
        self.complete_chunked = complete_chunked
        self.chunks = list(chunks)
        self.clock = clock
        self.cost = cost
        self.read_calls = []

    def begin(self, timeout):
        pass

    def read(self, read_len=None, timeout=60):
        self.read_calls.append((read_len, timeout))
        if self.clock is not None:
            if timeout < self.cost:
                self.clock.now += timeout
                raise socket.timeout("body read deadline")
            self.clock.now += self.cost
        if timeout <= 0 and not self.chunks:
            return b""
        data = self.chunks.pop(0) if self.chunks else b""
        if not data:
            if self.chunked:
                self.chunked_complete = self.complete_chunked
            elif self.eof_on_empty:
                self.eof = True
        if read_len is not None and len(data) > read_len:
            self.chunks.insert(0, data[read_len:])
            data = data[:read_len]
        return data


def run_h1_response(response, timeout=1, send_cost=0, clock=None, method=b"GET", body=b"",
                    on_response_begin=None, on_send=None):
    logger = types.SimpleNamespace(
        debug=lambda *args: None, warn=lambda *args: None)
    config = types.SimpleNamespace(show_state_debug=False, http2_show_debug=False)
    task = http_common.Task(
        logger, config, method, "example", b"/", {}, body,
        queue.Queue(), b"url", timeout)
    worker = http1.Http1Worker.__new__(http1.Http1Worker)
    worker.logger = logger
    worker.config = config
    worker.ip_str = "test"
    send_timeouts = []
    def send(data):
        send_timeouts.append(getattr(worker.ssl_sock, "timeout", None))
        if on_send is not None:
            on_send(worker, data)
        if clock is not None:
            clock.now += send_cost
        return len(data)

    worker.ssl_sock = types.SimpleNamespace(
        send=send, getsockname=lambda: None,
        received_size=0, ip_str="test")
    worker.ssl_sock.settimeout = lambda value: setattr(worker.ssl_sock, "timeout", value)
    worker.last_recv_time = time.time()
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
    worker.send_timeouts = send_timeouts
    if on_response_begin is not None:
        response.begin = lambda timeout: on_response_begin(task)
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

    def test_response_begin_never_extends_zero_budget(self):
        response = http1.simple_http_client.Response.__new__(http1.simple_http_client.Response)
        response.read_buffer = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
        response.buffer_start = 0

        response.begin(timeout=0)

        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_length, b"0")

    def test_response_begin_header_budget_is_not_extended(self):
        response = http1.simple_http_client.Response.__new__(http1.simple_http_client.Response)
        response.read_line = mock.Mock(return_value=b"HTTP/1.1 200 OK\r\n")
        response.read_headers = mock.Mock(return_value=b"")

        response.begin(timeout=0)

        self.assertEqual(response.read_headers.call_args.args[0], 0)

    def test_real_eof_is_sticky_but_zero_budget_empty_read_is_not_eof(self):
        response = http1.simple_http_client.Response.__new__(http1.simple_http_client.Response)
        response.read_buffer = b""
        response.buffer_start = 0
        response.eof = False
        response.select2 = types.SimpleNamespace(select=lambda timeout: [])
        self.assertEqual(response._read_plain(None, 0), b"")
        self.assertFalse(response.eof)

        class EofSocket:
            def __init__(self):
                self.calls = 0

            def recv(self, size):
                self.calls += 1
                return b""

        response.sock = EofSocket()
        self.assertEqual(response.recv(timeout=0.1), b"")
        self.assertTrue(response.eof)
        self.assertEqual(response.recv(timeout=0.1), b"")
        self.assertEqual(response.sock.calls, 1)

    def test_chunked_zero_block_consumes_all_trailers(self):
        response = http1.simple_http_client.Response.__new__(http1.simple_http_client.Response)
        response.read_buffer = b"0\r\nX-Trailer: yes\r\n\r\nNEXT"
        response.buffer_start = 0

        self.assertEqual(response._read_chunked(1), b"")
        self.assertEqual(response.read_buffer[response.buffer_start:], b"NEXT")
        self.assertTrue(response.chunked_complete)

    def test_mixed_read_and_read_all_counts_each_byte_once(self):
        config = types.SimpleNamespace(show_state_debug=False)
        task = http_common.Task(None, config, "GET", "example", b"/", {}, b"", None, b"url", 1)
        task.content_length = 5
        task.put_data(b"ab")
        task.put_data(b"cde")

        self.assertEqual(task.read(2), b"ab")
        self.assertEqual(task.read_all(), b"cde")
        self.assertEqual(task.body_readed, 5)

    def test_concurrent_finish_delivers_one_sentinel_and_keeps_first_state(self):
        config = types.SimpleNamespace(show_state_debug=False)
        task = http_common.Task(None, config, "GET", "example", b"/", {}, b"", None, b"url", 1)
        barrier = threading.Barrier(9)
        finish_barrier = threading.Barrier(8)
        original_put_data = task.put_data

        def gated_put_data(data):
            if data in (b"", ""):
                finish_barrier.wait(timeout=2)
            return original_put_data(data)

        task.put_data = gated_put_data

        def finish():
            barrier.wait()
            task.finish(complete=False)

        threads = [threading.Thread(target=finish) for _ in range(8)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(1)
        task.finish(complete=True)

        sentinels = 0
        while True:
            try:
                item = task.body_queue.get_nowait()
            except queue.Empty:
                break
            if item in (b"", ""):
                sentinels += 1
        self.assertEqual(sentinels, 1)
        self.assertFalse(task.response_complete)

    def test_retry_claim_is_unique_until_worker_accepts_attempt(self):
        config = types.SimpleNamespace(show_state_debug=False)
        task = http_common.Task(None, config, "GET", "example", b"/", {}, b"", None, b"url", 1)

        self.assertTrue(task.claim_retry())
        self.assertFalse(task.claim_retry())
        task.clear_retry()
        self.assertTrue(task.claim_retry())

    def test_final_response_body_semantics(self):
        has_body = http1.simple_http_client.response_has_body
        self.assertTrue(has_body(b"GET", 200))
        self.assertFalse(has_body(b"HEAD", 200))
        self.assertFalse(has_body(b"GET", 204))
        self.assertFalse(has_body(b"GET", 304))
        self.assertFalse(has_body(b"GET", 100))

    def test_dispatcher_overload_wait_never_exceeds_timeout(self):
        dispatcher = http_dispatcher.HttpsDispatcher.__new__(http_dispatcher.HttpsDispatcher)
        dispatcher.task_count = 1
        dispatcher._state_lock = threading.RLock()
        dispatcher.running = True
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
        dispatcher._state_lock = threading.RLock()
        dispatcher.running = True
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
        dispatcher.get_worker = lambda deadline=None: worker
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
            stream._close_lock = threading.RLock()
            stream._closed = False
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
        worker.close.assert_called_once_with("eof close")
        worker.idle_cb.assert_not_called()

        worker, truncated = run_h1_response(FakeH1Response([b"abc", b""], content_length=10))
        self.assertFalse(truncated.response_complete)
        worker.close.assert_called_once_with("down fail")
        worker.idle_cb.assert_not_called()

    def test_h1_zero_budget_empty_read_is_not_complete_eof(self):
        response = FakeH1Response([], eof_on_empty=False)
        with mock.patch.object(http1.Http1Worker, "_send_data", lambda self, task, data, expected_attempt=None: None):
            worker, task = run_h1_response(response, timeout=0)

        self.assertFalse(task.response_complete)
        self.assertFalse(response.eof)
        worker.close.assert_called_once_with("down fail")
        worker.idle_cb.assert_not_called()

    def test_h1_fixed_length_reads_exact_remaining_bytes(self):
        worker, task = run_h1_response(FakeH1Response([b"abcd"], content_length=3))

        self.assertEqual(task.body_len, 3)
        self.assertEqual(task.body_queue.get_nowait(), b"abc")
        self.assertTrue(task.response_complete)
        self.assertEqual(task.body_queue.get_nowait(), "")
        worker.idle_cb.assert_called_once_with()

    def test_h1_chunked_requires_protocol_completion(self):
        complete_response = FakeH1Response([b"abc", b""])
        complete_response.chunked = True
        worker, task = run_h1_response(complete_response)
        self.assertTrue(task.response_complete)
        worker.idle_cb.assert_called_once_with()

        truncated_response = FakeH1Response([b"abc", b""], complete_chunked=False, eof_on_empty=False)
        truncated_response.chunked = True
        worker, task = run_h1_response(truncated_response)
        self.assertFalse(task.response_complete)
        worker.close.assert_called_once_with("down fail")
        worker.idle_cb.assert_not_called()

    def test_h1_final_no_body_responses_do_not_read_body(self):
        for method, status in ((b"HEAD", 200), (b"GET", 204), (b"GET", 304)):
            with self.subTest(method=method, status=status):
                response = FakeH1Response([], content_length=10)
                response.status = status
                response.read = mock.Mock(side_effect=AssertionError("无正文响应不得读取"))
                worker, task = run_h1_response(response, method=method)

                self.assertTrue(task.response_complete)
                response.read.assert_not_called()
                worker.idle_cb.assert_called_once_with()

    def test_h1_informational_response_waits_for_final_response(self):
        response = FakeH1Response([b"ok"], content_length=2)
        statuses = iter([100, 200])
        response.begin = mock.Mock(side_effect=lambda timeout: setattr(response, "status", next(statuses)))

        worker, task = run_h1_response(response)

        self.assertEqual(response.begin.call_count, 2)
        self.assertEqual(task.body_queue.get_nowait(), b"ok")
        self.assertTrue(task.response_complete)

    def test_h1_upgrade_response_is_not_reused_as_normal_empty_body(self):
        response = FakeH1Response([], content_length=None)
        response.status = 101

        worker, task = run_h1_response(response)

        self.assertFalse(task.response_complete)
        worker.close.assert_called_once_with("upgrade unsupported")
        worker.idle_cb.assert_not_called()

    def test_h1_send_receives_remaining_absolute_budget(self):
        clock = Clock()
        response = FakeH1Response([b"ok"], content_length=2, clock=clock)
        with mock.patch.object(http1.time, "time", side_effect=clock.time), \
                mock.patch.object(http1.time, "monotonic", side_effect=clock.monotonic):
            worker, task = run_h1_response(response, timeout=0.1, send_cost=0.01,
                                           clock=clock, method=b"POST", body=b"abc")

        self.assertEqual(len(worker.send_timeouts), 2)
        self.assertAlmostEqual(worker.send_timeouts[0], 0.1)
        self.assertAlmostEqual(worker.send_timeouts[1], 0.09)
        self.assertTrue(task.response_complete)

    def test_h1_old_attempt_stops_before_next_socket_send(self):
        response = FakeH1Response([], content_length=0)
        new_attempts = []

        def replace_attempt(worker, data):
            if len(data) != 65535 or new_attempts:
                return
            thread = threading.Thread(target=worker.task.begin_attempt)
            new_attempts.append(thread)
            thread.start()
            thread.join(0.05)

        worker, task = run_h1_response(
            response, method=b"POST", body=b"x" * 70000, on_send=replace_attempt)
        for thread in new_attempts:
            thread.join(1)
            self.assertFalse(thread.is_alive())

        self.assertEqual(task.attempt_id, 1)
        self.assertEqual(len(worker.send_timeouts), 2)
        worker.retry_task_cb.assert_called_once_with(task, expected_attempt=0)

    def test_h2_content_length_is_checked_at_end_stream(self):
        logger = types.SimpleNamespace(debug=lambda *args: None, warn=lambda *args: None,
                                       error=lambda *args: None)
        config = types.SimpleNamespace(show_state_debug=False)

        def finish_with(method=b"GET", declared=None, received=b""):
            task = http_common.Task(logger, config, method, "example", b"/", {}, b"",
                                    queue.Queue(), b"url", 1)
            task.content_length = declared
            if received:
                task.put_data(received)
            task.responsed = True
            stream = http2_stream.Stream.__new__(http2_stream.Stream)
            stream.task = task
            stream.state = http2_stream.STATE_OPEN
            stream._close_lock = threading.RLock()
            stream._closed = False
            stream.stream_id = 1
            stream.connection = types.SimpleNamespace(retry_task_cb=mock.Mock())
            stream._close_cb = lambda *args: None
            stream.close("end stream")
            return task

        self.assertFalse(finish_with(declared=10, received=b"abc").response_complete)
        self.assertTrue(finish_with(declared=3, received=b"abc").response_complete)
        self.assertTrue(finish_with(declared=None, received=b"abc").response_complete)
        self.assertTrue(finish_with(method=b"HEAD", declared=10).response_complete)

    def test_h2_no_body_stream_rejects_data(self):
        logger = types.SimpleNamespace(debug=lambda *args: None, warn=lambda *args: None,
                                       error=lambda *args: None)
        config = types.SimpleNamespace(show_state_debug=False)
        task = http_common.Task(logger, config, b"HEAD", "example", b"/", {}, b"",
                                queue.Queue(), b"url", 1)
        task.responsed = True
        stream = http2_stream.Stream.__new__(http2_stream.Stream)
        stream.response_headers = {b":status": [b"200"]}
        stream.task = task
        stream.state = http2_stream.STATE_OPEN
        stream._close_lock = threading.RLock()
        stream._closed = False
        stream.stream_id = 1
        stream.connection = types.SimpleNamespace(retry_task_cb=mock.Mock())
        stream._close_cb = lambda *args: None
        stream.receive_window_manager = types.SimpleNamespace(_handle_frame=lambda size: 0)
        frame = http2_stream.DataFrame(1)
        frame.data = b"illegal"

        stream.receive_frame(frame)

        self.assertEqual(task.body_len, 0)
        self.assertTrue(task.finished)
        self.assertFalse(task.response_complete)

    def test_upper_front_does_not_reject_head_entity_length(self):
        wait_tests = load_module("front_wait_test_helpers",
                                 ROOT / "tests" / "test_front_dispatcher_wait_isolation.py")
        front_module = wait_tests.front_module
        response = types.SimpleNamespace(
            headers={b"Content-Length": 5},
            task=types.SimpleNamespace(response_complete=True))
        front = types.SimpleNamespace(
            name="fake",
            request=mock.Mock(return_value=(b"", 200, response)))
        front_module.get_front = lambda host, timeout: front
        front_module.g.server_host = ""

        with mock.patch.object(front_module.time, "sleep") as sleep:
            content, status, returned = front_module.request("HEAD", "example", timeout=0.01)

        self.assertEqual((content, status), (b"", 200))
        self.assertIs(returned, response)
        self.assertEqual(front.request.call_count, 1)
        sleep.assert_not_called()

    def test_standalone_client_head_does_not_wait_for_declared_body(self):
        client = http1.simple_http_client.Client()
        sock = types.SimpleNamespace(send=lambda data: len(data), settimeout=lambda timeout: None)
        client.connect = lambda *args: sock
        response = types.SimpleNamespace(
            status=200, headers={b"Content-Length": b"5"},
            begin=mock.Mock(),
            getheader=lambda key, default: b"5",
            read=mock.Mock(side_effect=AssertionError("HEAD 不得读取正文")))

        with mock.patch.object(http1.simple_http_client, "Response", return_value=response):
            result = client.request("HEAD", "http://example.com/")

        self.assertIs(result, response)
        response.read.assert_not_called()
        self.assertEqual(response.text, b"")

    def test_standalone_client_skips_100_and_reads_final_response(self):
        client = http1.simple_http_client.Client(timeout=0.2)
        client_sock, server_sock = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server_sock.close)
        def connect(*args):
            client.sock = client_sock
            return client_sock

        client.connect = connect
        server_sock.sendall(
            b"HTTP/1.1 100 Continue\r\n\r\n"
            b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")

        response = client.request("GET", "http://example.com/")

        self.assertIsNotNone(response)
        self.assertEqual(response.status, 200)

    def test_standalone_client_101_closes_connection(self):
        client = http1.simple_http_client.Client(timeout=0.2)
        client_sock, server_sock = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server_sock.close)

        def connect(*args):
            client.sock = client_sock
            return client_sock

        client.connect = connect
        server_sock.sendall(b"HTTP/1.1 101 Switching Protocols\r\n\r\n")

        response = client.request("GET", "http://example.com/")

        self.assertEqual(response.status, 101)
        self.assertIsNone(client.sock)

    def test_standalone_client_reads_eof_delimited_200_and_disables_reuse(self):
        client = http1.simple_http_client.Client(timeout=0.2)
        client_sock, server_sock = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server_sock.close)

        def connect(*args):
            client.sock = client_sock
            return client_sock

        client.connect = connect
        server_sock.sendall(b"HTTP/1.0 200 OK\r\n\r\nabc")
        server_sock.shutdown(socket.SHUT_WR)

        response = client.request("GET", "http://example.com/")

        self.assertEqual(response.text, b"abc")
        self.assertTrue(response.eof)
        self.assertIsNone(client.sock)

    def test_standalone_client_reads_non_200_fixed_body(self):
        client = http1.simple_http_client.Client(timeout=0.2)
        client_sock, server_sock = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server_sock.close)

        def connect(*args):
            client.sock = client_sock
            return client_sock

        client.connect = connect
        server_sock.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 3\r\n\r\nabc")

        response = client.request("GET", "http://example.com/")

        self.assertEqual(response.status, 404)
        self.assertEqual(response.text, b"abc")
        self.assertEqual(response.read_buffer[response.buffer_start:], b"")
        self.assertIs(client.sock, client_sock)

    def test_standalone_client_informational_and_body_share_one_deadline(self):
        clock = Clock()
        client = http1.simple_http_client.Client(timeout=0.12)
        sock = types.SimpleNamespace(send=lambda data: len(data), settimeout=lambda timeout: None)
        client.connect = lambda *args: sock
        response = types.SimpleNamespace(
            status=100, headers={b"Content-Length": b"3"}, text=b"",
            getheader=lambda key, default=None: {b"Content-Length": b"3"}.get(key, default),
            read=mock.Mock())
        timeouts = []

        def begin(timeout):
            timeouts.append(timeout)
            if timeout <= 0:
                raise socket.timeout("absolute deadline")
            clock.now += 0.06
            response.status = 100 if len(timeouts) < 3 else 200

        response.begin = begin
        with mock.patch.object(http1.simple_http_client, "Response", return_value=response), \
                mock.patch.object(http1.simple_http_client.time, "monotonic", side_effect=clock.monotonic):
            result = client.request("GET", "http://example.com/")

        self.assertIsNone(result)
        self.assertEqual(len(timeouts), 3)
        self.assertAlmostEqual(timeouts[0], 0.12)
        self.assertAlmostEqual(timeouts[1], 0.06)
        self.assertEqual(timeouts[2], 0)
        response.read.assert_not_called()

    def test_standalone_client_retries_partial_header_send(self):
        client = http1.simple_http_client.Client(timeout=0.2)
        sent = []

        class PartialSocket:
            def settimeout(self, timeout):
                pass

            def send(self, data):
                sent_now = min(7, len(data))
                sent.append(bytes(data[:sent_now]))
                return sent_now

        sock = PartialSocket()
        client.connect = lambda *args: sock
        response = types.SimpleNamespace(
            status=200, headers={b"Content-Length": b"0"}, text=b"",
            begin=mock.Mock(),
            getheader=lambda key, default=None: b"0")

        with mock.patch.object(http1.simple_http_client, "Response", return_value=response):
            result = client.request(
                "GET", "http://example.com/", headers={"X-Pad": "x" * 1400})

        self.assertIsNotNone(result)
        request_data = b"".join(sent)
        self.assertIn(b"X-Pad: " + b"x" * 1400 + b"\r\n", request_data)
        self.assertTrue(request_data.endswith(b"\r\n\r\n"))

    def test_response_destruction_does_not_close_borrowed_socket(self):
        client_sock, server_sock = socket.socketpair()
        self.addCleanup(client_sock.close)
        self.addCleanup(server_sock.close)
        response = http1.simple_http_client.Response(client_sock)

        del response
        gc.collect()

        self.assertNotEqual(client_sock.fileno(), -1)
        server_sock.sendall(b"still-open")
        self.assertEqual(client_sock.recv(10), b"still-open")

    def test_standalone_client_send_error_closes_cached_socket(self):
        client = http1.simple_http_client.Client(timeout=0.2)

        class FailingSocket:
            def __init__(self):
                self.closed = False

            def settimeout(self, timeout):
                pass

            def send(self, data):
                raise socket.error("send failed")

            def close(self):
                self.closed = True

        sock = FailingSocket()

        def connect(*args):
            client.sock = sock
            return sock

        client.connect = connect
        result = client.request("GET", "http://example.com/")

        self.assertIsNone(result)
        self.assertIsNone(client.sock)
        self.assertTrue(sock.closed)

    def test_standalone_client_header_error_closes_cached_socket(self):
        client = http1.simple_http_client.Client(timeout=0.2)
        client_sock, server_sock = socket.socketpair()
        self.addCleanup(server_sock.close)

        def connect(*args):
            client.sock = client_sock
            return client_sock

        client.connect = connect
        server_sock.sendall(b"not-http\r\n\r\n")
        server_sock.shutdown(socket.SHUT_WR)

        result = client.request("GET", "http://example.com/")

        self.assertIsNone(result)
        self.assertIsNone(client.sock)
        self.assertEqual(client_sock.fileno(), -1)

    def test_standalone_client_incomplete_body_closes_cached_socket(self):
        client = http1.simple_http_client.Client(timeout=0.2)
        client_sock, server_sock = socket.socketpair()
        self.addCleanup(server_sock.close)

        def connect(*args):
            client.sock = client_sock
            return client_sock

        client.connect = connect
        server_sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nabc")
        server_sock.shutdown(socket.SHUT_WR)

        try:
            result = client.request("GET", "http://example.com/")
        except Exception as error:
            self.fail("正文不完整应转换为请求失败并清理缓存，而非泄漏异常：%r" % error)

        self.assertIsNone(result)
        self.assertIsNone(client.sock)
        self.assertEqual(client_sock.fileno(), -1)

    def test_standalone_client_incomplete_chunked_body_fails_and_closes(self):
        client = http1.simple_http_client.Client(timeout=0.2)
        client_sock, server_sock = socket.socketpair()
        self.addCleanup(server_sock.close)

        def connect(*args):
            client.sock = client_sock
            return client_sock

        client.connect = connect
        server_sock.sendall(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\n")
        server_sock.shutdown(socket.SHUT_WR)

        result = client.request("GET", "http://example.com/")

        self.assertIsNone(result)
        self.assertIsNone(client.sock)
        self.assertEqual(client_sock.fileno(), -1)

    def test_standalone_client_second_request_reuses_completed_connection(self):
        client = http1.simple_http_client.Client(timeout=0.2)
        client_sock, server_sock = socket.socketpair()
        client.sock = client_sock
        client.host = b"example.com"
        client.port = 80
        client.tls = False
        self.addCleanup(client.close)
        self.addCleanup(server_sock.close)
        server_errors = []

        def serve_two_requests():
            try:
                reader = server_sock.makefile("rb")
                for text in (b"one", b"two"):
                    while reader.readline() not in (b"\r\n", b""):
                        pass
                    server_sock.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\n" + text)
                reader.close()
            except Exception as error:
                server_errors.append(error)

        server = threading.Thread(target=serve_two_requests)
        server.start()
        first = client.request("GET", "http://example.com/first")
        second = client.request("GET", "http://example.com/second")
        server.join(1)

        self.assertFalse(server.is_alive())
        self.assertEqual(server_errors, [])
        self.assertEqual((first.text, second.text), (b"one", b"two"))
        self.assertIs(client.sock, client_sock)

    def test_standalone_client_successful_host_change_closes_old_socket(self):
        client = http1.simple_http_client.Client(timeout=0.2)
        first, first_peer = socket.socketpair()
        second, second_peer = socket.socketpair()
        for sock in (first, first_peer, second, second_peer):
            self.addCleanup(sock.close)
        sockets = iter((first, second))
        client.direct_connect = lambda *args: next(sockets)

        self.assertIs(client.connect(b"one.example", 80, False), first)
        self.assertIs(client.connect(b"two.example", 80, False), second)

        self.assertEqual(first.fileno(), -1)
        self.assertIs(client.sock, second)
        self.assertNotEqual(second.fileno(), -1)

    def test_standalone_client_proxy_connect_failure_closes_new_socket(self):
        class FailingProxySocket:
            def __init__(self):
                self.closed = False

            def set_proxy(self, **kwargs):
                pass

            def setsockopt(self, *args):
                pass

            def settimeout(self, timeout):
                pass

            def connect(self, address):
                raise OSError("injected proxy connect failure")

            def close(self):
                self.closed = True

        sock = FailingProxySocket()
        client = http1.simple_http_client.Client(proxy={
            "type": "http", "host": "127.0.0.1", "port": 8087,
            "user": None, "pass": None}, timeout=0.2)
        fake_socks = types.SimpleNamespace(
            socksocket=lambda family: sock)
        with mock.patch.dict(sys.modules, {"socks": fake_socks}):
            result = client.request("GET", "http://example.com/")

        self.assertIsNone(result)
        self.assertTrue(sock.closed)

    def test_standalone_client_tls_wrap_failure_closes_raw_socket(self):
        class RawSocket:
            def __init__(self):
                self.closed = False

            def settimeout(self, timeout):
                pass

            def close(self):
                self.closed = True

        class FailingContext:
            def wrap_socket(self, raw_sock, server_hostname):
                raise ssl.SSLError("injected TLS wrap failure")

        raw = RawSocket()
        client = http1.simple_http_client.Client(timeout=0.2)
        client.cert = "test-ca"
        client.direct_connect = lambda *args: raw
        client.ssl_context = FailingContext()
        with mock.patch.object(http1.simple_http_client.os.path, "isfile", return_value=True):
            result = client.request("GET", "https://example.com/")

        self.assertIsNone(result)
        self.assertTrue(raw.closed)

    def test_response_read_line_eof_fails_without_busy_loop(self):
        client_sock, server_sock = socket.socketpair()
        response = http1.simple_http_client.Response(client_sock)
        server_sock.shutdown(socket.SHUT_WR)
        errors = []

        def read_status_line():
            try:
                response.read_line(timeout=60)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=read_status_line)
        thread.start()
        thread.join(0.2)
        if thread.is_alive():
            client_sock.close()
            server_sock.close()
            thread.join(1)
            self.fail("EOF 后 read_line 仍持续空读，未立即失败")
        server_sock.close()
        client_sock.close()

        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], socket.error)

    def test_standalone_direct_connect_shares_deadline_across_addresses(self):
        clock = Clock()

        class FakeSocket:
            def __init__(self, cost=0, fail=False):
                self.cost = cost
                self.fail = fail
                self.timeouts = []
                self.closed = False

            def setsockopt(self, *args):
                pass

            def settimeout(self, timeout):
                self.timeouts.append(timeout)

            def connect(self, address):
                clock.now += self.cost
                if self.fail:
                    raise socket.timeout("connect deadline")

            def close(self):
                self.closed = True

        first = FakeSocket(cost=0.07, fail=True)
        second = FakeSocket()
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.0.2.1", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.0.2.2", 443)),
        ]
        with mock.patch.object(http1.simple_http_client.socket, "getaddrinfo", return_value=addresses), \
                mock.patch.object(http1.simple_http_client.socket, "socket", side_effect=[first, second]), \
                mock.patch.object(http1.simple_http_client.time, "monotonic", side_effect=clock.monotonic):
            result = http1.simple_http_client.Client.direct_connect(
                b"example.com", 443, timeout=0.1)

        self.assertIs(result, second)
        self.assertAlmostEqual(first.timeouts[0], 0.1)
        self.assertAlmostEqual(second.timeouts[0], 0.03)

    def test_standalone_tls_handshake_uses_remaining_connect_deadline(self):
        clock = Clock()
        client = http1.simple_http_client.Client(timeout=0.2)
        client.cert = "test-ca"
        sock = types.SimpleNamespace(timeouts=[], settimeout=lambda timeout: sock.timeouts.append(timeout))
        wrapped = object()
        wrap_timeouts = []

        def direct_connect(host, port, timeout):
            clock.now += 0.07
            sock.settimeout(timeout)
            return sock

        class FakeContext:
            def wrap_socket(self, raw_sock, server_hostname):
                wrap_timeouts.append(raw_sock.timeouts[-1])
                clock.now += 0.04
                return wrapped

        client.direct_connect = direct_connect
        client.ssl_context = FakeContext()
        with mock.patch.object(http1.simple_http_client.os.path, "isfile", return_value=True), \
                mock.patch.object(http1.simple_http_client.time, "monotonic", side_effect=clock.monotonic):
            result = client.connect(b"example.com", 443, True, timeout=0.1)

        self.assertIs(result, wrapped)
        self.assertEqual(len(wrap_timeouts), 1)
        self.assertAlmostEqual(wrap_timeouts[0], 0.03)

    def test_h1_response_delivery_is_rejected_after_retry_claim(self):
        response = FakeH1Response([], content_length=0)
        _, h1_task = run_h1_response(
            response, on_response_begin=lambda task: setattr(task, "retry_pending", True))
        self.assertFalse(h1_task.responsed)
        self.assertTrue(h1_task.queue.empty())

    def test_h2_response_delivery_is_rejected_after_retry_claim(self):
        h2_task = self.make_task()
        h2_task.retry_pending = True
        stream = http2_stream.Stream.__new__(http2_stream.Stream)
        stream.task = h2_task
        stream.response_headers = {b":status": [b"200"]}
        stream.logger = types.SimpleNamespace(error=lambda *args: None)
        stream.config = types.SimpleNamespace(http2_show_debug=False, http2_status_to_close=[])
        stream.connection = types.SimpleNamespace(
            ssl_sock=object(), close=mock.Mock(), retry_task_cb=mock.Mock())
        stream._close_lock = threading.RLock()
        stream._closed = False
        stream.state = http2_stream.STATE_OPEN
        stream.stream_id = 1
        stream._close_cb = mock.Mock()

        stream.send_response()

        self.assertFalse(h2_task.responsed)
        self.assertTrue(h2_task.queue.empty())

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

    def test_h1_response_begin_uses_remaining_deadline(self):
        clock = Clock()
        response = FakeH1Response([b"abc", b""], clock=clock)
        response.begin = mock.Mock(wraps=response.begin)
        with mock.patch.object(http1.time, "time", side_effect=clock.time), \
                mock.patch.object(http1.time, "monotonic", side_effect=clock.monotonic):
            run_h1_response(response, timeout=0.1, send_cost=0.06, clock=clock)

        begin_timeout = response.begin.call_args.kwargs["timeout"]
        self.assertAlmostEqual(begin_timeout, 0.04)
        self.assertLessEqual(clock.now, 0.100001)

    def make_h2_response_stream(self, responses, method="GET"):
        task = self.make_task(method=method)
        stream = http2_stream.Stream.__new__(http2_stream.Stream)
        stream.task = task
        stream.attempt_id = task.attempt_id if hasattr(task, "attempt_id") else 0
        stream.state = http2_stream.STATE_OPEN
        stream._close_lock = threading.RLock()
        stream._closed = False
        stream.stream_id = 1
        stream.response_header_datas = []
        stream.response_headers = None
        stream.start_connection_point = 0
        stream.start_time = time.time()
        stream.get_head_stream_num = 0
        stream.logger = types.SimpleNamespace(
            debug=lambda *args: None, warn=lambda *args: None,
            error=lambda *args: None, exception=lambda *args: None)
        stream.config = types.SimpleNamespace(
            http2_show_debug=False, http2_status_to_close=[])
        stream.connection = types.SimpleNamespace(
            ssl_sock=types.SimpleNamespace(ip_str="test"),
            _sock=types.SimpleNamespace(bytes_received=0),
            streams={1: stream}, close=mock.Mock(), retry_task_cb=mock.Mock(),
            update_speed=mock.Mock(), continue_timeout=0, ip_str="test")
        stream._close_cb = mock.Mock()
        stream._decoder = types.SimpleNamespace(decode=lambda data: responses[data])
        stream.receive_window_manager = types.SimpleNamespace(_handle_frame=lambda size: 0)
        stream.request_body = b""
        return task, stream

    def receive_h2_headers(self, stream, marker, end_stream=False):
        frame = http2_stream.HeadersFrame(1)
        frame.data = marker
        frame.flags.add("END_HEADERS")
        if end_stream:
            frame.flags.add("END_STREAM")
        stream.receive_frame(frame)
        return frame

    def test_h2_closed_old_attempt_cannot_send_after_new_attempt(self):
        worker = self.make_h2_worker()
        task = self.make_task()
        task.attempt_id = 2
        old_stream = types.SimpleNamespace(task=task, _closed=True, attempt_id=1)
        worker.streams = {1: old_stream}
        frame = http2_connection.DataFrame(1)
        frame.data = b"old"

        self.assertFalse(worker._should_send_frame(frame))

    def test_h2_old_attempt_cannot_deliver_final_response(self):
        task, stream = self.make_h2_response_stream({b"final": [(b":status", b"200")]})
        task.attempt_id = 2
        stream.attempt_id = 1
        stream.response_headers = {b":status": [b"200"]}

        stream.send_response()

        self.assertFalse(task.responsed)
        self.assertTrue(task.queue.empty())

    def test_h2_closed_old_attempt_cannot_deliver_data(self):
        task, stream = self.make_h2_response_stream({})
        task.attempt_id = 2
        task.responsed = True
        stream.attempt_id = 1
        stream._closed = True
        stream.response_headers = {b":status": [b"200"]}
        stream.response_status = 200
        frame = http2_stream.DataFrame(1)
        frame.data = b"old-data"

        stream.receive_frame(frame)

        self.assertEqual(task.body_len, 0)

    def test_h1_closed_worker_rejects_new_request(self):
        worker = http1.Http1Worker.__new__(http1.Http1Worker)
        worker.keep_running = False
        worker.accept_task = False
        worker.task_queue = queue.Queue()
        worker._lock = threading.Lock()
        worker.task = None
        worker.task_queue.put(None)
        task = self.make_task()

        result = worker.request(task)

        self.assertFalse(result)
        self.assertIsNone(worker.task_queue.get_nowait())
        self.assertTrue(worker.task_queue.empty())
        self.assertIsNone(worker.task)

    def test_h1_old_attempt_retry_cannot_requeue_after_new_attempt(self):
        dispatcher = http_dispatcher.HttpsDispatcher.__new__(http_dispatcher.HttpsDispatcher)
        dispatcher.fail_num = 0
        dispatcher.continue_fail_num = 0
        dispatcher.last_fail_time = 0
        dispatcher.retry_log_throttle = types.SimpleNamespace(allow=lambda key: (False, 0))
        dispatcher.logger = types.SimpleNamespace(warn=lambda *args: None)
        dispatcher.running = True
        dispatcher.request_queue = queue.Queue()
        task = self.make_task()
        task.attempt_id = 1

        try:
            dispatcher.retry_task_cb(task, "first", expected_attempt=1)
        except TypeError as error:
            self.fail("retry 回调尚不接受 attempt 身份：%r" % error)
        task.retry_pending = False
        task.attempt_id = 2
        dispatcher.retry_task_cb(task, "late-old", expected_attempt=1)

        self.assertEqual(dispatcher.request_queue.qsize(), 1)
        self.assertEqual(task.retry_count, 1)

    def test_stale_retry_queue_entry_is_not_dispatched(self):
        dispatcher = http_dispatcher.HttpsDispatcher.__new__(http_dispatcher.HttpsDispatcher)
        dispatcher.fail_num = 0
        dispatcher.continue_fail_num = 0
        dispatcher.last_fail_time = 0
        dispatcher.retry_log_throttle = types.SimpleNamespace(allow=lambda key: (False, 0))
        dispatcher.logger = types.SimpleNamespace(
            debug=lambda *args: None, warn=lambda *args: None,
            exception=lambda *args: None)
        dispatcher.running = True
        dispatcher.request_queue = queue.Queue()
        dispatcher.wait_a_worker_cv = types.SimpleNamespace(notify=lambda: None)
        dispatcher.trigger_create_worker_cv = types.SimpleNamespace(notify=lambda: None)
        dispatcher.ping_speed_ip_str_last_active = {}
        worker = types.SimpleNamespace(
            ip_str="test", get_score=lambda: 0, last_request_time=0, request=mock.Mock())
        dispatcher.get_worker = mock.Mock(return_value=worker)
        task = self.make_task()
        task.attempt_id = 1
        dispatcher.retry_task_cb(task, "queued", expected_attempt=1)
        queued = dispatcher.request_queue.get_nowait()

        task.attempt_id = 2
        task.retry_pending = False
        dispatcher.request_queue.put(queued)
        dispatcher.request_queue.put(None)
        dispatcher.dispatcher()

        dispatcher.get_worker.assert_not_called()

    def test_h2_informational_headers_wait_for_final_response(self):
        task, stream = self.make_h2_response_stream({
            b"continue": [(b":status", b"100")],
            b"final": [(b":status", b"200"), (b"content-length", b"0")],
        })

        self.receive_h2_headers(stream, b"continue")
        self.assertTrue(task.queue.empty())
        self.receive_h2_headers(stream, b"final")

        response = task.queue.get_nowait()
        self.assertEqual(response.status, 200)

    def test_h2_trailers_finish_response_without_replacing_final_headers(self):
        task, stream = self.make_h2_response_stream({
            b"final": [(b":status", b"200")],
            b"trailers": [(b"x-checksum", b"ok")],
        })
        self.receive_h2_headers(stream, b"final")
        self.assertEqual(task.queue.get_nowait().status, 200)
        data = http2_stream.DataFrame(1)
        data.data = b"abc"
        stream.receive_frame(data)

        self.receive_h2_headers(stream, b"trailers", end_stream=True)

        self.assertEqual(task.body_queue.get_nowait(), b"abc")
        self.assertTrue(task.finished)
        self.assertTrue(task.response_complete)
        self.assertNotIn(b"x-checksum", stream.response_headers)

    def test_h2_data_before_final_headers_is_rejected(self):
        task, stream = self.make_h2_response_stream({})
        frame = http2_stream.DataFrame(1)
        frame.data = b"early"

        stream.receive_frame(frame)

        self.assertEqual(task.body_len, 0)
        self.assertTrue(stream._closed)

    def test_h2_rst_stream_does_not_close_shared_connection(self):
        task, stream = self.make_h2_response_stream({})
        stream.ip_str = "test"
        stream.connection.last_recv_time = time.time()
        frame = http2_stream.RstStreamFrame(1)
        frame.error_code = 8

        stream.receive_frame(frame)

        stream.connection.close.assert_not_called()
        self.assertTrue(stream._closed)
        stream.connection.retry_task_cb.assert_called_once()

    def make_dispatcher(self):
        dispatcher = http_dispatcher.HttpsDispatcher.__new__(http_dispatcher.HttpsDispatcher)
        dispatcher.logger = types.SimpleNamespace(
            debug=lambda *args: None, info=lambda *args: None,
            warn=lambda *args: None, exception=lambda *args: None)
        dispatcher.config = types.SimpleNamespace(
            show_state_debug=False, max_task_num=100,
            dispather_max_workers=4, dispather_min_idle_workers=1,
            dispather_min_workers=1, dispather_work_min_idle_time=0,
            dispather_work_max_score=10000, http2_target_concurrent=10,
            dispather_max_idle_workers=2,
            dispather_ping_check_speed_interval=10000)
        dispatcher.workers = []
        dispatcher.h1_num = 0
        dispatcher.h2_num = 0
        dispatcher.running = True
        dispatcher._state_lock = threading.RLock()
        dispatcher.request_queue = queue.Queue()
        dispatcher.wait_a_worker_cv = http_dispatcher.SimpleCondition()
        dispatcher.trigger_create_worker_cv = http_dispatcher.SimpleCondition()
        dispatcher.retry_log_throttle = types.SimpleNamespace(
            allow=lambda key: (False, 0), flush=lambda: (0, 0))
        dispatcher.task_count = 0
        dispatcher.task_count_lock = threading.Lock()
        dispatcher.fail_num = 0
        dispatcher.continue_fail_num = 0
        dispatcher.last_fail_time = 0
        dispatcher.last_request_time = 0
        dispatcher.ping_speed_ip_str_last_active = {}
        return dispatcher

    def test_dispatcher_worker_wait_uses_task_deadline(self):
        dispatcher = self.make_dispatcher()
        task = self.make_task(timeout=0.03)
        started = time.monotonic()
        try:
            worker = dispatcher.get_worker(deadline=task.deadline)
        except TypeError as error:
            self.fail("get_worker 尚不接受任务期限：%r" % error)

        self.assertIsNone(worker)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_dispatcher_late_worker_is_closed_after_stop(self):
        dispatcher = self.make_dispatcher()
        entered = threading.Event()
        release = threading.Event()
        created = []

        class LateWorker:
            def __init__(self, *args, **kwargs):
                self.ip_str = "late"
                self.close = mock.Mock()
                created.append(self)
                entered.set()
                release.wait(1)

        dispatcher.http1worker = LateWorker
        dispatcher.http2worker = LateWorker
        dispatcher.http2stream_class = object
        dispatcher.ip_manager = None
        dispatcher.log_debug_data = lambda *args: None
        dispatcher.ping_speed_ip_str_last_active = {"late": time.time()}
        sock = types.SimpleNamespace(h2=False, ip_str="late", close=mock.Mock())
        thread = threading.Thread(
            target=dispatcher.on_ssl_created_cb, args=(sock,),
            kwargs={"remove_slowest_worker": False})
        thread.start()
        self.assertTrue(entered.wait(1))
        dispatcher.stop()
        release.set()
        thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(dispatcher.workers, [])
        self.assertEqual(created[0].close.call_count, 1)

    def test_dispatcher_request_after_stop_does_not_enqueue(self):
        dispatcher = self.make_dispatcher()
        dispatcher.running = False

        result = dispatcher.request("GET", "example", b"/", {}, b"", timeout=0.01)

        self.assertIsNone(result)
        self.assertTrue(dispatcher.request_queue.empty())
        self.assertEqual(dispatcher.task_count, 0)

    def test_dispatcher_remove_slowest_worker_closes_socket_owner(self):
        dispatcher = self.make_dispatcher()
        workers = []
        for index, score in enumerate((100, 200, 300, 400)):
            worker = types.SimpleNamespace(
                accept_task=True, version="1.1", streams={}, ip_str=str(index),
                get_score=lambda score=score: score)
            worker.close = mock.Mock(side_effect=lambda reason, worker=worker: dispatcher.close_cb(worker))
            workers.append(worker)
        dispatcher.workers = list(workers)

        dispatcher._remove_slowest_worker()

        self.assertNotIn(workers[-1], dispatcher.workers)
        workers[-1].close.assert_called_once_with("remove slowest worker")

    def test_connect_manager_concurrent_creation_starts_one_supervisor(self):
        manager = connect_manager.ConnectManager.__new__(connect_manager.ConnectManager)
        manager.config = types.SimpleNamespace(
            show_state_debug=False, https_max_connect_thread=0,
            connect_create_interval=0)
        manager.logger = types.SimpleNamespace(
            name="test", debug=lambda *args: None,
            warning=lambda *args: None)
        manager.connecting_more_thread = None
        manager.thread_num = 0
        manager.start_connect_time = 0

        arrivals = queue.Queue()
        raw_lock = threading.Lock()

        class GateLock:
            def acquire(self):
                arrivals.put(threading.current_thread().name)
                return raw_lock.acquire()

            def release(self):
                raw_lock.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                self.release()

        manager.thread_num_lock = GateLock()
        raw_lock.acquire()
        callers = [
            threading.Thread(
                target=manager._create_more_connection,
                name="creator-%d" % index)
            for index in range(2)
        ]
        created = []
        release_supervisors = threading.Event()
        real_thread = threading.Thread

        def thread_factory(target, name):
            def gated_worker():
                release_supervisors.wait()
                target()

            thread = real_thread(target=gated_worker, name=name)
            created.append(thread)
            return thread

        self.addCleanup(release_supervisors.set)
        try:
            with mock.patch.object(
                    connect_manager.threading, "Thread", side_effect=thread_factory):
                for caller in callers:
                    caller.start()
                self.assertEqual(
                    {arrivals.get(timeout=1), arrivals.get(timeout=1)},
                    {"creator-0", "creator-1"})
                raw_lock.release()
                for caller in callers:
                    caller.join(1)
                self.assertFalse(any(caller.is_alive() for caller in callers))

                # 两个调用都已在锁外看到空闲；锁内必须复核唯一所有权。
                self.assertEqual(len(created), 1)
        finally:
            if raw_lock.locked():
                raw_lock.release()
            release_supervisors.set()
            for thread in created:
                thread.join(1)
            for caller in callers:
                caller.join(1)

        self.assertFalse(any(thread.is_alive() for thread in created))
        self.assertIsNone(manager.connecting_more_thread)

    def test_connect_manager_supervisor_can_restart_after_completion(self):
        manager = connect_manager.ConnectManager.__new__(connect_manager.ConnectManager)
        manager.config = types.SimpleNamespace(
            show_state_debug=False, https_max_connect_thread=0,
            connect_create_interval=0)
        manager.logger = types.SimpleNamespace(
            name="test", debug=lambda *args: None,
            warning=lambda *args: None)
        manager.thread_num_lock = threading.Lock()
        manager.connecting_more_thread = None
        manager.thread_num = 0
        manager.start_connect_time = 0

        supervisors = []
        for _ in range(2):
            manager._create_more_connection()
            supervisor = manager.connecting_more_thread
            self.assertIsNotNone(supervisor)
            supervisors.append(supervisor)
            supervisor.join(1)
            self.assertFalse(supervisor.is_alive())
            self.assertIsNone(manager.connecting_more_thread)

        self.assertIsNot(supervisors[0], supervisors[1])

    def make_h2_worker(self):
        worker = http2_connection.Http2Worker.__new__(http2_connection.Http2Worker)
        worker.logger = types.SimpleNamespace(
            debug=lambda *args: None, warn=lambda *args: None,
            exception=lambda *args: None)
        worker.config = types.SimpleNamespace(
            show_state_debug=False, http2_show_debug=False,
            http2_max_concurrent=100)
        worker.keep_running = True
        worker.accept_task = True
        worker.streams = {}
        worker.next_stream_id = 1
        worker.request_lock = threading.Lock()
        worker._state_lock = threading.RLock()
        worker._send_lock = threading.RLock()
        worker._header_lock = threading.RLock()
        worker._closing = False
        self.addCleanup(setattr, worker, "_closing", True)
        worker.send_queue = queue.Queue()
        worker.blocked_send_frames = []
        worker.retry_task_cb = mock.Mock()
        worker.processed_tasks = 0
        worker.remote_window_size = 65535
        worker.ip_str = "test"
        worker.ssl_sock = types.SimpleNamespace(
            create_time=time.time(), ip_str="test", sni="test", host="example",
            settimeout=mock.Mock())
        worker.get_trace = lambda: "trace"
        worker.idle_cb = mock.Mock()
        worker.is_life_end = lambda: False
        worker.decoder = None
        worker.encode_header = lambda headers: b""
        worker._send_cb = lambda frame: None
        worker._lock = threading.Lock()
        worker.local_settings = {
            http2_connection.SettingsFrame.INITIAL_WINDOW_SIZE: 65535,
            http2_connection.SettingsFrame.SETTINGS_MAX_FRAME_SIZE: 16384,
        }
        worker.remote_settings = {
            http2_connection.SettingsFrame.INITIAL_WINDOW_SIZE: 65535,
            http2_connection.SettingsFrame.SETTINGS_MAX_FRAME_SIZE: 16384,
        }
        return worker

    def make_task(self, timeout=1, method="GET"):
        config = types.SimpleNamespace(show_state_debug=False)
        return http_common.Task(
            types.SimpleNamespace(debug=lambda *args: None, warn=lambda *args: None,
                                  error=lambda *args: None),
            config, method, "example", b"/", {}, b"", queue.Queue(), b"url", timeout)

    def test_h2_request_close_interleave_never_registers_after_close(self):
        worker = self.make_h2_worker()
        task = self.make_task()

        class ClosingStream:
            def __init__(self, logger, config, connection, *args):
                connection.close("close during request")

            def start_request(self):
                raise AssertionError("关闭后的 stream 不得开始发送")

        worker.stream_class = ClosingStream
        with mock.patch.object(http2_connection.HttpWorker, "close"):
            worker.request(task)

        self.assertEqual(worker.streams, {})
        worker.retry_task_cb.assert_called_once_with(task)

    def test_h2_concurrent_close_only_stops_send_loop_once(self):
        worker = self.make_h2_worker()
        barrier = threading.Barrier(3)
        with mock.patch.object(http2_connection.HttpWorker, "close"):
            threads = [threading.Thread(target=lambda: (barrier.wait(), worker.close("race")))
                       for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(1)

        sentinels = []
        while not worker.send_queue.empty():
            sentinels.append(worker.send_queue.get_nowait())
        self.assertEqual(sentinels, [None])

    def test_retry_callback_claims_each_attempt_only_once(self):
        dispatcher = http_dispatcher.HttpsDispatcher.__new__(http_dispatcher.HttpsDispatcher)
        dispatcher.fail_num = 0
        dispatcher.continue_fail_num = 0
        dispatcher.last_fail_time = 0
        dispatcher.retry_log_throttle = types.SimpleNamespace(allow=lambda key: (False, 0))
        dispatcher.logger = types.SimpleNamespace(warn=lambda *args: None)
        dispatcher.running = True
        dispatcher.request_queue = queue.Queue()
        task = self.make_task()

        dispatcher.retry_task_cb(task, "first")
        dispatcher.retry_task_cb(task, "duplicate")

        self.assertEqual(dispatcher.request_queue.qsize(), 1)
        self.assertEqual(task.retry_count, 1)

    def test_h2_retry_claim_handoff_covers_refusal_and_immediate_failure(self):
        for running in (False, True):
            with self.subTest(running=running):
                worker = self.make_h2_worker()
                worker.keep_running = running
                task = self.make_task()
                task.retry_pending = True
                reclaimed = []

                class ImmediateStream:
                    def __init__(self, logger, config, connection, ip_str, stream_id, task, *args):
                        self.connection = connection
                        self.task = task

                    def start_request(self):
                        reclaimed.append(self.task.claim_retry())

                worker.stream_class = ImmediateStream
                worker.retry_task_cb = lambda task: reclaimed.append(task.claim_retry())
                worker.request(task)

                self.assertEqual(reclaimed, [True])

    def test_h2_close_after_registration_can_retry_previous_attempt(self):
        worker = self.make_h2_worker()
        task = self.make_task()
        task.retry_pending = True
        reclaimed = []

        class RegisteredStream:
            def __init__(self, logger, config, connection, ip_str, stream_id, task, *args):
                self.task = task

            def start_request(self):
                pass

            def close(self, reason):
                reclaimed.append(self.task.claim_retry())

        class CloseOnRegister(dict):
            def __setitem__(self, stream_id, stream):
                super().__setitem__(stream_id, stream)
                worker.close("close after register")

        worker.stream_class = RegisteredStream
        worker.streams = CloseOnRegister()
        with mock.patch.object(http2_connection.HttpWorker, "close"):
            worker.request(task)

        self.assertEqual(reclaimed, [True])

    def test_stream_close_is_idempotent(self):
        task = self.make_task()
        task.responsed = True
        stream = http2_stream.Stream.__new__(http2_stream.Stream)
        stream.task = task
        stream.state = http2_stream.STATE_OPEN
        stream._close_lock = threading.RLock()
        stream._closed = False
        stream.stream_id = 1
        stream.connection = types.SimpleNamespace(retry_task_cb=mock.Mock())
        stream._close_cb = mock.Mock()
        stream._close_lock = threading.RLock()
        stream._closed = False

        stream.close("recv fail")
        stream.close("recv fail")

        stream._close_cb.assert_called_once_with(1, "recv fail")
        self.assertTrue(task.finished)

    def test_missing_stream_close_does_not_increment_processed_tasks(self):
        worker = self.make_h2_worker()
        worker.keep_running = False

        worker._close_stream_cb(99, "duplicate")

        self.assertEqual(worker.processed_tasks, 0)

    def test_connection_checker_applies_per_stream_timeouts(self):
        dispatcher = http_dispatcher.HttpsDispatcher.__new__(http_dispatcher.HttpsDispatcher)
        dispatcher.running = True
        worker = self.make_h2_worker()
        worker.version = "2"
        worker.is_life_end = lambda: False
        worker.last_request_time = time.time()
        expired = types.SimpleNamespace(check_timeout=mock.Mock())
        active = types.SimpleNamespace(check_timeout=mock.Mock())
        worker.streams = {1: expired, 3: active}
        dispatcher.workers = [worker]
        dispatcher.config = types.SimpleNamespace(
            dispather_worker_idle_time=1000, dispather_min_workers=1,
            dispather_ping_check_speed_interval=1000)
        dispatcher.ping_speed_ip_str_last_active = {worker.ip_str: time.time()}
        dispatcher.last_get_score_time = 0
        dispatcher.logger = types.SimpleNamespace(exception=lambda *args: None)

        with mock.patch.object(http_dispatcher.time, "sleep",
                               side_effect=lambda seconds: setattr(dispatcher, "running", False)):
            dispatcher.connection_checker()

        expired.check_timeout.assert_called_once()
        active.check_timeout.assert_called_once()
        worker.idle_cb.assert_not_called()

    def test_stream_timeout_finishes_and_unregisters_only_itself(self):
        worker = self.make_h2_worker()
        task = self.make_task(timeout=0.01)
        task.deadline = time.monotonic() - 1
        task.start_time = time.time() - 1
        stream = http2_stream.Stream.__new__(http2_stream.Stream)
        stream.task = task
        stream.state = http2_stream.STATE_OPEN
        stream._close_lock = threading.RLock()
        stream._closed = False
        stream.stream_id = 1
        stream.start_time = task.start_time
        stream.logger = worker.logger
        stream.connection = worker
        stream._close_cb = worker._close_stream_cb
        stream._close_lock = threading.RLock()
        stream._closed = False
        worker.streams = {1: stream}
        worker.continue_timeout = 0

        stream.check_timeout(time.time())

        self.assertTrue(task.finished)
        self.assertNotIn(1, worker.streams)
        self.assertTrue(worker.keep_running)

    def test_send_loop_drops_expired_stream_frames_without_touching_shared_socket(self):
        worker = self.make_h2_worker()
        task = self.make_task()
        task.deadline = time.monotonic() - 1
        stream = types.SimpleNamespace(task=task)
        worker.streams = {1: stream}
        worker._sock = mock.Mock()
        frame = http2_connection.DataFrame(1)
        frame.data = b"late"
        worker.send_queue.put(frame)
        worker.send_queue.put(None)

        worker.h2_send_loop()

        worker._sock.send.assert_not_called()
        worker._sock.flush.assert_not_called()
        worker.ssl_sock.settimeout.assert_not_called()

    def test_expired_h2_frame_restores_connection_window(self):
        worker = self.make_h2_worker()
        worker.blocked_send_frames = []
        worker._send_cb = lambda frame: http2_connection.Http2Worker._send_cb(worker, frame)
        task = self.make_task()
        task.deadline = time.monotonic() - 1
        worker.streams = {1: types.SimpleNamespace(task=task)}
        worker._sock = mock.Mock()
        frame = http2_connection.DataFrame(1)
        frame.data = b"late"

        worker._send_cb(frame)
        self.assertEqual(worker.remote_window_size, 65531)
        worker.send_queue.put(None)
        worker.h2_send_loop()

        self.assertEqual(worker.remote_window_size, 65535)
        worker._sock.send.assert_not_called()

    def test_h2_closed_stream_drops_blocked_data(self):
        worker = self.make_h2_worker()
        worker.remote_window_size = 0
        worker.blocked_send_frames = []
        task = self.make_task()
        worker.streams = {1: types.SimpleNamespace(task=task)}
        frame = http2_connection.DataFrame(1)
        frame.data = b"blocked"

        http2_connection.Http2Worker._send_cb(worker, frame)
        self.assertEqual(worker.blocked_send_frames, [frame])
        worker._close_stream_cb(1, "timeout")

        self.assertEqual(worker.blocked_send_frames, [])
        self.assertEqual(worker.remote_window_size, 0)
        self.assertEqual(worker.send_queue.qsize(), 0)

    def make_h2_sending_stream(self, body=b"abcdef", window=6, max_frame_size=2):
        stream = http2_stream.Stream.__new__(http2_stream.Stream)
        stream.stream_id = 1
        stream.request_body = body
        stream.request_body_left = len(body)
        stream.request_body_sended = False
        stream.remote_window_size = window
        stream.max_frame_size = max_frame_size
        stream._send_lock = threading.RLock()
        stream.task = self.make_task()
        stream._closed = False
        stream.attempt_id = stream.task.attempt_id
        stream._close_local = mock.Mock()
        return stream

    def test_h2_headers_do_not_consume_connection_data_window(self):
        worker = self.make_h2_worker()
        worker.blocked_send_frames = []
        frame = http2_connection.HeadersFrame(1)
        frame.data = b"abcdef"

        http2_connection.Http2Worker._send_cb(worker, frame)

        self.assertEqual(worker.remote_window_size, 65535)
        self.assertIs(worker.send_queue.get_nowait(), frame)
        self.assertEqual(worker.blocked_send_frames, [])

    def test_h2_connection_window_charge_is_atomic(self):
        worker = self.make_h2_worker()
        worker.remote_window_size = 4
        worker.blocked_send_frames = []
        first_entered = threading.Event()
        release_first = threading.Event()

        class GatedBytes(bytes):
            def __init__(self, value):
                self.length_calls = 0

            def __len__(self):
                self.length_calls += 1
                if self.length_calls == 2:
                    first_entered.set()
                    release_first.wait(1)
                return bytes.__len__(self)

        first = http2_connection.DataFrame(1)
        first.data = GatedBytes(b"aaaa")
        second = http2_connection.DataFrame(3)
        second.data = b"bbbb"
        threads = [
            threading.Thread(target=http2_connection.Http2Worker._send_cb, args=(worker, first)),
            threading.Thread(target=http2_connection.Http2Worker._send_cb, args=(worker, second)),
        ]
        threads[0].start()
        self.assertTrue(first_entered.wait(1))
        threads[1].start()
        time.sleep(0.02)
        release_first.set()
        for thread in threads:
            thread.join(1)
            self.assertFalse(thread.is_alive())

        self.assertEqual(worker.remote_window_size, 0)
        self.assertEqual(worker.send_queue.qsize(), 1)
        self.assertEqual(len(worker.blocked_send_frames), 1)

    def test_h2_window_update_cannot_reorder_request_body(self):
        stream = self.make_h2_sending_stream()
        first_entered = threading.Event()
        release_first = threading.Event()
        sent = []

        class GatedBody(bytes):
            def __init__(self, value):
                self.length_calls = 0

            def __len__(self):
                self.length_calls += 1
                if self.length_calls == 1:
                    first_entered.set()
                    release_first.wait(1)
                return bytes.__len__(self)

        stream.request_body = GatedBody(stream.request_body)
        stream._send_cb = lambda frame: sent.append(bytes(frame.data))
        initial = threading.Thread(target=stream.send_left_body)
        initial.start()
        self.assertTrue(first_entered.wait(1))
        update = http2_stream.WindowUpdateFrame(stream.task.attempt_id or 1)
        update.window_increment = 2
        updater = threading.Thread(target=stream.receive_frame, args=(update,))
        updater.start()
        time.sleep(0.02)
        release_first.set()
        initial.join(1)
        updater.join(1)

        self.assertFalse(initial.is_alive())
        self.assertFalse(updater.is_alive())
        self.assertEqual(sent, [b"ab", b"cd", b"ef"])
        self.assertEqual(stream.request_body_left, 0)

    def test_h2_hpack_encoding_matches_headers_queue_order(self):
        worker = self.make_h2_worker()
        worker.encoder = http2_connection.Encoder()
        worker.encode_header = worker.encoder.encode
        worker.blocked_send_frames = []
        worker._sock = types.SimpleNamespace(bytes_received=0)
        first_entered = threading.Event()
        release_first = threading.Event()

        def send_frame(stream, frame):
            if stream.stream_id == 1:
                first_entered.set()
                release_first.wait(1)
            http2_connection.Http2Worker._send_cb(worker, frame)

        streams = []
        for stream_id in (1, 3):
            task = self.make_task()
            task.headers = {"x-reused": "same-value"}
            stream = http2_stream.Stream(
                worker.logger, worker.config, worker, "test", stream_id, task,
                lambda frame, stream_id=stream_id: None,
                mock.Mock(), worker.encode_header, worker.decoder, None, 65535, 16384)
            stream._send_cb = lambda frame, stream=stream: send_frame(stream, frame)
            streams.append(stream)

        threads = [threading.Thread(target=stream.start_request) for stream in streams]
        threads[0].start()
        self.assertTrue(first_entered.wait(1))
        threads[1].start()
        time.sleep(0.02)
        release_first.set()
        for thread in threads:
            thread.join(1)
            self.assertFalse(thread.is_alive())

        queued = []
        while not worker.send_queue.empty():
            queued.append(worker.send_queue.get_nowait().stream_id)
        self.assertEqual(queued, [1, 3])

    def test_h2_header_table_settings_wait_for_in_progress_encoding(self):
        worker = self.make_h2_worker()
        worker.encoder = http2_connection.Encoder()
        worker.encode_header = worker.encoder.encode
        worker._sock = types.SimpleNamespace(bytes_received=0)
        worker._send_cb = lambda frame: http2_connection.Http2Worker._send_cb(worker, frame)
        task = self.make_task()
        task.headers = {"x-settings-race": "value"}
        stream = http2_stream.Stream(
            worker.logger, worker.config, worker, "test", 1, task,
            worker._send_cb, mock.Mock(), worker.encode_header, worker.decoder,
            None, 65535, 16384)
        add_entered = threading.Event()
        release_add = threading.Event()
        settings_done = threading.Event()
        observed_sizes = []
        original_add = worker.encoder.add

        def gated_add(*args):
            add_entered.set()
            release_add.wait(1)
            observed_sizes.append(worker.encoder.header_table_size)
            return original_add(*args)

        frame = http2_connection.SettingsFrame(0)
        frame.settings[http2_connection.SettingsFrame.HEADER_TABLE_SIZE] = 0
        with mock.patch.object(worker.encoder, "add", side_effect=gated_add):
            encoder_thread = threading.Thread(target=stream.start_request)
            encoder_thread.start()
            self.assertTrue(add_entered.wait(1))

            def update_settings():
                worker._update_settings(frame)
                settings_done.set()

            settings_thread = threading.Thread(target=update_settings)
            settings_thread.start()
            finished_during_encode = settings_done.wait(0.1)
            release_add.set()
            encoder_thread.join(1)
            settings_thread.join(1)

        self.assertFalse(encoder_thread.is_alive())
        self.assertFalse(settings_thread.is_alive())
        self.assertFalse(finished_during_encode)
        self.assertEqual(observed_sizes, [4096] * 5)
        self.assertEqual(worker.encoder.header_table_size, 0)

    def test_h2_initial_connection_send_window_uses_protocol_default(self):
        fake_thread = mock.Mock()
        fake_thread.start.return_value = None
        def init_base(worker, logger, ip_manager, config, ssl_sock, *args):
            worker.ssl_sock = ssl_sock
            worker.ip_str = "test"

        with mock.patch.object(http2_connection.HttpWorker, "__init__", init_base), \
                mock.patch.object(http2_connection.BufferedSocket, "__init__", return_value=None), \
                mock.patch.object(http2_connection.Http2Worker, "_send_preamble"), \
                mock.patch.object(http2_connection.threading, "Thread", return_value=fake_thread):
            worker = http2_connection.Http2Worker(
                types.SimpleNamespace(), None, types.SimpleNamespace(), mock.Mock(),
                mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock())
        worker.close = mock.Mock()

        self.assertEqual(worker.remote_window_size, 65535)

    def test_h2_settings_replace_max_frame_and_apply_header_table_size(self):
        worker = self.make_h2_worker()
        worker.encoder = http2_connection.Encoder()
        stream = types.SimpleNamespace(
            max_frame_size=16384, remote_window_size=65535,
            _send_lock=threading.RLock())
        worker.streams = {1: stream}
        frame = http2_connection.SettingsFrame(0)
        frame.settings[http2_connection.SettingsFrame.SETTINGS_MAX_FRAME_SIZE] = 16384
        frame.settings[http2_connection.SettingsFrame.HEADER_TABLE_SIZE] = 0

        worker._update_settings(frame)

        self.assertEqual(stream.max_frame_size, 16384)
        self.assertEqual(worker.encoder.header_table_size, 0)

    def test_h2_settings_and_new_stream_share_one_state_transaction(self):
        worker = self.make_h2_worker()
        exited = threading.Event()
        release = threading.Event()

        class GatedSendLock:
            def __init__(self):
                self.lock = threading.Lock()

            def __enter__(self):
                self.lock.acquire()

            def __exit__(self, *args):
                self.lock.release()
                exited.set()
                release.wait(1)

        old_stream = types.SimpleNamespace(
            remote_window_size=65535, max_frame_size=16384,
            _send_lock=GatedSendLock())
        worker.streams = {1: old_stream}
        created = []

        class NewStream:
            def __init__(self, *args):
                self.remote_window_size = args[-2]
                self.max_frame_size = args[-1]
                self._send_lock = threading.RLock()
                created.append(self)

            def start_request(self):
                pass

        worker.stream_class = NewStream
        frame = http2_connection.SettingsFrame(0)
        frame.settings[http2_connection.SettingsFrame.INITIAL_WINDOW_SIZE] = 0
        frame.settings[http2_connection.SettingsFrame.SETTINGS_MAX_FRAME_SIZE] = 32768
        updater = threading.Thread(target=worker._update_settings, args=(frame,))
        requester = threading.Thread(target=worker.request_task, args=(self.make_task(),))
        updater.start()
        self.assertTrue(exited.wait(1))
        requester.start()
        threading.Event().wait(0.05)
        release.set()
        updater.join(1)
        requester.join(1)

        self.assertFalse(updater.is_alive())
        self.assertFalse(requester.is_alive())
        self.assertEqual(len(created), 1)
        self.assertEqual(old_stream.remote_window_size, 0)
        self.assertEqual(created[0].remote_window_size, 0)
        self.assertEqual(created[0].max_frame_size, 32768)

    def test_h2_negative_stream_window_does_not_send_or_corrupt_body_cursor(self):
        stream = self.make_h2_sending_stream(body=b"0123456789", window=-5, max_frame_size=5)
        stream._send_cb = mock.Mock()

        stream.send_left_body()

        stream._send_cb.assert_not_called()
        self.assertEqual(stream.request_body_left, 10)
        self.assertEqual(stream.remote_window_size, -5)

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
