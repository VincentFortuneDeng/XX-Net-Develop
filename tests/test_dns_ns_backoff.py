#!/usr/bin/env python3
"""H 阶段：LocalDnsQuery 路由不可达退避的离线测试。

按相对路径加载仓库快照，不创建真实 socket/线程，不触运行进程。
"""
import errno
import importlib.util
import os
import sys
import threading
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(REPO, "Coding", "XX-Net-5.16.7.1", "code", "default")
NOARCH = os.path.join(BASE, "lib", "noarch")
if NOARCH not in sys.path:
    sys.path.insert(0, NOARCH)


def _stub_pkg(name, path):
    if name not in sys.modules:
        mod = types.ModuleType(name)
        mod.__path__ = [path]
        sys.modules[name] = mod
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


class Rec(object):
    def __init__(self):
        self.lines = []

    def _r(self, level, fmt, *args):
        self.lines.append((level, fmt % args if args else fmt))

    def debug(self, fmt, *args): self._r("DEBUG", fmt, *args)
    def info(self, fmt, *args): self._r("INFO", fmt, *args)
    def warn(self, fmt, *args): self._r("WARN", fmt, *args)
    def error(self, fmt, *args): self._r("ERROR", fmt, *args)
    def exception(self, fmt, *args): self._r("ERROR", fmt, *args)

    def warnings(self):
        return [msg for level, msg in self.lines if level == "WARN"]


class FakeTime(object):
    def __init__(self):
        self.now = 100.0

    def time(self):
        return self.now


class FakeUdpSocket(object):
    def __init__(self, error=None):
        self.error = error
        self.sent = []

    def sendto(self, data, address):
        if self.error:
            raise self.error
        self.sent.append((data, address))


failures = []


def check(name, condition, detail=""):
    print(("PASS" if condition else "FAIL"), name, detail)
    if not condition:
        failures.append(name)


dq = load_mod("smart_router.local.dns_query",
              os.path.join(BASE, "smart_router", "local", "dns_query.py"))
rec = Rec()
dq.xlog = rec
fake_time = FakeTime()
dq.time = fake_time
S1 = b"1.1.1.1"
S2 = b"8.8.8.8"


def make_query(error=None):
    inst = dq.LocalDnsQuery.__new__(dq.LocalDnsQuery)
    inst.timeout = 0.01
    inst.waiters = {}
    inst.dns_server = [S1, S2]
    inst.sock = FakeUdpSocket(error)
    inst.sock6 = FakeUdpSocket(error)
    # RED 时预置目标账本，旧代码会忽略它并暴露缺失行为。
    inst.ns_backoff = {}
    inst.ns_backoff_lock = threading.Lock()
    return inst


# 两类路由不可达等价：首条 WARN 带 server，冷却重复仅新增 DEBUG。
for route_errno in (errno.EHOSTUNREACH, errno.ENETUNREACH):
    error_name = errno.errorcode[route_errno]
    fake_time.now = 100.0
    q = make_query(OSError(route_errno, os.strerror(route_errno)))
    rec.lines.clear()
    q.send_request(1, S1, b"example.com", 1)
    check("N1 %s warns with server and sets cooldown" % error_name,
          q.ns_backoff.get(S1) == 160.0 and len(rec.lines) == 1
          and len(rec.warnings()) == 1 and "1.1.1.1" in rec.warnings()[0],
          "backoff=%r lines=%r" % (q.ns_backoff, rec.lines))
    fake_time.now = 101.0
    q.send_request(2, S1, b"example.com", 1)
    check("N2 %s cooldown repeat adds one DEBUG without extending" % error_name,
          q.ns_backoff.get(S1) == 160.0 and len(rec.warnings()) == 1
          and len(rec.lines) == 2 and rec.lines[-1][0] == "DEBUG"
          and "1.1.1.1" in rec.lines[-1][1]
          and "during cooldown" in rec.lines[-1][1],
          "backoff=%r lines=%r" % (q.ns_backoff, rec.lines))

# 其他错误不属于路由退避。
fake_time.now = 100.0
q = make_query(OSError(errno.ECONNREFUSED, "Connection refused"))
rec.lines.clear()
q.send_request(1, S1, b"example.com", 1)
check("N3 non-route errno does not cool down",
      not q.ns_backoff and len(rec.warnings()) == 1,
      "backoff=%r warnings=%r" % (q.ns_backoff, rec.warnings()))

# query 只向可用 server 发送；响应由 fake send 立即喂回 waiter。
def run_query(inst):
    calls = []
    def fake_send(dns_id, server_ip, domain, dns_type):
        calls.append(server_ip)
        inst.waiters[dns_id].put([b"192.0.2.1"])
    inst.send_request = fake_send
    result = inst.query(b"example.com", 1)
    return calls, result

fake_time.now = 100.0
q = make_query()
q.ns_backoff.update({S1: 150.0, S2: 0.0})
calls, result = run_query(q)
check("N4 query skips cooled server", calls == [S2] and result == [b"192.0.2.1"],
      "calls=%r result=%r" % (calls, result))

q = make_query()
q.ns_backoff.update({S1: 150.0, S2: 180.0})
calls, result = run_query(q)
check("N5 all cooled uses only earliest fail-open probe",
      calls == [S1] and result == [b"192.0.2.1"],
      "calls=%r result=%r" % (calls, result))

q = make_query()
q.ns_backoff.update({S1: 100.0, S2: 150.0})
calls, result = run_query(q)
check("N6 expired server retries", calls == [S1], "calls=%r" % calls)

q = make_query()
q.ns_backoff[S1] = 160.0
q.send_request(1, S1, b"example.com", 1)
check("N7 successful send clears cooldown", S1 not in q.ns_backoff,
      "backoff=%r" % q.ns_backoff)

print()
if failures:
    print("RESULT: %d FAILURES: %s" % (len(failures), failures))
    sys.exit(1)
print("RESULT: ALL PASS")
