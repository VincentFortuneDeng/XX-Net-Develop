import importlib.util
from pathlib import Path
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


def namespace(name, path=None, **attributes):
    module = types.ModuleType(name)
    if path is not None:
        module.__path__ = [str(path)]
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


namespace("smart_router", APP / "smart_router")
namespace("smart_router.local", APP / "smart_router" / "local")
global_var = namespace(
    "smart_router.local.global_var",
    config=types.SimpleNamespace(PROXY_ENABLE=0),
)


class IpCache:
    def get_connect_time(self, ip, port):
        return 1

    def update_connect_time(self, ip, port, cost):
        pass

    def report_connect_fail(self, ip, port):
        pass


global_var.ip_cache = IpCache()


class SocketWrap:
    def __init__(self, sock, ip, port, host):
        self.sock = sock
        self.ip = ip
        self.port = port
        self.host = host
        self.closed = False

    def close(self):
        self.closed = True
        self.sock.close()


namespace("smart_router.local.socket_wrap", SocketWrap=SocketWrap)

SPEC = importlib.util.spec_from_file_location(
    "smart_router.local.connect_manager",
    APP / "smart_router" / "local" / "connect_manager.py",
)
connect_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = connect_module
SPEC.loader.exec_module(connect_module)


class RawSocket:
    def __init__(self, behavior):
        self.behavior = behavior
        self.closed = False

    def setsockopt(self, *args):
        pass

    def settimeout(self, timeout):
        pass

    def connect(self, address):
        return self.behavior(address)

    def close(self):
        self.closed = True


class ConnectBoundsTest(unittest.TestCase):
    def make_manager(self, connect_threads=2):
        manager = connect_module.ConnectManager.__new__(connect_module.ConnectManager)
        manager.lock = threading.Lock()
        manager.cache = {}
        manager.connection_timeout = 15
        manager.connect_timeout = 5
        manager.connect_threads = connect_threads
        manager.max_cache_per_host = connect_threads
        manager.max_cache_total = 4
        manager.running = True
        return manager

    def test_connect_race_limits_in_flight_sockets_and_closes_failures(self):
        manager = self.make_manager(connect_threads=2)
        active = 0
        maximum = 0
        counter_lock = threading.Lock()
        sockets = []

        def make_socket(*args):
            def behavior(address):
                nonlocal active, maximum
                with counter_lock:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.03)
                with counter_lock:
                    active -= 1
                raise OSError("injected connect failure")

            raw = RawSocket(behavior)
            sockets.append(raw)
            return raw

        ips = [("192.0.2.%d" % index).encode() for index in range(1, 7)]
        with mock.patch.object(connect_module.socket, "socket", side_effect=make_socket):
            result = manager.get_conn("slow.example", ips, 443, timeout=0.5)

        self.assertIsNone(result)
        self.assertLessEqual(maximum, 2)
        self.assertEqual(len(sockets), 6)
        self.assertTrue(all(raw.closed for raw in sockets))

    def test_cache_enforces_per_host_and_global_limits(self):
        manager = self.make_manager(connect_threads=2)
        sockets = [RawSocket(lambda address: None) for _ in range(8)]

        for index in range(4):
            manager.add_sock("one.example:443", SocketWrap(sockets[index], b"ip", 443, "one.example"))
        for index in range(4, 8):
            manager.add_sock("two.example:443", SocketWrap(sockets[index], b"ip", 443, "two.example"))

        self.assertLessEqual(len(manager.cache.get("one.example:443", [])), 2)
        self.assertLessEqual(sum(len(items) for items in manager.cache.values()), 4)
        self.assertTrue(all(raw.closed for raw in sockets[2:4]))

    def test_expired_last_connection_removes_empty_host_key(self):
        manager = self.make_manager()
        raw = RawSocket(lambda address: None)
        wrapped = SocketWrap(raw, b"ip", 443, "old.example")
        manager.cache["old.example:443"] = [{"create_time": 0, "conn": wrapped}]

        self.assertIsNone(manager.get_sock_from_cache("old.example:443"))
        self.assertNotIn("old.example:443", manager.cache)
        self.assertTrue(raw.closed)

    def test_stop_closes_cached_and_late_connections(self):
        manager = self.make_manager()
        cached = RawSocket(lambda address: None)
        late = RawSocket(lambda address: None)
        manager.add_sock("stop.example:443", SocketWrap(cached, b"ip", 443, "stop.example"))

        manager.stop()
        manager.add_sock("stop.example:443", SocketWrap(late, b"ip", 443, "stop.example"))

        self.assertTrue(cached.closed)
        self.assertTrue(late.closed)
        self.assertEqual(manager.cache, {})


if __name__ == "__main__":
    unittest.main()
