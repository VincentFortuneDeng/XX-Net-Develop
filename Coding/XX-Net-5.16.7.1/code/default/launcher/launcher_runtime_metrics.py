"""launcher 的有界、脱敏运行指标聚合。"""
import functools
import os
import sys
import threading
import time
import uuid

import runtime_state
import xlog


_PROVIDER_DEADLINE_SECONDS = 0.25
_PROCESS_STARTED_AT = time.time()
_PROCESS_GENERATION = uuid.uuid4().hex
_PROVIDER_LOCK = threading.Lock()
_PROVIDER_RUNS = {}


class _ProviderRun:
    def __init__(self, provider):
        self.provider = provider
        self.done = threading.Event()
        self.result = None
        self.error = False


def _run_provider(name, run):
    try:
        run.result = run.provider()
    except BaseException:
        run.error = True
    finally:
        run.done.set()


def _provider_run(name, provider):
    with _PROVIDER_LOCK:
        run = _PROVIDER_RUNS.get(name)
        if run is not None and not run.done.is_set():
            return run
        run = _ProviderRun(provider)
        _PROVIDER_RUNS[name] = run
        thread = threading.Thread(
            target=_run_provider, args=(name, run),
            name="runtime-metrics:%s" % name, daemon=True)
        thread.start()
        return run


def _module_snapshot(name):
    import module_init

    state = module_init.proc_handler.get(name)
    if state is None:
        return None
    return state["imp"].local.metrics.snapshot()


def _default_module_providers():
    return {
        name: functools.partial(_module_snapshot, name)
        for name in ("gae_proxy", "smart_router", "x_tunnel")
    }


def _darwin_process_resources():
    import ctypes

    result = {
        "threads": None,
        "rss_bytes": None,
        "cpu_time_seconds": None,
        "fd_count": None,
    }

    class TaskInfo(ctypes.Structure):
        _fields_ = [
            ("virtual_size", ctypes.c_uint64),
            ("resident_size", ctypes.c_uint64),
            ("total_user", ctypes.c_uint64),
            ("total_system", ctypes.c_uint64),
            ("threads_user", ctypes.c_uint64),
            ("threads_system", ctypes.c_uint64),
            ("policy", ctypes.c_int32),
            ("faults", ctypes.c_int32),
            ("pageins", ctypes.c_int32),
            ("cow_faults", ctypes.c_int32),
            ("messages_sent", ctypes.c_int32),
            ("messages_received", ctypes.c_int32),
            ("syscalls_mach", ctypes.c_int32),
            ("syscalls_unix", ctypes.c_int32),
            ("csw", ctypes.c_int32),
            ("threadnum", ctypes.c_int32),
            ("numrunning", ctypes.c_int32),
            ("priority", ctypes.c_int32),
        ]

    libc = ctypes.CDLL(None)
    proc_pidinfo = libc.proc_pidinfo
    proc_pidinfo.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
    ]
    proc_pidinfo.restype = ctypes.c_int
    pid = os.getpid()

    task_info = TaskInfo()
    size = proc_pidinfo(pid, 4, 0, ctypes.byref(task_info), ctypes.sizeof(task_info))
    if size == ctypes.sizeof(task_info):
        result["threads"] = int(task_info.threadnum)
        result["rss_bytes"] = int(task_info.resident_size)
        result["cpu_time_seconds"] = (task_info.total_user + task_info.total_system) / 1e9

    needed = proc_pidinfo(pid, 1, 0, None, 0)
    if needed > 0:
        fd_buffer = ctypes.create_string_buffer(needed + 8 * 16)
        size = proc_pidinfo(pid, 1, 0, fd_buffer, ctypes.sizeof(fd_buffer))
        if size >= 0:
            result["fd_count"] = size // 8
    return result


def _process_snapshot():
    result = {
        "pid": os.getpid(),
        "started_at": _PROCESS_STARTED_AT,
        "threads": threading.active_count(),
        "rss_bytes": None,
        "cpu_time_seconds": None,
        "fd_count": None,
        "fd_soft_limit": None,
        "background_exceptions_total": 0,
        "unknown_stops_total": 0,
    }
    if sys.platform == "darwin":
        try:
            result.update(_darwin_process_resources())
        except Exception:
            pass
    else:
        try:
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF)
            result["cpu_time_seconds"] = usage.ru_utime + usage.ru_stime
        except Exception:
            pass
    try:
        import resource
        soft_limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
        result["fd_soft_limit"] = int(soft_limit) if soft_limit >= 0 else None
    except Exception:
        pass
    for key, value in xlog.runtime_snapshot().items():
        result[key[len("process."):]] = value
    return result


def collect_snapshot(module_providers=None):
    started_at = time.time()
    started_monotonic = time.monotonic()
    if module_providers is None:
        module_providers = _default_module_providers()
    deadline = started_monotonic + _PROVIDER_DEADLINE_SECONDS
    runs = [(name, _provider_run(name, provider))
            for name, provider in module_providers.items()]
    process = _process_snapshot()
    modules = {}
    for name, run in runs:
        remaining = max(0, deadline - time.monotonic())
        completed = run.done.wait(remaining)
        if not completed:
            modules[name] = {
                "state": "unknown", "collection": "unknown", "metrics": None,
            }
        elif run.error:
            modules[name] = {
                "state": "unknown", "collection": "error", "metrics": None,
            }
        elif run.result is None:
            modules[name] = {
                "state": "not_running", "collection": "not_running", "metrics": None,
            }
        else:
            modules[name] = {
                "state": "running", "collection": "ok", "metrics": run.result,
            }
    return {
        "schema_version": 1,
        "sampled_at": started_at,
        "process_generation": _PROCESS_GENERATION,
        "collection_duration_seconds": time.monotonic() - started_monotonic,
        "process": process,
        "launcher": runtime_state.snapshot(),
        "modules": modules,
    }
