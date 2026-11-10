"""X-Tunnel 白名单运行指标。"""

import time

from runtime_metrics import CounterSet
from . import global_var as g


_COUNTER_NAMES = (
    "upload_bytes_total",
    "download_bytes_total",
    "command_gaps_total",
    "command_gap_recovered_total",
    "command_queue_overflow_total",
    "command_gap_timeout_total",
    "peer_close_total",
    "network_errors_total",
    "generation_stops_total",
)
_COUNTERS = CounterSet(_COUNTER_NAMES)
_GAUGE_NAMES = (
    "active_conns",
    "active_transfers",
    "inflight_requests",
    "send_buffer_bytes",
    "conn_send_buffer_bytes",
    "cmd_queue_bytes",
    "cmd_queue_window_ratio",
    "cmd_queue_gap_age_seconds",
)
_LEGACY_COUNTERS = {
    "sn_retries_total": "resend",
    "slow_roundtrips_total": "slow_roundtrip",
    "timeout_roundtrips_total": "timeout_roundtrip",
}


def increment(name, amount=1):
    _COUNTERS.increment(name, amount)


def record_conn_stop(reason):
    if reason == "peer close":
        increment("peer_close_total")
    elif reason == "command_queue_overflow":
        increment("command_queue_overflow_total")
    elif reason == "command_gap_timeout":
        increment("command_gap_timeout_total")
    elif reason in ("send fail.", "connect fail", "network error"):
        increment("network_errors_total")
    elif reason in ("session reset", "session stop", "session generation", "generation"):
        increment("generation_stops_total")


def _read_with_lock(lock, reader):
    if lock is None or not lock.acquire(False):
        return None
    try:
        return reader()
    finally:
        lock.release()


def _session_values(session):
    conns = [conn for conn in getattr(session, "conn_list", {}).values()
             if getattr(conn, "running", False)]
    transfers = [transfer for transfer in getattr(session, "transfer_list", {}).values()
                 if getattr(transfer, "running", True)]
    return conns, len(transfers), getattr(session, "on_road_num", 0)


def _conn_values(conn, now):
    send_buffer = getattr(conn, "send_buffer", None)
    queue_bytes = getattr(conn, "cmd_queue_bytes", 0)
    window = getattr(conn, "windows_size", 0)
    since = getattr(conn, "cmd_queue_since", None)
    return {
        "send_buffer": len(send_buffer) if send_buffer else 0,
        "queue_bytes": queue_bytes,
        "ratio": queue_bytes / window if window > 0 else None,
        "gap_age": max(0.0, now - since) if since is not None else None,
    }


def snapshot(session=None, now=None):
    if session is None:
        session = getattr(g, "session", None)
    names = _GAUGE_NAMES + _COUNTER_NAMES + tuple(_LEGACY_COUNTERS)
    result = {"x_tunnel." + name: None for name in names}
    if session is None or not getattr(session, "running", False):
        return result

    counters = _COUNTERS.snapshot()
    for name, value in counters.items():
        result["x_tunnel." + name] = value
    for name, legacy_name in _LEGACY_COUNTERS.items():
        result["x_tunnel." + name] = getattr(g, "stat", {}).get(legacy_name, 0)

    session_values = _read_with_lock(
        getattr(session, "lock", None), lambda: _session_values(session))
    if session_values is None:
        return result
    conns, active_transfers, inflight = session_values
    result["x_tunnel.active_conns"] = len(conns)
    result["x_tunnel.active_transfers"] = active_transfers
    result["x_tunnel.inflight_requests"] = inflight

    send_buffer = getattr(session, "send_buffer", None)
    if send_buffer is not None:
        result["x_tunnel.send_buffer_bytes"] = _read_with_lock(
            getattr(send_buffer, "mutex", None), lambda: getattr(send_buffer, "pool_size", 0))

    if now is None:
        now = time.monotonic()
    conn_send_buffer = 0
    cmd_queue_bytes = 0
    result["x_tunnel.conn_send_buffer_bytes"] = 0
    result["x_tunnel.cmd_queue_bytes"] = 0
    ratios = []
    gap_ages = []
    ratio_unavailable = False
    gap_age_unavailable = False
    for conn in conns:
        values = _read_with_lock(
            getattr(conn, "_lock", None), lambda conn=conn: _conn_values(conn, now))
        if values is None:
            result["x_tunnel.conn_send_buffer_bytes"] = None
            result["x_tunnel.cmd_queue_bytes"] = None
            result["x_tunnel.cmd_queue_window_ratio"] = None
            result["x_tunnel.cmd_queue_gap_age_seconds"] = None
            ratio_unavailable = True
            gap_age_unavailable = True
            continue
        if result["x_tunnel.conn_send_buffer_bytes"] is not None:
            conn_send_buffer += values["send_buffer"]
            result["x_tunnel.conn_send_buffer_bytes"] = conn_send_buffer
        if result["x_tunnel.cmd_queue_bytes"] is not None:
            cmd_queue_bytes += values["queue_bytes"]
            result["x_tunnel.cmd_queue_bytes"] = cmd_queue_bytes
        if values["ratio"] is not None:
            ratios.append(values["ratio"])
        if values["gap_age"] is not None:
            gap_ages.append(values["gap_age"])

    if conns and not ratio_unavailable:
        result["x_tunnel.cmd_queue_window_ratio"] = max(ratios) if ratios else None
    if not gap_age_unavailable:
        result["x_tunnel.cmd_queue_gap_age_seconds"] = max(gap_ages) if gap_ages else None
    return result
