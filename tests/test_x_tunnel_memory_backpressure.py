import importlib.util
from pathlib import Path
import collections
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

    def remove_sock(self, sock):
        self.events.append(("remove_sock", sock))


class Conn:
    def __init__(self):
        self.sock = object()
        self.running = True
        self.read_started = True
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
        session.lock = threading.RLock()
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
        conn.read_started = True
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

    def test_create_conn_send_failure_removes_unstarted_conn(self):
        session = self.make_session()
        session.conn_list = {}
        session.connection_pipe = Pipe()
        session.last_conn_id = 0
        session.target_on_roads = 0
        session.trigger_more = mock.Mock()
        session.send_conn_data = mock.Mock(side_effect=RuntimeError("create frame failed"))
        global_var.config.windows_size = 10
        global_var.config.windows_ack = 5
        sock = mock.Mock()
        sock.fileno.return_value = 10

        with self.assertRaisesRegex(RuntimeError, "create frame failed"):
            session.create_conn(sock, "example.com", 443)

        self.assertEqual(session.conn_list, {})

    def test_create_conn_waits_for_start_before_enabling_local_read(self):
        session = self.make_session()
        session.conn_list = {}
        session.connection_pipe = Pipe()
        session.last_conn_id = 0
        session.target_on_roads = 0
        session.trigger_more = mock.Mock()
        global_var.config.windows_size = 10
        global_var.config.windows_ack = 5
        global_var.config.concurent_thread_num = 4
        global_var.config.min_on_road = 1
        sock = mock.Mock()
        sock.fileno.return_value = 10

        conn_id = session.create_conn(sock, "example.com", 443)

        self.assertNotIn(("add", base.selectors.EVENT_READ), session.connection_pipe.events)
        self.assertTrue(session.start_conn(conn_id, block=False))
        self.assertIn(("add", base.selectors.EVENT_READ), session.connection_pipe.events)

    def test_server_side_conn_still_enables_read_after_connect(self):
        session = self.make_session()
        session.conn_list = {}
        session.connection_pipe = Pipe()
        conn = base.Conn(
            session, 2, None, b"remote.example", 443, 10, 5, False,
            types.SimpleNamespace(debug=lambda *args: None),
        )
        conn.sock = mock.Mock()
        conn.sock.fileno.return_value = 11
        session.conn_list[2] = conn

        session.update_backpressure()

        self.assertIn(("add", base.selectors.EVENT_READ), session.connection_pipe.events)

    def test_stale_conn_stop_cannot_remove_reused_conn_id(self):
        session = self.make_session()
        new_conn = RecordingConn()
        session.conn_list = {1: new_conn}
        old_conn = base.Conn.__new__(base.Conn)
        old_conn.session = session
        old_conn.conn_id = 1
        old_conn.host = b"old.example"
        old_conn.port = 443
        old_conn._fd = 10
        old_conn.running = True
        old_conn.sock = None
        old_conn.cmd_queue = {}
        old_conn.connection_pipe = Pipe()
        old_conn.xlog = types.SimpleNamespace(debug=lambda *args: None)

        old_conn.do_stop("delayed old reset")

        self.assertIs(session.conn_list.get(1), new_conn)
        self.assertEqual(new_conn.events, [])

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

    def test_start_conn_failure_stops_failed_conn_without_touching_others(self):
        session = self.make_session()
        conn = RecordingConn()
        other_conn = RecordingConn()
        conn.transfer_received_data = mock.Mock(side_effect=RuntimeError("first packet failed"))
        session.conn_list = {1: conn, 2: other_conn}

        with self.assertRaisesRegex(RuntimeError, "first packet failed"):
            session.start_conn(1, b"first-packet")

        self.assertEqual(conn.events, [("stop", "start_conn_fail")])
        self.assertEqual(other_conn.events, [])

    def test_socks4_reply_failure_stops_created_conn(self):
        session = self.make_session()
        conn = RecordingConn()
        session.conn_list = {1: conn}
        global_var.session = session
        handler = proxy_handler.Socks5Server.__new__(proxy_handler.Socks5Server)
        handler.connection = mock.Mock()
        handler.connection.send.side_effect = OSError("reply failed")
        handler.client_address = ("127.0.0.1", 12345)
        handler.read_bytes = mock.Mock(side_effect=[
            b"\x01",
            b"\x00\x50\x7f\x00\x00\x01",
        ])
        handler.read_null_end_line = mock.Mock(return_value=b"")
        handler.read_buffer = b""
        handler.buffer_start = 0

        with mock.patch.object(proxy, "create_conn", return_value=1):
            handler.socks4_handler()

        self.assertEqual(conn.events, [("stop", "close_on_Socks4_reply")])

    def test_https_reply_failure_stops_created_conn_without_start(self):
        session = self.make_session()
        conn = RecordingConn()
        session.conn_list = {1: conn}
        global_var.session = session
        handler = proxy_handler.Socks5Server.__new__(proxy_handler.Socks5Server)
        handler.connection = mock.Mock()
        handler.connection.send.side_effect = OSError("reply failed")
        handler.client_address = ("127.0.0.1", 12345)
        handler.read_crlf_line = mock.Mock(return_value=b"ONNECT example.com:443 HTTP/1.1")
        handler.read_headers = mock.Mock(return_value=b"")
        handler.read_buffer = b""
        handler.buffer_start = 0

        with mock.patch.object(proxy, "create_conn", return_value=1):
            handler.https_handler()

        self.assertEqual(conn.events, [("stop", "close_on_Https_reply")])

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

    def test_start_creates_live_timeout_checker(self):
        global_var.config.enable_tls_relay = False
        global_var.config.upload_logs = False
        global_var.config.concurent_thread_num = 0
        session = proxy.ProxySession()
        session.timeout_check_th.join(1)
        self.assertFalse(session.timeout_check_th.is_alive())
        session.login_session = mock.Mock(return_value=True)

        try:
            self.assertTrue(session.start())
            self.assertTrue(session.timeout_check_th.is_alive())
        finally:
            session.running = False
            session.timeout_check_th.join(2)
            session.wait_queue.stop()

    def test_restart_replaces_stopped_wait_queue(self):
        global_var.config.enable_tls_relay = False
        global_var.config.upload_logs = False
        global_var.config.concurent_thread_num = 0
        session = proxy.ProxySession()
        session.timeout_check_th.join(1)
        session.login_session = mock.Mock(return_value=True)

        self.assertTrue(session.start())
        session.stop()
        self.assertFalse(session.wait_queue.running)
        try:
            self.assertTrue(session.start())
            self.assertTrue(session.wait_queue.running)
        finally:
            session.stop()
            session.timeout_check_th.join(2)

    def test_restart_replaces_alive_but_exiting_timeout_checker(self):
        global_var.config.enable_tls_relay = False
        global_var.config.upload_logs = False
        global_var.config.concurent_thread_num = 0
        session = proxy.ProxySession()
        session.timeout_check_th.join(1)
        old_checker = types.SimpleNamespace(
            is_alive=lambda: True, join=lambda timeout: None)
        session.timeout_check_th = old_checker
        session.login_session = mock.Mock(return_value=True)

        try:
            self.assertTrue(session.start())
            self.assertIsNot(session.timeout_check_th, old_checker)
            self.assertTrue(session.timeout_check_th.is_alive())
        finally:
            session.stop()
            session.timeout_check_th.join(2)

    def test_stale_roundtrip_response_cannot_touch_new_session_transfer(self):
        session = self.make_session()
        session.get_data_lock = threading.Lock()
        session.on_road_num = 0
        session.target_on_roads = 0
        session.traffic_upload = 0
        session.traffic_download = 0
        session.server_time_deviation = 0
        session.round_trip_process = mock.Mock()
        session.check_upload_not_acked = mock.Mock()
        session.trigger_more = mock.Mock()
        data_info = {
            "transfer_no": 1,
            "stat": "request",
            "start_time": proxy.time.time() - 1,
            "server_timeout": 30,
            "send_data": base.WriteBuffer(),
            "send_ack": base.WriteBuffer(),
            "download_timeout": base.WriteBuffer(),
            "request_session_id": session.session_id,
        }
        session.transfer_list = {1: data_info}
        session.get_upload_data = mock.Mock(return_value=data_info)

        global_var.protocol_version = 1
        payload = struct.pack(
            "<cBBdIIIIIIII", b"P", global_var.protocol_version, 2,
            proxy.time.time(), 0, 0, 0, 0, 0, 0, 0, 0)
        response = types.SimpleNamespace(
            headers={b"Content-Length": str(len(payload)).encode()},
            worker=types.SimpleNamespace(ip_str="test-front"))

        def return_stale_response(**kwargs):
            session.session_id = b"new-session"
            session.transfer_list = {1: {"owner": "new-session"}}
            return payload, 200, response

        global_var.http_client = types.SimpleNamespace(request=return_stale_response)
        global_var.server_host = "test-server"
        global_var.stat = {"roundtrip_num": 0, "slow_roundtrip": 0}
        global_var.quota = 100000
        global_var.config.roundtrip_timeout = 30
        global_var.config.network_timeout = 1
        global_var.config.server_time_max_deviation = 10000
        global_var.config.concurent_thread_num = 4
        global_var.config.min_on_road = 1
        with mock.patch.object(proxy, "encrypt_data", side_effect=lambda data: data), \
                mock.patch.object(proxy, "decrypt_data", side_effect=lambda data: data):
            session.roundtrip_task(0)

        self.assertEqual(session.transfer_list, {1: {"owner": "new-session"}})
        session.round_trip_process.assert_not_called()
        self.assertEqual(session.on_road_num, 0)

    def test_stale_response_after_generation_check_cannot_touch_new_session(self):
        session = self.make_session()
        session.get_data_lock = threading.Lock()
        session.on_road_num = 0
        session.target_on_roads = 0
        session.traffic_upload = 0
        session.traffic_download = 0
        session.server_time_deviation = 0
        session.round_trip_process = mock.Mock()
        session.check_upload_not_acked = mock.Mock()
        session.trigger_more = mock.Mock()
        data_info = {
            "transfer_no": 1,
            "stat": "request",
            "start_time": proxy.time.time() - 1,
            "server_timeout": 30,
            "send_data": base.WriteBuffer(),
            "send_ack": base.WriteBuffer(),
            "download_timeout": base.WriteBuffer(),
            "request_session_id": session.session_id,
        }
        session.transfer_list = {1: data_info}
        session.get_upload_data = mock.Mock(return_value=data_info)

        global_var.protocol_version = 1
        payload = struct.pack(
            "<cBBdIIIIIIII", b"P", global_var.protocol_version, 2,
            proxy.time.time(), 0, 0, 0, 0, 0, 0, 0, 0)
        response = types.SimpleNamespace(
            headers={b"Content-Length": str(len(payload)).encode()},
            worker=types.SimpleNamespace(ip_str="test-front"))

        class ResetAfterGenerationCheck(dict):
            def __getitem__(self, key):
                if key == "roundtrip_num":
                    session.session_id = b"new-session"
                    session.transfer_list = {1: {"owner": "new-session"}}
                return super().__getitem__(key)

        global_var.http_client = types.SimpleNamespace(
            request=lambda **kwargs: (payload, 200, response))
        global_var.server_host = "test-server"
        global_var.stat = ResetAfterGenerationCheck(roundtrip_num=0, slow_roundtrip=0)
        global_var.quota = 100000
        global_var.config.roundtrip_timeout = 30
        global_var.config.network_timeout = 1
        global_var.config.server_time_max_deviation = 10000
        global_var.config.concurent_thread_num = 4
        global_var.config.min_on_road = 1
        with mock.patch.object(proxy, "encrypt_data", side_effect=lambda data: data), \
                mock.patch.object(proxy, "decrypt_data", side_effect=lambda data: data):
            session.roundtrip_task(0)

        self.assertEqual(session.transfer_list, {1: {"owner": "new-session"}})
        session.round_trip_process.assert_not_called()
        self.assertEqual(session.on_road_num, 0)

    def test_timeout_sn_is_requested_then_retry_is_throttled(self):
        logger = types.SimpleNamespace(warn=lambda *args: None)
        pool = base.BlockReceivePool(lambda data: None, logger)
        pool.mark_sn_timeout(7, t=100.0, server_time=110.0)

        self.assertEqual(pool.get_timeout_list(server_time=110.0, timeout=5), [7])
        self.assertEqual(pool.get_timeout_list(server_time=110.0, timeout=5), [])
        self.assertEqual(pool.get_timeout_list(server_time=116.0, timeout=5), [7])

    def test_wait_queue_rejects_new_waiter_after_stop(self):
        wait_queue = base.WaitQueue()
        wait_queue.stop()
        returned = threading.Event()

        waiter = threading.Thread(target=lambda: (wait_queue.wait(1), returned.set()))
        waiter.start()
        waiter.join(0.2)
        try:
            self.assertTrue(returned.is_set())
            self.assertEqual(wait_queue.waiters, [])
        finally:
            wait_queue.notify()
            waiter.join(1)

    def test_remote_data_waits_for_protocol_start_then_flushes(self):
        session = self.make_session()
        conn = base.Conn.__new__(base.Conn)
        conn.session = session
        conn.conn_id = 1
        conn.sock = mock.Mock()
        conn.running = True
        conn.read_started = False
        conn._lock = threading.Lock()
        conn.cmd_queue = {}
        conn.next_cmd_seq = 1
        conn.connection_pipe = Pipe()
        conn.xlog = types.SimpleNamespace(debug=lambda *args: None, warn=lambda *args: None)

        conn.put_cmd_data(base.ReadBuffer(struct.pack("<IB", 1, 1) + b"x"))

        self.assertNotIn(("add", base.selectors.EVENT_WRITE), conn.connection_pipe.events)
        conn.start(block=False)
        self.assertIn(("add", base.selectors.EVENT_WRITE), conn.connection_pipe.events)
        conn.sock.setblocking.assert_called_once_with(False)

    def test_nonblocking_write_buffers_would_block_without_closing(self):
        session = self.make_session()
        conn = base.Conn.__new__(base.Conn)
        conn.session = session
        conn.conn_id = 1
        conn.sock = mock.Mock()
        conn.sock.send.side_effect = [2, BlockingIOError()]
        conn.is_client = True
        conn.blocked = False
        conn.send_buffer = b""
        conn.sended_position = 0
        conn.sent_window_position = 0
        conn.windows_ack = 100
        conn.connection_pipe = Pipe()
        conn.xlog = types.SimpleNamespace(debug=lambda *args: None, info=lambda *args: None)
        conn.transfer_peer_close = mock.Mock()
        conn.do_stop = mock.Mock()

        can_continue = conn.send_to_sock(b"abcdef")

        self.assertFalse(can_continue)
        self.assertTrue(conn.blocked)
        self.assertEqual(conn.send_buffer, b"cdef")
        self.assertEqual(conn.sended_position, 2)
        self.assertIn(("add", base.selectors.EVENT_WRITE), conn.connection_pipe.events)
        conn.transfer_peer_close.assert_not_called()
        conn.do_stop.assert_not_called()

    def test_connected_target_socket_switches_to_nonblocking_io(self):
        session = self.make_session()
        conn = base.Conn.__new__(base.Conn)
        conn.session = session
        conn.xlog = types.SimpleNamespace(debug=lambda *args: None)
        sock = mock.Mock()
        with mock.patch.object(base.socket, "socket", return_value=sock):
            result, ok = conn.do_connect("127.0.0.1", 443)

        self.assertTrue(ok)
        self.assertIs(result, sock)
        sock.setblocking.assert_called_once_with(False)

    def test_old_pipe_worker_cleanup_preserves_concurrently_added_conn(self):
        cleanup_started = threading.Event()
        allow_cleanup = threading.Event()
        pipe = None

        class RacingMap(collections.UserDict):
            def keys(self):
                cleanup_started.set()
                allow_cleanup.wait(1)
                return super().keys()

        class FakeSelector:
            def register_event(self, sock, event, conn):
                pass

            def unregister_event(self, sock, event):
                return True

            def unregister(self, sock):
                pass

            def select(self, timeout):
                return []

        session = types.SimpleNamespace(
            config=types.SimpleNamespace(show_debug=False),
            session_id=b"test", check_upload=lambda: False)
        logger = types.SimpleNamespace(
            debug=lambda *args: None, warn=lambda *args: None,
            exception=lambda *args: None)
        pipe = base.ConnectionPipe(session, logger)
        pipe.select2 = FakeSelector()
        pipe.sock_conn_map = RacingMap()
        new_sock = object()
        new_conn = types.SimpleNamespace(conn_id=2)
        old_worker = threading.Thread(target=pipe.pipe_worker)
        pipe.th = old_worker
        old_worker.start()
        self.assertTrue(cleanup_started.wait(1))

        add_done = threading.Event()
        adder = threading.Thread(
            target=lambda: (pipe.add_sock_event(
                new_sock, new_conn, base.selectors.EVENT_READ), add_done.set()))
        adder.start()
        add_done.wait(0.1)
        allow_cleanup.set()
        old_worker.join(1)
        adder.join(1)

        try:
            self.assertFalse(old_worker.is_alive())
            self.assertFalse(adder.is_alive())
            self.assertIs(pipe.sock_conn_map.get(new_sock), new_conn)
        finally:
            pipe.running = False
            if pipe.th is not None:
                pipe.th.join(2)

    def test_stopped_session_rejects_frame_waiting_for_send_lock(self):
        session = self.make_session()
        inner_lock = threading.Lock()
        lock_entered = threading.Event()

        class GateLock:
            def __enter__(self):
                lock_entered.set()
                inner_lock.acquire()
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                inner_lock.release()

        session.lock = GateLock()
        session.update_backpressure = mock.Mock()
        inner_lock.acquire()
        sender = threading.Thread(target=session.send_conn_data, args=(1, b"late-frame"))
        sender.start()
        self.assertTrue(lock_entered.wait(1))

        session.running = False
        session.send_buffer.reset()
        inner_lock.release()
        sender.join(1)

        self.assertFalse(sender.is_alive())
        self.assertEqual(session.send_buffer.pool_size, 0)
        self.assertEqual(session.buffered_bytes, 0)
        session.update_backpressure.assert_not_called()

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
