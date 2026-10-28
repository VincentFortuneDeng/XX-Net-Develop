#!/usr/bin/env python3
"""F-phase T3 offline test: xlog warning_log roll + LogThrottle.

Loads the modified xlog.py by file path with stubbed deps (utils/six),
runs everything in a temp dir, never touches the running launcher.

Checks:
 A1) warning log under cap -> no roll
 A2) writing past warning_file_max_size -> rolls once to .1, main file
     stays small, writes continue into the new main file
 A3) second overflow -> .1 replaced (single generation, no .2)
 A4) .1 kept intact at open time when main file oversized (init-time roll)
 B)  LogThrottle: first hit allowed, window hits suppressed, suppressed
     count reported on next allowed hit, prune keeps dict bounded
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

print()
if failures:
    print("RESULT: %d FAILURES: %s" % (len(failures), failures))
    sys.exit(1)
print("RESULT: ALL PASS")
