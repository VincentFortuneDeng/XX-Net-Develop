"""Smart Router 回退时的字节与 socket 所有权回归测试。"""
import importlib.util
from pathlib import Path
import socket
import sys
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


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


namespace("smart_router", APP / "smart_router")
namespace("smart_router.local", APP / "smart_router" / "local")
g = namespace(
    "smart_router.local.global_var",
    x_tunnel=object(),
    x_tunnel_socks_port=1080,
)
socket_wrap = load_module(
    "smart_router.local.socket_wrap", APP / "smart_router" / "local" / "socket_wrap.py")
smart_route = load_module(
    "smart_router.local.smart_route", APP / "smart_router" / "local" / "smart_route.py")
import simple_http_server


class PartialSendSocket:
    def __init__(self, fail=False):
        self.sent = b""
        self.fail = fail
        self.closed = False

    def send(self, data):
        if self.fail:
            raise OSError("受控发送失败")
        accepted = data[:2]
        self.sent += accepted
        return len(accepted)

    def sendall(self, data):
        view = memoryview(data)
        while view:
            sent = self.send(view)
            if not sent:
                raise OSError("受控零字节写入")
            view = view[sent:]

    def close(self):
        self.closed = True


class RecordingPipe:
    def __init__(self):
        self.calls = []

    def add_socks(self, local, remote):
        self.calls.append((local, remote, getattr(remote, "_sock", remote).sent))
        return True


class SilentLogger:
    def debug(self, *args):
        pass

    def info(self, *args):
        pass

    def warn(self, *args):
        pass

    def warning(self, *args):
        pass

    def exception(self, *args):
        pass


def make_request():
    return types.SimpleNamespace(
        connection=object(),
        rfile=types.SimpleNamespace(_close=True),
        wfile=types.SimpleNamespace(_close=True),
    )


class SmartRouterHandoffTest(unittest.TestCase):
    def setUp(self):
        self.pipe = RecordingPipe()
        g.pipe_socks = self.pipe
        smart_route.xlog = SilentLogger()

    def test_redirect_sendall_forwards_entire_prefix(self):
        remote = PartialSendSocket()
        remote._sock = remote
        remote.ip = "192.0.2.80"
        tls_remote = PartialSendSocket()
        g.connect_manager = types.SimpleNamespace(get_conn=lambda *args: remote)
        local = smart_route.SocketWrap(socket.socket(), "192.0.2.10", 12345)
        self.addCleanup(local.close)

        with mock.patch.object(smart_route.ssl_context, "wrap_socket", return_value=tls_remote):
            smart_route.do_redirect_https(
                local, b"example.com", [b"192.0.2.80"], 80,
                ("192.0.2.10", 12345), b"abcdef")

        self.assertEqual(tls_remote.sent, b"abcdef")
        self.assertEqual(len(self.pipe.calls), 1)

    def test_redirect_tls_wrap_failure_closes_remote(self):
        remote = PartialSendSocket()
        remote._sock = remote
        g.connect_manager = types.SimpleNamespace(get_conn=lambda *args: remote)
        local = socket.socket()
        self.addCleanup(local.close)

        with mock.patch.object(smart_route.ssl_context, "wrap_socket", side_effect=OSError("wrap failed")):
            with self.assertRaises(smart_route.RedirectHttpsFail):
                smart_route.do_redirect_https(
                    local, b"example.com", [b"192.0.2.80"], 80,
                    ("192.0.2.10", 12345), b"abcdef")

        self.assertTrue(remote.closed)
        self.assertEqual(self.pipe.calls, [])

    def test_redirect_send_failure_closes_tls_remote(self):
        remote = PartialSendSocket()
        remote._sock = remote
        remote.ip = "192.0.2.80"
        tls_remote = PartialSendSocket(fail=True)
        g.connect_manager = types.SimpleNamespace(get_conn=lambda *args: remote)
        local = socket.socket()
        self.addCleanup(local.close)

        with mock.patch.object(smart_route.ssl_context, "wrap_socket", return_value=tls_remote):
            with self.assertRaises(OSError):
                smart_route.do_redirect_https(
                    local, b"example.com", [b"192.0.2.80"], 80,
                    ("192.0.2.10", 12345), b"abcdef")

        self.assertTrue(tls_remote.closed)
        self.assertEqual(self.pipe.calls, [])

    def test_direct_sendall_forwards_entire_prefix(self):
        remote = PartialSendSocket()
        remote.ip = "192.0.2.80"
        g.connect_manager = types.SimpleNamespace(get_conn=lambda *args: remote)
        local = socket.socket()

        try:
            smart_route.do_direct(
                local, b"example.com", [b"192.0.2.80"], 80, ("192.0.2.10", 12345), b"abcdef")
        finally:
            local.close()

        self.assertEqual(remote.sent, b"abcdef")
        self.assertEqual(len(self.pipe.calls), 1)

    def test_unwrap_sendall_forwards_entire_prefix_before_pipe_publish(self):
        remote = PartialSendSocket()
        req = make_request()
        local = socket.socket()

        try:
            with mock.patch.object(smart_route.socks, "create_connection", return_value=remote):
                smart_route.do_unwrap_socks(
                    local, b"example.com", 80, ("192.0.2.10", 12345), req, b"abcdef")
        finally:
            local.close()

        self.assertEqual(remote.sent, b"abcdef")
        self.assertEqual(len(self.pipe.calls), 1)
        self.assertEqual(self.pipe.calls[0][2], b"abcdef")

    def test_unwrap_transfers_buffered_request_body_after_headers(self):
        remote = PartialSendSocket()
        client, peer = socket.socketpair()
        request_bytes = (
            b"OPTIONS / HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"Content-Length: 6\r\n\r\n"
            b"BODY!!"
        )
        peer.sendall(request_bytes)
        req = simple_http_server.HttpServerHandler(
            client, ("192.0.2.10", 12345), None, SilentLogger())
        self.assertTrue(req.parse_request())

        try:
            with mock.patch.object(smart_route.socks, "create_connection", return_value=remote):
                smart_route.do_unwrap_socks(
                    client, b"example.com", 80, ("192.0.2.10", 12345), req, request_bytes[:-6])
        finally:
            req.connection = None
            client.close()
            peer.close()

        self.assertEqual(remote.sent, request_bytes)
        self.assertEqual(len(self.pipe.calls), 1)

    def test_unwrap_failed_prefix_send_closes_remote_without_publishing(self):
        remote = PartialSendSocket(fail=True)
        req = make_request()
        local = socket.socket()

        try:
            with mock.patch.object(smart_route.socks, "create_connection", return_value=remote):
                try:
                    smart_route.do_unwrap_socks(
                        local, b"example.com", 80, ("192.0.2.10", 12345), req, b"abcdef")
                except OSError as error:
                    self.fail("发送失败后仍向调用方抛出异常：%r" % error)
        finally:
            local.close()

        self.assertTrue(remote.closed)
        self.assertEqual(self.pipe.calls, [])
        self.assertIsNotNone(req.connection)

    def test_direct_send_failure_closes_remote_without_publishing(self):
        remote = PartialSendSocket(fail=True)
        remote.ip = "192.0.2.80"
        g.connect_manager = types.SimpleNamespace(get_conn=lambda *args: remote)
        local = socket.socket()
        self.addCleanup(local.close)

        with self.assertRaises(smart_route.ConnectFail):
            smart_route.do_direct(
                local, b"example.com", [b"192.0.2.80"], 80,
                ("192.0.2.10", 12345), b"abcdef")

        self.assertTrue(remote.closed)
        self.assertEqual(self.pipe.calls, [])

    def test_direct_publish_failure_closes_remote(self):
        remote = PartialSendSocket()
        remote.ip = "192.0.2.80"
        g.connect_manager = types.SimpleNamespace(get_conn=lambda *args: remote)
        self.pipe.add_socks = mock.Mock(side_effect=OSError("publish failed"))
        local = socket.socket()
        self.addCleanup(local.close)

        with self.assertRaises(OSError):
            smart_route.do_direct(local, b"example.com", [b"192.0.2.80"], 80,
                                  ("192.0.2.10", 12345), b"abcdef")

        self.assertTrue(remote.closed)

    def test_unwrap_publish_failure_closes_remote_and_preserves_local_owner(self):
        remote = PartialSendSocket()
        req = make_request()
        self.pipe.add_socks = mock.Mock(side_effect=OSError("publish failed"))
        local = socket.socket()
        self.addCleanup(local.close)

        with mock.patch.object(smart_route.socks, "create_connection", return_value=remote):
            try:
                smart_route.do_unwrap_socks(
                    local, b"example.com", 80, ("192.0.2.10", 12345), req, b"abcdef")
            except OSError as error:
                self.fail("发布失败应完成资源收尾并返回：%r" % error)

        self.assertTrue(remote.closed)
        self.assertIsNotNone(req.connection)
        self.assertTrue(req.rfile._close)
        self.assertTrue(req.wfile._close)

    def test_direct_rejected_publication_closes_remote_and_signals_failure(self):
        remote = PartialSendSocket()
        remote.ip = "192.0.2.80"
        g.connect_manager = types.SimpleNamespace(get_conn=lambda *args: remote)
        self.pipe.add_socks = mock.Mock(return_value=False)
        local = socket.socket()
        self.addCleanup(local.close)

        with self.assertRaises(smart_route.ConnectFail):
            smart_route.do_direct(local, b"example.com", [b"192.0.2.80"], 80,
                                  ("192.0.2.10", 12345), b"abcdef")

        self.assertTrue(remote.closed)

    def test_redirect_rejected_publication_closes_tls_remote_and_signals_failure(self):
        remote = PartialSendSocket()
        remote._sock = remote
        remote.ip = "192.0.2.80"
        tls_remote = PartialSendSocket()
        g.connect_manager = types.SimpleNamespace(get_conn=lambda *args: remote)
        self.pipe.add_socks = mock.Mock(return_value=False)
        local = smart_route.SocketWrap(socket.socket(), "192.0.2.10", 12345)
        self.addCleanup(local.close)

        with mock.patch.object(smart_route.ssl_context, "wrap_socket", return_value=tls_remote):
            with self.assertRaises(smart_route.RedirectHttpsFail):
                smart_route.do_redirect_https(
                    local, b"example.com", [b"192.0.2.80"], 80,
                    ("192.0.2.10", 12345), b"abcdef")

        self.assertTrue(tls_remote.closed)

    def test_unwrap_rejected_publication_preserves_local_owner(self):
        remote = PartialSendSocket()
        req = make_request()
        self.pipe.add_socks = mock.Mock(return_value=False)
        local = socket.socket()
        self.addCleanup(local.close)

        with mock.patch.object(smart_route.socks, "create_connection", return_value=remote):
            smart_route.do_unwrap_socks(
                local, b"example.com", 80, ("192.0.2.10", 12345), req, b"abcdef")

        self.assertTrue(remote.closed)
        self.assertIsNotNone(req.connection)
        self.assertTrue(req.rfile._close)
        self.assertTrue(req.wfile._close)


if __name__ == "__main__":
    unittest.main()
