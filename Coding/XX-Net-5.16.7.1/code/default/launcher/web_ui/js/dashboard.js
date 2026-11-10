(function (root, factory) {
    const api = factory();
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.XXNetDashboard = api;
    }
}(typeof globalThis !== "undefined" ? globalThis : this, function () {
    "use strict";

    const MODULE_NAMES = ["gae_proxy", "smart_router", "x_tunnel"];
    const PROCESS_FIELDS = [
        "pid", "started_at", "threads", "rss_bytes", "cpu_time_seconds",
        "fd_count", "fd_soft_limit", "background_exceptions_total",
        "unknown_stops_total", "log_queue_length", "log_dropped_total",
        "log_write_failures_total", "log_throttled_total",
    ];
    const PROBE_CATEGORIES = ["control", "http", "https", "socks5", "direct", "proxy"];
    const PROBE_STATUSES = ["idle", "running", "complete", "failed"];
    const FAILURE_NAMES = [
        "timeout", "truncated", "connection", "http_status", "content_mismatch", "other",
    ];
    const PROBE_FIELDS = [
        "category", "status", "attempts", "complete_successes", "failures",
        "expected_bytes", "actual_bytes", "latency_seconds",
    ];

    function isObject(value) {
        return value !== null && typeof value === "object" && !Array.isArray(value);
    }

    function hasExactKeys(value, required, optional) {
        if (!isObject(value)) {
            return false;
        }
        const allowed = new Set(required.concat(optional || []));
        const keys = Object.keys(value);
        return required.every(key => Object.prototype.hasOwnProperty.call(value, key)) &&
            keys.every(key => allowed.has(key));
    }

    function isFiniteNumber(value) {
        return typeof value === "number" && Number.isFinite(value);
    }

    function isNonNegativeInteger(value) {
        return Number.isInteger(value) && value >= 0;
    }

    function invalidProbe() {
        throw new Error("INVALID_PROBE_SCHEMA");
    }

    function normalizeSnapshot(value) {
        if (!isObject(value) || value.schema_version !== 1 ||
                !isFiniteNumber(value.sampled_at) ||
                typeof value.process_generation !== "string" || !value.process_generation ||
                !isFiniteNumber(value.collection_duration_seconds) ||
                !isObject(value.process) || !isObject(value.launcher) || !isObject(value.modules)) {
            throw new Error("INVALID_RUNTIME_SNAPSHOT");
        }
        const process = Object.assign({}, value.process);
        for (const field of PROCESS_FIELDS) {
            if (process[field] === undefined) {
                process[field] = null;
            }
        }
        const modules = {};
        for (const name of MODULE_NAMES) {
            const moduleValue = value.modules[name];
            if (!isObject(moduleValue) ||
                    !["running", "not_running", "unknown"].includes(moduleValue.state) ||
                    !["ok", "not_running", "error", "unknown"].includes(moduleValue.collection)) {
                throw new Error("INVALID_RUNTIME_SNAPSHOT");
            }
            modules[name] = {
                state: moduleValue.state,
                collection: moduleValue.collection,
                metrics: isObject(moduleValue.metrics) ? Object.assign({}, moduleValue.metrics) : null,
            };
        }
        const moduleStates = MODULE_NAMES.map(name => name + ":" + modules[name].state).join("|");
        const pipeStatus = smartRouterPipeStatus({modules});
        return {
            valid: true,
            stale: false,
            schemaVersion: 1,
            sampledAt: value.sampled_at,
            generation: value.process_generation,
            collectionDurationSeconds: value.collection_duration_seconds,
            process,
            launcher: Object.assign({}, value.launcher),
            modules,
            segment: value.process_generation + "|" + moduleStates + "|pipe:" + pipeStatus,
        };
    }

    function appendSample(samples, value, limit) {
        const maximum = limit === undefined ? 360 : limit;
        if (!Array.isArray(samples) || !isNonNegativeInteger(maximum) || maximum < 1) {
            throw new Error("INVALID_SAMPLE_BUFFER");
        }
        const normalized = value && value.valid === true && value.stale === false &&
            typeof value.segment === "string" ? value : normalizeSnapshot(value);
        return samples.concat([normalized]).slice(-maximum);
    }

    function counterDelta(samples, moduleName, field) {
        if (!Array.isArray(samples) || samples.length < 2) {
            return null;
        }
        const previous = samples[samples.length - 2];
        const current = samples[samples.length - 1];
        if (!previous || !current || previous.stale || current.stale ||
                previous.segment !== current.segment) {
            return null;
        }
        const previousModule = previous.modules && previous.modules[moduleName];
        const currentModule = current.modules && current.modules[moduleName];
        if (!previousModule || !currentModule || !previousModule.metrics || !currentModule.metrics) {
            return null;
        }
        const before = moduleMetric(previous, moduleName, field);
        const after = moduleMetric(current, moduleName, field);
        if (before === null || after === null || after < before) {
            return null;
        }
        return after - before;
    }

    function observationWindow(samples) {
        const empty = {count: 0, start: null, end: null, seconds: null};
        if (!Array.isArray(samples)) {
            return empty;
        }
        const valid = sample => sample && sample.valid && !sample.stale && isFiniteNumber(sample.sampledAt);
        let endIndex = samples.length - 1;
        while (endIndex >= 0 && !valid(samples[endIndex])) {
            endIndex -= 1;
        }
        if (endIndex < 0) {
            return empty;
        }
        const segment = samples[endIndex].segment;
        let startIndex = endIndex;
        while (startIndex > 0 && valid(samples[startIndex - 1]) &&
                samples[startIndex - 1].segment === segment) {
            startIndex -= 1;
        }
        const start = samples[startIndex].sampledAt;
        const end = samples[endIndex].sampledAt;
        return {count: endIndex - startIndex + 1, start, end, seconds: Math.max(0, end - start)};
    }

    function formatRate(value) {
        if (!isFiniteNumber(value) || value < 0 || value > 1) {
            return "NO_SAMPLE";
        }
        return (value * 100).toFixed(1) + "%";
    }

    function validateProbe(value) {
        if (!hasExactKeys(value, PROBE_FIELDS, ["content_sha256"]) ||
                !PROBE_CATEGORIES.includes(value.category) ||
                !PROBE_STATUSES.includes(value.status) ||
                !isNonNegativeInteger(value.attempts) ||
                !isNonNegativeInteger(value.complete_successes) ||
                value.complete_successes > value.attempts ||
                !hasExactKeys(value.failures, FAILURE_NAMES) ||
                !isNonNegativeInteger(value.expected_bytes) ||
                !isNonNegativeInteger(value.actual_bytes) ||
                !isFiniteNumber(value.latency_seconds) || value.latency_seconds < 0) {
            invalidProbe();
        }
        let failureCount = 0;
        for (const name of FAILURE_NAMES) {
            if (!isNonNegativeInteger(value.failures[name])) {
                invalidProbe();
            }
            failureCount += value.failures[name];
        }
        if (value.complete_successes + failureCount > value.attempts ||
                (value.status === "idle" && value.attempts !== 0) ||
                (value.status === "complete" &&
                    (value.complete_successes !== value.attempts || failureCount !== 0)) ||
                (value.status === "failed" && failureCount === 0)) {
            invalidProbe();
        }
        if (value.content_sha256 !== undefined &&
                (typeof value.content_sha256 !== "string" || !/^[0-9a-fA-F]{64}$/.test(value.content_sha256))) {
            invalidProbe();
        }
        return {
            category: value.category,
            status: value.status,
            attempts: value.attempts,
            completeSuccesses: value.complete_successes,
            failures: Object.assign({}, value.failures),
            expectedBytes: value.expected_bytes,
            actualBytes: value.actual_bytes,
            latencySeconds: value.latency_seconds,
            contentSha256: value.content_sha256 || null,
        };
    }

    function summarizeProbes(document, currentGeneration) {
        if (!hasExactKeys(document, ["schema_version", "process_generation", "probes"]) ||
                document.schema_version !== 1 || !Array.isArray(document.probes)) {
            invalidProbe();
        }
        if (typeof currentGeneration !== "string" || !currentGeneration ||
                document.process_generation !== currentGeneration) {
            throw new Error("PROBE_GENERATION_MISMATCH");
        }
        const items = document.probes.map(validateProbe);
        const summary = {
            attempts: 0,
            completeSuccesses: 0,
            successRate: null,
            display: "NO_SAMPLE",
            failures: Object.fromEntries(FAILURE_NAMES.map(name => [name, 0])),
            expectedBytes: 0,
            actualBytes: 0,
            items,
        };
        for (const item of items) {
            summary.attempts += item.attempts;
            summary.completeSuccesses += item.completeSuccesses;
            summary.expectedBytes += item.expectedBytes;
            summary.actualBytes += item.actualBytes;
            for (const name of FAILURE_NAMES) {
                summary.failures[name] += item.failures[name];
            }
        }
        if (summary.attempts > 0) {
            summary.successRate = summary.completeSuccesses / summary.attempts;
            summary.display = formatRate(summary.successRate);
        }
        return summary;
    }

    function createPoller(options) {
        const settings = options || {};
        if (typeof settings.fetcher !== "function") {
            throw new Error("POLLER_FETCHER_REQUIRED");
        }
        const onSample = typeof settings.onSample === "function" ? settings.onSample : function () {};
        const onError = typeof settings.onError === "function" ? settings.onError : function () {};
        const intervalMs = settings.intervalMs === undefined ? 5000 : settings.intervalMs;
        const setTimeoutFn = settings.setTimeoutFn || setTimeout;
        const clearTimeoutFn = settings.clearTimeoutFn || clearTimeout;
        const Controller = settings.AbortController || AbortController;
        let running = false;
        let inflight = false;
        let timer = null;
        let controller = null;
        let generation = 0;

        function schedule(runGeneration) {
            if (!running || runGeneration !== generation) {
                return;
            }
            timer = setTimeoutFn(function () {
                timer = null;
                execute(runGeneration);
            }, intervalMs);
        }

        function execute(runGeneration) {
            if (!running || inflight || runGeneration !== generation) {
                return;
            }
            inflight = true;
            controller = new Controller();
            let request;
            try {
                request = Promise.resolve(settings.fetcher(controller.signal));
            } catch (error) {
                request = Promise.reject(error);
            }
            request.then(function (value) {
                if (!running || runGeneration !== generation) {
                    return;
                }
                try {
                    onSample(normalizeSnapshot(value));
                } catch (error) {
                    onError({stale: true, status: "unknown", error: "INVALID_RUNTIME_SNAPSHOT"});
                }
            }, function () {
                if (!running || runGeneration !== generation) {
                    return;
                }
                onError({stale: true, status: "unknown", error: "FETCH_FAILED"});
            }).then(function () {
                if (runGeneration !== generation) {
                    return;
                }
                inflight = false;
                controller = null;
                schedule(runGeneration);
            });
        }

        return {
            start: function () {
                if (running) {
                    return;
                }
                running = true;
                generation += 1;
                execute(generation);
            },
            stop: function () {
                running = false;
                generation += 1;
                if (timer !== null) {
                    clearTimeoutFn(timer);
                    timer = null;
                }
                if (controller) {
                    controller.abort();
                    controller = null;
                }
                inflight = false;
            },
            isRunning: function () { return running; },
            isInflight: function () { return inflight; },
        };
    }

    function byId(id) {
        return document.getElementById(id);
    }

    function setText(id, value) {
        const element = byId(id);
        if (element) {
            element.textContent = value;
        }
    }

    function numberOrNull(value) {
        return isFiniteNumber(value) ? value : null;
    }

    function formatNumber(value) {
        return isFiniteNumber(value) ? new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 2}).format(value) : "NO_SAMPLE";
    }

    function formatBytes(value) {
        if (!isFiniteNumber(value)) {
            return "NO_SAMPLE";
        }
        const units = ["B", "KiB", "MiB", "GiB"];
        let scaled = value;
        let unit = 0;
        while (scaled >= 1024 && unit < units.length - 1) {
            scaled /= 1024;
            unit += 1;
        }
        return scaled.toFixed(unit === 0 ? 0 : 1) + " " + units[unit];
    }

    function formatTime(value) {
        if (!isFiniteNumber(value)) {
            return "NO_SAMPLE";
        }
        return new Date(value * 1000).toLocaleTimeString("zh-CN", {hour12: false});
    }

    function formatSeconds(value) {
        return isFiniteNumber(value) ? value.toFixed(2) + " s" : "NO_SAMPLE";
    }

    function moduleMetric(sample, moduleName, field) {
        if (!sample || !sample.modules[moduleName] || !sample.modules[moduleName].metrics) {
            return null;
        }
        const metrics = sample.modules[moduleName].metrics;
        const value = metrics[field] === undefined ? metrics[moduleName + "." + field] : metrics[field];
        return numberOrNull(value);
    }

    function smartRouterPipeStatus(sample) {
        if (!sample || !sample.modules || !sample.modules.smart_router) {
            return "unknown";
        }
        const module = sample.modules.smart_router;
        if (module.state === "not_running") {
            return "not_running";
        }
        if (module.state !== "running" || !module.metrics) {
            return "unknown";
        }
        const qualified = module.metrics["smart_router.pipe_running"];
        const value = numberOrNull(qualified === undefined ? module.metrics.pipe_running : qualified);
        if (value === 1) {
            return "running";
        }
        if (value === 0) {
            return "stopped";
        }
        return "unknown";
    }

    function frontMetric(sample, field) {
        if (!sample || !sample.modules.gae_proxy.metrics) {
            return null;
        }
        const fronts = sample.modules.gae_proxy.metrics["front.by_type"];
        if (!isObject(fronts)) {
            return null;
        }
        let total = 0;
        let found = false;
        for (const category of ["gae", "direct", "cloudflare", "cloudfront"]) {
            const value = fronts[category] && fronts[category][field];
            if (isFiniteNumber(value)) {
                total += value;
                found = true;
            }
        }
        return found ? total : null;
    }

    function frontDelta(samples, field) {
        if (!Array.isArray(samples) || samples.length < 2) {
            return null;
        }
        const previous = samples[samples.length - 2];
        const current = samples[samples.length - 1];
        if (previous.segment !== current.segment) {
            return null;
        }
        const before = frontMetric(previous, field);
        const after = frontMetric(current, field);
        return before !== null && after !== null && after >= before ? after - before : null;
    }

    function svgElement(name, attributes) {
        const element = document.createElementNS("http://www.w3.org/2000/svg", name);
        for (const [key, value] of Object.entries(attributes || {})) {
            element.setAttribute(key, String(value));
        }
        return element;
    }

    function appendText(parent, value, className) {
        const element = document.createElement("span");
        if (className) {
            element.className = className;
        }
        element.textContent = value;
        parent.appendChild(element);
        return element;
    }

    function renderLegend(id, series) {
        const legend = byId(id);
        if (!legend) {
            return;
        }
        const items = series.map((item, index) => {
            const wrapper = document.createElement("span");
            wrapper.className = "legend-item";
            const swatch = document.createElement("span");
            swatch.className = "legend-swatch";
            swatch.dataset.series = String(index);
            swatch.setAttribute("aria-hidden", "true");
            wrapper.appendChild(swatch);
            appendText(wrapper, item.name);
            return wrapper;
        });
        legend.replaceChildren(...items);
    }

    function renderLineChart(plotId, series) {
        const plot = byId(plotId);
        if (!plot) {
            return;
        }
        renderLegend(plotId.replace("-chart", "-legend"), series);
        const points = series.flatMap((item, seriesIndex) => item.points.map(point => ({
            x: point.x, y: point.y, segment: point.segment, seriesIndex, name: item.name,
        }))).filter(point => isFiniteNumber(point.x) && isFiniteNumber(point.y));
        if (!points.length) {
            const empty = document.createElement("div");
            empty.className = "chart-empty";
            empty.textContent = "NO_SAMPLE";
            plot.replaceChildren(empty);
            return;
        }
        const width = 640;
        const height = 230;
        const margin = {top: 18, right: 60, bottom: 30, left: 50};
        const innerWidth = width - margin.left - margin.right;
        const innerHeight = height - margin.top - margin.bottom;
        const minX = Math.min(...points.map(point => point.x));
        const maxX = Math.max(...points.map(point => point.x));
        const maxY = Math.max(1, ...points.map(point => point.y));
        const x = value => margin.left + (maxX === minX ? innerWidth / 2 :
            (value - minX) / (maxX - minX) * innerWidth);
        const y = value => margin.top + innerHeight - value / maxY * innerHeight;
        const svg = svgElement("svg", {
            class: "chart-svg", viewBox: `0 0 ${width} ${height}`,
            role: "img", "aria-label": plot.getAttribute("aria-label") || "运行趋势图",
        });
        for (let tick = 0; tick <= 4; tick += 1) {
            const value = maxY * tick / 4;
            const lineY = y(value);
            svg.appendChild(svgElement("line", {
                class: "chart-grid-line", x1: margin.left, x2: width - margin.right,
                y1: lineY, y2: lineY,
            }));
            const label = svgElement("text", {
                class: "chart-axis-label", x: margin.left - 7, y: lineY + 3, "text-anchor": "end",
            });
            label.textContent = formatNumber(value);
            svg.appendChild(label);
        }
        for (const time of [minX, maxX]) {
            const label = svgElement("text", {
                class: "chart-axis-label", x: x(time), y: height - 8,
                "text-anchor": time === minX ? "start" : "end",
            });
            label.textContent = formatTime(time);
            svg.appendChild(label);
        }
        const colors = ["var(--series-a)", "var(--series-b)", "var(--series-c)"];
        series.forEach((item, seriesIndex) => {
            let path = "";
            let previous = null;
            for (const point of item.points) {
                if (!isFiniteNumber(point.x) || !isFiniteNumber(point.y)) {
                    previous = null;
                    continue;
                }
                const command = previous && previous.segment === point.segment ? "L" : "M";
                path += `${command}${x(point.x).toFixed(2)},${y(point.y).toFixed(2)} `;
                previous = point;
            }
            if (path) {
                svg.appendChild(svgElement("path", {
                    class: "chart-series", d: path.trim(), stroke: colors[seriesIndex],
                    "data-series": seriesIndex, "stroke-width": 2,
                }));
            }
            const valid = item.points.filter(point => isFiniteNumber(point.x) && isFiniteNumber(point.y));
            const last = valid[valid.length - 1];
            if (last) {
                svg.appendChild(svgElement("circle", {
                    class: "chart-point", cx: x(last.x), cy: y(last.y), r: 4,
                    fill: colors[seriesIndex], "data-series": seriesIndex,
                }));
                const directLabel = svgElement("text", {
                    class: "chart-axis-label", x: Math.min(width - margin.right + 7, width - 50),
                    y: y(last.y) + 3,
                });
                directLabel.textContent = formatNumber(last.y);
                svg.appendChild(directLabel);
            }
        });
        const crosshair = svgElement("line", {
            class: "chart-crosshair", x1: 0, x2: 0, y1: margin.top,
            y2: height - margin.bottom, visibility: "hidden",
        });
        svg.appendChild(crosshair);
        const tooltip = plot.parentElement.querySelector(".chart-tooltip");
        function hideTooltip() {
            crosshair.setAttribute("visibility", "hidden");
            if (tooltip) {
                tooltip.hidden = true;
            }
        }
        svg.addEventListener("pointermove", event => {
            const bounds = svg.getBoundingClientRect();
            if (!bounds.width || !tooltip) {
                return;
            }
            const localX = (event.clientX - bounds.left) / bounds.width * width;
            const nearest = points.reduce((best, point) =>
                Math.abs(x(point.x) - localX) < Math.abs(x(best.x) - localX) ? point : best, points[0]);
            const sameTime = points.filter(point => point.x === nearest.x);
            crosshair.setAttribute("x1", x(nearest.x));
            crosshair.setAttribute("x2", x(nearest.x));
            crosshair.setAttribute("visibility", "visible");
            const lines = [formatTime(nearest.x)];
            for (const point of sameTime) {
                lines.push(point.name + " " + formatNumber(point.y));
            }
            const children = lines.map(line => {
                const row = document.createElement("div");
                row.textContent = line;
                return row;
            });
            tooltip.replaceChildren(...children);
            tooltip.hidden = false;
            const plotBounds = plot.getBoundingClientRect();
            const requestedLeft = event.clientX - plotBounds.left + 12;
            tooltip.style.left = Math.max(0, Math.min(requestedLeft, plotBounds.width - 170)) + "px";
            tooltip.style.top = Math.max(0, event.clientY - plotBounds.top - 15) + "px";
        });
        svg.addEventListener("pointerleave", hideTooltip);
        svg.addEventListener("blur", hideTooltip);
        plot.replaceChildren(svg);
    }

    function makeCell(value, header) {
        const cell = document.createElement(header ? "th" : "td");
        if (header) {
            cell.scope = "row";
        }
        cell.textContent = value;
        return cell;
    }

    function renderRows(id, rows, emptyColspan) {
        const body = byId(id);
        if (!body) {
            return;
        }
        if (!rows.length) {
            const row = document.createElement("tr");
            const cell = makeCell("NO_SAMPLE", false);
            cell.colSpan = emptyColspan;
            row.appendChild(cell);
            body.replaceChildren(row);
            return;
        }
        body.replaceChildren(...rows.map(values => {
            const row = document.createElement("tr");
            values.forEach((value, index) => row.appendChild(makeCell(value, index === 0 && typeof value === "string" && !value.includes(":"))));
            return row;
        }));
    }

    function renderCharts(samples) {
        const queuePoints = samples.map(sample => ({
            x: sample.sampledAt, y: moduleMetric(sample, "x_tunnel", "cmd_queue_window_ratio"),
            segment: sample.segment,
        })).filter(point => point.y !== null);
        renderLineChart("queue-chart", [{name: "队列/协议窗口", points: queuePoints}]);
        renderRows("queue-table-body", samples.slice().reverse().map(sample => [
            formatTime(sample.sampledAt),
            formatBytes(moduleMetric(sample, "x_tunnel", "cmd_queue_bytes")),
            formatRate(moduleMetric(sample, "x_tunnel", "cmd_queue_window_ratio")),
            formatSeconds(moduleMetric(sample, "x_tunnel", "cmd_queue_gap_age_seconds")),
        ]), 4);

        const retryPoints = [];
        const recoveryPoints = [];
        const eventRows = [];
        samples.forEach((sample, index) => {
            const windowSamples = samples.slice(0, index + 1);
            const retries = frontDelta(windowSamples, "request_retries_total");
            const recoveries = counterDelta(windowSamples, "x_tunnel", "command_gap_recovered_total");
            if (retries !== null) {
                retryPoints.push({x: sample.sampledAt, y: retries, segment: sample.segment});
            }
            if (recoveries !== null) {
                recoveryPoints.push({x: sample.sampledAt, y: recoveries, segment: sample.segment});
            }
            eventRows.unshift([formatTime(sample.sampledAt), formatNumber(retries), formatNumber(recoveries)]);
        });
        renderLineChart("events-chart", [
            {name: "Front 重试", points: retryPoints},
            {name: "缺口恢复", points: recoveryPoints},
        ]);
        renderRows("events-table-body", eventRows, 3);

        const threadPoints = samples.map(sample => ({
            x: sample.sampledAt, y: numberOrNull(sample.process.threads), segment: sample.segment,
        })).filter(point => point.y !== null);
        const fdPoints = samples.map(sample => ({
            x: sample.sampledAt, y: numberOrNull(sample.process.fd_count), segment: sample.segment,
        })).filter(point => point.y !== null);
        renderLineChart("resources-chart", [
            {name: "线程", points: threadPoints},
            {name: "文件描述符", points: fdPoints},
        ]);
        renderRows("resources-table-body", samples.slice().reverse().map(sample => [
            formatTime(sample.sampledAt), formatNumber(sample.process.threads), formatNumber(sample.process.fd_count),
        ]), 3);
    }

    function renderResources(current) {
        if (!current) {
            renderRows("resource-table-body", [], 2);
            return;
        }
        const rows = [
            ["线程", formatNumber(current.process.threads)],
            ["RSS", formatBytes(current.process.rss_bytes)],
            ["CPU 累计时间", formatSeconds(current.process.cpu_time_seconds)],
            ["文件描述符", formatNumber(current.process.fd_count)],
            ["fd 软限制", formatNumber(current.process.fd_soft_limit)],
            ["启动模式", current.launcher.startup_mode || "unknown"],
            ["托盘后端", current.launcher.tray_backend || "unknown"],
            ["托盘状态", current.launcher.tray_state || "unknown"],
            ["页面打开请求", current.launcher.page_open_request || "unknown"],
        ];
        renderRows("resource-table-body", rows, 2);
        setText("evidence-startup", current.launcher.startup_mode || "unknown");
        setText("evidence-tray", current.launcher.tray_state || "unknown");
        setText("evidence-page", current.launcher.page_open_request || "unknown");
    }

    function renderExceptions(samples, current) {
        if (!current) {
            renderRows("exception-table-body", [], 4);
            return;
        }
        const definitions = [
            ["乱序缺口", "x_tunnel", "command_gaps_total", "出现缺口不等于已恢复"],
            ["缺口恢复", "x_tunnel", "command_gap_recovered_total", "仅在原缺失命令恢复按序处理后计数"],
            ["队列保护停止", "x_tunnel", "command_queue_overflow_total", "受控停止，不代表正常恢复"],
            ["永久缺口期限", "x_tunnel", "command_gap_timeout_total", "到期停止，不计作恢复"],
            ["DNS 空结果", "smart_router", "dns.empty_results_total", "按时返回但没有地址"],
            ["DNS 超时", "smart_router", "dns.timeouts_total", "只记录真实期限事件"],
            ["direct 建连回退", "smart_router", "direct_connect_failures_total", "会尝试下一条路由，不等于用户最终失败"],
            ["PipeSocks 交接拒绝", "smart_router", "handoff_failures_total", "不包含 direct 建连回退；不得计作 direct 成功"],
            ["注册失败", "smart_router", "pipe_register_failures_total", "只回滚当前配对，不应拖停整个管道"],
            ["停止后拒绝", "smart_router", "pipe_stopped_handoffs_total", "管道停止后到达的新配对"],
            ["关闭 socket 拒绝", "smart_router", "pipe_closed_socket_handoffs_total", "发布前发现 socket 已关闭"],
            ["selector 故障", "smart_router", "pipe_select_failures_total", "关键管道线程停止并清理现有配对"],
            ["转发循环故障", "smart_router", "pipe_loop_failures_total", "关键管道线程停止并清理现有配对"],
            ["日志丢弃", "process", "log_dropped_total", "持久日志队列实际丢弃事件"],
            ["日志写失败", "process", "log_write_failures_total", "写失败批次，不包含异常正文"],
        ];
        const rows = definitions.map(([label, moduleName, field, boundary]) => {
            const value = moduleName === "process" ? numberOrNull(current.process[field]) :
                moduleMetric(current, moduleName, field);
            const delta = moduleName === "process" ? null : counterDelta(samples, moduleName, field);
            return [label, formatNumber(value), formatNumber(delta), boundary];
        });
        const pipeStatus = smartRouterPipeStatus(current);
        const pipeLabels = {
            running: "运行中", stopped: "已停止", not_running: "模块未运行", unknown: "NO_SAMPLE",
        };
        rows.unshift([
            "PipeSocks 转发状态", pipeLabels[pipeStatus], "—",
            "显式读取 pipe_running；socket 集合锁忙不会改变这个状态",
        ]);
        renderRows("exception-table-body", rows, 4);
    }

    function renderProbes(summary) {
        const hasSummary = summary !== null;
        setText("probe-success", hasSummary ? summary.display : "NO_SAMPLE");
        setText("probe-attempts", hasSummary && summary.attempts ?
            `${summary.completeSuccesses} / ${summary.attempts} 次完整成功` : "没有当前代际的有效探针尝试");
        for (const name of FAILURE_NAMES) {
            setText("failure-" + name, hasSummary ? formatNumber(summary.failures[name]) : "0");
        }
        const rows = hasSummary ? summary.items.map(item => [
            item.category, item.status, formatNumber(item.attempts), formatNumber(item.completeSuccesses),
            formatBytes(item.expectedBytes), formatBytes(item.actualBytes), formatSeconds(item.latencySeconds),
        ]) : [];
        renderRows("probe-table-body", rows, 7);
    }

    function renderDashboard(samples, current, probeSummary, stale) {
        const window = observationWindow(samples);
        setText("observation-window", "实际观察窗口：" + (window.count ?
            `${formatTime(window.start)}—${formatTime(window.end)}，${window.count} 个有效样本，${window.seconds} 秒` : "NO_SAMPLE"));
        setText("sample-health", stale ? "unknown / stale" : window.count ? `${window.count} 个有效样本` : "NO_SAMPLE");
        setText("sample-detail", current ? `采集耗时 ${formatSeconds(current.collectionDurationSeconds)}` : "没有有效样本");
        setText("generation-state", current ? current.generation : "unknown");
        const states = current ? MODULE_NAMES.map(name => name + ": " + current.modules[name].state) : [];
        const pipeStatus = current ? smartRouterPipeStatus(current) : "unknown";
        const pipeSuffix = pipeStatus === "stopped" ? "；PipeSocks 已停止" :
            pipeStatus === "unknown" && current && current.modules.smart_router.state === "running" ?
                "；PipeSocks 状态未知" : "";
        setText("module-state", current ?
            states.filter(value => value.endsWith(": running")).length + " / 3 运行" + pipeSuffix : "unknown");
        setText("module-detail", current ? states.join("；") : "GAE、Smart Router、X-Tunnel");
        const status = byId("collection-status");
        if (status) {
            status.dataset.status = stale ? "stale" : current ? "ok" : "unknown";
            status.textContent = stale ? "采样失败；最近数据已过期" : current ? "运行快照已更新" : "等待首个运行快照";
        }
        renderCharts(samples);
        renderResources(current);
        renderExceptions(samples, current);
        renderProbes(probeSummary);
    }

    function startDashboard() {
        let samples = [];
        let current = null;
        let probeSummary = null;
        const poller = createPoller({
            fetcher: signal => fetch("/runtime_metrics", {
                method: "GET", headers: {Accept: "application/json"}, cache: "no-store", signal,
            }).then(response => {
                if (!response.ok) {
                    throw new Error("RUNTIME_HTTP_ERROR");
                }
                return response.json();
            }),
            onSample: sample => {
                if (current && current.generation !== sample.generation) {
                    probeSummary = null;
                    setText("probe-import-status", "进程已换代；请导入新代际的探针结果");
                }
                current = sample;
                samples = appendSample(samples, sample, 360);
                renderDashboard(samples, current, probeSummary, false);
            },
            onError: () => renderDashboard(samples, current, probeSummary, true),
        });
        byId("toggle-polling").addEventListener("click", () => {
            if (poller.isRunning()) {
                poller.stop();
                setText("toggle-polling", "继续采样");
                const status = byId("collection-status");
                status.dataset.status = "unknown";
                status.textContent = "采样已暂停；页面数据不再更新";
            } else {
                setText("toggle-polling", "暂停采样");
                poller.start();
            }
        });
        byId("reset-observation").addEventListener("click", () => {
            poller.stop();
            samples = [];
            current = null;
            probeSummary = null;
            renderDashboard(samples, current, probeSummary, false);
            poller.start();
            setText("toggle-polling", "暂停采样");
        });
        byId("probe-file").addEventListener("change", async event => {
            const input = event.currentTarget;
            const file = input.files && input.files[0];
            if (!file) {
                return;
            }
            if (!current) {
                setText("probe-import-status", "等待运行快照；未读取探针文件");
                input.value = "";
                return;
            }
            if (file.size > 1048576) {
                setText("probe-import-status", "探针文件超过 1 MiB，已拒绝");
                input.value = "";
                return;
            }
            try {
                const documentValue = JSON.parse(await file.text());
                probeSummary = summarizeProbes(documentValue, current.generation);
                setText("probe-import-status", "已导入当前代际探针；仅显示白名单汇总");
                renderProbes(probeSummary);
            } catch (error) {
                probeSummary = null;
                const message = error && error.message === "PROBE_GENERATION_MISMATCH" ?
                    "探针代际与当前运行快照不符，已拒绝" : "探针 schema 或 JSON 无效，已拒绝";
                setText("probe-import-status", message);
                renderProbes(null);
            } finally {
                input.value = "";
            }
        });
        window.addEventListener("pagehide", () => poller.stop());
        renderDashboard(samples, current, probeSummary, false);
        poller.start();
    }

    if (typeof document !== "undefined") {
        document.addEventListener("DOMContentLoaded", startDashboard, {once: true});
    }

    return {
        normalizeSnapshot,
        appendSample,
        summarizeProbes,
        counterDelta,
        moduleMetric,
        smartRouterPipeStatus,
        observationWindow,
        formatRate,
        createPoller,
    };
}));
