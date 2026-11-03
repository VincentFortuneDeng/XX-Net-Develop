#!/usr/bin/env python3
"""独立模型、随机输入与故障注入验收；不调用既有测试辅助函数。"""
import concurrent.futures
import importlib.util
import io
import itertools
import json
import os
from pathlib import Path
import queue
import random
import re
import socket
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'Coding/XX-Net-5.16.7.1/code/default'
LIB = APP / 'lib/noarch'
sys.path.insert(0, str(LIB))
import xlog

# 只建立命名空间，避免导入模块时执行整个应用启动流程。
for package_name, package_path in (
        ('smart_router', APP / 'smart_router'),
        ('smart_router.local', APP / 'smart_router/local'),
        ('x_tunnel', APP / 'x_tunnel'),
        ('x_tunnel.local', APP / 'x_tunnel/local')):
    package = types.ModuleType(package_name)
    package.__path__ = [str(package_path)]
    sys.modules[package_name] = package
for package_name in ('smart_router.local.global_var', 'x_tunnel.local.global_var'):
    global_var = types.ModuleType(package_name)
    global_var.config = types.SimpleNamespace(PROXY_ENABLE=False, country_code='CN',
        PROXY_TYPE='HTTP', PROXY_HOST='', PROXY_PORT=0, PROXY_USER='', PROXY_PASSWD='')
    global_var.local_ips = []
    sys.modules[package_name] = global_var
for package_name in ('gae_proxy', 'gae_proxy.local'):
    package = types.ModuleType(package_name)
    package.__path__ = []
    sys.modules[package_name] = package
sys.modules['gae_proxy.local.check_local_network'] = types.SimpleNamespace(IPv6=types.SimpleNamespace(is_ok=lambda: False))


class Collector:
    name = 'acceptance'

    def __init__(self):
        self.events = []

    def _add(self, level, fmt, *args, **kwargs):
        try:
            self.events.append((level, fmt % args))
        except Exception as error:
            self.events.append(('FORMAT_ERROR', repr(error)))

    debug = lambda self, *a, **k: self._add('DEBUG', *a, **k)
    info = lambda self, *a, **k: self._add('INFO', *a, **k)
    warn = lambda self, *a, **k: self._add('WARN', *a, **k)
    warning = warn
    error = lambda self, *a, **k: self._add('ERROR', *a, **k)
    exception = lambda self, *a, **k: self._add('ERROR', *a, **k)


def load_file(name, path, package=None):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    if package:
        module.__package__ = package
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class XlogIndependent(unittest.TestCase):
    def test_real_logger_accepts_non_utf8_business_fields(self):
        logger = xlog.Logger('bytes', buffer_size=10)
        with mock.patch.object(xlog.sys, 'stderr', new=io.StringIO()):
            logger.warn('retry path:%s', b'/download\xff?tid=1')
        self.assertIn('download', logger.get_last_lines(10))

    def test_random_unicode_roll_model(self):
        rng = random.Random(20261103)
        for trial in range(20):
            with tempfile.TemporaryDirectory() as tmp:
                logger = xlog.Logger('model', log_path=tmp, save_warning_log=True, buffer_size=10)
                logger.set_console_color = lambda color: None
                with mock.patch.object(xlog.sys, 'stderr', new=io.StringIO()):
                    logger.persistent_file_max_size = rng.randint(300, 1500)
                    payloads = []
                    for _ in range(50):
                        text = ''.join(rng.choice('ab中文🙂é') for _ in range(rng.randint(1, 300)))
                        payloads.append(text)
                        logger.warn('%s', text)
                if logger.persistent_fd:
                    logger.persistent_fd.close()
                main = Path(tmp, 'model.log').read_bytes()
                backup = Path(tmp, 'model.log.1').read_bytes()
                main.decode('utf-8')
                backup.decode('utf-8')
                self.assertGreater(len(payloads), 0)
                self.assertLessEqual(len(main), logger.persistent_file_max_size + 65536)
                self.assertFalse(Path(tmp, 'model.log.2').exists())
                self.assertTrue(any(p[:10] in main.decode('utf-8') for p in payloads[-3:]))

    def test_throttle_random_differential(self):
        rng = random.Random(3811)
        clock = [1000.0]
        throttle = xlog.LogThrottle(60)
        reference = {}
        with mock.patch.object(xlog.time, 'time', side_effect=lambda: clock[0]):
            for _ in range(5000):
                clock[0] += rng.choice((0, 0, 0.1, 1, 30, 60, 61))
                key = rng.randrange(11)
                now = clock[0]
                entry = reference.get(key)
                if entry is not None and now - entry[0] < 60:
                    entry[1] += 1
                    expected = (False, entry[1])
                else:
                    expected = (True, entry[1] if entry else 0)
                    reference[key] = [now, 0]
                self.assertEqual(expected, throttle.allow(key))
            expected_pending = sum(v[1] for v in reference.values() if v[1])
            self.assertEqual((expected_pending, sum(bool(v[1]) for v in reference.values())), throttle.flush())
            self.assertEqual((False, 0), throttle.allow(0))
            throttle.reset()
            self.assertEqual((True, 0), throttle.allow(0))

    def test_throttle_concurrent_conservation(self):
        throttle = xlog.LogThrottle(60)
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: throttle.allow('same'), range(4000)))
        self.assertEqual(1, sum(allowed for allowed, _ in results))
        self.assertEqual((3999, 1), throttle.flush())

    def test_open_failure_recovers_without_losing_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, 'not-created')
            logger = xlog.Logger('recover', buffer_size=10, log_path=str(target), save_warning_log=True)
            logger.debug('memory-only')
            self.assertIsNone(logger.persistent_fd)
            self.assertIn('memory-only', logger.get_last_lines(10))
            target.mkdir()
            logger.reset_log_files()
            with mock.patch.object(xlog.sys, 'stderr', new=io.StringIO()):
                logger.error('persisted-after-recovery')
            logger.persistent_fd.close()
            self.assertIn('persisted-after-recovery', (target / 'recover.log').read_text())

    def test_error_body_survives_context_and_stack_rolls(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = xlog.Logger('small', buffer_size=100, log_path=tmp, save_warning_log=True)
            logger.persistent_file_max_size = 700
            with mock.patch.object(xlog.sys, 'stderr', new=io.StringIO()):
                for index in range(40):
                    logger.debug('context-%d-%s', index, 'x' * 100)
                try:
                    raise RuntimeError('z' * 500)
                except RuntimeError:
                    logger.exception('unique-error-body')
            logger.persistent_fd.close()
            content = (Path(tmp, 'small.log.1').read_text() + Path(tmp, 'small.log').read_text())
            self.assertIn('unique-error-body', content)
            self.assertIn('Traceback', content)

    def test_export_incomplete_context_does_not_hide_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'broken.log')
            path.write_bytes(
                b'2026-11-03 00:00:00.000 - [ERROR] ERROR_CONTEXT_BEGIN\n'
                b'2026-11-03 00:00:00.001 - [DEBUG] before failure\n'
                b'2026-11-03 00:00:00.002 - [ERROR] actual database failure\n')
            logger = xlog.Logger('broken', log_path=tmp, save_warning_log=True)
            exported = logger.export_view('warning', 10000)
            if logger.persistent_fd:
                logger.persistent_fd.close()
            self.assertIn('actual database failure', exported)


class DnsIndependent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_file('acceptance_dns_query', APP / 'smart_router/local/dns_query.py', 'smart_router.local')

    def test_nameserver_classifier_against_ipaddress(self):
        import ipaddress
        rng = random.Random(99)
        samples = ['127.0.0.1', '8.8.8.8', '0.0.0.0', '224.0.0.1', '169.254.1.1', '255.255.255.255', '::1', '::', 'fe80::1', 'fc00::1', 'ff02::1', '2001:4860:4860::8888', '3fff::1']
        samples += [str(ipaddress.ip_address(rng.getrandbits(128))) for _ in range(1000)]
        for value in samples:
            ip = ipaddress.ip_address(value.split('%')[0])
            expected = ip.version == 4 and not (ip.is_multicast or ip.is_link_local or ip.is_unspecified) and value not in ('127.0.0.1', '255.255.255.255') or ip.version == 6 and ip in ipaddress.ip_network('2000::/3')
            self.assertEqual(expected, self.module.LocalDnsQuery._is_usable_nameserver(value), value)

    def make_query(self):
        obj = self.module.LocalDnsQuery.__new__(self.module.LocalDnsQuery)
        obj.dns_server = [b'a', b'b', b'c']
        obj.ns_backoff = {}
        obj.ns_backoff_lock = threading.Lock()
        obj.timeout = 0
        obj.waiters = {}
        return obj

    def test_backoff_query_differential(self):
        obj = self.make_query()
        now = [1000.0]
        sent = []
        obj.send_request = lambda _id, server, _domain, _type: sent.append(server)
        with mock.patch.object(self.module.time, 'time', side_effect=lambda: now[0]):
            obj.ns_backoff = {b'a': 1030, b'b': 1010, b'c': 1050}
            obj.query('example', timeout=0)
            self.assertEqual([b'b'], sent, '全冷却只允许最早到期者 fail-open')
            sent.clear()
            now[0] = 1011
            obj.query('example', timeout=0)
            self.assertEqual([b'b'], sent)

    def test_close_failure_does_not_replace_connect_handling(self):
        obj = self.module.DnsOverTcpQuery()
        sock = mock.Mock()
        sock.connect.side_effect = OSError(65, 'original')
        sock.close.side_effect = RuntimeError('close failed')
        with mock.patch.object(self.module.socket, 'socket', return_value=sock), \
             mock.patch.object(self.module.utils, 'check_ip_valid4', return_value=True):
            self.assertIsNone(obj.direct_connect(b'8.8.8.8', 53))

    def test_dot_failures_close_once_and_preserve_exception(self):
        obj = self.module.DnsOverTcpQuery()
        for error in (OSError(65, 'no route'), RuntimeError('unexpected')):
            sock = mock.Mock()
            sock.connect.side_effect = error
            with mock.patch.object(self.module.socket, 'socket', return_value=sock), \
                 mock.patch.object(self.module.utils, 'check_ip_valid4', return_value=True):
                self.assertIsNone(obj.direct_connect(b'8.8.8.8', 53))
            self.assertEqual(1, sock.close.call_count)
        with mock.patch.object(self.module.g.config, 'PROXY_ENABLE', True), \
             mock.patch.dict(sys.modules, {'socks': types.SimpleNamespace(socksocket=lambda *_: fake)}):
            fake = mock.Mock()
            failure = RuntimeError('proxy failed')
            fake.connect.side_effect = failure
            with self.assertRaisesRegex(RuntimeError, 'proxy failed'):
                obj.connect(b'8.8.8.8', 53)
            fake.close.assert_called_once_with()


class SocketAndSelectorIndependent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_file('acceptance_pipe_socks', APP / 'smart_router/local/pipe_socks.py', 'smart_router.local')

    def make_pipe(self, selector):
        pipe = self.module.PipeSocks.__new__(self.module.PipeSocks)
        pipe.select2 = selector
        pipe.read_set = set()
        pipe.write_set = set()
        pipe.blockio_log_times = {}
        return pipe

    def test_selector_race_leaves_no_stale_set(self):
        sock = mock.Mock()
        sock.fileno.return_value = 8
        selector = mock.Mock()
        selector.unregister.side_effect = KeyError('already removed')
        pipe = self.make_pipe(selector)
        pipe.read_set.add(sock)
        pipe.try_remove('READ', sock)
        self.assertNotIn(sock, pipe.read_set)

    def test_unknown_selector_error_remains_exception(self):
        sock = mock.Mock()
        sock.fileno.return_value = 8
        selector = mock.Mock()
        selector.register.side_effect = RuntimeError('real selector failure')
        pipe = self.make_pipe(selector)
        with mock.patch.object(self.module.xlog, 'exception') as error:
            pipe.try_add('READ', sock)
        error.assert_called_once()

    def test_real_nonblocking_socket_and_final_account(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        left.setblocking(False)
        pipe = self.make_pipe(mock.Mock())
        with mock.patch.object(self.module.xlog, 'warn') as warn:
            for _ in range(20):
                try:
                    left.send(b'x' * 65536)
                except BlockingIOError as error:
                    pipe.warn_send_block(left, error)
            pipe.blockio_log_times[id(left)] = (time.time(), 7)
            pending = pipe.blockio_log_times.pop(id(left))
            self.assertEqual(7, pending[1])


class FrontModelsIndependent(unittest.TestCase):
    def test_no_ip_independent_episode_model(self):
        module = load_file('acceptance_connect_manager', LIB / 'front_base/connect_manager.py', 'front_base')
        obj = module.ConnectManager.__new__(module.ConnectManager)
        obj.config = types.SimpleNamespace(show_state_debug=False, connect_create_interval=0)
        obj.no_ip_lock = threading.Lock()
        obj.no_ip_time = 0
        obj.last_no_ip_warn = 0
        obj.suppressed_no_ip_warn = 0
        obj.total_no_ip_events = 0
        obj.new_conn_pool = mock.Mock()
        obj._create_ssl_connection = mock.Mock(return_value=None)
        clock = [1000.0]
        for no_ip_count in (1, 2, 17, 120):
            with mock.patch.object(module.time, 'time', side_effect=lambda: clock[0]), \
                 mock.patch.object(module.time, 'sleep', return_value=None):
                logger = Collector()
                obj.logger = logger
                obj.ip_manager = types.SimpleNamespace(get_ip_sni_host=lambda: None)
                for _ in range(no_ip_count):
                    obj._connect_process()
                clock[0] += 61
                obj.ip_manager = types.SimpleNamespace(get_ip_sni_host=lambda: {'ip_str': '1.1.1.1', 'sni': None, 'host': None})
                obj._connect_process()
                recovered = [text for level, text in logger.events if 'recovered' in text]
                self.assertEqual(1, len(recovered))
                self.assertIn('total %d' % no_ip_count, recovered[0])
                self.assertEqual((0, 0), (obj.last_no_ip_warn, obj.total_no_ip_events))

    def test_connect_ssl_all_inputs_returned(self):
        module = load_file('acceptance_connect_manager_ssl', LIB / 'front_base/connect_manager.py', 'front_base')
        obj = module.ConnectManager.__new__(module.ConnectManager)
        obj.config = types.SimpleNamespace(show_state_debug=False)
        obj.logger = Collector()
        obj.connect_ssl_log_throttle = xlog.LogThrottle(60)
        rng = random.Random(816)
        for _ in range(500):
            ip = '.'.join(str(rng.randrange(256)) for _ in range(4))
            result = RuntimeError(str(rng.random())) if rng.randrange(2) else object()
            obj.connect_creator = types.SimpleNamespace(connect_ssl=mock.Mock(side_effect=result) if isinstance(result, Exception) else mock.Mock(return_value=result))
            output = queue.Queue()
            obj._connect_ssl(ip, None, None, None, output)
            self.assertIs(result, output.get_nowait())

    def test_task_key_random_differential_and_none_url(self):
        module = load_file('acceptance_http_dispatcher', LIB / 'front_base/http_dispatcher.py', 'front_base')
        obj = module.HttpsDispatcher.__new__(module.HttpsDispatcher)
        obj.retry_log_throttle = xlog.LogThrottle(60)
        rng = random.Random(10)
        for _ in range(1000):
            path = rng.choice((b'/a', b'/a?x=1', b'/b\xff?z=2'))
            host = rng.choice((b'example', None))
            worker = types.SimpleNamespace(ip_str=rng.choice(('1.1.1.1', '2.2.2.2')))
            task = types.SimpleNamespace(path=path, host=host, method=b'GET', worker=worker)
            expected = (b'GET', host, module.utils.to_bytes(path).split(b'?', 1)[0], worker.ip_str)
            self.assertEqual(expected, obj._task_log_key(task))

    def test_receive_pool_random_business_model(self):
        module = load_file('acceptance_base_container', APP / 'x_tunnel/local/base_container.py', 'x_tunnel.local')
        logger = Collector()
        pool = module.BlockReceivePool(lambda data: None, logger)
        rng = random.Random(883)
        reference = {}
        for _ in range(2000):
            sn = rng.randrange(1, 30)
            t = rng.randrange(100)
            old = reference.get(sn)
            if old is None or t > old:
                reference[sn] = t
            pool.mark_sn_timeout(sn, t, 1000)
            self.assertEqual(reference, {key: value['server_send_time'] for key, value in pool.timeout_sn_list.items()})
        tail = pool.reset(defer_log=True)
        self.assertEqual({}, pool.timeout_sn_list)
        self.assertEqual(1, pool.next_sn)
        self.assertEqual(2, len(tail))
        pool.mark_sn_timeout(1, 1, 2)
        self.assertIn(1, pool.timeout_sn_list)


class NetworkEdgeIndependent(unittest.TestCase):
    def test_reserved_frag_and_malformed_packets(self):
        module = load_file('acceptance_dns_server', APP / 'smart_router/local/dns_server.py', 'smart_router.local')
        server = module.DnsServer.__new__(module.DnsServer)
        server.dns_query = mock.Mock(return_value=b'answer')
        sender = mock.Mock()
        with mock.patch.object(module.xlog, 'warn') as warn:
            server.on_udp_relay(sender, b'\x00\x01\x00', ('127.0.0.1', 1))
            server.on_udp_relay(sender, b'\x00\x00\x01', ('127.0.0.1', 1))
        warn.assert_not_called()
        server.dns_query.assert_not_called()
        with mock.patch.object(module.xlog, 'exception') as error:
            server.on_udp_relay(sender, b'\x00', ('127.0.0.1', 1))
        error.assert_called_once()

    def test_ipv6_probe_and_selection_model(self):
        module = load_file('acceptance_ip_manager', APP / 'x_tunnel/local/tls_relay_front/ip_manager.py', 'x_tunnel.local.tls_relay_front')
        for local, expected in [('::', False), ('::1', False), ('fe80::1', False), ('fc00::1', False), ('2001:db8::1', True), ('3fff::1', True), ('4000::1', False)]:
            sock = mock.Mock()
            sock.getsockname.return_value = (local, 0, 0, 0)
            module.ipv6_available.cached_until = 0
            with mock.patch.object(module.socket, 'socket', return_value=sock):
                self.assertEqual(expected, module.ipv6_available())
            sock.close.assert_called_once_with()
        manager = module.IpManager.__new__(module.IpManager)
        manager.config = types.SimpleNamespace(use_ipv6='auto', max_links_per_ip=10,
                                               ip_cal_expect_time_package_size=40000)
        manager.host_manager = types.SimpleNamespace(info={
            '2001:db8::1': {'sni': None}, '1.1.1.1': {'sni': None}})
        manager.ip_dict = {}
        manager.logger = Collector()
        manager.get_speed = lambda ip: (1, 1)
        with mock.patch.object(module, 'ipv6_available', return_value=False):
            selected = manager.get_ip_sni_host()
        self.assertEqual('1.1.1.1:443', selected['ip_str'])
        self.assertNotIn('2001:db8::1', manager.ip_dict)

    def test_head_three_exits_share_front_account(self):
        module = load_file('acceptance_http1', LIB / 'front_base/http1.py', 'front_base')
        module.Http1Worker.__del__ = lambda self: None
        worker = module.Http1Worker.__new__(module.Http1Worker)
        worker.logger = Collector()
        worker.request_onway = False
        worker.ip_str = 'front-a'
        worker.record_active = lambda *_: None
        worker.get_trace = lambda: 'trace'
        module.Http1Worker.head_fail_throttle.reset()
        outcomes = []
        for mode in ('short', 'status', 'exception'):
            sock = types.SimpleNamespace(host='example', ip_str='front-a')
            if mode == 'short':
                sock.send = lambda data: len(data) - 1
            elif mode == 'status':
                sock.send = lambda data: len(data)
            else:
                sock.send = mock.Mock(side_effect=OSError('closed'))
            worker.ssl_sock = sock
            response = types.SimpleNamespace(status=503, begin=lambda timeout: None)
            with mock.patch.object(module.simple_http_client, 'Response', return_value=response):
                outcomes.append(worker.head_request())
            self.assertFalse(worker.request_onway)
        self.assertEqual([False, False, False], outcomes)
        self.assertEqual(2, len(worker.logger.events), '同 front 仅首报，后续出口共享聚合账本')

    def test_transfer_event_uses_captured_session(self):
        module = load_file('acceptance_proxy_session', APP / 'x_tunnel/local/proxy_session.py', 'x_tunnel.local')
        session = module.ProxySession.__new__(module.ProxySession)
        session.transfer_log_throttle = xlog.LogThrottle(60)
        with mock.patch.object(module, 'xlog') as logger:
            for _ in range(3):
                session.transfer_warn(b'old', 7, 'roundtrip_status', 'status')
                session.transfer_warn(b'new', 7, 'roundtrip_status', 'status')
        self.assertEqual(2, logger.warn.call_count)
        self.assertEqual(2, session.transfer_log_throttle.flush()[1])

    def test_front_retry_full_path_and_lifecycle(self):
        module = load_file('acceptance_front_dispatcher', APP / 'x_tunnel/local/front_dispatcher.py', 'x_tunnel.local')
        module.front_retry_log_throttle.reset()
        for path in ('/a?tid=1', '/a?tid=2', '/b?tid=1'):
            for _ in range(3):
                module.front_retry_log_throttle.allow(('host', path))
        self.assertEqual((6, 3), module.front_retry_log_throttle.flush())
        module.front_retry_log_throttle.reset()
        module.front_retry_log_throttle.allow(('host', '/late'))
        module.front_retry_log_throttle.allow(('host', '/late'))
        module.all_fronts = []
        module.light_fronts = []
        module.session_fronts = []
        with mock.patch.object(module, 'xlog') as logger:
            module.stop()
            self.assertEqual(1, logger.warn.call_count)
        self.assertEqual((False, 0), module.front_retry_log_throttle.allow(('host', '/late')))


class LifecycleIndependent(unittest.TestCase):
    def test_front_stop_failure_still_seals(self):
        module = load_file('acceptance_front_stop_failure', APP / 'x_tunnel/local/front_dispatcher.py', 'x_tunnel.local')
        module.all_fronts = [types.SimpleNamespace(stop=mock.Mock(side_effect=RuntimeError('front cleanup')))]
        module.light_fronts = []
        module.session_fronts = []
        module.front_retry_log_throttle.reset()
        module.front_retry_log_throttle.allow('key')
        module.front_retry_log_throttle.allow('key')
        with mock.patch.object(module, 'xlog'), self.assertRaisesRegex(RuntimeError, 'front cleanup'):
            module.stop()
        self.assertEqual((0, 0), module.front_retry_log_throttle.flush())

    def test_dispatcher_stop_failure_still_seals(self):
        module = load_file('acceptance_dispatcher_stop_failure', LIB / 'front_base/http_dispatcher.py', 'front_base')
        dispatcher = module.HttpsDispatcher.__new__(module.HttpsDispatcher)
        dispatcher.running = True
        dispatcher.request_queue = mock.Mock()
        dispatcher.close_all_worker = mock.Mock(side_effect=RuntimeError('worker cleanup'))
        dispatcher.retry_log_throttle = xlog.LogThrottle(60)
        dispatcher.retry_log_throttle.allow('key')
        dispatcher.retry_log_throttle.allow('key')
        dispatcher.logger = Collector()
        with self.assertRaisesRegex(RuntimeError, 'worker cleanup'):
            dispatcher.stop()
        self.assertEqual((0, 0), dispatcher.retry_log_throttle.flush())

    def test_session_stop_failure_still_seals_once(self):
        module = load_file('acceptance_proxy_session_stop', APP / 'x_tunnel/local/proxy_session.py', 'x_tunnel.local')
        module.g.config.concurent_thread_num = 0
        for failing in ('send_buffer', 'receive_process', 'wait_queue', 'connection_pipe'):
            session = module.ProxySession.__new__(module.ProxySession)
            session.running = True
            session.lock = threading.Lock()
            session.wait_queue = types.SimpleNamespace(notify=lambda: None, stop=lambda: None)
            session.send_buffer = types.SimpleNamespace(reset=lambda: None)
            session.receive_process = types.SimpleNamespace(reset=lambda defer_log: (1, 1), log_reset=lambda *args: None)
            session.connection_pipe = types.SimpleNamespace(stop=lambda: None)
            session.close_all_connection = lambda: None
            session.transfer_log_throttle = xlog.LogThrottle(60)
            session.transfer_log_throttle.allow('old')
            session.transfer_log_throttle.allow('old')
            setattr(getattr(session, failing), 'stop' if failing != 'send_buffer' else 'reset',
                    mock.Mock(side_effect=RuntimeError(failing)) if failing != 'receive_process' else None)
            if failing == 'receive_process':
                session.receive_process.reset = mock.Mock(side_effect=RuntimeError(failing))
            with mock.patch.object(module, 'xlog'), self.assertRaisesRegex(RuntimeError, failing):
                session.stop()
            self.assertEqual((0, 0), session.transfer_log_throttle.flush())
            session.stop()


class StartupFailureIndependent(unittest.TestCase):
    def test_all_stop_failure_combinations(self):
        source = (APP / 'smart_router/local/__init__.py').read_text()
        start = source.index('def start(args):')
        stop = source.index('\ndef stop():', start)
        function_source = source[start:stop]
        for mask in range(16):
            calls = []
            class Resource:
                listen_port = 53
                def __init__(self, name): self.name = name
                def run(self): pass
                def start(self): pass
                def stop(self):
                    calls.append(self.name)
                    if mask & (1 << ['dns_srv', 'dns_query', 'pipe_socks', 'connect_manager'].index(self.name)):
                        raise RuntimeError(self.name)
            g = types.SimpleNamespace(
                config=types.SimpleNamespace(proxy_bind_ip='127.0.0.1', proxy_port=8086,
                    dns_bind_ip='127.0.0.1', dns_port=53, dns_backup_port=8053, dns_ttl=60,
                    dns_cache_size=200, ip_cache_size=1000, pip_cache_size=32768),
                local_ips=[], dns_query=Resource('dns_query'), dns_srv=Resource('dns_srv'),
                pipe_socks=Resource('pipe_socks'), connect_manager=Resource('connect_manager'))
            class BadServer:
                def __init__(self, *args, **kwargs): pass
                def start(self): raise OSError('listen failed')
            namespace = dict(os=os, data_path=tempfile.gettempdir(), proc_handler={'launcher': object()}, g=g, gfwlist=mock.Mock(), ip_region=mock.Mock(),
                host_records=mock.Mock(), user_rules=mock.Mock(),
                connect_manager=types.SimpleNamespace(load_proxy_config=lambda: None,
                    ConnectManager=lambda: Resource('connect_manager')),
                pipe_socks=types.SimpleNamespace(PipeSocks=lambda *_: Resource('pipe_socks')),
                load_config=lambda: None,
                dns_query=types.SimpleNamespace(get_local_ips=lambda: [],
                    CombineDnsQuery=lambda: Resource('dns_query')),
                dns_server=types.SimpleNamespace(DnsServer=lambda **_: Resource('dns_srv')),
                simple_http_server=types.SimpleNamespace(HTTPServer=BadServer),
                proxy_handler=mock.Mock(), xlog=Collector(), ready=True)
            exec(function_source, namespace)
            with self.assertRaisesRegex(OSError, 'listen failed'):
                namespace['start']({})
            self.assertEqual(['dns_srv', 'dns_query', 'pipe_socks', 'connect_manager'], calls)
            self.assertFalse(namespace['ready'])


class DiagnosticPackIndependent(unittest.TestCase):
    def setUp(self):
        self.module = load_file('acceptance_upload_logs', APP / 'x_tunnel/local/upload_logs.py', 'x_tunnel.local')

    def test_malformed_client_config_never_returns_secret(self):
        for raw in ('{"login_password":"top-secret"', '["top-secret"]', 'null'):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, 'client.json')
                path.write_text(raw)
                with mock.patch.object(self.module, 'xlog') as logger:
                    sanitized = self.module.mask_x_tunnel_password(str(path))
                self.assertNotIn('top-secret', sanitized)
                self.assertNotIn('top-secret', repr(logger.mock_calls))

    def test_random_zip_reference_and_password(self):
        rng = random.Random(227)
        for trial in range(30):
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                expected = {}
                for name in ('launcher/config.json', 'x_tunnel/client.json', 'x_tunnel/x_tunnel.log', 'x_tunnel/x_tunnel.log.1', 'downloads/launcher_log.txt', 'old_warning.log'):
                    path = base / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    data = rng.randbytes(rng.randrange(0, 3000))
                    if name.endswith('client.json'):
                        data = json.dumps({'login_password': 'never-upload', 'other': 'keep'}).encode()
                    path.write_bytes(data)
                    if name != 'old_warning.log':
                        expected[name] = data
                self.module.data_path = tmp
                self.module.collect_debug_and_log = lambda: None
                views = {'views/a.log': rng.randbytes(rng.randrange(1000)) for _ in range(3)}
                self.module.collect_log_views = lambda: views.copy()
                expected.update(views)
                budget = rng.randrange(500, 12000)
                raw = self.module.pack_logs(budget)
                self.assertIsNotNone(raw)
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    names = archive.namelist()
                    self.assertEqual(len(names), len(set(names)))
                    self.assertLessEqual(sum(item.file_size for item in archive.infolist()), budget)
                    self.assertNotIn('old_warning.log', names)
                    for name in names:
                        content = archive.read(name)
                        self.assertIn(name, expected)
                        if name.endswith('client.json'):
                            self.assertNotIn(b'never-upload', content)
                            self.assertEqual('keep', json.loads(content)['other'])
                        elif not name.endswith('client.json'):
                            self.assertEqual(expected[name], content)

    def test_single_view_failure_omits_only_that_view(self):
        calls = []
        def request(_method, url, timeout):
            calls.append(url)
            if 'warning' in url:
                raise OSError('injected')
            return types.SimpleNamespace(text='ok')
        self.module.get_launcher_port = lambda: 8085
        self.module.get_module_enable_flags = lambda port: {'gae_proxy': False, 'x_tunnel': False, 'smart_router': True}
        with mock.patch.object(self.module.simple_http_client, 'request', side_effect=request):
            views = self.module.collect_log_views()
        self.assertEqual({'views/launcher_startup.log', 'views/smart_router_startup.log'}, set(views))
        self.assertEqual(4, len(calls))


if __name__ == '__main__':
    unittest.main(verbosity=2)
