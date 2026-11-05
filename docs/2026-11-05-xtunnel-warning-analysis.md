# X-Tunnel 频繁 WARNING 只读诊断

日期：2026-11-05
范围：生产日志、正常直连/代理请求与开发快照源码；未修改生产代码、配置或进程。

## 结论

当前最主要的问题是 **X-Tunnel 三条 front 链路同时出现等待、响应超时和重试，并已影响代理请求**。这不等于数万次独立故障：同一传输编号会在 front、X-Tunnel 主会话和 Smart Router 多层记录，并触发自动重试。

采样时的直连/代理对照为：直连 `http://www.gstatic.com/generate_204` 返回 204，耗时约 0.164 秒；经 `127.0.0.1:8086` 的相同请求在 20 秒后超时。这证明当时整机普通直连网络仍可用，而 XX-Net 转发链路不健康；不能据此区分远端服务、特定路由或本地调度中的唯一根因。

当前生产 Launcher 已变为 PID 52948，启动时间为 2026-11-05 13:10:51；它不是此前部署复核时的 PID 89760。本记录没有推断该进程变化的原因。

## 警告分布与故障链

近 24 小时采样：

| 模块 | WARNING 约数 | 主要类型 |
|---|---:|---|
| Seley Front | 48,819 | 接收/重试超时、内部 601、可用 IP 波动 |
| X-Tunnel 主会话 | 20,677 | 等待/选择 front 失败、会话重试、往返失败 |
| TLS Relay | 8,660 | 响应超时、TLS 建连失败、连接关闭 |
| Smart Router | 6,598 | DNS 查询失败、SOCKS 转发失败 |
| Cloudflare Front | 5,086 | 响应超时、可用 IP 不足、连接重置 |
| Launcher | 3 | 更新/SwitchyOmega 下载失败、一次 accept errno 9 |

以传输编号 638 为例，14:54:25 同时出现 X-Tunnel 的 `front retry` 和 Cloudflare 的 `get response timeout`。这是同一故障在不同层的记录，不应相加解释为两次独立用户请求。

### 状态码与日志语义

- `601`：`Task.response_fail()` 创建的默认内部失败响应。Seley 样本中的 `response_fail:retry timeout` 和接收超时属于这一类，不是远端 HTTP 状态。
- `602`：`front_dispatcher.request()` 在 `get_front()` 超时时返回，表示没有选到可用 front；各 front 在 dispatcher 未返回响应时也使用 602。
- `603`：front 请求的初始/最终超时时限状态。
- `get_front_time`：选择 front 超过 0.1 秒即记录，单独出现不代表请求已失败。
- `not enough ip recovered`：恢复通知也以 WARNING 记录，不应把“不足”和“恢复”都算作失败次数。
- `response length incorrect`：响应声明的 `Content-Length` 与实际读取长度不一致后重试。旧日志证明该现象在本轮修改前已经存在。

## DNS 与登录异常

Smart Router 的 `query_dns_from_xxnet fail status:602` 是 X-Tunnel 无可用 front 的下游表现；但公共 DoH/DoT 也同时出现超时、重置、`Broken pipe` 和 `Network is unreachable`，因此 DNS 不是只有单一来源。

13:21 左右部分 DNS 请求记录约 581–606 秒耗时并集中失败。现有日志不足以区分系统休眠、网络切换或线程停顿，不能仅凭耗时归因。

14:36 左右登录多次返回内部 601/602，另有 `login_session head error:3c:73:63:72:69:70`。这些字节对应 `<scrip`，说明 HTTP 200 的内容解包后不是预期协议头，更像 HTML/JavaScript 内容；同期 Cloudflare front 有异常响应。14:36:47 登录成功，因此该轮协议错误已经恢复。未读取或输出账号、密码或令牌，现有证据也不支持把该现象归因于凭据错误。

## 独立的连接注册表竞态

Smart Router 在近 24 小时出现 3 次 `conn_list[conn_id]` 的 `KeyError`。`do_socks()` 在 `create_conn()` 返回后重新索引共享字典；连接若已被停止线程移除，就会抛出异常并中断该次转发。

这是有真实堆栈和代码路径支持的代码缺陷，但频率低，不能解释 front 超时洪峰。修复只针对该生命周期竞态，不宣称能够治愈网络或远端超时。

## 历史与边界

旧 `_warning.log` 已包含 2026-10-27 至 2026-11-03 的 front 超时、可用 IP 不足、`get_front fail`、DNS 失败和响应长度不一致。因此不能把这些现象全部归因于近期可靠性修改。

本轮没有生产故障注入、长期 soak、远端服务内部证据或本机休眠/网络切换日志。后续若继续定位共同超时，应在正常请求下对照 front 评分、worker 状态、建连/TLS 分阶段耗时和系统网络事件；不得通过修改生产参数进行猜测性试验。

## 连接启动竞态修复与离线验证

开发提交 `16c07e0` 为 `ProxySession` 新增 `start_conn(conn_id, left_buf=b"", block=True)`，在同一个 `RLock` 临界区内完成连接查找、可选首包传递和启动；连接已被移除时返回 `False`。Smart Router 与 X-Tunnel 的 SOCKS4、SOCKS5、HTTP、HTTPS 启动路径均改用该接口，不再在 `create_conn()` 返回后重新索引共享 `conn_list`。

TDD 先新增三项回归测试，并确认 RED：均因 `ProxySession` 尚无 `start_conn` 而以 `AttributeError` 失败。最小实现及调用方迁移后，同一测试文件 8/8 通过；测试分别约束已移除连接返回 `False` 且不启动、首包先于启动且透传 `block`、空首包只启动。修改文件通过 `py_compile`，差异空白检查通过。

独立审查确认五个目标启动点的首包顺序、`RLock` 重入链和 `False` 分支符合约定，但发现 SOCKS5 成功回复发送失败时仍有“先检查成员、再索引字典”的同类竞态。新增 `test_socks5_reply_failure_tolerates_concurrent_conn_removal` 后，先在原实现复现 `KeyError: 1`；提交 `49c23c9` 在同一把会话锁内用 `.get()` 获取并停止仍存在的连接，随后该文件 9/9 通过。

审查另有一项延后测试改进：现有 `start_conn` 测试未构造真实并发，也未用真实 `Conn` 覆盖重入链。这是覆盖限制而非已证实的当前行为缺陷；生产对象的 `RLock` 及 `transfer_received_data()` → `send_conn_data()` 重入路径已做静态核对，本轮不顺带扩大测试改造。

审查修复后重新执行完整离线回归：26 个 `tests/test_*.py`、独立模型测试和性能基线自验共 27/28 个命令通过。独立模型 30 项测试通过，性能基线 3 项自验通过。唯一失败仍是旧 `tests/test_ipv6_filter.py`：它直接加载 `/Applications/XX-Net` 的生产 TLS Relay 文件，其配置替身缺少 `ip_cal_expect_time_package_size`，触发 `AttributeError`。该失败与本轮连接启动差异无关，本轮未扩大范围修改它，也不把完整套件记为全绿。

以上离线结果只证明连接生命周期路径及其既有回归范围，不能证明生产三条 front 的网络超时已经解决。离线验证完成时尚未部署、重启或 push。

## 部署结果

用户随后明确授权“可以部署”。三份开发运行文件与生产目标存在差异，且生产目标均与本轮修改前的开发版本一致，因此只部署了 `proxy_session.py`、`proxy_handler.py` 和 `smart_route.py`；未部署文档、测试或配置。

新备份为 `backups/pre-rsync-20261105-xtunnel-conn-start-race/`，保存三份原始文件、部署差异、原/新 SHA256 清单和来源提交。部署后三份生产文件均与开发快照逐字节一致，备份与原/新清单均通过哈希复核。

本次未重启 Launcher。进程仍为 PID 52948，启动时间为 2026-11-05 13:10:51，并继续监听 8085、8086，因此运行进程尚未加载新文件。只读正常请求中，`/config` 两次出现 `RemoteDisconnected`，`/debug` 返回 200；这些请求发生在旧进程上，不能归因于刚写入但尚未加载的修复，也不构成生产疗效验证。分支仍未 push。

## 重启与生产验收

用户随后明确回复“授权重启”。旧 PID 52948 接收正常 `SIGTERM` 后退出，使用项目自带 `/Applications/XX-Net/start -no_popup` 启动；新 PID 为 78220，启动时间为 2026-11-05 15:49:49，8085、8086 均正常监听。

首轮正式黑盒为 9/10：唯一失败是 `A22-deployed-provenance` 尚未纳入本批部署清单。将新 `DEPLOYED-SHA256SUMS` 加入正式脚本后，连续三轮均为 **10/10 PASS**。每轮均验证 29 个累计部署文件来源、配置接口、禁用模块零请求、默认与显式关闭连接的各 24 路 `/debug`、全部启用模块日志视图，以及显式生产代理 `generate_204=204`。重启后 `/config` 已返回 200，但单次或三轮成功不证明间歇问题永久消失。

重启后日志按堆栈所属时间复核，未发现新的目标 `KeyError` 或 `conn_list[conn_id]` 堆栈。15:52:10 一次实际 SOCKS 转发在 `smart_route.py:258` 进入 `XTunnelNotRunning`，与新增 `start_conn()` 返回 `False` 后的既有处理路径一致，证明新代码已被加载并执行；该事件不是 `KeyError`。仍观察到 DNS/front WARNING 和三条 `check_upload_not_acked lock time` ERROR，因此不能宣称共同超时或全部告警已经消除。

本轮未注入生产故障，未专门触发 SOCKS5 回复失败分支，也未做长期 soak。三轮黑盒通过只代表上述验收时段和项目，不代表连接竞态或间歇网络问题已在所有生产条件下治愈。分支仍未合并或 push。

## 27 日前后基线与跨层去重（后续只读诊断）

### 27 日修改是否相关

找到 `backups/logs-20261027/` 中 26 日至 27 日治理前的归档日志，并逐份核对 `backups/pre-rsync-20261027/` 的实际部署差异。27 日这批六份文件修改的是 WARNING 滚动、热日志节流和 DNS 包字段降级；`connect_manager.py`、`http1.py`、`http_dispatcher.py` 的差异没有改动建连、重试、响应等待或状态迁移的控制流程。

归档已经包含三条 front 的响应超时、建连/TLS 失败、可用 IP 不足，以及 X-Tunnel 的 front 重试、往返失败和 Smart Router 的 DNS 下游失败。例如，约 32.2–32.7 小时的归档中，Seley、Cloudflare、TLS Relay 分别有 281、50、269 条响应超时，Smart Router 有 1,696 条 `query_dns_from_xxnet fail`。因此，现有证据不支持“27 日修改首次制造这些网络问题”。

27 日至 11 月 3 日的 WARNING 归档继续出现同类事件，但节流前后行数不是同一计量口径，且两个窗口的请求量、网络环境和后续代码版本不同。即使按观测时长折算，也只能描述日志，不能据此证明真实故障变轻、变重或与 27 日修改存在因果关系。可复现脚本及完整分类结果保存在 SDD 目录的 `log_baseline.py` 和 `log-baseline.json`；统计排除了验收脚本故意产生的 `invalid log export view`，堆栈续行继承最近主行时间。

### 同一批传输反复换路，不等于大量独立故障

在旧 PID 52948 的 13:10:51–15:49:49 窗口内，三条 front 共记录 2,828 条带编号的 `get response timeout`，但只对应 **44 个 tid**；前 10 个 tid 贡献约 **51.5%**。其中 **20 个 tid** 分别在 Seley、Cloudflare 和 TLS Relay 上都出现响应超时。

`tid=634` 从 14:38:46 到 14:57:11 持续换路和重试：Seley 首次超时后，统一调度器记录 `front retry`，X-Tunnel 记录状态 602，随后同一编号在 Cloudflare、TLS Relay 等路径继续超时。源码证实 `front_dispatcher.request()` 在总 deadline 内重新选择 front 并重试；`ProxySession` 对未完成的传输另有重试。因此，三条 front 的告警不能直接相加为三次独立用户故障，也不能把每个 WARNING 当作一个独立请求。

共同失败点已缩小到：front 已被选中并发出 `/data?tid=...` 请求后，front 包装层等待 dispatcher 响应超时。Seley 同窗口的 `h1_req_sent`、`recv timeout` 和约 5 秒跟踪进一步证明至少该路径已经发送请求、但未及时收到响应。Cloudflare/TLS Relay 的同类包装层日志没有足够状态字段，不能进一步区分排队、worker、远端或本地调度。建连/TLS 失败和 IP 池不足同时存在，但不是这类已选中 front 后响应超时的同义事件。

### 正常请求与系统事件对照

后续采样中，普通直连 `generate_204` 返回 204（约 0.178 秒），8086 代理返回 204（约 0.377 秒），共同远端 `ny3.xx-net.org` 的 HTTPS 根路径返回 404（约 1.066 秒）。根路径 404 只证明当时 DNS、TCP、TLS 和 HTTP 服务可到达，不证明 `/data` 会话接口或此前故障时段正常。

系统只读日志显示：14:33 起出现 `invalid source ip`，14:35:32 起 en0 链路与 DHCP 报告网络变化，14:35–14:43 又出现多次 DNS/代理配置变化；14:33–15:02 共记录 258 条 `invalid source ip`。这与 14:38–14:57 的 front 集中爆发在时间上重叠，是当前较强的本机网络异常线索。不过这些内核记录主要属于其他进程，且此前窗口也有 front 告警，尚不能证明它们是唯一原因。14:35–15:00 的电源日志未显示实际休眠/唤醒，只筛选到一条不代表休眠发生的 `darkwake_mode` 配置记录。15:49:49 重启后至采样时未再看到 `invalid source ip`。

原始系统事件和正常请求结果保存在本计划 SDD 目录。当前仍没有远端 `/data` 内部日志或每次 front 尝试的统一分阶段关联字段，不能把共同超时唯一归因于远端服务、本机网络变化或调度器。

## 当前分级结论与后续边界

1. **27 日相关性：目前不支持其为根因。** 主要问题在修改前已经存在，实际差异也只改变日志行为；但日志节流使数量前后不可直接比较。
2. **WARNING 洪峰的直接放大机制：已确认。** 少量传输在三条 front 间反复重试，跨层重复记录；它是告警数量的放大器，不是底层网络为什么超时的根因。
3. **共同底层原因：未唯一定位。** 当前优先线索是故障窗口的本机网络/路由变化，共同远端 `/data` 服务和本地调度仍待证据区分。
4. **本轮只完成诊断，不做猜测性修复。** 若继续，应在开发分支用离线测试设计按 tid 记录每次尝试的 front、worker、状态、阶段和耗时的最小诊断方案；另行授权后才部署或重启。是否增加“每 tid 聚合/限频”也必须与保留排障证据一起评估，不能只隐藏 WARNING。
5. 旧 IPv6 测试失败、真实并发覆盖限制、锁耗时 ERROR 和登录协议异常仍是独立边界，不与本次网络归因混同。

## Front 分阶段诊断实现与离线验收

提交 `0bec792` 在 dispatcher 既有无响应 WARNING 中改用 `Task.unique_id` 和 `Task.get_trace()`，保留 Seley 经 `X-Path` 传递的原始 `/data?tid=...`，并在响应分支前统一记录 `get_response`。成功与失败仍走原有返回、计数和节流路径，不打印请求头或正文。新增测试先因旧日志缺少 tid 而 RED，实现后 `test_front_body_deadline.py` 4/4 通过。

提交 `165022f` 在既有 `front retry` 放行日志中增加实际 `front`、内部 `status`、`select` 和 `request` 秒数。新增测试先因旧格式不含这些字段而 RED，实现后 `test_front_request_deadline.py` 5/5 通过；X1–X14 节流回归全部通过，其中 X7 仍验证同 tid 聚合、不同 tid 独立，X10 的 stop 尾账格式未变。总 deadline、重试和节流键未改。

新增 `tests/analyze_xtunnel_front_diagnostics.py`，以 `analyze_records(records: list[dict]) -> dict` 按字符串 tid 关联可见跨层记录，保留来源、时间、会话标识、front、状态、轨迹最后状态和选择/请求耗时；输出 `visible_records`、`unique_tids`、`suppressed_reports` 与 `unmatched`，不生成“独立尝试数”。轨迹解析保留 worker 状态中的空格和逗号；`ambiguous_tids` 标记跨会话复用或缺少会话范围的编号，这些组不得解释为同一次传输。CLI 要求显式日志、输出 JSON 和 `--session` 单会话标识。内存关联、真实轨迹、会话复用、缺失范围及 CLI 样本测试均通过。

本轮逐一执行 27 个 `tests/test_*.py`、独立模型和性能自验，共 29 个命令中 28 个通过。独立模型 30/30、性能自验 3/3、四份修改/新增 Python 文件的 `py_compile` 及 `git diff --check` 均通过。唯一失败仍是 `test_ipv6_filter.py` 的生产 TLS Relay 配置替身缺少 `ip_cal_expect_time_package_size`，与本轮诊断差异无关，未扩范围修复。逐项退出码和完整输出保存在本计划 SDD 的 `regression/`。

这些测试没有复现 HTTP/2、真实远端排队、系统网络切换或生产定时行为，也没有证明共同超时已经治愈。截至本次离线验收记录，两份新诊断运行文件尚未部署或加载；后续生产观察必须按 tid 对照可见记录和系统事件，不能以短期无 WARNING 代替根因证据。

fresh-context 整体审查未发现 Critical 问题，指出两项 Important：真实 `get_worker`/HTTP2 轨迹中的空格和逗号会截断旧解析；会话重启会重置 tid，可能跨会话误并。分别新增轨迹保真/最后状态和会话复用/缺失范围测试并观察 RED，修正后分析器 4/4 通过。修正后再次执行完整回归，仍为 29 个命令中 28 个通过、独立模型 30/30、性能自验 3/3，唯一失败保持为同一旧 IPv6 测试替身问题；完整证据在 `regression-final/`。审查搁置的根因疗效、既有零耗时状态省略、计划指定的 tid 路径和无法归属 tid 的 stop 尾账均未扩大解释。

## 分阶段诊断部署与首轮生产证据

用户随后授权按证据自主决定程序、测试、部署和正常重启并持续迭代。两份生产目标在写入前均与 `b3248ad` 基线一致，原始副本保存于 `backups/pre-rsync-20261105-xtunnel-front-diagnostics/`；部署后来自 `2c000ad` 的两份运行文件与开发快照逐字节一致，原备份哈希复核 2/2 通过。旧 PID 78220 接收正常 SIGTERM 后退出，项目启动脚本于 17:39:10 启动新 PID 977，8085/8086 均通过 TCP 就绪检查。

部署后首轮黑盒 8/10：`A22-deployed-provenance` 尚未登记新清单，正常代理 `generate_204` 同时发生一次 25 秒超时。登记本轮清单后第二轮为 10/10，但这只说明后续正常请求成功，不能消除前一次超时的诊断意义，也不能宣称间歇问题已治愈。

新字段已在真实失败中生效。17:39:18 的登录路径由 `tls_relay_front` 处理，统一调度日志为 `status:602 select:0.000914 request:5.009701`；dispatcher 轨迹为 `get_worker(0)` → 约 5009ms → `get_response`，没有 `h1_req_sent`。同窗口多条 Seley/TLS Relay 建连或 TLS 握手超时。因此这次样本的失败边界在请求发出前，而非此前部分样本的“已发送后等待响应”。该登录请求路径为 `/data`、本身不带 tid，分析器保留为 unmatched，未伪造关联。新进程截至第二轮黑盒尚无带 tid 的新失败样本，仍需被动观察；本轮没有生产故障注入或参数猜测性修改。

后续真实 `upload_logs` 重试样本提示路径可能带 `session_id`。为避免新 dispatcher `unique_id` 和分析器 unmatched 副本扩大该既有字段的暴露，分别加入脱敏测试并观察 RED；实现仅将查询参数值替换为 `[redacted]`，保留 tid、front、状态和轨迹，且分析器不修改输入记录。派生分析 JSON 已原地脱敏，旧日志与备份未删除或改写。

脱敏回归还暴露 `test_xlog_bounded_writer.py` 的既有时序缺口：测试未等待 reset 线程真正持有 `buffer_lock`，第二个写入可能先完成。提交 `d52f542` 只增加 reset 进入受锁 flush 的事件同步，不改 xlog 生产实现；该测试随后连续 20/20 通过。
