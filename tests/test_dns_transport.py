"""DNS TCP/TLS 分帧与失败连接所有权测试，全程使用离线替身。"""
import importlib.util
from pathlib import Path
import queue
import socket
import sys
import threading
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "Coding" / "XX-Net-5.16.7.1" / "code" / "default"
NOARCH = APP / "lib" / "noarch"
if str(NOARCH) not in sys.path:
    sys.path.insert(0, str(NOARCH))


def namespace(name, path):
    if name not in sys.modules:
        module = types.ModuleType(name)
        module.__path__ = [str(path)]
        sys.modules[name] = module
    return sys.modules[name]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


namespace("smart_router", APP / "smart_router")
namespace("smart_router.local", APP / "smart_router" / "local")
dns_query = load_module(
    "smart_router.local.dns_query", APP / "smart_router" / "local" / "dns_query.py")
smart_route = load_module(
    "smart_router.local.smart_route", APP / "smart_router" / "local" / "smart_route.py")
simple_http_client = load_module(
    "simple_http_client", NOARCH / "simple_http_client.py")


def dns_answer():
    question = dns_query.DNSRecord(dns_query.DNSHeader(id=7))
    question.add_question(dns_query.DNSQuestion("example.com", dns_query.QTYPE.A))
    answer = question.reply()
    answer.add_answer(dns_query.RR(
        "example.com", dns_query.QTYPE.A, rdata=dns_query.A("192.0.2.1")))
    return answer.pack()


class ScriptedSocket:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.recv_calls = 0
        self.close_calls = 0
        self.sent = []

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, size):
        self.recv_calls += 1
        if not self.chunks:
            raise AssertionError("DNS 分帧在脚本结束后仍继续 recv")
        chunk = self.chunks.pop(0)
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk

    def close(self):
        self.close_calls += 1


class FailingClient:
    def __init__(self):
        self.close_calls = 0

    def request(self, *args, **kwargs):
        return None

    def close(self):
        self.close_calls += 1


class DnsTcpTransportTest(unittest.TestCase):
    def run_query(self, sock):
        query = dns_query.DnsOverTcpQuery()
        query.get_connection = lambda: sock
        result = query.query("example.com", dns_query.QTYPE.A)
        return query, result

    def test_split_length_prefix_is_reassembled(self):
        packet = dns_answer()
        framed = len(packet).to_bytes(2, "big") + packet
        sock = ScriptedSocket([framed[:1], framed[1:5], framed[5:]])

        query, result = self.run_query(sock)

        self.assertEqual(result, [b"192.0.2.1"])
        self.assertEqual(sock.recv_calls, 3)
        self.assertEqual(sock.close_calls, 0)
        self.assertEqual([entry[0] for entry in query.connections], [sock])

    def test_truncated_frame_stops_at_eof_and_closes_socket(self):
        packet = dns_answer()
        framed = len(packet).to_bytes(2, "big") + packet
        sock = ScriptedSocket([framed[:6], b""])

        query, result = self.run_query(sock)

        self.assertEqual(result, [])
        self.assertEqual(sock.recv_calls, 2)
        self.assertEqual(sock.close_calls, 1)
        self.assertEqual(query.connections, [])

    def test_timeout_closes_checked_out_socket(self):
        sock = ScriptedSocket([socket.timeout("forced timeout")])

        query, result = self.run_query(sock)

        self.assertEqual(result, [])
        self.assertEqual(sock.close_calls, 1)
        self.assertEqual(query.connections, [])

    def test_invalid_dns_payload_closes_checked_out_socket(self):
        sock = ScriptedSocket([b"\x00\x04bad!"])

        query, result = self.run_query(sock)

        self.assertEqual(result, [])
        self.assertEqual(sock.close_calls, 1)
        self.assertEqual(query.connections, [])


class DnsHttpsOwnershipTest(unittest.TestCase):
    def test_failed_request_closes_checked_out_client(self):
        client = FailingClient()
        query = dns_query.DnsOverHttpsQuery()
        query.get_connection = lambda: client

        self.assertEqual(query.query("example.com"), [])
        self.assertEqual(client.close_calls, 1)
        self.assertEqual(query.connections, [])

    def test_http_client_close_releases_socket(self):
        client = simple_http_client.Client()
        sock = ScriptedSocket([])
        client.sock = sock

        client.close()

        self.assertEqual(sock.close_calls, 1)
        self.assertIsNone(client.sock)


class LocalAndCombinedDnsTest(unittest.TestCase):
    def test_ipv6_udp_response_address_is_accepted(self):
        local = dns_query.LocalDnsQuery.__new__(dns_query.LocalDnsQuery)
        local.running = True
        result_queue = queue.Queue()
        result_queue.domain = "example.com"
        local.waiters = {7: result_queue}

        class UdpSocket:
            def __init__(self):
                self.calls = 0
                self.close_calls = 0

            def recvfrom(self, size):
                self.calls += 1
                if self.calls == 1:
                    return dns_answer(), ("2001:db8::1", 53, 0, 0)
                local.running = False
                raise OSError("结束接收线程")

            def close(self):
                self.close_calls += 1

        sock = UdpSocket()
        local.dns_recv_worker(sock)

        self.assertEqual(result_queue.get_nowait(), [b"192.0.2.1"])
        self.assertEqual(sock.close_calls, 1)

    def test_local_query_honors_caller_timeout(self):
        local = dns_query.LocalDnsQuery.__new__(dns_query.LocalDnsQuery)
        local.timeout = 3
        local.waiters = {}
        local.dns_server = []
        local.ns_backoff = {}
        local.ns_backoff_lock = threading.Lock()
        observed_timeouts = []

        class TimeoutQueue:
            def get(self, timeout):
                observed_timeouts.append(timeout)
                raise queue.Empty()

        original_queue = dns_query.Queue
        dns_query.Queue = TimeoutQueue
        try:
            result = local.query("example.com", timeout=0.125)
        finally:
            dns_query.Queue = original_queue

        self.assertEqual(result, [])
        self.assertEqual(observed_timeouts, [0.125])

    def test_fallbacks_preserve_aaaa_query_type(self):
        class LocalRecorder:
            def __init__(self):
                self.calls = []

            def query(self, domain, dns_type=1, timeout=3):
                self.calls.append((domain, dns_type, timeout))
                return []

        class EmptyCache:
            def get_ips(self, domain, dns_type):
                return []

            def set_ips(self, domain, ips, dns_type):
                pass

        original_g = dns_query.g
        for route in ("blocked", "white"):
            with self.subTest(route=route):
                combined = dns_query.CombineDnsQuery.__new__(dns_query.CombineDnsQuery)
                combined.domain_allowed_pattern = dns_query.re.compile(
                    br"(?!-)[A-Z\d-]{1,63}(?<!-)$")
                local = LocalRecorder()
                combined.local_dns_resolve = local
                combined.query_blocked_domain = lambda domain, dns_type: []
                dns_query.g = types.SimpleNamespace(
                    domain_cache=EmptyCache(),
                    user_rules=types.SimpleNamespace(check_host=lambda host, port: None),
                    gfwlist=types.SimpleNamespace(
                        in_block_list=lambda host: route == "blocked",
                        in_white_list=lambda host: route == "white"),
                    config=types.SimpleNamespace(pac_policy=""))
                try:
                    self.assertEqual(combined.query("example.com", 28), [])
                finally:
                    dns_query.g = original_g

                self.assertTrue(local.calls)
                self.assertTrue(all(call[1] == 28 for call in local.calls), local.calls)
    def test_ipv6_direct_queries_aaaa_not_soa(self):
        calls = []
        original_g = smart_route.g
        original_is_ipv6_ok = smart_route.is_ipv6_ok
        original_do_direct = smart_route.do_direct
        smart_route.g = types.SimpleNamespace(
            dns_query=types.SimpleNamespace(
                query=lambda host, dns_type: calls.append((host, dns_type)) or [b"2001:db8::1"]))
        smart_route.is_ipv6_ok = lambda: True
        smart_route.do_direct = lambda *args: None
        try:
            smart_route.try_loop("test", ["direct"], None, b"example.com", 443, None)
        finally:
            smart_route.g = original_g
            smart_route.is_ipv6_ok = original_is_ipv6_ok
            smart_route.do_direct = original_do_direct

        self.assertEqual(calls, [(b"example.com", 28)])


if __name__ == "__main__":
    unittest.main()
