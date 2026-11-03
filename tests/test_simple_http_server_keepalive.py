import importlib.util
import io
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "Coding" / "XX-Net-5.16.7.1" / "code" / "default" / "lib" / "noarch"
sys.path.insert(0, str(LIB))
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


class KeepAliveStateTest(unittest.TestCase):
    def make_handler(self, request):
        handler = RecordingHandler.__new__(RecordingHandler)
        handler.rfile = io.BytesIO(request)
        handler.wfile = io.BytesIO()
        handler.logger = Collector()
        handler.close_connection = 1
        handler.setup()
        return handler

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


if __name__ == "__main__":
    unittest.main()
