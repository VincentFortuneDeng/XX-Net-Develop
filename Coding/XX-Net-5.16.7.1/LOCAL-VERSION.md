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
| `code/default/x_tunnel/local/upload_logs.py` | 根修：`get_module_enable_flags()` 按启用状态过滤 debug/log 轮询条目（失败安全：拉取失败保持原全量轮询） |
| `code/default/smart_router/local/dns_query.py` | 四项修复 1：DoT `connect` 判空（B1/B2：close 保留 + 注释实证、基类 direct_connect fd 泄漏与日志参数修复、代理路径 connect 失败 close）；E 阶段：`_is_usable_nameserver()` nameserver 过滤（拒 fe80::/10、fc00::/7、::1、ff00::/8，放行 IPv4 与全球单播 v6）+ get_local_dns_server 过滤前移与兔底；H 阶段：EHOSTUNREACH/ENETUNREACH per-nameserver 60s 退避，冷却跳过、全冷却最早到期单点 fail-open、重复失败不延长、成功清账 |
| `code/default/smart_router/local/pipe_socks.py` | 四项修复 2+4：`try_add` fd 竞态（fileno 预检 + ValueError/KeyError DEBUG）；BlockingIOError 限流 `warn_send_block`（同 socket 每秒 1 条 + suppressed 聚合）；B3 try_remove 对称处理；B6 close 时补打 final 并清账 |
| `code/default/x_tunnel/local/tls_relay_front/ip_manager.py` | 四项修复 3：`ipv6_available()` UDP 探测（全球单播判定，缓存 300s）+ get_ip_sni_host 过滤 IPv6 前端；B4 修正 `::` 误判；B5 过滤 INFO 可观测。`seley_front/ip_manager.py` 经相对导入继承 |
| `code/default/lib/darwin/boringssl.abi3.so` | ARM64 原生 BoringSSL（abi3），指纹与官方 x86_64 一致，见 `../../boringssl-arm64/BUILD-macOS-ARM64.md` |
| `code/default/lib/noarch/xlog.py` | F 阶段：① warning_log 10MB 上限单份滚动 `_roll_warning_log_if_needed()`（仅一代 `.1`，磁盘上限约 2×10MB；挂点 4 处：`__init__` 开文件后（66-75 行）、`reset_log_files()` 重开后（111-115 行）、`log()` 写入路径 buffer_lock 内写前检查（261-270 行）；roll 失败重开 fd，再失败跳过写入，与主日志既有失败路径一致）② 通用节流类 `LogThrottle`（385-422 行附近）：`allow(key) -> (allowed, suppressed_count)`，首条立即放行、窗口内仅计数、下一条放行时附 `(suppressed N)` 结算；带锁、512 键过期剪枝；内存只持有 `[ts, count]` 整数对，从不暂存日志内容 |
| `code/default/smart_router/local/dns_server.py` | F 阶段：`reserved/frag` 包字段检查 `xlog.warn` → `xlog.debug`（159-165 行，附依据注释；实测 14,897+ 条/天的解析包字段检查非运行故障，DEBUG 仍进 start_log 可查） |
| `code/default/lib/noarch/front_base/connect_manager.py` | F+G 阶段：`not enough ip` 告警 60s 节流；放行行附上一窗口 `(suppressed N last window)`，旱态恢复时补打 `(suppressed X, total N)` 尾窗与本轮累计并清账（账本共用 `no_ip_lock`）。H 阶段：total 改为本轮总事件数（放行+抑制，首次为 1）；`_connect_ssl` 增加实例级 `LogThrottle(60)`，按 `(ip_str, 异常类别)` 聚合，异常仍全部回传 Queue。`no_ip_time`、`_need_more_ip` 轮询、`sleep(1)` 与建连数据路径零改动 |
| `code/default/lib/noarch/front_base/http_dispatcher.py` | F 阶段：`retry_task_cb` 轨迹 WARN 按 task.url 60s 节流。I 阶段：内部日志 key 改为 `(method, host, 去查询参数 endpoint bytes, front ip)`，retry 与 request 的 task timeout/status 共用 `LogThrottle(60)`；非 UTF-8 path 不解码，放行行保留完整 URL/tid 与 trace，重试状态、fail 计数、Queue、响应和返回路径零改动 |
| `code/default/lib/noarch/front_base/http1.py` | F+G 阶段：`head_request` 三出口（send len / status / exception）失败 WARN+trace 统一改为类级 `LogThrottle(60)`、按前端 `ssl_sock.ip_str` 跨 worker 聚合；删除 per-worker 节流属性，放行 trace 附 `(suppressed N)`。探测/recheck/close/返回值逻辑零改动 |
| `code/default/x_tunnel/local/proxy_session.py` | I 阶段：实例级 `transfer_log_throttle = LogThrottle(60)` 在后台线程前创建；ack timeout、retry、非 200 roundtrip、最终 failed 按事件所属 `(session_id, transfer_no, kind)` 聚合，roundtrip 使用请求前捕获的 session，避免 reset 后旧响应错账；放行保留原字段并结算 suppressed。状态迁移、重试、notify、sleep 与传输路径零改动 |
| `code/default/x_tunnel/local/front_dispatcher.py` | I 阶段：模块级 `LogThrottle(60)` 按完整 `(host, path)` 聚合 front retry；同 tid 抑制、不同 tid 独立，front 选择、状态码、重试和请求内容零改动 |
| `code/default/x_tunnel/local/base_container.py` | J 阶段：`BlockReceivePool` 实例级 `LogThrottle(60)` 按 SN 聚合 `mark_sn_timeout` 首报/renew；仅实际日志事件进入账本，静默旧事件不计数；reset 在原接收池锁内同时清空状态并重建账本，同号 SN 重新首报。被抑制 renew 仍按原条件更新 `server_send_time`，`get_timeout_list`、重试与传输零改动 |
| `test_xlog_roll_f.py` | F+G+H+I 阶段离线测试（仓库快照内）：warning_log 滚动 7 项 + LogThrottle 语义 8 项（活跃 key 可超过 512、仅剪枝过期 key、并发精确计数）+ HEAD 6 项 + no-IP 4 项 + `_connect_ssl` 8 项（含真实 `__init__` 实例隔离、20 线程与 Queue 全送达）= 33/33 PASS |
| `../../tests/test_dns_ns_backoff.py` | H+I 阶段 DNS 离线测试：EHOSTUNREACH/ENETUNREACH 首错 WARN+60s 冷却、冷却重复仅新增 DEBUG 且不延长、其他 errno 不退避、query 跳过冷却 server、全冷却最早到期单点 fail-open、到期重试、成功清账 = 9/9 PASS；按相对路径加载本快照，不创建真实 socket/线程 |
| `../../tests/test_retry_log_throttle.py` | I 阶段 dispatcher 离线测试：同 front/endpoint 忽略 tid，不同 front/host/endpoint 独立，retry 与 timeout/status 共窗，worker=None 安全，下一窗 suppressed、完整 URL 保留、20 线程并发单 WARN，非 UTF-8 path 重试/响应不变 = 9/9 PASS |
| `../../tests/test_xtunnel_log_throttle.py` | I 阶段 x_tunnel 离线测试：session/transfer/kind 隔离、下一窗 suppressed、20 线程单放行、状态迁移/retry/notify/响应不变，front retry 同 tid 聚合，以及 reset 后旧响应不与新 session 错账 = 8/8 PASS |
| `../../tests/test_mark_sn_timeout_throttle.py` | J 阶段接收池离线测试：同 SN 聚合且状态更新、不同 SN/池实例独立、跨窗 suppressed、reset 后同号 SN 首报、20 线程单 WARN 且保存最大 t、静默旧事件不消耗窗口、reset/mark 可控交错保证新生命周期首报 = 8/8 PASS |

## 修复阶段索引（详见 docs/XX-Net-macOS-ARM64-修复记录.md）

- **四项修复**（2026-10-26 晚，日志高频告警治理）：① DoT AttributeError 判空；② try_add fd 竞态；③ 无 IPv6 时前端池过滤（含 seley）；④ BlockingIOError 日志限流
- **B1-B6**（质检与边界完善）：A1 wrap_socket fd 语义实证；B1 注释实证；B2 基类既有隐患（fd 泄漏 + 日志参数 TypeError）；B3 try_remove 对称 DEBUG；B4 `::` 误判修正；B5 过滤 INFO；B6 close 限流账本闭环
- **根修（按启用状态过滤轮询）**：upload_logs.py + status.html 调用方先查 `get_config` 启用状态，gae_proxy 未启用时零 404
- **E 阶段（日志收敛）**：dns_query.py nameserver 过滤（resolv.conf link-local v6 污染治理，`skip unusable DNS server` INFO + CN/通用兔底）
- **D 阶段**：重启上线与回归审计（2026-10-27 05:50 重启）；E 阶段重启 12:47（PID 27738），回归零新增
- **F 阶段（日志治理，2026-10-27 晚—10-28 午）**：T1 历史日志归档止损（7 份约 237MB，commit `7738967`）；T2 真问题三分类清单（残留 bug 零修复级）；T3 五文件日志机制改造（warning_log 10MB 滚动、`LogThrottle` 节流、reserved/frag 降 DEBUG、not enough ip / retry / HEAD 探测三处告警 60s 节流）+ 第 7 套离线测试 `test_xlog_roll_f.py`（10/10）；T4 重启后审计（2026-10-28 10:44 用户重启 PID 14268）：seley retry trace 846→3（-99.6%）、not enough ip tls_relay 2,066→7 全带抑制标记、reserved/frag 32,596→0、7 份日志 ERROR 合计 0、增速受控
- **G 阶段（节流尾账完善，2026-10-28 午后，无部署）**：提案 A+B 落盘——not enough ip 放行行补上一窗计数、恢复时结算尾窗与本轮累计；HEAD 三出口改为按前端 `ip_str` 跨 worker 聚合；`test_xlog_roll_f.py` 扩至 23/23，五套既有回归与 fd 探针结论不变；未实施 C，未 rsync、未重启
- **H 阶段（建连与 DNS 治理，2026-10-29，已部署并重启）**：total 改为本轮旱态总事件数（首次为 1）；`_connect_ssl` 按 `(ip_str, 异常类别)` 60s 聚合并保持 Queue 全送达；DNS 对 EHOSTUNREACH/ENETUNREACH 做 per-nameserver 60s 退避与全冷却单点 fail-open。快照测试 30/30 + 7/7 PASS，五套既有回归全 PASS、fd 探针 A(detached)。已部署至 `/Applications/XX-Net`，用户重启后 PID 2066（19:00:28）加载；`_connect_ssl` suppressed 与 total=1 新口径已获生产日志实证，DNS 延长观察约 82 分钟未触发目标 errno，不声称现场疗效
- **I 阶段（前端与 x_tunnel 故障链日志聚合，2026-10-29，未部署）**：dispatcher 改按 `(method, host, 去查询参数 endpoint bytes, front ip)` 聚合 retry/timeout/status；x_tunnel 按事件所属 `(session_id, transfer_no, kind)` 聚合四类 WARN，front retry 按完整 `(host, path)` 聚合；补强 LogThrottle、DNS 与真实初始化实例隔离测试。独立审查两项 Important（非 UTF-8 path、reset 后旧响应错账）已 RED→GREEN 修复；新测试 9/9 + 8/8，综合测试 33/33 + 9/9，五套既有回归与 fd 探针通过；只改日志，未改重试、状态、Queue、评分或传输路径，未 rsync、未重启、未 push
- **J 阶段（下载 SN 超时日志聚合，2026-10-29，未部署）**：复评 H 日志发现 `mark_sn_timeout` 首报 363、renew 379；按 SN 60s 聚合首报/renew，reset 在原锁内同步重建账本，状态更新条件不变。独立审查两项 Important（静默旧事件误计窗口、reset/mark 非原子）已 RED→GREEN 修复；新测试 8/8，全部新旧回归与 fd 探针通过；`req get response timeout` 留待 I 部署后复评，未 rsync、未重启、未 push

## 已知评估后接受项

- 限流键 `id(s)` 复用窗口（±1 秒）；ipv6 探测缓存 300 秒；全 v6 前端池 `not enough ip` 为真实信号保留；seley handshake timeout 属前端池正常淘汰
- F/G 阶段节流边界：`LogThrottle` 某 key 窗口过期后永久沉默时，其最后一轮 suppressed 计数（纯诊断元数据）不结算随进程消失（提案 C 本轮不实施）；信号本体（每窗口首行）始终实时落盘。F 阶段 HEAD 按 worker 记账的结构性限制已由 G 阶段按前端聚合解决；not enough ip 的恢复尾账已由 G 阶段闭环
- `xlog.py` 顶层 `warning()` 笔误调用 `warnin`（官方 5.16.7 既有，385 行附近）：当前无调用方触达，未修，建议上游同步
- 交付包 `installer/XX-Net-5.16.7.zip` 仍为官方原包（补丁仅在本机实装与本地快照），建议上游合并
