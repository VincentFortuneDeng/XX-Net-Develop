import collections
import json
import queue
import re
import sys
import threading
import types
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "Coding" / "XX-Net-5.16.7.1" / "code" / "default"
TESTS = ROOT / "tests"
for path in (str(TESTS), str(APP / "lib" / "noarch")):
    if path not in sys.path:
        sys.path.insert(0, path)

import xlog
from runtime_metrics import CounterSet
import test_x_tunnel_memory_backpressure as xt_fixture

base = xt_fixture.base
global_var = xt_fixture.global_var
struct = xt_fixture.struct
from x_tunnel.local import metrics as x_tunnel_metrics


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
smart_g = namespace("smart_router.local.global_var")
from smart_router.local import metrics as smart_metrics
from smart_router.local import dns_query, host_records, pipe_socks, smart_route

namespace("front_base", APP / "lib" / "noarch" / "front_base")
from front_base import metrics as front_metrics
from front_base import http_dispatcher
from gae_proxy.local import metrics as front_snapshot


SCHEMA = json.loads((ROOT / "test-results" / "2026-11-09-runtime-convergence" /
                     "runtime-metrics-schema.json").read_text())


class BusyLock:
    def acquire(self, blocking=True):
        return False

    def locked(self):
        return True

    def release(self):
        raise AssertionError("未获取的锁不应释放")

    def __enter__(self):
        raise AssertionError("snapshot 不应无限等待业务锁")

    def __exit__(self, *args):
        return False


def metric(snapshot, name):
    return snapshot[name]


class TargetDict(dict):
    def __getitem__(self, key):
        if key not in self:
            raise AssertionError("缺少目标指标字段: %s" % key)
        value = super().__getitem__(key)
        if isinstance(value, dict) and not isinstance(value, TargetDict):
            value = TargetDict(value)
            self[key] = value
        return value


def xt_snapshot(*args, **kwargs):
    return TargetDict(x_tunnel_metrics.snapshot(*args, **kwargs))


def sr_snapshot(*args, **kwargs):
    return TargetDict(smart_metrics.snapshot(*args, **kwargs))


def gae_snapshot():
    return TargetDict(front_snapshot.snapshot())


def xlog_snapshot(*args, **kwargs):
    return TargetDict(xlog.runtime_snapshot(*args, **kwargs))


class CounterSetTest(unittest.TestCase):
    def test_concurrent_increment_is_atomic_and_snapshot_is_isolated(self):
        counters = CounterSet(["events"])
        workers = []
        barrier = threading.Barrier(9)

        def increment():
            barrier.wait()
            for _ in range(500):
                counters.increment("events")

        for _ in range(8):
            worker = threading.Thread(target=increment)
            worker.start()
            workers.append(worker)
        barrier.wait()
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())

        self.assertEqual(counters.snapshot(), {"events": 4000})
        copied = counters.snapshot()
        copied["events"] = 0
        self.assertEqual(counters.snapshot()["events"], 4000)

    def test_increment_rejects_unknown_boolean_non_integer_and_non_positive(self):
        counters = CounterSet(["events"])
        with self.assertRaises(KeyError):
            counters.increment("unknown")
        for value in (True, False, 1.0, "1", None):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    counters.increment("events", value)
        for value in (0, -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    counters.increment("events", value)
        self.assertEqual(counters.snapshot(), {"events": 0})


class XTunnelsMetricsTest(unittest.TestCase):
    def setUp(self):
        self.helper = xt_fixture.MemoryBackpressureTest()
        self.helper.setUp()
        self.sent = bytearray()

    def make_conn(self, sock=None, windows_size=10 * 1024 * 1024, is_client=True):
        if sock is None and is_client:
            sock = mock.Mock()
            sock.fileno.return_value = 12
            sock.send.side_effect = lambda data: self.sent.extend(data) or len(data)
        return self.helper.make_command_conn(
            sock=sock, windows_size=windows_size, is_client=is_client)

    def put_data(self, conn, seq, payload):
        conn.put_cmd_data(base.ReadBuffer(struct.pack("<IB", seq, 1) + payload))

    def test_recovery_waits_for_original_command_and_send_progress(self):
        conn = self.make_conn()
        before = xt_snapshot(conn.session)["x_tunnel.command_gap_recovered_total"]
        gaps = xt_snapshot(conn.session)["x_tunnel.command_gaps_total"]

        self.put_data(conn, 2, b"later")
        pending = xt_snapshot(conn.session)
        self.assertEqual(pending["x_tunnel.command_gaps_total"], gaps + 1)
        self.assertEqual(pending["x_tunnel.command_gap_recovered_total"], before)

        self.put_data(conn, 1, b"original")
        conn.process_cmd()
        recovered = xt_snapshot(conn.session)
        self.assertEqual(recovered["x_tunnel.command_gap_recovered_total"], before + 1)
        self.assertEqual(bytes(self.sent), b"originallater")

        self.put_data(conn, 3, b"next")
        conn.process_cmd()
        self.assertEqual(
            xt_snapshot(conn.session)["x_tunnel.command_gap_recovered_total"],
            before + 1)

    def test_original_missing_close_command_is_not_recovery(self):
        conn = self.make_conn()
        before = xt_snapshot(conn.session)["x_tunnel.command_gap_recovered_total"]

        self.put_data(conn, 2, b"later")
        conn.put_cmd_data(base.ReadBuffer(struct.pack("<IB", 1, 2)))
        conn.process_cmd()

        self.assertEqual(
            xt_snapshot(conn.session)["x_tunnel.command_gap_recovered_total"], before)

    def test_missing_data_without_local_send_progress_is_not_recovery(self):
        conn = self.make_conn(sock=None, is_client=False)
        before = xt_snapshot(conn.session)["x_tunnel.command_gap_recovered_total"]

        self.put_data(conn, 2, b"later")
        self.put_data(conn, 1, b"original")
        conn.process_cmd()

        self.assertEqual(conn.sended_position, 0)
        self.assertEqual(
            xt_snapshot(conn.session)["x_tunnel.command_gap_recovered_total"], before)

    def test_timeout_stop_clear_and_generation_do_not_count_recovery(self):
        for reason in ("command_gap_timeout", "session reset", "test stop"):
            with self.subTest(reason=reason):
                conn = self.make_conn()
                before = xt_snapshot(conn.session)["x_tunnel.command_gap_recovered_total"]
                self.put_data(conn, 2, b"later")
                if reason == "command_gap_timeout":
                    with mock.patch.object(base.time, "monotonic", return_value=100.0):
                        conn.cmd_queue_since = 100.0
                    with mock.patch.object(base.time, "monotonic", return_value=131.0):
                        conn.check_cmd_queue()
                else:
                    conn.do_stop(reason)
                self.assertFalse(conn.running)
                self.assertEqual(conn.cmd_queue, {})
                self.assertEqual(
                    xt_snapshot(conn.session)["x_tunnel.command_gap_recovered_total"],
                    before)

    def test_gap_gauges_buffers_and_process_counters_have_schema_types(self):
        conn = self.make_conn(windows_size=10)
        with mock.patch.object(base.time, "monotonic", return_value=100.0):
            self.put_data(conn, 2, b"12345")
        conn.session.send_buffer.put(b"abc")
        conn.send_buffer = b"1234"

        result = xt_snapshot(conn.session, now=112.0)
        self.assertEqual(result["x_tunnel.active_conns"], 1)
        self.assertEqual(result["x_tunnel.send_buffer_bytes"], 3)
        self.assertEqual(result["x_tunnel.conn_send_buffer_bytes"], 4)
        self.assertEqual(result["x_tunnel.cmd_queue_bytes"], 6)
        self.assertEqual(result["x_tunnel.cmd_queue_window_ratio"], 0.6)
        self.assertEqual(result["x_tunnel.cmd_queue_gap_age_seconds"], 12.0)
        self.assertIsInstance(result["x_tunnel.upload_bytes_total"], int)
        self.assertIsInstance(result["x_tunnel.command_gaps_total"], int)

    def test_no_conn_and_unavailable_locks_return_null_not_fake_zero(self):
        conn = self.make_conn()
        empty = self.make_conn()
        empty.session.conn_list = {}
        result = xt_snapshot(empty.session)
        self.assertIsNone(result["x_tunnel.cmd_queue_window_ratio"])
        self.assertIsNone(result["x_tunnel.cmd_queue_gap_age_seconds"])

        conn.session.lock = BusyLock()
        result = xt_snapshot(conn.session)
        self.assertIsNone(result["x_tunnel.active_conns"])
        self.assertIsNone(result["x_tunnel.cmd_queue_bytes"])

        conn = self.make_conn()
        conn.session.send_buffer.mutex = BusyLock()
        conn._lock = BusyLock()
        result = xt_snapshot(conn.session)
        self.assertIsNone(result["x_tunnel.send_buffer_bytes"])
        self.assertIsNone(result["x_tunnel.conn_send_buffer_bytes"])
        self.assertIsNone(result["x_tunnel.cmd_queue_window_ratio"])

    def test_module_not_running_keeps_full_keys_without_leaking_objects(self):
        conn = self.make_conn()
        conn.host = b"private-target.invalid"
        conn.last_error = "private exception text"
        conn.session.running = False
        result = xt_snapshot(conn.session)
        expected = {name for name in SCHEMA["fields"] if name.startswith("x_tunnel.")}
        self.assertEqual(set(result), expected)
        encoded = json.dumps(result, allow_nan=False)
        self.assertNotIn("private-target.invalid", encoded)
        self.assertNotIn("private exception text", encoded)
        self.assertIsNone(result["x_tunnel.active_conns"])


class DnsMetricsTest(unittest.TestCase):
    def setUp(self):
        self.cache = types.SimpleNamespace(
            get_ips=lambda *args: [],
            get_epoch=lambda: 0,
            set_ips=lambda *args, **kwargs: None,
        )
        smart_g.domain_cache = self.cache
        smart_g.user_rules = types.SimpleNamespace(check_host=lambda *args: "direct")
        smart_g.gfwlist = types.SimpleNamespace(
            in_white_list=lambda domain: True,
            in_block_list=lambda domain: False,
        )
        smart_g.config = types.SimpleNamespace(pac_policy="")

    def make_combine(self, local):
        query = dns_query.CombineDnsQuery.__new__(dns_query.CombineDnsQuery)
        query.domain_allowed_pattern = re.compile(br"(?!-)[A-Z\d-]{1,63}(?<!-)$")
        query.local_dns_resolve = local
        return query

    def test_top_level_complete_empty_and_alias_recursion_are_not_double_counted(self):
        smart_g.user_rules.check_host = lambda *args: None
        smart_g.gfwlist.in_white_list = lambda domain: False
        smart_g.gfwlist.in_block_list = lambda domain: True
        query = self.make_combine(mock.Mock())
        query.local_dns_resolve.query.return_value = []
        query.query_blocked_domain = mock.Mock(
            side_effect=[[b"alias.test"], [b"192.0.2.1"], []])
        before = sr_snapshot(pipe_socks=False, domain_cache=self.cache)

        self.assertEqual(query.query("example.test"), [b"192.0.2.1"])
        self.assertEqual(query.query("empty.test"), [])
        after = sr_snapshot(pipe_socks=False, domain_cache=self.cache)
        self.assertEqual(after["dns.queries_total"] - before["dns.queries_total"], 2)
        self.assertEqual(
            after["dns.complete_results_total"] - before["dns.complete_results_total"], 1)
        self.assertEqual(after["dns.empty_results_total"] - before["dns.empty_results_total"], 1)
        self.assertGreaterEqual(after["dns.latency_seconds_total"], before["dns.latency_seconds_total"])

    def test_real_local_deadline_distinguishes_timeout_from_empty_result(self):
        local = dns_query.LocalDnsQuery.__new__(dns_query.LocalDnsQuery)
        local.waiters = {}
        local.waiters_lock = threading.Lock()
        local.dns_server = []
        local.ns_backoff = {}
        local.ns_backoff_lock = threading.Lock()
        query = self.make_combine(local)
        before = sr_snapshot(pipe_socks=False, domain_cache=self.cache)

        with mock.patch.object(dns_query, "Queue", queue.Queue):
            self.assertEqual(query.query("timeout.test"), [])

        after = sr_snapshot(pipe_socks=False, domain_cache=self.cache)
        self.assertEqual(after["dns.timeouts_total"] - before["dns.timeouts_total"], 1)
        self.assertEqual(after["dns.empty_results_total"], before["dns.empty_results_total"])

    def test_old_parallel_result_and_old_cache_epoch_are_separate_counters(self):
        parallel = dns_query.ParallelQuery()
        key = ("example.test", 1)
        parallel.generations[key] = 2
        task = queue.Queue()
        task.domain, task.dns_type, task.generation, task.pending = key[0], 1, 1, 1
        task.domain_cache = self.cache
        task.cache_epoch = 0
        before = sr_snapshot(pipe_socks=False, domain_cache=self.cache)
        parallel.query_worker(task, lambda *args: [b"192.0.2.1"])
        after = sr_snapshot(pipe_socks=False, domain_cache=self.cache)
        self.assertEqual(after["dns.generation_drops_total"] - before["dns.generation_drops_total"], 1)

        records = host_records.DomainRecords.__new__(host_records.DomainRecords)
        records.cache = collections.OrderedDict()
        records.capacity = 2
        records.epoch = 2
        records.lock = threading.RLock()
        records.set_ips("example.test", [b"192.0.2.1"], 1, expected_epoch=1)
        final = sr_snapshot(pipe_socks=False, domain_cache=records)
        self.assertEqual(
            final["dns.cache_commit_rejected_total"] - before["dns.cache_commit_rejected_total"], 1)
        self.assertEqual(final["dns.cache_entries"], 0)
        self.assertEqual(final["dns.cache_capacity"], 2)
        self.assertEqual(final["dns.cache_epoch"], 2)

    def test_dns_cache_lock_unavailable_and_snapshot_whitelist(self):
        records = host_records.DomainRecords.__new__(host_records.DomainRecords)
        records.cache = collections.OrderedDict({b"private-target.invalid": {}})
        records.capacity = 2
        records.epoch = 3
        records.lock = BusyLock()
        result = sr_snapshot(pipe_socks=False, domain_cache=records)
        self.assertIsNone(result["dns.cache_entries"])
        self.assertNotIn("private-target.invalid", json.dumps(result))


class PipeMetricsTest(unittest.TestCase):
    def test_fixed_route_outcomes_update_only_their_category(self):
        before = sr_snapshot(pipe_socks=False, domain_cache=False)
        sock = mock.Mock()
        with mock.patch.object(smart_route, "do_direct"), \
                mock.patch.object(smart_route, "is_ipv6_ok", return_value=False):
            smart_g.dns_query = types.SimpleNamespace(query=lambda *args: [b"192.0.2.1"])
            smart_route.try_loop("test", ["direct"], sock, "example.test", 443, ("local", 1))
        smart_route.try_loop("test", ["black"], sock, "example.test", 443, ("local", 1))
        smart_route.try_loop("test", [], sock, "example.test", 443, ("local", 1))
        after = sr_snapshot(pipe_socks=False, domain_cache=False)
        for name in ("direct", "blocked", "unknown"):
            self.assertEqual(
                after["smart_router.route_%s_total" % name] -
                before["smart_router.route_%s_total" % name], 1)
        self.assertEqual(
            after["smart_router.route_proxy_total"], before["smart_router.route_proxy_total"])

    def test_failed_direct_connect_is_fallback_not_pipe_handoff_failure(self):
        before = sr_snapshot(pipe_socks=False, domain_cache=False)
        sock = mock.Mock()
        with mock.patch.object(smart_route, "do_direct", side_effect=smart_route.ConnectFail), \
                mock.patch.object(smart_route, "is_ipv6_ok", return_value=False):
            smart_g.dns_query = types.SimpleNamespace(query=lambda *args: [b"192.0.2.1"])
            smart_route.try_loop("test", ["direct"], sock, "example.test", 443, ("local", 1))
        after = sr_snapshot(pipe_socks=False, domain_cache=False)
        self.assertEqual(
            after["smart_router.direct_connect_failures_total"] -
            before["smart_router.direct_connect_failures_total"], 1)
        self.assertEqual(
            after["smart_router.handoff_failures_total"],
            before["smart_router.handoff_failures_total"],
            "direct 远端建连失败后的正常回退不得混入 PipeSocks 交接失败")
        self.assertEqual(
            after["smart_router.route_direct_total"], before["smart_router.route_direct_total"])

    def test_direct_prefix_send_failure_is_fallback_not_pipe_handoff_failure(self):
        before = sr_snapshot(pipe_socks=False, domain_cache=False)
        remote = mock.Mock()
        remote.ip = "192.0.2.1"
        remote.sendall.side_effect = OSError("controlled prefix send failure")
        smart_g.connect_manager = types.SimpleNamespace(get_conn=lambda *args: remote)
        smart_g.pipe_socks = types.SimpleNamespace(add_socks=mock.Mock())
        with mock.patch.object(smart_route, "is_ipv6_ok", return_value=False):
            smart_g.dns_query = types.SimpleNamespace(query=lambda *args: [b"192.0.2.1"])
            smart_route.try_loop(
                "test", ["direct"], mock.Mock(), "example.test", 443,
                ("local", 1), left_buf=b"prefix")
        after = sr_snapshot(pipe_socks=False, domain_cache=False)
        self.assertEqual(
            after["smart_router.direct_connect_failures_total"] -
            before["smart_router.direct_connect_failures_total"], 1)
        self.assertEqual(
            after["smart_router.handoff_failures_total"],
            before["smart_router.handoff_failures_total"])
        self.assertEqual(after["smart_router.route_direct_total"], before["smart_router.route_direct_total"])
        remote.close.assert_called_once_with()
        smart_g.pipe_socks.add_socks.assert_not_called()

    def test_direct6_connect_and_pipe_failures_keep_the_same_semantics(self):
        def rejected_by_pipe(*args):
            smart_metrics.increment("smart_router.handoff_failures_total")
            raise smart_route.HandoffFail()

        for failure, expected_counter in (
                (smart_route.ConnectFail, "smart_router.direct_connect_failures_total"),
                (rejected_by_pipe, "smart_router.handoff_failures_total")):
            with self.subTest(failure=getattr(failure, "__name__", "HandoffFail")):
                before = sr_snapshot(pipe_socks=False, domain_cache=False)
                with mock.patch.object(smart_route, "do_direct", side_effect=failure), \
                        mock.patch.object(smart_route, "is_ipv6_ok", return_value=True):
                    smart_g.dns_query = types.SimpleNamespace(query=lambda *args: [b"2001:db8::1"])
                    smart_route.try_loop(
                        "test", ["direct6"], mock.Mock(), "example.test", 443, ("local", 1))
                after = sr_snapshot(pipe_socks=False, domain_cache=False)
                self.assertEqual(after[expected_counter] - before[expected_counter], 1)
                other_counter = (
                    "smart_router.handoff_failures_total"
                    if expected_counter == "smart_router.direct_connect_failures_total"
                    else "smart_router.direct_connect_failures_total")
                self.assertEqual(after[other_counter], before[other_counter])
                self.assertEqual(
                    after["smart_router.route_direct_total"],
                    before["smart_router.route_direct_total"])

    def test_rejected_pipe_publication_is_not_a_successful_direct_route(self):
        before = sr_snapshot(pipe_socks=False, domain_cache=False)
        sock = mock.Mock()
        remote = mock.Mock()
        remote.ip = "192.0.2.1"

        def reject_once(*args):
            smart_metrics.increment("smart_router.handoff_failures_total")
            return False

        smart_g.connect_manager = types.SimpleNamespace(get_conn=lambda *args: remote)
        smart_g.pipe_socks = types.SimpleNamespace(add_socks=mock.Mock(side_effect=reject_once))
        with mock.patch.object(smart_route, "is_ipv6_ok", return_value=False):
            smart_g.dns_query = types.SimpleNamespace(query=lambda *args: [b"192.0.2.1"])
            smart_route.try_loop("test", ["direct"], sock, "example.test", 443, ("local", 1))
        after = sr_snapshot(pipe_socks=False, domain_cache=False)
        self.assertEqual(
            after["smart_router.route_direct_total"], before["smart_router.route_direct_total"],
            "PipeSocks 明确拒绝接管时不得把请求计为 direct 成功")
        self.assertEqual(
            after["smart_router.handoff_failures_total"] -
            before["smart_router.handoff_failures_total"], 1,
            "同一次 PipeSocks 拒绝只能在 PipeSocks 边界计数一次")
        self.assertEqual(
            after["smart_router.direct_connect_failures_total"],
            before["smart_router.direct_connect_failures_total"])

    class Sock:
        def __init__(self):
            self._sock = types.SimpleNamespace(socket_closed=False)
            self.buf_size = 7
            self.closed = False

        def setblocking(self, value):
            pass

        def close(self):
            self.closed = True

    def make_pipe(self):
        pipe = pipe_socks.PipeSocks.__new__(pipe_socks.PipeSocks)
        pipe.read_set = {self.Sock()}
        pipe.write_set = {self.Sock()}
        pipe.running = True
        pipe.sock_lock = threading.Lock()
        pipe.sock_notify = threading.Condition(pipe.sock_lock)
        return pipe

    def test_pipe_gauges_lock_unavailable_and_stopped_handoff(self):
        pipe = self.make_pipe()
        result = sr_snapshot(pipe_socks=pipe, domain_cache=False)
        self.assertEqual(result["smart_router.pipe_running"], 1)
        self.assertEqual(result["smart_router.pipe_read_sockets"], 1)
        self.assertEqual(result["smart_router.pipe_write_sockets"], 1)
        self.assertEqual(result["smart_router.pipe_write_buffer_bytes"], 7)

        pipe.sock_lock = BusyLock()
        pipe.sock_notify = threading.Condition(pipe.sock_lock)
        result = sr_snapshot(pipe_socks=pipe, domain_cache=False)
        self.assertEqual(
            result["smart_router.pipe_running"], 1,
            "socket 集合锁忙不得把明确的运行状态变成 NO_SAMPLE")
        self.assertIsNone(result["smart_router.pipe_read_sockets"])
        self.assertIsNone(result["smart_router.pipe_write_buffer_bytes"])

        pipe = self.make_pipe()
        pipe.running = False
        self.assertEqual(
            sr_snapshot(pipe_socks=pipe, domain_cache=False)["smart_router.pipe_running"], 0)
        before = sr_snapshot(pipe_socks=False, domain_cache=False)
        self.assertFalse(pipe.add_socks(self.Sock(), self.Sock()))
        after = sr_snapshot(pipe_socks=False, domain_cache=False)
        self.assertEqual(
            after["smart_router.pipe_controlled_closes_total"] -
            before["smart_router.pipe_controlled_closes_total"], 1)
        self.assertEqual(
            after["smart_router.handoff_failures_total"] -
            before["smart_router.handoff_failures_total"], 1)
        self.assertEqual(
            after["smart_router.pipe_stopped_handoffs_total"] -
            before["smart_router.pipe_stopped_handoffs_total"], 1)
        self.assertEqual(
            after["smart_router.pipe_register_failures_total"],
            before["smart_router.pipe_register_failures_total"])

    def test_register_and_closed_socket_rejections_have_distinct_single_counts(self):
        for scenario in ("register", "closed_socket"):
            with self.subTest(scenario=scenario):
                pipe = self.make_pipe()
                left = self.Sock()
                right = self.Sock()
                if scenario == "register":
                    pipe.try_add = mock.Mock(return_value=False)
                    pipe.try_remove = mock.Mock()
                else:
                    left._sock.socket_closed = True
                before = sr_snapshot(pipe_socks=False, domain_cache=False)

                self.assertFalse(pipe.add_socks(left, right))

                after = sr_snapshot(pipe_socks=False, domain_cache=False)
                self.assertEqual(
                    after["smart_router.handoff_failures_total"] -
                    before["smart_router.handoff_failures_total"], 1)
                for reason in ("register", "stopped", "closed_socket"):
                    name = "smart_router.pipe_%s%s_total" % (
                        reason, "_failures" if reason == "register" else "_handoffs")
                    expected = 1 if scenario == reason else 0
                    self.assertEqual(after[name] - before[name], expected)

    def test_pipe_stop_causes_are_counted_separately(self):
        for scenario in ("select", "loop"):
            with self.subTest(scenario=scenario):
                pipe = self.make_pipe()
                pending = self.Sock()
                pipe.read_set = {pending}
                pipe.write_set = set()
                pipe.close = lambda sock, reason: pipe.read_set.discard(sock)
                if scenario == "select":
                    pipe.select2 = types.SimpleNamespace(
                        select=lambda timeout: (_ for _ in ()).throw(OSError("selector failed")))
                else:
                    pending.is_closed = mock.Mock(side_effect=RuntimeError("controlled loop failure"))
                    pipe.select2 = types.SimpleNamespace(select=lambda timeout: [(
                        types.SimpleNamespace(fileobj=pending),
                        pipe_socks.selectors.EVENT_READ)])
                before = sr_snapshot(pipe_socks=False, domain_cache=False)

                pipe.pipe()

                after = sr_snapshot(pipe_socks=False, domain_cache=False)
                self.assertFalse(pipe.running)
                for reason in ("select", "loop"):
                    name = "smart_router.pipe_%s_failures_total" % reason
                    self.assertEqual(after[name] - before[name], 1 if scenario == reason else 0)


class FrontMetricsTest(unittest.TestCase):
    def make_dispatcher(self, category="direct"):
        dispatcher = http_dispatcher.HttpsDispatcher.__new__(http_dispatcher.HttpsDispatcher)
        dispatcher.metrics_category = category
        dispatcher.running = True
        dispatcher._state_lock = threading.Lock()
        dispatcher.workers = {"private-worker-key": object()}
        dispatcher.h1_num = 1
        dispatcher.h2_num = 2
        dispatcher.request_queue = queue.Queue()
        dispatcher.connection_manager = types.SimpleNamespace(
            new_conn_pool={"private-pool-key": object()},
            thread_num=3,
            thread_num_lock=threading.Lock(),
            total_no_ip_events=4,
        )
        return dispatcher

    def test_fixed_categories_rtt_and_sensitive_keys(self):
        dispatcher = self.make_dispatcher()
        front_metrics.register(dispatcher)
        self.addCleanup(front_metrics.unregister, dispatcher)
        front_metrics.increment("direct", "request_retries_total")
        front_metrics.add_rtt("direct", 0.25)
        result = gae_snapshot()["front.by_type"]

        self.assertEqual(set(result), set(front_metrics.CATEGORIES))
        expected_fields = set(SCHEMA["fields"]["front.by_type"]["value_fields"])
        for category in result.values():
            self.assertEqual(set(category), expected_fields)
        self.assertEqual(result["direct"]["workers"], 1)
        self.assertEqual(result["direct"]["h1_connections"], 1)
        self.assertEqual(result["direct"]["h2_connections"], 2)
        self.assertEqual(result["direct"]["connection_pools"], 1)
        self.assertEqual(result["direct"]["connections_creating"], 3)
        self.assertEqual(result["direct"]["no_available_ip_total"], 4)
        self.assertGreaterEqual(result["direct"]["request_retries_total"], 1)
        self.assertGreaterEqual(result["direct"]["rtt_seconds_total"], 0.25)
        encoded = json.dumps(result)
        self.assertNotIn("private-worker-key", encoded)
        self.assertNotIn("private-pool-key", encoded)
        with self.assertRaises(ValueError):
            front_metrics.increment("private-target.invalid", "request_retries_total")

    def test_front_lock_unavailable_returns_null_gauges(self):
        dispatcher = self.make_dispatcher("gae")
        dispatcher._state_lock = BusyLock()
        dispatcher.connection_manager.thread_num_lock = BusyLock()
        front_metrics.register(dispatcher)
        self.addCleanup(front_metrics.unregister, dispatcher)
        result = gae_snapshot()["front.by_type"]["gae"]
        self.assertIsNone(result["workers"])
        self.assertIsNone(result["h1_connections"])
        self.assertIsNone(result["connections_creating"])

    def test_real_retry_response_failure_body_timeout_and_stale_attempt(self):
        dispatcher = self.make_dispatcher("cloudflare")
        dispatcher.fail_num = 0
        dispatcher.continue_fail_num = 0
        dispatcher.last_fail_time = 0
        dispatcher.retry_log_throttle = xlog.LogThrottle(60)
        dispatcher.logger = mock.Mock()
        front_metrics.register(dispatcher)
        self.addCleanup(front_metrics.unregister, dispatcher)
        before = gae_snapshot()["front.by_type"]["cloudflare"]
        task = mock.Mock()
        task.method = b"GET"
        task.host = b"example.test"
        task.path = b"/"
        task.worker = None
        task.responsed = False
        task.claim_retry_token.return_value = 1
        task.retry_count = 0
        task.start_time = __import__("time").time()
        task.timeout = 60
        dispatcher.retry_task_cb(task, "test", expected_attempt=1)
        after_retry = gae_snapshot()["front.by_type"]["cloudflare"]
        self.assertEqual(
            after_retry["request_retries_total"] - before["request_retries_total"], 1)

        def make_task():
            item = http_dispatcher.http_common.Task.__new__(http_dispatcher.http_common.Task)
            item._state_lock = threading.RLock()
            item.attempt_id = 1
            item.responsed = False
            item.finished = False
            item.logger = mock.Mock()
            item.queue = None
            item.worker = None
            item.url = "private-target.invalid"
            item.metrics_category = "cloudflare"
            item.finish = mock.Mock()
            return item

        make_task().response_fail("body timeout", expected_attempt=1)
        stale = make_task()
        stale.response_fail("late", expected_attempt=2)
        final = gae_snapshot()["front.by_type"]["cloudflare"]
        self.assertEqual(final["response_failures_total"] - before["response_failures_total"], 1)
        self.assertEqual(final["body_timeouts_total"] - before["body_timeouts_total"], 1)
        self.assertEqual(final["generation_drops_total"] - before["generation_drops_total"], 1)

    def test_dispatcher_requires_explicit_fixed_category_at_construction(self):
        self.assertIn("metrics_category", inspect_signature(http_dispatcher.HttpsDispatcher))
        for relative, category in (
                ("gae_proxy/local/front.py", "gae"),
                ("x_tunnel/local/cloudflare_front/front.py", "cloudflare"),
                ("x_tunnel/local/cloudfront_front/front.py", "cloudfront")):
            source = (APP / relative).read_text()
            self.assertIn('metrics_category="%s"' % category, source)
        source = (APP / "gae_proxy/local/front.py").read_text()
        self.assertIn('metrics_category="direct"', source)


def inspect_signature(cls):
    import inspect
    return inspect.signature(cls.__init__).parameters


class XlogMetricsTest(unittest.TestCase):
    def test_throttle_queue_and_process_fields_are_numeric_only(self):
        throttle = xlog.LogThrottle(60)
        before = xlog_snapshot(loggers=[])
        throttle.allow("fixed-test-key")
        throttle.allow("fixed-test-key")
        logger = types.SimpleNamespace(write_queue=queue.Queue())
        logger.write_queue.put(b"one")
        result = xlog_snapshot(loggers=[logger])
        expected = {name for name in SCHEMA["fields"] if name.startswith("process.log_")}
        self.assertEqual(set(result), expected)
        self.assertEqual(result["process.log_queue_length"], 1)
        self.assertEqual(
            result["process.log_throttled_total"] - before["process.log_throttled_total"], 1)
        self.assertIsInstance(result["process.log_dropped_total"], int)
        self.assertIsInstance(result["process.log_write_failures_total"], int)
        self.assertNotIn("fixed-test-key", json.dumps(result))


if __name__ == "__main__":
    unittest.main(verbosity=2)
