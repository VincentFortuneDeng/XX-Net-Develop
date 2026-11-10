# 故障机理驱动网络修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复本轮交互审计确认的字节顺序、流控、跨代状态、资源交接和启动/停止边界，并完成隔离回归、独立复核、部署与运行验收。

**Architecture:** 不重建网络栈。以既有对象为单位补齐发送锁、对象/代际凭据、绝对期限和所有权收尾；H2 的连接流控、HPACK 顺序和 stream 正文游标分别由明确锁边界保护，模块启动失败则回滚已发布资源。生产修改严格遵循 RED→GREEN。

**Tech Stack:** Python 3、unittest、threading/queue/selectors、本地 socketpair 与故障替身；不依赖真实公网完成缺陷复现。

**Spec:** `docs/superpowers/specs/2026-11-08-mechanism-first-reaudit-design.md`

## Global Constraints

- 保留当前分支和既有未提交修改；不提交、不 push、不合并、不创建 PR，不额外创建 worktree。
- 所有测试用 `python3 -I`；按文件独立进程运行，避免既有全局替身串扰。
- 不读取或输出账号、密码、token；日志和派生分析中的 session_id 脱敏；不执行真实 iptables。
- DNS 全调用绝对期限、`Task.read_all()` 完成语义、NS 全局 single-flight、同 transfer 多 attempt 协议身份和 quota 回滚保持既有契约，不在本计划中擅改。
- 每项生产修复必须有先准确失败的回归测试；不得通过放宽断言、放大超时或只测替身制造通过。
- 部署前新建独立备份目录，不覆盖或删除旧备份、日志和其他 SDD 证据。

## Review Focus

- 真实 H2 peer 在 SETTINGS/WINDOW_UPDATE 与正文发送同时到达时，线上帧顺序、窗口和 HPACK 动态表必须一致；由任务 1 的并发事件门测试覆盖。
- H1 close/retry 与新 attempt 接管交错时不得继续发送旧字节；由任务 2 的真实 Task/Worker 测试覆盖。
- 模块启动在任一资源阶段失败时不得发布 ready 或留下监听/线程；由任务 7、8 的故障注入覆盖。
- 正常非循环 CNAME、合法多地址监听、正常可复用 Client 响应不得被异常修复误伤；由任务 3、4、5 的正例覆盖。
- 生产外部上游和对端 reset 仍是替代解释；部署验收只证明当前进程与请求路径，不反填历史根因。

---

### Task 1: H2 发送顺序与流控账本

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/front_base/http2_connection.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/front_base/http2_stream.py`
- Test: `tests/test_front_body_deadline.py`

**Interfaces:**
- Produces: 连接级发送锁保护 DATA 窗口与 blocked 队列；stream 级发送锁保护窗口和正文游标；HPACK 编码与 HEADERS 入队保持同一顺序事务。既有 `request()`、`_send_cb(frame)` 和 `send_left_body()` 公共入口保持兼容。

- [x] **Step 1: 写失败测试**：HEADERS 不耗 DATA 窗口；两个并发 DATA 不得把窗口扣成负数；`abcdef` 在初始发送与 WINDOW_UPDATE 交错下仍按 `ab/cd/ef` 顺序入队；两个请求 HPACK 编码和 HEADERS 入队顺序一致；初始连接窗口为 65535；重复 SETTINGS_MAX_FRAME_SIZE 不累加；负 stream 窗口不发送；peer HEADER_TABLE_SIZE 应用于 encoder；过期 DATA 只返还一次 DATA 窗口；关闭 stream 清除其 blocked DATA。
- [x] **Step 2: 运行 RED**：`python3 -I -m unittest discover -s tests -p 'test_front_body_deadline.py'`，新增 8 项均因目标行为失败，无夹具错误。
- [x] **Step 3: 最小实现**：DATA-only 连接流控；连接发送锁覆盖窗口检查/扣减、blocked 搬运和过期返还；stream 发送锁覆盖窗口更新及正文游标；头部编码和入队使用共享顺序锁；SETTINGS 使用赋值/增量协议语义。
- [x] **Step 4: 运行 GREEN 与正例**：同文件 68/68 通过，合法窗口恢复和正常多空 stream 不退化。

### Task 2: H1 与 dispatcher 的发送资格、期限和 worker 生命周期

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/front_base/http1.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/front_base/http_dispatcher.py`
- Test: `tests/test_front_body_deadline.py`

**Interfaces:**
- Produces: `_send_data(..., expected_attempt=...)` 发送期间以 Task 状态锁阻止新 attempt 越过旧发送；`get_worker(nowait=False, deadline=None)` 按当前任务剩余期限等待；停止后的 request 立即失败，晚到 worker 被关闭且不登记，淘汰 worker 真正关闭。

- [x] **Step 1: 写失败测试**：旧 attempt 在新 attempt 接受后不能 send；无 worker 时当前任务到期即完成且后续任务可被分发；stop/create 交错不留下 worker；慢 worker 淘汰调用 close；停止后 request 不进入无人消费的队列。
- [x] **Step 2: 运行 RED**：同任务 1 命令，新增 5 项均因目标行为准确失败；日志 `task2-red.log`。
- [x] **Step 3: 最小实现**：在 Task 状态锁内复核 attempt 并执行单次 socket send；worker 等待使用 deadline；停止/登记使用共同状态边界；淘汰路径 close 后移除，并修正慢 worker 候选分数初值。
- [x] **Step 4: 运行 GREEN**：73/73 通过，正常 retry/keepalive 正例保持；日志 `task2-green.log`。

### Task 3: 独立 Client 完整写入与 socket 所有权

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/simple_http_client.py`
- Test: `tests/test_front_body_deadline.py`

**Interfaces:**
- Produces: Client 是缓存 socket 的关闭责任方；Response 析构只释放 selector，不关闭仍由 Client/H1 持有的 socket；任何发送、头部或正文异常都清除 Client 缓存并关闭该 socket。

- [x] **Step 1: 写失败测试**：大请求头部分写入必须补写；正常 Response 析构后 Client 不得复用已关闭 socket；正文不完整后缓存清空；Response 状态行/头部遇 EOF 立即失败而非忙循环。
- [x] **Step 2: 运行 RED**：6 个新增用例准确失败，实际字节、fileno/缓存身份和 EOF 行为均符合复现；日志 `task3-red.log`。
- [x] **Step 3: 最小实现**：请求头与正文统一完整发送循环；Response 不再反向关闭外部 owner 的 socket；Client 异常路径 close；`read_line()` 对空读且 eof 立即抛 socket error。
- [x] **Step 4: 运行 GREEN**：79/79 通过，固定长度、chunked、EOF 正文、非 200、read_payload=False 和多地址/TLS 期限正例全部通过；日志 `task3-green.log`。

### Task 4: PipeSocks、Direct 与共享监听的交接收尾

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/smart_router/local/pipe_socks.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/smart_router/local/smart_route.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/simple_http_server.py`
- Test: `tests/test_pipe_socks_condition_race.py`, `tests/test_smart_router_handoff.py`, `tests/test_simple_http_server_keepalive.py`

**Interfaces:**
- Produces: `try_add()` 返回明确成败；首次 READ 注册失败回滚整对；pipe 任意异常退出均先停止发布再清理成员；Direct 仅在 PipeSocks 接管后解除 remote 关闭责任；监听自动重启后旧 serve 返回，部分 bind 失败回收全部 listener；HTTPServer 跟踪尚未返回的 accepted socket，shutdown 可中断慢握手，handler 返回后不再由 server 关闭已交接 socket。

- [x] **Step 1: 写失败测试**：selector OSError 后 running=False 且成员关闭；第一/第二端注册失败均无半注册和泄漏；Direct send/add_socks 异常关闭 remote；自动重启后旧 serve 不再 select 或关闭新 listener；第二地址 bind 失败关闭两端；停止可关闭尚未完成握手的 accepted socket，正常 handler 返回不错误关闭已交接连接。
- [x] **Step 2: 运行 RED**：三个测试文件分别运行，新增场景均准确失败；日志 `task4-*-red.log`。
- [x] **Step 3: 最小实现**：统一 pipe 异常收尾、可回滚注册、Direct 所有权 finally、`check_listen_port_or_reset()` 返回重启事实、`add_listen/init_socket` 失败清理、accepted socket 登记与 shutdown 快照关闭。
- [x] **Step 4: 运行 GREEN**：PipeSocks 5/5、Smart Router 7/7、HTTPServer 14/14 通过，既有正例保持；日志 `task4-*-green.log`。

### Task 5: DNS 跨实例写回、别名环和连接池关闭

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/smart_router/local/dns_query.py`
- Test: `tests/test_dns_query_history_isolation.py`, `tests/test_dns_transport.py`, `tests/test_dns_response_ownership.py`

**Interfaces:**
- Produces: DNS 任务同时捕获 DomainRecords 对象和 epoch，写回要求全局对象身份未变；TCP `query(..., history=None)` 与 DoH 在保留第三参数 `url=None` 后增加 `history=None`；同一调用共享规范化别名历史和最大 16 跳；过期池项显式 close。

- [x] **Step 1: 写失败测试**：旧 parallel/direct worker 不得写新缓存或继续写已停用旧缓存；TCP/DoH 的 a→b→a 有界停止且不累积池项；合法非循环 CNAME 成功；过期 TCP/DoH 池项关闭且 close 异常不阻止新连接；并行成功池有容量边界且 stop 全部关闭；UDP 本地查询期限使用单调时钟，墙钟回拨不放大等待。
- [x] **Step 2: 运行 RED**：分别运行三个 DNS 文件，确认不是测试替身签名错误。
- [x] **Step 3: 最小实现**：缓存对象身份+epoch；递归 callback 传同一 history；池项年龄检查后关闭。
- [x] **Step 4: 运行 GREEN**：UDP/TCP/DoH/xx-net/system 既有归属、地址族、NS 与退避测试全部通过。

### Task 6: 持久化记录、脏位与配置原子性

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/smart_router/local/host_records.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/xconfig.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/smart_router/local/__init__.py`
- Test: `tests/test_domain_records_concurrency.py`, `tests/test_config_concurrency.py`（新建）

**Interfaces:**
- Produces: DomainRecords 公共 key 统一 bytes，load 补齐 `r/dns/g/query_count/update`；DomainRecords/IpRecord 在同一锁内完成快照与清脏位；Config 使用实例 RLock 和同目录临时文件+`os.replace`；Smart Router 在生产者停止后保存最终 cache。

- [x] **Step 1: 写失败测试**：str/bytes 均恢复 rule 和 GAE deny；save 窗口中的并发写仍保持 dirty；IpRecord 同型；并发 save/load 不读到空/半截 JSON、不丢合法配置；stop 最终保存发生在生产者停止之后。
- [x] **Step 2: 运行 RED**：运行既有持久化文件和新配置文件。
- [x] **Step 3: 最小实现**：规范化 schema、锁内清脏位、配置原子替换及同步、stop 保存顺序调整。
- [x] **Step 4: 运行 GREEN**：旧格式兼容、正常 save/load 和无配置文件默认值正例通过。

### Task 7: X-Tunnel 首包、跨代副作用与半启动回滚

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/proxy_handler.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/proxy_session.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/client.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/front_dispatcher.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/base_container.py`
- Test: `tests/test_proxy_protocol_eof.py`, `tests/test_x_tunnel_memory_backpressure.py`, `tests/test_front_dispatcher_wait_isolation.py`

**Interfaces:**
- Produces: SOCKS5 端口统一经 `read_bytes(2)`；下载重试扫描携带 expected generation 并在副作用前复核；登录/API 仅在本代身份有效时提交全局变更；Session 启动失败不保持 running；on_road 覆盖所有登记后异常；周期线程使用本代停止事件/对象身份，不只依赖共享布尔值。

- [x] **Step 1: 写失败测试**：SOCKS5 请求与 EARLY 同包时首包完整移交；旧 worker 不消费新 SN 重试预算；旧 521/login/API 不写新 server/front；worker start 失败后 running=False 且可重试；encrypt 异常不泄漏 on_road；旧响应不污染新 speed；快速重启周期线程至多本代一个且 front 统计不会在启动顺序中立即退出；永久命令缺口有字节/期限边界，stop 后迟到命令不入队，小缓冲 flush 使用单调时钟。
- [x] **Step 2: 运行 RED**：运行三个关联文件，保存每个准确失败。
- [x] **Step 3: 最小实现**：统一缓冲读取；代际参数与锁内复核；全局提交前身份检查；启动事务回滚；扩大 on_road finally；周期线程捕获本代凭据。
- [x] **Step 4: 运行 GREEN**：既有 58 项 X-Tunnel、日志节流、连接/ACK 正例和 front 等待测试全部通过。

### Task 8: launcher、必要监听和 front 停止边界

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/launcher/module_init.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/gae_proxy/local/proxy.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/client.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/smart_router/local/dns_server.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/gae_proxy/local/front.py` 及使用按 host 懒创建 dispatcher 的 X-Tunnel front
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/front_base/connect_manager.py`
- Test: `tests/test_module_lifecycle.py`（新建）及关联 front 测试

**Interfaces:**
- Produces: 后台启动异常终止等待并回滚 proc_handler；GAE 在 ready 前 bind，X-Tunnel 全部端口失败即失败，DNS 无监听即失败并清理 socket；front stop 与懒创建/请求共享停止边界；ConnectManager.stop 清池且迟到 SSL 关闭；禁用模块/front 的旧发布引用清除。

- [x] **Step 1: 写失败测试**：后台 start 抛错后 start 返回失败、注册清理且可重入；三类必要监听失败均不 ready；get_dispatcher 与 stop 交错不创建幸存 dispatcher，停止后 request 立即失败；建连中 stop 后 socket 不入池且池清空，建连超时后的迟到 socket 由发送方关闭；缺席 X-Tunnel/禁用 front 引用为 None；DNS handler 有在途上限且 stop 接回。
- [x] **Step 2: 运行 RED**：运行新生命周期文件与既有 front 关联文件。
- [x] **Step 3: 最小实现**：启动 wrapper 记录异常并检查线程存活；ready 移到必要监听成功之后；失败回滚；dispatcher/front 停止锁；ConnectPool.clear 和迟到关闭；清理发布引用。
- [x] **Step 4: 运行 GREEN**：正常启动/停止/重入、健康 DNS/HTTP listener、正常 front 请求正例通过。

### Task 9: IpManager reset 与停止竞态

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/front_base/ip_manager.py`
- Test: `tests/test_ip_manager_lifecycle.py`（新建）

**Interfaces:**
- Produces: reset/stop/search_more_ip/scan_ip_worker 使用代际和线程计数的一致边界；旧 worker 退出只扣自己的代际计数，stop 后 reset 不得复活扫描。

- [x] **Step 1: 写失败测试**：reset 与旧扫描退出交错不产生负计数/超额线程；stop 与 reset 交错最终 running=False；正常 reset 能创建本代所需扫描线程。
- [x] **Step 2: 运行 RED**：确认真实方法在事件门下出现错误计数或复活。
- [x] **Step 3: 最小实现**：增加 scan generation 和生命周期锁，线程目标携带本代凭据，计数只由本代活动线程修改。
- [x] **Step 4: 运行 GREEN**：三项交错及正常扫描正例通过，不执行真实网络扫描。

### Task 10: 完整离线验收与独立复核

**Files:**
- Update: `docs/2026-11-08-mechanism-first-network-audit.md`
- Update: `.superpowers/sdd/2026-11-08-mechanism-first-reaudit/progress.md`

- [x] **Step 1: 完整套件**：逐文件运行全部 `tests/test_*.py`，统计 unittest 和独立脚本，任何失败按名称记录并修复。
- [x] **Step 2: 竞态重复**：DNS、H2/H1、Client、PipeSocks、X-Tunnel、生命周期关键文件各至少 10 轮，扫描 Traceback、后台线程异常和失败标记。
- [x] **Step 3: 静态检查**：全部改动 Python 文件 `py_compile`，`git diff --check`，确认无敏感输出和意外文件。
- [x] **Step 4: 独立复核**：提交计划、规范、真实 diff 和测试证据给新审查者；Critical/Important 各自先 RED 再 GREEN，并重跑完整套件。
- [x] **Step 5: 收敛记录**：只声明已覆盖模型内没有未处理高优先级项；C 级契约、低影响 deferred 和生产未知项单列。

### Task 11: 备份部署、重启与运行验收

**Files:**
- Create: `.superpowers/sdd/2026-11-08-mechanism-first-reaudit/deployment/` 下独立证据
- Update: `docs/2026-11-08-mechanism-first-network-audit.md`

- [x] **Step 1: 部署前核对**：确认生产目标文件无未知侧修改，记录开发/生产 SHA256；新建独立备份，保存原文件和 diff，不覆盖旧证据。
- [x] **Step 2: 部署与重启**：仅在完整回归和独立复核通过后同步目标运行文件；正常停止旧 PID，使用 `/Applications/XX-Net/start -no_popup` 启动，确认旧进程退出、新 PID 和 1080/8085/8086 监听。
- [x] **Step 3: 运行验收**：核对运行文件哈希、本地控制接口、经 8086 的实际代理请求及必要的 Smart Router/X-Tunnel 入口；检查部署窗口脱敏日志，无指向本轮修改的新异常。
- [x] **Step 4: 回滚判定**：新进程无法就绪、控制接口失败、代理基本请求持续失败或出现本轮新异常时，恢复原运行文件并再次验证；短时 204 不作为间歇问题永久治愈证据。
