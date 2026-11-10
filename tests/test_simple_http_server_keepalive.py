import contextlib
import importlib.util
import io
from pathlib import Path
import socket
import sys
import threading
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "Coding" / "XX-Net-5.16.7.1" / "code" / "default" / "lib" / "noarch"
APP = LIB.parents[1]
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(APP))
SPEC = importlib.util.spec_from_file_location("simple_http_server_keepalive", LIB / "simple_http_server.py")
server_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server_module)


class Collector:
    def warn(self, *args):
        pass

    def exception(self, *args):
        pass


class RecordingHandler(server_module.HttpServerHandler):
    def setup(self):
        self.get_count = 0

    def do_GET(self):
        self.get_count += 1
        self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")


class ResponseHandler(server_module.HttpServerHandler):
    def setup(self):
        self.res_headers = {}

    def do_GET(self):
        self.send_response(content=b"ok")


class KeepAliveStateTest(unittest.TestCase):
    def make_handler(self, request):
        handler = RecordingHandler.__new__(RecordingHandler)
        handler.rfile = io.BytesIO(request)
        handler.wfile = io.BytesIO()
        handler.logger = Collector()
        handler.close_connection = 1
        handler.setup()
        return handler

    def make_response_handler(self, request):
        handler = ResponseHandler.__new__(ResponseHandler)
        handler.rfile = io.BytesIO(request)
        handler.wfile = io.BytesIO()
        handler.logger = Collector()
        handler.close_connection = 1
        handler.setup()
        return handler

    def test_response_advertises_http11_close(self):
        handler = self.make_response_handler(b"GET / HTTP/1.1\r\nConnection: close\r\n\r\n")

        handler.handle_one_request()

        self.assertIn(b"Connection: close\r\n", handler.wfile.getvalue())

    def test_response_advertises_http10_keep_alive(self):
        handler = self.make_response_handler(b"GET / HTTP/1.0\r\nConnection: keep-alive\r\n\r\n")

        handler.handle_one_request()

        self.assertIn(b"Connection: keep-alive\r\n", handler.wfile.getvalue())

    def test_custom_close_header_updates_connection_state_once(self):
        handler = self.make_response_handler(b"GET / HTTP/1.1\r\n\r\n")
        handler.res_headers["Connection"] = "close"

        handler.handle_one_request()

        self.assertEqual(handler.close_connection, 1)
        self.assertEqual(handler.wfile.getvalue().lower().count(b"connection:"), 1)

    def test_http10_defaults_to_close(self):
        handler = self.make_handler(b"GET /first HTTP/1.0\r\n\r\n")

        handler.handle_one_request()

        self.assertEqual(handler.close_connection, 1)
        self.assertEqual(handler.get_count, 1)

    def test_http10_keep_alive_stays_open(self):
        handler = self.make_handler(b"GET /first HTTP/1.0\r\nConnection: keep-alive\r\n\r\n")

        handler.handle_one_request()

        self.assertEqual(handler.close_connection, 0)

    def test_http11_defaults_to_keep_alive(self):
        handler = self.make_handler(b"GET /first HTTP/1.1\r\nHost: local\r\n\r\n")

        handler.handle_one_request()

        self.assertEqual(handler.close_connection, 0)

    def test_http11_explicit_close_wins(self):
        handler = self.make_handler(b"GET /first HTTP/1.1\r\nConnection: close\r\n\r\n")

        handler.handle_one_request()

        self.assertEqual(handler.close_connection, 1)

    def test_second_request_is_readable_then_honors_close(self):
        handler = self.make_handler(
            b"GET /first HTTP/1.1\r\nConnection: keep-alive\r\n\r\n"
            b"GET /second HTTP/1.1\r\nConnection: close\r\n\r\n"
        )

        handler.handle_one_request()
        self.assertEqual(handler.close_connection, 0)
        handler.handle_one_request()

        self.assertEqual(handler.get_count, 2)
        self.assertTrue(handler.wfile.getvalue().endswith(b"\r\n\r\n"))
        self.assertEqual(handler.close_connection, 1)

    def test_handler_explicit_close_is_preserved(self):
        handler = self.make_handler(b"GET /tunnel HTTP/1.1\r\n\r\n")
        handler.do_GET = lambda: setattr(handler, "close_connection", 1)

        handler.handle_one_request()

        self.assertEqual(handler.close_connection, 1)

    def test_handler_return_does_not_close_transferred_connection(self):
        handler = RecordingHandler.__new__(RecordingHandler)
        handler.connection = None
        handler.logger = Collector()
        handler.handle_one_request = lambda: None

        try:
            handler.handle()
        except AttributeError as error:
            self.fail("已交接 connection=None 不应在 handler 返回时再次关闭：%r" % error)


class ServerSocketLifecycleTest(unittest.TestCase):
    class FakeSocket:
        def __init__(self, fail_bind=False):
            self.fail_bind = fail_bind
            self.closed = False

        def setsockopt(self, *args):
            pass

        def bind(self, address):
            if self.fail_bind:
                raise OSError("controlled bind failure")

        def listen(self, backlog):
            pass

        def close(self):
            self.closed = True

    def make_server(self):
        server = server_module.HTTPServer.__new__(server_module.HTTPServer)
        server.sockets = []
        server.server_address = [("127.0.0.1", 8080)]
        server.handler = None
        server.args = ()
        server.use_https = False
        server.cert = ""
        server.max_thread = 100
        server.check_listen_interval = None
        server.running = True
        server._accepted_sockets = set()
        server._handler_threads = set()
        server._accepted_lock = threading.Lock()
        server.logger = types.SimpleNamespace(
            info=lambda *args: None, warn=lambda *args: None,
            error=lambda *args: None, exception=lambda *args: None)
        return server

    def test_partial_bind_failure_closes_all_listeners(self):
        server = self.make_server()
        server.server_address = [("127.0.0.1", 8080), ("127.0.0.1", 8081)]
        listeners = [self.FakeSocket(), self.FakeSocket(fail_bind=True)]

        with mock.patch.object(server_module.socket, "socket", side_effect=listeners):
            with self.assertRaises(Exception):
                server.init_socket()

        self.assertTrue(all(listener.closed for listener in listeners))
        self.assertEqual(server.sockets, [])

    def test_old_serve_returns_without_closing_restarted_listener(self):
        server = self.make_server()
        old_listener = self.FakeSocket()
        new_listener = self.FakeSocket()
        server.sockets = [old_listener]
        server.check_listen_interval = -1
        server.check_listen_port = lambda ip, port: None

        def restart():
            server.running = True
            server.sockets = [new_listener]

        server.start = restart
        select_calls = []

        def controlled_select(readers, writers, errors, timeout):
            select_calls.append(list(readers))
            if len(select_calls) > 1:
                server.running = False
            return [], [], []

        with mock.patch.object(server_module.select, "select", side_effect=controlled_select), \
                mock.patch.object(server_module.time, "sleep"):
            server.serve_forever()

        self.assertEqual(len(select_calls), 1)
        self.assertTrue(old_listener.closed)
        self.assertFalse(new_listener.closed)
        self.assertEqual(server.sockets, [new_listener])

    def test_shutdown_closes_accepted_socket_before_handler_is_ready(self):
        server = self.make_server()
        accepted = self.FakeSocket()
        entered = threading.Event()
        release = threading.Event()

        class SlowHandler:
            def __init__(self, sock, address, args):
                entered.set()
                release.wait(1)

            def handle(self):
                pass

        server.handler = SlowHandler
        process_thread = threading.Thread(target=server.process_connect, args=(accepted, ("127.0.0.1", 12345)))
        process_thread.start()
        self.assertTrue(entered.wait(1))
        try:
            server.shutdown()
            self.assertTrue(accepted.closed)
        finally:
            release.set()
            process_thread.join(1)
        self.assertFalse(process_thread.is_alive())
    def test_shutdown_waits_for_registered_handler_to_finish(self):
        server = self.make_server()
        accepted = self.FakeSocket()
        entered = threading.Event()
        release = threading.Event()
        writes = []

        class GatedHandler:
            def __init__(self, sock, address, args):
                pass

            def handle(self):
                entered.set()
                release.wait(2)
                writes.append("handler-finished")

        server.handler = GatedHandler
        server.process_connect(accepted, ("127.0.0.1", 12345))
        self.assertTrue(entered.wait(1))
        stopper = threading.Thread(target=server.shutdown)
        stopper.start()
        try:
            threading.Event().wait(0.05)
            self.assertTrue(
                stopper.is_alive(),
                "shutdown 必须等待已登记 handler，最终持久化不能与其并发")
        finally:
            release.set()
            stopper.join(2)

        self.assertFalse(stopper.is_alive())
        self.assertEqual(writes, ["handler-finished"])
        self.assertEqual(server._accepted_sockets, set())

    def test_shutdown_wakes_real_blocking_handler_socket(self):
        server = self.make_server()
        accepted, peer = socket.socketpair()
        entered = threading.Event()

        class BlockingHandler:
            def __init__(self, sock, address, args):
                pass

            def handle(self):
                entered.set()
                try:
                    accepted.recv(1)
                except OSError:
                    pass

        server.handler = BlockingHandler
        server.process_connect(accepted, ("127.0.0.1", 12345))
        self.assertTrue(entered.wait(1))
        stopper = threading.Thread(target=server.shutdown)
        stopper.start()
        stopper.join(0.25)
        shutdown_returned = not stopper.is_alive()
        try:
            self.assertTrue(
                shutdown_returned,
                "跨线程 close 未唤醒真实阻塞 recv，shutdown 在无期限 join 中卡住")
        finally:
            # 对端关闭可确定解除 recv；无论断言成败都不遗留守护外线程。
            peer.close()
            stopper.join(2)
            self.assertFalse(stopper.is_alive())
        self.assertEqual(server._accepted_sockets, set())
        self.assertEqual(server._handler_threads, set())


class LightweightControlHandlerTest(unittest.TestCase):
    def test_real_controller_responds_without_full_http_request_state(self):
        with contextlib.redirect_stderr(io.StringIO()):
            from smart_router.local.web_control import ControlHandler

        output = io.BytesIO()
        handler = ControlHandler(
            ("127.0.0.1", 12345), {}, b"GET", b"/log", io.BytesIO(), output)
        handler.set_CORS({})

        handler.send_response("text/plain", "{}")

        self.assertEqual(
            output.getvalue(),
            b"HTTP/1.1 200\r\nContent-Type: text/plain\r\n"
            b"Content-Length: 2\r\n\r\n{}",
        )


if __name__ == "__main__":
    unittest.main()
