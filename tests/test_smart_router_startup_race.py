#!/usr/bin/env python3
"""Smart Router 启动期间，代理请求必须看到已初始化的 DNS 对象。"""
import importlib.util
import os
import shutil
import sys
import tempfile
import types

sys.dont_write_bytecode = True
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL = os.path.join(REPO, "Coding", "XX-Net-5.16.7.1", "code", "default", "smart_router", "local")


def module(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


class Logger(object):
    def debug(self, *args):
        pass

    def info(self, *args):
        pass

    def warn(self, *args):
        pass

    def set_buffer(self, *args):
        pass


class Config(object):
    def __init__(self, path):
        self.path = path

    def set_var(self, name, default):
        setattr(self, name, default)

    def load(self):
        pass


class EmptyService(object):
    def __init__(self, *args, **kwargs):
        pass


class DomainRecords(EmptyService):
    pass


class IpRecord(EmptyService):
    pass


class PipeSocks(EmptyService):
    def run(self):
        pass


class DnsServer(EmptyService):
    def start(self):
        self.listen_port = 8053
        self.udp_relay_port = 8086


class CombineDnsQuery(object):
    def query_recursively(self, domain, dns_type):
        return ["192.0.2.1"]


temp_root = tempfile.mkdtemp(prefix="xxnet-startup-race-")
try:
    module("env_info", data_path=temp_root)
    module("xlog", getLogger=lambda *args, **kwargs: Logger())
    module("xconfig", Config=Config)

    g = module("smart_router.local.global_var", dns_query=None, dns_srv=None)
    logger = Logger()
    startup_results = []
    startup_errors = []
    udp_results = []
    udp_errors = []

    class HTTPServer(object):
        def __init__(self, addresses, handler, logger, check_listen_interval):
            self.addresses = addresses
            self.handler = handler

        def start(self):
            # 模拟真实线程在 start() 返回前就已经受理第一个域名请求。
            try:
                startup_results.append(g.dns_query.query_recursively(b"startup.example", 1))
            except Exception as e:
                startup_errors.append(e)
            try:
                udp_results.append(g.dns_srv.udp_relay_port)
            except Exception as e:
                udp_errors.append(e)

    module("simple_http_server", HTTPServer=HTTPServer)
    module("smart_router")
    module("smart_router.local.apis")
    module("smart_router.local.dns_server", DnsServer=DnsServer)
    module("smart_router.local.dns_query", CombineDnsQuery=CombineDnsQuery,
           get_local_ips=lambda: ["192.168.1.2"])
    module("smart_router.local.host_records", DomainRecords=DomainRecords, IpRecord=IpRecord)
    module("smart_router.local.user_rules", Config=EmptyService)
    module("smart_router.local.proxy_handler", ProxyServer=object)
    module("smart_router.local.web_control")
    module("smart_router.local.connect_manager", ConnectManager=EmptyService,
           load_proxy_config=lambda: None)
    module("smart_router.local.pac_server")
    module("smart_router.local.pipe_socks", PipeSocks=PipeSocks)
    module("smart_router.local.ip_region", IpRegion=EmptyService)
    module("smart_router.local.gfwlist", GfwList=EmptyService)

    xtunnel_local = types.SimpleNamespace(
        global_var=types.SimpleNamespace(config=types.SimpleNamespace(socks_port=1080)))
    module("module_init", proc_handler={
        "x_tunnel": {"imp": types.SimpleNamespace(local=xtunnel_local)},
    })

    spec = importlib.util.spec_from_file_location(
        "smart_router.local", os.path.join(LOCAL, "__init__.py"),
        submodule_search_locations=[LOCAL])
    smart_router = importlib.util.module_from_spec(spec)
    sys.modules["smart_router.local"] = smart_router
    spec.loader.exec_module(smart_router)
    smart_router.start({})

    ok = (not startup_errors and startup_results == [["192.0.2.1"]]
          and smart_router.ready and g.local_ips == ["192.168.1.2"])
    print(("PASS" if ok else "FAIL"),
          "S1 first proxied request sees initialized DNS",
          "results=%r errors=%r ready=%r" % (
              startup_results, [repr(e) for e in startup_errors], smart_router.ready))
    if not ok:
        sys.exit(1)

    udp_ok = not udp_errors and udp_results == [8086]
    print(("PASS" if udp_ok else "FAIL"),
          "S2 first UDP ASSOCIATE sees initialized DNS relay",
          "ports=%r errors=%r" % (udp_results, [repr(e) for e in udp_errors]))
    if not udp_ok:
        sys.exit(1)
    print("RESULT: ALL PASS")
finally:
    shutil.rmtree(temp_root)
