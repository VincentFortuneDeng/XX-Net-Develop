"""PipeSocks 空集合检查与条件等待之间不能丢失新连接通知。"""
import importlib.util
from pathlib import Path
import socket
import sys
import threading
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
namespace("smart_router.local", APP / "smart_router" / "local")
global_var = namespace("smart_router.local.global_var")
global_var.config = types.SimpleNamespace(direct_split_SNI=False)
pipe_module = load_module(
    "smart_router.local.pipe_socks", APP / "smart_router" / "local" / "pipe_socks.py")
wrap_module = load_module(
    "smart_router.local.socket_wrap", APP / "smart_router" / "local" / "socket_wrap.py")


class GatedCondition:
    """只在转发线程首次进入条件区前暂停，制造真实的检查/等待交错。"""

    def __init__(self, pipe_thread_name):
        self.condition = threading.Condition()
        self.pipe_thread_name = pipe_thread_name
        self.entered = threading.Event()
        self.release = threading.Event()
        self.gated = False

    def __enter__(self):
        if threading.current_thread().name == self.pipe_thread_name and not self.gated:
            self.gated = True
            self.entered.set()
            self.release.wait(2)
        return self.condition.__enter__()

    def __exit__(self, *args):
        return self.condition.__exit__(*args)

    def wait(self):
        return self.condition.wait()

    def notify(self):
        return self.condition.notify()


class PipeSocksConditionRaceTest(unittest.TestCase):
    def test_first_connection_added_before_wait_is_not_stranded(self):
        pipe = pipe_module.PipeSocks()
        thread_name = "pipe-lost-notify-test"
        gate = GatedCondition(thread_name)
        pipe.sock_notify = gate
        app, local_sock = socket.socketpair()
        remote_sock, target = socket.socketpair()
        local = wrap_module.SocketWrap(local_sock, "127.0.0.1", 12345)
        remote = wrap_module.SocketWrap(remote_sock, "192.0.2.1", 443, "example.com")
        thread = threading.Thread(target=pipe.pipe, name=thread_name, daemon=True)
        received = []
        error = []

        def receive():
            target.settimeout(0.3)
            try:
                received.append(target.recv(1))
            except Exception as caught:
                error.append(caught)

        try:
            thread.start()
            self.assertTrue(gate.entered.wait(1))
            pipe.add_socks(local, remote)
            app.sendall(b"x")
            gate.release.set()
            receive()

            self.assertEqual(received, [b"x"])
            self.assertEqual(error, [])
        finally:
            gate.release.set()
            pipe.stop()
            thread.join(6)
            app.close()
            target.close()
            local.close()
            remote.close()
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
