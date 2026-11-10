# 网络可靠性第三轮质检与修复收敛设计

日期：2026-11-06

## 目标与边界

将前两轮质检确认的 DNS、front 和 X-Tunnel 缺陷合并为一轮统一修复。目标是保证字节正确性、任务唯一性、会话代际隔离、共享期限和资源可清理性，而不是通过放大超时、隐藏日志或降低断言来制造收敛。

本轮授权覆盖仓库内设计、测试和代码修改，以及离线故障替身和生产只读检查；不包含部署、重启、git 提交、push、合并 main 或创建 PR。生产进程继续运行既有版本，不能用离线修复结果宣称生产已更新。

## 第三轮质检结果与证据等级

第三轮增量基线的完整套件为 31/31 个测试文件退出 0，关键关联测试重复 80/80，生产只读黑盒 10/10；日志目录为 `/tmp/xxnet-iteration-qa.XYQ7w3`。这些结果说明当前常规路径可用，但不能覆盖下列特定交错时序和协议边界。

下列条目均已由主线程用真实源码和可控替身复现，或逐行核实到明确缺陷路径。独立审查只作为补充线索；其中重要意见已经主线程核对并转化为设计约束。静态候选、离线复现和生产日志关联不等于已经证明某个公网故障的唯一根因。

### DNS 查询所有权与响应匹配

- UDP 的 16 位 ID 占用检查与 waiter 登记不是原子操作；两个并发查询可共用 ID，后登记者覆盖先登记者。真实代码复现结果为一份答案只有一个查询成功。
- 活跃 waiter 使用会按容量淘汰的 LRU，且接收和超时清理没有按 waiter 对象身份执行；旧清理可能删除后来复用同 ID 的新查询。
- UDP、TCP 和 DoH 没有统一核对响应 ID、QR 和 question；UDP 还未按 A/AAAA 等查询语义筛选答案。多 UDP 服务器发送后仍重新等待完整 timeout，会突破调用方期限。
- TCP/DoH 校验失败时，已取出的连接/client 必须关闭，不能归还连接池。

### front 响应、任务状态与连接生命周期

- HTTP/1 无 Content-Length 响应在零剩余预算、无缓冲数据时得到空读取，worker 将其当成完整 EOF；真实 EOF 定界响应完成后又会错误进入 idle 复用。
- 固定长度 HTTP/1 响应没有限制单次读取长度，已复现声明 3 字节却交付 4 字节；短缺、恰好收满和超长必须分别处理。
- HEAD、204、304 的合法无正文响应仍进入 body read；上层 `front_dispatcher` 又无条件比较 Content-Length，会抵消底层修复。
- chunked 零块后没有逐行消费 trailer；真实 Response 已复现 trailer 留到下一次响应。EOF 和 chunked 正常结束都需要粘滞状态，不能用一次空读取代替。
- HTTP/2 END_STREAM 未核对声明 Content-Length 与接收侧 `task.body_len`；DATA 也未遵守无正文语义。
- `Task.finish()` 的检查、状态更新和哨兵投递不是原子操作；迟到收尾不得改变已终止任务。混合 `read()`/`read_all()` 已复现 5 字节被累计为 8。
- HTTP/2 `request()` 与 `close()` 可交错，关闭完成后仍创建 stream；两个 stream 引用同一 Task 时可重复 retry。
- HTTP/2 stream 删除即使 KeyError 也增加完成任务计数；`Stream.close()` 和删除必须幂等。
- `simple_http_client.begin()` 最低追加 0.1 秒；HTTP/1 发送循环仍使用 socket 自有超时，没有按任务绝对期限约束。
- HTTP/2 `check_timeout()` 没有实际调用方，且只 finish Task、不注销 stream。共享 socket 的帧收发没有单 Task 上下文，禁止把某个 Task 的期限直接设成整条连接的 socket timeout，也禁止因单个 stream 到期关闭其他正常 stream。

### X-Tunnel 会话代际与对象所有权

- 上传路径只检查 transfer 编号存在，并使用当前 `self.session_id` 打包旧 `send_data`；已复现旧负载携带新会话编号并修改同号新 transfer。旧请求 finally 也不得递减新代际的 `on_road_num`，新代际开始时应清理旧代际在途计数。
- 旧 521 响应可停止新会话并触发 login；error_code=1、error_code=3 和背压 checker 也有跨代 stop/reset 路径。旧 521 的全局 `server_host` 清理和 login 必须一起受代际保护。
- `pipe_worker()` 已取出的旧事件没有 Conn 身份校验；已复现旧 Conn 字节进入新会话。数据、ACK、peer-close 和 “exceed the max connection” reset 都必须验证 Conn 所有权。
- `add_sock_event()` 持有 pipe 锁时的异常清理会请求会话锁，而 `start_conn()` 以相反顺序获取锁；已复现双向等待。注册失败先记录，退出 pipe 锁后再按原 Conn 条件清理。
- roundtrip/pipe worker 和 checker 的 finally 删除与 start 的存在性检查可能交错；退出和清理必须同时核对当前线程身份，start 必须为当前代际建立接替者。

## 必须保持的不变量

1. **DNS 一次查询一个所有者**：ID 分配和 waiter 登记原子完成；响应匹配 ID、QR、规范化 qname、qtype 和答案语义；超时清理按对象身份执行。
2. **响应完整性可证明**：已知长度必须恰好收满；未知长度只能由真实 EOF 证明完整；chunked 必须消费到 trailer 终止空行；零期限不是 EOF。
3. **无正文不等于任意空响应**：HEAD、204、304 可以直接完成。100 等 informational response 后仍读取最终响应；101 不按普通可复用空响应处理。
4. **任务终止与重试唯一**：finish 只投递一个终止哨兵；同一 Task 同时最多一个待处理重试；worker 在接受任务的同一状态边界内清除上一 retry claim，dispatcher 不在出队时提前清除。
5. **worker 关闭后不接受新任务**：HTTP/2 状态检查、stream 登记和 close 状态翻转串行化；重试、finish 和回调在锁外执行，不能用非重入 `_lock` 包住整个 close。
6. **会话操作属于同一代际**：旧上传保留旧会话编号且不得修改新 transfer；旧响应不得 stop/reset/login 新会话；旧 Conn 不得提交数据或控制帧。
7. **清理按对象身份执行**：旧 worker、旧 checker、旧 Conn 的收尾只能删除自己，不得删除同 ID 的新对象。
8. **锁顺序单一**：会话锁到管道锁的路径存在时，管道锁临界区内不得反向请求会话锁；异常清理在释放管道锁后执行。
9. **共享绝对期限**：单请求的排队、发送、状态行、响应头和正文共享预算；HTTP/2 用 per-stream 到期调度/取消和过期帧过滤，不让一个 stream 的期限波及共享连接上的其他 stream。

## 修复结构

### DNS

活跃 waiter 改为普通 dict 和专用锁，在同一临界区完成“选择空 ID + 登记”。接收和 finally 使用 `waiters.get(id) is waiter` 后条件删除。统一响应匹配 helper 核对 QR、ID、唯一 question、忽略大小写和尾点的 qname、qtype；A/AAAA 只收对应地址记录，CNAME 递归和 NS 语义保留。UDP 等待只使用剩余期限；TCP/DoH 匹配失败关闭连接/client。

### front

在 `simple_http_client` 集中定义最终响应的无正文判定和粘滞 EOF/chunked 完成状态。`Response.begin()` 不追加最低预算，缓冲数据足够时零等待也可解析。HTTP/1 按无正文、chunked、固定长度、EOF 定界四类处理，发送使用任务剩余期限；EOF 定界连接关闭而不复用。

HTTP/2 在正常 END_STREAM 时按无正文语义或接收侧 `body_len` 判定完整性；连接检查器实际执行 per-stream 超时并注销 stream。发送队列过滤已过期 stream 的帧，但不把单 stream 期限施加到共享 socket。

Task 用独立可重入状态锁保护 finish、responsed 和 retry claim。H1/H2 worker 在确认接受任务、尚未暴露给工作循环或 stream 回调的临界区内清除 retry pending；拒绝接受时保留重试能力。HTTP/2 使用专用可重入状态锁串行化 request/close，只锁状态翻转、登记和快照；stream close/retry/finish/回调在锁外执行。

### X-Tunnel

以上传任务捕获的 `request_session_id` 打包，状态修改前在同一会话锁内复核代际和 transfer 对象身份；on_road 增减仅属于当前代际。stop/reset 接受预期会话编号或 Conn，并在锁内验证后执行；旧 521 的全局清理、stop 和 login 作为同一受保护控制流程。

Conn 向会话提交 data/ack/close 时携带自身引用，会话锁内验证 `conn_list[conn_id] is conn`。selector 最终交付和异常清理都按 Conn 身份复核。worker/checker 以当前线程身份退出和清理，start 总是为当前代际创建新 worker。管道注册异常在锁外清理。

## 验收与收敛标准

- 每个已复现缺陷先有准确 RED，再做最小修复得到 GREEN；不得修改生产代码后才补测试。
- 关联测试、完整 `tests/test_*.py` 文件套件、关键竞态至少十轮重复均通过。完整套件中硬编码读取 `/Applications/XX-Net` 的测试只能证明生产旧代码兼容，必须另跑直接加载 `Coding/` 的关联测试作为本地修复证据。
- 新增正反例：完整响应不受影响；合法无正文和 informational 后续最终响应正常；真实 EOF 仍成功但不复用；正常重试和下一 attempt 重试不被吞；新会话接受新数据；旧对象不能清理新对象。
- 独立整组审查只作为候选来源，重要发现由主线程复核并补测试。
- 最终语法检查、`git diff --check` 和工作树范围核对通过；不自动提交。未部署、未重启，生产只读结果单独报告。
- “代码收敛”表示本轮已确认本地缺陷均有回归证据且独立复审无未处理高优先级问题；不宣称公网间歇故障永久治愈。

## 本地验证证据

- 最终代码的完整 `tests/test_*.py` 套件逐文件执行，32/32 个文件退出 0；结果汇总保存在 `.superpowers/sdd/2026-11-06-network-reliability-convergence/full-tests-converged/summary.txt`，未扫描到未处理的后台线程异常。
- `test_blockio_ratelimit.py`、`test_dns_dot.py`、`test_dns_nameserver_filter.py`、`test_ipv6_filter.py` 和 `test_try_add_remove_races.py` 硬编码加载 `/Applications/XX-Net`，仅作为生产旧代码兼容证据；直接加载 `Coding/` 的 DNS、front、X-Tunnel 关联测试另行通过。
- DNS/front 关键关联套件连续 20/20 轮通过；front 45 项测试连续 50/50 轮通过；X-Tunnel 43 项测试连续 20/20 轮通过，异常扫描均为空。
- 独立整组复审发现的 8 项漏口均经主线程核实，并逐项取得准确 RED 后最小修复至 GREEN：H2 登记前清除 retry claim、过期帧返还窗口、H1/H2 原子响应交付、独立 Client 的 100/101 语义、CREATE 的 Conn 身份、旧 READ/WRITE 注销保护、运行中 worker 的接替者、旧 checker 的代际复核。最终 front 45 项和 X-Tunnel 43 项关联测试通过。
- 所有修改和新增 Python 文件最终均通过 `py_compile`，`git diff --check` 无输出；工作树修改限定于本轮网络可靠性代码、测试、设计和计划，HEAD 保持 `5dc3f12a6d9561e6722f5496bdb53af1d76dea6c`。
- 验证过程中额外复现并修复“停止发生在发送等待期间仍登记上传任务”的竞态；旧 DNS 退避测试仅同步新增 `waiters_lock` 夹具字段，没有放宽断言。101 测试替身最初未设置 `client.sock`，已修正并取得真实 RED 后才修改生产代码。
- 本轮未部署、未重启、未提交；生产进程仍运行旧代码。以上为本地离线收敛证据，不代表公网间歇故障已经永久消失。
