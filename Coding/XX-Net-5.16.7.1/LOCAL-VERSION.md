# XX-Net 5.16.7.1（本地版本）说明

本目录是本机开发快照，版本线 **5.16.7.1 起**：官方 5.16.7 基线 + 本地补丁集。对应本机实装运行目录 `/Applications/XX-Net`（macOS ARM64，Apple Silicon）。

## 基线

- 官方版本：**XX-Net 5.16.7**（见 `upstream/XX-Net-5.16.7.zip` / `upstream/XX-Net-5.16.7/`）
- 本目录从 `/Applications/XX-Net` rsync 复制生成（2026-10-27），排除运行数据 `data/`、`.DS_Store`、`__pycache__`、`*.pyc`
- 测试目标是运行中的安装（`/Applications/XX-Net`），非本快照；本快照用于版本管理与后续迭代

## 累计改动文件（相对官方 5.16.7）

| 文件 | 补丁 |
|---|---|
| `start` | 启动期提示修正：先探测 `boringssl` (abi3) 实际可加载性再打印提示（原无条件打印误导性 system SSL 提示）；PyObjC/Pillow 缺失时先试用户级安装（`pip install --user --break-system-packages`，兼容 PEP 668），不再直接 `sudo pip3` |
| `code/default/launcher/web_control.py` | 噪音降级：两处 `no module in path` WARN → DEBUG（191-193、268-271 行附近）；竞态修复：`req_debug_handler` 先取 `sys._current_frames()` 快照再 `.get(thread.ident)`，消除线程退出竞态 KeyError |
| `code/default/launcher/mac_tray.py` | PyObjC 11.x 兼容（`import objc` 独立导入 + 装饰器修复）、语言检测备选、setupHelper 守护线程、finishLaunching、helper 路径与权限检查修复（问题一/问题五） |
| `code/default/launcher/lang/zh_CN/LC_MESSAGES/messages.po` | 补充 15+ 条缺失中文翻译（问题二） |
| `code/default/smart_router/lang/zh_CN/LC_MESSAGES/messages.po` | 同上（问题二） |
| `code/default/launcher/web_ui/status.html` | 系统信息标签 i18n 包裹（问题二）；根修：`loadModuleEnableFlags()` 拉 `GET /config?cmd=get_config`，gae_proxy 未启用时 updateDetailStatus / updateNoobStatus / OneKeyReport 跳过轮询（E 前置根修） |
| `code/default/x_tunnel/local/upload_logs.py` | 根修：`get_module_enable_flags()` 按启用状态过滤 debug/log 轮询条目（失败安全：拉取失败保持原全量轮询）。O 阶段：按需收集七模块 startup/warning 视图，诊断包按状态/配置→内存快照→视图→统一正文→`.1`→显式 legacy 排序，默认未压缩预算 10MiB，大项跳过继续，旧日志默认排除 |
| `code/default/smart_router/local/__init__.py` | K 阶段：将 `g.local_ips`、`g.dns_query` 与 `g.dns_srv` 初始化前移到 `proxy_server.start()` 之前，消除启动首请求访问 None DNS 查询对象或 UDP 中继的竞态；代理/DNS 地址分离，端口参数与业务逻辑不变。N 阶段：代理 Server 构造/启动失败时按逆序尝试清理 DNS、PipeSocks 与 ConnectManager，清理异常不遮蔽原启动异常，ready=False；正常启动与 stop 不变 |
| `code/default/smart_router/local/dns_query.py` | 四项修复 1：DoT `connect` 判空（B1/B2：close 保留 + 注释实证、基类 direct_connect fd 泄漏与日志参数修复、代理路径 connect 失败 close）；E 阶段：`_is_usable_nameserver()` nameserver 过滤（拒 fe80::/10、fc00::/7、::1、ff00::/8，放行 IPv4 与全球单播 v6）+ get_local_dns_server 过滤前移与兔底；H 阶段：EHOSTUNREACH/ENETUNREACH per-nameserver 60s 退避，冷却跳过、全冷却最早到期单点 fail-open、重复失败不延长、成功清账 |
| `code/default/smart_router/local/pipe_socks.py` | 四项修复 2+4：`try_add` fd 竞态（fileno 预检 + ValueError/KeyError DEBUG）；BlockingIOError 限流 `warn_send_block`（同 socket 每秒 1 条 + suppressed 聚合）；B3 try_remove 对称处理；B6 close 时补打 final 并清账 |
| `code/default/x_tunnel/local/tls_relay_front/ip_manager.py` | 四项修复 3：`ipv6_available()` UDP 探测（全球单播判定，缓存 300s）+ get_ip_sni_host 过滤 IPv6 前端；B4 修正 `::` 误判；B5 过滤 INFO 可观测。`seley_front/ip_manager.py` 经相对导入继承 |
| `code/default/lib/darwin/boringssl.abi3.so` | ARM64 原生 BoringSSL（abi3），指纹与官方 x86_64 一致，见 `../../boringssl-arm64/BUILD-macOS-ARM64.md` |
| `code/default/lib/noarch/xlog.py` | F 阶段：① warning_log 10MB 上限单份滚动 `_roll_warning_log_if_needed()`（仅一代 `.1`，磁盘上限约 2×10MB；挂点 4 处：`__init__` 开文件后、`reset_log_files()` 重开后、`log()` 写入路径 buffer_lock 内写前检查；roll 失败重开 fd，再失败跳过写入，与主日志既有失败路径一致）② 通用节流类 `LogThrottle`：`allow(key) -> (allowed, suppressed_count)`，首条立即放行、窗口内仅计数、下一条放行时附 `(suppressed N)` 结算；带锁、512 键过期剪枝；内存只持有 `[ts, count]` 整数对，从不暂存日志内容。L 阶段：顶层 `warning()` 修正为委托 `default_log.warn()`。M 阶段：`flush() -> (suppressed_total, pending_key_count)` 原子取出、清空并封住旧生命周期，`reset()` 显式开启新生命周期，供真实 stop/reset 终点汇总 |
| `code/default/smart_router/local/dns_server.py` | F 阶段：`reserved/frag` 包字段检查 `xlog.warn` → `xlog.debug`（159-165 行，附依据注释；实测 14,897+ 条/天的解析包字段检查非运行故障，DEBUG 仍进 start_log 可查） |
| `code/default/lib/noarch/front_base/connect_manager.py` | F+G 阶段：`not enough ip` 告警 60s 节流；放行行附上一窗口 `(suppressed N last window)`，旱态恢复时补打 `(suppressed X, total N)` 尾窗与本轮累计并清账（账本共用 `no_ip_lock`）。H 阶段：total 改为本轮总事件数（放行+抑制，首次为 1）；`_connect_ssl` 增加实例级 `LogThrottle(60)`，按 `(ip_str, 异常类别)` 聚合，异常仍全部回传 Queue。M 阶段：`stop()` 汇总并清除 connect_ssl 尾账。`no_ip_time`、`_need_more_ip` 轮询、`sleep(1)` 与建连数据路径零改动 |
| `code/default/lib/noarch/front_base/http_dispatcher.py` | F 阶段：`retry_task_cb` 轨迹 WARN 按 task.url 60s 节流。I 阶段：内部日志 key 改为 `(method, host, 去查询参数 endpoint bytes, front ip)`，retry 与 request 的 task timeout/status 共用 `LogThrottle(60)`；非 UTF-8 path 不解码，放行行保留完整 URL/tid 与 trace，重试状态、fail 计数、Queue、响应和返回路径零改动。L 阶段：默认诊断 URL 对 `host=None` 使用 `<unknown>` 占位，避免 bytes 格式化异常；task host 不变。M 阶段：`stop()` 在关闭 worker 后汇总 dispatcher 尾账 |
| `code/default/lib/noarch/front_base/http1.py` | F+G 阶段：`head_request` 三出口（send len / status / exception）失败 WARN+trace 统一改为类级 `LogThrottle(60)`、按前端 `ssl_sock.ip_str` 跨 worker 聚合；删除 per-worker 节流属性，放行 trace 附 `(suppressed N)`。探测/recheck/close/返回值逻辑零改动 |
| `code/default/x_tunnel/local/proxy_session.py` | I 阶段：实例级 `transfer_log_throttle = LogThrottle(60)` 在后台线程前创建；ack timeout、retry、非 200 roundtrip、最终 failed 按事件所属 `(session_id, transfer_no, kind)` 聚合，roundtrip 使用请求前捕获的 session，避免 reset 后旧响应错账；放行保留原字段并结算 suppressed。M 阶段：首次有效 `stop()` 用 `finally` 保证 transfer 尾账结算，重复 stop 与迟到事件不重开账；接收池汇总延迟到外层业务锁释放后，start/reset 重新开账。状态迁移、重试、notify、sleep 与传输路径零改动 |
| `code/default/x_tunnel/local/front_dispatcher.py` | I 阶段：模块级 `LogThrottle(60)` 按完整 `(host, path)` 聚合 front retry；同 tid 抑制、不同 tid 独立，front 选择、状态码、重试和请求内容零改动。M 阶段：模块 `stop()` 在停止全部 front 后汇总 retry 尾账并封口，`init()` 重新开账 |
| `code/default/x_tunnel/local/base_container.py` | J 阶段：`BlockReceivePool` 实例级 `LogThrottle(60)` 按 SN 聚合 `mark_sn_timeout` 首报/renew；仅实际日志事件进入账本，静默旧事件不计数；reset 在原接收池锁内同时清空状态并重建账本，同号 SN 重新首报。M 阶段：reset 在同一锁内取出旧尾账并替换账本，默认锁外补一条汇总；ProxySession 可延迟到其外层锁释放后写日志。构造器无旧账安全，reset/mark 原子性不变。被抑制 renew 仍按原条件更新 `server_send_time`，`get_timeout_list`、重试与传输零改动 |
| `test_xlog_roll_f.py` | F+G+H+I+L+M 阶段离线测试（仓库快照内）：warning_log 滚动 7 项 + LogThrottle 语义 11 项（容量、剪枝、并发、warning 委托、flush 清账封口、reset 重开）+ HEAD 6 项 + no-IP 4 项 + `_connect_ssl` 10 项（实例隔离、Queue 全送达、stop 尾账与迟到事件封口）= 38/38 PASS |
| `../../tests/test_dns_ns_backoff.py` | H+I 阶段 DNS 离线测试：EHOSTUNREACH/ENETUNREACH 首错 WARN+60s 冷却、冷却重复仅新增 DEBUG 且不延长、其他 errno 不退避、query 跳过冷却 server、全冷却最早到期单点 fail-open、到期重试、成功清账 = 9/9 PASS；按相对路径加载本快照，不创建真实 socket/线程 |
| `../../tests/test_retry_log_throttle.py` | I+L+M 阶段 dispatcher 离线测试：聚合 key、跨窗 suppressed、并发、非 UTF-8 path、`host=None`、stop 单次尾账，以及 stop 后真实 retry 保持 stopped 失败出口但不重开账本 = 12/12 PASS |
| `../../tests/test_xtunnel_log_throttle.py` | I+M 阶段 x_tunnel 离线测试：聚合与业务语义、reset 错账防护、session/front stop 尾账与封口、接收池汇总移出外层锁、stop 清理异常 finally 结算、start 重开新生命周期 = 14/14 PASS |
| `../../tests/test_mark_sn_timeout_throttle.py` | J+M 阶段接收池离线测试：同 SN 聚合且状态更新、不同 SN/池实例独立、跨窗 suppressed、reset 后同号 SN 首报、20 线程单 WARN 且保存最大 t、静默旧事件不消耗窗口、reset/mark 可控交错保证新生命周期首报、reset 尾账单次结算 = 9/9 PASS |
| `../../tests/test_smart_router_startup_race.py` | K+N 阶段启动离线测试：S1/S2 覆盖启动即收到域名请求与 UDP ASSOCIATE；S3/S4 覆盖代理启动失败后的逆序资源清理、清理异常隔离与原异常保留；执行前后恢复应用模块替身，避免同进程测试污染 |
| `../../tests/test_smart_router_startup_test_isolation.py` | N 阶段测试隔离回归：在同一进程用 runpy 执行真实启动竞态脚本，断言 env_info/xlog 等原有模块对象及 Smart Router 模块表恢复；普通标准库导入允许保留 |
| `../../tests/test_unified_log_storage.py` | O 阶段统一正文与错误上下文：U1–U9、C1–C10，共 19/19 PASS |
| `../../tests/test_log_view_export.py` | O 阶段 startup/warning 视图、损坏记录与七模块 Web 走线：V1–V10，共 10/10 PASS |
| `../../tests/test_log_diagnostic_pack.py` | O 阶段视图收集、禁用模块零请求、预算、优先级、脱敏和 legacy 边界：P1–P10，共 10/10 PASS |
| `../../tests/test_persistent_lifecycle_logs.py` | O 阶段 AST 契约：只允许八处指定生命周期 INFO 使用 `persist=True` |

## 修复阶段索引（详见 docs/XX-Net-macOS-ARM64-修复记录.md）

- **四项修复**（2026-10-26 晚，日志高频告警治理）：① DoT AttributeError 判空；② try_add fd 竞态；③ 无 IPv6 时前端池过滤（含 seley）；④ BlockingIOError 日志限流
- **B1-B6**（质检与边界完善）：A1 wrap_socket fd 语义实证；B1 注释实证；B2 基类既有隐患（fd 泄漏 + 日志参数 TypeError）；B3 try_remove 对称 DEBUG；B4 `::` 误判修正；B5 过滤 INFO；B6 close 限流账本闭环
- **根修（按启用状态过滤轮询）**：upload_logs.py + status.html 调用方先查 `get_config` 启用状态，gae_proxy 未启用时零 404
- **E 阶段（日志收敛）**：dns_query.py nameserver 过滤（resolv.conf link-local v6 污染治理，`skip unusable DNS server` INFO + CN/通用兔底）
- **D 阶段**：重启上线与回归审计（2026-10-27 05:50 重启）；E 阶段重启 12:47（PID 27738），回归零新增
- **F 阶段（日志治理，2026-10-27 晚—10-28 午）**：T1 历史日志归档止损（7 份约 237MB，commit `7738967`）；T2 真问题三分类清单（残留 bug 零修复级）；T3 五文件日志机制改造（warning_log 10MB 滚动、`LogThrottle` 节流、reserved/frag 降 DEBUG、not enough ip / retry / HEAD 探测三处告警 60s 节流）+ 第 7 套离线测试 `test_xlog_roll_f.py`（10/10）；T4 重启后审计（2026-10-28 10:44 用户重启 PID 14268）：seley retry trace 846→3（-99.6%）、not enough ip tls_relay 2,066→7 全带抑制标记、reserved/frag 32,596→0、7 份日志 ERROR 合计 0、增速受控
- **G 阶段（节流尾账完善，2026-10-28 午后，无部署）**：提案 A+B 落盘——not enough ip 放行行补上一窗计数、恢复时结算尾窗与本轮累计；HEAD 三出口改为按前端 `ip_str` 跨 worker 聚合；`test_xlog_roll_f.py` 扩至 23/23，五套既有回归与 fd 探针结论不变；未实施 C，未 rsync、未重启
- **H 阶段（建连与 DNS 治理，2026-10-29，已部署并重启）**：total 改为本轮旱态总事件数（首次为 1）；`_connect_ssl` 按 `(ip_str, 异常类别)` 60s 聚合并保持 Queue 全送达；DNS 对 EHOSTUNREACH/ENETUNREACH 做 per-nameserver 60s 退避与全冷却单点 fail-open。快照测试 30/30 + 7/7 PASS，五套既有回归全 PASS、fd 探针 A(detached)。已部署至 `/Applications/XX-Net`，用户重启后 PID 2066（19:00:28）加载；`_connect_ssl` suppressed 与 total=1 新口径已获生产日志实证，DNS 延长观察约 82 分钟未触发目标 errno，不声称现场疗效
- **I 阶段（前端与 x_tunnel 故障链日志聚合，2026-10-29 实施，2026-10-31 已部署并重启）**：dispatcher 改按 `(method, host, 去查询参数 endpoint bytes, front ip)` 聚合 retry/timeout/status；x_tunnel 按事件所属 `(session_id, transfer_no, kind)` 聚合四类 WARN，front retry 按完整 `(host, path)` 聚合；补强 LogThrottle、DNS 与真实初始化实例隔离测试。独立审查两项 Important（非 UTF-8 path、reset 后旧响应错账）已 RED→GREEN 修复；新测试 9/9 + 8/8，综合测试 33/33 + 9/9，五套既有回归与 fd 探针通过；只改日志，未改重试、状态、Queue、评分或传输路径。已于 2026-10-31 与 J 一并 rsync并由用户重启（PID 24668，18:16:13）；代理 204，首批 I 独立首报及后续同 key suppressed 聚合均已获生产实证、J 已出现首报，未 push
- **J 阶段（下载 SN 超时日志聚合，2026-10-29 实施，2026-10-31 已部署并重启）**：复评 H 日志发现 `mark_sn_timeout` 首报 363、renew 379；按 SN 60s 聚合首报/renew，reset 在原锁内同步重建账本，状态更新条件不变。独立审查两项 Important（静默旧事件误计窗口、reset/mark 非原子）已 RED→GREEN 修复；新测试 8/8，全部新旧回归与 fd 探针通过；`req get response timeout` 留待重启后复评。已于 2026-10-31 与 I 一并 rsync并由用户重启；代理 204，首批 I 独立首报及后续同 key suppressed 聚合均已获生产实证、J 已出现首报，未 push
- **K 阶段（Smart Router 启动竞态，2026-10-31，已部署并重启）**：生产重启首秒出现 `NoneType.query_recursively`；独立审查补充 SOCKS5 UDP ASSOCIATE 的 `NoneType.udp_relay_port` 同类窗口。将 `g.local_ips`、`g.dns_query`、`g.dns_srv` 全部前移到 `proxy_server.start()` 之前，分离代理/DNS 地址变量。S1/S2 均 RED→GREEN，完整回归通过；两项 Minor（启动失败资源清理、测试 sys.modules 隔离）延后。已与 L/M 一并由用户重启（PID 74385，21:10:26）；本轮启动未复现两类 NoneType，代理 204，未 push
- **L 阶段（剩余推荐错误修复，2026-10-31，已部署并重启）**：dispatcher 默认诊断 URL 对 `host=None` 使用 `<unknown>`，不改变 task host、重试或响应；顶层 `xlog.warning()` 修正为 `default_log.warn()`。R10 与 B9 均先按目标 RED 再 GREEN，dispatcher 10/10、xlog 34/34，完整回归通过。`req get response timeout` 当前生产日志及 I/J 重启后均 0，按证据不做新增节流；尾账推荐有明确生命周期时用 flush，不引入全局后台脉冲。已随 K/M 重启加载，本轮未触发 host=None 目标路径
- **M 阶段（生命周期尾账结算，2026-10-31，已部署并重启）**：`LogThrottle.flush()` 原子结算、清账并封口，`reset()` 开启新生命周期；接入 dispatcher、ConnectManager、ProxySession、BlockReceivePool.reset 与 front_dispatcher.stop，每个对象最多一条汇总 WARN，不逐 key、不后台线程。独立审查三项 Important（stop 后迟到事件、外层锁内日志、清理异常跳过结算）均一轮 RED→GREEN 修复；xlog 38/38、dispatcher 12/12、x_tunnel 14/14、SN 9/9，完整回归通过；http1 类级 HEAD 账本没有安全全局终点，保持现状。已随 K/L 重启加载；约 20 小时生产观察已出现 16 条 transfer final 聚合尾账，证明真实 stop/reset 终点可落盘，其余终点按实际事件继续观察
- **N 阶段（启动失败清理与测试隔离，2026-11-01，已部署并重启）**：代理 Server 构造/启动失败时按 `dns_srv → dns_query → pipe_socks → connect_manager` 各尝试 stop，清理异常写日志但不遮蔽原始异常，ready=False；正常 K 启动顺序与正常 stop 不变。S3/S4 与独立 sys.modules 隔离回归均 RED→GREEN，S1–S4、xlog 38/38、dispatcher 12/12、x_tunnel 14/14、SN 9/9、DNS 9/9、五套既有回归与 fd 探针通过。2026-11-01 19:28 仅部署 `smart_router/local/__init__.py`，备份于 `backups/pre-rsync-20261101-n/`；部署 cmp 一致，SHA-256 为 `335f2090b3b525a98c4e679c0db1ea79411ef026cfb1f39a71c1687846fd14a8`。用户重启后 PID 43995（19:33:14）加载，8086 LISTEN；首次代理请求 20 秒超时，随后两次 generate_204 均 204。启动日志 ERROR/Traceback 为 0，代理正常启动，N 清理路径未触发，未 push
- **O 阶段（统一日志存储与有界错误上下文，2026-11-02，开发完成未部署）**：每个已有持久 Logger 统一写 `<name>.log`，仅保留单代 `.1`；启动详细期后普通 DEBUG/INFO 仅内存，重要项可 `persist=True`，WARNING+ 持续落盘。ERROR/CRITICAL 从同一内存 buffer 导出近期 DEBUG/INFO，最多 40 条、16KiB、同原始 fmt 冷却 60 秒，本体和 exception 堆栈不受冷却影响。新增 startup/warning 按需视图，诊断包按明确优先级与 10MiB 未压缩预算装包；仅八处生命周期 INFO 显式持久化。新测试 U/C 19、V 10、P 10、生命周期契约 1 项均通过，旧 xlog 38/38 与计划完整回归通过，fd 探针 A(detached)。独立审查一项 Critical、两项 Important 均一轮 RED→GREEN 修复。未部署、未重启、未 push，不宣称生产疗效。

## 已知评估后接受项

- 限流键 `id(s)` 复用窗口（±1 秒）；ipv6 探测缓存 300 秒；全 v6 前端池 `not enough ip` 为真实信号保留；seley handshake timeout 属前端池正常淘汰
- 尾账边界：M 只覆盖有真实 stop/reset 终点的账本；http1 的类级 HEAD 账本不能随单个短命 worker.close 清账，沉默剪枝、异常退出仍可能丢失最后的 suppressed 数量注解。信号本体（每窗口首行）始终实时落盘；若需完整审计，应使用独立事件统计而非 WARN 流
- 尾账方案选择：生命周期 flush 无后台定时器、只在终点结算；不实施全局脉冲。每个对象仅输出聚合总数与待结算 key 数，不缓存或补打被抑制日志原文
- 交付包 `installer/XX-Net-5.16.7.zip` 仍为官方原包（补丁仅在本机实装与本地快照），建议上游合并
