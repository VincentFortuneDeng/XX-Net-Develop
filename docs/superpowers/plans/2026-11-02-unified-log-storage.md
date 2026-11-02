# XX-Net 统一日志存储与错误上下文 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将七个现有持久 Logger 收敛为单份有界权威正文，并为 ERROR 提供有限现场、按需视图和有序诊断包。

**Architecture:** 保留 `xlog` 公共调用与内存 JSON 接口，在 `Logger` 内用同一把锁统一内存、正文滚动和上下文快照。各模块通过现有 `/log` 接口导出自己的启动/warning 视图，X-Tunnel 诊断打包器只通过本机 HTTP 收集，不跨进程读取内存。

**Tech Stack:** Python 3、现有 xlog/simple_http_server、zipfile、仓库独立测试脚本。

**Spec:** `docs/superpowers/specs/2026-11-01-unified-log-storage-design.md`

## Global Constraints

- 主文件每模块 10MiB，只留一个 `.1`；单行最多 64KiB；ERROR 上下文最多 40 条/16KiB，同模板冷却 60 秒。
- ERROR/CRITICAL 正文和 exception 堆栈永不因上下文冷却或普通日志节流而省略。
- 保持现有 `debug/info/warn/warning/error/exception/critical` 调用与 `get_last_lines/get_new_lines` JSON 格式兼容。
- 不新增后台线程，不把锁扩展到网络、Queue 或业务生命周期操作。
- 旧 `start_log_*`、`*_warning.log*` 只读保留，不自动删除；常态最终不双写正文。
- 不部署、不重启、不 push；生产核验须另行批准部署并由用户重启。
- 所有新行为先 RED 后 GREEN；提交信息末尾使用 `Co-Authored-By: Kimi <noreply@moonshot.cn>`。

## Review Focus

- 多行 traceback/上下文与滚动同时发生：块必须完整且 ERROR 不丢失；Task 2 用多行条目、并发和小文件上限测试钉住。
- 视图遇到非 UTF-8、残缺尾部或无效导出参数：不得让诊断打包失败；Task 3 用损坏文件和无效参数测试钉住。
- 小诊断预算与高优先级视图冲突：高优先级条目必须保留且不能输出伪完整文件；Task 4 用小预算和不可整放文件测试钉住。
- 禁用模块或导出端点超时：不得重新产生 404 轮询或阻塞打包；Task 4 用模块开关和请求异常测试钉住。
- 启动详细期结束后模块再次 start/stop：关键生命周期仍应可见，普通 per-request INFO 不得落盘；Task 5 用精确调用点契约测试钉住。

---

### Task 1: 统一权威正文与启动持久化策略

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/xlog.py`
- Modify: `Coding/XX-Net-5.16.7.1/test_xlog_roll_f.py`
- Create: `tests/test_unified_log_storage.py`

**Interfaces:**
- Consumes: 现有 `Logger(..., log_path, save_start_log, save_warning_log)` 与 `buffer_lock`。
- Produces: `<name>.log`/`.1`；`Logger.export_view()` 后续任务读取的 `boot_started_at`、`startup_verbose_ended_at`；`debug/info(..., persist=True)`；`save_persistent_log` 兼容别名。

- [ ] **Step 1: 写统一存储失败测试并迁移旧滚动断言**

新脚本覆盖 U1–U8：启动预算内所有级别写入 `<name>.log`；预算后普通 DEBUG/INFO 仅内存可见；`persist=True` 落盘；WARNING 以上持续落盘；reset 不删除正文且内存编号连续；keep/full_log 仍滚动；非 ASCII 按字节滚动；超过 64KiB 或测试文件上限的单行保留首尾并标记中部省略。将旧 A1–A4 的文件名与大小属性改为统一正文契约，其余 34 项断言语义不变。

- [ ] **Step 2: 运行并确认目标 RED**

Run: `python3 tests/test_unified_log_storage.py; python3 Coding/XX-Net-5.16.7.1/test_xlog_roll_f.py`
Expected: 新脚本因统一文件/持久化接口不存在而失败；旧 A1–A4 因仍写 `mod_warning.log` 失败，其余既有项不因测试夹具产生无关异常。

- [ ] **Step 3: 实现最小统一正文**

在 `xlog.py` 增加独立 `persistent_fd`，不占用可选 `file_name` 使用的 `log_fd`。`save_warning_log=True` 与新增 `save_persistent_log=True` 均打开 `<name>.log`；保留现有启动行数预算，预算结束后仅 WARNING 以上或 `persist=True` 写正文。reset 只 flush/重开，不删除正文、不重置内存编号；keep/full_log 只延长详细期。实现 UTF-8 字节尺寸、写前单代滚动及 64KiB 中省略，滚动/打开失败仍不得递归写日志。

- [ ] **Step 4: 运行并确认 GREEN**

Run: `python3 tests/test_unified_log_storage.py && python3 Coding/XX-Net-5.16.7.1/test_xlog_roll_f.py`
Expected: 新 U1–U8 与旧 38/38 全部通过，无 `.2`、无递归异常。

- [ ] **Step 5: 提交 Task 1**

提交生产文件与两份测试，信息为 `feat: O 阶段统一日志权威正文`。

### Task 2: ERROR 有界上下文与冷却

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/xlog.py`
- Test: `tests/test_unified_log_storage.py`

**Interfaces:**
- Consumes: Task 1 的 `persistent_fd`、统一写单元和既有内存 `buffer`。
- Produces: ERROR 级 `ERROR_CONTEXT_BEGIN/END` 完整块；同 `(logger.name, fmt)` 60 秒上下文冷却。

- [ ] **Step 1: 写上下文失败测试**

新增 C1–C9：ERROR 前最近 DEBUG/INFO 被带出，WARNING 不作为普通上下文；最多 40 条/16KiB 且完整 UTF-8；第二条同模板 ERROR 正文仍写但上下文冷却；不同模板独立；一次 exception 仅一个上下文块和一份堆栈；CRITICAL 等价；多行 traceback 条目不截断；并发块不交错且内存编号连续；小文件上限下整块滚动且 ERROR 本体存在。

- [ ] **Step 2: 运行并确认目标 RED**

Run: `python3 tests/test_unified_log_storage.py`
Expected: U1–U8 继续通过，C1 因没有 ERROR_CONTEXT 首先失败，不出现文件描述符异常。

- [ ] **Step 3: 实现单缓冲上下文快照**

在 `buffer_lock` 内、ERROR 自身入-buffer 前筛选既有 DEBUG/INFO；只写正文、不回填内存。按原始 fmt 冷却，exception 的第二段堆栈禁止再次捕获；整块有界写入并保持失败安全，不新增线程或递归 xlog 调用。

- [ ] **Step 4: 运行并确认 GREEN**

Run: `python3 tests/test_unified_log_storage.py && python3 Coding/XX-Net-5.16.7.1/test_xlog_roll_f.py`
Expected: U/C 与旧 38/38 全部通过，重复 ERROR 本体数量不被冷却改变。

- [ ] **Step 5: 提交 Task 2**

提交信息为 `feat: O 阶段补充有界错误上下文`。

### Task 3: 启动/warning 视图与模块导出接口

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/xlog.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/launcher/web_control.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/gae_proxy/local/web_control.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/smart_router/local/web_control.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/web_control.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/cloudflare_front/web_control.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/seley_front/web_control.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/tls_relay_front/web_control.py`
- Create: `tests/test_log_view_export.py`

**Interfaces:**
- Consumes: Task 1 时间边界和 Task 2 上下文标记。
- Produces: `Logger.export_view(view: str, max_bytes: int) -> str`；现有 `/log?cmd=export&view=startup|warning&max_bytes=N` 文本响应。

- [ ] **Step 1: 写视图与七模块接线失败测试**

覆盖 V1–V9：startup 仅本次详细期；warning 含 WARNING 以上及完整 ERROR_CONTEXT；视图超限时保留最新完整记录；启动/warning 默认 Web 上限分别 256/512KiB；非法 view、非整数或非正 max_bytes 返回空文本并记录 WARN；`.1` 与主文件顺序正确；非 UTF-8/残缺行跳过不中断；无持久文件返回空串。用轻量源契约断言七个现有 control `/log` 都接 `cmd=export` 并调用本模块 Logger，原有 get_last/get_new 分支不变。

- [ ] **Step 2: 运行并确认目标 RED**

Run: `python3 tests/test_log_view_export.py`
Expected: 因 `export_view` 不存在及七个 handler 未接线而失败。

- [ ] **Step 3: 实现视图和最小 Web 分支**

`export_view` 只接受 startup/warning，读取本 Logger 的 `.1` 与主文件，按完整记录和字节上限过滤；warning 用状态保留整个上下文块。各 handler 在现有 `req_log_handler` 增加 export 分支，成功返回 `text/plain`，无效参数返回空文本；不得改变内存 JSON 编号或新增跨进程读取。

- [ ] **Step 4: 运行并确认 GREEN**

Run: `python3 tests/test_log_view_export.py && python3 tests/test_unified_log_storage.py && python3 Coding/XX-Net-5.16.7.1/test_xlog_roll_f.py`
Expected: V/U/C 与旧 38/38 全部通过。

- [ ] **Step 5: 提交 Task 3**

提交信息为 `feat: O 阶段增加日志按需视图`。

### Task 4: 有优先级且不重复的诊断包

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/upload_logs.py`
- Create: `tests/test_log_diagnostic_pack.py`

**Interfaces:**
- Consumes: Task 3 的本机 export URL；现有模块启用过滤与 `simple_http_client`。
- Produces: `collect_log_views() -> dict[str, bytes]`；`pack_logs(max_size=10*1024*1024, include_legacy=False)`；`views/<name>_startup.log` 与 `views/<name>_warning.log` 条目。

- [ ] **Step 1: 写诊断包失败测试**

用临时 data 目录和 stub HTTP 覆盖 P1–P10：各启用模块请求自己的 startup/warning 视图；禁用模块零请求；超时仅 WARN 并省略该视图；优先级为状态/配置→内存快照→视图→主正文→`.1`→显式 legacy；默认无旧 start/warning；`include_legacy=True` 最后加入；10MiB 或测试小预算不被突破；不可整放的大文件跳过并继续较小条目；client.json 密码仍脱敏；zip 中无默认重复旧正文。

- [ ] **Step 2: 运行并确认目标 RED**

Run: `python3 tests/test_log_diagnostic_pack.py`
Expected: 因 `collect_log_views`、include_legacy 和优先级逻辑不存在而失败，测试不访问真实 8085。

- [ ] **Step 3: 实现视图收集与预算装包**

复用 `get_module_enable_flags()` 的 fail-safe 与现有 control URL 映射；startup/warning 分别请求 256/512KiB。将候选项显式分为六级，按原始未压缩尺寸计入预算，大文件跳过继续；legacy 仅显式开启。保持 `collect_debug_and_log()` 的当前内存快照和脱敏行为，不引入常驻 warning 副本。

- [ ] **Step 4: 运行并确认 GREEN**

Run: `python3 tests/test_log_diagnostic_pack.py && python3 tests/test_log_view_export.py && python3 tests/test_unified_log_storage.py`
Expected: P/V/U/C 全部通过，zip 条目与预算断言一致。

- [ ] **Step 5: 提交 Task 4**

提交信息为 `feat: O 阶段整理诊断日志包优先级`。

### Task 5: 生命周期持久化、完整回归与文档

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/launcher/module_init.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/launcher/start.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/smart_router/local/__init__.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/client.py`
- Modify: `Coding/XX-Net-5.16.7.1/LOCAL-VERSION.md`
- Modify: `docs/XX-Net-macOS-ARM64-修复记录.md`
- Modify: `docs/XX-Net-2026-10-26-change-summary.html`
- Create: `tests/test_persistent_lifecycle_logs.py`

**Interfaces:**
- Consumes: Task 1 `info(..., persist=True)`，前四任务全部测试。
- Produces: 仅指定低频生命周期 INFO 持久化；O 阶段开发与验证记录。

- [ ] **Step 1: 写精确生命周期调用点失败测试**

断言只有以下既有 INFO 增加 `persist=True`：module_init 的 module started/stopped/start time cost；launcher 的 Stopping all modules before exit；Smart Router 的 Proxy server listen；X-Tunnel 的 Socks5 listen/Close Socks5 server/Stopping session。同时检查相邻 per-request INFO 未被批量标记，消息模板与业务控制流不变。

- [ ] **Step 2: 运行并确认目标 RED**

Run: `python3 tests/test_persistent_lifecycle_logs.py`
Expected: 八个指定调用点缺少 persist=True，无其他失败。

- [ ] **Step 3: 最小标注并运行全部新旧回归**

只修改八个调用的关键字。运行：

```bash
python3 tests/test_persistent_lifecycle_logs.py
python3 tests/test_log_diagnostic_pack.py
python3 tests/test_log_view_export.py
python3 tests/test_unified_log_storage.py
python3 Coding/XX-Net-5.16.7.1/test_xlog_roll_f.py
python3 tests/test_smart_router_startup_test_isolation.py
python3 tests/test_smart_router_startup_race.py
python3 tests/test_retry_log_throttle.py
python3 tests/test_xtunnel_log_throttle.py
python3 tests/test_mark_sn_timeout_throttle.py
python3 tests/test_dns_ns_backoff.py
python3 tests/test_dns_dot.py
python3 tests/test_try_add_remove_races.py
python3 tests/test_ipv6_filter.py
python3 tests/test_blockio_ratelimit.py
python3 tests/test_dns_nameserver_filter.py
python3 tests/probe_wrap_socket_fd.py
```

Expected: 全部 PASS，fd 探针保持 A(detached)。

- [ ] **Step 4: 静态检查、独立全差异审查与文档**

对全部修改的 Python 文件执行 py_compile、`git diff --check`；HTMLParser 检查 23 张 change 卡、ID 唯一与页内锚点。派独立 reviewer 审查 Task 1–5 全差异，重点为 ERROR 永不丢失、锁范围、UTF-8/滚动、跨进程边界和包预算；Critical/Important 一轮 RED→GREEN 修复后重跑完整回归。文档记录实际测试数、审查结果和“开发完成未部署”，不得写成生产已生效。

- [ ] **Step 5: 提交代码与记录**

先提交生命周期代码和契约测试，信息为 `feat: O 阶段保留关键生命周期日志`；审查与文档完成后提交 `docs: 记录 O 阶段统一日志验证`。保持未部署、未重启、未 push。
