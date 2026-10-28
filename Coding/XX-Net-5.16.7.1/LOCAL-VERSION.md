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
| `code/default/smart_router/local/dns_query.py` | 四项修复 1：DoT `connect` 判空（B1/B2：close 保留 + 注释实证、基类 direct_connect fd 泄漏与日志参数修复、代理路径 connect 失败 close）；E 阶段：`_is_usable_nameserver()` nameserver 过滤（拒 fe80::/10、fc00::/7、::1、ff00::/8，放行 IPv4 与全球单播 v6）+ get_local_dns_server 过滤前移与兔底 |
| `code/default/smart_router/local/pipe_socks.py` | 四项修复 2+4：`try_add` fd 竞态（fileno 预检 + ValueError/KeyError DEBUG）；BlockingIOError 限流 `warn_send_block`（同 socket 每秒 1 条 + suppressed 聚合）；B3 try_remove 对称处理；B6 close 时补打 final 并清账 |
| `code/default/x_tunnel/local/tls_relay_front/ip_manager.py` | 四项修复 3：`ipv6_available()` UDP 探测（全球单播判定，缓存 300s）+ get_ip_sni_host 过滤 IPv6 前端；B4 修正 `::` 误判；B5 过滤 INFO 可观测。`seley_front/ip_manager.py` 经相对导入继承 |
| `code/default/lib/darwin/boringssl.abi3.so` | ARM64 原生 BoringSSL（abi3），指纹与官方 x86_64 一致，见 `../../boringssl-arm64/BUILD-macOS-ARM64.md` |
| `code/default/lib/noarch/xlog.py` | F 阶段：① warning_log 10MB 上限单份滚动 `_roll_warning_log_if_needed()`（仅一代 `.1`，磁盘上限约 2×10MB；挂点 4 处：`__init__` 开文件后（66-75 行）、`reset_log_files()` 重开后（111-115 行）、`log()` 写入路径 buffer_lock 内写前检查（261-270 行）；roll 失败重开 fd，再失败跳过写入，与主日志既有失败路径一致）② 通用节流类 `LogThrottle`（385-422 行附近）：`allow(key) -> (allowed, suppressed_count)`，首条立即放行、窗口内仅计数、下一条放行时附 `(suppressed N)` 结算；带锁、512 键过期剪枝；内存只持有 `[ts, count]` 整数对，从不暂存日志内容 |
| `code/default/smart_router/local/dns_server.py` | F 阶段：`reserved/frag` 包字段检查 `xlog.warn` → `xlog.debug`（159-165 行，附依据注释；实测 14,897+ 条/天的解析包字段检查非运行故障，DEBUG 仍进 start_log 可查） |
| `code/default/lib/noarch/front_base/connect_manager.py` | F 阶段：`not enough ip` 告警 60s 节流（沿用既有 `no_ip_lock`，串行语义与 `_need_more_ip` 轮询不变；新增 `last_no_ip_warn`/`suppressed_no_ip_warn` 生命周期计数器；输出行附 `(suppressing for 60s after this line)` 标记）。数据路径零改动 |
| `code/default/lib/noarch/front_base/http_dispatcher.py` | F 阶段：`retry_task_cb` 轨迹 WARN 按 task.url 60s 节流（`LogThrottle(60)` 实例 `retry_log_throttle`；首条立即打、窗口内计数、下一条放行附 `(suppressed N)`）。重试状态机零改动，仅告警行走节流 |
| `code/default/lib/noarch/front_base/http1.py` | F 阶段：`head_request` 探测失败 WARN+trace 对按 worker 60s 节流（`last_head_fail_warn`/`suppressed_head_fail_warn` worker 属性；warn/close 仅在该 worker 自己的 work_loop 线程调用，单线程不变式免锁；trace 行附 `(suppressed N)`）。探测/recheck/close 逻辑零改动 |
| `test_xlog_roll_f.py` | F 阶段第 7 套离线测试（仓库根）：warning_log 滚动 4 项（未超限不滚 / 超限滚出 `.1` 主文件重开 / 滚动后写入继续 / 二次滚动 `.1` 单代替换）+ LogThrottle 语义 6 项（首条放行 / 窗口抑制 / 计数累计 / 键隔离 / 窗口过期 / 并发 512 剪枝）= 10/10 PASS |

## 修复阶段索引（详见 docs/XX-Net-macOS-ARM64-修复记录.md）

- **四项修复**（2026-10-26 晚，日志高频告警治理）：① DoT AttributeError 判空；② try_add fd 竞态；③ 无 IPv6 时前端池过滤（含 seley）；④ BlockingIOError 日志限流
- **B1-B6**（质检与边界完善）：A1 wrap_socket fd 语义实证；B1 注释实证；B2 基类既有隐患（fd 泄漏 + 日志参数 TypeError）；B3 try_remove 对称 DEBUG；B4 `::` 误判修正；B5 过滤 INFO；B6 close 限流账本闭环
- **根修（按启用状态过滤轮询）**：upload_logs.py + status.html 调用方先查 `get_config` 启用状态，gae_proxy 未启用时零 404
- **E 阶段（日志收敛）**：dns_query.py nameserver 过滤（resolv.conf link-local v6 污染治理，`skip unusable DNS server` INFO + CN/通用兔底）
- **D 阶段**：重启上线与回归审计（2026-10-27 05:50 重启）；E 阶段重启 12:47（PID 27738），回归零新增
- **F 阶段（日志治理，2026-10-27 晚—10-28 午）**：T1 历史日志归档止损（7 份约 237MB，commit `7738967`）；T2 真问题三分类清单（残留 bug 零修复级）；T3 五文件日志机制改造（warning_log 10MB 滚动、`LogThrottle` 节流、reserved/frag 降 DEBUG、not enough ip / retry / HEAD 探测三处告警 60s 节流）+ 第 7 套离线测试 `test_xlog_roll_f.py`（10/10）；T4 重启后审计（2026-10-28 10:44 用户重启 PID 14268）：seley retry trace 846→3（-99.6%）、not enough ip tls_relay 2,066→7 全带抑制标记、reserved/frag 32,596→0、7 份日志 ERROR 合计 0、增速受控

## 已知评估后接受项

- 限流键 `id(s)` 复用窗口（±1 秒）；ipv6 探测缓存 300 秒；全 v6 前端池 `not enough ip` 为真实信号保留；seley handshake timeout 属前端池正常淘汰
- F 阶段节流边界：某 key 窗口过期后永久沉默时，其最后一轮 suppressed 计数（纯诊断元数据）不结算随进程消失；信号本体（每窗口首行）始终实时落盘；seley HEAD 探测失败未降已定性为上游前端波动 + 每 worker 生命周期至多 1 条的结构必然（`http1.py:112-115` close 路径），非回归
- `xlog.py` 顶层 `warning()` 笔误调用 `warnin`（官方 5.16.7 既有，385 行附近）：当前无调用方触达，未修，建议上游同步
- 交付包 `installer/XX-Net-5.16.7.zip` 仍为官方原包（补丁仅在本机实装与本地快照），建议上游合并
