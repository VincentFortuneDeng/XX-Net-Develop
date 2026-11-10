# XX-Net 设计意图、功能契约与变更损益审计报告

日期：2026-11-09
分支：`fix/h-log-dns-backoff`
冻结 HEAD：`5dc3f12a6d9561e6722f5496bdb53af1d76dea6c`

## 1. 结论

**已覆盖模型内没有未处理高优先级项。**

本轮以“功能契约—变化意图—执行流—风险—双向证据—裁定”为闭环，而不是只扫描锁或只运行既有测试。审计确认了 15 项可确定性复现的真实产品缺陷，均完成准确 RED、最小修复、目标与相邻 GREEN，并通过完整离线套件、正式十轮复验和条件式生产部署验收。

这一结论不代表未知问题或间歇故障永久消失，也不代表真实性能、资源占用或所有外部平台已经得到证明。15 项确认缺陷也不应全部归因为 2026-10-27 之后的优化；其中包含上游或当前代码中的既有缺陷，具体来源以变化账本中的冻结提交、hunk 和 revision 为准。

## 2. 审计范围与覆盖分母

- 主行为基线：`68d8506`；已提交范围：`68d8506..5dc3f12`。
- 日期边界：`f1aa5bc..68d8506`，2026-10-27 当天也纳入边界核查。
- 根快照：`f1aa5bc`，并与 `upstream/XX-Net-5.16.7` 对照。
- 产品范围：整个 `Coding/XX-Net-5.16.7.1/`，包括安装、启动、控制面、front、Smart Router、X-Tunnel、DNS、配置和日志支持路径。
- 最终分母：966 个产品 hunk、958 个逻辑变化、49 个 hunk 涉及路径、626 个辅助 hunk、8 个核心流程、41 个测试条目。
- 变化状态：25 项 `fixed`、933 项 `audited-no-issue`；无 `pending`，无 Minor deferred。
- 风险状态：15 项均为真实代码确定性复现的 R 级证据并修复，其中 13 项 Important、2 项 Minor；无未处理 Critical/Important，无未关闭的 S 级高影响项。
- 最终快照：480 个产品文件和 41 个测试文件；41 个测试条目中 38 个为离线验收项，3 个非离线项未被混入离线结果。

独立 QA 另行重建了 36 个历史提交的 392 个产品 hunk，与账本一致。QA 未发现 Critical；其提出的 reporter Important/S 候选经主线程核实后成为真实 R 级缺陷并修复。

## 3. 已确认并修复的产品问题

| 风险 | 问题 | 修复与平衡边界 |
|---|---|---|
| `RISK-FRONT-001` | H2 SETTINGS 可在 HEADERS 编码中并发修改同一 HPACK encoder | 将编码、头块入队和表大小更新纳入一致协议顺序；未把网络 I/O 放入头锁 |
| `RISK-FRONT-002` | 独立 Client 在代理 connect 或 TLS wrap 失败时泄漏新 socket | 只回收尚未交接的局部 socket；正常 keep-alive 二次复用保持 |
| `RISK-FRONT-003` | 成功切换 host/port/TLS 后旧 socket 未关闭 | 新连接成功交接后关闭旧 socket；不提前破坏可用旧连接 |
| `RISK-FRONT-004` | 两个线程可同时越过锁外空闲判断并创建重复 supervisor | 锁内复核唯一所有权；结束后仍能重启，不改变连接预算 |
| `RISK-FRONT-005` | IpManager 首次阈值移除与后台重试分支倒置 | 恢复既定 recheck 状态机，保留五秒复查、成功恢复和代际退出 |
| `RISK-SMART-001` | Redirect HTTPS 首包可能只写入部分字节，wrap/send 失败泄漏远端 | 使用 `sendall`；在 PipeSocks 接管前按所有权回收，回退语义不变 |
| `RISK-XTUNNEL-001` | Pipe 快速重启后，旧已关闭 socket 仍留在 selector | 重启时在 Pipe 锁内注销旧项；旧清理不得删除新 Conn |
| `RISK-XTUNNEL-002` | 同代较慢的旧余额响应可覆盖较新请求 | 用独立 `balance_lock` 串行完整事务；不改变 quota、回滚或公共签名 |
| `RISK-LIFECYCLE-001` | 启动中停止可留下无注册表登记的 ready 孤儿模块 | 同模块 start/stop/发布使用持久 RLock；不同模块不全局串行 |
| `RISK-LIFECYCLE-002` | 配置广播遍历活字典时，并发 stop 可导致字典大小变化异常 | 固定单次调用快照；不把快照升级为跨模块原子配置事务 |
| `RISK-LIFECYCLE-003`（Minor） | 半启动 Web 停止对 `None` 调用 `shutdown` | 仅增加空状态防护；正常 server 停止路径不变 |
| `RISK-LIFECYCLE-004`（Minor） | 日志 reset/keep 与并发 getLogger 相互打断 | 使用 logger 条目快照；不追补后创建对象，也不虚构同名首次创建事务 |
| `RISK-XTUNNEL-003` | 已通过身份检查的余额提交可穿过真实 `client.stop()` 污染新代 | Session 停止/清除与余额事务共用换代锁；停止可等待在途请求的既有期限 |
| `RISK-LIFECYCLE-005` | X-Tunnel 开关不刷新 Smart Router，停用后还可能保留旧引用和端口 | 开关后重启已启用 Smart Router，缺失模块时清空引用；未启用时不做无意义重启 |
| `RISK-XTUNNEL-004` | reporter 在首次身份检查后暂停，可在换代后把旧 IP 集提交给新 front | 锁内复核 Session 对象和 generation 后提交；`set_ips()` 串行化代价明确保留 |

每项的准确 RED、最终 GREEN、正常路径、目标异常、相邻异常和兼容面见[风险登记册](../.superpowers/sdd/2026-11-08-design-intent-change-audit/risks.md)及[损益矩阵](../.superpowers/sdd/2026-11-08-design-intent-change-audit/balance.md)。失败夹具和无效运行均保留，没有被后续 GREEN 隐去。

## 4. 关键平衡点与设计裁定

本轮没有为消除单一风险而牺牲另一层既有契约，主要平衡点如下：

1. **DNS**：保持全调用总期限、NS 非全局 single-flight 和旧调用结果交付；缓存提交继续受 generation、缓存对象身份和 epoch 限制。
2. **Task/传输**：保持 `Task.read_all()` 完成语义、`body_queue` 背压和同 transfer 多 attempt 的协议身份；网络重试不承诺 exactly-once。
3. **quota**：没有新增 quota 回滚或账户事务语义；余额串行与换代只保护既有提交边界。
4. **停止**：保留第三方不可中断调用和无期限 join 的既有边界；当前 macOS socket 正例不能外推到所有平台或自定义 handler。
5. **模块控制**：`call_each_module()` 的快照只固定本次调用集合，不构成跨模块原子配置事务。
6. **Smart Router 刷新**：以中断并重建当前连接、DNS/代理监听为代价刷新 X-Tunnel 引用；未启用时不重启。
7. **余额和 reporter 换代**：复用 `balance_lock`，避免新增锁和反向锁顺序；代价是停止/余额等待在途事务，reporter 的 HostManager 更新和触发建连也与二者串行。
8. **日志**：保留有界队列和满时丢弃计数；512 个 throttle 键不是严格容量上限，快照不追补后创建 logger。

所有性能与资源项目均保持 **INCONCLUSIVE（收益未证实）**：本轮证明了功能与并发正确性，但没有真实生产 fd、线程、内存、吞吐、时延或故障率窗口。一次或多次 204 不能被解释为性能提升。

## 5. 独立 QA 与离线验收

### 5.1 验收工具纠偏

任务七还确认并修正了四类验收工具问题：五个离线脚本误加载 `/Applications/XX-Net`、场景脚本误标 unittest、运行器未核实实际测试哈希，以及结果缺少显式 `retry_count`。这些均先取得工具级 RED 再修复；生产黑盒路径是有意保留的唯一例外，且未加入离线套件。

### 5.2 正式结果

- 带显式重试字段的完整套件：38/38 项通过。
- 26 个 unittest 文件：409/409；12 个场景脚本：12/12。
- 所有结果 `retry_count=0`，无 skip、timeout、非零退出、哈希/代码根或日志扫描错误。
- 正式十轮完整复验：每轮 38/38，累计 380/380 项、4090 个 unittest、120 个场景；380 个逐项结果均为零重试且无扫描错误。
- 任务收尾又 fresh 执行一次完整套件：38/38、409/409、12/12。
- 最终静态检查：480 个产品文件、41 个测试文件和 41 个 manifest 条目与最终快照一致；86 个修改/新增及审计 Python 文件编译通过；`git diff --check=0`。
- 最终覆盖校验：0 未映射文件/hunk/变化/流程/测试，0 重复 ID。

证据目录：[完整套件](../.superpowers/sdd/2026-11-08-design-intent-change-audit/full-suite-final-retry/summary.json)、[正式十轮复验](../.superpowers/sdd/2026-11-08-design-intent-change-audit/race-repeat-final/summary.json)、[静态检查](../.superpowers/sdd/2026-11-08-design-intent-change-audit/final-static-checks.json)。

## 6. 部署与生产验收

本轮确有产品修复，且已获部署授权；只有上述离线门槛全部通过后才执行部署。

- 部署瞬间重新核对开发、最终验收和生产三方哈希：14 个待替换文件，生产无未知漂移。
- 独立备份覆盖 13 个既有文件，另登记 1 个新增路径；备份哈希全部匹配。备份位于 `backups/pre-rsync-20261108-design-intent-change-audit/`，未覆盖旧备份。
- 使用本机 `/quit` 正常停止；未执行顶层安装脚本中的 pip、xattr 或真实 iptables。
- 真实控制监听由 PID 75167 换代为 PID 44246；旧进程已退出，8085、8086、1080 均由新 PID 监听。
- 部署后 14 个文件哈希全部等于最终验收快照。
- 安全黑盒只读取安全状态码和固定探针结果：控制接口 200，8086 HTTP/HTTPS 204，1080 域名式 SOCKS5 204；部署前、部署后和任务收尾均为 4/4。
- 日志采集器只在本地进程内读取部署窗口，发布哈希和派生计数：21 个日志文件、402 行，0 traceback、0 新异常指纹、0 后台线程异常。
- 未读取完整配置响应，未把原始生产日志、完整 URL/query、账号、token、Cookie、Authorization 或未脱敏 session_id 写入报告或证据。
- 未发生回滚；未提交、推送、合并或创建 PR。

这些结果只证明本次部署、进程换代和当次真实代理路径成功，不证明长期生产性能或间歇问题永久消失。

## 7. 已证伪、deferred 与未覆盖

### 已证伪候选

- 当前 macOS 上“共享 HTTP 跨线程 `close()` 不能解除目标阻塞 `recv()`”未形成 RED；真实 socketpair 测试直接通过，因此没有增加 `shutdown(SHUT_RDWR)` 或停止期限。
- Client chunked 截断空 partial 等候选由真实代码和 socket 测试证伪，没有仅因代码气味修改产品。

### deferred

无。两项 Minor 产品问题和 QA 的 Minor 测试漏项均已处理，不作为 deferred。

### 仍未覆盖

1. `GAP-H2-001`：真实外部 H2 peer 与 TLS 后端全栈。
2. `GAP-SMART-001`：真实多阶段 Smart Router/TLS/桌面 WebUI 故障注入。
3. `GAP-HANDLER-001`：所有平台及完全忽略 socket 关闭的自定义 handler。
4. `GAP-DNS-001`：真实外部 DNS/DoH/DoT 上游与系统解析差异。
5. `GAP-PLATFORM-001`：真实安装、pip/xattr、iptables 和完整桌面退出。
6. `GAP-PRODUCTION-001`：生产性能、fd/线程/内存趋势及长期间歇故障归因。

这些缺口没有被局部关闭，也没有用于支撑依赖其证据的高风险结论。完整机器可读状态见[coverage.json](../.superpowers/sdd/2026-11-08-design-intent-change-audit/coverage.json)，执行过程见[progress.md](../.superpowers/sdd/2026-11-08-design-intent-change-audit/progress.md)。
