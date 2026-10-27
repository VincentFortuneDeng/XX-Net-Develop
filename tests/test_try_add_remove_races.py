#!/usr/bin/env python3
"""C 阶段测试：try_add/try_remove 竞态分级（Bug 5 与 B3）。

真 selectors + 真 socketpair / 可控 fake socket，全部离线。
"""
import importlib.util
import os
import socket
import sys
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


class ScriptedFdSock:
    """fileno() 第一次返回真 fd（通过预检），其后返回 -1（register 内部触发 ValueError）。"""
    def __init__(self, fd):
        self.fd = fd
        self.calls = 0

    def fileno(self):
        self.calls += 1
        return self.fd if self.calls == 1 else -1

    def __str__(self):
        return "ScriptedFdSock(%d)" % self.fd


def main():
    ps = load_mod("smart_router.local.pipe_socks",
                  os.path.join(BASE, "smart_router", "local", "pipe_socks.py"))
    rec = Rec()
    ps.xlog = rec

    pipe = ps.PipeSocks()

    # ---- try_add：已关闭 socket 预检跳过（fileno()==-1 / 抛 OSError）----
    class ClosedSock:
        def fileno(self): return -1
        def __str__(self): return "ClosedSock"

    class RaisingFdSock:
        def fileno(self): raise OSError("bad fd")
        def __str__(self): return "RaisingFdSock"

    rec.lines.clear()
    pipe.try_add("READ", ClosedSock())
    pipe.try_add("READ", RaisingFdSock())
    assert all(l == "DEBUG" and "closed socket" in m for l, m in rec.lines), rec.lines
    assert len(rec.lines) == 2, rec.lines

    # ---- try_add happy path：真 socketpair，READ+WRITE 走 modify ----
    a, b = socket.socketpair()
    pipe.try_add("READ", a)
    pipe.try_add("WRITE", a)
    assert a in pipe.read_set and a in pipe.write_set
    rec.lines.clear()

    # ---- try_add：fd 在预检后失效 -> register 抛 ValueError -> DEBUG ----
    scripted = ScriptedFdSock(a.fileno())  # 借真 fd 号通过预检，随后 -1
    pipe.try_add("READ", scripted)
    dbg = [m for l, m in rec.lines if l == "DEBUG" and "race" in m]
    assert dbg, rec.lines
    assert not any(l == "ERROR" for l, m in rec.lines), rec.lines

    # ---- try_add：重复注册 -> KeyError -> DEBUG ----
    # 两个 set 都清空，让代码走到 register 分支；selector 里仍已注册 -> KeyError
    pipe.read_set.discard(a)
    pipe.write_set.discard(a)
    rec.lines.clear()
    pipe.try_add("READ", a)
    assert any(l == "DEBUG" and "already registered" in m for l, m in rec.lines), rec.lines
    assert not any(l == "ERROR" for l, m in rec.lines), rec.lines

    # ---- try_remove：已关且不在 selector -> _fileobj_lookup ValueError -> DEBUG ----
    # 注：macOS kqueue 对已注册后关闭的 fd 吞掉 OSError，不抛 ValueError；
    # 真实 ValueError 场景是「已关闭且未注册/已注销」（身份搜索失败后重抛）。
    f, _ = socket.socketpair()
    f.close()                      # 从未注册，fd 已回收
    pipe.read_set.add(f)           # 竞态现场：set 认为还在，实际 fd 已失效
    rec.lines.clear()
    pipe.try_remove("READ", f)
    assert any(l == "DEBUG" and "race" in m for l, m in rec.lines), rec.lines
    assert not any(l == "ERROR" for l, m in rec.lines), rec.lines

    # ---- try_remove：selector 中不存在 -> KeyError -> DEBUG ----
    c, d = socket.socketpair()
    pipe.try_add("READ", c)
    pipe.select2.unregister(c)  # 绕过 set，制造失配
    rec.lines.clear()
    pipe.try_remove("READ", c)
    assert any(l == "DEBUG" and "not registered" in m for l, m in rec.lines), rec.lines
    assert not any(l == "ERROR" for l, m in rec.lines), rec.lines

    # ---- try_remove：未知异常仍走全量报警（ERROR/exception）----
    e1, _ = socket.socketpair()
    pipe.try_add("READ", e1)
    pipe.try_add("WRITE", e1)
    orig_modify = pipe.select2.modify

    def boom(*args, **kwargs):
        raise RuntimeError("unexpected selector failure")

    pipe.select2.modify = boom
    try:
        rec.lines.clear()
        pipe.try_remove("READ", e1)
        assert any(l == "ERROR" and "try_remove e:" in m for l, m in rec.lines), rec.lines
    finally:
        pipe.select2.modify = orig_modify

    # ---- A4 结构确认：try_add 的 modify 与 register 同 try 块（静态断言）----
    src = open(os.path.join(BASE, "smart_router", "local", "pipe_socks.py")).read()
    add_body = src.split("def try_add")[1].split("def try_remove")[0]
    assert add_body.count("self.select2.modify") == 2
    assert add_body.count("self.select2.register") == 2
    assert "except ValueError" in add_body and "except KeyError" in add_body

    # 清理
    for s in [b, c, d, e1]:
        try:
            pipe.try_remove("READ", s)
            pipe.try_remove("WRITE", s)
        except Exception:
            pass
        try:
            s.close()
        except Exception:
            pass
    pipe.select2.close()

    print("test_try_add_remove_races: ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
