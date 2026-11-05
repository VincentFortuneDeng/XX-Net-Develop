# X-Tunnel Front 分阶段诊断 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变传输、重试、评分和节流行为的前提下，让同一 `tid` 的失败日志能够回答“选择了哪条 front、停在哪个请求状态、耗时多少、返回什么内部状态”。

**Architecture:** 不新增常驻采样器或高频率 DEBUG 日志，只扩充两条既有 WARNING：dispatcher 无响应时输出 `Task.unique_id` 和状态轨迹，统一 front 重试时输出 front 名称、状态码及选择/请求耗时。沿用现有按 endpoint、worker、host/path 的节流，再以离线脚本按 tid 汇总证据；生产部署与重启不在本计划内。

**Tech Stack:** Python 3 标准库、既有 `Task.get_trace()`、`LogThrottle`、`unittest`、离线 JSON 日志分析。

**Spec:** `docs/2026-11-05-xtunnel-warning-analysis.md`，重点是“27 日前后基线与跨层去重”和“当前分级结论与后续边界”。

## Global Constraints

- 只在 `fix/h-log-dns-backoff` 开发分支工作；不直接在 main 开发，不部署、不重启、不修改生产配置或进程，不 push、不创建 PR。
- 所有实现遵循 TDD：先运行新测试并观察预期 RED，再写最小实现并观察 GREEN。
- 不改变 front 选择、请求 deadline、重试次数、worker 状态迁移、评分、传输内容或现有节流键；不增加每请求一条的 DEBUG。
- 日志不得输出账号、密码、token、请求头、正文或会话密钥；只允许既有的 tid、front 名、状态码、轨迹、worker IP 与耗时。
- 离线测试使用 `python3 -I`；不读取生产凭据，不执行真实 `iptables`，不注入生产故障。
- 不把离线测试、文件部署、单次正常请求或短时间无告警写成共同超时已治愈。
- 保留 SDD 证据目录；提交署名使用 `Co-Authored-By: Kimi <noreply@moonshot.cn>`。

## Review Focus

- Seley 经 `X-Path` 传递原始 `/data?tid=...`，dispatcher 内路径却是 `/`：失败日志必须保留原始 tid；Task 1 的 RED 测试覆盖。
- dispatcher 可能在尚未分配 worker 时超时：诊断日志不得假定 `task.worker` 非空；Task 1 覆盖无 worker 队列超时。
- 同一 tid 的重试仍须使用既有节流，不能因诊断字段绕过聚合或增加日志洪峰；Task 2 复跑 X7/X10。
- 请求头和正文可能含敏感信息：只能输出 `Task.unique_id` 与轨迹，不能改为打印 headers/data；Task 1 检查日志不含注入的敏感标记。
- HTTP/2、真实远端排队和系统网络切换无法由离线日志单元测试复现：保持既有 front 包装层诊断，不宣称已覆盖这些生产路径；Task 3 明确剩余边界。

---

### Task 1: 保留无响应请求的原始 tid 与最后状态

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/front_base/http_dispatcher.py:351-410`
- Test: `tests/test_front_body_deadline.py`

**Interfaces:**
- Consumes: 既有 `http_common.Task.unique_id`、`Task.get_trace()`、`Task.set_state()` 和 `_task_log_allowed()`。
- Produces: 无响应时既有 WARNING 变为 `task <unique_id> timeout trace:<trace>[ (suppressed N)]`；返回值、节流键和计数器不变。

- [ ] **Step 1: 写入 dispatcher 无响应诊断的 RED 测试**

在 `test_front_body_deadline.py` 复用其真实 `http_common`/`http_dispatcher` 加载方式，新增 `test_timeout_log_preserves_xtunnel_path_and_trace`。用 `HttpsDispatcher.__new__()` 建立最小对象，捕获创建的**真实** `Task`，让队列等待超时；在队列退出前将任务推进到 `h1_req_sent`，请求 headers 含 `X-Path: /data?tid=634` 和测试敏感标记，但不含真实凭据。

核心断言：

```python
self.assertIsNone(result)
self.assertEqual(len(logger.warnings), 1)
warning = logger.warnings[0]
self.assertIn("/data?tid=634", warning)
self.assertIn("h1_req_sent", warning)
self.assertIn("get_response", warning)
self.assertNotIn("test-sensitive-marker", warning)
```

- [ ] **Step 2: 运行测试并确认因缺少诊断字段而失败**

Run: `python3 -I tests/test_front_body_deadline.py`
Expected: 新测试 FAIL；旧日志只有方法、host、dispatcher 内路径 `/` 和 timeout，不含 `/data?tid=634`、轨迹或最终 `get_response`。

- [ ] **Step 3: 最小扩展现有无响应 WARNING**

在 `http_dispatcher.py` 中将 `task.set_state("get_response")` 移到响应分支之前，使成功与失败都记录响应等待结束；无响应分支改用 `task.unique_id` 和 `task.get_trace()`，格式严格为：

```python
self.logger.warn("task %s timeout trace:%s%s", task.unique_id, task.get_trace(), suffix)
```

不得打印 headers/body，不改变队列等待、返回值、worker 计数或节流键。

- [ ] **Step 4: 运行定向与既有日志回归**

Run: `python3 -I tests/test_front_body_deadline.py && python3 -I tests/test_xtunnel_log_throttle.py`
Expected: 两个命令均通过；新测试确认 tid/轨迹保留，既有节流与传输行为测试不回归。

- [ ] **Step 5: 提交 Task 1**

```bash
git add tests/test_front_body_deadline.py Coding/XX-Net-5.16.7.1/code/default/lib/noarch/front_base/http_dispatcher.py
git commit -m "test: 保留 front 无响应诊断轨迹"
```

### Task 2: 在既有 front retry 中记录实际选择与耗时

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/front_dispatcher.py:175-224`
- Test: `tests/test_front_request_deadline.py`
- Test: `tests/test_xtunnel_log_throttle.py`

**Interfaces:**
- Consumes: `get_front()` 返回的对象均有类属性 `name`；既有 `get_front_time` 和 `time.monotonic()`。
- Produces: 放行日志包含 `front retry <host><path> front:<name> status:<status> select:<seconds> request:<seconds>`，并继续保留原有 suppressed 后缀。

- [ ] **Step 1: 写入失败尝试字段的 RED 测试**

在 `test_front_request_deadline.py` 让捕获 logger 保存格式化后的 WARNING，给 `Front` stub 增加 `name = "seley_front"`，新增 `test_failed_attempt_log_records_front_status_and_costs`。以一次 503 后一次 200 驱动真实 `request()`，并断言：

```python
self.assertEqual(result[1], 200)
self.assertEqual(len(warnings), 1)
self.assertRegex(
    warnings[0],
    r"front retry example\.com/ front:seley_front status:503 "
    r"select:\d+\.\d+ request:\d+\.\d+$",
)
```

同时保留现有 deadline 断言，证明新增计时没有扩大总预算。

- [ ] **Step 2: 运行测试并确认旧格式不匹配**

Run: `python3 -I tests/test_front_request_deadline.py`
Expected: 新测试 FAIL；旧 WARNING 只有 `front retry example.com/`，没有 front、status、select 或 request 字段。

- [ ] **Step 3: 最小增加失败尝试计时与字段**

在调用 `front.request()` 前记录 `front_request_start = time.monotonic()`，返回后计算 `front_request_time`。只在 `status not in [200, 521, 400, 404]` 的既有放行日志中增加 `front.name`、`status`、`get_front_time` 和 `front_request_time`；suppressed 后缀和 `(host, path)` 节流键保持不变，成功路径不新增日志。

- [ ] **Step 4: 运行 deadline 与节流回归**

Run: `python3 -I tests/test_front_request_deadline.py && python3 -I tests/test_xtunnel_log_throttle.py`
Expected: 两个命令均通过；X7 同一 tid 仍聚合、不同 tid 独立，X10 stop 尾账格式不变，总 deadline 与返回结果不变。

- [ ] **Step 5: 提交 Task 2**

```bash
git add tests/test_front_request_deadline.py tests/test_xtunnel_log_throttle.py Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/front_dispatcher.py
git commit -m "test: 记录 front 失败尝试阶段"
```

### Task 3: 离线关联、完整回归与生产外验收

**Files:**
- Create: `tests/analyze_xtunnel_front_diagnostics.py`
- Create: `tests/test_front_diagnostic_analysis.py`
- Test: 全部 `tests/test_*.py`、`tests/acceptance_independent_models.py`、`tests/performance_baseline.py`
- Evidence: `.superpowers/sdd/2026-11-05-xtunnel-front-diagnostics/`

**Interfaces:**
- Consumes: Task 1 的 `task <unique_id> timeout trace:` 与 Task 2 的 `front retry ... front:... status:... select:... request:...`。
- Produces: 只读分析器 `analyze_records(records: list[dict]) -> dict`，按字符串 tid 分组，输出可见跨层记录中的 front、状态、阶段和耗时；不推断被节流隐藏的尝试，不把多层记录当作多次尝试。

- [ ] **Step 1: 为离线关联写入 RED 测试**

新增 `test_front_diagnostic_analysis.py`，输入一组内存记录，覆盖 Seley 的 `unique_id`、Cloudflare/TLS Relay 的普通 tid、同一 tid 多次失败及一个不含 tid 的警告。断言只有两个 tid 被分组，`634` 同时关联三条 front，保留 `h1_req_sent`、`status:602` 和耗时，未知 tid 进入 `unmatched` 而不是伪造关联。

先调用尚不存在的 `analyze_records()`，并验证记录顺序与 suppressed 数量仅作展示、不会被当成独立请求数。

- [ ] **Step 2: 运行分析器测试并确认接口缺失**

Run: `python3 -I tests/test_front_diagnostic_analysis.py`
Expected: FAIL，原因是 `tests/analyze_xtunnel_front_diagnostics.py` 或 `analyze_records` 尚不存在。

- [ ] **Step 3: 实现最小只读分析器**

仅解析既有文本记录，按 `tid=(\d+)` 或 Seley `unique_id` 中的 `/data?tid=` 提取编号；保存源模块、时间戳、front、status、select/request 秒数和 `Task.get_trace()` 的最后一个状态。输出明确包含 `visible_records`、`unique_tids`、`suppressed_reports` 和 `unmatched`，不得将可见日志数写成真实用户请求数或独立尝试数。

CLI 只接受显式日志路径和输出 JSON 路径，不默认读取生产配置，不上传数据。

- [ ] **Step 4: 运行分析器与两项修改的定向回归**

Run: `python3 -I tests/test_front_diagnostic_analysis.py && python3 -I tests/test_front_body_deadline.py && python3 -I tests/test_front_request_deadline.py && python3 -I tests/test_xtunnel_log_throttle.py`
Expected: 四个命令均通过。

- [ ] **Step 5: 执行完整离线回归并保留真实结果**

逐一运行所有 `tests/test_*.py`，随后运行：

```bash
python3 -I tests/acceptance_independent_models.py
python3 -I tests/performance_baseline.py --self-test
```

将每个命令的退出码和完整输出保存到 SDD Evidence 目录。预期既有 `test_ipv6_filter.py` 仍可能因加载生产 TLS Relay 文件且配置替身缺字段而失败；不得为本计划扩范围修复，也不得把结果写成全绿。另运行修改文件的 `py_compile` 和 `git diff --check`。

- [ ] **Step 6: 复核差异并提交**

确认差异只包含两个既有 WARNING、必要的状态记录时机、分析器和测试；没有部署、生产请求、重试/deadline/节流键变化或敏感字段。将测试数量、已知失败和未覆盖的 HTTP/2/真实网络边界写入 `docs/2026-11-05-xtunnel-warning-analysis.md`。

```bash
git add tests/analyze_xtunnel_front_diagnostics.py tests/test_front_diagnostic_analysis.py docs/2026-11-05-xtunnel-warning-analysis.md
git commit -m "test: 增加 front 诊断离线关联"
```

## 后续授权边界

完成上述任务只代表开发分支具备可验证的诊断能力和离线证据。若要取得生产答案，必须另行审查并明确授权部署两份运行文件、正常重启及只读观察；届时按 tid 对照系统网络事件，不主动制造故障，不以短时间正常请求宣称治愈。
