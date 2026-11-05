#!/usr/bin/env python3
"""C 阶段测试：IPv6 过滤（tls_relay + seley，B4 收紧 + B5 可观测 + force 覆盖）。"""
import importlib.util
import os
import sys
import time
import types

BASE = "/Applications/XX-Net/code/default"
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
    # 先加载 tls_relay ip_manager（stub 其父包，避免执行 heavy __init__ 链）
    tls = load_mod("x_tunnel.local.tls_relay_front.ip_manager",
                   os.path.join(BASE, "x_tunnel", "local", "tls_relay_front", "ip_manager.py"))
    # 再加载 seley（相对导入 ..tls_relay_front.ip_manager 复用上面模块）
    seley = load_mod("x_tunnel.local.seley_front.ip_manager",
                     os.path.join(BASE, "x_tunnel", "local", "seley_front", "ip_manager.py"))

    # ---- B4：ipv6_available 探测语义（fake socket 注入）----
    real_socket = tls.socket.socket
    real_time = tls.time.time

    class FakeV6Sock:
        local = "::"
        def __init__(self, *a, **k): pass
        def connect(self, addr): pass
        def getsockname(self): return (FakeV6Sock.local, 0, 0, 0)
        def close(self): pass

    def probe_with(local, expect):
        FakeV6Sock.local = local
        tls.socket.socket = FakeV6Sock
        tls.ipv6_available.cached_until = 0.0  # 绕过缓存
        got = tls.ipv6_available()
        assert got is expect, "local=%s expect=%s got=%s" % (local, expect, got)

    try:
        probe_with("::", False)            # B4：未指定源 -> 不可用（修复前误判可用）
        probe_with("2001:db8::1", True)    # 全局单播 -> 可用
        probe_with("2606:4700::1", True)   # 2/3 开头 -> 可用
        probe_with("fe80::1", False)       # link-local -> 不可用
        probe_with("::1", False)           # loopback -> 不可用
        probe_with("fc00::1", False)       # ULA -> 不可用

        # 探测异常 -> False
        def broken_socket(*a, **k):
            raise OSError("no v6 support")
        tls.socket.socket = broken_socket
        tls.ipv6_available.cached_until = 0.0
        assert tls.ipv6_available() is False

        # 缓存生效：设置后不再探测
        tls.ipv6_available.cached_result = True
        tls.ipv6_available.cached_until = real_time() + 300
        tls.socket.socket = broken_socket
        assert tls.ipv6_available() is True
    finally:
        tls.socket.socket = real_socket
        tls.ipv6_available.cached_until = 0.0

    # ---- tls_relay get_ip_sni_host：v4/v6 混合池 + B5 可观测 ----
    rec = Rec()
    # max_links 设大，避免多次调用把 v4 条目 links 打满而提前 return None
    cfg = types.SimpleNamespace(max_links_per_ip=10, ip_initial_score=100,
                                ip_initial_rtt=1000, ip_initial_speed=1,
                                ip_cal_expect_time_package_size=1000,
                                use_ipv6="auto")
    hm = types.SimpleNamespace(info={
        "198.18.0.9": {"sni": "a", "port": 443},
        "2001:db8::9": {"sni": "b", "port": 443},
    })
    mgr = tls.IpManager(cfg, hm, rec, None)
    mgr._ipv6_enabled = lambda: False

    res = mgr.get_ip_sni_host()
    assert res is not None and "198.18.0.9" in res["ip_str"], res
    assert "::" not in res["ip_str"]
    infos = [m for l, m in rec.lines if l == "INFO" and "skipped 1 IPv6 fronts" in m]
    assert len(infos) == 1, rec.lines

    # 同一 5 分钟周期内再调 -> 不重复记
    rec.lines.clear()
    mgr.get_ip_sni_host()
    infos = [m for l, m in rec.lines if l == "INFO" and "skipped" in m]
    assert len(infos) == 0, rec.lines

    # 跨周期（推进 400s）-> 再记一条
    tls.time.time = lambda: real_time() + 400
    try:
        rec.lines.clear()
        mgr.get_ip_sni_host()
        infos = [m for l, m in rec.lines if l == "INFO" and "skipped 1 IPv6 fronts" in m]
        assert len(infos) == 1, rec.lines
    finally:
        tls.time.time = real_time

    # ---- 退化场景：全 v6 池 -> 返回 None，且不额外记 INFO（由 not enough ip 兜底）----
    hm6 = types.SimpleNamespace(info={"2001:db8::9": {"sni": "b", "port": 443}})
    mgr6 = tls.IpManager(cfg, hm6, rec, None)
    mgr6._ipv6_enabled = lambda: False
    rec.lines.clear()
    assert mgr6.get_ip_sni_host() is None
    assert not [m for l, m in rec.lines if l == "INFO" and "skipped" in m], rec.lines

    # ---- seley _get_ip_dict 三路径 + force 覆盖 ----
    seley_rec = Rec()
    seley.xlog = seley_rec
    cfg_fn = os.path.join(os.environ.get("TMPDIR", "/tmp"), "seley_test_hosts_%d.json" % os.getpid())

    def make_seley(use_ipv6):
        c = types.SimpleNamespace(max_links_per_ip=2, ip_initial_score=100,
                                  ip_initial_rtt=1000, ip_initial_speed=1,
                                  ip_cal_expect_time_package_size=1000,
                                  use_ipv6=use_ipv6)
        return seley.IpManager(c, seley_rec, cfg_fn, None)

    real_getaddrinfo = seley.socket.getaddrinfo
    # 注入受控探测结果：本机网络状态会变（实测 auto 时真实 probe 可能返回 True），
    # 测试不能依赖真实网络。
    probe_val = [False]
    seley.ipv6_available = lambda logger=None: probe_val[0]

    def fake_gai(host, port, *a, **k):
        return [
            (10, 1, 6, "", ("2001:db8::5", port, 0, 0)),
            (2, 1, 6, "", ("198.18.0.5", port)),
        ]

    try:
        # 1) 域名 + auto + 无 v6 -> 只取 v4
        m = make_seley("auto")
        seley.socket.getaddrinfo = fake_gai
        d = m._get_ip_dict("front.example.com")
        assert d is not None and "198.18.0.5" in d["ip_str"], d

        # 1b) 仅 v6 答案的 host：probe False -> 全部被跳过 -> None；
        #     probe True -> 接受 v6 答案。直接验证过滤开关本身。
        def fake_gai_v6only(host, port, *a, **k):
            return [(10, 1, 6, "", ("2001:db8::7", port, 0, 0))]
        seley.socket.getaddrinfo = fake_gai_v6only
        assert m._get_ip_dict("v6only.example.com") is None
        probe_val[0] = True
        d = m._get_ip_dict("v6only.example.com")
        assert d is not None and "2001:db8::7" in d["ip_str"], d
        probe_val[0] = False

        # 2) IPv4 字面量 -> 原样保留
        d = m._get_ip_dict("1.2.3.4")
        assert d is not None and "1.2.3.4" in d["ip_str"], d

        # 3) IPv6 字面量 + auto -> None
        assert m._get_ip_dict("2001:db8::1") is None

        # 4) force_ipv4 + v6 字面量 -> None
        m4 = make_seley("force_ipv4")
        assert m4._get_ip_dict("2001:db8::1") is None

        # 5) force_ipv6 + v6 字面量 -> 接受（用户覆盖生效）
        m6 = make_seley("force_ipv6")
        d = m6._get_ip_dict("2001:db8::1")
        assert d is not None and "2001:db8::1" in d["ip_str"], d

        # 6) bytes host 归一化（回归：bytes-like TypeError）
        d = m._get_ip_dict(b"1.2.3.4")
        assert d is not None and "1.2.3.4" in d["ip_str"], d
    finally:
        seley.socket.getaddrinfo = real_getaddrinfo
        try:
            os.remove(cfg_fn)
        except OSError:
            pass

    print("test_ipv6_filter: ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
