# XX-Net 真实工作流收敛与运行 Dashboard 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将真实 `./start`、跨层正常/异常/恢复契约、脱敏运行指标和本地 Dashboard 组成可机检的验收闭环，并持续迭代到已覆盖范围内没有未处理高优先级项。

**Architecture:** 先用验收卡和两段式证据门禁约束结论，再补齐跨层反例。运行侧由各模块提供白名单 snapshot，launcher 做有界、单在途聚合，浏览器只保存当前观察窗口；固定探针结果通过版本化本地文件导入，不新增远程写入或主动探测接口。

**Tech Stack:** Python 3 标准库、unittest、现有 `simple_http_server`、原生 HTML/CSS/JavaScript/SVG、Node.js 纯逻辑测试、本地浏览器验收。

**Spec:** `docs/superpowers/specs/2026-11-09-runtime-convergence-dashboard-design.md`

## Global Constraints

- 沿用当前普通 checkout 和 `fix/h-log-dns-backoff`；不新建 worktree，不执行 commit、push、merge 或 PR。
- 每项产品行为修复先取得真实实现上的准确 RED，再最小修复并验证正常、目标异常、相邻异常、恢复和兼容面；导入、文件缺失和夹具错误不算目标 RED。
- 不读取或输出账号、密码、token、签名 URL、Host、IP、Cookie、Authorization、session_id、响应正文或完整配置。
- 项目和测试 Python 命令使用 `python3 -I`；唯一例外是从中性目录用受信任系统解释器做与真实启动相同的非隔离桌面模块预检，不导入仓库代码。不安装依赖、不上传仓库、不执行真实 iptables、不提权、不输入密码、不修改系统代理。
- 不删除旧日志、备份和既有证据；失败运行、计划变更和裁定全部写入本轮台账。
- 既有 DNS 全调用期限、Task 完成语义、NS single-flight、transfer 身份、quota 回滚和 body queue 背压契约不得为通过测试而放宽。
- 证据等级严格区分 E0/E1/E2/E3；E1 永远标注“无界面服务验收”，E3 未执行时禁止“完整桌面验收通过”。终态区分 `CONVERGED/BLOCKED/FAILED`，安全阻塞不等于成功。
- 本计划不包含提交步骤；每项任务以指定测试和台账记录作为完成边界。

## Review Focus

- 首/中缺口必须实际超过旧 1 MiB 上限，关闭或清空队列不得增加恢复计数；Task 2、3 覆盖。
- 生产校验器必须校验真实证据并向门禁输出绑定哈希的 verdict，不能只跑夹具；Task 1、6 覆盖。
- provider 阻塞、锁竞争、模块重启和迟到响应不能拖死控制面或制造虚假趋势；Task 3、4、5 覆盖。
- 真实 `./start` 可能自动安装依赖；预检失败时完全不得调用入口；Task 6 覆盖。
- 探针额外字段、缺字段或代际不符必须拒绝；无样本、采集失败和短观察窗口不得显示健康；Task 1、5、6 覆盖。

---

### Task 1: 验收证据门禁与两段式生产校验契约

**已完成前置:** CodeGraph 索引、豆包搜索、调研报告及规范工具采用边界均已完成；不得重复创建。主线程官方网页抽查受网络策略阻止，保留该限制。

**Files:**
- Create: `tests/acceptance_gate.py`
- Create: `tests/test_acceptance_gate.py`
- Create: `test-results/2026-11-09-runtime-convergence/evidence-schema.json`

**Interfaces:**
- Produces: `validate_evidence(document: dict, production_verdict: dict | None = None) -> list[str]`；空列表表示允许当前声明，否则每项为固定错误码。
- Produces: CLI `python3 -I tests/acceptance_gate.py EVIDENCE [--production-evidence FILE --production-verdict FILE]`；合法退出 0，非法退出 1，只输出固定错误码。
- Produces: 等级 `E0/E1/E2/E3`、声明 `offline/headless_service/default_startup/full_desktop`、终态 `CONVERGED/BLOCKED/FAILED`。

- [ ] **Step 1: 建立可导入 stub 和目标断言测试**

先创建固定返回空列表的 `validate_evidence` stub，再覆盖：E1 声称 `default_startup`、E3 缺真实菜单/退出事实却声称 `full_desktop`、无样本却记录 100%、未登记 uncovered 却声明 `CONVERGED`、manifest 失败/漏跑/未声明 skip、缺生产 verdict、verdict 与原始证据哈希/代际不符、`would_auto_install=true` 或 `start_invoked=false` 却声明 E2/E3。

- [ ] **Step 2: 运行并确认准确 RED**

Run: `python3 -I tests/test_acceptance_gate.py -v`
Expected: 因目标错误码缺失失败，不是导入错误。

- [ ] **Step 3: 实现最小校验器、schema 和 CLI**

校验必要字段、枚举、声明与等级、uncovered 与终态、样本数与成功率、manifest 汇总和生产 verdict 绑定。BLOCKED 可以终止操作但不能通过 CONVERGED；E3 必须来自实际菜单交互和正常退出事实，等级字段不能自我证明。

- [ ] **Step 4: 验证门禁并记录调研限制**

Run: `python3 -I tests/test_acceptance_gate.py -v`
Expected: 全部通过。台账记录官方来源核验范围、网络限制及“不外发、不安装、不引入外部监控前提”的裁定。

### Task 2: 契约—测试—指标—运行结果双向矩阵

**Files:**
- Create: `test-results/2026-11-09-runtime-convergence/contract-matrix.json`
- Create: `test-results/2026-11-09-runtime-convergence/runtime-metrics-schema.json`
- Create: `tests/test_contract_matrix.py`
- Modify: `tests/test_x_tunnel_memory_backpressure.py`
- Modify: `tests/test_dns_query_history_isolation.py`
- Modify: `tests/test_dns_transport.py`
- Modify: `tests/test_front_request_deadline.py`
- Modify: `tests/test_front_body_deadline.py`
- Modify: `tests/test_smart_router_handoff.py`
- Modify: `tests/test_pipe_socks_condition_race.py`
- Modify: `tests/test_module_lifecycle.py`
- Modify: `tests/test_mac_tray_quit.py`（后八项仅在矩阵暴露对应缺口时修改）

**Interfaces:**
- Produces: 每卡固定 `id/intent/oracles/tests/metrics/production_level/uncovered`；`oracles` 分别包含 `normal/target_exception/adjacent_exception/recovery/stop_or_generation`。
- Produces: 测试引用精确到 `path.py::Class.test_method`；每个 oracle 关联测试节点及最终实际运行结果，指标引用关联 schema 键或明确人工证据。

- [ ] **Step 1: 建立空矩阵 stub 和完整性失败测试**

五组均必须存在，五类 oracle 分别必填或记录固定的不适用理由；AST 验证测试方法真实存在，schema 验证指标键真实存在，结果验证要求对应 verbose 日志中该精确方法为 `ok`。任意存在的同名文件或不相关测试不能闭合矩阵。

- [ ] **Step 2: 运行并确认具体断言 RED**

Run: `python3 -I tests/test_contract_matrix.py -v`
Expected: 空矩阵产生缺失组/缺失 oracle 等目标断言，不是 FileNotFoundError。

- [ ] **Step 3: 建立矩阵并运行现有定向测试**

映射下载、DNS 全期限/代际、HTTP 剩余期限/完整读、连接交接、模块启停和 macOS 退出。既有行为测试标为 characterization；声称新修复必须保留旧实现准确 RED。

- [ ] **Step 4: 补齐量化反例和相邻场景**

首/中/尾缺口分别覆盖；首/中场景在补包前显式断言 `1 MiB < cmd_queue_bytes <= windows_size`，缺失命令在重取期限内到达后按序完整交付，数据发送进度前进且队列释放。另测永久缺口、超过 `windows_size + max_payload`、多 Conn、Session 换代和晚到事件。普通 1 MiB 生产探针不得代替该 E0 向量。若产品失败，暂停 Dashboard，登记 Critical/Important 并返回最小 RED/GREEN。

- [ ] **Step 5: 验证矩阵、精确方法结果和定向套件**

Run: `python3 -I tests/test_contract_matrix.py -v && python3 -I tests/test_x_tunnel_memory_backpressure.py -v && python3 -I tests/test_dns_query_history_isolation.py -v && python3 -I tests/test_dns_transport.py -v && python3 -I tests/test_front_request_deadline.py -v && python3 -I tests/test_front_body_deadline.py -v && python3 -I tests/test_smart_router_handoff.py -v && python3 -I tests/test_pipe_socks_condition_race.py -v && python3 -I tests/test_module_lifecycle.py -v && python3 -I tests/test_mac_tray_quit.py -v`
Expected: 全部通过；任何失败保留原始输出并动态调整计划。

### Task 3: 字段级指标契约与各模块白名单 snapshot

**Files:**
- Create: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/runtime_metrics.py`
- Create: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/metrics.py`
- Create: `Coding/XX-Net-5.16.7.1/code/default/smart_router/local/metrics.py`
- Create: `Coding/XX-Net-5.16.7.1/code/default/gae_proxy/local/metrics.py`
- Create: `tests/test_runtime_metrics.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/base_container.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/proxy_session.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/smart_router/local/dns_query.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/smart_router/local/host_records.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/smart_router/local/pipe_socks.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/front_base/http_dispatcher.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/front_base/connect_manager.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/xlog.py`

**Interfaces:**
- Produces: `CounterSet.increment(name: str, amount: int = 1) -> None`、`CounterSet.snapshot() -> dict`；未知键 `KeyError`，bool/非整数 `TypeError`，非正数 `ValueError`。
- Produces: 各模块 `snapshot() -> dict`，严格匹配 `runtime-metrics-schema.json` 的名称、类型、单位、累计/瞬时和缺失规则。
- Produces: 进程级单调累计计数器；模块重启不清零。瞬时量独立读取，禁止用负增量 Counter 模拟 gauge。

- [ ] **Step 1: 建立可导入指标 stub 和目标失败测试**

先让模块/方法存在但返回固定不完整值，再测试并发增量、非法输入、schema 全字段及敏感键禁止。X-Tunnel 覆盖缺口出现、原缺失命令处理后恢复、数据发送进度、停止原因、队列/窗口比例、SendBuffer、SN 重取、慢/超时 roundtrip 和无 Conn 时 `None`。

- [ ] **Step 2: 运行 RED 后实现最小计数和恢复语义**

Run: `python3 -I tests/test_runtime_metrics.py -v`
Expected: 先因目标字段/语义断言失败，实现后通过。恢复不得由队列从非空到空触发；缺口后超时、do_stop、清空、reset 和换代均须 RED 验证 recovered 不增加，正常补包且恢复处理才增加。

- [ ] **Step 3: 写 DNS、PipeSocks、Front、xlog 和真实锁顺序测试**

覆盖规范目录全部 schema 字段、异常/socket 对象不泄漏、网络锁不可获取时对应 gauge 为 `None`。禁止嵌套持有网络锁和指标锁；锁内只复制少量数值，锁外聚合。用真实产品锁顺序做并发 snapshot/停止测试，不只测 CounterSet 自身。

- [ ] **Step 4: 实现各 snapshot，保持数据路径语义不变**

模块未初始化返回 `not_running`，锁不可用或平台不支持值为 `None`；异常不向外泄露。累计计数跨模块重启保持单调，运行状态变化由页面重置差分基线。不得改变重试、期限、提交资格、路由或关闭决策。

- [ ] **Step 5: 运行 schema、定向与相邻回归**

Run: `python3 -I tests/test_contract_matrix.py -v && python3 -I tests/test_runtime_metrics.py -v && python3 -I tests/test_x_tunnel_memory_backpressure.py -v && python3 -I tests/test_dns_transport.py -v && python3 -I tests/test_domain_records_concurrency.py -v && python3 -I tests/test_pipe_socks_condition_race.py -v && python3 -I tests/test_front_body_deadline.py -v && python3 -I tests/test_xlog_bounded_writer.py -v`
Expected: 全部通过，无新增后台异常。

### Task 4: 有界 launcher 聚合、启动事实与受保护只读 API

**Files:**
- Create: `Coding/XX-Net-5.16.7.1/code/default/launcher/runtime_metrics.py`
- Create: `Coding/XX-Net-5.16.7.1/code/default/launcher/runtime_state.py`
- Create: `tests/test_launcher_runtime_metrics.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/launcher/web_control.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/launcher/start.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/launcher/sys_platform.py`

**Interfaces:**
- Produces: `collect_snapshot(module_providers: dict | None = None) -> dict`，包含 `schema_version=1`、采样时间、进程代际、采集耗时、process/launcher/modules。
- Produces: 每 provider 单在途执行和 0.25 秒聚合等待上限；阻塞或超时只返回固定 `unknown`，已阻塞 provider 不重复提交新线程。
- Produces: 受现有控制面认证的 GET `/runtime_metrics`；GET `/dashboard.html` 复用静态路径。

- [ ] **Step 1: 建立可导入 stub 和聚合/启动事实失败测试**

覆盖单模块抛错、Barrier 阻塞 provider 的有界返回和无线程堆积、未启动、采集不调用网络、进程代际、argv 分类、页面请求结果、托盘/fallback 固定枚举。Darwin 真实进程必须取得非空线程数、RSS、CPU、fd 数和 fd 软限制；fd 可用标准库 `ctypes` 调用 `proc_pidinfo`，不得启动 `lsof`。

- [ ] **Step 2: 运行并确认目标断言 RED**

Run: `python3 -I tests/test_launcher_runtime_metrics.py -v`
Expected: 因目标字段、超时或资源语义失败，不是导入错误。

- [ ] **Step 3: 实现聚合和启动状态记录**

代际使用随机非语义 ID、PID 和启动时间。`webbrowser.open()` 只记录请求结果；托盘只记录 backend、serve 开始和返回。快照不得包含完整 argv、cwd、可执行路径。并发停止/重启期间不得出现负值或迭代崩溃；累计计数保持进程级单调，模块状态转换交给 UI 分段。

- [ ] **Step 4: 接入 `/runtime_metrics` 并验证访问边界**

路由必须位于现有 Referer 和 Basic Auth 检查之后，并额外限制 `client_address` 为 loopback；返回 JSON 和 `Cache-Control: no-store`，不新增 CORS，不改变监听地址。启用认证时未授权请求返回 401，非本地请求返回 403，均不返回指标。单模块失败只含固定模块名和 `error`，不返回异常文本。

- [ ] **Step 5: 验证 API 和 launcher 回归**

Run: `python3 -I tests/test_launcher_runtime_metrics.py -v && python3 -I tests/test_module_lifecycle.py -v && python3 -I tests/test_launcher_logging_shutdown.py -v && python3 -I tests/test_mac_tray_quit.py -v`
Expected: 全部通过。

### Task 5: 本地 Dashboard、单在途采样与版本化探针导入

**Files:**
- Create: `Coding/XX-Net-5.16.7.1/code/default/launcher/web_ui/dashboard.html`
- Create: `Coding/XX-Net-5.16.7.1/code/default/launcher/web_ui/css/dashboard.css`
- Create: `Coding/XX-Net-5.16.7.1/code/default/launcher/web_ui/js/dashboard.js`
- Create: `tests/dashboard_state_test.js`
- Create: `tests/test_dashboard_static.py`

**Interfaces:**
- Produces: `normalizeSnapshot(value)`、`appendSample(samples, value, limit=360)`、`summarizeProbes(document, currentGeneration)`；无效输入抛固定错误。
- Produces: 探针 schema v1，仅含进程代际、固定类别、状态、尝试/完整成功/失败分类、预期/实际字节、时延和可选内容 SHA256；额外或缺失字段拒绝。

- [ ] **Step 1: 建立导出 stub 和页面状态失败测试**

覆盖无样本 `NO_SAMPLE`、缺失 `null`、360 条封顶、进程换代和模块 running/not_running 转换分段、累计差分基线重置、未知状态显示、探针 schema/代际拒绝、0 次尝试不生成 100%。轮询必须单在途、完成后计时、支持 abort、拒绝迟到响应，失败时当前样本 stale/unknown。

- [ ] **Step 2: 运行目标 RED**

Run: `node tests/dashboard_state_test.js`
Expected: stub 返回值违反目标断言，不是模块装载错误。

- [ ] **Step 3: 实现状态层和六区静态页面**

实现总览、用户链路、跨层压力、资源与生命周期、异常与平衡点、证据边界。最多 360 个有效样本，实际窗口按首尾有效采样时间显示；刷新开始新窗口。无外部字体、CDN 或监控服务。

- [ ] **Step 4: 执行图表规范和可访问性检查**

先确认已加载 dataviz skill 的脚本实际存在，再用该根目录运行 light/dark `validate_palette.js`；不得硬编码临时版本路径。工具不可用记为 BLOCKED，不安装替代。分类色保持 `#2a78d6,#eb6834,#1baf7a` 与 `#3987e5,#d95926,#199e70`；折线 2px、单 Y 轴、图例、crosshair/tooltip、表格视图、状态图标+文字，不使用双 Y 轴。

- [ ] **Step 5: 静态安全扫描与逻辑回归**

Run: `python3 -I tests/test_dashboard_static.py -v && python3 -I tests/test_launcher_runtime_metrics.py -v && node tests/dashboard_state_test.js`
Expected: 全部通过；HTML 不含远程资源、内联敏感数据或写接口。

### Task 6: 部署器自测、真实 `./start`、故障注入与观察窗口

**Files:**
- Create: `test-results/2026-11-09-runtime-convergence/deploy.py`
- Create: `test-results/2026-11-09-runtime-convergence/production_probe.py`
- Create: `test-results/2026-11-09-runtime-convergence/validate_production.py`
- Create: `test-results/2026-11-09-runtime-convergence/evidence.json`
- Create: `test-results/2026-11-09-runtime-convergence/production-evidence.json`
- Create: `test-results/2026-11-09-runtime-convergence/production-verdict.json`

**Interfaces:**
- Produces: `validate_production.py PRODUCTION_EVIDENCE --out PRODUCTION_VERDICT`，合法退出 0、非法退出 1；verdict 绑定原始证据 SHA256、代码哈希、代码根、代际和等级事实。
- Produces: E2 事实记录；E3 只能来自真实菜单交互和正常退出观察。故障证据分别记录 E0/E1/E2、实例、代码哈希和代际。

- [ ] **Step 1: 先验证验收工具与部署器反例**

对生产校验器输入 E1 argv、缺页面/托盘事实、错误代码根/代际、无样本、失败探针、Dashboard 异常、不足 60 秒或少于 6 个有效样本，必须分别失败；完整 E2 夹具才通过。部署器在临时目录测试旧哈希不符、备份失败、部分写入、异常退出、非预期路径/链接和恢复路径；未通过不得停止真实旧代。

- [ ] **Step 2: 完整离线验收并冻结最终哈希**

使用既有 manifest 驱动器执行全部项目测试和场景；verbose 日志必须能映射矩阵精确方法。记录 0 失败、0 未声明 skip、0 timeout、0 重试和最终代码指纹。任何失败返回对应 Task。

- [ ] **Step 3: 在停止旧代之前做真实入口等价预检**

绑定最终部署哈希，按 Darwin 脚本实际规则选择解释器，在中性目录以相同非隔离 user-site 可见性检查 `PyObjCTools/AppKit/SystemConfiguration/PIL`，并判断 pip、解包、提权或系统设置分支。任一禁止副作用即将发生时写 `BLOCKED / START_NOT_INVOKED / AUTO_INSTALL_REQUIRED`，不得调用 `./start`。增加反例证明预检失败时入口完全未执行；禁止用 `-no_popup/-no_systray` 绕过。

- [ ] **Step 4: 原子部署并运行真实默认入口**

仅在预检通过后停止旧代，校验生产旧哈希并独立备份；运行 `/Applications/XX-Net/start`，不追加降能参数。记录 argv、cwd、解释器、代码根、配置来源类别、PID 和代际。密码或系统设置要求出现时不授权，标记 BLOCKED 并安全停止本轮启动。

- [ ] **Step 5: 驱动真实页面、API 和代理链路**

打开 `http://127.0.0.1:8085/dashboard.html`，确认六区、真实 snapshot、无控制台错误、图表/表格和单在途更新；固定控制页、HTTP、HTTPS、SOCKS5 域名探针全部通过，已知 1 MiB Range 直连/代理各至少 16 次完整读。URL 只在内存中使用，输出仅保留 schema 白名单字段。

- [ ] **Step 6: 受控异常注入和最短观察窗口**

在 E0/E1 或独立测试实例注入合法乱序、永久缺口、慢响应、DNS 空结果和采集器异常，确认指标与页面区分恢复、受控关闭、无样本和未知。每条证据绑定来源实例和代际，不能补足 E2 故障证据。真实观察实际起止至少 60 秒且至少 6 个有效样本，缺口单独登记；不自动点击真实托盘菜单时 E3 保持未覆盖。

- [ ] **Step 7: 对真实证据执行固定两段式门禁**

Run: `python3 -I test-results/2026-11-09-runtime-convergence/validate_production.py test-results/2026-11-09-runtime-convergence/production-evidence.json --out test-results/2026-11-09-runtime-convergence/production-verdict.json`

Run: `python3 -I tests/acceptance_gate.py test-results/2026-11-09-runtime-convergence/evidence.json --production-evidence test-results/2026-11-09-runtime-convergence/production-evidence.json --production-verdict test-results/2026-11-09-runtime-convergence/production-verdict.json`
Expected: 两个真实文件均被校验，合法退出 0；任一失败退出 1，禁止以夹具或自述等级代替。

### Task 7: 独立复核、完整回归与动态终态

**Files:**
- Create: `test-results/2026-11-09-runtime-convergence/independent-review.md`
- Create: `test-results/2026-11-09-runtime-convergence/final-report.md`
- Create: `test-results/2026-11-09-runtime-convergence/ledger.md`
- Modify: 本计划及 matrix/evidence（仅新证据要求时）

**Interfaces:**
- Consumes: 所有最终代码、测试、页面、真实生产 verdict 和台账。
- Produces: 分级发现、证据外推检查、未覆盖项及 `CONVERGED/BLOCKED/FAILED` 的允许措辞。

- [ ] **Step 1: 派未参与设计者审查最终证据**

重点寻找未进入矩阵的正常行为、跨层参数冲突、锁内采集、provider 阻塞、隐私泄漏、恢复误报、模块重启趋势、E1/E2 越级、E3 过度声明和“没有样本=健康”。审查报告只是候选，主线程逐项核实。

- [ ] **Step 2: 对确认的 Critical/Important 返回 RED/GREEN**

每个确认发现先写真实失败测试或生产反例，做最小修复，再跑相邻与完整套件；不得删指标、放宽断言、隐藏失败或仅修改措辞绕过。

- [ ] **Step 3: 重跑完整离线、两段式门禁和最终生产核验**

最终代码指纹与部署一致；核对真实代际、默认入口、Dashboard 实际渲染、固定探针、采集健康和脱敏结果。漂移或失败时停止结论并按已自测的恢复流程处理、验证。

- [ ] **Step 4: 检查收敛门槛并选择真实终态**

七项全部满足才是 `CONVERGED`；安全边界禁止执行为 `BLOCKED`，真实反例为 `FAILED`。最终只有 CONVERGED 可声明“已覆盖模型和已执行真实工作流内，没有未处理高优先级项”；E3 人工交互、外部平台和长期趋势按实际单列。
