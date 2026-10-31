#!/usr/bin/env python3
"""I 阶段：dispatcher 重试/超时日志按 front+endpoint 聚合。"""
import importlib.util
import os
import queue
import sys
import tempfile
import threading
import time
import types

sys.dont_write_bytecode = True
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(REPO, "Coding", "XX-Net-5.16.7.1", "code", "default")
NOARCH = os.path.join(BASE, "lib", "noarch")
FRONT_BASE = os.path.join(NOARCH, "front_base")

# 按文件加载真实 xlog.LogThrottle，外部依赖保持最小 stub。
utils_stub = types.ModuleType("utils")
utils_stub.to_bytes = lambda x: x.encode() if isinstance(x, str) else x
utils_stub.to_str = lambda x: x.decode() if isinstance(x, bytes) else str(x)
utils_stub.bytes2str_only = lambda x: x
utils_stub.SimpleCondition = type("SimpleCondition", (), {})
sys.modules["utils"] = utils_stub
six_stub = types.ModuleType("six")
six_moves = types.ModuleType("six.moves")
six_moves.queue = queue
six_stub.string_types = str
sys.modules["six"] = six_stub
sys.modules["six.moves"] = six_moves

xlog_path = os.path.join(NOARCH, "xlog.py")
xlog_mod = types.ModuleType("xlog_under_test")
with open(xlog_path) as fp:
    exec(compile(fp.read(), xlog_path, "exec"), xlog_mod.__dict__)
LogThrottle = xlog_mod.LogThrottle
xlog_stub = types.ModuleType("xlog")
xlog_stub.LogThrottle = LogThrottle
sys.modules["xlog"] = xlog_stub

PKG = "front_base_retry_test"
pkg = types.ModuleType(PKG)
pkg.__path__ = [FRONT_BASE]
sys.modules[PKG] = pkg

simple_http_client = types.ModuleType("simple_http_client")
class BaseResponse(object):
    def __init__(self, body=""):
        self.body = body
simple_http_client.BaseResponse = BaseResponse
sys.modules["simple_http_client"] = simple_http_client


def load_mod(name, path):
    spec = importlib.util.spec_from_file_location(PKG + "." + name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


http_common = load_mod("http_common", os.path.join(FRONT_BASE, "http_common.py"))
http1_stub = types.ModuleType(PKG + ".http1")
http1_stub.Http1Worker = type("Http1Worker", (), {})
sys.modules[PKG + ".http1"] = http1_stub
http2_stub = types.ModuleType(PKG + ".http2_connection")
http2_stub.Http2Worker = type("Http2Worker", (), {})
http2_stub.Stream = type("Stream", (), {})
sys.modules[PKG + ".http2_connection"] = http2_stub
hd = load_mod("http_dispatcher", os.path.join(FRONT_BASE, "http_dispatcher.py"))


class CaptureLogger(object):
    def __init__(self):
        self.warnings = []

    def warn(self, fmt, *args):
        self.warnings.append(fmt % args if args else fmt)

    def debug(self, fmt, *args):
        pass

    def exception(self, fmt, *args):
        self.warn(fmt, *args)


class Notify(object):
    def __init__(self):
        self.count = 0

    def notify(self):
        self.count += 1


failures = []
def check(name, condition, detail=""):
    print(("PASS" if condition else "FAIL"), name, detail)
    if not condition:
        failures.append(name)


def make_dispatcher(logger):
    disp = hd.HttpsDispatcher.__new__(hd.HttpsDispatcher)
    disp.logger = logger
    disp.config = types.SimpleNamespace(
        max_task_num=1000, dispather_worker_max_continue_fail=3,
        show_state_debug=False)
    disp.task_count = 0
    disp.task_count_lock = threading.Lock()
    disp.request_queue = queue.Queue()
    disp.trigger_create_worker_cv = Notify()
    disp.running = True
    disp.success_num = 0
    disp.fail_num = 0
    disp.continue_fail_num = 0
    disp.last_fail_time = 0
    disp.last_request_time = 0
    disp.last_host = None
    disp.retry_log_throttle = LogThrottle(60)
    return disp


def make_task(disp, tid, front="198.18.0.159", host=b"ny3.xx-net.org",
              endpoint=b"/data", method=b"POST"):
    path = endpoint + (b"?tid=%d" % tid)
    url = method + b" " + host + path
    task = http_common.Task(disp.logger, disp.config, method, host, path,
                            {}, b"", queue.Queue(), url, 100)
    if front is not None:
        task.worker = types.SimpleNamespace(ip_str=front, continue_fail_tasks=0)
    return task


def retry_once(disp, task):
    disp.retry_task_cb(task)
    queued = disp.request_queue.get_nowait()
    assert queued is task


log = CaptureLogger()
disp = make_dispatcher(log)
retry_once(disp, make_task(disp, 1))
retry_once(disp, make_task(disp, 2))
check("R1 same front and endpoint ignores tid",
      len(log.warnings) == 1 and disp.fail_num == 2,
      "warnings=%r fail=%d" % (log.warnings, disp.fail_num))
retry_once(disp, make_task(disp, 3, front="198.18.0.160"))
check("R2 different front independent", len(log.warnings) == 2,
      "warnings=%r" % log.warnings)
retry_once(disp, make_task(disp, 4, host=b"other.example"))
check("R3 different host independent", len(log.warnings) == 3,
      "warnings=%r" % log.warnings)
retry_once(disp, make_task(disp, 5, endpoint=b"/other"))
check("R4 different endpoint independent", len(log.warnings) == 4,
      "warnings=%r" % log.warnings)

# retry 与 request timeout 共用同一 front/endpoint 窗口；worker=None 也安全。
shared_log = CaptureLogger()
shared = make_dispatcher(shared_log)
retry_once(shared, make_task(shared, 10, front=None))
result = shared.request("POST", "ny3.xx-net.org", "/data?tid=11", {}, b"", timeout=0)
check("R5 request timeout shares retry window and none result preserved",
      result is None and len(shared_log.warnings) == 1
      and shared.fail_num == 2 and shared.task_count == 0
      and shared.trigger_create_worker_cv.count == 1,
      "warnings=%r fail=%d count=%d notify=%d" %
      (shared_log.warnings, shared.fail_num, shared.task_count,
       shared.trigger_create_worker_cv.count))

# 非 200 status 也按同一 key 节流，response 原样返回。
status_log = CaptureLogger()
status_disp = make_dispatcher(status_log)
def status_request(tid, path=None):
    response = types.SimpleNamespace(status=602)
    original_queue = hd.Queue
    hd.Queue = lambda: queue.Queue()
    q = hd.Queue()
    q.put(response)
    hd.Queue = lambda: q
    try:
        return status_disp.request("POST", "ny3.xx-net.org",
                                   path or "/data?tid=%d" % tid,
                                   {}, b"", timeout=0)
    finally:
        hd.Queue = original_queue
r1 = status_request(20)
r2 = status_request(21)
check("R6 status responses unchanged while warnings aggregate",
      r1.status == 602 and r2.status == 602 and len(status_log.warnings) == 1,
      "warnings=%r" % status_log.warnings)

# 下一窗口结算上一窗计数，完整 URL/tid 仍保留在放行行。
settle_log = CaptureLogger()
settle = make_dispatcher(settle_log)
retry_once(settle, make_task(settle, 30))
retry_once(settle, make_task(settle, 31))
settle.retry_log_throttle.interval = 0.01
time.sleep(0.02)
retry_once(settle, make_task(settle, 32))
check("R7 next window reports suppressed and retains full url",
      len(settle_log.warnings) == 2
      and "tid=32" in settle_log.warnings[-1]
      and settle_log.warnings[-1].endswith("(suppressed 1)"),
      "warnings=%r" % settle_log.warnings)

# 并发只抑制日志，不改变每个 task 的重试入队与计数。
concurrent_log = CaptureLogger()
concurrent = make_dispatcher(concurrent_log)
tasks = [make_task(concurrent, 100 + i) for i in range(20)]
threads = [threading.Thread(target=retry_once, args=(concurrent, task)) for task in tasks]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
check("R8 concurrent retries one warn with all state and queue deliveries",
      len(concurrent_log.warnings) == 1 and concurrent.fail_num == 20
      and all(task.retry_count == 1 for task in tasks),
      "warnings=%d fail=%d retries=%r" %
      (len(concurrent_log.warnings), concurrent.fail_num,
       [task.retry_count for task in tasks]))

# 日志 key 不得因严格解码改变非 UTF-8 path 的重试入队或响应返回。
raw_log = CaptureLogger()
raw_disp = make_dispatcher(raw_log)
raw_task = make_task(raw_disp, 90, endpoint=b"/data-\xff")
raw_retry_error = None
try:
    retry_once(raw_disp, raw_task)
except UnicodeDecodeError as e:
    raw_retry_error = e
raw_response = status_request(91, path=b"/data-\xff?tid=91")
check("R9 non utf8 path preserves retry and response behavior",
      raw_retry_error is None and raw_task.retry_count == 1
      and raw_response is not None and raw_response.status == 602
      and status_disp.fail_num == 3 and status_disp.trigger_create_worker_cv.count == 3,
      "retry_error=%r retry=%d response=%r fail=%d notify=%d" %
      (raw_retry_error, raw_task.retry_count, raw_response,
       status_disp.fail_num, status_disp.trigger_create_worker_cv.count))

# host 缺失只影响诊断 URL 的展示，不得让 request 落入异常出口。
none_log = CaptureLogger()
none_disp = make_dispatcher(none_log)
none_result = none_disp.request("POST", None, "/data?tid=92", {}, b"", timeout=0)
check("R10 none host keeps request failure semantics without log exception",
      none_result is None and none_disp.last_host is None
      and none_disp.fail_num == 1 and none_disp.task_count == 0
      and len(none_log.warnings) == 1
      and "timeout" in none_log.warnings[0]
      and "http_dispatcher:request" not in none_log.warnings[0],
      "warnings=%r host=%r fail=%d count=%d" %
      (none_log.warnings, none_disp.last_host, none_disp.fail_num, none_disp.task_count))

# stop 在关闭 worker 后结算 dispatcher 的聚合尾账，不输出逐 key 日志。
flush_log = CaptureLogger()
flush_disp = make_dispatcher(flush_log)
flush_disp.workers = []
flush_disp.h1_num = 0
flush_disp.h2_num = 0
retry_once(flush_disp, make_task(flush_disp, 101))
retry_once(flush_disp, make_task(flush_disp, 102))
flush_stop_error = None
try:
    flush_disp.stop()
    flush_disp.stop()
except Exception as e:
    flush_stop_error = e
check("R11 stop flushes dispatcher tail once",
      flush_stop_error is None and not flush_disp.running
      and flush_log.warnings == [
          "retry_task_cb: b'POST ny3.xx-net.org/data?tid=101', trace::0",
          "dispatcher final suppressed:1 keys:1",
      ]
      and flush_disp.fail_num == 2,
      "warnings=%r fail=%d error=%r" %
      (flush_log.warnings, flush_disp.fail_num, flush_stop_error))

# 首次 stop 后的真实 retry 回调仍走原有 stopped 失败出口，但不得重新开账。
assert flush_disp.request_queue.get_nowait() is None
assert flush_disp.request_queue.get_nowait() is None
flush_disp.retry_task_cb(make_task(flush_disp, 103, front="198.18.0.199"))
flush_disp.retry_task_cb(make_task(flush_disp, 104, front="198.18.0.199"))
flush_disp.stop()
check("R12 late retries after stop do not reopen lifecycle ledger",
      flush_log.warnings == [
          "retry_task_cb: b'POST ny3.xx-net.org/data?tid=101', trace::0",
          "dispatcher final suppressed:1 keys:1",
          "b'POST ny3.xx-net.org/data?tid=103' response_fail:retry but stopped.",
          "b'POST ny3.xx-net.org/data?tid=104' response_fail:retry but stopped.",
      ] and flush_disp.fail_num == 4 and not flush_disp.retry_log_throttle.hits,
      "warnings=%r fail=%d hits=%r" %
      (flush_log.warnings, flush_disp.fail_num, flush_disp.retry_log_throttle.hits))

print()
if failures:
    print("RESULT: %d FAILURES: %s" % (len(failures), failures))
    sys.exit(1)
print("RESULT: ALL PASS")
