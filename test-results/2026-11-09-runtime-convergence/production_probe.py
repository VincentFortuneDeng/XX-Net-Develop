"""XX-Net 生产探针与真实启动边界；不保存敏感 URL、Host、正文或异常文本。"""
import argparse
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import struct
import subprocess
import time
from urllib.parse import urlsplit


_DEFAULT_ARGV = ["/Applications/XX-Net/start"]
_PRODUCTION_ROOT = Path("/Applications/XX-Net")
_RANGE_SIZE = 1024 * 1024
_FAILURE_NAMES = ("timeout", "truncated", "connection", "http_status", "content_mismatch", "other")
_PIPE_METRIC_NAMES = (
    "smart_router.pipe_running",
    "smart_router.direct_connect_failures_total",
    "smart_router.handoff_failures_total",
    "smart_router.pipe_register_failures_total",
    "smart_router.pipe_stopped_handoffs_total",
    "smart_router.pipe_closed_socket_handoffs_total",
    "smart_router.pipe_select_failures_total",
    "smart_router.pipe_loop_failures_total",
)
_PROBE_HOST = "www.gstatic.com"
_PROBE_PATH = "/generate_204"


class _HttpStatusError(Exception):
    pass


def _empty_probe(category):
    return {
        "category": category, "status": "idle", "attempts": 0,
        "complete_successes": 0,
        "failures": {name: 0 for name in _FAILURE_NAMES},
        "expected_bytes": 0, "actual_bytes": 0, "latency_seconds": 0.0,
    }


def _exception_failure(error):
    if isinstance(error, _HttpStatusError):
        return "http_status"
    if isinstance(error, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(error, (http.client.IncompleteRead, http.client.ResponseNotReady)):
        return "truncated"
    if isinstance(error, (ConnectionError, OSError, http.client.HTTPException, ssl.SSLError)):
        return "connection"
    return "other"


def _summarize_attempts(category, results):
    summary = _empty_probe(category)
    summary["attempts"] = len(results)
    summary["latency_seconds"] = sum(item["latency_seconds"] for item in results)
    if results:
        summary["expected_bytes"] = max(item["expected_bytes"] for item in results)
        summary["actual_bytes"] = min(item["actual_bytes"] for item in results)
    hashes = []
    for item in results:
        if item.get("ok"):
            summary["complete_successes"] += 1
            if item.get("content_sha256"):
                hashes.append(item["content_sha256"])
        else:
            failure = item.get("failure", "other")
            summary["failures"][failure if failure in _FAILURE_NAMES else "other"] += 1
    summary["status"] = "complete" if results and summary["complete_successes"] == len(results) else (
        "failed" if results else "idle"
    )
    if hashes and len(hashes) == summary["complete_successes"] and len(set(hashes)) == 1:
        summary["content_sha256"] = hashes[0]
    return summary


def one_range_request(parts, route):
    started = time.monotonic()
    connection = None
    received = 0
    digest = hashlib.sha256()
    try:
        if route == "proxy":
            connection = http.client.HTTPSConnection("127.0.0.1", 8086, timeout=45)
            connection.set_tunnel(parts.hostname, 443)
        else:
            connection = http.client.HTTPSConnection(parts.hostname, 443, timeout=45)
        target = parts.path + (("?" + parts.query) if parts.query else "")
        connection.request("GET", target, headers={
            "Range": "bytes=0-%d" % (_RANGE_SIZE - 1),
            "User-Agent": "XX-Net-runtime-convergence/1.0",
            "Accept": "*/*", "Connection": "close",
        })
        response = connection.getresponse()
        content_range = response.getheader("Content-Range") or ""
        range_match = re.fullmatch(r"bytes 0-%d/([0-9]+)" % (_RANGE_SIZE - 1), content_range)
        if response.status != 206:
            failure = "http_status"
        elif not range_match or int(range_match.group(1)) < _RANGE_SIZE:
            failure = "truncated"
        else:
            failure = None
        while failure is None and received < _RANGE_SIZE:
            block = response.read(min(65536, _RANGE_SIZE - received))
            if not block:
                break
            digest.update(block)
            received += len(block)
        if failure is None and received != _RANGE_SIZE:
            failure = "truncated"
        return {
            "ok": failure is None, "failure": failure,
            "expected_bytes": _RANGE_SIZE, "actual_bytes": received,
            "latency_seconds": time.monotonic() - started,
            "content_sha256": digest.hexdigest(),
        }
    except Exception as error:
        if isinstance(error, http.client.IncompleteRead):
            received += len(error.partial)
        return {
            "ok": False, "failure": _exception_failure(error),
            "expected_bytes": _RANGE_SIZE, "actual_bytes": received,
            "latency_seconds": time.monotonic() - started,
        }
    finally:
        if connection is not None:
            connection.close()


def run_range_probes(url, attempts=16, request=None):
    request = request or one_range_request
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("a non-sensitive HTTPS download URL is required")
    results = {"direct": [], "proxy": []}
    baseline_hash = None
    for _ in range(attempts):
        for route in ("direct", "proxy"):
            result = request(parts, route)
            if result.get("ok"):
                content_hash = result.get("content_sha256")
                if baseline_hash is None:
                    baseline_hash = content_hash
                elif content_hash != baseline_hash:
                    result = dict(result, ok=False, failure="content_mismatch")
            results[route].append(result)
    return [_summarize_attempts(route, results[route]) for route in ("direct", "proxy")]


def _read_response(connection, expected_status):
    response = connection.getresponse()
    body = response.read()
    status = response.status
    connection.close()
    if status != expected_status:
        raise _HttpStatusError()
    return body


def _control():
    connection = http.client.HTTPConnection("127.0.0.1", 8085, timeout=10)
    connection.request("GET", "/", headers={"Connection": "close"})
    return _read_response(connection, 200)


def _http_proxy():
    connection = http.client.HTTPConnection("127.0.0.1", 8086, timeout=20)
    connection.request("GET", "http://" + _PROBE_HOST + _PROBE_PATH,
                       headers={"Connection": "close"})
    return _read_response(connection, 204)


def _https_proxy():
    connection = http.client.HTTPSConnection("127.0.0.1", 8086, timeout=20)
    connection.set_tunnel(_PROBE_HOST, 443)
    connection.request("GET", _PROBE_PATH, headers={"Connection": "close"})
    return _read_response(connection, 204)


def _receive_exact(sock, length):
    data = b""
    while len(data) < length:
        block = sock.recv(length - len(data))
        if not block:
            raise http.client.IncompleteRead(data, length)
        data += block
    return data


def _socks5_domain():
    sock = socket.create_connection(("127.0.0.1", 1080), timeout=20)
    sock.settimeout(20)
    try:
        sock.sendall(b"\x05\x01\x00")
        if _receive_exact(sock, 2) != b"\x05\x00":
            raise ConnectionError("SOCKS5 negotiation failed")
        host = _PROBE_HOST.encode("ascii")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + struct.pack("!H", 443))
        reply = _receive_exact(sock, 4)
        if reply[:2] != b"\x05\x00":
            raise ConnectionError("SOCKS5 connect failed")
        if reply[3] == 1:
            remaining = 4
        elif reply[3] == 3:
            remaining = _receive_exact(sock, 1)[0]
        elif reply[3] == 4:
            remaining = 16
        else:
            raise ConnectionError("SOCKS5 address type unsupported")
        _receive_exact(sock, remaining + 2)
        tls = ssl.create_default_context().wrap_socket(sock, server_hostname=_PROBE_HOST)
        try:
            tls.sendall(("GET " + _PROBE_PATH + " HTTP/1.1\r\nHost: " + _PROBE_HOST +
                         "\r\nConnection: close\r\n\r\n").encode("ascii"))
            data = b""
            while b"\r\n\r\n" not in data:
                block = tls.recv(4096)
                if not block:
                    raise http.client.IncompleteRead(data)
                data += block
            if int(data.split(b" ", 2)[1]) != 204:
                raise _HttpStatusError()
            return data
        finally:
            tls.close()
    finally:
        sock.close()


def run_entry_probes():
    checks = (("control", _control), ("http", _http_proxy),
              ("https", _https_proxy), ("socks5", _socks5_domain))
    probes = []
    for category, check in checks:
        started = time.monotonic()
        try:
            body = check()
            result = {
                "ok": True, "expected_bytes": len(body), "actual_bytes": len(body),
                "latency_seconds": time.monotonic() - started,
            }
        except Exception as error:
            result = {
                "ok": False, "failure": _exception_failure(error),
                "expected_bytes": 0, "actual_bytes": 0,
                "latency_seconds": time.monotonic() - started,
            }
        probes.append(_summarize_attempts(category, [result]))
    return probes


def find_signed_url():
    url = os.environ.get("XXNET_RANGE_PROBE_URL")
    if not url:
        raise RuntimeError("RANGE_URL_REQUIRED")
    return url


def probe_document(process_generation=None):
    # 兼容旧调用；权威代际只能来自探针前后的真实运行快照。
    before = fetch_runtime_snapshot()
    generation = before.get("process_generation") if isinstance(before, dict) else None
    if not generation or not _valid_snapshot(before, generation):
        raise RuntimeError("INVALID_RUNTIME_SNAPSHOT")
    probes = run_entry_probes()
    probes.extend(run_range_probes(find_signed_url(), 16))
    after = fetch_runtime_snapshot()
    if not isinstance(after, dict) or after.get("process_generation") != generation:
        raise RuntimeError("PROCESS_GENERATION_MISMATCH")
    if not _valid_snapshot(after, generation):
        raise RuntimeError("INVALID_RUNTIME_SNAPSHOT")
    return {"schema_version": 1, "process_generation": generation, "probes": probes}


def fetch_runtime_snapshot():
    connection = http.client.HTTPConnection("127.0.0.1", 8085, timeout=10)
    try:
        connection.request("GET", "/runtime_metrics", headers={
            "Accept": "application/json", "Connection": "close",
        })
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise RuntimeError("runtime metrics HTTP error")
        return json.loads(body)
    finally:
        connection.close()


def _valid_snapshot(snapshot, process_generation):
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        return False
    if snapshot.get("process_generation") != process_generation:
        return False
    if not isinstance(snapshot.get("process"), dict) or not isinstance(snapshot.get("modules"), dict):
        return False
    sampled_at = snapshot.get("sampled_at")
    if not isinstance(sampled_at, (int, float)) or isinstance(sampled_at, bool) or not math.isfinite(sampled_at):
        return False
    modules = snapshot["modules"]
    if set(modules) != {"gae_proxy", "smart_router", "x_tunnel"}:
        return False
    if any(not isinstance(item, dict) for item in modules.values()):
        return False
    for name in ("smart_router", "x_tunnel"):
        if modules[name].get("state") != "running" or modules[name].get("collection") != "ok":
            return False
    pipe_metrics = modules["smart_router"].get("metrics")
    if not isinstance(pipe_metrics, dict) or any(
            not isinstance(pipe_metrics.get(name), int) or isinstance(pipe_metrics.get(name), bool) or
            pipe_metrics[name] < 0 for name in _PIPE_METRIC_NAMES):
        return False
    gae = modules["gae_proxy"]
    return (gae.get("state"), gae.get("collection")) in {
        ("running", "ok"), ("not_running", "not_running"),
    }


def observe_runtime(process_generation, duration_seconds=65, interval_seconds=5,
                    fetch_snapshot=None, sleep=None, monotonic=None, wall=None):
    fetch_snapshot = fetch_snapshot or fetch_runtime_snapshot
    sleep = sleep or time.sleep
    monotonic = monotonic or time.monotonic
    wall = wall or time.time
    started_monotonic = monotonic()
    started_at = wall()
    valid_samples = []
    snapshots = []
    gap_count = 0
    while True:
        try:
            snapshot = fetch_snapshot()
        except Exception:
            snapshot = None
        if _valid_snapshot(snapshot, process_generation):
            snapshots.append(snapshot)
            valid_samples.append({
                "sampled_at": snapshot["sampled_at"],
                "process_generation": process_generation,
                "collection": "ok",
                "modules": {
                    name: {
                        "state": item["state"], "collection": item["collection"],
                        **({"metrics": {field: item["metrics"][field] for field in _PIPE_METRIC_NAMES}}
                           if name == "smart_router" else {}),
                    }
                    for name, item in snapshot["modules"].items()
                },
            })
        else:
            gap_count += 1
        if monotonic() - started_monotonic >= duration_seconds:
            break
        sleep(interval_seconds)
    return {
        "started_at": started_at, "ended_at": wall(),
        "valid_samples": valid_samples, "gap_count": gap_count,
    }, snapshots


def evaluate_preflight(installed_marker, embedded_exists, embedded_usable,
                       archive_exists, desktop_modules_available):
    using_embedded = embedded_exists and embedded_usable
    would_unpack = not embedded_exists and archive_exists
    return {
        "would_auto_install": not using_embedded and not desktop_modules_available,
        "would_unpack": would_unpack,
        "would_escalate": False,
        "would_modify_system": not installed_marker or would_unpack,
    }


def _command_ok(command):
    try:
        return subprocess.run(
            command, cwd="/", stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _system_python():
    python = shutil.which("python")
    if python:
        proc = subprocess.run([python, "-V"], cwd="/", text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if proc.returncode == 0 and proc.stdout.startswith("Python 3"):
            return python
    python3 = shutil.which("python3")
    if not python3:
        raise RuntimeError("system Python 3 not found")
    return python3


def preflight_start(root=_PRODUCTION_ROOT):
    root = Path(root)
    system_python = _system_python()
    embedded = root / "python3/bin/python3"
    embedded_exists = embedded.is_file()
    embedded_usable = embedded_exists and _command_ok([str(embedded), "-c", "pass"])
    executable = str(embedded) if embedded_usable else system_python
    desktop_modules_available = _command_ok([
        executable, "-c", "import PyObjCTools, AppKit, SystemConfiguration",
    ])
    pillow_available = _command_ok([executable, "-c", "import PIL"])
    result = evaluate_preflight(
        installed_marker=(root / "data/launcher/installed").is_file(),
        embedded_exists=embedded_exists, embedded_usable=embedded_usable,
        archive_exists=(root / "python3.zip").is_file(),
        desktop_modules_available=desktop_modules_available,
    )
    result.update({
        "python_executable": executable, "pillow_available": pillow_available,
        "start_invoked": False,
    })
    return result


def run_start_after_preflight(preflight, runner):
    if not isinstance(preflight, dict):
        return {"status": "BLOCKED", "reason": "PREFLIGHT_INCOMPLETE", "start_invoked": False}
    blockers = (
        ("would_auto_install", "AUTO_INSTALL_REQUIRED"),
        ("would_unpack", "UNPACK_REQUIRED"),
        ("would_escalate", "ESCALATION_REQUIRED"),
        ("would_modify_system", "SYSTEM_MODIFICATION_REQUIRED"),
    )
    for field, reason in blockers:
        if preflight.get(field) is not False:
            return {"status": "BLOCKED", "reason": reason, "start_invoked": False}
    process = runner(_DEFAULT_ARGV, cwd="/Applications/XX-Net")
    return {"status": "STARTED", "start_invoked": True, "process": process}


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                          encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--out", required=True)
    probes = subparsers.add_parser("probes")
    probes.add_argument("--out", required=True)
    observe = subparsers.add_parser("observe")
    observe.add_argument("--generation", required=True)
    observe.add_argument("--out", required=True)
    observe.add_argument("--raw-out", required=True)
    observe.add_argument("--duration", type=float, default=65)
    observe.add_argument("--interval", type=float, default=5)
    args = parser.parse_args(argv)
    if args.command == "preflight":
        result = preflight_start()
        _write_json(args.out, result)
        blocked = any(result[field] for field in (
            "would_auto_install", "would_unpack", "would_escalate", "would_modify_system"))
        print(json.dumps({"status": "BLOCKED" if blocked else "SAFE", "start_invoked": False}))
        return 1 if blocked else 0
    if args.command == "probes":
        document = probe_document()
        _write_json(args.out, document)
        complete = all(item["status"] == "complete" for item in document["probes"])
        print(json.dumps({"status": "complete" if complete else "failed", "probe_count": len(document["probes"])}))
        return 0 if complete else 1
    observation, snapshots = observe_runtime(
        args.generation, args.duration, args.interval,
    )
    _write_json(args.out, observation)
    _write_json(args.raw_out, {"schema_version": 1, "snapshots": snapshots})
    complete = (observation["ended_at"] - observation["started_at"] >= 60
                and len(observation["valid_samples"]) >= 6)
    print(json.dumps({
        "status": "complete" if complete else "failed",
        "valid_samples": len(observation["valid_samples"]), "gap_count": observation["gap_count"],
    }))
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
