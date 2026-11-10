#!/usr/bin/env python3
"""离线测试：smart_router dns_query.py nameserver 过滤（本轮日志收敛项）。

背景：macOS /etc/resolv.conf 常含 link-local IPv6 nameserver
（fe80::...%en0），send_request 对其裸 sock6.sendto 恒报
"No route to host"(errno 65)，刷爆 smart_router_warning.log。
修复：_is_usable_nameserver 只放行 IPv4(非 127.0.0.1) 与全球单播
IPv6(2000::/3，同 tls_relay ip_manager.ipv6_available 语义)；过滤全部
落空时在公共 DNS 兜底之前完成（fail-safe 前移）。

离线运行：stub 包 + 按文件加载，不触碰运行中的 XX-Net 进程。
用法：/usr/bin/python3 test_dns_nameserver_filter.py
"""
import importlib.util
import io
import os
import sys
import types
from unittest.mock import mock_open, patch

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
            msg = "FMTFAIL(%r)" % ex
        self.lines.append((lvl, msg))

    def debug(self, f, *a, **k): self._r("DEBUG", f, *a)
    def info(self, f, *a, **k): self._r("INFO", f, *a)
    def warn(self, f, *a, **k): self._r("WARN", f, *a)
    def error(self, f, *a, **k): self._r("ERROR", f, *a)
    def exception(self, f, *a, **k): self._r("ERROR", f, *a)


def main():
    dq = load_mod("smart_router.local.dns_query",
                  os.path.join(BASE, "smart_router", "local", "dns_query.py"))
    rec = Rec()
    dq.xlog = rec

    usable = dq.LocalDnsQuery._is_usable_nameserver

    # ---- 1) _is_usable_nameserver 语义表 ----
    keep = [
        b"192.168.31.1",           # 私网 v4：UDP 可达，保留
        b"1.1.1.1",
        "8.8.8.8",                 # str 输入也要接受
        b"2001:4860:4860::8888",   # 全球单播 v6
        b"2606:4700:4700::1111",
        b"3fff:ffff::1",           # 2000::/3 末段
    ]
    drop = [
        b"fe80::4ec6:4cff:fe4f:ae64%en0",  # 本机真实噪音源
        b"fe80::1",
        b"::1",
        b"fc00::1",                # ULA
        b"fd12:3456::1",           # ULA fd00::/8
        b"ff02::fb",               # 组播
        b"127.0.0.1",
    ]
    for ip in keep:
        assert usable(ip) is True, ip
    for ip in drop:
        assert usable(ip) is False, ip

    # ---- 2) 端到端：mock /etc/resolv.conf ----
    inst = dq.LocalDnsQuery.__new__(dq.LocalDnsQuery)  # 免 __init__（无 socket/线程）
    real_isfile = dq.os.path.isfile

    def with_resolv(content, country="CN", local_ips=()):
        dq.g.config = types.SimpleNamespace(country_code=country)
        dq.g.local_ips = list(local_ips)
        dq.os.path.isfile = lambda p: p == "/etc/resolv.conf"
        rec.lines.clear()
        with patch("builtins.open", mock_open(read_data=content)):
            return inst.get_local_dns_server()

    try:
        # 2a) 本机实际形态：fe80 + 内网 v4 -> 只留 v4，不触发兜底
        out = with_resolv(b"nameserver fe80::4ec6:4cff:fe4f:ae64%en0\n"
                          b"nameserver 192.168.31.1\n")
        assert out == [b"192.168.31.1"], out
        assert any(l == "INFO" and "skip unusable DNS server" in m and "fe80::" in m
                   for l, m in rec.lines), rec.lines
        assert not any("114.114" in m for l, m in rec.lines), rec.lines

        # 2b) 只有 link-local v6 -> 全滤空 -> CN 兜底（fail-safe 前移生效）
        out = with_resolv(b"nameserver fe80::4ec6:4cff:fe4f:ae64%en0\n")
        assert b"114.114.114.114" in out, out
        assert all(b":" not in s for s in out), out

        # 2c) 非 CN -> 通用兜底
        out = with_resolv(b"nameserver fe80::1\n", country="US")
        assert b"1.1.1.1" in out, out

        # 2d) 全球单播 v6 + v4 混合 -> 都保留、保序
        out = with_resolv(b"nameserver fe80::1\n"
                          b"nameserver 2001:4860:4860::8888\n"
                          b"nameserver 192.168.31.1\n")
        assert out == [b"2001:4860:4860::8888", b"192.168.31.1"], out

        # 2e) 127.0.0.1 仍被剔除（原语义保留）
        out = with_resolv(b"nameserver 127.0.0.1\nnameserver 1.1.1.1\n", country="US")
        assert out == [b"1.1.1.1"], out

        # 2f) resolv.conf 不存在 -> 直接兜底
        dq.g.config = types.SimpleNamespace(country_code="CN")
        dq.g.local_ips = []
        dq.os.path.isfile = lambda p: False
        rec.lines.clear()
        out = inst.get_local_dns_server()
        assert b"114.114.114.114" in out, out

        # 2g) local_ips 剔除交互：内网 v4 被既有逻辑移除后全空 -> 兜底
        out = with_resolv(b"nameserver 192.168.31.1\n",
                          local_ips=[b"192.168.31.1"])
        assert b"114.114.114.114" in out, out
    finally:
        dq.os.path.isfile = real_isfile

    print("test_dns_nameserver_filter: ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
