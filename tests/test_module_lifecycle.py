"""模块启动、监听发布与连接池停止边界的离线回归测试。"""
import ast
import importlib.util
from pathlib import Path
import queue
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "Coding" / "XX-Net-5.16.7.1" / "code" / "default"
NOARCH = APP / "lib" / "noarch"
sys.path.insert(0, str(NOARCH))


class Collector:
    name = "lifecycle-test"

    def __init__(self):
        self.records = []

    def _record(self, *args, **kwargs):
        self.records.append(args)

    debug = info = warn = warning = error = exception = _record

    def set_buffer(self, size):
        pass


class Throttle:
    def allow(self, key):
        return True, 0

    def flush(self):
        return 0, 0


def namespace(name, path=None, **attributes):
    module = types.ModuleType(name)
    if path is not None:
        module.__path__ = [str(path)]
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class LauncherLifecycleTest(unittest.TestCase):
    def test_background_start_failure_is_returned_and_retryable(self):
        logger = Collector()
        config = types.SimpleNamespace(all_modules=["demo"])
        namespace("config", config=config)
        namespace("xlog", getLogger=lambda *args, **kwargs: logger)
        namespace("web_control")
        namespace("env_info", data_path=tempfile.gettempdir())
        module_init = load_module("module_init", APP / "launcher" / "module_init.py")

        class Local:
            def __init__(self):
                self.ready = False
                self.fail = True
                self.stop_calls = 0
                self.received_args = []

            def start(self, args):
                self.received_args.append(args)
                if self.fail:
                    raise RuntimeError("start failed")
                self.ready = True

            def is_ready(self):
                return self.ready

            def stop(self):
                self.stop_calls += 1
                self.ready = False

        local = Local()
        namespace("demo", local=local)
        with tempfile.TemporaryDirectory() as root:
            demo = Path(root) / "demo"
            demo.mkdir()
            (demo / "__init__.py").write_text("")
            module_init.root_path = root

            result = []
            starter = threading.Thread(target=lambda: result.append(module_init.start("demo")), daemon=True)
            starter.start()
            starter.join(1)

            self.assertFalse(starter.is_alive(), "后台启动异常后仍无限等待 ready")
            self.assertEqual(result, ["fail"])
            self.assertNotIn("demo", module_init.proc_handler)
            self.assertEqual(local.stop_calls, 1)

            local.fail = False
            self.assertEqual(module_init.start("demo"), "start success.")
            self.assertIn("demo", module_init.proc_handler)
            self.assertEqual(local.received_args, [module_init.xargs, module_init.xargs])
            self.assertTrue(all(args is module_init.xargs for args in local.received_args),
                            "launcher 必须把选项字典原样交给模块，不能再包一层列表")

    def test_stop_during_start_cannot_leave_unregistered_ready_module(self):
        logger = Collector()
        config = types.SimpleNamespace(all_modules=["demo"])
        namespace("config", config=config)
        namespace("xlog", getLogger=lambda *args, **kwargs: logger)
        namespace("web_control")
        namespace("env_info", data_path=tempfile.gettempdir())
        module_init = load_module("module_init", APP / "launcher" / "module_init.py")

        class Local:
            def __init__(self):
                self.ready = False
                self.start_entered = threading.Event()
                self.release_start = threading.Event()
                self.stop_called = threading.Event()

            def start(self, args):
                self.start_entered.set()
                if not self.release_start.wait(3):
                    raise RuntimeError("test start gate timed out")
                self.ready = True

            def is_ready(self):
                return self.ready

            def stop(self):
                self.ready = False
                self.stop_called.set()

        local = Local()
        namespace("demo", local=local)
        with tempfile.TemporaryDirectory() as root:
            demo = Path(root) / "demo"
            demo.mkdir()
            (demo / "__init__.py").write_text("")
            module_init.root_path = root
            self.addCleanup(module_init.proc_handler.pop, "demo", None)
            self.addCleanup(local.stop)

            start_result = []
            stop_result = []
            thread_errors = []
            stop_requested = threading.Event()
            stop_done = threading.Event()
            stopped_before_release = threading.Event()

            def start_module():
                try:
                    start_result.append(module_init.start("demo"))
                except Exception as e:
                    thread_errors.append(e)

            def stop_module():
                stop_requested.set()
                try:
                    stop_result.append(module_init.stop("demo"))
                except Exception as e:
                    thread_errors.append(e)
                finally:
                    stop_done.set()

            def release_start():
                stop_requested.wait(1)
                # 旧实现必须先完成停止；串行实现则由超时分支放行启动，再允许停止。
                if local.stop_called.wait(0.25) and stop_done.wait(1):
                    stopped_before_release.set()
                local.release_start.set()

            starter = threading.Thread(target=start_module)
            stopper = threading.Thread(target=stop_module)
            releaser = threading.Thread(target=release_start)
            starter.start()
            self.assertTrue(local.start_entered.wait(1))
            stopper.start()
            releaser.start()
            starter.join(4)
            stopper.join(4)
            releaser.join(4)
            local.release_start.set()

            self.assertEqual(thread_errors, [])
            self.assertFalse(starter.is_alive(), "模块启动未在事件门放行后结束")
            self.assertFalse(stopper.is_alive(), "模块停止未在启动结束后返回")
            self.assertEqual(start_result, ["start success."])
            self.assertEqual(stop_result, ["stop success."])
            self.assertTrue(stopped_before_release.is_set() or not local.ready)
            self.assertFalse(local.ready and "demo" not in module_init.proc_handler,
                             "停止删除注册表后，旧启动又发布 ready，形成无法再由 launcher 停止的孤儿模块")

    def test_module_api_iteration_tolerates_concurrent_stop(self):
        logger = Collector()
        config = types.SimpleNamespace(all_modules=["first", "second"])
        namespace("config", config=config)
        namespace("xlog", getLogger=lambda *args, **kwargs: logger)
        namespace("web_control")
        namespace("env_info", data_path=tempfile.gettempdir())
        module_init = load_module("module_init", APP / "launcher" / "module_init.py")
        entered = threading.Event()
        release = threading.Event()
        calls = []

        class Local:
            def __init__(self, name):
                self.name = name
                self.ready = True
                self.apis = self

            def is_ready(self):
                return self.ready

            def stop(self):
                self.ready = False

            def update(self, args):
                calls.append(self.name)
                if self.name == "first":
                    entered.set()
                    if not release.wait(2):
                        raise RuntimeError("test API gate timed out")

        first = Local("first")
        second = Local("second")
        namespace("first", local=first)
        namespace("second", local=second)
        with tempfile.TemporaryDirectory() as root:
            for name in ("first", "second"):
                module = Path(root) / name
                module.mkdir()
                (module / "__init__.py").write_text("")
            module_init.root_path = root
            module_init.proc_handler.update({
                "first": {"imp": sys.modules["first"]},
                "second": {"imp": sys.modules["second"]},
            })
            self.addCleanup(module_init.proc_handler.clear)
            self.addCleanup(release.set)
            errors = []

            def call_api():
                try:
                    module_init.call_each_module("update", {})
                except Exception as e:
                    errors.append(e)

            caller = threading.Thread(target=call_api)
            caller.start()
            self.assertTrue(entered.wait(1))
            self.assertEqual(module_init.stop("second"), "stop success.")
            release.set()
            caller.join(2)

            self.assertFalse(caller.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(calls, ["first"])
            self.assertNotIn("second", module_init.proc_handler)

    def test_web_control_stop_before_server_creation_is_a_noop(self):
        path = APP / "launcher" / "web_control.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        stop_node = next(node for node in tree.body
                         if isinstance(node, ast.FunctionDef) and node.name == "stop")
        namespace_dict = {"server": None, "xlog": Collector()}
        module = ast.Module(body=[stop_node], type_ignores=[])
        exec(compile(module, str(path), "exec"), namespace_dict)

        errors = []
        try:
            namespace_dict["stop"]()
        except Exception as e:
            errors.append(e)

        self.assertEqual(errors, [])
        server = mock.Mock()
        namespace_dict["server"] = server
        namespace_dict["stop"]()
        server.shutdown.assert_called_once_with()

    def test_x_tunnel_toggle_restarts_running_smart_router(self):
        path = APP / "launcher" / "web_control.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        handler_node = next(node for node in tree.body
                            if isinstance(node, ast.ClassDef) and node.name == "Http_Handler")
        config_node = next(node for node in handler_node.body
                           if isinstance(node, ast.FunctionDef) and node.name == "req_config_handler")
        module = ast.Module(body=[config_node], type_ignores=[])
        namespace_dict = {}
        exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace_dict)
        req_config_handler = namespace_dict["req_config_handler"]

        for enabled in (1, 0):
            for smart_router_enabled in (True, False):
                with self.subTest(enabled=enabled, smart_router_enabled=smart_router_enabled):
                    calls = []
                    config = types.SimpleNamespace(
                        enable_x_tunnel=1 - enabled,
                        enable_smart_router=smart_router_enabled,
                        save=lambda: calls.append(("save",)),
                    )
                    module_init = types.SimpleNamespace(
                        start=lambda name: calls.append(("start", name)),
                        stop=lambda name: calls.append(("stop", name)),
                    )
                    handler = types.SimpleNamespace(
                        path="/config?cmd=set_config&x_tunnel_enable=%d" % enabled,
                        unpack_reqs=lambda values: {key: value[0] for key, value in values.items()},
                        load_module_menus=lambda: calls.append(("menus",)),
                        send_response=lambda *args: calls.append(("response",)),
                    )
                    with mock.patch.dict(namespace_dict, {
                        "urlparse": __import__("urllib.parse", fromlist=["urlparse"]).urlparse,
                        "parse_qs": __import__("urllib.parse", fromlist=["parse_qs"]).parse_qs,
                        "config": config,
                        "module_init": module_init,
                    }):
                        req_config_handler(handler)

                    expected = [
                        ("save",),
                        ("start" if enabled else "stop", "x_tunnel"),
                    ]
                    if smart_router_enabled:
                        expected.extend([
                            ("stop", "smart_router"),
                            ("start", "smart_router"),
                        ])
                    expected.extend([("menus",), ("response",)])
                    self.assertEqual(calls, expected)

    def test_smart_router_start_clears_stopped_x_tunnel_reference(self):
        path = APP / "smart_router" / "local" / "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        start_node = next(node for node in tree.body
                          if isinstance(node, ast.FunctionDef) and node.name == "start")
        module = ast.Module(body=[start_node], type_ignores=[])

        class Resource:
            listen_port = 8053

            def __init__(self, *args, **kwargs):
                pass

            def run(self):
                pass

            def start(self):
                pass

        g = types.SimpleNamespace(
            x_tunnel=object(), x_tunnel_socks_port=1080,
            config=types.SimpleNamespace(
                dns_cache_size=200, ip_cache_size=1000, pip_cache_size=32,
                dns_ttl=60, proxy_bind_ip="127.0.0.1", proxy_port=8086,
                dns_bind_ip="127.0.0.1", dns_port=8053, dns_backup_port=8054,
            ),
        )
        namespace_dict = {
            "os": __import__("os"), "data_path": tempfile.gettempdir(),
            "proc_handler": {"unrelated": {}}, "ready": False, "g": g, "xlog": Collector(),
            "load_config": lambda: None,
            "gfwlist": types.SimpleNamespace(GfwList=Resource),
            "ip_region": types.SimpleNamespace(IpRegion=Resource),
            "host_records": types.SimpleNamespace(DomainRecords=Resource, IpRecord=Resource),
            "user_rules": types.SimpleNamespace(Config=Resource),
            "connect_manager": types.SimpleNamespace(
                load_proxy_config=lambda: None, ConnectManager=Resource),
            "pipe_socks": types.SimpleNamespace(PipeSocks=Resource),
            "dns_query": types.SimpleNamespace(
                get_local_ips=lambda: [], CombineDnsQuery=Resource),
            "dns_server": types.SimpleNamespace(DnsServer=Resource),
            "simple_http_server": types.SimpleNamespace(HTTPServer=Resource),
            "proxy_handler": types.SimpleNamespace(ProxyServer=object),
        }
        exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace_dict)

        namespace_dict["start"]({})
        self.assertIsNone(g.x_tunnel)
        self.assertIsNone(g.x_tunnel_socks_port)

    def test_gae_front_init_clears_missing_module_publication(self):
        namespace("module_init", proc_handler={})
        namespace("xlog", getLogger=lambda *args, **kwargs: Collector())
        gae_front = load_module("x_tunnel.local.gae_front", APP / "x_tunnel" / "local" / "gae_front.py")
        gae_front.gae_proxy = object()

        self.assertFalse(gae_front.init())
        self.assertIsNone(gae_front.gae_proxy)


class FakeHTTPServer:
    fail_init = False
    instances = []

    def __init__(self, addresses, handler, **kwargs):
        self.addresses = addresses
        self.sockets = []
        self.init_calls = 0
        self.serve_calls = 0
        self.shutdown_calls = 0
        self.close_calls = 0
        self.__class__.instances.append(self)

    def init_socket(self):
        self.init_calls += 1
        if self.fail_init:
            raise OSError("bind failed")
        self.sockets = [object()]

    def serve_forever(self):
        self.serve_calls += 1

    def shutdown(self):
        self.shutdown_calls += 1

    def server_close(self):
        self.close_calls += 1


class FrontService:
    def __init__(self):
        self.config = types.SimpleNamespace(
            listen_ip="127.0.0.1", listen_port=8087, PROXY_ENABLE=0,
            PROXY_TYPE="", PROXY_HOST="", PROXY_PORT=0, GAE_APPIDS=[])
        self.start_calls = 0
        self.stop_calls = 0

    def start(self):
        self.start_calls += 1

    def stop(self):
        self.stop_calls += 1


class ListenerStartupTest(unittest.TestCase):
    def setUp(self):
        FakeHTTPServer.fail_init = False
        FakeHTTPServer.instances = []

    def load_gae_proxy(self):
        namespace("env_info", data_path=tempfile.gettempdir(), os_detail=lambda: "test")
        namespace("gae_proxy", APP / "gae_proxy")
        namespace("gae_proxy.local", APP / "gae_proxy" / "local")
        namespace("gae_proxy.local.cert_util", CertUtil=types.SimpleNamespace(init_ca=lambda value: None))
        namespace("gae_proxy.local.proxy_handler", GAEProxyHandler=object)
        self.gae_front = FrontService()
        self.direct_front = FrontService()
        namespace("gae_proxy.local.front", front=self.gae_front, direct_front=self.direct_front)
        namespace("xlog", getLogger=lambda *args, **kwargs: Collector())
        namespace("simple_http_server", HTTPServer=FakeHTTPServer)
        return load_module("gae_proxy.local.proxy", APP / "gae_proxy" / "local" / "proxy.py")

    def test_gae_ready_requires_successful_listen_and_rolls_back(self):
        proxy = self.load_gae_proxy()
        FakeHTTPServer.fail_init = True

        with self.assertRaises(OSError):
            proxy.main({})

        server = FakeHTTPServer.instances[0]
        self.assertFalse(proxy.ready)
        self.assertEqual(server.init_calls, 1)
        self.assertEqual(server.serve_calls, 0)
        self.assertEqual(server.close_calls, 1)
        self.assertEqual(self.gae_front.stop_calls, 1)
        self.assertEqual(self.direct_front.stop_calls, 1)

    def test_gae_front_start_failure_rolls_back_partial_startup(self):
        proxy = self.load_gae_proxy()

        def fail_start():
            raise RuntimeError("front start failed")

        self.gae_front.start = fail_start
        with self.assertRaises(RuntimeError):
            proxy.main({})

        self.assertFalse(proxy.ready)
        self.assertEqual(self.gae_front.stop_calls, 1)
        self.assertEqual(self.direct_front.stop_calls, 1)

    def test_gae_healthy_listener_still_serves(self):
        proxy = self.load_gae_proxy()

        proxy.main({})

        self.assertTrue(proxy.ready)
        self.assertEqual(FakeHTTPServer.instances[0].serve_calls, 1)

    def load_x_tunnel(self):
        namespace("env_info", data_path=tempfile.gettempdir())
        namespace("x_tunnel", APP / "x_tunnel")
        namespace("x_tunnel.local", APP / "x_tunnel" / "local")
        namespace("x_tunnel.local.proxy_handler", Socks5Server=object)
        namespace("os_platform", platform="test")
        namespace("xlog", getLogger=lambda *args, **kwargs: Collector())
        namespace("simple_http_server", HTTPServer=FakeHTTPServer)
        namespace("x_tunnel.local.web_control")

        class Session:
            def __init__(self):
                self.stop_calls = 0

            def stop(self):
                self.stop_calls += 1

        namespace("x_tunnel.local.proxy_session", ProxySession=Session,
                  balance_lock=threading.Lock())
        self.front_stop_calls = 0

        def front_stop():
            self.front_stop_calls += 1

        namespace("x_tunnel.local.front_dispatcher", init=lambda: None, stop=front_stop)
        self.x_config = types.SimpleNamespace(
            server_host="server.example", server_port=443, api_server="api.example",
            socks_host="127.0.0.1", socks_port=1080)
        namespace("x_tunnel.local.config", load_config=lambda: self.x_config)
        self.x_global = namespace(
            "x_tunnel.local.global_var", running=False, socks5_server=None,
            session=None, http_client=None, server_host="server.example", server_port=443)
        client = load_module("x_tunnel.local.client", APP / "x_tunnel" / "local" / "client.py")
        client.xxnet_version = lambda: "test"
        client.get_launcher_uuid = lambda: "test"
        return client

    def test_x_tunnel_all_ports_failed_never_ready_or_serves(self):
        client = self.load_x_tunnel()
        FakeHTTPServer.fail_init = True

        with self.assertRaises(OSError):
            client.start({})

        self.assertFalse(client.ready)
        self.assertFalse(self.x_global.running)
        self.assertIsNone(self.x_global.socks5_server)
        self.assertTrue(all(server.serve_calls == 0 for server in FakeHTTPServer.instances))
        self.assertGreater(self.front_stop_calls, 0)

    def test_x_tunnel_healthy_listener_still_serves(self):
        client = self.load_x_tunnel()

        client.start({})

        self.assertTrue(client.ready)
        self.assertEqual(self.x_global.bind_port, 1080)
        self.assertEqual(FakeHTTPServer.instances[0].serve_calls, 1)


class DnsServerLifecycleTest(unittest.TestCase):
    def load_dns_server(self):
        namespace("env_info", data_path=tempfile.gettempdir())
        namespace("smart_router", APP / "smart_router")
        namespace("smart_router.local", APP / "smart_router" / "local")
        namespace("smart_router.local.global_var", config=types.SimpleNamespace(udp_relay_port=5300))
        namespace("xlog", getLogger=lambda *args, **kwargs: Collector())
        return load_module("smart_router.local.dns_server", APP / "smart_router" / "local" / "dns_server.py")

    def test_dns_without_listener_raises_and_closes_every_socket(self):
        module = self.load_dns_server()
        sockets = []

        class FailingSocket:
            def __init__(self, *args):
                self.close_calls = 0
                sockets.append(self)

            def bind(self, address):
                raise OSError("bind failed")

            def close(self):
                self.close_calls += 1

        server = module.DnsServer(port=53000, backup_port=53001)
        with mock.patch.object(module.socket, "socket", FailingSocket):
            with self.assertRaises(OSError):
                server.start()

        self.assertFalse(server.running)
        self.assertEqual(server.sockets, [])
        self.assertTrue(sockets)
        self.assertTrue(all(sock.close_calls == 1 for sock in sockets))

    def test_dns_healthy_listener_starts_and_stops(self):
        module = self.load_dns_server()
        server = module.DnsServer(port=0, backup_port=0)
        with mock.patch.object(server, "bing_udp_relay", lambda ip: None):
            server.start()
            try:
                self.assertTrue(server.running)
                self.assertEqual(len(server.sockets), 1)
                self.assertIsNotNone(server.th)
            finally:
                server.stop()
        self.assertEqual(server.sockets, [])
        self.assertEqual(server.handlers, set())

    def test_dns_handler_limit_rejects_excess_inflight_work(self):
        module = self.load_dns_server()
        server = module.DnsServer()
        server.running = True
        server.max_handlers = 2
        gate = threading.Event()
        self.addCleanup(gate.set)

        self.assertTrue(hasattr(server, "_start_handler"), "缺少有界 handler 启动入口")
        for _ in range(2):
            self.assertTrue(server._start_handler(gate.wait, (5,), "test"))
        self.assertFalse(server._start_handler(gate.wait, (5,), "overflow"))
        self.assertEqual(len(server.handlers), 2)

        gate.set()
        server.stop()

    def test_dns_stop_joins_inflight_handler_before_returning(self):
        module = self.load_dns_server()
        server = module.DnsServer()
        server.running = True
        entered = threading.Event()
        release = threading.Event()

        def handler():
            entered.set()
            release.wait(2)

        self.assertTrue(hasattr(server, "_start_handler"), "缺少在途 handler 登记")
        self.assertTrue(server._start_handler(handler, (), "test"))
        self.assertTrue(entered.wait(1))
        stopped = threading.Event()
        stopper = threading.Thread(target=lambda: (server.stop(), stopped.set()))
        stopper.start()
        stopper.join(0.1)
        self.assertFalse(stopped.is_set(), "stop 未等待在途 handler")
        release.set()
        stopper.join(2)
        self.assertTrue(stopped.is_set())
        self.assertEqual(server.handlers, set())


class DirectFrontLifecycleTest(unittest.TestCase):
    def load_front(self, dispatcher_factory):
        logger = Collector()
        namespace("env_info", data_path=tempfile.gettempdir())
        namespace("xlog", getLogger=lambda *args, **kwargs: logger, null=logger)
        namespace("gae_proxy", APP / "gae_proxy")
        namespace("gae_proxy.local", APP / "gae_proxy" / "local")
        namespace("gae_proxy.local.check_local_network")
        namespace("gae_proxy.local.config", config=types.SimpleNamespace(), direct_config=types.SimpleNamespace())
        namespace("gae_proxy.local.host_manager", HostManager=object)
        namespace("gae_proxy.local.check_ip", CheckIp=object)
        namespace("gae_proxy.local.appid_manager", AppidManager=object)
        namespace("front_base", APP / "lib" / "noarch" / "front_base")
        namespace("front_base.openssl_wrap", SSLContext=object)
        namespace("front_base.connect_creator", ConnectCreator=object)
        namespace("front_base.ip_manager", IpManager=object)
        namespace("front_base.ip_source", Ipv4RangeSource=object, Ipv6PoolSource=object, IpCombineSource=object)
        namespace("front_base.http_dispatcher", HttpsDispatcher=dispatcher_factory)
        namespace("front_base.connect_manager", ConnectManager=object)
        return load_module("gae_proxy.local.front", APP / "gae_proxy" / "local" / "front.py")

    def make_direct_front(self, module):
        direct = module.DirectFront()
        direct.running = True
        direct.dispatchs = {}
        direct.connect_manager = mock.Mock()
        module.front.ip_manager = mock.Mock()
        return direct

    def test_stopped_direct_front_rejects_lazy_dispatcher_and_request(self):
        module = self.load_front(mock.Mock())
        direct = self.make_direct_front(module)
        direct.running = False

        self.assertIsNone(direct.get_dispatcher("example.com"))
        self.assertEqual(direct.request("GET", "example.com"), ("", 602, {}))
        module.HttpsDispatcher.assert_not_called()

    def test_running_direct_front_reuses_dispatcher_and_serves_request(self):
        dispatcher = mock.Mock()
        dispatcher.request.return_value = "response"
        module = self.load_front(mock.Mock(return_value=dispatcher))
        direct = self.make_direct_front(module)

        self.assertIs(direct.get_dispatcher("example.com"), dispatcher)
        self.assertIs(direct.get_dispatcher("example.com"), dispatcher)
        self.assertEqual(direct.request("GET", "example.com"), "response")
        self.assertEqual(module.HttpsDispatcher.call_count, 1)

    def test_direct_front_stop_cannot_miss_gated_dispatcher_creation(self):
        created = []
        entered = threading.Event()
        release = threading.Event()

        class Dispatcher:
            def __init__(self, *args, **kwargs):
                self.stop_calls = 0
                created.append(self)
                entered.set()
                release.wait(2)

            def stop(self):
                self.stop_calls += 1

        module = self.load_front(Dispatcher)
        direct = self.make_direct_front(module)
        getter = threading.Thread(target=lambda: direct.get_dispatcher("example.com"))
        getter.start()
        self.assertTrue(entered.wait(1))
        manager_stopped = threading.Event()
        direct.connect_manager.stop.side_effect = manager_stopped.set
        stopper = threading.Thread(target=direct.stop)
        stopper.start()
        manager_stopped.wait(0.2)
        release.set()
        getter.join(2)
        stopper.join(2)

        self.assertFalse(getter.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].stop_calls, 1)
        self.assertEqual(direct.dispatchs, {})
        self.assertFalse(direct.running)


class ConnectManagerLifecycleTest(unittest.TestCase):
    def load_manager(self):
        logger = Collector()
        namespace("front_base", APP / "lib" / "noarch" / "front_base")
        namespace("front_base.openssl_wrap", SSLConnection=object)
        namespace("xlog", LogThrottle=lambda seconds: Throttle())
        return load_module("front_base.connect_manager", APP / "lib" / "noarch" / "front_base" / "connect_manager.py")

    def make_manager(self, module):
        manager = module.ConnectManager.__new__(module.ConnectManager)
        manager.logger = Collector()
        manager.config = types.SimpleNamespace(show_state_debug=0, connect_create_interval=0)
        manager.running = True
        manager.thread_num_lock = threading.Lock()
        manager.no_ip_lock = threading.Lock()
        manager.no_ip_time = 0
        manager.last_no_ip_warn = 0
        manager.suppressed_no_ip_warn = 0
        manager.total_no_ip_events = 0
        manager.connect_ssl_log_throttle = Throttle()
        manager.new_conn_pool = module.ConnectPool()
        manager.ip_manager = mock.Mock()
        manager.ip_manager.get_ip_sni_host.return_value = {
            "ip_str": "192.0.2.1", "sni": "example.com", "host": "example.com"}
        manager.check_local_network = types.SimpleNamespace(is_ok=lambda ip: True)
        return manager

    def test_stop_closes_pool_and_late_connection(self):
        module = self.load_manager()
        manager = self.make_manager(module)
        pooled = mock.Mock()
        late = mock.Mock(handshake_time=1)
        manager.new_conn_pool.put((1, pooled))
        entered = threading.Event()
        release = threading.Event()

        def create_ssl(host_info):
            entered.set()
            release.wait(2)
            return late

        manager._create_ssl_connection = create_ssl
        worker = threading.Thread(target=manager._connect_process)
        worker.start()
        self.assertTrue(entered.wait(1))
        manager.stop()
        release.set()
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(manager.new_conn_pool.qsize(), 0)
        pooled.close.assert_called_once_with()
        late.close.assert_called_once_with()

    def test_timed_out_connect_closes_late_ssl_socket(self):
        module = self.load_manager()
        manager = self.make_manager(module)
        late = mock.Mock(handshake_time=1, h2=False, sni="example.com", host="example.com")
        entered = threading.Event()
        release = threading.Event()

        class ImmediateTimeoutQueue:
            def get(self, timeout=None):
                raise TimeoutError("timeout")

            def put(self, value):
                pass

        def connect_ssl(*args, **kwargs):
            entered.set()
            release.wait(2)
            return late

        manager.connect_creator = types.SimpleNamespace(connect_ssl=connect_ssl)
        with mock.patch.object(module, "Queue", ImmediateTimeoutQueue):
            self.assertIsNone(manager._create_ssl_connection(manager.ip_manager.get_ip_sni_host()))
        self.assertTrue(entered.wait(1))
        release.set()
        deadline = threading.Event()
        for _ in range(100):
            if late.close.called:
                break
            deadline.wait(0.01)

        late.close.assert_called_once_with()
    def test_ssl_enqueue_and_timeout_share_one_delivery_boundary(self):
        module = self.load_manager()
        manager = self.make_manager(module)
        late = mock.Mock(handshake_time=1, h2=False, sni="example.com", host="example.com")
        put_entered = threading.Event()
        release_put = threading.Event()
        put_done = threading.Event()
        queues = []

        class GatedQueue(queue.Queue):
            def __init__(self):
                super().__init__()
                queues.append(self)

            def put(self, value):
                put_entered.set()
                release_put.wait(2)
                try:
                    super().put(value)
                finally:
                    put_done.set()

            def get(self, timeout=None):
                if timeout == 30:
                    if not put_entered.wait(1):
                        raise AssertionError("worker 未进入结果入队")
                    releaser = threading.Thread(target=lambda: (
                        threading.Event().wait(0.05), release_put.set()))
                    releaser.start()
                    raise queue.Empty()
                return super().get(timeout=timeout)

        manager.connect_creator = types.SimpleNamespace(
            connect_ssl=lambda *args, **kwargs: late)
        with mock.patch.object(module, "Queue", GatedQueue):
            self.assertIsNone(manager._create_ssl_connection(manager.ip_manager.get_ip_sni_host()))

        self.assertTrue(put_done.wait(1))
        self.assertEqual(len(queues), 1)
        self.assertTrue(queues[0].empty(), "超时后排干与结果入队不得交错留下无主 socket")
        late.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
