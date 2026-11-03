import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import unittest


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
