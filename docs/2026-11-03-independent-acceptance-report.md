# 2026-10-26 以来全部变更独立验收报告

日期：2026-11-03  
范围：23 张变更卡（A01–A23），不以既有 TDD 通过作为验收结论。  
边界：验收阶段生产仅只读检查和正常请求，故障注入均在临时目录、替身边界或独立 socket 中完成。  
部署追加：独立验收完成后，用户于 2026-11-03 明确授权部署；6 个修复文件已备份并部署，Launcher 已重启为 PID 33775，未 push。

## 总结

- 验收时独立黑盒：**9/10 PASS**；唯一 FAIL 是旧 `simple_http_server` 的 HTTP/1.1 长连接收尾问题，不是 A03 的线程快照竞态。部署重启后复验为 **10/10 PASS**，但 F04 根因未修，不能据单次通过宣称修复。
- 独立模型、随机输入、故障注入和生命周期验收：**30/30 PASS**。其中六个红队反例先以 4 FAIL、2 ERROR 独立复现，再修复为 GREEN。
- 本次独立验收共确认并修复 **9 个**此前测试未覆盖的缺陷；6 个相关运行文件已部署且 Launcher 已重启，常规生产黑盒通过，特定故障路径仍未在生产注入验证。
- 修复后的原新旧回归全部通过：统一日志 19、视图 10、诊断包 10、生命周期契约 1、启动竞态与隔离、DNS 9、dispatcher 12、x_tunnel 14、SN 9、旧 xlog 38，以及 DoT、selector、IPv6、BlockingIOError 和 DNS 地址过滤测试。
- 不能把本报告理解为“所有真实环境场景均已验证”：A01 整卡因 GUI 未操作而保持 **UNVERIFIED**；实际依赖安装、特殊远端故障、诊断包上传及新修复的生产加载也未覆盖。

## 独立发现并修复的缺陷

### F01：半截错误上下文吞掉后续真实 ERROR（A22）

- **反例：** 文件包含 `ERROR_CONTEXT_BEGIN` 和 DEBUG 行，但没有 `ERROR_CONTEXT_END`，其后存在真实 ERROR。
- **实际：** `export_view("warning")` 返回空，后续 ERROR 被并入未闭合上下文块。
- **影响：** 异常截断或损坏时，诊断视图可能恰好丢失最重要的错误本体。
- **修复：** [xlog.py](Coding/XX-Net-5.16.7.1/code/default/lib/noarch/xlog.py) 对未闭合块逐条放回分组，不再吞掉后续记录。
- **复验：** `test_export_incomplete_context_does_not_hide_error` RED→GREEN。

### F02：DNS 将 `3000::/4` 误判为全球单播（A09）

- **反例：** 随机 IPv6 与 `ipaddress.ip_network("2000::/3")` 独立模型对照，得到 `0312:d463:...` 等反例。
- **原因：** 字符串 `startswith(("2", "3"))` 同时接受 `2000::/3` 和不在该范围内的 `3000::/4`。
- **影响：** 非全球单播地址可能被选为上游 DNS，重新引入不可达告警和退避。
- **修复：** [dns_query.py](Coding/XX-Net-5.16.7.1/code/default/smart_router/local/dns_query.py) 用 `inet_pton` 后检查首字节 `& 0xE0 == 0x20`，非法地址拒绝。
- **复验：** 1000 个随机 IPv6 加边界样本全部匹配独立模型。

### F03：selector 已注销后本地集合残留（A07）

- **反例：** socket 位于 `read_set`，selector 的 `unregister()` 抛 `KeyError` 表示已注销。
- **实际：** 异常被 DEBUG 吞掉，但 `read_set/write_set` 没清理。
- **影响：** 已关闭 socket 可残留在集合中，被后续遍历或 stop 再次处理。
- **修复：** [pipe_socks.py](Coding/XX-Net-5.16.7.1/code/default/smart_router/local/pipe_socks.py) 在已知关闭/重复注销分支 `discard()` 对应集合。
- **复验：** `test_selector_race_leaves_no_stale_set` RED→GREEN；未知 selector 异常仍保留 ERROR。

### F05：真实 xlog 遇到非 UTF-8 业务 bytes 会抛异常（A16，Critical）

- **反例：** 直接调用真实 `Logger.warn('retry path:%s', b'/download\xff?tid=1')`。
- **实际：** `utils.bytes2str_only()` 在日志内部保护之前严格解码，抛出 `UnicodeDecodeError`；旧业务测试使用 logger 替身，因此全部通过也覆盖不到这一层。
- **影响：** 首条重试日志可能中断 `retry_task_cb`，使 retry_count、重新入队和响应交付失去原有语义。
- **修复：** xlog 仅对解码失败的 bytes 参数使用 UTF-8 replacement 显示，正常 Unicode 和其他参数不变，不让日志编码错误中断业务。
- **复验：** `test_real_logger_accepts_non_utf8_business_fields` 从 `UnicodeDecodeError` 转为 PASS；旧 R9 的非 UTF-8 重试/响应行为回归仍通过。

### F06：畸形 client.json 脱敏失败会回传密码原文（A22，Important）

- **反例：** 畸形 JSON、数组 JSON 和 `null` 均包含 `top-secret`。
- **实际：** 解析或删除字段失败后返回原始 `content`，异常日志还复制完整配置正文。
- **影响：** 诊断包可能泄露登录密码。
- **修复：** [upload_logs.py](Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/upload_logs.py) 改为 fail-closed：失败返回空内容，日志只保留文件路径和异常，不记录配置正文。
- **复验：** `test_malformed_client_config_never_returns_secret` RED→GREEN；合法配置脱敏及随机 zip 独立解压仍通过。

### F07：DNS direct close 异常遮蔽连接失败处理（A06，Important）

- **反例：** `connect()` 抛 `OSError(65)`，随后 `close()` 抛异常。
- **实际：** close 异常从 `direct_connect()` 逸出，而不是按原语义返回 None。
- **修复：** direct 分支采用与 proxy 分支一致的保护性 close，不改变连接失败出口。
- **复验：** `test_close_failure_does_not_replace_connect_handling` 从 ERROR 转为 PASS；DoT direct/proxy 回归通过。

### F08：front stop 清理异常绕过最终尾账（A20，Important）

- **反例：** 任一 `front.stop()` 抛异常，账本中已有 suppressed 计数。
- **实际：** 列表清理和 `front_retry_log_throttle.flush()` 被跳过，重复 stop 仍无法封口。
- **修复：** [front_dispatcher.py](Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/front_dispatcher.py) 用 `try/finally` 保证 flush，同时保留原清理异常向调用方传播。
- **复验：** `test_front_stop_failure_still_seals` RED→GREEN；旧 X10/X11 生命周期回归通过。

### F09：HTTP dispatcher stop 清理异常绕过最终尾账（A20，Important）

- **反例：** `close_all_worker()` 抛异常，retry 账本中已有 suppressed 计数。
- **实际：** `retry_log_throttle.flush()` 未执行。
- **修复：** [http_dispatcher.py](Coding/XX-Net-5.16.7.1/code/default/lib/noarch/front_base/http_dispatcher.py) 用 `try/finally` 保证封口，原异常不被吞掉。
- **复验：** `test_dispatcher_stop_failure_still_seals` RED→GREEN；旧 R11/R12 生命周期回归通过。

### F10：IPv4 上游过滤误收组播、链路本地和未指定地址（A09，Minor）

- **反例：** 独立 `ipaddress` 模型对照发现 `0.0.0.0`、`224.0.0.1`、`169.254.1.1` 和有限广播地址被接受。
- **影响：** 不可直接路由的地址可能进入上游 DNS 池。
- **修复：** IPv4 先经 `inet_pton` 校验，再拒绝 unspecified、link-local、multicast/reserved 和 `127.0.0.1`；保留 `127.0.0.53` 等本地 stub resolver 的既有设计。
- **复验：** 边界样本与 1000 随机 IPv6 全部符合独立模型；旧 DNS nameserver 过滤回归通过。

## 仍未修复的生产发现

### F04：旧 Web Server 的 HTTP/1.1 长连接并发收尾（独立于 A03）

- 默认 `urllib` 的 2/4/8 路并发在 `/config` 和 `/debug` 均可出现 `ConnectionResetError(54)` 或超时，因此与 `/debug` 处理内容无关。
- `/debug` 正常响应约 20–80ms，不是耗时处理导致。
- 显式 `Connection: close` 后，两种页面共 48 个分层请求全部 200；最终生产黑盒中 `/debug` 的 24 个显式关闭请求也全部 200。
- 同期 Launcher 统一正文没有对应异常堆栈。
- 该问题位于此前未修改的 `simple_http_server` 长连接语义，不归因于 2026-10-26 后的线程快照修复；本轮没有扩大范围修改或部署它。

## 23 卡逐项结论

| ID | 结论 | 本次独立证据与边界 |
|---|---|---|
| A01 | UNVERIFIED | 生产 BoringSSL 为真实 arm64 Mach-O，启动无相关 import 异常；但 GUI 托盘未实际操作，整卡不判 PASS。 |
| A02 | PASS | `bash -n`、无 `sudo pip3`、真实 import 判定。**实际缺失依赖安装流程 UNVERIFIED，未主动安装。** |
| A03 | PASS | 8 路、24 个显式关闭连接的真实 `/debug` 全 200，无 KeyError/no-module 异常；旧长连接另记 F04。 |
| A04 | PASS | 真实配置中 gae_proxy 禁用，黑盒请求集合不含该模块；视图收集的启用过滤与 fail-safe 经独立桩验证。 |
| A05 | PASS | 初始化顺序审查：DNS/UDP 对象均先于代理 `start()`；生产代理 204，启动无 NoneType。 |
| A06 | PASS→F07→PASS | direct close 异常不再改变失败出口；direct/proxy 异常路径均 close，proxy 原异常不遮蔽。 |
| A07 | PASS→F03→PASS | 独立 selector 反例修复；未知异常仍 ERROR；集合清理复验通过。 |
| A08 | PASS | 真实非阻塞 socketpair 注入 BlockingIOError，业务发送/关闭语义不变，尾账守恒。 |
| A09 | PASS→F02/F10→PASS | IPv4 边界和 1000 随机 IPv6 差分；全冷却仅最早到期 fail-open、到期清理符合模型。 |
| A10 | PASS | reserved/frag 仅 DEBUG 且不查询；过短包仍走异常记录，不误降级。 |
| A11 | PASS | `::`、loopback、link-local、ULA、全球 v6 和范围外地址探测对照；无 v6 时 IPv4 保留。 |
| A12 | PASS | 20 轮随机 Unicode/尺寸/滚动、5000 事件参考节流模型、4000 并发计数守恒。 |
| A13 | PASS | 1/2/17/120 次旱态 episode，恢复 total 精确、仅结算一次、账目清零。 |
| A14 | PASS | 500 随机成功/异常输入 Queue 全送达且对象不变，聚合键独立。 |
| A15 | PASS | HEAD 三出口返回值和 request_onway 不变，同 front 共用聚合账本。 |
| A16 | PASS→F05→PASS | 真实 xlog 非 UTF-8 bytes 反例修复；1000 随机键对照含 host=None；旧 12 项业务回归通过。 |
| A17 | PASS | reset 前后同 transfer 不同 session 独立入账；旧业务状态差分回归通过。 |
| A18 | PASS | 完整 path 不被错误合并，stop 尾账与封口符合独立账本。 |
| A19 | PASS | 2000 随机 SN/新旧 t 对照业务状态；reset 清空、同号新生命周期重开。 |
| A20 | PASS→F08/F09→PASS | front 和 dispatcher 清理异常均结算且原异常保留；ProxySession 四类故障及 N 的 16 种 stop 组合通过。 |
| A21 | PASS | 16 种资源 stop 异常组合均逆序尝试全部资源，ready=False，原代理异常保留。 |
| A22 | PASS→F01/F06→PASS | 半截上下文修复、畸形配置 fail-closed；随机 zip、预算、密码、单视图失败及生产视图/JSON 通过。 |
| A23 | PASS | 13 份 `.orig` 同时匹配 SHA 清单与固定 Git 基线；未重新 truncate 或删除旧归档。 |

“PASS→Fxx→PASS”表示本轮先取得独立反例、再修复并复验，不是原有测试的事后复述。F04 独立于卡片修复，验收时 FAIL，部署重启后复测 PASS，但根因仍未修复。

## 已知接受风险与未验证项

- **ACCEPTED RISK：** HTTP/1 类级 HEAD 账本无安全全局终点，异常退出可能丢失最后 suppressed 注解；首报仍保留。
- **ACCEPTED RISK：** PipeSocks 的 `id(socket)` 复用窗口和 IPv6 300 秒缓存是既有明确取舍。
- **ACCEPTED RISK（Minor）：** 纯 IPv6 候选全部被过滤时缺少额外的 skip 汇总 INFO；fallback 行为已有验证，本轮不为此扩大修改。
- **UNVERIFIED：** macOS 托盘 GUI（因此 A01 整卡 UNVERIFIED）、缺失依赖的真实安装、特殊远端网络故障、诊断包实际上传。
- **F04：** 历史 FAIL；重启后黑盒及追加三轮 HTTP/1.1 并发均 PASS，但未修改根因，不能宣称已修复。
- **UNVERIFIED：** F01–F03、F05–F10 的特定生产故障路径；相关文件已部署且进程已重启，但非 UTF-8、清理异常和脱敏失败等故障未在生产主动注入。

## 可复现命令

```bash
python3 -I tests/acceptance_independent_models.py
```

```bash
python3 -I tests/acceptance_production_blackbox.py
```

第二条必须保留 F04 用例，不能为了“全套绿”删除；它在验收时非零退出，部署重启后为 10/10 PASS。独立测试中的六个红队反例必须先失败再通过的记录保存在本轮执行日志中；独立模型当前最终结果为 30/30 PASS。
