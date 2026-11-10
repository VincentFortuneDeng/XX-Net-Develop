#!/usr/bin/env python3
"""J 阶段：BlockReceivePool 同 SN 超时日志聚合，状态语义保持不变。"""
import importlib.util
import os
import selectors
import sys
import threading
import types

sys.dont_write_bytecode = True
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(REPO, "Coding", "XX-Net-5.16.7.1", "code", "default")
NOARCH = os.path.join(BASE, "lib", "noarch")
LOCAL = os.path.join(BASE, "x_tunnel", "local")
sys.path.insert(0, NOARCH)  # 仅用于加载仓库真实的 xstruct。


class Clock(object):
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now


clock = Clock()
utils_stub = types.ModuleType("utils")
utils_stub.to_bytes = lambda value: value.encode() if isinstance(value, str) else value
utils_stub.to_str = lambda value: value.decode() if isinstance(value, bytes) else str(value)
utils_stub.bytes2str_only = lambda value: value
utils_stub.str2hex = lambda value: bytes(value).hex()
utils_stub.SimpleCondition = type("SimpleCondition", (), {})
sys.modules["utils"] = utils_stub
six_stub = types.ModuleType("six")
six_stub.string_types = str
sys.modules["six"] = six_stub
selectors2_stub = types.ModuleType("selectors2")
selectors2_stub.DefaultSelector = selectors.DefaultSelector
sys.modules["selectors2"] = selectors2_stub

xlog_path = os.path.join(NOARCH, "xlog.py")
xlog_mod = types.ModuleType("xlog_under_test")
with open(xlog_path) as fp:
    exec(compile(fp.read(), xlog_path, "exec"), xlog_mod.__dict__)
xlog_mod.time = clock
LogThrottle = xlog_mod.LogThrottle
xlog_stub = types.ModuleType("xlog")
xlog_stub.LogThrottle = LogThrottle
xlog_stub.getLogger = lambda *args, **kwargs: None
sys.modules["xlog"] = xlog_stub

PKG = "x_tunnel_local_test"
pkg = types.ModuleType(PKG)
pkg.__path__ = [LOCAL]
sys.modules[PKG] = pkg
spec = importlib.util.spec_from_file_location(
    PKG + ".base_container", os.path.join(LOCAL, "base_container.py"))
base_container = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = base_container
spec.loader.exec_module(base_container)


class CaptureLogger(object):
    def __init__(self):
        self.warnings = []

    def warn(self, fmt, *args):
        self.warnings.append(fmt % args if args else fmt)


def make_pool(logger=None):
    logger = logger or CaptureLogger()
    pool = base_container.BlockReceivePool(lambda data: None, logger)
    return pool, logger


failures = []
def check(name, condition, detail=""):
    print(("PASS" if condition else "FAIL"), name, detail)
    if not condition:
        failures.append(name)


# M1：renew 被聚合时，较新的 server_send_time 仍必须写入。
pool, log = make_pool()
pool.mark_sn_timeout(7, 90.0, 100.0)
pool.mark_sn_timeout(7, 91.0, 100.0)
check("M1 same sn aggregates while state renews",
      len(log.warnings) == 1
      and pool.timeout_sn_list[7]["server_send_time"] == 91.0,
      "warnings=%r info=%r" % (log.warnings, pool.timeout_sn_list))

# M2：不同 SN 各自保留首报。
pool, log = make_pool()
pool.mark_sn_timeout(1, 90.0, 100.0)
pool.mark_sn_timeout(2, 90.0, 100.0)
check("M2 different sn first warnings independent",
      len(log.warnings) == 2 and set(pool.timeout_sn_list) == {1, 2},
      "warnings=%r" % log.warnings)

# M3：下一窗口输出上一窗 suppressed，并保留 renew 原字段。
pool, log = make_pool()
pool.mark_sn_timeout(9, 100.0, 200.0)
pool.mark_sn_timeout(9, 101.0, 200.0)
clock.now += 61.0
pool.mark_sn_timeout(9, 102.0, 200.0)
check("M3 next window settles suppressed",
      len(log.warnings) == 2
      and log.warnings[-1] == "mark_sn_timeout renew sn:9 t:98.000000 (suppressed 1)"
      and pool.timeout_sn_list[9]["server_send_time"] == 102.0,
      "warnings=%r" % log.warnings)

# M4：reset 开启新的 SN 生命周期，同号 SN 不继承旧日志窗口。
pool, log = make_pool()
pool.mark_sn_timeout(11, 100.0, 200.0)
pool.reset()
pool.mark_sn_timeout(11, 110.0, 200.0)
check("M4 reset allows same sn first warning",
      len(log.warnings) == 2
      and log.warnings[-1] == "mark_sn_timeout sn:11 t:90.000000"
      and pool.next_sn == 1 and pool.block_list == []
      and pool.timeout_sn_list[11]["server_send_time"] == 110.0,
      "warnings=%r" % log.warnings)

# M5：并发只压缩日志，不破坏原锁内的状态更新。
pool, log = make_pool()
threads = [threading.Thread(target=pool.mark_sn_timeout,
                            args=(21, 900.0 + i, 1000.0))
           for i in range(20)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
check("M5 concurrent same sn one warning and latest state",
      len(log.warnings) == 1
      and pool.timeout_sn_list[21]["server_send_time"] == 919.0,
      "warnings=%r info=%r" % (log.warnings, pool.timeout_sn_list))

# M6：节流账本属于接收池实例，不跨池共享。
shared_log = CaptureLogger()
first, _ = make_pool(shared_log)
second, _ = make_pool(shared_log)
first.mark_sn_timeout(31, 100.0, 200.0)
second.mark_sn_timeout(31, 100.0, 200.0)
check("M6 pool instances have independent windows",
      len(shared_log.warnings) == 2,
      "warnings=%r" % shared_log.warnings)

# M7：原本不输出日志的重复/旧 t 不得消耗窗口或虚增 suppressed。
pool, log = make_pool()
pool.mark_sn_timeout(41, 100.0, 200.0)
pool.mark_sn_timeout(41, 100.0, 200.0)  # 静默重复，无 allow。
pool.mark_sn_timeout(41, 101.0, 200.0)  # 唯一被抑制的 renew。
clock.now += 61.0
pool.mark_sn_timeout(41, 100.0, 200.0)  # 跨窗旧事件仍应完全静默。
pool.mark_sn_timeout(41, 102.0, 200.0)
check("M7 silent stale events do not consume windows",
      len(log.warnings) == 2
      and log.warnings[-1] == "mark_sn_timeout renew sn:41 t:98.000000 (suppressed 1)"
      and pool.timeout_sn_list[41]["server_send_time"] == 102.0,
      "warnings=%r" % log.warnings)

# M8：reset 构造新账本时，同号 SN 的 mark 不得落入旧窗口。
pool, log = make_pool()
pool.mark_sn_timeout(51, 100.0, 200.0)
real_throttle_type = base_container.LogThrottle
old_throttle = pool.timeout_log_throttle
old_allow_used = threading.Event()
class SignalThrottle(object):
    def allow(self, key):
        old_allow_used.set()
        return old_throttle.allow(key)

    def flush(self):
        return old_throttle.flush()
pool.timeout_log_throttle = SignalThrottle()
reset_constructing = threading.Event()
finish_reset = threading.Event()
def controlled_throttle(interval):
    reset_constructing.set()
    assert finish_reset.wait(2.0)
    return real_throttle_type(interval)
base_container.LogThrottle = controlled_throttle
reset_thread = threading.Thread(target=pool.reset)
reset_thread.start()
assert reset_constructing.wait(1.0)
mark_thread = threading.Thread(target=pool.mark_sn_timeout, args=(51, 110.0, 200.0))
mark_thread.start()
old_allow_used.wait(0.1)  # 新实现中 mark 等 reset 锁；旧实现会在此使用旧账本。
finish_reset.set()
reset_thread.join(2.0)
mark_thread.join(2.0)
base_container.LogThrottle = real_throttle_type
check("M8 reset and mark are atomic for new sn lifecycle",
      not reset_thread.is_alive() and not mark_thread.is_alive()
      and len(log.warnings) == 2
      and log.warnings[-1] == "mark_sn_timeout sn:51 t:90.000000"
      and pool.timeout_sn_list[51]["server_send_time"] == 110.0,
      "old_allow=%s warnings=%r" % (old_allow_used.is_set(), log.warnings))

# M9：reset 结算旧池被抑制的 renew；新池从空账开始，重复 reset 不重复输出。
pool, log = make_pool()
pool.mark_sn_timeout(61, 100.0, 200.0)
pool.mark_sn_timeout(61, 101.0, 200.0)
pool.reset()
pool.reset()
check("M9 reset flushes sn tail once without constructor failure",
      log.warnings == [
          "mark_sn_timeout sn:61 t:100.000000",
          "mark_sn_timeout final suppressed:1 keys:1",
      ] and pool.next_sn == 1 and pool.timeout_sn_list == {},
      "warnings=%r next_sn=%d timeout=%r" %
      (log.warnings, pool.next_sn, pool.timeout_sn_list))

print()
if failures:
    print("RESULT: %d FAILURES: %s" % (len(failures), failures))
    sys.exit(1)
print("RESULT: ALL PASS")
