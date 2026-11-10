# 交互优先网络可靠性统一修复实施计划

> **执行方式：** 主线程按任务顺序内联执行；每项严格 RED→GREEN，最后进行一次独立整体复审。不得提交、部署或重启。

**目标：** 修复只读审计确认的跨 attempt/代际、字节交接、协议状态、资源清理和调度缺陷，使迟到事件不能污染新对象，平行实现遵守同一完整性与期限规则。

**架构：** 为 front Task 引入单调递增的 attempt 身份，为 X-Tunnel 会话和 Conn 调用方延续对象/代际凭据；Smart Router 回退按“读取剩余缓冲区→完整发送→发布连接”的事务处理。DNS 接收线程只解析和交付，别名递归由接收线程之外执行；缓存和停止操作成为真正的代际边界。

**技术栈：** Python 3、unittest、线程事件门、socketpair、dnslib、现有 front/X-Tunnel 测试替身。

**规范：** `docs/2026-11-06-interaction-first-network-audit.md`

## 全局约束

- 始终使用简体中文说明；代码标识符保持英文，新增注释使用简体中文。
- 所有生产修改必须先有准确失败的自动化测试；不得延长超时、放宽断言或隐藏异常制造通过。
- 不读取账号、token 等敏感配置；日志与派生证据不得暴露真实 session_id。
- 不执行真实 iptables，不访问生产网络；仅使用本地 socket、虚拟时钟和可控替身。
- 不删除既有日志、备份或 SDD 证据，不 git commit/push/merge/PR，不部署、不重启。
- DNS 全调用总期限与 `Task.read_all()` 的完成语义属于 C 级契约问题，本轮不擅自改变。
- 普通 checkout 的 `fix/h-log-dns-backoff` 分支继续保留上一阶段未提交修改。

## 接口与顺序

1. Smart Router 字节交接独立，可先修复。
2. front `Task.begin_attempt()` 与可选 `expected_attempt` 参数是 H1/H2 的共同接口；同组实现，避免半修复状态。
3. 独立 Client 复用既有 Response 正文分类，只修改调用级期限与响应处理，不改变 front Task 接口。
4. DNS helper 保留现有返回类型，增加来源/所有者/地址族校验；递归结果由接收线程外的调用方完成。
5. X-Tunnel 先修复身份与资源所有权，再缩小 roundtrip 锁范围；不得在代际保护之前扩大并发。
6. 最后统一验证缓存 epoch、PipeSocks/login 停止边界及全部套件。

### 任务 1：Smart Router 回退字节交接

**文件：** `smart_router/local/smart_route.py`；新建 `tests/test_smart_router_handoff.py`。

- [ ] 写 `test_unwrap_sendall_forwards_entire_prefix_before_pipe_publish`：底层每次最多接受 2 字节时，远端必须收到完整首包且之后才登记 Pipe。
- [ ] 写 `test_unwrap_transfers_buffered_request_body_after_headers`：真实缓冲 reader 已含 `BODY!!` 时，远端首包必须是重建请求头加正文。
- [ ] 写 `test_unwrap_failed_prefix_send_closes_remote_without_publishing`：发送失败不登记 Pipe，并关闭远端。
- [ ] 运行确认当前实现因部分写入、正文缺失和失败仍交接而准确 RED。
- [ ] 以非阻塞 `peek()` 转移旧 reader 已缓冲/立即可读字节，使用 `sendall()`，仅在完整成功后发布；所有失败路径关闭仍由本函数持有的远端 socket。
- [ ] 对 `do_direct()` 与非活动 `do_redirect_https()` 的同型单次 `send()` 一并改为 `sendall()`，并跑正反例。

### 任务 2：front attempt 身份与 H2 状态机

**文件：** `front_base/http_common.py`、`http1.py`、`http2_connection.py`、`http2_stream.py`、`http_dispatcher.py`；扩展 `tests/test_front_body_deadline.py`。

- [ ] 写旧 H2 stream 在新 attempt 接管后不得发送、mark response、put DATA、finish 或 retry 的事件门测试。
- [ ] 写旧 H1 发送异常在新 attempt 已接受后不得再次排队 retry 的测试；写 H1 close 后 `request()` 返回 False 且不排在 None 后的测试。
- [ ] 写合法 `100 HEADERS→200 HEADERS→DATA`、`200→DATA→trailers+END_STREAM` 正例，以及 DATA 早于 final headers 的反例。
- [ ] 写单 stream `RST_STREAM` 只关闭当前 stream、不调用连接 close 的测试。
- [ ] 运行得到对应准确 RED，再实现 `Task.begin_attempt()` 和 attempt 感知的响应/终止/retry 检查；H1/H2 接受任务时保存并传递 attempt，`_should_send_frame()` 同时核对登记身份、关闭状态和 attempt。
- [ ] H2 分离 informational、final 与 trailers；最终响应只交付一次，trailers 不覆盖最终头；RST 走 stream close。
- [ ] 运行全部 front 测试及关键竞态重复。

### 任务 3：独立 Client 正文完整性与调用期限

**文件：** `lib/noarch/simple_http_client.py`；扩展 `tests/test_front_body_deadline.py`。

- [ ] 写 EOF 定界 `HTTP/1.0 200 + abc` 返回 `abc` 且 eof=True 的测试。
- [ ] 写 `404 + Content-Length:3 + abc` 返回完整正文且缓冲无残留的测试。
- [ ] 写多个 informational/final/正文阶段共享 120ms 绝对预算的虚拟时钟测试，并保留 100 后 200 正例。
- [ ] 运行准确 RED 后，让非 200 也进入统一正文分类；缺失长度的非无正文响应按 EOF 读取，并禁止该连接复用。
- [ ] 从请求入口建立一次 deadline，连接、各响应头和正文只使用剩余预算；不得用逐阶段完整 timeout 续期。
- [ ] 运行独立 Client 与 front 全套测试。

### 任务 4：DNS 接收解耦、归属与地址族

**文件：** `smart_router/local/dns_query.py`；扩展 `tests/test_dns_response_ownership.py`、`test_dns_transport.py`。

- [ ] 写 CNAME-only UDP 响应不在接收线程同步等待、后续别名响应可由该线程读取并交付的确定性测试。
- [ ] 写合法 qname/CNAME 目标 A/AAAA 可接受、无关 rname 被拒绝、NS+CNAME 正确跟随的测试。
- [ ] 写未配置 UDP 来源或非 53 端口被拒绝、配置来源正常通过的测试。
- [ ] 写 xx-net AAAA 请求不得交付 IPv4，system 查询按请求类型选择地址族的测试。
- [ ] 运行准确 RED；将响应解析与别名查询拆开，接收线程只交付已经完整解析的结果，调用方或其他 worker 执行递归。
- [ ] 用 qname 加合法 CNAME 链验证 RR 所有者，核对 UDP 来源，所有平行来源按 QTYPE 过滤。

### 任务 5：X-Tunnel 代际与 Conn 资源所有权

**文件：** `x_tunnel/local/proxy_session.py`、`proxy_handler.py`、`base_container.py`；扩展 `tests/test_x_tunnel_memory_backpressure.py`。

- [ ] 写取旧 A 数据后切换 B 时不得登记/发送的测试；同代正常上传保留为正例。
- [ ] 写处理器持有 `(conn_id, Conn)` 凭据，reset 后旧首包和旧 stop 不得作用于同号新 Conn 的测试。
- [ ] 写首次 selector 注册失败仍执行 Conn peer-close/do_stop；建连中停止后的迟到 socket 立即关闭且不装回的测试。
- [ ] 写 reset 与并发注册不丢新映射、旧 200/create_conn 不修改新代际调度状态、ACK 恢复后不 reset 的测试。
- [ ] 运行准确 RED；上传取数与代际/登记使用一致的锁边界，start/stop 接收对象凭据，注册失败有显式清理路径，建连返回后复核 running/对象身份。
- [ ] reset 的 map/selector 替换与注册串行化，响应副作用在最终代际核对后发生。
- [ ] 运行 X-Tunnel 全套及竞态重复。

### 任务 6：缓存、停止与 roundtrip 调度边界

**文件：** `smart_router/local/host_records.py`、`dns_query.py`、`smart_router/local/pipe_socks.py`、`x_tunnel/local/proxy_session.py`；相应测试文件。

- [ ] 写 clean 后旧 DNS worker 不得复活缓存、新 worker 可写新结果的测试。
- [ ] 写 PipeSocks 停止后 add_socks 立即关闭且不登记；select ValueError 退出后不得留下可发布状态的测试。
- [ ] 写模块停止/全局 session 更换后旧 login 不得启动旧对象的测试。
- [ ] 写两个 roundtrip 的网络等待阶段可并行，但取数、登记和共享统计仍串行且代际安全的测试。
- [ ] 运行准确 RED；为缓存引入 epoch 并让 direct/white/parallel 写回都核对，停止发布采用“拒绝并关闭”，login 启动前复核全局运行与对象身份。
- [ ] 仅在已验证的身份边界外缩小 get_data_lock，网络请求及耗时 sleep 不持锁；异常与 reset 路径保持原语义。

### 任务 7：完整回归、独立复审与收敛

- [ ] 运行所有关联测试、完整 `tests/test_*.py`、front/X-Tunnel/DNS 关键竞态重复；扫描后台异常。
- [ ] 运行 `py_compile` 与 `git diff --check`，确认 HEAD 未变且未部署/重启。
- [ ] 派一个全新独立审查者按审计不变量检查完整工作树；主线程核实所有重要发现。
- [ ] Critical/Important 逐项 RED→GREEN 后再跑全套；C 级契约和低价值候选项明确记录，不把本地收敛写成生产永久治愈。
- [ ] 更新修复验收文档与台账，保留所有证据。
