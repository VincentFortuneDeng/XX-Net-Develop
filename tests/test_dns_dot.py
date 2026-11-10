#!/usr/bin/env python3
"""C 阶段测试：DnsOverTlsQuery.connect 三路径 + 基类 direct/proxy 路径加固（B1/B2）。

离线运行：stub 包 + 按文件加载模块，不触碰运行中的 XX-Net 进程。
"""
import importlib.util
import os
import sys
import time
import types

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "Coding", "XX-Net-5.16.7.1", "code", "default")
NOARCH = os.path.join(BASE, "lib", "noarch")
if NOARCH not in sys.path:
    sys.path.insert(0, NOARCH)


def _stub_pkg(name, path):
    if name not in sys.modules:
        m = types.ModuleType(name)
        m.__path__ = [path]
        sys.modules[name] = m
    return sys.modules[name]


def load_mod(fullname, filepath):
    parts = fullname.split(".")
    for i in range(1, len(parts)):
        _stub_pkg(".".join(parts[:i]), os.path.join(BASE, *parts[:i]))
    spec = importlib.util.spec_from_file_location(fullname, filepath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[fullname] = mod
    spec.loader.exec_module(mod)
    return mod


class Rec:
    def __init__(self):
        self.lines = []

    def _r(self, lvl, fmt, *a):
        try:
            msg = fmt % a
        except Exception as ex:
            msg = "FMTFAIL(%r): %r" % (ex, (fmt, a))
        self.lines.append((lvl, msg))

    def debug(self, f, *a, **k): self._r("DEBUG", f, *a)
    def info(self, f, *a, **k): self._r("INFO", f, *a)
    def warn(self, f, *a, **k): self._r("WARN", f, *a)
    def error(self, f, *a, **k): self._r("ERROR", f, *a)
    def exception(self, f, *a, **k): self._r("ERROR", f, *a)


class CountingSock:
    def __init__(self):
        self.close_count = 0
        self.closed = False

    def close(self):
        self.close_count += 1
        self.closed = True


class RaisingCtx:
    def wrap_socket(self, sock, server_hostname=None):
        raise ValueError("boom: forced wrap failure")


class FakePlaintextSock:
    """direct 路径 fake：socket() 成功，connect 抛非 socket.error 异常。"""
    def __init__(self, *a, **k):
        self.close_count = 0
    def setsockopt(self, *a): pass
    def settimeout(self, *a): pass
    def connect(self, addr):
        raise RuntimeError("unexpected non-socket.error")
    def close(self):
        self.close_count += 1


class FakeProxySock:
    """代理路径 fake：socksocket，connect 抛 OSError，close 可计数。"""
    def __init__(self, *a, **k):
        self.close_count = 0
    def set_proxy(self, **k): self.proxy_set = True
    def setsockopt(self, *a): pass
    def settimeout(self, *a): pass
    def connect(self, addr):
        raise OSError("proxy connect refused")
    def close(self):
        self.close_count += 1


def main():
    dq = load_mod("smart_router.local.dns_query",
                  os.path.join(BASE, "smart_router", "local", "dns_query.py"))
    assert hasattr(dq, "DnsOverTlsQuery")

    rec = Rec()
    dq.xlog = rec

    # ---- B1 路径 1：基类 connect 返回 None -> DoT 返回 None，无 close 尝试 ----
    orig_base_connect = dq.DnsOverTcpQuery.connect
    dq.DnsOverTcpQuery.connect = lambda self, host, port: None
    try:
        dot = dq.DnsOverTlsQuery()
        rec.lines.clear()
        host = {"domain": "one.one.one.one", "ipv4s": [b"1.1.1.1"]}
        assert dot.connect(host, 853) is None
        # base-returns-None is silent here by design: the base already logged
        # its own warn (comment in DoT connect); no duplicate line expected.
        assert not any("DnsOverTlsQuery connect" in m for l, m in rec.lines), rec.lines
    finally:
        dq.DnsOverTcpQuery.connect = orig_base_connect

    # ---- B1 路径 2：基类 connect 抛异常 -> DoT 返回 None（s=None，不 close）----
    def raise_base(self, host, port):
        raise RuntimeError("base connect exploded")

    dq.DnsOverTcpQuery.connect = raise_base
    try:
        dot2 = dq.DnsOverTlsQuery()
        rec.lines.clear()
        assert dot2.connect(host, 853) is None
        assert any(l == "WARN" and "fail" in m for l, m in rec.lines), rec.lines
    finally:
        dq.DnsOverTcpQuery.connect = orig_base_connect

    # ---- B1 路径 3：wrap_socket 抛异常 -> 恰好 close 一次（计数 fake）----
    class DotWithFakeCtx(dq.DnsOverTlsQuery):
        def __init__(self):
            dq.DnsOverTcpQuery.__init__(self, server_list=[host], port=853)
            self.protocol = "DoT"
            self.ssl_context = RaisingCtx()

    dq.DnsOverTcpQuery.connect = lambda self, h, p: CountingSock()
    try:
        dot3 = DotWithFakeCtx()
        rec.lines.clear()
        assert dot3.connect(host, 853) is None
        raw = dq.DnsOverTcpQuery.connect.__self__ if False else None  # noqa
    finally:
        dq.DnsOverTcpQuery.connect = orig_base_connect

    # 直接验证 close 一次：单独构造
    raw_sock = CountingSock()
    calls = {"n": 0}

    def base_returns_fake(self, h, p):
        calls["n"] += 1
        return raw_sock

    dq.DnsOverTcpQuery.connect = base_returns_fake
    try:
        dot4 = DotWithFakeCtx()
        rec.lines.clear()
        assert dot4.connect(host, 853) is None
        assert raw_sock.close_count == 1, raw_sock.close_count
        assert any(l == "WARN" and "fail" in m for l, m in rec.lines), rec.lines
    finally:
        dq.DnsOverTcpQuery.connect = orig_base_connect

    # ---- B2：direct 路径 except Exception 关闭 s 且日志参数齐全 ----
    real_socket = dq.socket.socket
    dq.socket.socket = FakePlaintextSock
    try:
        tcp = dq.DnsOverTcpQuery()
        rec.lines.clear()
        assert tcp.direct_connect(b"127.0.0.1", 53) is None
        fake_sock_instance = None
        # FakePlaintextSock 的 close 计数在实例上，通过重放确认至少逻辑无异常；
        # 关键断言：warn 行成功格式化（缺 e 参数时会 FMTFAIL/TypeError）
        warn_lines = [m for l, m in rec.lines if l == "WARN" and "fail:" in m]
        assert warn_lines, rec.lines
        assert "FMTFAIL" not in warn_lines[0], warn_lines
    finally:
        dq.socket.socket = real_socket

    # close 计数验证（direct 非 socket.error 路径）
    class CloseCountingPlainSock(FakePlaintextSock):
        instances = []
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.__class__.instances.append(self)

    dq.socket.socket = CloseCountingPlainSock
    try:
        tcp2 = dq.DnsOverTcpQuery()
        assert tcp2.direct_connect(b"127.0.0.1", 53) is None
        assert CloseCountingPlainSock.instances, "socket() not called"
        assert CloseCountingPlainSock.instances[0].close_count == 1, \
            CloseCountingPlainSock.instances[0].close_count
    finally:
        dq.socket.socket = real_socket

    # ---- B2：代理路径 connect 失败 -> close 后原样上抛 ----
    dq.g.config = types.SimpleNamespace(
        PROXY_ENABLE=True, PROXY_TYPE="HTTP", PROXY_HOST="127.0.0.1",
        PROXY_PORT=1, PROXY_USER="", PROXY_PASSWD="")
    fake_socks = types.ModuleType("socks")
    fake_socks.socksocket = FakeProxySock
    sys.modules["socks"] = fake_socks
    try:
        tcp3 = dq.DnsOverTcpQuery()
        rec.lines.clear()
        try:
            tcp3.connect(b"1.2.3.4", 53)
            raised = None
        except OSError:
            raised = "OSError"
        assert raised == "OSError", raised
        # close 计数：FakeProxySock 实例记录在 connect 时创建——通过类钩子拿不到，
        # 改为包装验证：再次调用并统计类级计数
    finally:
        del sys.modules["socks"]
        dq.g.config = None

    class CloseCountingProxy(FakeProxySock):
        instances = []
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.__class__.instances.append(self)

    fake_socks2 = types.ModuleType("socks")
    fake_socks2.socksocket = CloseCountingProxy
    sys.modules["socks"] = fake_socks2
    dq.g.config = types.SimpleNamespace(
        PROXY_ENABLE=True, PROXY_TYPE="HTTP", PROXY_HOST="127.0.0.1",
        PROXY_PORT=1, PROXY_USER="", PROXY_PASSWD="")
    try:
        tcp4 = dq.DnsOverTcpQuery()
        try:
            tcp4.connect(b"1.2.3.4", 53)
            assert False, "expected OSError"
        except OSError:
            pass
        assert CloseCountingProxy.instances, "proxy sock not created"
        assert CloseCountingProxy.instances[0].close_count == 1, \
            CloseCountingProxy.instances[0].close_count
        assert CloseCountingProxy.instances[0].proxy_set
    finally:
        del sys.modules["socks"]
        dq.g.config = None

    print("test_dns_dot: ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
