# 网络可靠性交互优先只读审计

日期：2026-11-06

## 范围与证据边界

本轮不再按文件平均扫描，而是从历史故障机理出发，检查多个执行流、多个功能与多个层之间的共享状态、对象所有权和事件顺序。重点事件链为：

- front：dispatcher → H1/H2 worker → Task/Stream → 响应消费者，穿插 timeout、close、retry、finish。
- X-Tunnel：selector → ConnectionPipe → Conn → Session → roundtrip/checker，穿插 stop/reset/start/login。
- DNS：调用方 → UDP/TCP/DoH/平行来源 → waiter → 接收线程/超时清理 → cache/backoff。
- Smart Router：协议入口 → GAE → X-Tunnel 回退 → SocketWrap/PipeSocks，穿插请求缓冲区与 socket 所有权交接。

审计基于当前工作树，HEAD 为 `5dc3f12a6d9561e6722f5496bdb53af1d76dea6c`。本轮未修改生产代码、未部署、未重启、未提交。独立审查只提供候选；重要条目均由主线程阅读真实实现复核，关键条目另做确定性离线复现。

证据等级：

- **R**：真实代码与可控替身/事件门的离线复现；证明当前实现存在该行为，不证明它是某次公网故障的唯一根因。
- **S**：源码可推出完整触发与影响路径，但本轮未做动态复现。
- **C**：行为存在，但是否违反接口契约仍需设计裁定。

## 总结

本轮确实发现了此前测试没有覆盖的新问题，主要集中在四类：

1. **旧 attempt/旧代际在新对象接管后重新获得执行资格**：H2 旧帧、H1 重复 retry、X-Tunnel 上传与同号 Conn。
2. **跨层交接没有连同缓冲区或剩余字节一起移交**：GAE→X-Tunnel 回退的部分写入与缓冲请求体丢失。
3. **平行协议实现语义漂移**：H2 的 informational/trailers/RST、独立 Client 的 EOF/非 200/期限、DNS 平行来源地址族。
4. **资源清理只覆盖已登记对象**：首次注册失败、建连中停止、清缓存与旧 worker、停模块与旧 login。

上一轮已修复并通过回归的 DNS waiter 原子登记、Task 终止互斥、H2 登记窗口、X-Tunnel Conn 身份检查等并未被本轮否定；本轮发现的是它们与下一层组合后的漏口。因此“上一轮本地收敛”不能解释为整个网络系统已经收敛。

## 一、Smart Router 回退与转发交界

### SR-1：GAE→X-Tunnel 回退接受部分写入后仍移交连接（高，R）

`smart_route.py:261-296` 的 `do_unwrap_socks()` 对非空 `left_buf` 只调用一次 `remote_ssl_sock.send(left_buf)`，不检查返回值，随后创建 `SocketWrap` 并交给 `PipeSocks`。`socket_wrap.py:31-32` 只是委派底层 `send()`，没有补写保证。

真实回退来源位于 `smart_route.py:425-434`：GAE 抛出 `NotSupported` 后重建原始请求头并调用该函数。离线输入 6 字节、底层只接受 2 字节时，函数仍配对连接，剩余 4 字节未发送。

证据：`interaction-audit/unwrap-first-fragment-partial-send.log`。

边界：`do_direct()` 在 `smart_route.py:217-219` 也有同型单次 `send()`，但当前仓库内普通 Direct 调用通常没有非空 `left_buf`，所以列为潜在接口风险，不与上述实际回退路径混为一谈；`do_redirect_https()` 当前在 `try_loop()` 中被立即 `continue`，不是活动路径。

### SR-2：回退放弃带缓冲的请求读取器，已读请求体不随 socket 交接（高，R）

`simple_http_server.py:49,58,113-171` 使用带 32KiB 缓冲的 `rfile` 解析请求行与请求头。`readline()` 可以把紧随请求头的请求体提前读入缓冲区。

GAE 不支持的方法在 `smart_route.py:350-352` 进入回退；`smart_route.py:427-434` 只重建请求行和请求头。`do_unwrap_socks()` 在 `283-286` 阻止旧请求对象关闭 socket，却把后续读取交给原始 socket，没有转移 `req.rfile` 中已缓冲的字节。

主线程以真实 `do_gae()`、真实请求解析器和真实 `do_unwrap_socks()` 复现：`OPTIONS` 请求声明 `Content-Length: 6`，请求头与 `BODY!!` 一起到达；回退只转发请求头，6 字节全部留在旧缓冲区，原始 socket 已进入配对。

证据：`interaction-audit/unwrap-buffered-body-loss.log`。

### SR-3：PipeSocks 停止/退出交界仍可能接收新连接（中，S）

`pipe_socks.py:251-262` 在 `select()` 抛出 `ValueError` 时直接返回，没有恢复 worker；`add_socks()` 在 `82-103` 不检查 `running`。若 worker 已退出或停止清理已完成，新连接仍可被登记但没有活动消费者。该条目是生命周期候选，本轮未关联生产异常，不升级为已发生故障。

## 二、DNS 组合时序与平行来源

### DNS-1：UDP 接收线程同步递归查询，堵住自己的响应入口（高，R）

`dns_query.py:301-324` 的唯一 socket 接收线程调用 `_extract_response_ips()`；CNAME-only 响应在 `56-78` 内同步执行 `self.query(alias, dns_type)`。内层查询登记 waiter 后等待结果，但递归响应仍需要同一个接收线程读取，形成自阻塞；外层在 `316-317` 不交付空结果，原调用方也只能等待到期。

确定性事件门记录到顺序为“收到外层响应 → 当前接收线程进入内层等待 → 才有机会再次 recvfrom”，原 waiter 未交付且仍登记。独立审查使用真实队列另复现约 3 秒内层等待。

证据：`interaction-audit/dns-cname-receiver-reentry.log`。

### DNS-2：只核对 RR 类型，不验证 RR 所有者与 CNAME 链（高，R）

`_extract_response_ips()` 在 `dns_query.py:56-66` 接受所有类型匹配的 RR，没有检查 `record.rname` 是否属于原 qname 或合法 CNAME 链。合法别名目标的 A/AAAA 应当允许，但不能因此接受任意无关所有者。

当前实现对 `example.com A` 响应中的 `unrelated.example A 192.0.2.99` 返回该地址。UDP、TCP、DoH 共用该 helper，错误结果还可进入缓存。

证据：`interaction-audit/dns-cross-layer-probes.log`。

### DNS-3：UDP 响应不校验来源 IP/端口（高，R）

`dns_recv_worker()` 在 `dns_query.py:282-315` 从 `recvfrom()` 取得 `server, port`，但后续只用于日志；未核对它是否为本次候选上游及端口 53。主线程用未配置来源 `198.51.100.99:53` 返回匹配 ID/question 的包，当前代码仍交付答案。

证据：同上。路径外伪造仍需猜测 ID 和时序；路径上攻击者则不受这一限制。

### DNS-4：xx-net 平行来源忽略 AAAA，可能交付并缓存 IPv4（中，R）

`query_dns_from_xxnet()` 在 `dns_query.py:139-167` 的请求 URL 不含 `dns_type`，返回后也不筛选地址族。`ParallelQuery.query_worker()` 在 `770-777` 以原任务类型交付和写缓存。AAAA 任务取得 `192.0.2.1` 的行为已复现。

iOS 的 `query_by_system()` 在 `363-379` 始终调用 `gethostbyname()`，存在同型静态缺口，但本轮未在该平台复现。

### DNS-5：清空缓存不是代际边界，旧 worker 可复活旧结果（中，R）

`host_records.py:77-80` 的 `DomainRecords.clean()` 只清空缓存；`ParallelQuery` 的 generation 在 `dns_query.py:765-783` 不因清理而改变。清理前启动的 worker 返回后仍通过 generation 检查并写回。主线程复现清理后缓存先为空，释放旧 worker 后又出现旧结果。

证据：同上。`CombineDnsQuery` 的 direct/white 路径在 `868-871` 还直接写缓存，同样不受缓存 epoch 保护。

### DNS-6：NS 查询提前返回，NS+CNAME 不跟随别名（中，R）

`dns_query.py:65-66` 对 NS 无论是否有答案都立即返回，导致 `68-78` 的 CNAME 遍历不可达。当前 helper 对 NS+CNAME 返回空且递归回调次数为 0，UDP/TCP/DoH 均受影响。

### DNS-7：整个 DNS 调用是否应共享一个绝对期限（C）

`dns_query.py:826-881` 的 unknown 路径可顺序消耗本地 UDP、平行等待和最终 UDP 的独立预算；TCP/DoH 的 CNAME 递归也重新建立期限。代码行为明确，但现有设计只强制 UDP 发送后的剩余等待预算，没有把全 DNS 调用的总墙钟期限写成契约，因此本轮不把它直接定为缺陷。

## 三、front attempt、协议状态与独立 Client

### FR-1：新 H2 attempt 接受后，旧 stream 可重新发送或交付（高，R）

`http2_stream.py:376-404` 的 `close()` 先设置 `_closed`、执行 retry，最后才注销。新 worker 在 `http2_connection.py:179-181` 清除同一 Task 的 retry claim；旧 stream 若尚未注销，`196-206` 的发送过滤只看登记和 Task 当前状态，不看 `_closed` 或 attempt 身份。

主线程复现到 `stream._closed=True` 且旧 stream 仍登记时，旧 HEADERS 再次具备发送资格。独立审查还复现旧 stream 的 HEADERS 在新 attempt 中交付 200。接收路径 `http2_stream.py:213-247,353-368` 同样缺少关闭/attempt 检查。

风险包括重复发送非幂等请求、旧响应或 DATA 归入新 attempt。

证据：`interaction-audit/front-cross-layer-probes.log`。

### FR-2：H1 外部 close 与旧发送异常可重复排队同一 Task（高，R）

发送异常在 `http1.py:166-179` 调 retry，外部 `close()` 在 `359-370` 也可对同一 Task 调 retry。第一次 retry 后，新 attempt 在 `63-67` 清除 claim；旧异常路径恢复时可再次通过 `http_dispatcher.py:423-458` 的 claim，因为 claim 不绑定 attempt。

独立审查用真实异常/close 路径复现队列从 1 个变为 2 个、retry_count=2；主线程已核对完整交错。这与“同一时刻只保留一个 retry claim”不同：claim 被新 attempt 合法清除后，旧 attempt 不应再拥有调用资格。

### FR-3：H2 第二个 HEADERS 一律报错，误伤 100 后最终响应与 trailers（高，R）

`http2_stream.py:288-319` 只要已有 `response_headers` 就抛 `ProtocolError("Too many header blocks")`。第一个 100 又会在 `353-368` 被当作响应交付。合法序列 `100 HEADERS → 200 HEADERS` 因此失败；`200 HEADERS → DATA → trailer HEADERS+END_STREAM` 也在正常收尾前失败。异常由 `http2_connection.py:250-256` 扩大为共享连接关闭。

主线程已复现第二个 END_HEADERS 稳定触发该异常。

### FR-4：单个 H2 RST_STREAM 关闭整条共享连接（中，R）

`http2_stream.py:272-277` 收到 RST_STREAM 后调用 `connection.close("RESET")`，而不是只终止当前 stream；`http2_connection.py:258-279` 随后关闭所有 stream。主线程复现单 stream reset 直接调用连接级 close。

### FR-5：H1 close 后仍接受 request，任务排在退出哨兵之后（中，R）

`http1.py:63-67` 不检查 `keep_running`/关闭状态。dispatcher 在 `http_dispatcher.py:480-505` 选中 worker 与调用 request 之间存在窗口。主线程构造已放入 None 的关闭队列后调用真实 `request()`，得到 `[None, task]`，任务没有 retry 或 finish。

### FR-6：独立 Client 将无 Content-Length 的 200 直接返回为空正文（中，R）

`simple_http_client.py:609-614` 将缺失长度默认成 0，未读取合法 EOF 定界正文。socketpair 中 `HTTP/1.0 200 OK` 后的 `abc` 未消费，返回 `text=b''`、`eof=False`。连接也不能安全复用。

### FR-7：独立 Client 对所有非 200 在正文处理前返回（中，R）

`simple_http_client.py:581-583` 早于 `read_payload` 和正文分类直接返回。`404 + Content-Length:3 + abc` 复现为空 `text`，旧正文仍在缓冲区；后续复用可能把旧正文当作新状态行。`gae_proxy/local/proxy_handler.py:114-126` 是真实正文消费者之一。

### FR-8：独立 Client 各阶段重新使用完整 timeout（中，R）

`simple_http_client.py:567-570,610-613` 对每个 informational/final 头和正文重新使用 `self.timeout`，直连还在 `427-453` 固定使用 30 秒连接超时。独立审查以 120ms 调用预算复现约 250ms 仍成功；主线程已核对计时路径。应统一为调用级绝对期限，而不是逐阶段续期。

### 不升级为正式缺陷的两个 front 条目

- `Task.read(size)` 在已缓冲尾块不足 size 且收到终止哨兵时返回空并保留缓冲，通用 API 行为可疑；但已定位的合法消费者没有充分证明会触发，列低优先级 API 候选。
- `Task.read_all()` 收满 Content-Length 即设置 `response_complete=True`，与 H2“必须 END_STREAM”注释存在设计冲突；现有测试明确允许 worker finish 前完成，需先裁定消费者契约，不能擅自改断言。

## 四、X-Tunnel 代际、资源与调度

### X-1：上传取数、代际捕获与登记不在同一代际边界（高，R）

`proxy_session.py:975-998` 先 `get_send_data()`，再读取当前 `session_id`，最后只检查 `running`。事件门中，旧 A 数据取出后切换到 B，真实 `get_upload_data()` 将 `OLD-A` 以 B 的 session_id 和 transfer_no 登记。后续检查都看到的是 B，无法识别污染。

证据：`interaction-audit/xtunnel-cross-layer-probes.log`。

### X-2：处理器只保存 conn_id，同号新 Conn 可收到旧首包或被旧 stop（高，R）

`create_conn()` 在 `proxy_session.py:539-569` 只返回整数；`start_conn()/stop_conn()` 在 `574-589` 只按当前 `conn_list[conn_id]` 找对象。处理器在 `proxy_handler.py:214-230,278-294,321-335,385-395` 持有旧 ID 跨回复/首包阶段。reset 后 ID 可从 2 重新分配，旧处理器的 `left_buf` 会交给新 Conn。

主线程对当前同号 Conn 调用旧 ID 的真实 `start_conn()`，复现旧首包注入。已有 CREATE 帧的 `expected_conn` 保护覆盖不到这个调用方凭据缺口。

### X-3：首次 socket 注册失败时，新增清理实际跳过（中高，R）

`base_container.py:444-468` 只有注册成功才写入 `sock_conn_map`；失败后调用 `close_sock(expected_conn=conn)`。但 `close_sock()` 在 `505-516` 发现映射不存在便直接返回，原 Conn 不执行 peer-close/do_stop。主线程用真实方法复现首次注册失败后清理事件为空。

### X-4：停止发生在阻塞 CREATE 期间，迟到 socket 在清理后重新装回（中高，R）

`Conn.process_cmd()` 在 `base_container.py:798-860` 阻塞执行 `do_connect()`；`do_stop()` 在 `695-716` 看到 `self.sock is None` 时没有 socket 可关闭。连接随后成功，`859` 无条件赋给已停止且已从会话删除的 Conn，循环随即退出，留下无登记、无后续关闭者的 socket。

主线程事件门复现 `late_socket_installed=True`、`late_socket_closed=False`。

### X-5：旧 200 响应与旧 create_conn 仍可修改新代际调度状态（中，S）

`proxy_session.py:1091` 的早期代际检查与 `1234` 的最终检查之间，`1179-1210` 已更新服务器时间、`target_on_roads` 并唤醒 worker；最终拒绝交付不会回滚这些副作用。`create_conn()` 在 `561-565` 也无锁、无代际复核地增加当前 target。源码路径成立，本轮未做完整加密响应动态复现。

### X-6：reset_all_connections 与并发注册可丢失新映射（中高，S）

`base_container.py:518-523` 对旧快照逐一清理后，无 pipe 锁地清空整个当前 map 并替换 selector；`add_sock_event()` 可在清理期间登记新 Conn。新 Conn 不在旧快照中，不会被关闭，却会失去 selector 映射和事件。入口依赖 selector 异常，因此保留为静态完整竞态。

### X-7：ACK 已恢复，同代际 checker 仍按旧暂停结论 reset（中，R）

`check_backpressure_timeout()` 在 `proxy_session.py:190-200` 第二次持锁只比较 session_id，没有重新检查 `read_paused` 和 `read_paused_since`。主线程在两次锁之间模拟同代际 ACK 恢复，真实方法仍调用 reset。旧暂停结论也能误伤同代际随后发生的新暂停。

### X-8：模块停止后，旧 login_process 可重新启动局部 session（中，S）

`login_process()` 在 `proxy_session.py:1496-1524` 先保存 `session=g.session`，等待锁/请求后不再检查 `g.running` 或 `g.session is session`。`client.py:128-142` 停止并清空全局 session 后，旧线程仍可在 `1523-1524` 对已停止对象调用 `start()`。是否最终复活取决于登录请求结果，因此为静态关停竞态。

### X-9：get_data_lock 包住整个网络 roundtrip，默认 20 worker 实际串行（中，S）

`proxy_session.py:1002-1262` 从取数到网络请求、响应处理、异常 sleep 和 reset/login 都在普通 `get_data_lock` 内；`config.py:53-56` 默认创建 20 个 worker。实际在途请求最多为 1，`target_on_roads>1` 无法形成对应并发。它可能是历史保守设计，但与当前多 worker/on-road 调度语义不一致，需统一设计而不是简单删除锁。

## 建议的统一修复顺序

本轮只检查，不执行修复。后续不应逐条打补丁，而应围绕以下所有权边界组织修复：

1. **先阻断跨 attempt/跨代数据与控制权**：为 H1/H2 attempt 和 X-Tunnel 上传/Conn 调用方引入明确身份；所有迟到的发送、响应、retry、start/stop、调度副作用都核对身份。
2. **把回退交接视为一个事务**：转移旧读取器中的剩余字节，完整写入首包，成功后才把 socket 发布给 PipeSocks；失败时唯一所有者负责关闭。
3. **统一协议状态机**：H2 分别处理 informational、final headers、trailers、END_STREAM、RST；独立 Client 与 H1 共用正文分类、完整性和绝对期限规则。
4. **将 DNS 递归移出唯一接收线程**：响应解析只交付事件，递归查询由其他执行流完成；同时补齐来源、RR 所有者/别名链和地址族验证。
5. **让清理成为状态边界**：首次注册失败也要有可清理身份；停止后迟到资源必须关闭；清缓存、停模块要使旧 worker/login 失效。
6. **最后处理性能与统计**：在正确性回归保护下重新审视 get_data_lock、target_on_roads 与 pipe worker 生命周期，避免用扩大并发放大尚未修复的代际竞态。

## 证据文件

均位于 `.superpowers/sdd/2026-11-06-network-reliability-convergence/interaction-audit/`：

- `direct-first-fragment-partial-send.log`
- `unwrap-first-fragment-partial-send.log`
- `unwrap-buffered-body-loss.log`
- `dns-cross-layer-probes.log`
- `dns-cname-receiver-reentry.log`
- `front-cross-layer-probes.log`
- `xtunnel-cross-layer-probes.log`

这些日志使用临时替身、保留测试地址和伪造代际标识，不包含真实账号、token 或生产 session_id。

## 修复后复核记录

统一修复已按上述边界落实到当前工作树：Smart Router 完整首包与缓冲正文交接，front attempt 隔离和 H1/H2 协议状态，独立 Client 正文完整性与绝对期限，DNS 接收线程交付、响应归属和缓存 epoch，以及 X-Tunnel 上传/Conn 代际、资源清理、停止和调度副作用均有对应回归测试。SR-3、DNS-5、X-8 也在修复阶段由静态候选转为可控交错下的失败复现，再验证修复。

X-9 的原始静态判断被后续源码复核和测试否定：当前 `roundtrip_task()` 只在取数与发送前登记阶段持有 `get_data_lock`，实际 HTTP 请求在锁外。两个 worker 已在同一网络屏障同时会合并正常完成，因此没有为这项错误假设修改锁范围。DNS 全调用绝对期限和 `Task.read_all()` 完成语义仍属 C 级契约事项，本轮未擅自改变。

独立整体复审最终报告为 Critical 0、Important 4、Minor 2。主线程核实四项 Important 均有完整触发路径，并将“ACK 在最后复核与 reset 之间恢复仍可能误重置”提升为同一可靠性边界的必修项。相应修复包括：reset 在 pipe 锁外执行 Conn 回调；上传 worker 以捕获的代际约束取数，旧 worker 不得消费新代际发送缓冲区；内部登录成功后、启动 worker 前复核模块运行状态与当前 session；独立 Client 的多地址 TCP 和 TLS 握手共享绝对期限；背压 reset/stop 以暂停时间戳作为恢复复核凭据。六项新增测试均先在旧实现准确失败，再在修复后通过。

另一项 Minor 涉及旧 roundtrip 的真实流量和全局 quota 记账。旧请求确实消耗了账号网络流量，是否因代际变化回滚全局 quota 需要独立的记账契约裁定，本轮不为了让统计好看而隐去真实消耗，故保留为暂缓项。

最终离线验证采用每个测试文件独立进程，避免既有测试对 `threading.Thread`、`xlog`、`six` 等全局模块的替身串扰。33 个 `tests/test_*.py` 文件全部通过，其中 262 项 `unittest` 用例和 11 个独立脚本均成功；DNS 三组竞态重复 20 轮，front/Smart Router 交接及 X-Tunnel/PipeSocks 两组分别重复 10 轮通过，最终日志未出现 Traceback、后台线程异常或失败标记。22 个修改及新增 Python 文件通过 `py_compile`，`git diff --check` 通过，HEAD 保持在 `5dc3f12`。以上仅证明当前代码与离线可控场景收敛，不等于生产间歇故障已被永久消除；本阶段未部署、未重启、未提交。
