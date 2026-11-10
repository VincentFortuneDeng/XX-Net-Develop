"""Smart Router 与 DNS 白名单运行指标。"""

import threading

from runtime_metrics import CounterSet, NumberSum
from . import global_var as g


_COUNTER_NAMES = (
    "dns.queries_total",
    "dns.complete_results_total",
    "dns.empty_results_total",
    "dns.timeouts_total",
    "dns.generation_drops_total",
    "dns.cache_commit_rejected_total",
    "smart_router.route_direct_total",
    "smart_router.route_proxy_total",
    "smart_router.route_blocked_total",
    "smart_router.route_unknown_total",
    "smart_router.handoff_failures_total",
    "smart_router.direct_connect_failures_total",
    "smart_router.pipe_register_failures_total",
    "smart_router.pipe_stopped_handoffs_total",
    "smart_router.pipe_closed_socket_handoffs_total",
    "smart_router.pipe_select_failures_total",
    "smart_router.pipe_loop_failures_total",
    "smart_router.pipe_controlled_closes_total",
)
_COUNTERS = CounterSet(_COUNTER_NAMES)
_LATENCY = NumberSum()
_QUERY_STATE = threading.local()
_GAUGE_NAMES = (
    "dns.cache_entries",
    "dns.cache_capacity",
    "dns.cache_epoch",
    "smart_router.pipe_read_sockets",
    "smart_router.pipe_write_sockets",
    "smart_router.pipe_write_buffer_bytes",
    "smart_router.pipe_running",
)


def increment(name, amount=1):
    _COUNTERS.increment(name, amount)


def begin_query():
    if getattr(_QUERY_STATE, "token", None) is not None:
        return None
    token = {"timed_out": False}
    _QUERY_STATE.token = token
    return token


def mark_timeout():
    token = getattr(_QUERY_STATE, "token", None)
    if token is not None:
        token["timed_out"] = True


def finish_query(token, ips, elapsed):
    if token is None:
        return
    _QUERY_STATE.token = None
    _LATENCY.add(max(0.0, elapsed))
    increment("dns.queries_total")
    if ips:
        increment("dns.complete_results_total")
    elif token["timed_out"]:
        increment("dns.timeouts_total")
    else:
        increment("dns.empty_results_total")


def _read_with_lock(lock, reader):
    if lock is None or not lock.acquire(False):
        return None
    try:
        return reader()
    finally:
        lock.release()


def snapshot(pipe_socks=None, domain_cache=None):
    if pipe_socks is None:
        pipe_socks = getattr(g, "pipe_socks", None)
    if domain_cache is None:
        domain_cache = getattr(g, "domain_cache", None)
    result = {name: None for name in _GAUGE_NAMES}
    running = getattr(g, "running", True)
    counters = _COUNTERS.snapshot()
    for name in _COUNTER_NAMES:
        result[name] = counters[name] if running else None
    result["dns.latency_seconds_total"] = _LATENCY.snapshot() if running else None
    if not running:
        return result

    if domain_cache:
        values = _read_with_lock(
            getattr(domain_cache, "lock", None),
            lambda: (len(domain_cache.cache), domain_cache.capacity, domain_cache.epoch))
        if values is not None:
            result["dns.cache_entries"] = values[0]
            result["dns.cache_capacity"] = values[1]
            result["dns.cache_epoch"] = values[2]

    if pipe_socks:
        result["smart_router.pipe_running"] = 1 if getattr(pipe_socks, "running", False) else 0

    if pipe_socks and getattr(pipe_socks, "running", False):
        values = _read_with_lock(
            getattr(pipe_socks, "sock_lock", None),
            lambda: (
                len(pipe_socks.read_set),
                len(pipe_socks.write_set),
                sum(getattr(sock, "buf_size", 0) for sock in pipe_socks.write_set),
            ))
        if values is not None:
            result["smart_router.pipe_read_sockets"] = values[0]
            result["smart_router.pipe_write_sockets"] = values[1]
            result["smart_router.pipe_write_buffer_bytes"] = values[2]
    return result
