# X-Tunnel 警告诊断与连接启动竞态修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 查明当前三条 front 同时超时的证据边界，并消除新建 X-Tunnel 连接启动前的 `conn_list` 竞态异常。

**Architecture:** 生产侧只做只读日志关联和正常连通性请求，不以猜测修改网络参数。连接生命周期修复集中在 `ProxySession` 提供原子化启动入口，各调用方不再在 `create_conn()` 返回后自行索引共享注册表。

**Tech Stack:** Python 3、标准库 `unittest`、线程同步、现有 X-Tunnel/Smart Router 代码。

**Spec:** `docs/2026-11-03-reliability-roadmap-deployment.md` 及 2026-11-05 生产 WARNING 只读诊断证据；本轮没有独立设计文档。

## Global Constraints

- 使用简体中文说明和注释，代码标识符保持英文。
- 只在 `fix/h-log-dns-backoff` 开发分支修改；不部署、不重启生产、不 push。
- 生产只读检查和正常请求，不注入故障，不读取或输出账号、密码、token。
- 修复遵循 RED→GREEN；不得把离线通过写成生产疗效。
- 不删除旧日志、备份或 SDD 证据目录。

## Review Focus

- 连接在 `create_conn()` 后、`start()` 前被移除：调用方应安静终止该连接，不得抛出 `KeyError`。
- 带首包数据的正常连接：必须只传递一次首包并启动一次读取事件。
- 空首包和默认 `block=True`：保持现有调用语义。
- 同一连接停止与启动并发：不得因共享字典重新索引产生异常；本计划不承诺恢复已关闭连接。
- 三条 front 共同超时：离线测试不能证明远端或生产网络根因，诊断结论必须保留证据边界。

---

### Task 1: 关联三条 front 的共同失败链

**Files:**
- Create: `docs/2026-11-05-xtunnel-warning-analysis.md`

**Interfaces:**
- Consumes: 2026-11-05 生产日志、正常直连/代理请求和现有 front 请求代码。
- Produces: 按证据分级的原因判断及后续观测重点，不产生运行代码接口。

- [ ] **Step 1: 汇总同一时间窗口的多层日志**

对同一传输编号和相近时间戳比较 Seley、Cloudflare、TLS Relay、X-Tunnel 与 Smart Router，区分同一故障的重复日志和独立故障。

- [ ] **Step 2: 核对正常与失败路径**

阅读 `front_dispatcher.request()`、`HttpDispatcher.request()` 和各 front 请求出口，确认 601、602、响应长度不一致及重试日志的实际语义。

- [ ] **Step 3: 写入诊断结论**

记录当前代理实测影响、共同故障层、DNS 下游关系、登录协议错误恢复情况，以及尚不能确认的远端/本地根因；不把时间相关性写成因果。

- [ ] **Step 4: 提交诊断记录**

```bash
git add docs/2026-11-05-xtunnel-warning-analysis.md
git commit -m "docs: 分析 X-Tunnel 警告故障链"
```

### Task 2: 原子化连接首包传递与启动

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/proxy_session.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/proxy_handler.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/smart_router/local/smart_route.py`
- Test: `tests/test_x_tunnel_memory_backpressure.py`

**Interfaces:**
- Consumes: 现有 `ProxySession.lock`（`threading.RLock`）、`conn_list`、`Conn.transfer_received_data(data)` 和 `Conn.start(block)`。
- Produces: `ProxySession.start_conn(conn_id, left_buf=b"", block=True) -> bool`；连接不存在时返回 False，正常时传递可选首包、启动并返回 True。`create_conn()` 返回连接编号的接口不变。

- [ ] **Step 1: 写连接已移除的 RED 测试**

在现有 X-Tunnel 会话测试中构造只含 `conn_list`、`lock` 和启动替身的真实 `ProxySession` 对象，断言 `start_conn()` 对已被移除连接返回 False，且不抛出异常、不启动其他连接。

- [ ] **Step 2: 运行测试并确认预期失败**

Run: `python3 -I tests/test_x_tunnel_memory_backpressure.py`
Expected: 新增测试因 `ProxySession.start_conn` 尚不存在而失败，其余既有测试通过。

- [ ] **Step 3: 增加正常首包与空首包测试**

断言正常连接返回 True；有首包时先传递一次再启动一次，空首包时不调用首包传递；显式 `block=False` 原样传给 `Conn.start()`。

- [ ] **Step 4: 实现公共启动入口并迁移调用方**

在 `ProxySession` 内用既有 RLock 获取并操作对应连接；迁移 `proxy_handler.py` 的 SOCKS4、SOCKS5、HTTPS、HTTP 与 `smart_route.py` 的 `do_socks()`，移除这些路径的共享字典直接索引。Smart Router 在启动入口返回 False 时按既有 `XTunnelNotRunning` 路径退出。

- [ ] **Step 5: 运行相关测试并确认转绿**

Run: `python3 -I tests/test_x_tunnel_memory_backpressure.py`
Expected: 全部通过，无异常或警告输出。

- [ ] **Step 6: 检查所有调用点并提交**

确认 `create_conn()` 后不再有 `conn_list[conn_id].start()`；运行 `git diff --check` 和修改文件编译检查。

```bash
git add Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/proxy_session.py Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/proxy_handler.py Coding/XX-Net-5.16.7.1/code/default/smart_router/local/smart_route.py tests/test_x_tunnel_memory_backpressure.py
git commit -m "fix: 消除 X-Tunnel 连接启动竞态"
```

### Task 3: 完整离线回归与证据收尾

**Files:**
- Modify: `docs/2026-11-05-xtunnel-warning-analysis.md`

**Interfaces:**
- Consumes: Task 1 诊断记录和 Task 2 修复。
- Produces: 测试通过/失败清单、未覆盖边界和未部署状态。

- [ ] **Step 1: 运行完整离线测试**

逐个运行所有 `tests/test_*.py`，同时运行独立模型和性能基线自验；记录真实通过数及任何既有失败，不隐藏旧 IPv6 测试失败。

- [ ] **Step 2: 做整支修改的独立审查**

以 Task 2 的实际差异和上述 Review Focus 做 fresh-context 审查；Critical/Important 必须先 RED→GREEN 修复，Minor 记录而不顺带扩大范围。

- [ ] **Step 3: 更新结论并提交**

记录修复只证明离线竞态路径，不宣称已解决生产 front 超时；注明未部署、未重启、未 push。

```bash
git add docs/2026-11-05-xtunnel-warning-analysis.md
git commit -m "docs: 记录连接竞态修复验证"
```
