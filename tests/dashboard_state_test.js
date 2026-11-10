"use strict";

const assert = require("node:assert/strict");
const dashboard = require("../Coding/XX-Net-5.16.7.1/code/default/launcher/web_ui/js/dashboard.js");

const tests = [];
function test(name, fn) {
    tests.push({name, fn});
}

function snapshot(overrides = {}) {
    const value = {
        schema_version: 1,
        sampled_at: 1000,
        process_generation: "generation-a",
        collection_duration_seconds: 0.01,
        process: {pid: 10, started_at: 900, threads: 4},
        launcher: {startup_mode: "default"},
        modules: {
            x_tunnel: {state: "running", collection: "ok", metrics: {"x_tunnel.command_gaps_total": 2}},
            gae_proxy: {state: "running", collection: "ok", metrics: {requests_failed_total: 1}},
            smart_router: {state: "running", collection: "ok", metrics: {}},
        },
    };
    return Object.assign(value, overrides);
}

function probe(overrides = {}) {
    const value = {
        category: "direct",
        status: "complete",
        attempts: 2,
        complete_successes: 2,
        failures: {
            timeout: 0,
            truncated: 0,
            connection: 0,
            http_status: 0,
            content_mismatch: 0,
            other: 0,
        },
        expected_bytes: 2048,
        actual_bytes: 2048,
        latency_seconds: 0.25,
    };
    return Object.assign(value, overrides);
}

function probeDocument(probes = [probe()]) {
    return {schema_version: 1, process_generation: "generation-a", probes};
}

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => {
        resolve = res;
        reject = rej;
    });
    return {promise, resolve, reject};
}

test("normalizeSnapshot 保留真实字段并把缺失资源值设为 null", () => {
    const result = dashboard.normalizeSnapshot(snapshot());
    assert.equal(result.schemaVersion, 1);
    assert.equal(result.generation, "generation-a");
    assert.equal(result.process.threads, 4);
    assert.equal(result.process.rss_bytes, null);
    assert.equal(result.modules.x_tunnel.metrics["x_tunnel.command_gaps_total"], 2);
    assert.equal(result.stale, false);
    assert.match(result.segment, /^generation-a\|/);
});

test("normalizeSnapshot 对错误版本抛固定错误", () => {
    assert.throws(() => dashboard.normalizeSnapshot(snapshot({schema_version: 2})), {
        message: "INVALID_RUNTIME_SNAPSHOT",
    });
    assert.throws(() => dashboard.normalizeSnapshot(null), {message: "INVALID_RUNTIME_SNAPSHOT"});
});

test("appendSample 最多保留 360 个有效样本并计算真实观察窗口", () => {
    let samples = [];
    for (let index = 0; index < 370; index += 1) {
        samples = dashboard.appendSample(samples, snapshot({sampled_at: 1000 + index}), 360);
    }
    assert.equal(samples.length, 360);
    assert.equal(samples[0].sampledAt, 1010);
    assert.deepEqual(dashboard.observationWindow(samples), {
        count: 360, start: 1010, end: 1369, seconds: 359,
    });
});

test("观察窗口只统计末尾当前连续代际段落", () => {
    let samples = dashboard.appendSample([], snapshot({sampled_at: 1000}));
    samples = dashboard.appendSample(samples, snapshot({
        sampled_at: 1005, process_generation: "generation-b",
    }));
    samples = dashboard.appendSample(samples, snapshot({
        sampled_at: 1010, process_generation: "generation-b",
    }));
    assert.deepEqual(dashboard.observationWindow(samples), {
        count: 2, start: 1005, end: 1010, seconds: 5,
    });

    samples = dashboard.appendSample(samples, snapshot({
        sampled_at: 1015, process_generation: "generation-a",
    }));
    assert.deepEqual(dashboard.observationWindow(samples), {
        count: 1, start: 1015, end: 1015, seconds: 0,
    });
});

test("进程换代和模块状态转换都切分段落", () => {
    const first = snapshot({sampled_at: 1000});
    const second = snapshot({sampled_at: 1005});
    let samples = dashboard.appendSample([], first);
    samples = dashboard.appendSample(samples, second);
    assert.equal(samples[0].segment, samples[1].segment);
    assert.equal(dashboard.counterDelta(samples, "x_tunnel", "x_tunnel.command_gaps_total"), 0);

    const third = snapshot({sampled_at: 1010});
    third.modules.x_tunnel.metrics["x_tunnel.command_gaps_total"] = 8;
    samples = dashboard.appendSample(samples, third);
    assert.equal(dashboard.counterDelta(samples, "x_tunnel", "x_tunnel.command_gaps_total"), 6);

    const fourth = snapshot({sampled_at: 1015, process_generation: "generation-b"});
    fourth.modules.x_tunnel.metrics["x_tunnel.command_gaps_total"] = 20;
    samples = dashboard.appendSample(samples, fourth);
    assert.notEqual(samples.at(-1).segment, samples.at(-2).segment);
    assert.equal(dashboard.counterDelta(samples, "x_tunnel", "x_tunnel.command_gaps_total"), null);

    const fifth = snapshot({sampled_at: 1020});
    fifth.modules.x_tunnel.state = "not_running";
    fifth.modules.x_tunnel.collection = "not_running";
    fifth.modules.x_tunnel.metrics = null;
    samples = dashboard.appendSample(samples, fifth);
    assert.notEqual(samples.at(-1).segment, samples.at(-2).segment);
    assert.equal(dashboard.counterDelta(samples, "x_tunnel", "x_tunnel.command_gaps_total"), null);
});

test("counterDelta 对缺失、未知和负差分返回 null", () => {
    const first = snapshot();
    const second = snapshot({sampled_at: 1005});
    second.modules.x_tunnel.metrics["x_tunnel.command_gaps_total"] = 1;
    let samples = dashboard.appendSample([], first);
    samples = dashboard.appendSample(samples, second);
    assert.equal(dashboard.counterDelta(samples, "x_tunnel", "x_tunnel.command_gaps_total"), null);
    assert.equal(dashboard.counterDelta(samples, "x_tunnel", "x_tunnel.missing"), null);
    assert.equal(dashboard.counterDelta([], "x_tunnel", "command_gaps_total"), null);
});

test("PipeSocks 显式状态区分运行、停止、未知和模块未运行", () => {
    const running = dashboard.normalizeSnapshot(snapshot());
    running.modules.smart_router.metrics["smart_router.pipe_running"] = 1;
    assert.equal(dashboard.smartRouterPipeStatus(running), "running");

    const stopped = dashboard.normalizeSnapshot(snapshot());
    stopped.modules.smart_router.metrics["smart_router.pipe_running"] = 0;
    assert.equal(dashboard.smartRouterPipeStatus(stopped), "stopped");

    const unknown = dashboard.normalizeSnapshot(snapshot());
    unknown.modules.smart_router.metrics["smart_router.pipe_running"] = null;
    assert.equal(dashboard.smartRouterPipeStatus(unknown), "unknown");

    const notRunning = dashboard.normalizeSnapshot(snapshot());
    notRunning.modules.smart_router.state = "not_running";
    notRunning.modules.smart_router.metrics = null;
    assert.equal(dashboard.smartRouterPipeStatus(notRunning), "not_running");
});

test("PipeSocks 运行到停止会切分段落且不错算交接差分", () => {
    const first = snapshot({sampled_at: 1000});
    first.modules.smart_router.metrics = {
        "smart_router.pipe_running": 1,
        "smart_router.handoff_failures_total": 2,
    };
    const second = snapshot({sampled_at: 1005});
    second.modules.smart_router.metrics = {
        "smart_router.pipe_running": 0,
        "smart_router.handoff_failures_total": 3,
    };
    let samples = dashboard.appendSample([], first);
    samples = dashboard.appendSample(samples, second);
    assert.notEqual(samples[0].segment, samples[1].segment);
    assert.equal(dashboard.counterDelta(
        samples, "smart_router", "handoff_failures_total"), null);
});

test("真实完全限定键驱动队列读数和跨模块差分", () => {
    const first = snapshot();
    const second = snapshot({sampled_at: 1005});
    first.modules.x_tunnel.metrics = {
        "x_tunnel.command_gap_recovered_total": 10,
        "x_tunnel.cmd_queue_window_ratio": 0.25,
    };
    second.modules.x_tunnel.metrics = {
        "x_tunnel.command_gap_recovered_total": 14,
        "x_tunnel.cmd_queue_window_ratio": 0.5,
    };
    first.modules.smart_router.metrics = {
        "smart_router.handoff_failures_total": 2,
        "dns.timeouts_total": 3,
    };
    second.modules.smart_router.metrics = {
        "smart_router.handoff_failures_total": 4,
        "dns.timeouts_total": 5,
    };
    let samples = dashboard.appendSample([], first);
    samples = dashboard.appendSample(samples, second);
    assert.equal(dashboard.moduleMetric(
        samples[1], "x_tunnel", "cmd_queue_window_ratio"), 0.5);
    assert.equal(dashboard.counterDelta(
        samples, "x_tunnel", "command_gap_recovered_total"), 4);
    assert.equal(dashboard.counterDelta(
        samples, "smart_router", "handoff_failures_total"), 2);
    assert.equal(dashboard.counterDelta(samples, "smart_router", "dns.timeouts_total"), 2);
});

test("无探针或零尝试显示 NO_SAMPLE 而不是 100%", () => {
    for (const document of [probeDocument([]), probeDocument([probe({
        status: "idle", attempts: 0, complete_successes: 0,
        expected_bytes: 0, actual_bytes: 0, latency_seconds: 0,
    })])]) {
        const summary = dashboard.summarizeProbes(document, "generation-a");
        assert.equal(summary.attempts, 0);
        assert.equal(summary.successRate, null);
        assert.equal(summary.display, "NO_SAMPLE");
        assert.equal(dashboard.formatRate(summary.successRate), "NO_SAMPLE");
    }
});

test("探针汇总完整成功率、失败分类和字节边界", () => {
    const partial = probe({
        status: "failed", attempts: 3, complete_successes: 1,
        failures: {timeout: 1, truncated: 1, connection: 0, http_status: 0, content_mismatch: 0, other: 0},
        expected_bytes: 3072, actual_bytes: 1024, latency_seconds: 1.5,
        content_sha256: "a".repeat(64),
    });
    const summary = dashboard.summarizeProbes(probeDocument([partial]), "generation-a");
    assert.equal(summary.attempts, 3);
    assert.equal(summary.completeSuccesses, 1);
    assert.equal(summary.successRate, 1 / 3);
    assert.equal(summary.failures.timeout, 1);
    assert.equal(summary.failures.truncated, 1);
    assert.equal(summary.expectedBytes, 3072);
    assert.equal(summary.actualBytes, 1024);
    assert.equal(dashboard.formatRate(summary.successRate), "33.3%");
});

test("探针 schema 拒绝额外字段、缺失字段和非法分类", () => {
    const extra = probeDocument();
    extra.probes[0].host = "must-not-be-accepted";
    assert.throws(() => dashboard.summarizeProbes(extra, "generation-a"), {
        message: "INVALID_PROBE_SCHEMA",
    });

    const missing = probeDocument();
    delete missing.probes[0].actual_bytes;
    assert.throws(() => dashboard.summarizeProbes(missing, "generation-a"), {
        message: "INVALID_PROBE_SCHEMA",
    });

    const badCategory = probeDocument([probe({category: "arbitrary"})]);
    assert.throws(() => dashboard.summarizeProbes(badCategory, "generation-a"), {
        message: "INVALID_PROBE_SCHEMA",
    });

    const inconsistent = probeDocument([probe({attempts: 3})]);
    assert.throws(() => dashboard.summarizeProbes(inconsistent, "generation-a"), {
        message: "INVALID_PROBE_SCHEMA",
    });
});

test("探针代际必须来自当前运行快照", () => {
    assert.throws(() => dashboard.summarizeProbes(probeDocument(), "different-generation"), {
        message: "PROBE_GENERATION_MISMATCH",
    });
});

test("轮询立即开始且单在途，完成后才计时", async () => {
    const pending = deferred();
    const scheduled = [];
    const samples = [];
    let calls = 0;
    const poller = dashboard.createPoller({
        fetcher: () => {
            calls += 1;
            return pending.promise;
        },
        onSample: value => samples.push(value),
        onError: () => {},
        intervalMs: 5000,
        setTimeoutFn: (fn, delay) => {
            scheduled.push({fn, delay});
            return scheduled.length;
        },
        clearTimeoutFn: () => {},
    });
    poller.start();
    poller.start();
    assert.equal(calls, 1);
    assert.equal(poller.isInflight(), true);
    assert.equal(scheduled.length, 0);
    pending.resolve(snapshot());
    await pending.promise;
    await Promise.resolve();
    assert.equal(samples.length, 1);
    assert.equal(poller.isInflight(), false);
    assert.deepEqual(scheduled.map(item => item.delay), [5000]);
    poller.stop();
});

test("停止会 abort 并忽略迟到响应，重启也不接受旧代请求", async () => {
    const requests = [];
    const samples = [];
    const errors = [];
    const poller = dashboard.createPoller({
        fetcher: signal => {
            const pending = deferred();
            requests.push({pending, signal});
            return pending.promise;
        },
        onSample: value => samples.push(value),
        onError: value => errors.push(value),
        setTimeoutFn: () => 1,
        clearTimeoutFn: () => {},
    });
    poller.start();
    poller.stop();
    assert.equal(requests[0].signal.aborted, true);
    poller.start();
    requests[0].pending.resolve(snapshot({process_generation: "old-request"}));
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(samples.length, 0);
    assert.equal(errors.length, 0);
    requests[1].pending.resolve(snapshot({process_generation: "new-request"}));
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(samples.length, 1);
    assert.equal(samples[0].generation, "new-request");
    poller.stop();
});

test("请求失败产生 stale/unknown 状态且不冒充有效样本", async () => {
    const pending = deferred();
    const errors = [];
    const samples = [];
    const poller = dashboard.createPoller({
        fetcher: () => pending.promise,
        onSample: value => samples.push(value),
        onError: value => errors.push(value),
        setTimeoutFn: () => 1,
        clearTimeoutFn: () => {},
    });
    poller.start();
    // stub 不接管 Promise 时，也只保留断言失败，不让 Node 额外报未处理拒绝。
    pending.promise.catch(() => {});
    pending.reject(new Error("private network detail"));
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(samples.length, 0);
    assert.deepEqual(errors, [{stale: true, status: "unknown", error: "FETCH_FAILED"}]);
    poller.stop();
});

(async function run() {
    let failures = 0;
    for (const {name, fn} of tests) {
        try {
            await fn();
            console.log("ok - " + name);
        } catch (error) {
            failures += 1;
            console.error("not ok - " + name);
            console.error(error && error.stack ? error.stack : error);
        }
    }
    console.log(`\n${tests.length - failures}/${tests.length} passed`);
    if (failures) {
        process.exitCode = 1;
    }
}());
