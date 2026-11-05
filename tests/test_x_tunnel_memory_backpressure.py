import importlib.util
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "Coding" / "XX-Net-5.16.7.1" / "code" / "default"
sys.path.insert(0, str(APP / "lib" / "noarch"))
import xstruct as struct


def namespace(name, path=None, **attributes):
    module = types.ModuleType(name)
    if path is not None:
        module.__path__ = [str(path)]
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


namespace("x_tunnel", APP / "x_tunnel")
namespace("x_tunnel.local", APP / "x_tunnel" / "local")
global_var = namespace(
    "x_tunnel.local.global_var",
    running=True,
    stat={"resend": 0},
)
namespace("gae_proxy", APP / "gae_proxy")
namespace("gae_proxy.local", APP / "gae_proxy" / "local")
namespace("gae_proxy.local.check_local_network", IPv6=types.SimpleNamespace(is_ok=lambda: False))
namespace("x_tunnel.local.upload_logs", upload_logs_thread=lambda: None)
namespace("x_tunnel.local.openai_handler")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


base = load_module("x_tunnel.local.base_container", APP / "x_tunnel" / "local" / "base_container.py")
proxy = load_module("x_tunnel.local.proxy_session", APP / "x_tunnel" / "local" / "proxy_session.py")
proxy_handler = load_module("x_tunnel.local.proxy_handler", APP / "x_tunnel" / "local" / "proxy_handler.py")


class Pipe:
    def __init__(self):
        self.events = []

    def add_sock_event(self, sock, conn, event):
        self.events.append(("add", event))

    def remove_sock_event(self, sock, event):
        self.events.append(("remove", event))


class Conn:
    def __init__(self):
        self.sock = object()
        self.running = True
        self.received_position = 0
        self.remote_acked_position = 0
        self.windows_size = 10
        self.connection_pipe = Pipe()


class RecordingConn:
    def __init__(self):
        self.events = []

    def transfer_received_data(self, data):
        self.events.append(("transfer", data))

    def start(self, block):
        self.events.append(("start", block))

    def do_stop(self, reason):
        self.events.append(("stop", reason))


class MemoryBackpressureTest(unittest.TestCase):
    def make_session(self):
        session = proxy.ProxySession.__new__(proxy.ProxySession)
        session.config = global_var.config
        session.session_id = b"test-session"
        session.running = True
        session.lock = threading.Lock()
        session.send_buffer = base.SendBuffer(max_payload=16)
        session.wait_queue = types.SimpleNamespace(notify=lambda: None, stop=lambda: None)
        session.wait_ack_send_list = {}
        session.ack_send_continue_sn = 0
        session.oldest_received_time = 0
        session.send_delay = 0
        session.resend_timeout = 60
        session.read_paused = False
        session.read_paused_since = None
        session.buffered_bytes = 0
        session.conn_list = {1: Conn()}
        return session

    def setUp(self):
        global_var.config = types.SimpleNamespace(
            show_debug=0,
            max_payload=16,
            send_delay=0,
            ack_delay=0,
            resend_timeout=60000,
            session_buffer_high_water=100,
            session_buffer_low_water=40,
            session_backpressure_timeout=60,
        )

    def test_unacked_bytes_pause_and_resume_read_with_hysteresis(self):
        session = self.make_session()
        conn = session.conn_list[1]

        session.send_conn_data(1, b"first-" + b"a" * 64)
        session.send_conn_data(1, b"second-" + b"b" * 64)

        self.assertTrue(session.read_paused)
        self.assertIn(("remove", base.selectors.EVENT_READ), conn.connection_pipe.events)
        self.assertEqual(session.send_buffer.pool_size, 157)

        first_payload = session.get_data(0)
        session.ack_process(base.ReadBuffer(struct.pack("<II", 1, 1)))
        self.assertTrue(session.read_paused)

        second_payload = session.get_data(0)
        self.assertEqual(session.send_buffer.pool_size, 0)
        self.assertEqual(len(session.wait_ack_send_list), 1)
        session.ack_process(base.ReadBuffer(struct.pack("<II", 2, 2)))

        self.assertFalse(session.read_paused)
        self.assertIn(("add", base.selectors.EVENT_READ), conn.connection_pipe.events)
        self.assertEqual(session.wait_ack_send_list, {})
        self.assertEqual(session.buffered_bytes, 0)
        self.assertIn(b"first-", bytes(first_payload))
        self.assertIn(b"second-", bytes(second_payload))
        self.assertLess(bytes(first_payload).index(b"first-"), len(bytes(first_payload)))

    def test_connection_ack_does_not_override_session_pause(self):
        session = self.make_session()
        session.read_paused = True
        session.send_conn_data(1, b"x" * 140)
        conn = base.Conn.__new__(base.Conn)
        conn.session = session
        conn.conn_id = 1
        conn.sock = object()
        conn.running = True
        conn.blocked = False
        conn.send_buffer = None
        conn._lock = threading.Lock()
        conn.cmd_queue = {1: base.ReadBuffer(struct.pack("<BQ", 3, 10))}
        conn.next_cmd_seq = 1
        conn.sended_position = 0
        conn.remote_acked_position = 0
        conn.received_position = 100
        conn.windows_size = 10
        conn.connection_pipe = session.conn_list[1].connection_pipe
        conn.xlog = types.SimpleNamespace(debug=lambda *args: None)

        conn.process_cmd()

        self.assertTrue(session.read_paused)
        self.assertNotIn(("add", base.selectors.EVENT_READ), conn.connection_pipe.events)

    def test_conn_removal_cannot_invalidate_backpressure_snapshot(self):
        session = self.make_session()
        session.conn_list[2] = Conn()
        registry = session.conn_list

        class RacingRegistry(dict):
            def values(self):
                iterator = iter(super().values())

                class Iterator:
                    def __init__(self):
                        self.started = False

                    def __iter__(self):
                        return self

                    def __next__(self):
                        if not self.started:
                            self.started = True
                            value = next(iterator)
                            worker = threading.Thread(target=session.remove_conn, args=(2,))
                            worker.start()
                            worker.join(0.05)
                            return value
                        return next(iterator)

                return Iterator()

        session.conn_list = RacingRegistry(registry)
        session.update_backpressure()
        for _ in range(20):
            if 2 not in session.conn_list:
                break
            threading.Event().wait(0.005)

        self.assertNotIn(2, session.conn_list)

    def test_start_conn_returns_false_when_connection_was_removed(self):
        session = self.make_session()
        other_conn = RecordingConn()
        session.conn_list = {2: other_conn}

        started = session.start_conn(1, b"first-packet")

        self.assertFalse(started)
        self.assertEqual(other_conn.events, [])

    def test_start_conn_transfers_first_packet_before_start(self):
        session = self.make_session()
        conn = RecordingConn()
        session.conn_list = {1: conn}

        started = session.start_conn(1, b"first-packet", block=False)

        self.assertTrue(started)
        self.assertEqual(conn.events, [("transfer", b"first-packet"), ("start", False)])

    def test_start_conn_without_first_packet_only_starts(self):
        session = self.make_session()
        conn = RecordingConn()
        session.conn_list = {1: conn}

        started = session.start_conn(1)

        self.assertTrue(started)
        self.assertEqual(conn.events, [("start", True)])

    def test_socks5_reply_failure_tolerates_concurrent_conn_removal(self):
        session = self.make_session()
        conn = RecordingConn()

        class RemoveAfterContains(dict):
            def __contains__(self, conn_id):
                existed = super().__contains__(conn_id)
                if existed:
                    self.pop(conn_id)
                return existed

        session.conn_list = RemoveAfterContains({1: conn})
        global_var.session = session
        handler = proxy_handler.Socks5Server.__new__(proxy_handler.Socks5Server)
        handler.connection = mock.Mock()
        handler.connection.send.side_effect = [None, OSError("reply failed")]
        handler.client_address = ("127.0.0.1", 12345)
        handler.read_bytes = mock.Mock(side_effect=[
            b"\x01",
            b"\x00",
            b"\x05\x01\x00\x01",
            b"\x7f\x00\x00\x01",
        ])
        handler.rfile = types.SimpleNamespace(read=lambda size: b"\x00\x50")

        with mock.patch.object(proxy, "create_conn", return_value=1):
            handler.socks5_handler()

        self.assertEqual(conn.events, [("stop", "close_on_Socks5_reply")])

    def test_permanent_ack_stall_eventually_resets_session(self):
        session = self.make_session()
        session.read_paused = True
        session.read_paused_since = 10.0
        session.reset = mock.Mock()
        global_var.config.session_backpressure_timeout = 60

        with mock.patch.object(proxy.time, "monotonic", return_value=71.0):
            check = getattr(session, "check_backpressure_timeout", lambda: None)
            check()

        session.reset.assert_called_once_with()

    def test_stop_clears_retained_bytes_and_pause_state(self):
        session = self.make_session()
        session.send_conn_data(1, b"x" * 140)
        session.get_data(0)
        session.wait_queue = types.SimpleNamespace(notify=lambda: None, stop=lambda: None)
        session.receive_process = types.SimpleNamespace(
            reset=lambda defer_log: (0, 0),
            log_reset=lambda *args: None,
        )
        session.connection_pipe = types.SimpleNamespace(stop=lambda: None)
        session.close_all_connection = lambda: None
        session.transfer_log_throttle = types.SimpleNamespace(flush=lambda: (0, 0))
        global_var.config.concurent_thread_num = 0

        session.stop()

        self.assertFalse(session.read_paused)
        self.assertEqual(session.wait_ack_send_list, {})
        self.assertEqual(session.send_buffer.pool_size, 0)


if __name__ == "__main__":
    unittest.main()
