import importlib.util
from pathlib import Path
import ast
import collections
import socket
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

    def remove_sock_event(self, sock, event, expected_conn=None):
        self.events.append(("remove", event))

    def remove_sock(self, sock, expected_conn=None):
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
        global_var.running = True
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

    def test_create_conn_returns_caller_credential_when_requested(self):
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

        conn_id, conn = session.create_conn(sock, "example.com", 443, return_conn=True)

        self.assertIs(session.conn_list[conn_id], conn)
        self.assertTrue(session.start_conn(conn_id, block=False, expected_conn=conn))

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
        old_conn._lock = threading.Lock()
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

        with mock.patch.object(proxy, "create_conn", return_value=(1, conn)):
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

        with mock.patch.object(proxy, "create_conn", return_value=(1, conn)):
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
            b"\x00\x50",
        ])
        handler.rfile = types.SimpleNamespace(read=lambda size: b"\x00\x50")

        with mock.patch.object(proxy, "create_conn", return_value=(1, conn)):
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
        global_var.session = session

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
        global_var.session = session

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
        global_var.session = session

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
            session.on_road_num = 0
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
        self.assertEqual(session.traffic_upload, 0)
        self.assertEqual(session.traffic_download, 0)

    def test_stale_response_after_generation_check_cannot_touch_new_session(self):
        session = self.make_session()
        session.get_data_lock = threading.Lock()
        session.on_road_num = 0
        session.target_on_roads = 0
        session.traffic_upload = 0
        session.traffic_download = 0
        session.server_time_deviation = 0
        session.server_time_offset = 0
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
                    session.target_on_roads = 7
                    session.server_time_offset = 123
                    session.server_time_deviation = 456
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
        self.assertEqual(session.target_on_roads, 7)
        self.assertEqual(session.server_time_offset, 123)
        self.assertEqual(session.server_time_deviation, 456)
        session.trigger_more.assert_not_called()

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

    def test_pipe_restart_before_old_conn_stop_unregisters_old_socket(self):
        session = types.SimpleNamespace(
            config=types.SimpleNamespace(show_debug=False),
            session_id=b"old-session", remove_conn=mock.Mock())
        logger = types.SimpleNamespace(
            debug=lambda *args: None, warn=lambda *args: None,
            exception=lambda *args: None)
        pipe = base.ConnectionPipe(session, logger)
        sock = socket.socket()
        conn = base.Conn.__new__(base.Conn)
        conn.session = session
        conn.conn_id = 1
        conn.host = b""
        conn.port = 0
        conn._fd = sock.fileno()
        conn.running = True
        conn.cmd_queue = {}
        conn.cmd_queue_bytes = 0
        conn.cmd_queue_since = None
        conn.sock = sock
        conn._lock = threading.Lock()
        conn.connection_pipe = pipe
        conn.xlog = logger
        pipe.select2.register_event(sock, base.selectors.EVENT_READ, conn)
        pipe.sock_conn_map[sock] = conn

        try:
            pipe.stop()
            pipe.start()
            conn.do_stop("stop-after-pipe-restart")

            registered = pipe.select2.get_map() or {}
            self.assertNotIn(sock, registered)
        finally:
            pipe.select2.close()
            if sock.fileno() != -1:
                sock.close()

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

        session.reset.assert_called_once_with(
            expected_session_id=b"test-session", expected_read_paused_since=10.0)

    def test_stale_upload_uses_captured_generation_and_preserves_new_inflight_count(self):
        session = self.make_session()
        session.session_id = b"old-session"
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
            "transfer_no": 1, "stat": "request",
            "start_time": proxy.time.time() - 1, "server_timeout": 30,
            "send_data": base.WriteBuffer(), "send_ack": base.WriteBuffer(),
            "download_timeout": base.WriteBuffer(),
            "request_session_id": b"old-session",
        }
        session.transfer_list = {1: data_info}
        session.get_upload_data = mock.Mock(return_value=data_info)
        payload = struct.pack("<cBBdIIIIIIII", b"P", 1, 2, proxy.time.time(),
                              0, 0, 0, 0, 0, 0, 0, 0)
        response = types.SimpleNamespace(
            headers={b"Content-Length": str(len(payload)).encode()},
            worker=types.SimpleNamespace(ip_str="test-front"))
        uploaded_session_ids = []
        original_lock = session.lock

        class SwapGenerationLock:
            def __init__(self):
                self.swapped = False

            def __enter__(self):
                original_lock.acquire()
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                original_lock.release()
                if not self.swapped:
                    self.swapped = True
                    session.session_id = b"new-session"
                    session.on_road_num = 7
                    session.transfer_list = {1: {"owner": "new-session"}}

        session.lock = SwapGenerationLock()

        def stale_response(**kwargs):
            uploaded_session_ids.append(bytes(kwargs["data"][3:11]))
            return payload, 200, response

        global_var.protocol_version = 1
        global_var.http_client = types.SimpleNamespace(request=stale_response)
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

        self.assertEqual(uploaded_session_ids, [b"old-sess"])
        self.assertEqual(session.on_road_num, 7)
        self.assertEqual(session.transfer_list, {1: {"owner": "new-session"}})

    def test_stale_521_and_quota_error_do_not_stop_or_relogin_new_generation(self):
        for status, payload in ((521, b""),
                                (200, struct.pack("<cBBBH", b"P", 1, 3, 1, 0))):
            with self.subTest(status=status, payload_len=len(payload)):
                session = self.make_session()
                session.session_id = b"old-session"
                session.get_data_lock = threading.Lock()
                session.on_road_num = 0
                session.target_on_roads = 0
                session.traffic_upload = 0
                session.traffic_download = 0
                session.server_time_deviation = 0
                session.stop = mock.Mock()
                data_info = {
                    "transfer_no": 1, "stat": "request",
                    "start_time": proxy.time.time() - 1, "server_timeout": 30,
                    "send_data": base.WriteBuffer(), "send_ack": base.WriteBuffer(),
                    "download_timeout": base.WriteBuffer(),
                    "request_session_id": b"old-session",
                }
                session.transfer_list = {1: data_info}
                session.get_upload_data = mock.Mock(return_value=data_info)
                response = types.SimpleNamespace(headers={b"Content-Length": str(len(payload)).encode()})

                class NewGenerationStats(dict):
                    def __getitem__(self, key):
                        if key == "roundtrip_num":
                            session.session_id = b"new-session"
                            global_var.server_host = "new-server"
                        return super().__getitem__(key)

                global_var.protocol_version = 1
                global_var.http_client = types.SimpleNamespace(
                    request=lambda **kwargs: (payload, status, response))
                global_var.server_host = "old-server"
                global_var.stat = NewGenerationStats(roundtrip_num=0, slow_roundtrip=0)
                global_var.quota = 100000
                global_var.config.roundtrip_timeout = 30
                global_var.config.network_timeout = 1
                global_var.config.server_time_max_deviation = 10000
                global_var.config.concurent_thread_num = 4
                global_var.config.min_on_road = 1
                with mock.patch.object(proxy, "encrypt_data", side_effect=lambda data: data), \
                        mock.patch.object(proxy, "decrypt_data", side_effect=lambda data: data), \
                        mock.patch.object(proxy, "login_process") as login:
                    session.roundtrip_task(0)

                session.stop.assert_not_called()
                login.assert_not_called()
                self.assertEqual(global_var.server_host, "new-server")

    def test_conditional_stop_and_reset_reject_stale_generation_and_conn(self):
        session = self.make_session()
        new_conn = types.SimpleNamespace()
        session.conn_list = {1: new_conn}
        session.stop = mock.Mock()
        session.start = mock.Mock(return_value=True)

        self.assertFalse(session.reset(expected_session_id=b"other-generation"))
        self.assertFalse(session.reset(expected_conn=types.SimpleNamespace(), conn_id=1))
        session.stop.assert_not_called()
        session.start.assert_not_called()

    def test_stale_checker_does_not_reset_new_generation(self):
        session = self.make_session()
        session.read_paused = True
        session.read_paused_since = 10.0
        session.reset = mock.Mock()
        global_var.config.session_backpressure_timeout = 60
        original_lock = session.lock

        class GenerationChangeLock:
            def __enter__(self):
                original_lock.acquire()
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                original_lock.release()
                session.session_id = b"new-session"

        session.lock = GenerationChangeLock()
        with mock.patch.object(proxy.time, "monotonic", return_value=71.0):
            session.check_backpressure_timeout()

        session.reset.assert_not_called()

    def test_conn_transfers_carry_identity_and_stale_conn_cannot_append(self):
        session = self.make_session()
        old_conn = base.Conn.__new__(base.Conn)
        old_conn.session = session
        old_conn.conn_id = 1
        old_conn.transferred_close_to_peer = False
        old_conn.next_recv_seq = 1
        old_conn.received_position = 0
        old_conn.remote_acked_position = 0
        old_conn.windows_size = 100
        old_conn.connection_pipe = Pipe()
        session.send_conn_data = mock.Mock()

        old_conn.transfer_received_data(b"old")
        old_conn.transfer_ack(3)
        old_conn.transfer_peer_close("old close")

        for call in session.send_conn_data.call_args_list:
            self.assertIs(call.kwargs.get("expected_conn"), old_conn)

        live_session = self.make_session()
        live_session.conn_list[1] = types.SimpleNamespace()
        live_session.update_backpressure = mock.Mock()
        live_session.send_conn_data(1, b"late", expected_conn=old_conn)
        self.assertEqual(live_session.send_buffer.pool_size, 0)
        self.assertEqual(live_session.buffered_bytes, 0)

    def test_stale_max_connection_close_does_not_reset_new_conn(self):
        session = self.make_session()
        session.conn_list = {1: types.SimpleNamespace()}
        session.stop = mock.Mock()
        session.start = mock.Mock(return_value=True)
        conn = base.Conn.__new__(base.Conn)
        conn.session = session
        conn.conn_id = 1
        conn.running = True
        conn.blocked = False
        conn.send_buffer = None
        conn.sock = None
        conn.is_client = True
        conn.cmd_queue = {1: base.ReadBuffer(struct.pack("<IB", 1, 2) + b"exceed the max connection")}
        conn.cmd_queue[1].get(4)
        conn.next_cmd_seq = 1
        conn._lock = threading.Lock()
        conn.xlog = types.SimpleNamespace(debug=lambda *args: None)
        conn.transfer_peer_close = mock.Mock()
        conn.connection_pipe = Pipe()
        conn.stop = mock.Mock(side_effect=lambda reason: setattr(conn, "running", False))

        conn.process_cmd()
        session.stop.assert_not_called()
        session.start.assert_not_called()

    def test_create_conn_does_not_update_new_generation_schedule(self):
        session = self.make_session()
        session.last_conn_id = -1
        session.conn_list = {}
        session.target_on_roads = 0
        session.trigger_more = mock.Mock()
        session.update_backpressure = mock.Mock()
        global_var.config.concurent_thread_num = 4
        global_var.config.min_on_road = 1
        global_var.config.windows_size = 100
        global_var.config.windows_ack = 50
        old_conn = types.SimpleNamespace(do_stop=mock.Mock())

        def replace_generation(conn_id, data, expected_conn=None):
            session.session_id = b"new-session"
            session.target_on_roads = 7
            session.conn_list = {}

        session.send_conn_data = replace_generation
        with mock.patch.object(proxy.base_container, "Conn", return_value=old_conn):
            result = session.create_conn(None, b"example.com", 443)

        self.assertIsNone(result)
        self.assertEqual(session.target_on_roads, 7)
        session.trigger_more.assert_not_called()
        old_conn.do_stop.assert_called_once_with("create_conn_stale")

    def test_create_conn_frame_cannot_cross_into_reused_conn_id(self):
        session = self.make_session()
        session.last_conn_id = -1
        session.conn_list = {}
        session.target_on_roads = 0
        session.trigger_more = mock.Mock()
        session.update_backpressure = mock.Mock()
        old_conn = types.SimpleNamespace(do_stop=mock.Mock())
        new_conn = types.SimpleNamespace()
        original_send = session.send_conn_data
        expected_conns = []
        global_var.config.concurent_thread_num = 4
        global_var.config.min_on_road = 1
        global_var.config.windows_size = 100
        global_var.config.windows_ack = 50

        def replace_before_send(conn_id, data, expected_conn=None):
            expected_conns.append(expected_conn)
            session.conn_list = {conn_id: new_conn}
            return original_send(conn_id, data, expected_conn=expected_conn)

        session.send_conn_data = replace_before_send
        with mock.patch.object(proxy.base_container, "Conn", return_value=old_conn):
            session.create_conn(None, b"example.com", 443)

        self.assertEqual(expected_conns, [old_conn])
        self.assertEqual(session.send_buffer.pool_size, 0)
        self.assertEqual(session.buffered_bytes, 0)

    def test_stale_conn_read_and_write_unregistration_preserves_new_mapping(self):
        for event_name in ("read", "write"):
            with self.subTest(event=event_name):
                session = types.SimpleNamespace(
                    config=types.SimpleNamespace(show_debug=False),
                    session_id=b"test-session",
                    send_conn_data=mock.Mock())
                logger = types.SimpleNamespace(
                    debug=lambda *args: None, warn=lambda *args: None,
                    exception=lambda *args: None)
                pipe = base.ConnectionPipe(session, logger)
                sock = object()
                new_conn = types.SimpleNamespace(conn_id=1)
                pipe.sock_conn_map = {sock: new_conn}
                pipe.select2 = types.SimpleNamespace(unregister_event=lambda sock, event: False)
                old_conn = base.Conn.__new__(base.Conn)
                old_conn.session = session
                old_conn.conn_id = 1
                old_conn.sock = sock
                old_conn.connection_pipe = pipe
                old_conn.xlog = logger

                if event_name == "read":
                    old_conn.transferred_close_to_peer = False
                    old_conn.next_recv_seq = 1
                    old_conn.received_position = 0
                    old_conn.remote_acked_position = 0
                    old_conn.windows_size = 0
                    old_conn.transfer_received_data(b"old")
                else:
                    old_conn.running = True
                    old_conn.blocked = False
                    old_conn.send_buffer = None
                    old_conn.cmd_queue = {}
                    old_conn.next_cmd_seq = 1
                    old_conn._lock = threading.Lock()
                    old_conn.process_cmd()

                self.assertIs(pipe.sock_conn_map.get(sock), new_conn)

    def test_pipe_drops_stale_read_and_write_events(self):
        for event_name in ("read", "write"):
            with self.subTest(event=event_name):
                session = types.SimpleNamespace(
                    config=types.SimpleNamespace(show_debug=False),
                    session_id=b"test", check_upload=lambda: False)
                logger = types.SimpleNamespace(
                    debug=lambda *args: None, warn=lambda *args: None,
                    exception=lambda *args: None)
                pipe = base.ConnectionPipe(session, logger)
                class FakeSock:
                    def __init__(self):
                        self.recv = mock.Mock(return_value=b"data")

                sock = FakeSock()
                old_conn = types.SimpleNamespace(
                    conn_id=1, transfer_received_data=mock.Mock(), process_cmd=mock.Mock())
                new_conn = types.SimpleNamespace(conn_id=1)
                pipe.sock_conn_map = {sock: new_conn}
                pipe.th = threading.current_thread()
                event = base.selectors.EVENT_READ if event_name == "read" else base.selectors.EVENT_WRITE

                class OneEventSelector:
                    def select(self, timeout):
                        pipe.running = False
                        return [(types.SimpleNamespace(fileobj=sock, data=old_conn), event)]

                    def unregister(self, target):
                        pass

                pipe.select2 = OneEventSelector()
                pipe.pipe_worker()

                old_conn.transfer_received_data.assert_not_called()
                old_conn.process_cmd.assert_not_called()
                sock.recv.assert_not_called()

    def test_pipe_register_failure_cleans_outside_lock_and_preserves_new_mapping(self):
        session = types.SimpleNamespace(config=types.SimpleNamespace(show_debug=False))
        logger = types.SimpleNamespace(
            debug=lambda *args: None, warn=lambda *args: None,
            exception=lambda *args: None)
        pipe = base.ConnectionPipe(session, logger)
        sock = object()
        old_conn = types.SimpleNamespace(
            conn_id=1, transfer_peer_close=mock.Mock(), do_stop=mock.Mock())
        new_conn = types.SimpleNamespace(
            conn_id=1, transfer_peer_close=mock.Mock(), do_stop=mock.Mock())
        pipe.sock_conn_map = {sock: new_conn}

        class FailingSelector:
            def register_event(self, target, event, conn):
                raise RuntimeError("injected register failure")

        pipe.select2 = FailingSelector()
        pipe.add_sock_event(sock, old_conn, base.selectors.EVENT_READ)

        self.assertIs(pipe.sock_conn_map.get(sock), new_conn)
        old_conn.do_stop.assert_not_called()
        new_conn.transfer_peer_close.assert_not_called()
        new_conn.do_stop.assert_not_called()

    def test_old_roundtrip_and_pipe_finally_preserve_replacement_threads(self):
        session = self.make_session()
        replacement = types.SimpleNamespace()
        session.round_trip_thread = {0: replacement}
        session.running = False
        session.normal_round_trip_worker(0)
        self.assertIs(session.round_trip_thread.get(0), replacement)

        pipe_session = types.SimpleNamespace(
            config=types.SimpleNamespace(show_debug=False), check_upload=lambda: False)
        logger = types.SimpleNamespace(debug=lambda *args: None, warn=lambda *args: None)
        pipe = base.ConnectionPipe(pipe_session, logger)
        pipe.running = False
        pipe.sock_conn_map = {object(): types.SimpleNamespace(conn_id=1)}
        pipe.th = replacement
        pipe.select2 = types.SimpleNamespace(unregister=lambda sock: None)
        pipe.pipe_worker()
        self.assertIs(pipe.th, replacement)
        self.assertEqual(len(pipe.sock_conn_map), 1)

    def test_upload_data_is_not_registered_after_stop_during_send_wait(self):
        session = self.make_session()
        session.transfer_list = {}
        empty = base.WriteBuffer()

        def stop_during_send(work_id, expected_session_id=None):
            session.running = False
            return empty, empty, empty

        session.get_send_data = mock.Mock(side_effect=stop_during_send)
        session.get_transfer_no = mock.Mock(return_value=1)
        global_var.config.roundtrip_timeout = 30

        self.assertIsNone(session.get_upload_data(0))
        self.assertEqual(session.transfer_list, {})

    def test_exiting_roundtrip_worker_installs_replacement_while_running(self):
        session = self.make_session()
        current = threading.current_thread()
        session.running = True
        session.round_trip_thread = {0: current}
        session.roundtrip_task = mock.Mock(side_effect=RuntimeError("injected worker exit"))
        replacements = []

        class ReplacementThread:
            def __init__(self, target, args, name):
                self.target = target
                self.args = args
                self.name = name

            def start(self):
                replacements.append(self)

        with mock.patch.object(proxy.threading, "Thread", ReplacementThread):
            with self.assertRaisesRegex(RuntimeError, "injected worker exit"):
                session.normal_round_trip_worker(0)

        self.assertEqual(len(replacements), 1)
        self.assertIs(session.round_trip_thread.get(0), replacements[0])
        self.assertEqual(replacements[0].args, (0,))

    def test_stale_timeout_checker_does_not_update_new_target_on_roads(self):
        session = self.make_session()
        session.running = True
        session.timeout_check_th = threading.current_thread()
        session.check_backpressure_timeout = mock.Mock()
        session.transfer_list = {
            1: {"stat": "request", "start_time": 0, "server_timeout": 0}}
        session.target_on_roads = 0
        session.trigger_more = mock.Mock()
        global_var.config.send_timeout_retry = 1
        global_var.config.concurent_thread_num = 4
        global_var.config.min_on_road = 1
        original_lock = session.lock

        class NewTargetAfterScanLock:
            def __init__(self):
                self.swapped = False

            def __enter__(self):
                original_lock.acquire()
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                original_lock.release()
                if not self.swapped:
                    self.swapped = True
                    session.session_id = b"new-session"
                    session.target_on_roads = 7

        session.lock = NewTargetAfterScanLock()
        with mock.patch.object(proxy.time, "time", return_value=10), \
                mock.patch.object(proxy.time, "sleep",
                                  side_effect=lambda seconds: setattr(session, "running", False)):
            session.timeout_checker()

        self.assertEqual(session.target_on_roads, 7)
        session.trigger_more.assert_not_called()

    def test_start_replaces_dead_roundtrip_worker_slot(self):
        global_var.config.enable_tls_relay = False
        global_var.config.upload_logs = False
        global_var.config.concurent_thread_num = 1
        global_var.config.roundtrip_timeout = 30
        session = proxy.ProxySession()
        session.timeout_check_th.join(1)
        old_worker = types.SimpleNamespace(is_alive=lambda: False)
        session.round_trip_thread = {0: old_worker}
        session.login_session = mock.Mock(return_value=True)
        global_var.session = session
        global_var.config.concurent_thread_num = 1
        try:
            self.assertTrue(session.start())
            self.assertIsNot(session.round_trip_thread[0], old_worker)
            self.assertTrue(session.round_trip_thread[0].is_alive())
        finally:
            session.stop()
            current_worker = session.round_trip_thread.get(0)
            if hasattr(current_worker, "join"):
                current_worker.join(2)
            session.timeout_check_th.join(2)

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

    def test_upload_data_from_old_generation_is_not_registered(self):
        session = self.make_session()
        session.transfer_list = {}
        session.last_transfer_no = 0
        old_data = base.WriteBuffer(b"OLD-A")
        empty = base.WriteBuffer()

        def change_generation(work_id, expected_session_id=None):
            session.session_id = b"new-session"
            return old_data, empty, empty

        session.get_send_data = mock.Mock(side_effect=change_generation)
        session.get_transfer_no = mock.Mock(return_value=1)
        global_var.config.roundtrip_timeout = 30

        self.assertIsNone(session.get_upload_data(0))
        self.assertEqual(session.transfer_list, {})
        session.get_transfer_no.assert_not_called()

    def test_upload_data_from_same_generation_is_registered(self):
        session = self.make_session()
        session.transfer_list = {}
        session.last_transfer_no = 0
        data = base.WriteBuffer(b"CURRENT")
        empty = base.WriteBuffer()
        session.get_send_data = mock.Mock(return_value=(data, empty, empty))
        session.get_transfer_no = mock.Mock(return_value=1)
        global_var.config.roundtrip_timeout = 30

        info = session.get_upload_data(0)

        self.assertIsNotNone(info)
        self.assertEqual(info["session_id"], b"test-session")
        self.assertIs(info["send_data"], data)
        self.assertIs(session.transfer_list[1], info)

    def test_caller_conn_credential_rejects_reused_id_for_start_and_stop(self):
        session = self.make_session()
        old_conn = RecordingConn()
        new_conn = RecordingConn()
        session.conn_list = {1: new_conn}

        self.assertFalse(session.start_conn(1, b"old-first-packet", expected_conn=old_conn))
        session.stop_conn(1, "delayed-old-stop", expected_conn=old_conn)

        self.assertEqual(new_conn.events, [])

    def test_first_register_failure_cleans_unmapped_conn(self):
        session = types.SimpleNamespace(config=types.SimpleNamespace(show_debug=False))
        logger = types.SimpleNamespace(
            debug=lambda *args: None, warn=lambda *args: None,
            exception=lambda *args: None)
        pipe = base.ConnectionPipe(session, logger)
        pipe.th = threading.current_thread()
        sock = object()
        conn = types.SimpleNamespace(
            conn_id=1, transfer_peer_close=mock.Mock(), do_stop=mock.Mock())
        pipe.select2 = types.SimpleNamespace(
            register_event=mock.Mock(side_effect=RuntimeError("first register failed")))

        pipe.add_sock_event(sock, conn, base.selectors.EVENT_READ)

        conn.transfer_peer_close.assert_called_once()
        conn.do_stop.assert_called_once()
        self.assertNotIn(sock, pipe.sock_conn_map)

    def test_real_conn_register_failure_never_deadlocks_in_cleanup(self):
        for operation in ("start", "put_cmd_data"):
            with self.subTest(operation=operation):
                left, right = socket.socketpair()
                self.addCleanup(right.close)
                self.addCleanup(left.close)
                logger = types.SimpleNamespace(
                    debug=lambda *args: None, warn=lambda *args: None,
                    exception=lambda *args: None)
                session = types.SimpleNamespace(
                    config=types.SimpleNamespace(show_debug=False, max_payload=256 * 1024),
                    session_id=b"test", read_paused=False,
                    send_conn_data=mock.Mock(), remove_conn=mock.Mock())
                pipe = base.ConnectionPipe(session, logger)
                session.connection_pipe = pipe
                pipe.th = threading.current_thread()
                pipe.select2 = types.SimpleNamespace(
                    register_event=mock.Mock(
                        side_effect=RuntimeError("injected register failure")))
                conn = base.Conn(
                    session, 1, left, b"example.com", 443, 100, 10, True, logger)
                if operation == "start":
                    conn.cmd_queue[1] = b"pending"
                    target = conn.start
                    args = (False,)
                else:
                    conn.read_started = True
                    target = conn.put_cmd_data
                    args = (base.ReadBuffer(struct.pack("<I", 1) + b"x"),)

                worker = threading.Thread(target=target, args=args, daemon=True)
                worker.start()
                worker.join(0.5)

                self.assertFalse(
                    worker.is_alive(),
                    "注册失败后的真实 do_stop 不得重入已持有的 Conn 普通锁")
                self.assertFalse(conn.running)
                self.assertIsNone(conn.sock)
                session.remove_conn.assert_called_once_with(1, expected_conn=conn)

    def test_late_connect_result_is_closed_after_conn_stop(self):
        session = self.make_session()
        conn = base.Conn.__new__(base.Conn)
        conn.session = session
        conn.conn_id = 1
        conn.host = b""
        conn.port = 0
        conn.sock = None
        conn._fd = -1
        conn.running = True
        conn.read_started = False
        conn.blocked = False
        conn.send_buffer = b""
        conn.next_cmd_seq = 0
        conn.cmd_queue = {
            0: base.ReadBuffer(struct.pack("<IBBH", 0, 0, 0, 0) + struct.pack("<H", 443))}
        conn._lock = threading.Lock()
        conn.connection_pipe = Pipe()
        conn.xlog = types.SimpleNamespace(debug=lambda *args: None, info=lambda *args: None)
        session.conn_list = {1: conn}
        session.update_backpressure = mock.Mock()
        late_sock = mock.Mock()

        def stop_while_connecting(host, port):
            conn.do_stop("stop-during-connect")
            return late_sock, True

        conn.do_connect = stop_while_connecting
        conn.process_cmd()

        self.assertIsNone(conn.sock)
        late_sock.close.assert_called_once_with()
        session.update_backpressure.assert_not_called()

    def test_reset_serializes_registration_without_losing_new_mapping(self):
        session = types.SimpleNamespace(config=types.SimpleNamespace(show_debug=False))
        logger = types.SimpleNamespace(
            debug=lambda *args: None, warn=lambda *args: None,
            exception=lambda *args: None)
        pipe = base.ConnectionPipe(session, logger)
        pipe.th = threading.current_thread()
        old_sock = object()
        new_sock = object()
        old_conn = types.SimpleNamespace(
            conn_id=1, transfer_peer_close=mock.Mock(), do_stop=mock.Mock())
        new_conn = types.SimpleNamespace(
            conn_id=2, transfer_peer_close=mock.Mock(), do_stop=mock.Mock())
        pipe.sock_conn_map = {old_sock: old_conn}
        old_selector = types.SimpleNamespace(
            unregister=lambda target: None,
            unregister_event=lambda target, event: True)
        new_selector = types.SimpleNamespace(register_event=mock.Mock())
        pipe.select2 = old_selector
        close_started = threading.Event()
        allow_close = threading.Event()
        original_close = pipe.close_sock

        def gated_close(sock, reason, expected_conn=None):
            close_started.set()
            allow_close.wait(1)
            return original_close(sock, reason, expected_conn=expected_conn)

        pipe.close_sock = gated_close
        reset_thread = threading.Thread(target=pipe.reset_all_connections)
        add_done = threading.Event()
        with mock.patch.object(base.selectors, "DefaultSelector", return_value=new_selector):
            reset_thread.start()
            self.assertTrue(close_started.wait(1))
            adder = threading.Thread(target=lambda: (
                pipe.add_sock_event(new_sock, new_conn, base.selectors.EVENT_READ),
                add_done.set()))
            adder.start()
            add_done.wait(0.1)
            allow_close.set()
            reset_thread.join(2)
            adder.join(2)

        self.assertFalse(reset_thread.is_alive())
        self.assertFalse(adder.is_alive())
        self.assertIs(pipe.sock_conn_map.get(new_sock), new_conn)
        new_selector.register_event.assert_called_once_with(
            new_sock, base.selectors.EVENT_READ, new_conn)

    def test_ack_recovery_between_checker_locks_prevents_reset(self):
        session = self.make_session()
        session.read_paused = True
        session.read_paused_since = 10.0
        session.reset = mock.Mock()
        global_var.config.session_backpressure_timeout = 60
        original_lock = session.lock

        class AckRecoveryLock:
            def __init__(self):
                self.entries = 0

            def __enter__(self):
                original_lock.acquire()
                self.entries += 1
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                original_lock.release()
                if self.entries == 1:
                    session.read_paused = False
                    session.read_paused_since = None

        session.lock = AckRecoveryLock()
        with mock.patch.object(proxy.time, "monotonic", return_value=71.0):
            session.check_backpressure_timeout()

        session.reset.assert_not_called()

    def test_old_login_cannot_start_replaced_or_stopped_session(self):
        for change in ("replace", "stop"):
            with self.subTest(change=change):
                old_session = types.SimpleNamespace(
                    last_send_time=proxy.time.time(),
                    running=False,
                    start=mock.Mock(return_value=True),
                    stop=mock.Mock(),
                )
                new_session = types.SimpleNamespace()
                global_var.session = old_session
                global_var.running = True
                global_var.server_host = None
                global_var.tls_relay_front = None
                global_var.seley_front = None
                global_var.config.login_account = "account"
                global_var.config.login_password = "password"
                started = threading.Event()
                release = threading.Event()

                def blocked_login(*args):
                    started.set()
                    release.wait(2)
                    return True, "success"

                with mock.patch.object(proxy, "request_balance", side_effect=blocked_login):
                    worker = threading.Thread(target=proxy.login_process)
                    worker.start()
                    self.assertTrue(started.wait(1))
                    if change == "replace":
                        global_var.session = new_session
                    else:
                        global_var.running = False
                    release.set()
                    worker.join(2)

                self.assertFalse(worker.is_alive())
                old_session.start.assert_not_called()
                old_session.stop.assert_not_called()

    def test_two_roundtrips_can_wait_for_network_concurrently(self):
        session = self.make_session()
        session.get_data_lock = threading.Lock()
        session.on_road_num = 0
        session.target_on_roads = 2
        session.traffic_upload = 0
        session.traffic_download = 0
        session.server_time_deviation = 0
        session.server_time_offset = 0
        session.last_receive_time = 0
        session.round_trip_process = mock.Mock()
        session.check_upload_not_acked = mock.Mock()
        session.trigger_more = mock.Mock()
        infos = []
        for transfer_no in (1, 2):
            info = {
                "transfer_no": transfer_no,
                "stat": "request",
                "start_time": proxy.time.time(),
                "server_timeout": 30,
                "send_data": base.WriteBuffer(),
                "send_ack": base.WriteBuffer(),
                "download_timeout": base.WriteBuffer(),
                "request_session_id": session.session_id,
            }
            infos.append(info)
        session.transfer_list = {info["transfer_no"]: info for info in infos}
        session.get_upload_data = mock.Mock(side_effect=infos)

        global_var.protocol_version = 1
        payload = struct.pack(
            "<cBBdIIIIIIII", b"P", global_var.protocol_version, 2,
            proxy.time.time(), 0, 0, 0, 0, 0, 0, 0, 0)
        response = types.SimpleNamespace(
            headers={b"Content-Length": str(len(payload)).encode()},
            worker=types.SimpleNamespace(ip_str="test-front"))
        rendezvous = threading.Barrier(2)
        errors = []

        def request(**kwargs):
            try:
                rendezvous.wait(timeout=1)
            except Exception as caught:
                errors.append(caught)
                raise
            return payload, 200, response

        global_var.http_client = types.SimpleNamespace(request=request)
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
            workers = [threading.Thread(target=session.roundtrip_task, args=(work_id,))
                       for work_id in (0, 1)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(3)

        self.assertEqual(errors, [])
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(session.transfer_list, {})
        self.assertEqual(session.on_road_num, 0)
        self.assertEqual(session.round_trip_process.call_count, 2)

    def test_reset_callbacks_and_conn_start_do_not_invert_pipe_session_locks(self):
        session = self.make_session()
        logger = types.SimpleNamespace(
            debug=lambda *args: None, warn=lambda *args: None,
            exception=lambda *args: None)
        pipe = base.ConnectionPipe(session, logger)
        session.connection_pipe = pipe
        close_entered = threading.Event()
        allow_old_session_lock = threading.Event()
        start_entered = threading.Event()

        old_conn = types.SimpleNamespace(conn_id=1, do_stop=mock.Mock())

        def close_old(reason):
            close_entered.set()
            allow_old_session_lock.wait(1)
            with session.lock:
                pass

        old_conn.transfer_peer_close = close_old
        new_conn = types.SimpleNamespace(conn_id=2)

        def start_new(block):
            start_entered.set()
            with pipe._lock:
                pass

        new_conn.start = start_new
        session.conn_list = {2: new_conn}
        sock = object()
        pipe.sock_conn_map = {sock: old_conn}
        pipe.select2 = mock.Mock()
        reset_thread = threading.Thread(target=pipe.reset_all_connections, daemon=True)
        start_thread = threading.Thread(
            target=session.start_conn, args=(2,), kwargs={"expected_conn": new_conn}, daemon=True)

        reset_thread.start()
        self.assertTrue(close_entered.wait(1))
        start_thread.start()
        self.assertTrue(start_entered.wait(1))
        allow_old_session_lock.set()
        reset_thread.join(1)
        start_thread.join(1)

        self.assertFalse(reset_thread.is_alive())
        self.assertFalse(start_thread.is_alive())

    def test_old_upload_worker_cannot_consume_new_generation_send_buffer(self):
        session = self.make_session()
        session.session_id = b"old-session"
        session.transfer_list = {}
        session.last_transfer_no = 0
        session.oldest_received_time = 0
        session.send_delay = 0
        started = threading.Event()
        release = threading.Event()
        empty = base.WriteBuffer()

        def gated_get_send_data(work_id, expected_session_id=None):
            started.set()
            release.wait(1)
            if expected_session_id is None:
                data = session.get_data(work_id)
            else:
                data = session.get_data(work_id, expected_session_id=expected_session_id)
            return data, empty, empty

        session.get_send_data = gated_get_send_data
        global_var.config.roundtrip_timeout = 30
        worker = threading.Thread(target=session.get_upload_data, args=(0,))
        worker.start()
        self.assertTrue(started.wait(1))
        session.session_id = b"new-session"
        session.send_buffer.put(base.WriteBuffer(b"NEW-B"))
        release.set()
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(session.send_buffer.pool_size, len(b"NEW-B"))
        self.assertEqual(session.wait_ack_send_list, {})

    def test_module_stop_during_internal_login_cannot_finish_session_start(self):
        session = proxy.ProxySession.__new__(proxy.ProxySession)
        session.lock = threading.RLock()
        session.running = False
        session.transfer_log_throttle = types.SimpleNamespace(
            reset=lambda: None, flush=lambda: (0, 0))
        session.wait_queue = types.SimpleNamespace(running=True)
        session.round_trip_thread = {}
        session.connection_pipe = mock.Mock()
        global_var.config.concurent_thread_num = 0
        global_var.session = session
        global_var.running = True

        class NoWorkerThread:
            def __init__(self, *args, **kwargs):
                self.started = False

            def start(self):
                self.started = True

            def is_alive(self):
                return False

        def login_and_stop():
            global_var.running = False
            global_var.session = None
            session.stop()
            return True

        session.login_session = login_and_stop
        with mock.patch.object(proxy.threading, "Thread", NoWorkerThread):
            result = session.start()

        self.assertFalse(result)
        self.assertFalse(session.running)
        session.connection_pipe.start.assert_not_called()

    def test_ack_recovery_after_final_checker_lock_prevents_reset(self):
        session = self.make_session()
        session.read_paused = True
        session.read_paused_since = 10.0
        session.stop = mock.Mock(return_value=True)
        session.start = mock.Mock(return_value=True)
        original_reset = session.reset

        def reset_after_ack(*args, **kwargs):
            session.read_paused = False
            session.read_paused_since = None
            return original_reset(*args, **kwargs)

        session.reset = reset_after_ack
        global_var.config.session_backpressure_timeout = 60
        with mock.patch.object(proxy.time, "monotonic", return_value=71.0):
            session.check_backpressure_timeout()

        session.stop.assert_not_called()
        session.start.assert_not_called()

    def make_command_conn(self, sock=None, is_client=True, windows_size=10):
        session = self.make_session()
        logger = types.SimpleNamespace(
            debug=lambda *args: None, warn=lambda *args: None,
            info=lambda *args: None, error=lambda *args: None)
        session.connection_pipe = base.ConnectionPipe(session, logger)
        conn = base.Conn(session, 2, sock, b"", 0, windows_size, 5, is_client, logger)
        session.conn_list = {2: conn}
        return conn

    def test_in_order_command_without_socket_does_not_deadlock(self):
        conn = self.make_command_conn(sock=None, is_client=False)
        returned = threading.Event()
        worker = threading.Thread(
            target=lambda: (conn.put_cmd_data(
                base.ReadBuffer(struct.pack("<IBQ", 0, 3, 7))), returned.set()),
            daemon=True)
        worker.start()
        worker.join(0.3)

        self.assertFalse(worker.is_alive())
        self.assertTrue(returned.is_set())
        self.assertEqual(conn.remote_acked_position, 7)
        self.assertEqual(conn.cmd_queue, {})

    def test_stop_rejects_command_that_passed_initial_running_check(self):
        sock = mock.Mock()
        sock.fileno.return_value = 12
        conn = self.make_command_conn(sock=sock)
        entered = threading.Event()
        release = threading.Event()

        class GatedBuffer(base.ReadBuffer):
            def get(self, size=None):
                entered.set()
                release.wait(1)
                return super().get(size)

        worker = threading.Thread(target=lambda: conn.put_cmd_data(
            GatedBuffer(struct.pack("<IBQ", 1, 3, 7))))
        worker.start()
        self.assertTrue(entered.wait(1))
        conn.do_stop("test stop")
        release.set()
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertFalse(conn.running)
        self.assertEqual(conn.cmd_queue, {})

    def test_future_command_queue_has_a_byte_limit(self):
        sock = mock.Mock()
        sock.fileno.return_value = 12
        conn = self.make_command_conn(sock=sock)
        payload = struct.pack("<IB", 2, 1) + b"x" * (conn.max_cmd_queue_bytes + 1)

        conn.put_cmd_data(base.ReadBuffer(payload))

        self.assertFalse(conn.running)
        self.assertEqual(conn.cmd_queue, {})

    def test_middle_command_gap_allows_protocol_window_before_retry(self):
        sock = mock.Mock()
        sock.fileno.return_value = 12
        sent = bytearray()
        sock.send.side_effect = lambda data: sent.extend(data) or len(data)
        global_var.config.max_payload = 256 * 1024
        conn = self.make_command_conn(sock=sock, windows_size=10 * 1024 * 1024)
        first = b"a" * 15453
        missing = b"b" * 65536
        later = [bytes([seq]) * (256 * 1024) for seq in range(3, 8)]

        conn.put_cmd_data(base.ReadBuffer(struct.pack("<IB", 1, 1) + first))
        conn.process_cmd()
        for seq, payload in zip(range(3, 8), later):
            conn.put_cmd_data(base.ReadBuffer(struct.pack("<IB", seq, 1) + payload))

        self.assertTrue(conn.running)
        self.assertGreater(conn.cmd_queue_bytes, 1024 * 1024)
        self.assertLessEqual(conn.cmd_queue_bytes, conn.windows_size)
        self.assertEqual(bytes(sent), first)
        self.assertEqual(conn.sended_position, len(first))

        conn.put_cmd_data(base.ReadBuffer(struct.pack("<IB", 2, 1) + missing))
        conn.process_cmd()

        self.assertTrue(conn.running)
        self.assertEqual(conn.cmd_queue, {})
        self.assertEqual(bytes(sent), first + missing + b"".join(later))
        self.assertEqual(conn.sended_position, len(first) + len(missing) + sum(map(len, later)))

    def test_first_command_gap_allows_protocol_window_before_retry(self):
        sock = mock.Mock()
        sock.fileno.return_value = 12
        sent = bytearray()
        sock.send.side_effect = lambda data: sent.extend(data) or len(data)
        global_var.config.max_payload = 256 * 1024
        conn = self.make_command_conn(sock=sock, windows_size=10 * 1024 * 1024)
        first = b"a" * 15453
        later = [bytes([seq]) * (256 * 1024) for seq in range(2, 7)]

        for seq, payload in zip(range(2, 7), later):
            conn.put_cmd_data(base.ReadBuffer(struct.pack("<IB", seq, 1) + payload))

        self.assertTrue(conn.running)
        self.assertGreater(conn.cmd_queue_bytes, 1024 * 1024)
        self.assertLessEqual(conn.cmd_queue_bytes, conn.windows_size)
        self.assertEqual(bytes(sent), b"")

        conn.put_cmd_data(base.ReadBuffer(struct.pack("<IB", 1, 1) + first))
        conn.process_cmd()

        self.assertTrue(conn.running)
        self.assertEqual(conn.cmd_queue, {})
        self.assertEqual(bytes(sent), first + b"".join(later))
        self.assertEqual(conn.sended_position, len(first) + sum(map(len, later)))

    def test_tail_command_gap_recovers_ordered_data_then_eof(self):
        local, peer = socket.socketpair()
        peer.settimeout(1)
        self.addCleanup(peer.close)
        conn = self.make_command_conn(sock=local)
        conn.stop = lambda reason="": conn.do_stop(reason or "peer close")
        first = b"first"
        second = b"second"
        missing = b"missing"
        close_payload = struct.pack("<IB", 4, 2) + b"finish"

        conn.put_cmd_data(base.ReadBuffer(struct.pack("<IB", 1, 1) + first))
        conn.process_cmd()
        self.assertEqual(peer.recv(1024), first)
        conn.put_cmd_data(base.ReadBuffer(struct.pack("<IB", 2, 1) + second))
        conn.process_cmd()
        self.assertEqual(peer.recv(1024), second)
        conn.put_cmd_data(base.ReadBuffer(close_payload))
        self.assertTrue(conn.running)

        conn.put_cmd_data(base.ReadBuffer(struct.pack("<IB", 3, 1) + missing))
        conn.process_cmd()

        self.assertEqual(peer.recv(1024), missing)
        self.assertEqual(peer.recv(1024), b"")
        self.assertFalse(conn.running)
        self.assertEqual(conn.cmd_queue, {})

    def test_multiple_conns_recover_legal_gaps_concurrently(self):
        first = self.make_command_conn(sock=mock.Mock(), windows_size=10 * 1024 * 1024)
        second_sock = mock.Mock()
        second = base.Conn(
            first.session, 3, second_sock, b"", 0, 10 * 1024 * 1024, 5,
            True, first.xlog)
        first.session.conn_list = {2: first, 3: second}
        conns = (first, second)
        sent = {id(conn): bytearray() for conn in conns}
        for conn in conns:
            conn.sock.send.side_effect = lambda data, conn=conn: sent[id(conn)].extend(data) or len(data)
        barrier = threading.Barrier(3)
        failures = []

        def exercise(conn):
            try:
                later = [bytes([seq]) * (256 * 1024) for seq in range(2, 7)]
                for seq, payload in zip(range(2, 7), later):
                    conn.put_cmd_data(base.ReadBuffer(struct.pack("<IB", seq, 1) + payload))
                barrier.wait(1)
                if not conn.running or conn.cmd_queue_bytes <= 1024 * 1024:
                    raise AssertionError("合法并发缺口被提前关闭或未达到量化向量")
                conn.put_cmd_data(base.ReadBuffer(struct.pack("<IB", 1, 1) + b"first"))
                conn.process_cmd()
                if bytes(sent[id(conn)]) != b"first" + b"".join(later):
                    raise AssertionError("并发补包后未按序完整交付")
            except Exception as exc:
                failures.append(exc)

        workers = [threading.Thread(target=exercise, args=(conn,)) for conn in conns]
        for worker in workers:
            worker.start()
        barrier.wait(2)
        for worker in workers:
            worker.join(2)

        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(failures, [])
        for conn in conns:
            self.assertTrue(conn.running)
            self.assertEqual(conn.cmd_queue, {})

    def test_permanent_command_gap_expires_without_another_packet(self):
        sock = mock.Mock()
        sock.fileno.return_value = 12
        conn = self.make_command_conn(sock=sock)
        with mock.patch.object(base.time, "monotonic", return_value=100.0):
            conn.put_cmd_data(base.ReadBuffer(struct.pack("<IB", 2, 1) + b"x"))

        with mock.patch.object(base.time, "monotonic", return_value=131.0):
            check = getattr(conn, "check_cmd_queue", None)
            if check is None:
                self.fail("永久命令缺口没有主动期限检查")
            check()

        self.assertFalse(conn.running)
        self.assertEqual(conn.cmd_queue, {})

    def test_old_worker_does_not_consume_new_download_retry_budget(self):
        session = self.make_session()
        session.session_id = b"old-session"
        session.receive_process = base.BlockReceivePool(
            lambda data: None, types.SimpleNamespace(warn=lambda *args: None))
        session.receive_process.mark_sn_timeout(7, t=0.0, server_time=10.0)
        session.server_time_offset = 0
        session.server_time_deviation = 0
        session.on_road_num = 0
        session.target_on_roads = 0
        session.last_receive_time = 0
        session.last_send_time = 0
        session.ack_delay = 0
        global_var.config.server_time_max_deviation = 10000
        global_var.config.server_download_timeout_retry = 5

        def swap_after_get_data(work_id, expected_session_id=None):
            session.session_id = b"new-session"
            return base.WriteBuffer(b"D")

        session.get_data = swap_after_get_data
        data, ack, download_timeout = session.get_send_data(
            0, expected_session_id=b"old-session")

        self.assertNotIn(
            "retry_time", session.receive_process.timeout_sn_list[7])
        self.assertEqual(len(download_timeout), 0)

    def test_encrypt_failure_releases_registered_on_road(self):
        session = self.make_session()
        session.get_data_lock = threading.Lock()
        session.on_road_num = 0
        session.target_on_roads = 0
        session.server_time_deviation = 0
        session.wait_queue = mock.Mock()
        data_info = {
            "transfer_no": 1, "stat": "request",
            "start_time": proxy.time.time(), "server_timeout": 30,
            "send_data": base.WriteBuffer(), "send_ack": base.WriteBuffer(),
            "download_timeout": base.WriteBuffer(),
            "request_session_id": session.session_id,
        }
        session.transfer_list = {1: data_info}
        session.get_upload_data = mock.Mock(return_value=data_info)
        global_var.protocol_version = 1
        global_var.config.roundtrip_timeout = 30
        global_var.config.concurent_thread_num = 4
        global_var.config.min_on_road = 1
        global_var.config.server_time_max_deviation = 10000

        with mock.patch.object(proxy, "encrypt_data", side_effect=RuntimeError("encrypt failed")):
            try:
                session.roundtrip_task(0)
            except RuntimeError:
                pass

        self.assertEqual(session.on_road_num, 0)
        self.assertEqual(data_info["stat"], "timeout")

    def test_worker_start_failure_rolls_back_and_allows_retry(self):
        global_var.config.enable_tls_relay = False
        global_var.config.upload_logs = False
        global_var.config.concurent_thread_num = 1
        session = proxy.ProxySession()
        session.timeout_check_th.join(1)
        session.login_session = mock.Mock(return_value=True)
        global_var.session = session
        global_var.running = True

        class ControlledThread:
            def __init__(self, target=None, args=(), name=None, **kwargs):
                self.target = target
                self.args = args
                self.name = name or ""

            def start(self):
                if self.name.startswith("roundtrip"):
                    raise RuntimeError("thread start failed")

            def is_alive(self):
                return False

            def join(self, timeout=None):
                return None

        try:
            with mock.patch.object(proxy.threading, "Thread", ControlledThread):
                try:
                    result = session.start()
                except RuntimeError as caught:
                    self.fail("启动失败未回滚为可重试状态: %r" % caught)

            self.assertFalse(result)
            self.assertFalse(session.running)
            self.assertFalse(session.wait_queue.running)

            global_var.config.concurent_thread_num = 0
            self.assertTrue(session.start())
            self.assertTrue(session.running)
        finally:
            if session.running:
                session.stop()
            session.timeout_check_th.join(2)

    def test_small_buffer_flush_uses_monotonic_time(self):
        session = self.make_session()
        session.send_buffer.put(base.WriteBuffer(b"x"))
        session.oldest_received_time = 100.0
        session.send_delay = 0.5

        with mock.patch.object(proxy.time, "time", return_value=99.0), \
                mock.patch.object(proxy.time, "monotonic", return_value=101.0):
            data = session.get_data(0)

        self.assertGreater(len(data), 0)
        self.assertEqual(session.send_buffer.pool_size, 0)

    def test_reporter_exits_on_object_replacement_but_survives_same_object_reset(self):
        global_var.config.report_interval = 1
        for replace_object in (True, False):
            with self.subTest(replace_object=replace_object):
                session = self.make_session()
                global_var.session = session
                global_var.running = True
                calls = []

                def check_status():
                    calls.append(1)
                    if len(calls) == 1:
                        if replace_object:
                            global_var.session = self.make_session()
                        else:
                            session.session_id = b"new-session"
                    else:
                        global_var.running = False

                session.check_report_status = check_status
                with mock.patch.object(proxy, "sleep", return_value=None):
                    session.reporter()

                self.assertEqual(len(calls), 1 if replace_object else 2)

    def test_quota_loop_exits_when_session_object_is_replaced(self):
        session = self.make_session()
        global_var.session = session
        global_var.running = True
        global_var.quota = 1
        global_var.config.login_account = "test"
        global_var.config.login_password = "test"
        calls = []

        def replace_session(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                global_var.session = self.make_session()
            else:
                global_var.running = False

        with mock.patch.object(proxy, "request_balance", side_effect=replace_session), \
                mock.patch.object(proxy.time, "sleep", return_value=None):
            proxy.update_quota_loop()

        self.assertEqual(len(calls), 1)

    def test_stale_login_response_does_not_commit_to_new_generation(self):
        global_var.config.max_payload = 16
        global_var.config.send_delay = 0
        global_var.config.windows_size = 10
        global_var.config.windows_ack = 5
        global_var.config.resend_timeout = 60000
        global_var.config.ack_delay = 0
        global_var.config.login_account = "test"
        global_var.config.login_password = "test"
        global_var.config.network_timeout = 1
        global_var.protocol_version = 1
        global_var.xxnet_version = "test"
        global_var.system = "test"
        global_var.client_uuid = "test"

        for status in (521, 200):
            with self.subTest(status=status):
                session = self.make_session()
                session.get_login_extra_info = lambda: "{}"
                new_session = types.SimpleNamespace(session_id=b"new-session")
                new_http = mock.Mock()
                global_var.session = session
                global_var.server_host = "old-server"
                content = struct.pack("<cBBBH", b"P", 1, 1, 0, 0)

                def stale_response(**kwargs):
                    global_var.session = new_session
                    global_var.http_client = new_http
                    global_var.server_host = "new-server"
                    global_var.last_api_error = "new-error"
                    return content, status, None

                global_var.http_client = types.SimpleNamespace(request=stale_response)
                with mock.patch.object(proxy, "encrypt_data", side_effect=lambda data: data), \
                        mock.patch.object(proxy, "decrypt_data", side_effect=lambda data: data):
                    result = session.login_session()

                self.assertFalse(result)
                self.assertEqual(global_var.server_host, "new-server")
                self.assertEqual(global_var.last_api_error, "new-error")
                new_http.set_session_host.assert_not_called()

    def test_stale_api_response_does_not_replace_new_generation_error(self):
        session = self.make_session()
        global_var.session = session
        global_var.config.api_server = "api.example"
        global_var.last_api_error = "old-error"

        def stale_response(**kwargs):
            global_var.session = self.make_session()
            global_var.last_api_error = "new-error"
            return b'{"res":"success"}', 200, None

        global_var.http_client = types.SimpleNamespace(request=stale_response)
        with mock.patch.object(proxy, "encrypt_data", side_effect=lambda data: data), \
                mock.patch.object(proxy, "decrypt_data", side_effect=lambda data: data):
            result, info = proxy.call_api("/login", {})

        self.assertFalse(result)
        self.assertEqual(global_var.last_api_error, "new-error")

    def test_stale_balance_response_does_not_replace_new_quota_or_server(self):
        session = self.make_session()
        global_var.session = session
        global_var.quota = 100
        global_var.server_host = "old-server"
        global_var.tls_relay_front = None
        global_var.seley_front = None
        global_var.config.api_server = "api.example"
        global_var.config.server_host = ""
        global_var.config.update_cloudflare_domains = False
        global_var.xxnet_version = "test"
        global_var.system = "test"
        info = {
            "quota_list": {"current": {
                "end_time": int(proxy.time.time()) + 1000, "quota": 123}},
            "paypal_button_id": "test", "plans": [], "host": "old-response",
            "port": 443, "selectable": False, "promote_code": "",
            "promoter": "", "balance": 1, "openai_balance": 0,
            "openai_proxies": [], "tls_relays": {"ips": []},
        }

        def stale_balance(*args, **kwargs):
            global_var.session = self.make_session()
            global_var.quota = 7777
            global_var.server_host = "new-server"
            return True, info

        with mock.patch.object(proxy, "get_app_name", return_value="test"), \
                mock.patch.object(proxy, "call_api", side_effect=stale_balance):
            result, reason = proxy.request_balance("test", "test")

        self.assertFalse(result)
        self.assertEqual(global_var.quota, 7777)
        self.assertEqual(global_var.server_host, "new-server")
    def test_same_generation_balance_requests_cannot_commit_out_of_order(self):
        session = self.make_session()
        global_var.session = session
        global_var.tls_relay_front = None
        global_var.seley_front = None
        global_var.config.api_server = "api.example"
        global_var.config.server_host = ""
        global_var.config.update_cloudflare_domains = False
        global_var.xxnet_version = "test"
        global_var.system = "test"
        global_var.balance = 0
        global_var.server_host = ""
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        allow_second_return = threading.Event()
        first_done = threading.Event()
        second_done = threading.Event()
        results = {}

        def response(marker):
            return {
                "quota_list": {"current": {
                    "end_time": int(proxy.time.time()) + 1000, "quota": 123}},
                "paypal_button_id": "test", "plans": [], "host": str(marker) + "-server",
                "port": 443, "selectable": False, "promote_code": "",
                "promoter": "", "balance": marker, "openai_balance": 0,
                "openai_proxies": [], "tls_relays": {"ips": []},
            }

        def controlled_api(path, req_info):
            marker = req_info["account"]
            if marker == "first":
                first_entered.set()
                release_first.wait(2)
                return True, response(1)
            second_entered.set()
            allow_second_return.wait(2)
            return True, response(2)

        def run(marker, done):
            try:
                results[marker] = proxy.request_balance(marker, "test")
            finally:
                done.set()

        first = threading.Thread(target=run, args=("first", first_done))
        second = threading.Thread(target=run, args=("second", second_done))
        api_patch = mock.patch.object(proxy, "call_api", side_effect=controlled_api)
        api_patch.start()
        try:
            first.start()
            self.assertTrue(first_entered.wait(1))
            second.start()
            second_entered.wait(0.2)
            allow_second_return.set()
            second_done.wait(0.2)
            release_first.set()
        finally:
            allow_second_return.set()
            release_first.set()
            if first.ident is not None:
                first.join(2)
            if second.ident is not None:
                second.join(2)
            api_patch.stop()

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(results["first"][0], True)
        self.assertEqual(results["second"][0], True)
        self.assertEqual(global_var.balance, 2)
        self.assertEqual(global_var.server_host, "2-server")

    def test_report_response_cannot_cross_real_client_stop(self):
        old_session = self.make_session()
        old_session.is_idle = lambda: False
        old_session.last_state = {"timeout": 0}
        old_session.get_stat = lambda _period: {"global": {}, "tls_relay_front": {}}
        old_session.stop = lambda: None
        new_session = types.SimpleNamespace(session_id=b"new-session")
        old_front = types.SimpleNamespace(
            ip_manager=types.SimpleNamespace(ip_dict={"old": {"fail_times": 1}}),
            set_ips=lambda ips: None,
        )
        new_front = types.SimpleNamespace(
            ip_manager=types.SimpleNamespace(ip_dict={}), ips_calls=[])
        new_front.set_ips = lambda ips: new_front.ips_calls.append(ips)
        global_var.session = old_session
        global_var.tls_relay_front = old_front
        global_var.socks5_server = None
        global_var.http_client = types.SimpleNamespace(stop=lambda: None)
        global_var.config.api_server = "api.example"
        global_var.config.login_account = "test"
        global_var.config.login_password = "test"
        global_var.stat = {"timeout_roundtrip": 1}
        global_var.xxnet_version = "test"
        global_var.client_uuid = "test"
        response_entered = threading.Event()
        release_response = threading.Event()
        report_done = threading.Event()
        stop_done = threading.Event()
        errors = []

        class GatedInfo(dict):
            def __getitem__(self, key):
                if key == "data":
                    response_entered.set()
                    if not release_response.wait(2):
                        raise RuntimeError("test gate did not open")
                return super().__getitem__(key)

        client_path = APP / "x_tunnel" / "local" / "client.py"
        tree = ast.parse(client_path.read_text(), filename=str(client_path))
        stop_node = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "stop")
        stop_module = ast.fix_missing_locations(ast.Module(body=[stop_node], type_ignores=[]))
        stop_env = {
            "g": global_var,
            "proxy_session": proxy,
            "front_dispatcher": types.SimpleNamespace(stop=lambda: None),
            "xlog": types.SimpleNamespace(info=lambda *args, **kwargs: None),
        }
        exec(compile(stop_module, str(client_path), "exec"), stop_env)

        def run_report():
            try:
                old_session.check_report_status()
            except Exception as error:
                errors.append(error)
            finally:
                report_done.set()

        def run_stop():
            try:
                stop_env["stop"]()
            except Exception as error:
                errors.append(error)
            finally:
                stop_done.set()

        with mock.patch.object(proxy, "call_api", return_value=(True, GatedInfo(data={"ips": ["old-ip"]}))), \
                mock.patch.object(proxy.check_local_network.IPv6, "is_ok", return_value=False):
            report_thread = threading.Thread(target=run_report)
            stop_thread = threading.Thread(target=run_stop)
            report_thread.start()
            self.assertTrue(response_entered.wait(1))
            stop_thread.start()
            try:
                self.assertTrue(stop_done.wait(1))
                global_var.session = new_session
                global_var.tls_relay_front = new_front
                global_var.running = True
            finally:
                release_response.set()
            report_thread.join(2)
            stop_thread.join(2)

        self.assertTrue(report_done.is_set())
        self.assertFalse(report_thread.is_alive())
        self.assertFalse(stop_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(new_front.ips_calls, [])

    def test_report_response_commits_to_current_generation(self):
        session = self.make_session()
        session.is_idle = lambda: False
        session.last_state = {"timeout": 0}
        session.get_stat = lambda _period: {"global": {}, "tls_relay_front": {}}
        front = types.SimpleNamespace(
            ip_manager=types.SimpleNamespace(ip_dict={"old": {"fail_times": 1}}),
            ips_calls=[])
        front.set_ips = lambda ips: front.ips_calls.append(ips)
        global_var.session = session
        global_var.tls_relay_front = front
        global_var.config.api_server = "api.example"
        global_var.config.login_account = "test"
        global_var.config.login_password = "test"
        global_var.stat = {"timeout_roundtrip": 2}
        global_var.xxnet_version = "test"
        global_var.client_uuid = "test"

        with mock.patch.object(proxy, "call_api", return_value=(True, {"data": {"ips": ["current-ip"]}})), \
                mock.patch.object(proxy.check_local_network.IPv6, "is_ok", return_value=False):
            session.check_report_status()

        self.assertEqual(front.ips_calls, [["current-ip"]])
        self.assertEqual(session.last_state["timeout"], 2)

    def test_balance_commit_cannot_cross_real_client_stop(self):
        old_session = types.SimpleNamespace(session_id=b"old-session", stop=lambda: None)
        new_session = types.SimpleNamespace(session_id=b"new-session", stop=lambda: None)
        global_var.session = old_session
        global_var.socks5_server = None
        global_var.http_client = types.SimpleNamespace(stop=lambda: None)
        global_var.tls_relay_front = None
        global_var.seley_front = None
        global_var.config.api_server = "api.example"
        global_var.config.server_host = ""
        global_var.config.update_cloudflare_domains = False
        global_var.xxnet_version = "test"
        global_var.system = "test"
        global_var.balance = 0
        global_var.server_host = ""
        info = {
            "quota_list": {"current": {
                "end_time": int(proxy.time.time()) + 1000, "quota": 123}},
            "paypal_button_id": "test", "plans": [], "host": "old-response",
            "port": 443, "selectable": False, "promote_code": "",
            "promoter": "", "balance": 1, "openai_balance": 0,
            "openai_proxies": [], "tls_relays": {"ips": []},
        }
        commit_entered = threading.Event()
        release_commit = threading.Event()
        request_done = threading.Event()
        stop_done = threading.Event()

        def gated_calculate(quota_list):
            commit_entered.set()
            if not release_commit.wait(2):
                raise RuntimeError("test gate did not open")
            return 123

        client_path = APP / "x_tunnel" / "local" / "client.py"
        tree = ast.parse(client_path.read_text(), filename=str(client_path))
        stop_node = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "stop")
        stop_module = ast.fix_missing_locations(ast.Module(body=[stop_node], type_ignores=[]))
        stop_env = {
            "g": global_var,
            "proxy_session": proxy,
            "front_dispatcher": types.SimpleNamespace(stop=lambda: None),
            "xlog": types.SimpleNamespace(info=lambda *args, **kwargs: None),
        }
        exec(compile(stop_module, str(client_path), "exec"), stop_env)

        def run_request():
            try:
                proxy.request_balance("test", "test")
            finally:
                request_done.set()

        def run_stop():
            try:
                stop_env["stop"]()
            finally:
                stop_done.set()

        with mock.patch.object(proxy, "get_app_name", return_value="test"), \
                mock.patch.object(proxy, "call_api", return_value=(True, info)), \
                mock.patch.object(proxy, "calculate_quota_left", side_effect=gated_calculate):
            request_thread = threading.Thread(target=run_request)
            stop_thread = threading.Thread(target=run_stop)
            request_thread.start()
            self.assertTrue(commit_entered.wait(1))
            stop_thread.start()
            deadline = proxy.time.monotonic() + 1
            while global_var.session is not None and proxy.time.monotonic() < deadline:
                threading.Event().wait(0.005)
            if global_var.session is None:
                global_var.session = new_session
                global_var.balance = 999
                global_var.server_host = "new-server"
            release_commit.set()
            request_thread.join(2)
            stop_thread.join(2)
            self.assertTrue(request_done.is_set())
            self.assertTrue(stop_done.is_set())
            self.assertFalse(request_thread.is_alive())
            self.assertFalse(stop_thread.is_alive())
            if global_var.session is None:
                global_var.session = new_session
                global_var.balance = 999
                global_var.server_host = "new-server"

        self.assertIs(global_var.session, new_session)
        self.assertEqual(global_var.balance, 999)
        self.assertEqual(global_var.server_host, "new-server")


if __name__ == "__main__":
    unittest.main()
