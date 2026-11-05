# XX-Net 网络可靠性第二轮深度审计

日期：2026-11-06
起点：`ea4164b`，生产进程 PID 15161（2026-11-05 20:52:27 启动）。
原则：先保障代理数据面的正确性和可靠性，再讨论效率、性能与代码优雅度；不通过放大资源上限、延长超时或隐藏日志来制造“改善”。

## 审计方法

1. **意图与实现对照**：以系统架构文档、可靠性路线图和上一轮回归报告中的设计约束为意图，逐条定位真实执行代码。
2. **状态机审查**：围绕 Conn、front worker、DNS 查询和 Direct 候选分别列出创建、启动、读写、暂停、恢复、停止、异常和跨代复用状态。
3. **资源所有权核算**：每个本地/远端 socket、Conn、worker、缓冲字节和线程必须有唯一清理责任；异常和竞争路径不能重复关闭或遗失所有权。
4. **对抗时序复现**：在同一输入、时钟和故障序列下比较预期与实际，覆盖慢首包、旧 stop 延迟、DNS 乱序返回、front 超时、body 截断、黑洞 IP 和背压恢复。
5. **跨模块影响评估**：任何修复必须同时检查首包顺序、资源上限、重试幂等、front 隔离和生产可观测性，不做互相抵消的单点调参。
6. **分层验收**：目标测试 RED→GREEN、关联测试、全套、重复/模型测试、文件部署哈希、新进程正常请求、分进程日志窗口依次通过；测试与短时正常不等于长期治愈。

## 必须保持的网络不变量

| 边界 | 不变量 | 主要证伪方法 |
|---|---|---|
| 本地 Conn 生命周期 | 创建后所有出口都能清理同一对象；启动前不 READ，启动后不错过恢复 | 创建/回复/首包异常，旧 stop 与同 ID 新 Conn 交错 |
| 字节顺序 | `left_buf` 先于后续 socket 数据；重传和乱序不重复交付 | 首包与 READ 竞争、重复 SN、乱序 SN |
| ACK 与背压 | 未确认内存有界；暂停后恢复或按预算 reset；无死锁 | ACK 停滞、恢复、Conn 删除、锁重入 |
| front 请求 | 选路、等待、重试、header/body 共享总 deadline；worker 和连接不泄漏 | 慢 front、空 front、响应中途失败、重试交错 |
| DNS | 每次调用收到自己的有效结果；共享缓存只接受最新 generation；异常不阻塞后续查询 | 同 key 并发、迟到、空结果、TTL、别名循环 |
| Direct | 有界并发下仍能覆盖健康候选；winner 唯一，loser/取消 socket 全关闭 | 前序黑洞、慢健康地址、返回后迟到连接 |
| Smart Router 交界 | 通道回退不重复搬运首包；失败 socket 有唯一所有者 | Direct→X-Tunnel 回退、启动异常、本地提前断开 |
| 可观测性 | 日志节流不改变网络行为；统计按进程窗口和独立传输口径 | 新旧日志口径对照、脱敏、tid/session 边界 |

## 基线

27 个 `tests/test_*.py` 中 26 个通过，共报告 124 项测试。唯一失败仍是 `test_ipv6_filter.py` 的生产文件耦合与配置替身缺字段，不能当作生产网络回归证据。完整输出保存在 `.superpowers/sdd/2026-11-06-network-reliability-audit/baseline/`。

本轮并行审查 X-Tunnel 会话、front 请求/重试、DNS/Direct/Smart Router 三个区域；主线程负责生产窗口、跨模块整合、独立复现和修复，子审查结论必须经源码与测试核实后才采用。

## 已确认结果

### 1. 协议行/头读取在 EOF 后继续轮询（确认缺陷，已离线修复）

Smart Router 与 X-Tunnel 的 `read_null_end_line()`、`read_crlf_line()`、`read_headers()` 都把 `recv()` 返回的 `b""` 直接追加到缓冲区并继续循环；只有 `read_bytes()` 原来会把它视为失败。客户端在 SOCKS4 用户字段、SOCKS4a 域名或 HTTP 头发送途中正常断开时，处理线程可能持续空转，无法走到外层关闭逻辑。

新增 `tests/test_proxy_protocol_eof.py`，使用两个真实 handler 的六个子场景：首次 recv 返回 EOF，若实现继续读取则留下第二次调用证据。修复前六个场景全部 RED；在六个循环中统一将空读取转为 `socket.error` 后全部 GREEN，X-Tunnel 生命周期 16/16 保持通过。该修复只改变异常退出，不改变正常协议字节或网络超时参数。

### 2. 上一轮修复后仍有 front 故障簇（生产事实，原因待查）

PID 15161 的过滤脱敏长窗口中仍可见 11 行 `get response timeout`、23 行 601、21 行 602 和 7 行 504；无 Traceback、启动清理异常、背压 reset 或 603。front 诊断关联到 19 个 tid，Seley/TLS Relay 的 17 条请求记录都约为 30 秒。事件主要集中在 21:35–21:41，而不是均匀分布；这支持共享故障簇，但不能据此区分外部线路、远端服务和本地调度缺陷。

因此本轮继续保持 front deadline、worker 回收、重试和资源所有权为重点审查对象，不把上一轮的正常样本当作已经收敛。

## 后续结果

### 3. PipeSocks 丢失条件变量通知（确认缺陷，已离线修复）

`pipe_socks.py` 原先在条件锁外检查读/写集合，随后才进入锁内 `wait()`；另一线程可在两步之间清空集合并完成通知，使转发线程睡过一个实际事件。修复后，谓词检查和等待在同一条件锁内完成。可控交错测试修复后重复运行 10/10 通过；首次 GREEN 时线程仍需等待 selector 的正常 5 秒退出，测试清理改为 `join(6)`，未改生产停止逻辑掩盖该等待。

### 4. DNS 分帧、地址族、查询类型与缓存并发（确认缺陷，已离线修复）

`tests/test_dns_transport.py` 使用开发快照中的真实 DNS 实现，覆盖 10 个场景，均完成 RED→GREEN：

- TCP/TLS 的两字节大端长度前缀和内容都可分片到达；截断 EOF 不得继续空读。
- EOF、timeout、非法 DNS payload 和 DoH 请求失败必须关闭已取出的 socket/client；只有成功才归还连接池。
- IPv6 UDP `recvfrom()` 的四元组按 `(host, port)` 提取，正常结果可以交付。
- `LocalDnsQuery` 使用调用方传入的 timeout，而不是用实例默认值覆盖。
- blocked/white 后备查询保留 AAAA 类型；IPv6 Direct 使用 28（AAAA），不再误用 6（SOA）。

`DomainRecords.get_ips()` 原先在锁外迭代 set，而 `set_ips()` 可在另一线程修改同一 set。`tests/test_domain_records_concurrency.py` 用真实 RLock 和可控 GatedSet 在迭代器创建后插入写操作，修复前确定性得到 `RuntimeError: Set changed size during iteration`；把读取与写入口纳入同一把锁后通过。

### 5. X-Tunnel 会话与 Conn 跨代状态（确认缺陷，已离线修复）

在 `tests/test_x_tunnel_memory_backpressure.py` 中逐项复现并修复：

- SN 超时重传列表因未比较 `retry_time` 而永远为空；现在首次到期立即请求重发，随后按重试时间节流。
- WaitQueue 停止后仍接受新 waiter；现在停止会释放已有 waiter 并拒绝新等待，restart 同时重建已停止的队列。
- 远端 DATA 可在本地 Conn `start()` 前注册 WRITE；现在启动前不冲刷，启动后按序处理已有队列。
- 旧 `/data` 响应可删除并处理新会话的同号 transfer；现在在请求结束计数后先排除已换代的响应，最终交付又在会话锁内同时复核 `session_id` 和 transfer 对象身份，旧响应不能删除新 transfer 或向复用 conn ID 投递旧 payload。
- 正常 start 前 timeout checker 已退出，restart 又可能遇到 alive-but-exiting 的旧线程；现在每次 start 都创建新 checker，旧 checker 通过线程身份识别退出，不再依赖可能瞬时仍为 True 的 `is_alive()`。
- `send_conn_data()` 原先只在锁外检查 running，停止清理后等待锁的旧线程仍可把帧重新入队；现在锁内再次检查，停止后的帧不进入新队列。

旧的日志节流契约测试 X8 已同步为“旧响应整体丢弃”，而不是继续按旧会话记账；测试桩补齐 restart 所需的 running/is_alive 接口。该改动来自明确的新生命周期契约，不是通过修改断言掩盖随机失败。

### 6. X-Tunnel 阻塞写与 selector worker 退出交接（确认缺陷，已离线修复）

本地 accepted socket 在协议回复后仍保持阻塞模式，`Conn.send_to_sock()` 又循环到全部数据写完；慢本地消费者可能因此卡住唯一 ConnectionPipe，波及其他 Conn 的读写和 ACK。修复后，本地 Conn 在 `start()` 切换到非阻塞，目标直连 socket 在 connect 成功后同样切换；`BlockingIOError` 只保存未写余量并注册 WRITE，不再被当成连接失败关闭。`sended_position` 只累计真实写入字节。

另一个可控时序复现表明：旧 pipe worker 已因空 map 退出主循环时，新连接可在收尾期间注册；旧 worker 随后清空 map 并把线程句柄置空，导致新连接失去事件处理。现在收尾与注册由同一把锁串行化；若收尾前已有新连接则显式启动接替 worker，否则清完句柄后由注册方启动新 worker。目标竞态测试修复后重复 10/10 通过。

### 7. front 过期任务、响应完整性与总期限（确认缺陷，已离线修复）

`tests/test_front_body_deadline.py` 与 `tests/test_front_request_deadline.py` 共同覆盖以下边界：

- dispatcher 在取任务后和取得 worker 后都检查 monotonic deadline，已过期任务不再交给 worker。
- Task 新增显式 `response_complete`；只有收满已知 Content-Length、HTTP/2 正常 `end stream` 或 HTTP/1 完整读取才标记完整，异常 close、timeout 和截断均不标记。已知长度由真实 `read_all()` 在收满时标记，避免最后一块数据已入队、worker 尚未 finish 的瞬时窗口误报截断。
- HTTP/1 Content-Length 不足时在 `close("down fail")` 后立即返回，不再继续执行 finish/idle 成功收尾。
- HTTP/1 `response.begin()` 和每次 body read 都按原始绝对 deadline 重算剩余时间；发送与排队已消耗的预算不会被响应头或后续分片重新使用，多个分别未超时的分片也不能累计越过任务总预算。
- 上层收到不完整 200 时优先重试；无 Content-Length 也可通过任务状态识别截断。预算耗尽时返回内部 603，不交付半截内容和原 200。
- `close_all_worker()` 同时关闭忙和空闲 worker，不再清空列表却遗漏正在处理的连接。

### 8. 独立整组审查补足的三个竞态（确认缺陷，已离线修复）

独立只读审查在首组修复之上发现三个原测试未覆盖的窗口，均已重新定级并完成 RED→GREEN：

- **旧响应检查后的 TOCTOU**：原测试只覆盖 HTTP 返回前换代；新增可控 `g.stat` 交错，在通过首次代际检查后才换成新 session 和同号 transfer。修复前新 transfer 被删除且旧响应进入处理；修复后最终交付在会话锁内复核代际和对象身份，新状态保持不变。
- **完整正文瞬时误判**：真实 `Task.read_all()` 收满 Content-Length 后、worker 尚未 finish 时，原先 `response_complete` 仍为 False，可能导致多余重试或 603。新增真实 Task 测试修复前失败；现在收满已知长度即标记正文完整，不等待 worker 的收尾调度。
- **checker alive-but-exiting**：原 restart 只看 `is_alive()`，旧线程可能尚未退出但已不会继续循环。新增可控旧线程替身修复前失败；现在每次 start 更换 checker，旧线程按身份退出，新会话不依赖旧线程存活状态。

修复后 front 底层 10/10、上层 8/8，X-Tunnel 28/28，日志契约全部通过；front 与 X-Tunnel 关联套件重复 5/5。完整 31 文件套件再次全部退出 0，证据为 `full-suite-final-post-review.log`。

随后按文档不变量补查 HTTP/1 响应头路径，确认 `response.begin()` 仍使用原始 timeout。新增发送耗时 0.06 秒、总预算 0.1 秒的可控时钟测试，修复前实际 begin 预算为 0.1 而非 0.04；改为剩余预算后 11/11 通过。最终完整套件见 `full-suite-final-convergence.log`，31 个文件全部退出 0。

### 9. 仍需保留的边界

以下候选项尚未全部取得独立复现证据，不能写成已确认生产根因：HTTP/2 并发 close 的重复重试、front dispatcher 初始化竞争、DNS ParallelQuery 调用超时后的后台工作、Direct 对上游代理地址族和返回地址族的约束、握手行/头大小上限及首包路径的接收窗口细节。它们应在后续有明确触发路径时单独 TDD，不与本轮已验证修复混为一谈。

本轮离线结果只能证明对应代码路径的缺陷和修复有效，不能宣称 21:35–21:41 的生产故障簇已经治愈；最终结论仍以部署哈希、新进程和分窗口生产日志为准。

## 部署与生产验收

- 最终运行代码提交：`c85180936214a54067d3a5f48560d149c5ca4345`（包含前一提交 `6bd1838`）；未 push、未合并 main、未创建 PR。
- 备份目录：`backups/pre-rsync-20261106-network-reliability/`。14 个生产目标在首次写入前均与 `6bd1838^` 基线一致；保存 `.orig`、`.diff`、源码/备份/部署 SHA256 和来源提交。响应头期限修复后仅增量替换 `http1.py`，并把同一清单推进到最终提交。
- 14 个文件最终全部与 `c851809` 源码逐字节一致；未部署配置、日志或数据。黑盒 A22 已加入本轮累计部署清单，34 个受管生产文件 provenance 通过。
- 旧 PID 15161 正常 SIGTERM 退出；中间 PID 34559 用于验收 `6bd1838`，加入响应头修复后也正常退出。最终 PID 35330 于 2026-11-06 01:33:23 启动，8085/8086 均由该进程监听；后续只统计最终进程窗口。
- `production-blackbox-c851809.log`：最终真实黑盒 10/10，通过二进制/启动脚本、回滚基线、部署 provenance、配置契约、48 个并发控制面请求、各模块日志视图和正常代理 204。
- `production-immediate-35330.json`：10 个正常 HTTP 与 2 个正常 HTTPS `generate_204` 请求全部返回 204；新进程即时窗口的 front timeout、601/602/603、504、响应截断、Conn 清理失败、背压 reset 和 Traceback 均为 0。
- 最终即时资源快照：RSS 174784 KiB、122 个 ps 线程条目、225 个 lsof 条目。该快照包含启动与请求后的 worker 初始化，只作为同进程后续对照，不与旧 PID 直接作泄漏同比。
- `production-interval-requests-35330.json`：01:37:01–01:38:45 的 6 次间隔 HTTPS 正常请求全部返回 204。
- `production-five-minute-35330.json`：最终进程 341.7 秒窗口内，front timeout、601/602/603、504、响应长度不符/不完整、Conn 创建/启动清理失败、背压 reset 和 Traceback 均为 0。RSS 150000 KiB、109 个 ps 线程条目、231 个 lsof 条目；相对即时快照 RSS/线程未增长，FD 增加 6 条，短窗口不作长期泄漏结论。

## 本轮收敛结论

本轮确认的协议 EOF、DNS 分帧与所有权、地址族、共享缓存并发、Conn/Session 跨代竞态、非阻塞写、pipe worker 交接、front 完整性及完整请求期限缺陷均已取得对应 RED→GREEN 证据；独立审查发现的三处深层竞态也已修复。最终 31 个测试文件全部通过，14 个运行文件与 `c851809` 一致，新进程黑盒和五分钟正常请求样本通过。

因此本轮可以结束主动代码迭代：没有仍未处理的已复现 Critical/Important 缺陷。剩余候选项保持为需要新触发证据的边界，不靠继续调参或扩大修改制造“收敛”。五分钟零警告只代表当前样本，旧进程曾出现的集中公网故障簇仍需更长自然使用窗口验证，不能宣称永久治愈。
