#!/usr/bin/env python3
"""I 阶段：x_tunnel 同一 transfer 的故障日志聚合，传输行为保持不变。"""
import importlib.util
import os
import sys
import threading
import time
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


# 加载真实 xlog.LogThrottle；只替换其时钟，窗口测试不依赖实际等待。
clock = Clock()
utils_stub = types.ModuleType("utils")
utils_stub.to_bytes = lambda value: value.encode() if isinstance(value, str) else value
utils_stub.to_str = lambda value: value.decode() if isinstance(value, bytes) else str(value)
utils_stub.bytes2str_only = lambda value: value
utils_stub.str2hex = lambda value: bytes(value).hex()
utils_stub.generate_random_lowercase = lambda length: "s" * length
utils_stub.SimpleCondition = type("SimpleCondition", (), {})
sys.modules["utils"] = utils_stub
six_stub = types.ModuleType("six")
six_stub.string_types = str
sys.modules["six"] = six_stub

xlog_path = os.path.join(NOARCH, "xlog.py")
xlog_mod = types.ModuleType("xlog_under_test")
with open(xlog_path) as fp:
    exec(compile(fp.read(), xlog_path, "exec"), xlog_mod.__dict__)
xlog_mod.time = clock
LogThrottle = xlog_mod.LogThrottle


class CaptureLogger(object):
    def __init__(self):
        self.warnings = []

    def debug(self, fmt, *args):
        pass

    def info(self, fmt, *args):
        pass

    def warn(self, fmt, *args):
        self.warnings.append(fmt % args if args else fmt)

    def error(self, fmt, *args):
        pass

    def exception(self, fmt, *args):
        self.warn(fmt, *args)


proxy_log = CaptureLogger()
front_log = CaptureLogger()
xlog_stub = types.ModuleType("xlog")
xlog_stub.LogThrottle = LogThrottle
xlog_stub.getLogger = lambda *args, **kwargs: proxy_log
xlog_stub.keep_log = lambda **kwargs: None
sys.modules["xlog"] = xlog_stub


class Notify(object):
    def __init__(self):
        self.count = 0
        self.waiters = []

    def notify(self):
        self.count += 1

    def wait(self, work_id):
        pass


class Buffer(object):
    pool_size = 0


class Passive(object):
    def __init__(self, *args, **kwargs):
        pass


class WriteBuffer(object):
    def __init__(self, data=None):
        self.parts = [] if data is None else [data]

    def append(self, data):
        self.parts.append(data)

    def to_bytes(self):
        return b"".join(self.parts)


base_stub = types.ModuleType("xtunnel_test.base_container")
base_stub.WaitQueue = Notify
base_stub.SendBuffer = lambda **kwargs: Buffer()
base_stub.BlockReceivePool = Passive
base_stub.ConnectionPipe = Passive
base_stub.WriteBuffer = WriteBuffer
base_stub.ReadBuffer = Passive
base_stub.Conn = Passive

PKG = "xtunnel_test"
pkg = types.ModuleType(PKG)
pkg.__path__ = [LOCAL]
sys.modules[PKG] = pkg
sys.modules[PKG + ".base_container"] = base_stub

encrypt_stub = types.ModuleType("encrypt")
encrypt_stub.Encryptor = Passive
sys.modules["encrypt"] = encrypt_stub
gae_pkg = types.ModuleType("gae_proxy")
gae_pkg.__path__ = []
gae_local = types.ModuleType("gae_proxy.local")
gae_local.check_local_network = types.SimpleNamespace(IPv6=Passive)
sys.modules["gae_proxy"] = gae_pkg
sys.modules["gae_proxy.local"] = gae_local
upload_logs_stub = types.ModuleType(PKG + ".upload_logs")
upload_logs_stub.upload_logs_thread = lambda: None
sys.modules[PKG + ".upload_logs"] = upload_logs_stub

# 后台线程只记录创建，不运行；真实 ProxySession.__init__ 必须在线程前建好节流器。
created_threads = []
real_thread = threading.Thread
class ThreadStub(object):
    def __init__(self, target=None, name=None, args=()):
        self.target = target
        self.name = name
        self.args = args
        self.started = False
        created_threads.append(self)

    def start(self):
        self.started = True
        assert hasattr(self.target.__self__, "transfer_log_throttle")


def load_mod(name):
    path = os.path.join(LOCAL, name + ".py")
    spec = importlib.util.spec_from_file_location(PKG + "." + name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


g = load_mod("global_var")
g.config = types.SimpleNamespace(
    max_payload=1024, send_delay=0, ack_delay=0, resend_timeout=1000,
    enable_tls_relay=False, upload_logs=False, server_time_max_deviation=9999,
    send_timeout_retry=5, concurent_thread_num=10, min_on_road=1,
    roundtrip_timeout=30, network_timeout=1, show_debug=False,
    encrypt_data=False,
)
g.running = True
g.server_host = "server.example"
g.quota = 100000

proxy = load_mod("proxy_session")
proxy.threading.Thread = ThreadStub
proxy.xlog = proxy_log

# front_dispatcher 与 proxy_session 使用各自的真实 logger 捕获。
xlog_stub.getLogger = lambda *args, **kwargs: front_log
env_stub = types.ModuleType("env_info")
env_stub.data_path = os.path.join(REPO, "tests")
sys.modules["env_info"] = env_stub
front = load_mod("front_dispatcher")


def make_session(session_id=b"session-a"):
    session = proxy.ProxySession()
    session.running = True
    session.session_id = session_id
    return session


def timeout_info(session, transfer_no=7, stat="timeout"):
    return {
        "session_id": session.session_id,
        "transfer_no": transfer_no,
        "request_session_id": session.session_id,
        "stat": stat,
        "server_received": False,
        "server_sent": False,
        "start_time": time.time() - 10,
        "server_timeout": 0,
        "send_data": b"",
        "send_ack": b"",
        "download_timeout": b"",
        "retry": 0,
    }


failures = []
def check(name, condition, detail=""):
    print(("PASS" if condition else "FAIL"), name, detail)
    if not condition:
        failures.append(name)


# X1：真实初始化提供实例级节流；同 key 聚合，session/transfer/kind 均隔离。
key_log = proxy_log
s1 = make_session(b"session-a")
s2 = make_session(b"session-b")
s1.transfer_warn(b"session-a",7, "retry", "retry transfer_no:%d", 7)
s1.transfer_warn(b"session-a",7, "retry", "retry transfer_no:%d", 7)
s1.transfer_warn(b"session-a",8, "retry", "retry transfer_no:%d", 8)
s1.transfer_warn(b"session-a",7, "roundtrip_failed", "roundtrip failed, no:%d", 7)
s2.transfer_warn(b"session-b", 7, "retry", "retry transfer_no:%d", 7)
check("X1 session transfer kind isolate warnings",
      len(key_log.warnings) == 4,
      "warnings=%r" % key_log.warnings)

# X2：真实 retry 路径仍逐项迁移状态并计数，但同 transfer 只放行一条。
before = len(key_log.warnings)
info = timeout_info(s1, 21)
s1.transfer_list[21] = info
for _ in range(2):
    info["stat"] = "timeout"
    returned = s1.get_upload_data(0)
    assert returned is info
check("X2 retries preserve state while aggregating",
      len(key_log.warnings) == before + 1 and info["retry"] == 2
      and info["stat"] == "retry",
      "new_warnings=%r retry=%d stat=%s" %
      (key_log.warnings[before:], info["retry"], info["stat"]))

# X3：check_upload_not_acked 的 timeout、target 和 notify 不受日志抑制影响。
before = len(key_log.warnings)
check_info = timeout_info(s1, 31, stat="request")
s1.transfer_list = {31: check_info}
s1.server_time_offset = 0
s1.server_time_deviation = 0
check_info["start_time"] = time.time() - 10
server_time = time.time()
s1.check_upload_not_acked(server_time)
check_info["stat"] = "request"
s1.check_upload_not_acked(server_time)
check("X3 ack timeout state target notify unchanged",
      len(key_log.warnings) == before + 1 and check_info["stat"] == "timeout"
      and s1.target_on_roads == 2 and s1.wait_queue.count == 0,
      "new_warnings=%r stat=%s target=%d notify=%d" %
      (key_log.warnings[before:], check_info["stat"], s1.target_on_roads,
       s1.wait_queue.count))

# X4：下一窗口结算 suppressed，原始字段保留。
before = len(key_log.warnings)
s1.transfer_warn(b"session-a",41, "retry", "retry transfer_no:%d", 41)
s1.transfer_warn(b"session-a",41, "retry", "retry transfer_no:%d", 41)
clock.now += 61
s1.transfer_warn(b"session-a",41, "retry", "retry transfer_no:%d", 41)
check("X4 next window settles suppressed",
      len(key_log.warnings) == before + 2
      and key_log.warnings[-1].endswith("retry transfer_no:41 (suppressed 1)"),
      "new_warnings=%r" % key_log.warnings[before:])

# X5：并发同 key 仅一条 WARN。
concurrent_session = make_session(b"concurrent")
before = len(key_log.warnings)
threads = [real_thread(target=concurrent_session.transfer_warn,
                            args=(b"concurrent", 51, "retry", "retry transfer_no:%d", 51))
           for _ in range(20)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
check("X5 concurrent same transfer one warning",
      len(key_log.warnings) == before + 1,
      "new_warnings=%r" % key_log.warnings[before:])

# X6：真实 roundtrip 非 200 与最终 failed 使用不同 kind，且计数、状态、通知保持。
roundtrip_session = make_session(b"roundtrip")
roundtrip_session.conn_list = {}
roundtrip_session.get_data_lock = threading.Lock()
roundtrip_session.transfer_log_throttle = LogThrottle(60)
roundtrip_session.transfer_list = {}
trip = timeout_info(roundtrip_session, 61, stat="request")
trip["start_time"] = time.time()
roundtrip_session.transfer_list[61] = trip
roundtrip_session.get_upload_data = lambda work_id: trip
responses = [(b"", 503, None), (b"", 503, None)]
class HttpClient(object):
    def request(self, **kwargs):
        return responses.pop(0)
g.http_client = HttpClient()
original_sleep = proxy.time.sleep
proxy.time.sleep = lambda seconds: None
before = len(key_log.warnings)
roundtrip_session.roundtrip_task(0)
roundtrip_session.roundtrip_task(0)
proxy.time.sleep = original_sleep
check("X6 status and final failed aggregate without behavior changes",
      len(key_log.warnings) == before + 2
      and sum("status:503" in line for line in key_log.warnings[before:]) == 1
      and sum("roundtrip failed, no:61" in line for line in key_log.warnings[before:]) == 1
      and trip["stat"] == "timeout" and g.stat["roundtrip_num"] == 2
      and roundtrip_session.on_road_num == 0 and roundtrip_session.wait_queue.count == 2,
      "new_warnings=%r stat=%s roundtrips=%d on_road=%d notify=%d" %
      (key_log.warnings[before:], trip["stat"], g.stat["roundtrip_num"],
       roundtrip_session.on_road_num, roundtrip_session.wait_queue.count))

# X7：front retry 同 tid 聚合、不同 tid 独立；两次请求及最终响应不变。
class FrontStub(object):
    def __init__(self):
        self.calls = []

    def request(self, method, host, path, headers, data, timeout):
        self.calls.append((method, host, path, data, timeout))
        if len(self.calls) % 2:
            return b"bad", 503, None
        body = b"ok"
        response = types.SimpleNamespace(headers={b"Content-Length": b"2"})
        return body, 200, response

front_stub = FrontStub()
front.get_front = lambda host, timeout: front_stub
front.g.server_host = ""
front.cloudflare_front = None
front_sleep = front.time.sleep
front.time.sleep = lambda seconds: None
first = front.request("POST", "server.example", "/data?tid=71", {}, b"payload", timeout=10)
second = front.request("POST", "server.example", "/data?tid=71", {}, b"payload", timeout=10)
third = front.request("POST", "server.example", "/data?tid=72", {}, b"payload", timeout=10)
front.time.sleep = front_sleep
check("X7 front retry same tid aggregates different tid independent",
      first[1] == 200 and second[1] == 200 and third[1] == 200
      and len(front_log.warnings) == 2 and len(front_stub.calls) == 6
      and "front retry server.example/data?tid=71" in front_log.warnings[0]
      and "front retry server.example/data?tid=72" in front_log.warnings[1],
      "warnings=%r calls=%d" % (front_log.warnings, len(front_stub.calls)))

# X8：网络请求期间 reset 后，旧响应必须按请求时 session 记账，不能误抑制新 session。
reset_session = make_session(b"old-session")
reset_session.conn_list = {}
reset_session.get_data_lock = threading.Lock()
reset_info = timeout_info(reset_session, 81, stat="request")
reset_info["start_time"] = time.time()
reset_session.transfer_list = {81: reset_info}
reset_session.get_upload_data = lambda work_id: reset_info
class ResetHttpClient(object):
    def request(self, **kwargs):
        reset_session.session_id = b"new-session"
        return b"", 503, None
g.http_client = ResetHttpClient()
before = len(key_log.warnings)
reset_session.session_id = b"new-session"
reset_session.transfer_warn(b"new-session", 81, "roundtrip_status",
                            "roundtrip new transfer_no:81 status:503")
reset_session.session_id = b"old-session"
proxy.time.sleep = lambda seconds: None
reset_session.roundtrip_task(0)
proxy.time.sleep = original_sleep
reset_warnings = key_log.warnings[before:]
check("X8 delayed old response does not share new session key",
      len(reset_warnings) == 3
      and sum("status:503" in line for line in reset_warnings) == 2
      and sum("roundtrip failed, no:81" in line for line in reset_warnings) == 1
      and reset_info["stat"] == "timeout" and reset_session.wait_queue.count == 1,
      "warnings=%r stat=%s notify=%d" %
      (reset_warnings, reset_info["stat"], reset_session.wait_queue.count))

print()
if failures:
    print("RESULT: %d FAILURES: %s" % (len(failures), failures))
    sys.exit(1)
print("RESULT: ALL PASS")
