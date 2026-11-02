# XX-Net 可靠性与性能优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **执行闸门：本文件仅用于后续规划和 GitHub 跟踪。未经用户再次明确授权，不开始任何任务，不修改运行代码，不部署、不重启。**

**Goal:** 在保留 XX-Net 现有模块架构的前提下，优先消除已确认的并发与状态缺陷，再建立资源边界、性能观测和持续验收能力。

**Architecture:** 不重写 Smart Router、X-Tunnel 或 front_base。P0 先修复小范围正确性问题，P1 控制故障时的线程、内存和等待时间，P2 仅在 profiling 证据支持时优化字节复制、锁和数据结构。每项改动独立 TDD、独立提交，并保留现有生产黑盒 F04。

**Tech Stack:** Python 3、线程、selectors2、socket、项目自带 simple_http_server、xlog、GitHub Issues。

**Spec:** `docs/2026-11-03-independent-acceptance-report.md`；架构边界见 `docs/XX-Net-项目原理与系统架构.md`。

## Global Constraints

- 始终使用简体中文说明；代码标识符保持英文。
- 新行为必须先有独立失败测试，再最小实现；不得用既有 TDD 通过代替独立验收。
- 不删除既有日志、部署备份和 F04 黑盒用例，不用单次生产通过宣称间歇问题已修复。
- 生产仅做只读检查、正常 Web 请求和代理连通性检查；破坏性、异常时序和资源故障只在临时目录、替身边界或独立 socket 中注入。
- 不读取或输出账号密码、token 等敏感配置；诊断脱敏继续保持 fail-closed。
- 不推翻 selector、Direct 背压、HTTP/2 worker、front 评分、DNS 退避和生命周期 flush 的既有设计。
- 每个任务独立提交；部署、重启和 push 分别需要用户明确授权。
- 当前禁用 GAE，不将 GAE 优化置于正在运行的 Smart Router/X-Tunnel 之前。

## Review Focus

- HTTP/1.0 与 HTTP/1.1 混合、连续请求和显式 close 是否严格保持连接语义。
- 所有 front 不可用或恢复时，单个等待请求不得阻塞其他会话和控制请求。
- 多次独立 DNS 别名查询不得共享递归历史，迟到响应不得污染新查询。
- 高延迟、多 IP、慢 ACK、慢消费者下线程、FD 和内存必须有可观测上限。
- stop/reset、队列满、磁盘慢和重试超时时必须保留原异常、日志尾账与资源清理。

---

### Task 1: 修复本地 HTTP 长连接状态机

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/simple_http_server.py`
- Test: `tests/test_simple_http_server_keepalive.py`

**Interfaces:**
- Consumes: 现有 `HttpServerHandler.parse_request()`、`handle_one_request()` 与 F04 生产黑盒。
- Produces: HTTP 版本和 `Connection` 请求头决定 `close_connection` 的单一状态流，不被后续无条件重置。

- [ ] **Step 1: 编写连接状态失败测试**

覆盖 HTTP/1.0、HTTP/1.1、`keep-alive`、`close` 和同一连接连续两个请求；断言响应完成后的关闭行为及第二个请求是否可读。

- [ ] **Step 2: 运行测试确认 RED**

Run: `python3 -I tests/test_simple_http_server_keepalive.py`  
Expected: 至少显式 `Connection: close` 或连续请求状态用例失败，失败原因指向第 197 行覆盖解析结果。

- [ ] **Step 3: 最小修正连接状态流**

保留各 handler 对 Upgrade、CONNECT 和协议错误的显式关闭语义，不顺带重写线程模型。

- [ ] **Step 4: 运行独立测试和现有生产黑盒**

Run: `python3 -I tests/test_simple_http_server_keepalive.py`  
Expected: 全部 PASS。

Run: `python3 -I tests/acceptance_production_blackbox.py`  
Expected: 记录真实结果；F04 必须保留，单次 PASS 只记“本轮未复现”。

- [ ] **Step 5: 独立提交**

```bash
git add Coding/XX-Net-5.16.7.1/code/default/lib/noarch/simple_http_server.py tests/test_simple_http_server_keepalive.py
git commit -m "fix: 修正本地 HTTP 长连接状态"
```

### Task 2: 消除 front 选择的全局等待锁

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/front_dispatcher.py`
- Test: `tests/test_front_dispatcher_wait_isolation.py`

**Interfaces:**
- Consumes: `get_front(host, timeout)`、各 dispatcher `get_score()` 和 front 生命周期。
- Produces: 仅保护共享注册表/快照的短锁；评分、等待、恢复通知在全局锁外执行，函数签名不变。

- [ ] **Step 1: 编写全局阻塞失败测试**

让一个 front 在无可用 worker 时等待，同时发起另一独立 front 请求；断言后者能在独立短 deadline 内完成或失败，而不是被前者锁住。

- [ ] **Step 2: 运行测试确认 RED**

Run: `python3 -I tests/test_front_dispatcher_wait_isolation.py`  
Expected: 第二请求超出独立短 deadline，复现全局锁等待。

- [ ] **Step 3: 缩小锁并用事件/条件通知恢复**

评分基于一致快照，锁内不 sleep、不等待网络；保留 light/session front 分流与 stop 清理。

- [ ] **Step 4: 验证故障、恢复与旧回归**

Run: `python3 -I tests/test_front_dispatcher_wait_isolation.py`  
Expected: 全部 PASS；全部不可用、部分不可用、恢复和 stop 均无死锁。

- [ ] **Step 5: 独立提交**

```bash
git add Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/front_dispatcher.py tests/test_front_dispatcher_wait_isolation.py
git commit -m "fix: 隔离 front 选择等待"
```

### Task 3: 隔离 DNS 递归查询历史

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/smart_router/local/dns_query.py`
- Test: `tests/test_dns_query_history_isolation.py`

**Interfaces:**
- Consumes: `CombineDnsQuery.query(domain, dns_type=1, history=[])` 及递归别名解析。
- Produces: 每次顶层调用独立的递归访问集合；公开调用签名保持兼容，内部递归显式传递集合。

- [ ] **Step 1: 编写跨调用污染失败测试**

第一次查询经过别名，第二次独立查询复用相同别名；断言第二次仍执行解析且结果不被第一次历史裁剪。

- [ ] **Step 2: 运行测试确认 RED**

Run: `python3 -I tests/test_dns_query_history_isolation.py`  
Expected: 第二次查询因共享默认 `history` 而缺失结果或解析调用。

- [ ] **Step 3: 最小修正默认参数和递归入口**

保留循环别名防护，不改变 DNS 上游过滤和退避策略。

- [ ] **Step 4: 运行新测试和 DNS 回归**

Run: `python3 -I tests/test_dns_query_history_isolation.py`  
Expected: 全部 PASS，含重复、循环、跨域名和跨 dns_type 用例。

- [ ] **Step 5: 独立提交**

```bash
git add Coding/XX-Net-5.16.7.1/code/default/smart_router/local/dns_query.py tests/test_dns_query_history_isolation.py
git commit -m "fix: 隔离 DNS 递归查询历史"
```

### Task 4: 为 Direct 建连和缓存建立资源边界

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/smart_router/local/connect_manager.py`
- Test: `tests/test_smart_router_connect_bounds.py`

**Interfaces:**
- Consumes: 现有 `connect_threads=3`、`get_conn()`、IP 质量排序和 SocketWrap 缓存。
- Produces: 有界在途建连、每主机/全局缓存上限、空键清理和迟后结果回收；保留快速 IP 优先。

- [ ] **Step 1: 编写线程与缓存上限失败测试**

以可控替身模拟多 IP 慢连接和迟后成功，断言在途数不超过配置、缓存有界、过期后主连接键被移除。

- [ ] **Step 2: 运行测试确认 RED**

Run: `python3 -I tests/test_smart_router_connect_bounds.py`  
Expected: 当前实现创建超过 `connect_threads` 的任务或留下无界缓存。

- [ ] **Step 3: 实现有界竞争与缓存清理**

不在锁内执行阻塞 connect；迟后 socket 必须关闭或受容量约束，缓存连接首次使用失败按原候选策略回退。

- [ ] **Step 4: 运行资源与并发回归**

Run: `python3 -I tests/test_smart_router_connect_bounds.py`  
Expected: 全部 PASS；成功、超时、停止、缓存陈旧和重复调用均无 FD 泄漏。

- [ ] **Step 5: 独立提交**

```bash
git add Coding/XX-Net-5.16.7.1/code/default/smart_router/local/connect_manager.py tests/test_smart_router_connect_bounds.py
git commit -m "fix: 限制 Direct 建连资源占用"
```

### Task 5: 为 X-Tunnel 增加会话级背压和字节预算

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/base_container.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/proxy_session.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/config.py`
- Test: `tests/test_x_tunnel_memory_backpressure.py`

**Interfaces:**
- Consumes: `SendBuffer`、`wait_ack_send_list`、`Conn.windows_size`、selector READ 注册。
- Produces: 可配置的会话级高/低水位和每连接预算；达到高水位暂停本地 READ，恢复到低水位重新注册，超时可清理。

- [ ] **Step 1: 编写慢 ACK 内存失败测试**

持续写入且不让对端 ACK，断言总缓冲不能无限增长，READ 在高水位暂停，ACK 后恢复且数据顺序不变。

- [ ] **Step 2: 运行测试确认 RED**

Run: `python3 -I tests/test_x_tunnel_memory_backpressure.py`  
Expected: 当前会话缓冲超过独立预算或 READ 未暂停。

- [ ] **Step 3: 实现两级预算与恢复**

预算覆盖真实保留字节，而非只统计连接数；停止/reset 清理并唤醒等待方，不引入全局忙轮询。

- [ ] **Step 4: 运行背压、SN 和生命周期回归**

Run: `python3 -I tests/test_x_tunnel_memory_backpressure.py`  
Expected: 全部 PASS；高/低水位无抖动，stop 后无残留等待。

- [ ] **Step 5: 独立提交**

```bash
git add Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/base_container.py Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/proxy_session.py Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/config.py tests/test_x_tunnel_memory_backpressure.py
git commit -m "fix: 增加 X-Tunnel 会话背压"
```

### Task 6: 统一 front 请求的绝对 deadline

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/front_dispatcher.py`
- Test: `tests/test_front_request_deadline.py`

**Interfaces:**
- Consumes: `request(..., timeout=100)`、`get_front()`、`front.request()` 和重试 sleep。
- Produces: 基于 `time.monotonic()` 的绝对截止时间；每次子操作只使用剩余时间，公开参数保持秒单位。

- [ ] **Step 1: 编写累计超时失败测试**

让选路、front 请求和重试分别消耗接近完整 timeout，断言总耗时和子调用 timeout 不超过允许误差。

- [ ] **Step 2: 运行测试确认 RED**

Run: `python3 -I tests/test_front_request_deadline.py`  
Expected: 当前多次完整 timeout 导致总等待明显超过配置。

- [ ] **Step 3: 传递剩余 deadline**

deadline 到期后不再发起新尝试；保留 521/400/404 等既有响应语义。

- [ ] **Step 4: 运行重试与停止回归**

Run: `python3 -I tests/test_front_request_deadline.py`  
Expected: 全部 PASS；零 timeout、单次成功、重试成功和 stop 均符合边界。

- [ ] **Step 5: 独立提交**

```bash
git add Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/front_dispatcher.py tests/test_front_request_deadline.py
git commit -m "fix: 严格限制 front 请求总等待"
```

### Task 7: 将日志磁盘写入移出代理热路径

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/xlog.py`
- Test: `tests/test_xlog_bounded_writer.py`

**Interfaces:**
- Consumes: `Logger.log()`、持久文件滚动、startup/warning 视图、LogThrottle 和 reset/stop flush。
- Produces: 有界写入队列、顺序批量写、队列满降级计数及停止封口；Web JSON 和导出格式保持兼容。

- [ ] **Step 1: 编写慢磁盘与队列上限失败测试**

用独立慢写入替身证明业务调用不被逐条 flush 阻塞，同时验证队列满计数、顺序、ERROR_CONTEXT 和 stop 尾账。

- [ ] **Step 2: 运行测试确认 RED**

Run: `python3 -I tests/test_xlog_bounded_writer.py`  
Expected: 当前调用线程执行同步 flush 或缺少有界降级语义。

- [ ] **Step 3: 实现有界批量 writer**

不得引入无界队列；文件大小预算、单代 `.1`、敏感信息处理、原异常传播和生命周期 flush 保持不变。

- [ ] **Step 4: 运行日志全量回归**

Run: `python3 -I tests/test_xlog_bounded_writer.py`  
Expected: 全部 PASS；再运行既有统一日志、视图、诊断包和生命周期测试并记录数量。

- [ ] **Step 5: 独立提交**

```bash
git add Coding/XX-Net-5.16.7.1/code/default/lib/noarch/xlog.py tests/test_xlog_bounded_writer.py
git commit -m "perf: 批量处理持久日志写入"
```

### Task 8: 建立性能观测、profiling 与持续验收基线

**Files:**
- Create: `tests/performance_baseline.py`
- Create: `docs/performance-baseline.md`
- Modify: 仅在被测模块已有状态接口中补充必要计数器，具体文件由测量设计决定。

**Interfaces:**
- Consumes: Task 1–7 的状态和现有 Web/代理只读接口。
- Produces: DNS/建连/front/roundtrip 延迟，重传率、队列字节数、线程/FD/内存水位和日志等待指标；不改代理协议。

- [ ] **Step 1: 编写指标守恒失败测试**

用独立参考账本验证成功、失败、重试、reset 后计数不重复、不丢失，性能脚本默认不访问生产故障路径。

- [ ] **Step 2: 运行测试确认 RED**

Run: `python3 -I tests/performance_baseline.py --self-test`  
Expected: 指标或场景尚未实现。

- [ ] **Step 3: 实现最小观测和基线场景**

场景包含正常吞吐、慢消费者、多域名、front 故障恢复和长时间 soak；先测量再决定是否做 memoryview、锁拆分或窗口调参。

- [ ] **Step 4: 记录基线和优化门槛**

Run: `python3 -I tests/performance_baseline.py --self-test`  
Expected: 全部 PASS；报告包含环境、p50/p95/p99、资源高水位和未覆盖项，不把离线结果写成生产疗效。

- [ ] **Step 5: 独立提交**

```bash
git add tests/performance_baseline.py docs/performance-baseline.md
git commit -m "test: 建立性能与资源基线"
```

## 后续执行规则

1. 推荐顺序为 Task 1 → 2 → 3 → 4/5/6 → 7 → 8；Task 4、5、6 可在不同工作区独立开展，但合入前统一跑全量回归。
2. 任一任务需要修改协议、公开配置默认值或生产系统时，先重新提交设计供用户批准。
3. 只有 Task 8 的真实 profiling 证明存在瓶颈后，才新增零复制、时间轮、asyncio 或窗口/worker 调参任务。
4. 本计划创建 GitHub Issues 仅用于跟踪，不代表已授权开始实施。

## 2026-11-03 提交前验证记录

- `python3 -I tests/acceptance_independent_models.py`：**30/30 PASS**。
- `python3 -I tests/acceptance_production_blackbox.py`：**9/10 PASS**；F04 默认 HTTP/1.1 的 24 路并发 `/debug` 中 1 路 `TimeoutError('timed out')`，其余 23 路 HTTP 200；显式 `Connection: close` 组通过。
- F04 是已登记且尚未修复的历史问题，本次复现进一步支持 Task 1 的优先级；本次提交不包含其修复，也不因其他九项通过而宣称长连接问题已解决。
