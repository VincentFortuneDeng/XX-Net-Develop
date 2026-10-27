#!/usr/bin/env python3
"""C 阶段测试：BlockingIOError 限流 + close 收口（B6）+ 512 剪枝。"""
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

    def warns(self):
        return [m for l, m in self.lines if l == "WARN"]


class FakeSock:
    def __init__(self, name):
        self.name = name
        self.ip = "127.0.0.1"
        self.port = 443
        self.host = "example.com"
        self.create_time = time.time()
        self.recved_data = 0
        self.recved_times = 0
        self.sent_data = 0
        self.sent_times = 0
        self.buf_size = 0
        self.closed = False
        self.close_count = 0

    def __str__(self): return self.name
    def is_closed(self): return self.closed
    def close(self):
        self.closed = True
        self.close_count += 1
    def add_dat(self, d): pass


def main():
    ps = load_mod("smart_router.local.pipe_socks",
                  os.path.join(BASE, "smart_router", "local", "pipe_socks.py"))
    rec = Rec()
    ps.xlog = rec

    # 可控时钟（warn_send_block / close / 剪枝均用模块内 time.time）
    clock = [1000.0]
    ps.time.time = lambda: clock[0]

    pipe = ps.PipeSocks()
    e = BlockingIOError()

    # ---- 限流窗口：首行立即；同秒累计 suppressed；跨秒结算 ----
    s1 = FakeSock("s1")
    rec.lines.clear()
    pipe.warn_send_block(s1, e)          # t=1000.0 首行
    assert len(rec.warns()) == 1 and "suppressed" not in rec.warns()[0], rec.warns()
    clock[0] = 1000.5
    pipe.warn_send_block(s1, e)
    clock[0] = 1000.9
    pipe.warn_send_block(s1, e)
    assert len(rec.warns()) == 1, rec.warns()          # 同窗口不再出账
    clock[0] = 1001.2
    pipe.warn_send_block(s1, e)
    assert len(rec.warns()) == 2, rec.warns()
    assert "(suppressed 2)" in rec.warns()[1], rec.warns()  # 窗口内 2 次被抑制

    # ---- B6：close 时挂账补打 final 行并清账 ----
    clock[0] = 1001.5                    # 距上次出账 0.3s < 1s -> 挂账不出行
    pipe.warn_send_block(s1, e)
    assert len(rec.warns()) == 2, rec.warns()

    s2 = FakeSock("s2")
    s1.pair_sock = s2
    s2.pair_sock = s1
    rec.lines.clear()
    pipe.close(s1, "test")
    finals = [m for m in rec.warns() if "(final suppressed 1)" in m]
    assert len(finals) == 1, rec.warns()
    assert id(s1) not in pipe.blockio_log_times
    assert s1.close_count == 1

    # ---- B6：无挂账 close -> 无 final 行 ----
    s3, s4 = FakeSock("s3"), FakeSock("s4")
    s3.pair_sock = s4
    s4.pair_sock = s3
    s4.closed = True  # 跳过 s2 分支
    rec.lines.clear()
    pipe.close(s3, "test2")
    assert not [m for m in rec.warns() if "final" in m], rec.warns()

    # ---- 512 剪枝：旧条目（>60s 无活动）被清 ----
    pipe2 = ps.PipeSocks()
    now = clock[0]
    for i in range(600):
        pipe2.blockio_log_times[100000 + i] = (now - 100, 1)
    fresh = FakeSock("fresh")
    pipe2.warn_send_block(fresh, e)
    assert len(pipe2.blockio_log_times) <= 512, len(pipe2.blockio_log_times)
    assert id(fresh) in pipe2.blockio_log_times
    # 60s 内活跃条目保留
    pipe2.blockio_log_times[555555] = (now - 30, 0)
    pipe2.warn_send_block(fresh, e)  # 同窗口，再触发一次剪枝检查
    assert 555555 in pipe2.blockio_log_times

    # ---- 键维度：按 id(s)，非 target ----
    sa, sb = FakeSock("a"), FakeSock("b")
    pipe3 = ps.PipeSocks()
    pipe3.warn_send_block(sa, e)
    pipe3.warn_send_block(sb, e)
    assert len(pipe3.blockio_log_times) == 2  # 两个连接互不挤占

    print("test_blockio_ratelimit: ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
