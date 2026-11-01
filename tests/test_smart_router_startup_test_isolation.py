#!/usr/bin/env python3
"""启动竞态脚本在同一进程运行后，不得泄漏替换的模块。"""
import os
import runpy
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "tests", "test_smart_router_startup_race.py")

saved_modules = sys.modules.copy()
try:
    env_info_sentinel = types.ModuleType("env_info")
    xlog_sentinel = types.ModuleType("xlog")
    sys.modules["env_info"] = env_info_sentinel
    sys.modules["xlog"] = xlog_sentinel
    expected_modules = sys.modules.copy()

    runpy.run_path(SCRIPT, run_name="__main__")

    watched = {
        name for name in set(sys.modules).union(expected_modules)
        if name in ("env_info", "xlog", "xconfig", "module_init", "simple_http_server")
        or name == "smart_router" or name.startswith("smart_router.")
    }
    ok = all(sys.modules.get(name) is expected_modules.get(name) for name in watched)
    changed = sorted(name for name in watched
                     if sys.modules.get(name) is not expected_modules.get(name))
    print(("PASS" if ok else "FAIL"),
          "startup race script restores sys.modules",
          "added_or_removed=%r" % changed)
    if not ok:
        sys.exit(1)
finally:
    sys.modules.clear()
    sys.modules.update(saved_modules)
