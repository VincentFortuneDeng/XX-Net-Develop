#!/usr/bin/env python3
"""离线性能与资源基线；默认不连接代理、不访问生产服务。"""
import argparse
import json
import os
from pathlib import Path
import platform
import random
import resource
import sys
import threading
import time
import unittest
from collections import defaultdict


METRICS = ("dns_ms", "connect_ms", "front_ms", "roundtrip_ms", "log_wait_ms")


class MetricLedger:
    def __init__(self):
        self._lock = threading.Lock()
        self._latencies = defaultdict(list)
        self._counters = defaultdict(int)
        self._queue_bytes = 0
        self._queue_high_water = 0

    def latency(self, name, milliseconds):
        if name not in METRICS:
            raise ValueError("unknown metric: %s" % name)
        if milliseconds < 0:
            raise ValueError("latency must be non-negative")
        with self._lock:
            self._latencies[name].append(float(milliseconds))

    def outcome(self, success, retry=False):
        with self._lock:
            self._counters["attempts"] += 1
            self._counters["successes" if success else "failures"] += 1
            if retry:
                self._counters["retries"] += 1

    def queue(self, current_bytes):
        if current_bytes < 0:
            raise ValueError("queue bytes must be non-negative")
        with self._lock:
            self._queue_bytes = current_bytes
            self._queue_high_water = max(self._queue_high_water, current_bytes)

    def reset_queue(self):
        with self._lock:
            self._queue_bytes = 0

    def snapshot(self):
        with self._lock:
            latencies = {name: list(values) for name, values in self._latencies.items()}
            counters = dict(self._counters)
            result = {
                "counters": counters,
                "queue_bytes": self._queue_bytes,
                "queue_high_water": self._queue_high_water,
            }
        result["latency_ms"] = {name: percentiles(values) for name, values in latencies.items()}
        attempts = counters.get("attempts", 0)
        result["retransmission_rate"] = counters.get("retries", 0) / attempts if attempts else 0.0
        return result


def percentiles(values):
    if not values:
        return {"count": 0, "p50": None, "p95": None, "p99": None}
    ordered = sorted(values)

    def nearest_rank(ratio):
        index = max(0, min(len(ordered) - 1, int(ratio * len(ordered) + 0.999999) - 1))
        return ordered[index]

    return {
        "count": len(ordered),
        "p50": nearest_rank(0.50),
        "p95": nearest_rank(0.95),
        "p99": nearest_rank(0.99),
    }


def resource_high_water():
    try:
        fd_count = len(os.listdir("/dev/fd"))
    except OSError:
        fd_count = None
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        rss *= 1024
    return {
        "threads": threading.active_count(),
        "open_fds": fd_count,
        "max_rss_bytes": rss,
    }


def run_scenario(name, soak_seconds=1.0, seed=20261103):
    if name not in ("normal", "slow_consumer", "multi_domain", "front_recovery", "soak"):
        raise ValueError("unknown scenario: %s" % name)
    rng = random.Random(seed)
    ledger = MetricLedger()
    operations = 500 if name != "soak" else sys.maxsize
    deadline = time.monotonic() + soak_seconds
    index = 0
    while index < operations and (name != "soak" or time.monotonic() < deadline):
        started = time.perf_counter()
        payload = bytes(rng.randrange(64, 1024))
        ledger.latency("dns_ms", rng.uniform(0.1, 2.0))
        ledger.latency("connect_ms", rng.uniform(0.2, 5.0))
        ledger.latency("front_ms", rng.uniform(0.1, 3.0))
        ledger.latency("log_wait_ms", 0.0)
        failed = name == "front_recovery" and index < 50
        ledger.outcome(not failed, retry=failed)
        queue_bytes = len(payload) * (index % 7 if name == "slow_consumer" else 1)
        ledger.queue(queue_bytes)
        if index % 11 == 0:
            ledger.reset_queue()
        elapsed_ms = (time.perf_counter() - started) * 1000
        ledger.latency("roundtrip_ms", elapsed_ms)
        index += 1
    return {"scenario": name, "operations": index, "metrics": ledger.snapshot()}


def build_report(soak_seconds=1.0):
    return {
        "scope": "offline-synthetic; no production requests or fault injection",
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "pid": os.getpid(),
        },
        "scenarios": [run_scenario(name, soak_seconds) for name in
                      ("normal", "slow_consumer", "multi_domain", "front_recovery", "soak")],
        "resource_high_water": resource_high_water(),
        "uncovered": ["production deployment", "real network paths", "browser workloads", "long-duration production soak"],
    }


class PerformanceBaselineSelfTest(unittest.TestCase):
    def test_metric_conservation_against_reference_ledger(self):
        rng = random.Random(81)
        ledger = MetricLedger()
        reference = defaultdict(int)
        for index in range(1000):
            success = rng.choice((True, False))
            retry = rng.choice((True, False))
            ledger.outcome(success, retry)
            reference["attempts"] += 1
            reference["successes" if success else "failures"] += 1
            reference["retries"] += int(retry)
            current = rng.randrange(5000)
            ledger.queue(current)
            reference["high"] = max(reference["high"], current)
            if rng.randrange(10) == 0:
                ledger.reset_queue()
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot["counters"]["attempts"], 1000)
        self.assertEqual(snapshot["counters"]["attempts"],
                         snapshot["counters"]["successes"] + snapshot["counters"]["failures"])
        self.assertEqual(snapshot["counters"]["retries"], reference["retries"])
        self.assertEqual(snapshot["queue_high_water"], reference["high"])

    def test_percentiles_and_reset_do_not_lose_counts(self):
        ledger = MetricLedger()
        for value in range(1, 101):
            ledger.latency("dns_ms", value)
        ledger.outcome(True)
        ledger.outcome(False, retry=True)
        ledger.queue(900)
        ledger.reset_queue()
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot["latency_ms"]["dns_ms"]["p50"], 50)
        self.assertEqual(snapshot["latency_ms"]["dns_ms"]["p95"], 95)
        self.assertEqual(snapshot["latency_ms"]["dns_ms"]["p99"], 99)
        self.assertEqual(snapshot["queue_bytes"], 0)
        self.assertEqual(snapshot["queue_high_water"], 900)
        self.assertEqual(snapshot["counters"]["attempts"], 2)

    def test_all_scenarios_are_offline_and_report_resources(self):
        report = build_report(soak_seconds=0.001)
        self.assertEqual([item["scenario"] for item in report["scenarios"]],
                         ["normal", "slow_consumer", "multi_domain", "front_recovery", "soak"])
        self.assertIn("max_rss_bytes", report["resource_high_water"])
        self.assertIn("no production requests", report["scope"])
        json.dumps(report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--soak-seconds", type=float, default=1.0)
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.self_test:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(PerformanceBaselineSelfTest)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        sys.exit(0 if result.wasSuccessful() else 1)
    report = build_report(args.soak_seconds)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
