"""代理协议读取在客户端 EOF 后必须退出，不能继续轮询空字节。"""
import importlib.util
from pathlib import Path
import socket
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "Coding" / "XX-Net-5.16.7.1" / "code" / "default"
sys.path.insert(0, str(APP / "lib" / "noarch"))


def namespace(name, path=None):
    module = types.ModuleType(name)
    if path is not None:
        module.__path__ = [str(path)]
    sys.modules[name] = module
    return module


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


namespace("smart_router", APP / "smart_router")
smart_local = namespace("smart_router.local", APP / "smart_router" / "local")
namespace("smart_router.local.global_var")
namespace("smart_router.local.pac_server")
socket_wrap = namespace("smart_router.local.socket_wrap")
socket_wrap.SocketWrap = object
smart_route = namespace("smart_router.local.smart_route")
smart_route.handle_ip_proxy = lambda *args: None
smart_route.handle_domain_proxy = lambda *args: None
smart_route.netloc_to_host_port = lambda netloc: (netloc, 80)
smart_handler = load_module(
    "smart_router.local.proxy_handler", APP / "smart_router" / "local" / "proxy_handler.py")

namespace("x_tunnel", APP / "x_tunnel")
namespace("x_tunnel.local", APP / "x_tunnel" / "local")
namespace("x_tunnel.local.global_var")
namespace("x_tunnel.local.proxy_session")
namespace("x_tunnel.local.openai_handler")
xtunnel_handler = load_module(
    "x_tunnel.local.proxy_handler", APP / "x_tunnel" / "local" / "proxy_handler.py")


class EofSocket:
    def __init__(self):
        self.recv_calls = 0

    def setblocking(self, value):
        pass

    def recv(self, size):
        self.recv_calls += 1
        if self.recv_calls == 1:
            return b""
        raise RuntimeError("读取循环在 EOF 后再次调用 recv")


class ProxyProtocolEofTest(unittest.TestCase):
    def test_line_and_header_readers_stop_on_eof(self):
        cases = (
            ("smart_router", smart_handler.ProxyServer),
            ("x_tunnel", xtunnel_handler.Socks5Server),
        )
        for module_name, handler_class in cases:
            for method_name in ("read_null_end_line", "read_crlf_line", "read_headers"):
                with self.subTest(module=module_name, method=method_name):
                    handler = handler_class.__new__(handler_class)
                    if module_name == "smart_router":
                        handler.conn = EofSocket()
                    else:
                        handler.connection = EofSocket()
                    handler.read_buffer = b"unfinished"
                    handler.buffer_start = 0
                    error = None
                    try:
                        getattr(handler, method_name)()
                    except Exception as caught:
                        error = caught

                    sock = handler.conn if module_name == "smart_router" else handler.connection
                    self.assertIsInstance(error, socket.error)
                    self.assertEqual(sock.recv_calls, 1)


if __name__ == "__main__":
    unittest.main()
