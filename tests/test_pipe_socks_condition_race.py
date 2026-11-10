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

    def test_stopped_pipe_rejects_and_closes_new_pair(self):
        pipe = pipe_module.PipeSocks()
        pipe.stop()
        left, left_peer = socket.socketpair()
        right, right_peer = socket.socketpair()
        local = wrap_module.SocketWrap(left, "127.0.0.1", 12345)
        remote = wrap_module.SocketWrap(right, "192.0.2.1", 443, "example.com")

        try:
            pipe.add_socks(local, remote)

            self.assertTrue(local.is_closed())
            self.assertTrue(remote.is_closed())
            self.assertEqual(pipe.read_set, set())
            self.assertEqual(pipe.write_set, set())
        finally:
            left_peer.close()
            right_peer.close()
            local.close()
            remote.close()

    def test_select_failure_makes_pipe_unpublishable_and_cleans_membership(self):
        pipe = pipe_module.PipeSocks()
        pending = object()
        pipe.read_set = {pending}
        pipe.close = lambda sock, reason: pipe.read_set.discard(sock)
        pipe.select2 = types.SimpleNamespace(
            select=lambda timeout: (_ for _ in ()).throw(ValueError("detached selector")))

        pipe.pipe()

        self.assertFalse(pipe.running)
        self.assertEqual(pipe.read_set, set())

    def test_select_oserror_also_stops_and_cleans_membership(self):
        pipe = pipe_module.PipeSocks()
        pending = object()
        pipe.read_set = {pending}
        pipe.close = lambda sock, reason: pipe.read_set.discard(sock)
        pipe.select2 = types.SimpleNamespace(
            select=lambda timeout: (_ for _ in ()).throw(OSError("selector failed")))

        try:
            pipe.pipe()
        except Exception as error:
            self.fail("selector OSError 应进入统一收尾而非逃逸：%r" % error)

        self.assertFalse(pipe.running)
        self.assertEqual(pipe.read_set, set())

    def test_registration_failure_rolls_back_whole_pair(self):
        for fail_at in (1, 2):
            with self.subTest(fail_at=fail_at):
                pipe = pipe_module.PipeSocks()
                left, left_peer = socket.socketpair()
                right, right_peer = socket.socketpair()
                local = wrap_module.SocketWrap(left, "127.0.0.1", 12345)
                remote = wrap_module.SocketWrap(right, "192.0.2.1", 443, "example.com")
                register_calls = []

                class FailingSelector:
                    def register(self, sock, event):
                        register_calls.append(sock)
                        if len(register_calls) == fail_at:
                            raise OSError("register failed")

                    def unregister(self, sock):
                        pass

                    def modify(self, sock, event):
                        pass

                pipe.select2 = FailingSelector()
                try:
                    result = pipe.add_socks(local, remote)

                    self.assertFalse(result)
                    self.assertTrue(local.is_closed())
                    self.assertTrue(remote.is_closed())
                    self.assertEqual(pipe.read_set, set())
                    self.assertEqual(pipe.write_set, set())
                    self.assertTrue(
                        pipe.running,
                        "单对连接注册失败不得拖停仍在服务其他连接的管道")

                    recovery_left, recovery_left_peer = socket.socketpair()
                    recovery_right, recovery_right_peer = socket.socketpair()
                    recovery_local = wrap_module.SocketWrap(
                        recovery_left, "127.0.0.1", 12346)
                    recovery_remote = wrap_module.SocketWrap(
                        recovery_right, "192.0.2.2", 443, "example.com")
                    try:
                        self.assertTrue(
                            pipe.add_socks(recovery_local, recovery_remote),
                            "失败配对完整回滚后，下一对健康连接必须仍可注册")
                    finally:
                        pipe.stop()
                        recovery_left_peer.close()
                        recovery_right_peer.close()
                        recovery_local.close()
                        recovery_remote.close()
                finally:
                    left_peer.close()
                    right_peer.close()
                    local.close()
                    remote.close()
    def test_buffered_payload_closes_pair_when_write_registration_fails(self):
        pipe = pipe_module.PipeSocks()
        left, left_peer = socket.socketpair()
        right, right_peer = socket.socketpair()
        local = wrap_module.SocketWrap(left, "127.0.0.1", 12345)
        remote = wrap_module.SocketWrap(right, "192.0.2.1", 443, "example.com")
        remote.send = lambda data: 0

        class FailingWriteSelector:
            def __init__(self):
                self.select_calls = 0

            def register(self, sock, event):
                pass

            def unregister(self, sock):
                pass

            def modify(self, sock, event):
                if event & pipe_module.selectors.EVENT_WRITE:
                    raise OSError("injected write modify failure")

            def select(self, timeout):
                self.select_calls += 1
                if self.select_calls == 1:
                    return [(types.SimpleNamespace(fileobj=local), pipe_module.selectors.EVENT_READ)]
                pipe.running = False
                return []

        pipe.select2 = FailingWriteSelector()
        try:
            self.assertTrue(pipe.add_socks(local, remote))
            left_peer.sendall(b"buffered")
            close_reasons = []
            original_close = pipe.close
            pipe.close = lambda sock, reason: close_reasons.append(reason) or original_close(sock, reason)
            worker = threading.Thread(target=pipe.pipe)
            worker.start()
            for _ in range(100):
                if close_reasons:
                    break
                threading.Event().wait(0.005)
            pipe.stop()
            worker.join(2)

            self.assertFalse(worker.is_alive())
            self.assertIn(
                "write_register", close_reasons,
                "WRITE 注册失败后必须立即收尾，而不是等 pipe 最终停止时清理")
            self.assertTrue(
                remote.is_closed(),
                "WRITE 注册失败后不得保留永远无法刷新的缓冲连接")
            self.assertTrue(local.is_closed())
            self.assertNotIn(remote, pipe.write_set)
        finally:
            left_peer.close()
            right_peer.close()
            local.close()
            remote.close()


if __name__ == "__main__":
    unittest.main()
