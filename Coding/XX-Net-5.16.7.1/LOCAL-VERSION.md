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

## 修复阶段索引（详见 docs/XX-Net-macOS-ARM64-修复记录.md）

- **四项修复**（2026-10-26 晚，日志高频告警治理）：① DoT AttributeError 判空；② try_add fd 竞态；③ 无 IPv6 时前端池过滤（含 seley）；④ BlockingIOError 日志限流
- **B1-B6**（质检与边界完善）：A1 wrap_socket fd 语义实证；B1 注释实证；B2 基类既有隐患（fd 泄漏 + 日志参数 TypeError）；B3 try_remove 对称 DEBUG；B4 `::` 误判修正；B5 过滤 INFO；B6 close 限流账本闭环
- **根修（按启用状态过滤轮询）**：upload_logs.py + status.html 调用方先查 `get_config` 启用状态，gae_proxy 未启用时零 404
- **E 阶段（日志收敛）**：dns_query.py nameserver 过滤（resolv.conf link-local v6 污染治理，`skip unusable DNS server` INFO + CN/通用兔底）
- **D 阶段**：重启上线与回归审计（2026-10-27 05:50 重启）；E 阶段重启 12:47（PID 27738），回归零新增

## 已知评估后接受项

- 限流键 `id(s)` 复用窗口（±1 秒）；ipv6 探测缓存 300 秒；全 v6 前端池 `not enough ip` 为真实信号保留；seley handshake timeout 属前端池正常淘汰
- 交付包 `installer/XX-Net-5.16.7.zip` 仍为官方原包（补丁仅在本机实装与本地快照），建议上游合并
