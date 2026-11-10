"""DNS TCP/TLS 分帧与失败连接所有权测试，全程使用离线替身。"""
import importlib.util
from pathlib import Path
import queue
import socket
import sys
import threading
import time
import types
import unittest
from unittest import mock


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
from dnslib import CNAME


def cname_response(request, aliases):
    domain = dns_query._normalize_dns_name(request.questions[0].qname)
    dns_type = request.questions[0].qtype
    response = dns_query.DNSRecord(dns_query.DNSHeader(id=request.header.id, qr=1))
    response.add_question(dns_query.DNSQuestion(domain, dns_type))
    target = aliases.get(domain)
    if target is None:
        response.add_answer(dns_query.RR(
            domain, dns_type, rdata=dns_query.A("192.0.2.8")))
    else:
        response.add_answer(dns_query.RR(domain, 5, rdata=CNAME(target)))
    return response.pack()


def dns_answer(query_id=0):
    question = dns_query.DNSRecord(dns_query.DNSHeader(id=query_id))
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


class ResponsiveTcpSocket(ScriptedSocket):
    """根据真实请求 ID 构造离线 TCP DNS 响应。"""
    def __init__(self, response_factory=dns_answer, fragment_sizes=None):
        super().__init__([])
        self.response_factory = response_factory
        self.fragment_sizes = fragment_sizes or []

    def sendall(self, data):
        super().sendall(data)
        request = dns_query.DNSRecord.parse(data[2:])
        packet = self.response_factory(request.header.id)
        framed = len(packet).to_bytes(2, "big") + packet
        start = 0
        for size in self.fragment_sizes:
            self.chunks.append(framed[start:start + size])
            start += size
        self.chunks.append(framed[start:])


class CnameTcpSocket(ResponsiveTcpSocket):
    def __init__(self, aliases, barrier=None):
        super().__init__([])
        self.aliases = aliases
        self.barrier = barrier

    def sendall(self, data):
        ScriptedSocket.sendall(self, data)
        request = dns_query.DNSRecord.parse(data[2:])
        packet = cname_response(request, self.aliases)
        framed = len(packet).to_bytes(2, "big") + packet
        self.chunks.append(framed)
        if self.barrier is not None:
            self.barrier.wait(2)


class CnameClient:
    def __init__(self, aliases, fail_after=20):
        self.aliases = aliases
        self.fail_after = fail_after
        self.calls = 0
        self.close_calls = 0

    def request(self, *args, **kwargs):
        self.calls += 1
        if self.calls > self.fail_after:
            return None
        request = dns_query.DNSRecord.parse(kwargs["body"])
        return types.SimpleNamespace(text=cname_response(request, self.aliases))

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
        sock = ResponsiveTcpSocket(dns_answer, fragment_sizes=[1, 4])

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

    def test_expired_tcp_and_doh_pool_entries_are_closed(self):
        tcp = dns_query.DnsOverTcpQuery()
        expired_tcp = ScriptedSocket([])
        fresh_tcp = ResponsiveTcpSocket()
        tcp.connections = [[expired_tcp, time.time() - tcp.connection_timeout - 1]]
        tcp.connect = lambda host, port: fresh_tcp

        self.assertIs(tcp.get_connection(), fresh_tcp)
        self.assertEqual(expired_tcp.close_calls, 1)

        doh = dns_query.DnsOverHttpsQuery()
        expired_doh = FailingClient()
        fresh_doh = FailingClient()
        doh.connections = [[expired_doh, time.time() - doh.connection_timeout - 1]]
        dns_query.g.config = types.SimpleNamespace(PROXY_ENABLE=0)
        with mock.patch.object(dns_query.simple_http_client, "Client", return_value=fresh_doh):
            self.assertIs(doh.get_connection(), fresh_doh)
        self.assertEqual(expired_doh.close_calls, 1)

    def test_expired_pool_close_failure_does_not_block_new_connection(self):
        class CloseFailure(ScriptedSocket):
            def close(self):
                self.close_calls += 1
                raise OSError("controlled close failure")

        tcp = dns_query.DnsOverTcpQuery()
        expired = CloseFailure([])
        fresh = ResponsiveTcpSocket()
        tcp.connections = [[expired, 0]]
        tcp.connect = lambda host, port: fresh

        self.assertIs(tcp.get_connection(), fresh)
        self.assertEqual(expired.close_calls, 1)

    def test_tcp_cname_cycle_is_bounded_and_does_not_accumulate_pool(self):
        aliases = {"a.example": "B.EXAMPLE.", "b.example": "a.example."}
        sockets = []

        def connect(host, port):
            if len(sockets) >= 20:
                raise OSError("recursive query guard")
            sock = CnameTcpSocket(aliases)
            sockets.append(sock)
            return sock

        query = dns_query.DnsOverTcpQuery()
        query.connect = connect

        self.assertEqual(query.query("a.example"), [])
        self.assertLessEqual(len(sockets), 16)
        self.assertEqual(query.connections, [])
        self.assertTrue(all(sock.close_calls == 1 for sock in sockets))

    def test_doh_cname_cycle_is_bounded_and_does_not_accumulate_pool(self):
        aliases = {"a.example": "B.EXAMPLE.", "b.example": "a.example."}
        clients = []

        def get_connection():
            if len(clients) >= 20:
                return None
            client = CnameClient(aliases)
            clients.append(client)
            return client

        query = dns_query.DnsOverHttpsQuery()
        query.get_connection = get_connection

        self.assertEqual(query.query("a.example"), [])
        self.assertLessEqual(len(clients), 16)
        self.assertEqual(query.connections, [])
        self.assertTrue(all(client.close_calls == 1 for client in clients))

    def test_doh_noncycle_cname_succeeds(self):
        aliases = {"start.example": "alias.example."}
        client = CnameClient(aliases)
        query = dns_query.DnsOverHttpsQuery()
        query.get_connection = lambda: client

        self.assertEqual(query.query("START.EXAMPLE."), [b"192.0.2.8"])
        self.assertEqual(client.calls, 2)

    def test_parallel_tcp_success_pool_has_capacity_and_stop_closes_all(self):
        aliases = {"example.com": None}
        barrier = threading.Barrier(6)
        sockets = [CnameTcpSocket(aliases, barrier=barrier) for _ in range(6)]
        connect_lock = threading.Lock()
        next_socket = [0]

        def connect(host, port):
            with connect_lock:
                sock = sockets[next_socket[0]]
                next_socket[0] += 1
            return sock

        query = dns_query.DnsOverTcpQuery()
        query.connect = connect
        threads = [threading.Thread(target=query.query, args=("example.com", 1))
                   for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertFalse(any(thread.is_alive() for thread in threads))

        self.assertEqual(len(query.connections), 4)
        query.stop()
        self.assertEqual(query.connections, [])
        self.assertTrue(all(sock.close_calls == 1 for sock in sockets))

    def test_doh_stop_closes_every_pooled_client(self):
        query = dns_query.DnsOverHttpsQuery()
        clients = [FailingClient(), FailingClient()]
        query.connections = [[client, time.time()] for client in clients]

        if not hasattr(query, "stop"):
            self.fail("DoH 连接池缺少 stop 关闭入口")
        query.stop()

        self.assertEqual(query.connections, [])
        self.assertEqual([client.close_calls for client in clients], [1, 1])


class LocalAndCombinedDnsTest(unittest.TestCase):
    def test_ipv6_udp_response_address_is_accepted(self):
        local = dns_query.LocalDnsQuery.__new__(dns_query.LocalDnsQuery)
        local.running = True
        result_queue = queue.Queue()
        result_queue.domain = "example.com"
        result_queue.dns_type = dns_query.QTYPE.A
        local.waiters = {7: result_queue}
        local.waiters_lock = threading.Lock()
        local.dns_server = [b"2001:db8::1"]

        class UdpSocket:
            def __init__(self):
                self.calls = 0
                self.close_calls = 0

            def recvfrom(self, size):
                self.calls += 1
                if self.calls == 1:
                    return dns_answer(query_id=7), ("2001:db8::1", 53, 0, 0)
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
        local.waiters_lock = threading.Lock()
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
        self.assertEqual(len(observed_timeouts), 1)
        self.assertGreater(observed_timeouts[0], 0)
        self.assertLessEqual(observed_timeouts[0], 0.125)

    def test_local_udp_cname_cycle_is_bounded_and_alias_chain_still_works(self):
        local = dns_query.LocalDnsQuery.__new__(dns_query.LocalDnsQuery)
        local.waiters = {}
        local.waiters_lock = threading.Lock()
        local.dns_server = []
        local.ns_backoff = {}
        local.ns_backoff_lock = threading.Lock()
        calls = []

        class AliasQueue:
            def __init__(self):
                self.domain = None

            def get(self, timeout):
                domain = dns_query._normalize_dns_name(self.domain)
                calls.append(domain)
                if domain == "a.example":
                    return dns_query._DnsAlias("b.example.")
                if domain == "b.example":
                    if cycle:
                        return dns_query._DnsAlias("A.EXAMPLE.")
                    return [b"192.0.2.8"]
                raise AssertionError("unexpected alias target")

        original_queue = dns_query.Queue
        dns_query.Queue = AliasQueue
        try:
            cycle = False
            self.assertEqual(local.query("a.example"), [b"192.0.2.8"])
            self.assertEqual(calls, ["a.example", "b.example"])

            calls[:] = []
            cycle = True
            try:
                result = local.query("a.example")
            except RecursionError:
                self.fail("UDP CNAME 环必须由共享历史上限终止，而不是耗尽 Python 递归栈")
            self.assertEqual(result, [])
            self.assertLessEqual(len(calls), 16)
        finally:
            dns_query.Queue = original_queue

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

            def get_epoch(self):
                return 0

            def set_ips(self, domain, ips, dns_type, expected_epoch=None):
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
