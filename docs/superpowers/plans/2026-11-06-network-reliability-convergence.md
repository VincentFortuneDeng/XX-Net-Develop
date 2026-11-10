# 网络可靠性统一修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复已复现的 DNS 查询串号、front 响应完整性与重试竞态、X-Tunnel 跨代污染和锁顺序反转，并用离线回归证明本轮缺陷收敛。

**Architecture:** 以对象所有权和单一状态锁为主线：DNS 原子登记 waiter，front 用 Task 状态机与协议完整性规则约束 worker，X-Tunnel 用会话代际和 Conn/线程身份约束异步交付。每个缺陷先取得准确 RED，再做最小 GREEN；不重构无关模块。

**Tech Stack:** Python 3 兼容项目现有 Python 代码、标准库 `threading`/`socket`/`selectors`、仓库现有 `unittest` 与可控测试替身。

**Spec:** `docs/superpowers/specs/2026-11-06-network-reliability-convergence-design.md`

## Global Constraints

- 始终使用简体中文说明和注释；代码标识符保持英文。
- 从仓库根目录运行测试，使用 `python3 -I`；测试直接加载 `Coding/XX-Net-5.16.7.1` 开发快照。
- 所有生产代码修改之前必须先有针对该行为的新增测试，并观察到预期 RED；不得先改代码再补测试。
- 不放大超时、不隐藏日志、不放宽断言；合法协议正例和下一 attempt 的重试必须保留。
- 不部署、不重启、不注入生产故障、不执行真实 iptables；生产仅只读。
- 不删除旧日志、备份或其他计划证据目录；日志中的 session_id 必须脱敏。
- 不执行 git commit、push、合并 main 或创建 PR；计划因此不包含提交步骤。
- 当前普通 checkout，分支 `fix/h-log-dns-backoff`，直接在现有分支工作，不新建 worktree。

## Review Focus

- 非标准或不完整 HTTP 响应：零预算、非法长度、提前 EOF 不得被标完整；由 Task 2/3 的正反例覆盖。
- HTTP/2 多 stream 不同期限：过期 stream 被取消，正常 stream 和共享连接继续工作；由 Task 4 覆盖。
- retry 接受交接：worker 接受后立即失败、拒绝接受、下一 attempt 再次失败均不得丢重试或重复排队；由 Task 4 覆盖。
- DNS 大小写、尾点、CNAME 与 NS：匹配问题区而不是任意答案名，合法答案不得被过严校验误伤；由 Task 1 覆盖。
- X-Tunnel 同 ID 新对象和锁外交错：旧事件、旧 finally 和注册失败不得清理新对象或死锁；由 Task 5 覆盖。

---

### Task 1: DNS 查询所有权与响应匹配

**Files:**
- Create: `tests/test_dns_response_ownership.py`
- Modify: `tests/test_dns_transport.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/smart_router/local/dns_query.py`

**Interfaces:**
- Produces: UDP/TCP/DoH 共用的响应匹配逻辑；活跃 waiter 为普通 dict，配 `waiters_lock`，清理按 waiter 对象身份执行。

- [ ] **Step 1: 写并发所有权和响应匹配失败测试**

在 `test_dns_response_ownership.py` 覆盖：并发同 ID 不覆盖；旧 finally 不删除同 ID 新 waiter；接收与超时只有一个赢家；错误 QR/ID/qname/qtype 不交付；qname 大小写和尾点等价；A/AAAA 只收对应 RR，CNAME 递归和 NS 正例保留；UDP 等待使用剩余期限；TCP/DoH 不匹配响应关闭且不回池。修正既有 DNS fixture 回显真实请求 ID，并为 UDP 替身补专用锁。

- [ ] **Step 2: 运行测试并确认准确 RED**

Run: `python3 -I tests/test_dns_response_ownership.py && python3 -I tests/test_dns_transport.py`
Expected: 新测试因原子登记、身份清理或响应匹配缺失而失败；若出现替身错误，先修测试直至失败原因准确。

- [ ] **Step 3: 最小实现 DNS 所有权与匹配**

用普通 dict 和专用锁保护选 ID+登记；条件 pop 按对象身份。统一校验 QR、ID、唯一 question、规范化 qname/qtype 和答案类型，UDP 用剩余时间等待；TCP/DoH 校验失败关闭资源，仅合法成功回池。

- [ ] **Step 4: 验证 DNS 关联套件**

Run: `python3 -I tests/test_dns_response_ownership.py && python3 -I tests/test_dns_transport.py && python3 -I tests/test_dns_query_history_isolation.py && python3 -I tests/test_domain_records_concurrency.py`
Expected: 全部退出 0，无异常输出。

### Task 2: HTTP 底层读取与 Task 原子状态

**Files:**
- Modify: `tests/test_front_body_deadline.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/simple_http_client.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/front_base/http_common.py`

**Interfaces:**
- Produces: `response_has_body(method, status)` 最终响应判定；Response 的粘滞 `eof` 与 `chunked_complete`；Task 的 `claim_retry()`、`clear_retry()` 和原子一次性 `finish()`。

- [ ] **Step 1: 写底层读取和 Task 状态失败测试**

覆盖：begin 不追加 0.1 秒且缓冲完整时零预算可解析；零预算空读取不设置 EOF；真实 socket EOF 粘滞；chunked trailer 逐行消费；混合 read/read_all 的 `body_readed == 5`；并发 finish 只有一个终止哨兵且迟到 complete 不翻转状态；retry claim 去重和 clear 后可再次 claim。

- [ ] **Step 2: 运行并确认 RED**

Run: `python3 -I tests/test_front_body_deadline.py`
Expected: 新增用例因最低 0.1 秒、trailer 残留、重复计数、非原子 finish 或缺少 retry 接口而失败。

- [ ] **Step 3: 最小实现底层协议状态与 Task 状态机**

Response 记录真实 EOF，零 chunk 后消费 trailer 至空行并记录完成；读取长度判断覆盖缓冲恰好足够。Task 使用可重入状态锁保护响应/终止/retry 状态，finish 只投递一个哨兵；read_all 只补计直接消费的既有缓冲，不重复累计 `read()` 已计数字节。

- [ ] **Step 4: 验证底层与既有 front 测试**

Run: `python3 -I tests/test_front_body_deadline.py && python3 -I tests/test_front_request_deadline.py`
Expected: 全部退出 0；既有正常完整响应测试保持通过。

### Task 3: HTTP/1、HTTP/2 与上层响应完整性

**Files:**
- Modify: `tests/test_front_body_deadline.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/front_base/http1.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/front_base/http2_stream.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/front_dispatcher.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/simple_http_client.py`

**Interfaces:**
- Consumes: Task 2 的 `response_has_body()`、EOF/chunked 状态和 Task 原子 finish。
- Produces: H1 四类正文处理；H2 END_STREAM 长度核对；上层仅对有正文语义比较 Content-Length。

- [ ] **Step 1: 写协议完整性和无正文失败测试**

H1 覆盖：无长度零预算不完整、真实 EOF 完整且关闭不复用；固定长度短缺/恰好/超长；chunked 分片、截断、零预算和 trailer；HEAD/204/304 不读正文；100 后继续读最终响应，101 不作为普通 keep-alive 空响应；发送阻塞遵守任务期限。H2 覆盖短正文/恰好/无长度 END_STREAM、无正文和非法 DATA。上层覆盖 HEAD/304 携带实体长度不误判截断。独立 client 的 HEAD 不等待声明正文。

- [ ] **Step 2: 运行并确认 RED**

Run: `python3 -I tests/test_front_body_deadline.py`
Expected: 新增用例分别因误标完整、复用 EOF 连接、长度多读、无正文误读或上层误判而失败；旧“任意 END_STREAM 完整”断言按新契约改为长度敏感，不能保留错误期望。

- [ ] **Step 3: 最小实现协议完整性**

H1 发送和读取均使用剩余绝对期限；按四类正文处理，固定长度只读剩余量，EOF 定界完成后关闭。H2 以无正文语义或 `body_len == content_length` 判定完整，非法正文走失败收尾。上层只在有正文语义时比较长度；独立 client 使用同一无正文规则。

- [ ] **Step 4: 验证 front 关联套件**

Run: `python3 -I tests/test_front_body_deadline.py && python3 -I tests/test_front_request_deadline.py && python3 -I tests/test_front_dispatcher_wait_isolation.py && python3 -I tests/test_front_diagnostic_analysis.py`
Expected: 全部退出 0。

### Task 4: HTTP/2 生命周期、重试交接与 per-stream 期限

**Files:**
- Modify: `tests/test_front_body_deadline.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/front_base/http2_connection.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/front_base/http2_stream.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/front_base/http1.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/front_base/http_dispatcher.py`

**Interfaces:**
- Consumes: Task 2 的 `claim_retry()/clear_retry()`；Task 3 的 H2 完整性规则。
- Produces: H2 专用状态锁保护 request/close；worker 接受任务时清除 retry claim；幂等 Stream.close 和真实删除计数；连接检查器调用 stream 超时。

- [ ] **Step 1: 写生命周期和重试交错失败测试**

覆盖 request 检查后 close 的交错、并发 close、两个 stream 同 Task 仅一个待重试、worker 拒绝和接受后立即失败不丢重试、下一 attempt 可合法重试、finish/close 幂等、KeyError 不增加 processed_tasks。连接检查器实际取消到期 stream 并注销；两个不同期限 stream 互不关闭；发送队列不发送已过期 stream 的帧，共享 socket 不被设置单 Task 期限。

- [ ] **Step 2: 运行并确认 RED**

Run: `python3 -I tests/test_front_body_deadline.py`
Expected: 新用例因关闭后登记、重复 retry、错误计数、无超时调用或过期帧发送而失败。

- [ ] **Step 3: 最小实现生命周期与交接**

H2 使用专用可重入状态锁，仅保护 running 检查、登记、close 状态翻转和 stream 快照；回调、retry/finish 在锁外。H1/H2 在接受任务的临界区清除 retry pending，dispatcher 不在出队时清除。Stream.close 幂等，删除成功才计数；检查器执行 per-stream 超时，发送队列过滤过期 stream 帧而不关闭其他 stream。

- [ ] **Step 4: 验证全部 front 关联测试并重复竞态**

Run: `python3 -I tests/test_front_body_deadline.py && python3 -I tests/test_front_request_deadline.py && python3 -I tests/test_front_dispatcher_wait_isolation.py`
Expected: 全部退出 0；随后关键 request/close、retry 和 stream timeout 用例连续 10 轮无失败。

### Task 5: X-Tunnel 代际、Conn 所有权与线程交接

**Files:**
- Modify: `tests/test_x_tunnel_memory_backpressure.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/proxy_session.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/base_container.py`

**Interfaces:**
- Produces: 条件 stop/reset；`send_conn_data(..., expected_conn=...)` 式身份校验；按 Conn 条件摘除 selector；worker/checker 线程身份清理。

- [ ] **Step 1: 写跨代与锁顺序失败测试**

覆盖旧上传不修改新 transfer/on_road 且不拼接新 session_id；旧 521/error 1/error 3 不 stop/reset/login 新会话；旧 checker 不 reset；旧 selector READ/WRITE、data/ACK/peer-close 和 max-connection reset 不交付或影响新对象；注册异常锁外清理且不误删新注册；roundtrip/pipe worker 与 checker 的旧 finally 不删除新线程，start 交错后当前代际每槽位有接替者。保留正常 200、新 Conn、新 worker 正例。

- [ ] **Step 2: 运行并确认 RED**

Run: `python3 -I tests/test_x_tunnel_memory_backpressure.py && python3 -I tests/test_xtunnel_log_throttle.py && python3 -I tests/test_pipe_socks_condition_race.py`
Expected: 新增用例因跨代修改、旧事件交付、双向等待或线程槽位误删而失败；替身缺字段先修测试，不能误记为产品 RED。

- [ ] **Step 3: 最小实现代际与身份保护**

上传使用捕获的会话编号，锁内同时核对代际和 transfer 身份，on_road 仅当前代际增减并在换代时重置。stop/reset 在锁内验证预期代际/Conn；旧 521 的全局清理、stop 和 login 受同一条件保护。Conn 所有交付携带自身引用；selector 和关闭按身份复核；pipe 注册异常退出锁后清理；worker/checker 按线程身份退出和删除，start 建立当前代际接替者。

- [ ] **Step 4: 验证 X-Tunnel 关联套件并重复竞态**

Run: `python3 -I tests/test_x_tunnel_memory_backpressure.py && python3 -I tests/test_xtunnel_log_throttle.py && python3 -I tests/test_pipe_socks_condition_race.py && python3 -I tests/test_try_add_remove_races.py`
Expected: 全部退出 0；关键跨代、锁顺序和 worker 交接用例连续 10 轮无失败。

### Task 6: 整组回归、独立复审与收敛判定

**Files:**
- Modify: `docs/superpowers/specs/2026-11-06-network-reliability-convergence-design.md`（仅补最终证据摘要）
- Test: `tests/test_*.py`

**Interfaces:**
- Consumes: Task 1–5 的全部修复和 RED/GREEN 记录。

- [ ] **Step 1: 运行完整离线套件**

Run: 逐个执行所有 `tests/test_*.py` 并记录退出码。
Expected: 无失败文件；硬编码 `/Applications/XX-Net` 的测试单独标记为生产旧代码兼容证据，不替代 Coding 快照测试。

- [ ] **Step 2: 重复关键竞态并做静态检查**

Run: DNS/front/X-Tunnel 关键竞态各至少 10 轮；对全部修改 Python 文件做语法编译；`git diff --check`；`git status --short` 和 diff 范围核对。
Expected: 无失败、无语法错误、无 whitespace 错误，修改仅在计划文件内。

- [ ] **Step 3: 独立整组复审并处理重要发现**

让一个未参与实现的审查者检查完整工作树差异、锁顺序、协议兼容、重试交接和测试证据。主线程逐条核实；Critical/Important 新发现均先补准确 RED，再最小 GREEN，并重跑完整套件。

- [ ] **Step 4: 记录真实边界并交付**

记录通过数量、重复轮次和审查结果；明确未部署、未重启、未提交，生产仍为旧版本；不把短时黑盒或离线测试解释为公网间歇故障永久治愈。
