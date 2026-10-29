#!/usr/bin/env python3
"""F/G-phase offline tests: xlog rolling and log throttling.

Loads the modified modules by file path with stubbed external deps,
runs everything in a temp dir, never touches the running launcher.

Checks:
 A1) warning log under cap -> no roll
 A2) writing past warning_file_max_size -> rolls once to .1, main file
     stays small, writes continue into the new main file
 A3) second overflow -> .1 replaced (single generation, no .2)
 A4) .1 kept intact at open time when main file oversized (init-time roll)
 B)  LogThrottle: first hit allowed, window hits suppressed, suppressed
     count reported on next allowed hit, prune keeps dict bounded
 C)  HEAD probe failures aggregate by front ip_str across all exits and
     concurrent workers, with the previous window count on the next log
 D)  not-enough-ip lines settle the previous window and recovery emits
     the tail plus drought total exactly once before clearing the ledger
"""
import os
import sys
import tempfile
import types

sys.dont_write_bytecode = True

TMP = tempfile.mkdtemp(prefix="xlog_roll_test_")

# --- stub out xlog's imports so it loads standalone ---
utils_stub = types.ModuleType("utils")
utils_stub.bytes2str_only = lambda args: args
utils_stub.to_str = lambda x: x
utils_stub.to_bytes = lambda x: x
sys.modules["utils"] = utils_stub

six_stub = types.ModuleType("six")
six_moves = types.ModuleType("six.moves")
six_stub.string_types = str
sys.modules["six"] = six_stub
sys.modules["six.moves"] = six_moves

XLOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "code", "default", "lib", "noarch", "xlog.py")
with open(XLOG_PATH) as f:
    src = f.read()

import types as _t
xlog_mod = _t.ModuleType("xlog_under_test")
exec(compile(src, XLOG_PATH, "exec"), xlog_mod.__dict__)
Logger = xlog_mod.Logger
LogThrottle = xlog_mod.LogThrottle

failures = []
def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        failures.append(name)

LOG_DIR = os.path.join(TMP, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# --- A1/A2/A3: roll during writes ---
lg = Logger("mod", log_path=LOG_DIR, save_warning_log=True)
wl = os.path.join(LOG_DIR, "mod_warning.log")
b1 = os.path.join(LOG_DIR, "mod_warning.log.1")

lg.error("before cap")  # goes to warning log
sz = os.path.getsize(wl)
check("A1 under-cap no roll", sz > 0 and not os.path.exists(b1),
      "size=%d" % sz)

# force roll by shrinking the cap (same code path as real 10MB overflow)
lg.warning_file_max_size = 100
big = "x" * 80
for i in range(5):
    lg.error("overflow %d %s" % (i, big))

check("A2 rolled to .1", os.path.exists(b1), "")
check("A2 main reopened and small", os.path.getsize(wl) < 400,
      "size=%d" % os.path.getsize(wl))
first_gen = open(b1).read()
# .1 holds the most recently rolled generation: later overflow lines
# rolled again and replaced the first generation ("before cap" was in
# the first generation only).
check("A2 .1 holds rolled content", "overflow" in first_gen)

# keep writing: more lines must land in the new main file
lg.error("after roll line")
check("A2 write continues after roll",
      "after roll line" in open(wl).read())

# second overflow: .1 must be replaced, no .2 created
for i in range(5):
    lg.error("overflow2 %d %s" % (i, big))
check("A3 single generation, .1 replaced",
      os.path.exists(b1) and "overflow2" in open(b1).read()
      and not os.path.exists(b1 + ".2"))

# --- A4: init/write-time roll when file already oversized ---
lg2 = Logger("mod", log_path=LOG_DIR, save_warning_log=True)
lg2.warning_file_max_size = 100
lg2.error("trigger init roll")
main_txt = open(wl).read()
b1_txt = open(b1).read()
# the oversized pre-existing main was rolled away and the new line
# landed in a fresh main file
check("A4 roll cleared oversized main", "trigger init roll" in main_txt
      and "trigger init roll" not in b1_txt and "overflow" in b1_txt,
      "main=%dB .1=%dB" % (len(main_txt), len(b1_txt)))

# --- B: LogThrottle semantics ---
lt = LogThrottle(60)
allowed, sup = lt.allow("url-a")
check("B1 first hit allowed", allowed and sup == 0)
allowed, sup = lt.allow("url-a")
check("B2 window hit suppressed", not allowed and sup == 1)
lt.allow("url-a"); lt.allow("url-a")
allowed, sup = lt.allow("url-a")
check("B3 suppressed count accumulates", not allowed and sup == 4,
      "sup=%d" % sup)
allowed, sup = lt.allow("url-b")
check("B4 independent keys", allowed and sup == 0)
lt2 = LogThrottle(0.01)
allowed, _ = lt2.allow("k")
import time as _time
_time.sleep(0.02)
allowed, sup = lt2.allow("k")
check("B5 window expiry re-allows with 0 suppressed", allowed and sup == 0)

# concurrent hammering: dict must stay bounded, no exceptions
import threading
lt3 = LogThrottle(60)
def hammer(n):
    for i in range(n):
        lt3.allow("k-%d" % (i % 800))
threads = [threading.Thread(target=hammer, args=(200,)) for _ in range(8)]
[t.start() for t in threads]
[t.join() for t in threads]
check("B6 dict bounded under concurrency", len(lt3.hits) <= 512,
      "len=%d" % len(lt3.hits))

# --- C: G-phase HEAD probe throttling, aggregated by front ip_str ---
# Load the real worker/connect-manager modules with only their external
# dependencies stubbed. No running process is imported or touched.
import importlib.util

FRONT_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "code", "default", "lib", "noarch", "front_base")
PKG = "front_base_under_test"
pkg_stub = types.ModuleType(PKG)
pkg_stub.__path__ = [FRONT_BASE]
sys.modules[PKG] = pkg_stub

http_common_stub = types.ModuleType(PKG + ".http_common")
http_common_stub.time = _time
http_common_stub.HttpWorker = type("HttpWorker", (), {})
sys.modules[PKG + ".http_common"] = http_common_stub

openssl_wrap_stub = types.ModuleType(PKG + ".openssl_wrap")
openssl_wrap_stub.SSLConnection = type("SSLConnection", (), {})
sys.modules[PKG + ".openssl_wrap"] = openssl_wrap_stub

xlog_stub = types.ModuleType("xlog")
xlog_stub.LogThrottle = LogThrottle
sys.modules["xlog"] = xlog_stub

class ResponseStub(object):
    status = 500

    def __init__(self, sock):
        self.sock = sock

    def begin(self, timeout):
        pass

simple_http_client_stub = types.ModuleType("simple_http_client")
simple_http_client_stub.Response = ResponseStub
sys.modules["simple_http_client"] = simple_http_client_stub

def load_front_module(name):
    path = os.path.join(FRONT_BASE, name + ".py")
    spec = importlib.util.spec_from_file_location(PKG + "." + name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod

http1_mod = load_front_module("http1")
connect_manager_mod = load_front_module("connect_manager")
Http1Worker = http1_mod.Http1Worker

class CaptureLogger(object):
    def __init__(self):
        self.warnings = []

    def warn(self, fmt, *args):
        self.warnings.append(fmt % args if args else fmt)

    warning = warn

    def debug(self, fmt, *args):
        pass

    def exception(self, fmt, *args):
        self.warn(fmt, *args)

class HeadSocket(object):
    def __init__(self, ip_str, mode):
        self.ip_str = ip_str
        self.host = b"example.com"
        self.sni = "example.com"
        self.mode = mode

    def send(self, data):
        if self.mode == "exception":
            raise RuntimeError("head boom")
        if self.mode == "short-send":
            return len(data) - 1
        return len(data)

def make_head_worker(ip_str, mode, logger):
    worker = Http1Worker.__new__(Http1Worker)
    worker.logger = logger
    worker.ssl_sock = HeadSocket(ip_str, mode)
    worker.ip_str = ip_str
    worker.config = types.SimpleNamespace(http1_trace_size=20)
    worker.trace_time = []
    worker.processed_tasks = 0
    worker.transfered_size = 0
    worker.last_recv_time = 0
    worker.request_onway = False
    return worker

head_log = CaptureLogger()
Http1Worker.head_fail_throttle = LogThrottle(60)
w1 = make_head_worker("192.0.2.1", "exception", head_log)
check("C1 exception exit first hit allowed", w1.head_request() is False
      and len(head_log.warnings) == 2
      and "HEAD keep alive request fail" in head_log.warnings[-2],
      "warnings=%r" % head_log.warnings)
w2 = make_head_worker("192.0.2.1", "short-send", head_log)
check("C2 same ip short send suppressed", w2.head_request() is False
      and len(head_log.warnings) == 2,
      "warnings=%r" % head_log.warnings)
Http1Worker.head_fail_throttle.interval = 0.01
_time.sleep(0.02)
w3 = make_head_worker("192.0.2.1", "status", head_log)
check("C3 expired same ip status logs count and trace",
      w3.head_request() is False and len(head_log.warnings) == 4
      and "head fail status:500" in head_log.warnings[-2]
      and head_log.warnings[-1].endswith("(suppressed 1)"),
      "warnings=%r" % head_log.warnings)
Http1Worker.head_fail_throttle.interval = 60
w4 = make_head_worker("192.0.2.2", "exception", head_log)
check("C4 different ip independent", w4.head_request() is False
      and len(head_log.warnings) == 6,
      "warnings=%r" % head_log.warnings)

concurrent_log = CaptureLogger()
Http1Worker.head_fail_throttle = LogThrottle(60)
concurrent_results = []
def head_hammer():
    worker = make_head_worker("192.0.2.3", "exception", concurrent_log)
    concurrent_results.append(worker.head_request())
threads = [threading.Thread(target=head_hammer) for _ in range(20)]
[t.start() for t in threads]
[t.join() for t in threads]
check("C5 concurrent same ip emits one warn/trace",
      concurrent_results == [False] * 20 and len(concurrent_log.warnings) == 2,
      "warnings=%d" % len(concurrent_log.warnings))
Http1Worker.head_fail_throttle.interval = 0.01
_time.sleep(0.02)
w5 = make_head_worker("192.0.2.3", "exception", concurrent_log)
w5.head_request()
check("C6 concurrent suppressed count settled",
      concurrent_log.warnings[-1].endswith("(suppressed 19)"),
      "trace=%r" % concurrent_log.warnings[-1])

# --- D: G-phase not-enough-ip window counts and recovery settlement ---
class FakeTime(object):
    def __init__(self):
        self.now = 100.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

fake_time = FakeTime()
connect_manager_mod.time = fake_time
cm_log = CaptureLogger()
cm = connect_manager_mod.ConnectManager.__new__(connect_manager_mod.ConnectManager)
cm.logger = cm_log
cm.config = types.SimpleNamespace(show_state_debug=False, connect_create_interval=0)
cm.ip_manager = types.SimpleNamespace(get_ip_sni_host=lambda: None)
cm.no_ip_lock = threading.Lock()
cm.no_ip_time = 0
cm.last_no_ip_warn = 0
cm.suppressed_no_ip_warn = 0
cm.total_no_ip_events = 0
cm.new_conn_pool = connect_manager_mod.ConnectPool()
recovered_sock = HeadSocket("192.0.2.4", "status")
recovered_sock.handshake_time = 1
cm._create_ssl_connection = lambda host_info: recovered_sock

cm._connect_process()
check("D1 first drought line and total event count",
      cm_log.warnings == [
          "not enough ip (suppressed 0 last window, suppressing for 60s after this line)"]
      and cm.total_no_ip_events == 1,
      "warnings=%r total=%r" % (cm_log.warnings, cm.total_no_ip_events))
fake_time.now = 101.0
cm._connect_process()
fake_time.now = 160.0
cm._connect_process()
check("D2 next allowed line settles previous window",
      cm_log.warnings[-1] ==
      "not enough ip (suppressed 1 last window, suppressing for 60s after this line)",
      "warnings=%r" % cm_log.warnings)
fake_time.now = 161.0
cm._connect_process()
cm.ip_manager.get_ip_sni_host = lambda: {"ip_str": "192.0.2.4", "sni": "s", "host": "h"}
fake_time.now = 162.0
check("D3 recovery returns connection and logs tail plus total",
      cm._connect_process() is recovered_sock
      and cm_log.warnings[-1] == "not enough ip recovered (suppressed 1, total 4)",
      "warnings=%r" % cm_log.warnings)
cm._connect_process()
check("D4 recovery is once and ledger cleared",
      len(cm_log.warnings) == 3 and cm.last_no_ip_warn == 0
      and cm.suppressed_no_ip_warn == 0 and cm.total_no_ip_events == 0,
      "warnings=%r last=%r suppressed=%r total=%r" %
      (cm_log.warnings, cm.last_no_ip_warn, cm.suppressed_no_ip_warn,
       cm.total_no_ip_events))

# --- E: H-phase _connect_ssl failure aggregation by front and error type ---
from queue import Queue

def make_connect_ssl_manager(logger, error_type=RuntimeError):
    manager = connect_manager_mod.ConnectManager.__new__(connect_manager_mod.ConnectManager)
    manager.logger = logger
    manager.config = types.SimpleNamespace(show_state_debug=False)
    def fail_connect(*args, **kwargs):
        raise error_type("connect boom")
    manager.connect_creator = types.SimpleNamespace(connect_ssl=fail_connect)
    manager.connect_ssl_log_throttle = LogThrottle(60)
    return manager

def connect_ssl_once(manager, ip_str, result_queue):
    manager._connect_ssl(ip_str, "sni", "host", None, result_queue)

connect_log = CaptureLogger()
connect_manager = make_connect_ssl_manager(connect_log)
connect_queue = Queue()
connect_ssl_once(connect_manager, "192.0.2.10", connect_queue)
check("E1 first connect failure allowed and exception delivered",
      len(connect_log.warnings) == 1 and connect_queue.qsize() == 1,
      "warnings=%r queue=%d" % (connect_log.warnings, connect_queue.qsize()))
connect_ssl_once(connect_manager, "192.0.2.10", connect_queue)
check("E2 same front failure suppressed but queue unchanged",
      len(connect_log.warnings) == 1 and connect_queue.qsize() == 2,
      "warnings=%r queue=%d" % (connect_log.warnings, connect_queue.qsize()))
connect_ssl_once(connect_manager, "192.0.2.11", connect_queue)
check("E3 different front independent", len(connect_log.warnings) == 2,
      "warnings=%r" % connect_log.warnings)
value_error_manager = make_connect_ssl_manager(connect_log, ValueError)
value_error_manager.connect_ssl_log_throttle = connect_manager.connect_ssl_log_throttle
connect_ssl_once(value_error_manager, "192.0.2.10", connect_queue)
check("E4 different exception class independent", len(connect_log.warnings) == 3,
      "warnings=%r" % connect_log.warnings)
connect_manager.connect_ssl_log_throttle.interval = 0.01
_time.sleep(0.02)
connect_ssl_once(connect_manager, "192.0.2.10", connect_queue)
check("E5 expired front logs previous suppressed count",
      len(connect_log.warnings) == 4
      and connect_log.warnings[-1].endswith("(suppressed 1)"),
      "warnings=%r" % connect_log.warnings)

concurrent_connect_log = CaptureLogger()
concurrent_manager = make_connect_ssl_manager(concurrent_connect_log)
concurrent_queue = Queue()
threads = [threading.Thread(target=connect_ssl_once,
                            args=(concurrent_manager, "192.0.2.12", concurrent_queue))
           for _ in range(20)]
[t.start() for t in threads]
[t.join() for t in threads]
check("E6 concurrent same front one warn and all exceptions delivered",
      len(concurrent_connect_log.warnings) == 1 and concurrent_queue.qsize() == 20,
      "warnings=%r queue=%d" % (concurrent_connect_log.warnings, concurrent_queue.qsize()))
concurrent_manager.connect_ssl_log_throttle.interval = 0.01
_time.sleep(0.02)
connect_ssl_once(concurrent_manager, "192.0.2.12", concurrent_queue)
check("E7 concurrent suppressed count settled",
      concurrent_connect_log.warnings[-1].endswith("(suppressed 19)"),
      "warnings=%r" % concurrent_connect_log.warnings)

print()
if failures:
    print("RESULT: %d FAILURES: %s" % (len(failures), failures))
    sys.exit(1)
print("RESULT: ALL PASS")
