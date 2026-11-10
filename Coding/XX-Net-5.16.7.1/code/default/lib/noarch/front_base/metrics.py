"""Front 固定类别指标注册表；只保存数值和固定类别。"""

import threading
import weakref

from runtime_metrics import CounterSet, NumberSum


CATEGORIES = ("gae", "direct", "cloudflare", "cloudfront")
_COUNTER_NAMES = (
    "requests_succeeded_total",
    "requests_failed_total",
    "rtt_observations_total",
    "request_retries_total",
    "response_failures_total",
    "body_timeouts_total",
    "protocol_errors_total",
    "generation_drops_total",
)
_GAUGE_NAMES = (
    "workers",
    "h1_connections",
    "h2_connections",
    "pending_tasks",
    "connection_pools",
    "connections_creating",
)
_ALL_NAMES = (
    "workers", "h1_connections", "h2_connections", "pending_tasks",
    "requests_succeeded_total", "requests_failed_total", "rtt_seconds_total",
    "rtt_observations_total", "connection_pools", "connections_creating",
    "no_available_ip_total", "request_retries_total", "response_failures_total",
    "body_timeouts_total", "protocol_errors_total", "generation_drops_total",
)
_COUNTERS = {category: CounterSet(_COUNTER_NAMES) for category in CATEGORIES}
_RTTS = {category: NumberSum() for category in CATEGORIES}
_REGISTRY = {category: weakref.WeakSet() for category in CATEGORIES}
_REGISTRY_LOCK = threading.Lock()


def _validate_category(category):
    if category not in CATEGORIES:
        raise ValueError("unknown front metrics category")


def register(dispatcher):
    category = getattr(dispatcher, "metrics_category", None)
    _validate_category(category)
    with _REGISTRY_LOCK:
        _REGISTRY[category].add(dispatcher)


def unregister(dispatcher):
    category = getattr(dispatcher, "metrics_category", None)
    if category not in CATEGORIES:
        return
    with _REGISTRY_LOCK:
        _REGISTRY[category].discard(dispatcher)


def increment(category, name, amount=1):
    if category is None:
        return
    _validate_category(category)
    _COUNTERS[category].increment(name, amount)


def add_rtt(category, seconds):
    if category is None:
        return
    _validate_category(category)
    _RTTS[category].add(seconds)
    _COUNTERS[category].increment("rtt_observations_total")


def _read_with_lock(lock, reader):
    if lock is None or not lock.acquire(False):
        return None
    try:
        return reader()
    finally:
        lock.release()


def _dispatcher_values(dispatcher):
    return (
        len(getattr(dispatcher, "workers", ())),
        getattr(dispatcher, "h1_num", 0),
        getattr(dispatcher, "h2_num", 0),
    )


def _queue_values(request_queue):
    lock = getattr(request_queue, "mutex", None)
    if lock is None:
        return None
    return _read_with_lock(lock, lambda: len(request_queue.queue))


def _pool_values(pool):
    if isinstance(pool, dict):
        return len(pool)
    return _read_with_lock(
        getattr(pool, "pool_lock", None), lambda: len(getattr(pool, "pool", {})))


def _add_gauge(result, name, value):
    if value is None:
        result[name] = None
    elif result[name] is not None:
        result[name] += value


def snapshot():
    with _REGISTRY_LOCK:
        registered = {category: list(_REGISTRY[category]) for category in CATEGORIES}

    output = {}
    for category in CATEGORIES:
        dispatchers = [item for item in registered[category]
                       if getattr(item, "running", False)]
        result = {name: None for name in _ALL_NAMES}
        output[category] = result
        if not dispatchers:
            continue

        for name in _GAUGE_NAMES:
            result[name] = 0
        result["no_available_ip_total"] = 0
        for name, value in _COUNTERS[category].snapshot().items():
            result[name] = value
        result["rtt_seconds_total"] = _RTTS[category].snapshot()

        managers = []
        manager_ids = set()
        for dispatcher in dispatchers:
            values = _read_with_lock(
                getattr(dispatcher, "_state_lock", None),
                lambda dispatcher=dispatcher: _dispatcher_values(dispatcher))
            if values is None:
                for name in ("workers", "h1_connections", "h2_connections"):
                    result[name] = None
            else:
                for name, value in zip(("workers", "h1_connections", "h2_connections"), values):
                    _add_gauge(result, name, value)

            pending = _queue_values(getattr(dispatcher, "request_queue", None))
            _add_gauge(result, "pending_tasks", pending)
            manager = getattr(dispatcher, "connection_manager", None)
            if manager is not None and id(manager) not in manager_ids:
                manager_ids.add(id(manager))
                managers.append(manager)

        for manager in managers:
            pools = _pool_values(getattr(manager, "new_conn_pool", None))
            _add_gauge(result, "connection_pools", pools)
            creating = _read_with_lock(
                getattr(manager, "thread_num_lock", None),
                lambda manager=manager: getattr(manager, "thread_num", 0))
            _add_gauge(result, "connections_creating", creating)

            no_ip_lock = getattr(manager, "no_ip_lock", None)
            if no_ip_lock is None:
                no_ip = getattr(manager, "total_no_ip_events", 0)
            else:
                no_ip = _read_with_lock(
                    no_ip_lock, lambda manager=manager: getattr(manager, "total_no_ip_events", 0))
            if no_ip is None:
                result["no_available_ip_total"] = None
            elif result["no_available_ip_total"] is not None:
                result["no_available_ip_total"] += no_ip

    return output
