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
