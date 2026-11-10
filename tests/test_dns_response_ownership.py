"""DNS 查询所有权、响应匹配与期限回归测试。"""
import importlib.util
import queue
import threading
import time
import types
import unittest
from unittest import mock
from pathlib import Path


def load_transport_tests():
    path = Path(__file__).resolve().with_name("test_dns_transport.py")
    spec = importlib.util.spec_from_file_location("test_dns_transport", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


transport_tests = load_transport_tests()
ScriptedSocket = transport_tests.ScriptedSocket
ResponsiveTcpSocket = transport_tests.ResponsiveTcpSocket
dns_query = transport_tests.dns_query
from dnslib import CNAME, NS


def make_packet(query_id=7, domain="example.com", dns_type=1, qr=1, rr_type=None):
    record = dns_query.DNSRecord(dns_query.DNSHeader(id=query_id, qr=qr))
    record.add_question(dns_query.DNSQuestion(domain, dns_type))
    if rr_type is not None:
        rdata = dns_query.A("192.0.2.1") if rr_type == 1 else dns_query.AAAA("2001:db8::1")
        record.add_answer(dns_query.RR(domain, rr_type, rdata=rdata))
    return record.pack()


class UdpSocket:
    def __init__(self, local, packet, port=53, source="192.0.2.53"):
        self.local = local
        self.packet = packet
        self.port = port
        self.source = source
        self.calls = 0

    def recvfrom(self, size):
        self.calls += 1
        if self.calls == 1:
            return self.packet, (self.source, self.port)
        self.local.running = False
        raise OSError("结束接收线程")

    def close(self):
        pass


class DnsOwnershipTest(unittest.TestCase):
    def make_local(self):
        local = dns_query.LocalDnsQuery.__new__(dns_query.LocalDnsQuery)
        local.running = True
        local.waiters = {}
        local.waiters_lock = threading.Lock()
        local.dns_server = [b"192.0.2.53"]
        local.ns_backoff = {}
        local.ns_backoff_lock = threading.Lock()
        local.timeout = 1
        return local

    def test_concurrent_same_id_queries_get_distinct_transactions(self):
        local = self.make_local()
        barrier = threading.Barrier(2)
        sent_ids = []

        class GatedQueue:
            def __init__(self):
                self._domain = None

            @property
            def domain(self):
                return self._domain

            @domain.setter
            def domain(self, value):
                self._domain = value
                barrier.wait(timeout=2)

            def get(self, timeout):
                barrier.wait(timeout=2)
                raise queue.Empty()

        values = iter([7, 7, 8])
        original_queue = dns_query.Queue
        original_randint = dns_query.random.randint
        dns_query.Queue = GatedQueue
        dns_query.random.randint = lambda low, high: next(values)
        local.send_request = lambda query_id, *args: sent_ids.append(query_id)
        try:
            threads = [threading.Thread(target=local.query, args=("example.com", 1, 0.1))
                       for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(3)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        finally:
            dns_query.Queue = original_queue
            dns_query.random.randint = original_randint

        self.assertEqual(sorted(sent_ids), [7, 8])

    def test_old_query_cleanup_does_not_remove_reused_id(self):
        local = self.make_local()
        entered = threading.Event()
        release = threading.Event()
        old_waiter = queue.Queue()
        new_waiter = queue.Queue()

        class BlockingQueue:
            def get(self, timeout):
                entered.set()
                release.wait(2)
                raise queue.Empty()

        original_queue = dns_query.Queue
        original_randint = dns_query.random.randint
        dns_query.Queue = BlockingQueue
        dns_query.random.randint = lambda low, high: 7
        local.send_request = lambda *args: None
        thread = threading.Thread(target=local.query, args=("example.com", 1, 0.1))
        try:
            thread.start()
            self.assertTrue(entered.wait(1))
            local.waiters[7] = new_waiter
            release.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
        finally:
            release.set()
            dns_query.Queue = original_queue
            dns_query.random.randint = original_randint

        self.assertIs(local.waiters.get(7), new_waiter)
        self.assertTrue(old_waiter.empty())

    def test_late_response_after_timeout_is_not_delivered(self):
        local = self.make_local()
        entered = threading.Event()
        release = threading.Event()

        class GatedWaiter(queue.Queue):
            @property
            def domain(self):
                entered.set()
                release.wait(2)
                return "example.com"

        old_waiter = GatedWaiter()
        old_waiter.dns_type = 1
        local.waiters[7] = old_waiter
        thread = threading.Thread(target=local.dns_recv_worker,
                                  args=(UdpSocket(local, make_packet(rr_type=1)),))
        thread.start()
        self.assertTrue(entered.wait(1))
        local.waiters.pop(7, None)
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(old_waiter.empty())

    def test_udp_rejects_mismatched_response_metadata_and_rr(self):
        cases = {
            "qr": make_packet(qr=0, rr_type=1),
            "id": make_packet(query_id=8, rr_type=1),
            "qname": make_packet(domain="other.example", rr_type=1),
            "qtype": make_packet(dns_type=28, rr_type=28),
            "rr_type": make_packet(dns_type=1, rr_type=28),
        }
        for name, packet in cases.items():
            with self.subTest(name=name):
                local = self.make_local()
                waiter = queue.Queue()
                waiter.domain = "example.com"
                waiter.dns_type = 1
                local.waiters[7] = waiter
                local.dns_recv_worker(UdpSocket(local, packet))
                with self.assertRaises(queue.Empty):
                    waiter.get_nowait()

    def test_udp_accepts_case_and_trailing_dot_equivalence(self):
        local = self.make_local()
        waiter = queue.Queue()
        waiter.domain = "example.com"
        waiter.dns_type = 1
        local.waiters[7] = waiter
        local.dns_recv_worker(UdpSocket(local, make_packet(domain="EXAMPLE.COM.", rr_type=1)))
        self.assertEqual(waiter.get_nowait(), [b"192.0.2.1"])
        self.assertNotIn(7, local.waiters)

    def test_udp_rejects_unconfigured_source_and_non_dns_port(self):
        for source, port in (("198.51.100.99", 53), ("192.0.2.53", 5353)):
            with self.subTest(source=source, port=port):
                local = self.make_local()
                waiter = queue.Queue()
                waiter.domain = "example.com"
                waiter.dns_type = 1
                local.waiters[7] = waiter
                sock = UdpSocket(local, make_packet(rr_type=1), port=port, source=source)

                local.dns_recv_worker(sock)

                with self.assertRaises(queue.Empty):
                    waiter.get_nowait()

    def test_udp_cname_is_handed_off_without_recursive_call_on_receiver(self):
        local = self.make_local()
        waiter = queue.Queue()
        waiter.domain = "example.com"
        waiter.dns_type = 1
        local.waiters[7] = waiter
        local.query = mock.Mock(return_value=[])
        record = dns_query.DNSRecord(dns_query.DNSHeader(id=7, qr=1))
        record.add_question(dns_query.DNSQuestion("example.com", 1))
        record.add_answer(dns_query.RR("example.com", 5, rdata=CNAME("alias.example.com")))

        local.dns_recv_worker(UdpSocket(local, record.pack()))

        local.query.assert_not_called()
        result = waiter.get_nowait()
        self.assertEqual(getattr(result, "target", None), b"alias.example.com.")

    def test_xxnet_and_system_sources_preserve_requested_address_family(self):
        front = types.SimpleNamespace(request=lambda *args, **kwargs: (
            b'{"ip": ["192.0.2.1|CN", "2001:db8::1|CN"]}', 200, None))
        with mock.patch.object(dns_query.g, "x_tunnel", types.SimpleNamespace(front_dispatcher=front), create=True):
            self.assertEqual(dns_query.query_dns_from_xxnet("example.com", 28), ["2001:db8::1"])
            self.assertEqual(dns_query.query_dns_from_xxnet("example.com", 1), ["192.0.2.1"])

        local = self.make_local()
        addresses = [
            (dns_query.socket.AF_INET6, dns_query.socket.SOCK_STREAM, 6, "", ("2001:db8::2", 0, 0, 0)),
            (dns_query.socket.AF_INET, dns_query.socket.SOCK_STREAM, 0, "", ("192.0.2.2", 0)),
        ]
        with mock.patch.object(dns_query.socket, "getaddrinfo", return_value=addresses) as getaddrinfo, \
                mock.patch.object(dns_query.socket, "gethostbyname", return_value="192.0.2.2"):
            self.assertEqual(local.query_by_system("example.com", 28), ["2001:db8::2"])
            getaddrinfo.assert_called_once_with(
                "example.com", None, dns_query.socket.AF_INET6, dns_query.socket.SOCK_STREAM)

    def test_udp_alias_is_followed_by_calling_thread(self):
        local = self.make_local()
        query_threads = []

        class AliasQueue(queue.Queue):
            @property
            def domain(self):
                return self._domain

            @domain.setter
            def domain(self, value):
                self._domain = value
                query_threads.append(threading.get_ident())
                if value == "example.com":
                    self.put(dns_query._DnsAlias(b"alias.example.com."))
                else:
                    self.put([b"192.0.2.8"])

        original_queue = dns_query.Queue
        dns_query.Queue = AliasQueue
        local.send_request = lambda *args: None
        try:
            result = local.query("example.com", timeout=0.2)
        finally:
            dns_query.Queue = original_queue

        self.assertEqual(result, [b"192.0.2.8"])
        self.assertEqual(query_threads, [threading.get_ident(), threading.get_ident()])

    def test_udp_wait_uses_remaining_budget_after_multiple_sends(self):
        local = self.make_local()
        local.dns_server = [b"192.0.2.53", b"192.0.2.54"]
        observed = []

        class TimeoutQueue:
            def get(self, timeout):
                observed.append(timeout)
                raise queue.Empty()

        original_queue = dns_query.Queue
        dns_query.Queue = TimeoutQueue
        local.send_request = lambda *args: time.sleep(0.03)
        try:
            local.query("example.com", timeout=0.12)
        finally:
            dns_query.Queue = original_queue

        self.assertEqual(len(observed), 1)
        self.assertGreater(observed[0], 0)
        self.assertLess(observed[0], 0.07)

    def test_udp_deadline_is_not_extended_by_wall_clock_rollback(self):
        local = self.make_local()
        observed = []
        wall_clock = [1000.0]
        monotonic_clock = [0.0]

        class TimeoutQueue:
            def get(self, timeout):
                observed.append(timeout)
                raise queue.Empty()

        def send_request(*args):
            wall_clock[0] = 0.0

        original_queue = dns_query.Queue
        dns_query.Queue = TimeoutQueue
        local.send_request = send_request
        try:
            with mock.patch.object(dns_query.time, "time", side_effect=lambda: wall_clock[0]), \
                    mock.patch.object(dns_query.time, "monotonic", side_effect=lambda: monotonic_clock[0]):
                result = local.query("example.com", timeout=0.1)
        finally:
            dns_query.Queue = original_queue

        self.assertEqual(result, [])
        self.assertEqual(len(observed), 1)
        self.assertGreater(observed[0], 0)
        self.assertLessEqual(observed[0], 0.1)

    def test_udp_cost_log_uses_the_same_monotonic_clock(self):
        local = self.make_local()
        local.send_request = lambda *args: None

        class TimeoutQueue:
            def get(self, timeout):
                raise queue.Empty()

        original_queue = dns_query.Queue
        dns_query.Queue = TimeoutQueue
        try:
            with mock.patch.object(dns_query.time, "time", return_value=1000.0), \
                    mock.patch.object(dns_query.time, "monotonic",
                                      side_effect=[10.0, 10.0, 10.0, 10.25]), \
                    mock.patch.object(dns_query.xlog, "debug") as debug:
                local.query("example.com", timeout=0.1)
        finally:
            dns_query.Queue = original_queue

        cost_logs = [call for call in debug.call_args_list
                     if call.args[0] == "query by udp, %s cost:%f, return:%s"]
        self.assertEqual(len(cost_logs), 1)
        self.assertAlmostEqual(cost_logs[0].args[2], 0.25)


class DnsTransportMatchingTest(unittest.TestCase):
    def test_tcp_mismatched_answer_closes_without_repool(self):
        sock = ResponsiveTcpSocket(
            lambda query_id: make_packet(query_id=query_id, domain="other.example", rr_type=1))
        query = dns_query.DnsOverTcpQuery()
        query.get_connection = lambda: sock

        self.assertEqual(query.query("example.com", 1), [])
        self.assertEqual(sock.close_calls, 1)
        self.assertEqual(query.connections, [])
        self.assertTrue(sock.sent)

    def test_doh_mismatched_answer_closes_without_repool(self):
        class Client:
            def __init__(self):
                self.close_calls = 0

            def request(self, *args, **kwargs):
                request = dns_query.DNSRecord.parse(kwargs["body"])
                return types.SimpleNamespace(
                    text=make_packet(query_id=request.header.id, domain="other.example", rr_type=1))

            def close(self):
                self.close_calls += 1

        client = Client()
        query = dns_query.DnsOverHttpsQuery()
        query.get_connection = lambda: client
        self.assertEqual(query.query("example.com", 1, url="https://dns.invalid/query"), [])
        self.assertEqual(client.close_calls, 1)
        self.assertEqual(query.connections, [])

    def test_response_rejects_unrelated_owner_but_accepts_cname_target(self):
        unrelated = dns_query.DNSRecord(dns_query.DNSHeader(id=7, qr=1))
        unrelated.add_question(dns_query.DNSQuestion("example.com", 1))
        unrelated.add_answer(dns_query.RR("unrelated.example", 1, rdata=dns_query.A("192.0.2.99")))
        self.assertEqual(dns_query._extract_response_ips(unrelated, "example.com", 1, mock.Mock()), [])

        aliased = dns_query.DNSRecord(dns_query.DNSHeader(id=7, qr=1))
        aliased.add_question(dns_query.DNSQuestion("example.com", 1))
        aliased.add_answer(dns_query.RR("example.com", 5, rdata=CNAME("alias.example.com")))
        aliased.add_answer(dns_query.RR("alias.example.com", 1, rdata=dns_query.A("192.0.2.8")))
        recursive = mock.Mock()
        self.assertEqual(
            dns_query._extract_response_ips(aliased, "example.com", 1, recursive),
            [b"192.0.2.8"])
        recursive.assert_not_called()

    def test_ns_cname_is_followed_when_no_direct_ns_answer(self):
        response = dns_query.DNSRecord(dns_query.DNSHeader(id=7, qr=1))
        response.add_question(dns_query.DNSQuestion("example.com", 2))
        response.add_answer(dns_query.RR("example.com", 5, rdata=CNAME("alias.example.com")))
        recursive = mock.Mock(return_value=[b"ns1.example.com."])

        self.assertEqual(
            dns_query._extract_response_ips(response, "example.com", 2, recursive),
            [b"ns1.example.com."])
        recursive.assert_called_once()

    def test_ns_answer_keeps_existing_semantics(self):
        def response(query_id):
            record = dns_query.DNSRecord(dns_query.DNSHeader(id=query_id, qr=1))
            record.add_question(dns_query.DNSQuestion("example.com", 2))
            record.add_answer(dns_query.RR("example.com", 2, rdata=NS("ns1.example.com")))
            return record.pack()

        sock = ResponsiveTcpSocket(response)
        query = dns_query.DnsOverTcpQuery()
        query.get_connection = lambda: sock

        self.assertEqual(query.query("example.com", 2), [b"ns1.example.com."])
        self.assertEqual(sock.close_calls, 0)

    def test_cname_is_followed_only_when_no_direct_answer(self):
        def response(query_id):
            record = dns_query.DNSRecord(dns_query.DNSHeader(id=query_id, qr=1))
            record.add_question(dns_query.DNSQuestion("example.com", 1))
            record.add_answer(dns_query.RR("example.com", 5, rdata=CNAME("alias.example.com")))
            return record.pack()

        sock = ResponsiveTcpSocket(response)
        query = dns_query.DnsOverTcpQuery()
        query.get_connection = lambda: sock
        recursive_calls = []

        original_query = query.query
        def recursive(domain, dns_type=1, history=None):
            if domain in ("example.com", b"example.com"):
                return original_query(domain, dns_type, history=history)
            recursive_calls.append((domain, dns_type))
            return [b"192.0.2.9"]
        query.query = recursive

        self.assertEqual(query.query("example.com", 1), [b"192.0.2.9"])
        self.assertEqual(len(recursive_calls), 1)


if __name__ == "__main__":
    unittest.main()
