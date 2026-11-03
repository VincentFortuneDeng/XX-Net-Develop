# 可靠性与性能路线图部署及状态记录

日期：2026-11-03  
初始部署来源提交：`7e888ae`；轻量控制器修复来源提交：`2c1d0ff`
授权：用户明确回复“可以进行部署，并梳理当前完成状态和后续工作”。

## 部署范围

将开发快照 `Coding/XX-Net-5.16.7.1` 与 `/Applications/XX-Net` 比较后，部署 15 个存在差异的 Python 运行文件；不包含测试、文档、配置、账号信息或生产数据。

1. `code/default/launcher/start.py`
2. `code/default/lib/noarch/front_base/http_common.py`
3. `code/default/lib/noarch/front_base/http_dispatcher.py`
4. `code/default/lib/noarch/simple_http_server.py`
5. `code/default/lib/noarch/xlog.py`
6. `code/default/smart_router/local/connect_manager.py`
7. `code/default/smart_router/local/dns_query.py`
8. `code/default/x_tunnel/local/base_container.py`
9. `code/default/x_tunnel/local/config.py`
10. `code/default/x_tunnel/local/direct_front.py`
11. `code/default/x_tunnel/local/front_dispatcher.py`
12. `code/default/x_tunnel/local/proxy_session.py`
13. `code/default/x_tunnel/local/tls_relay_front/config.py`
14. `code/default/x_tunnel/local/tls_relay_front/front.py`
15. `code/default/x_tunnel/local/tls_relay_front/ip_manager.py`

## 备份与文件核验

- 新备份：`backups/pre-rsync-20261103-reliability-roadmap/`。
- 15 份生产原始文件保存为对应路径下的 `*.orig`，部署差异保存为 `*.diff`。
- `SHA256SUMS`、`DEPLOYED-SHA256SUMS`、`DEPLOYED-FILES` 和 `SOURCE-COMMIT` 均已记录。
- 未覆盖或删除既有备份、生产日志和生产数据。
- 15 个生产文件均与开发快照逐字节一致；15 份原始备份及部署清单均通过哈希核验。

## 重启边界与生产只读核验

- 本次只部署文件，**未重启 Launcher**。运行进程仍为 PID 33775，启动时间 2026-11-03 06:29:55，因此尚未加载本次部署代码。
- 首轮生产只读黑盒 8/10：部署来源清单仍引用上一批次固定哈希，旧进程默认 HTTP/1.1 长连接用例出现一次超时。
- 将本批次部署清单加入黑盒来源检查后复验 **9/10 PASS**：27 个累计部署文件来源匹配，配置契约、禁用模块零请求、日志视图、显式 `Connection: close` 和代理 `generate_204=204` 均通过。
- 唯一失败为旧进程的默认 HTTP/1.1 24 路并发 `/debug`，出现 `ConnectionResetError(54)` 和 `TimeoutError`。这只是重启前旧代码状态，不用于判断本轮修复疗效。

## 重启后验收（2026-11-03 11:03–11:12）

**结论：已检测到重启，新代码已激活，但生产验收未通过。**

- 旧 PID 33775 已退出；检测到新 PID 67758 于 **2026-11-03 11:03:15** 从 `/Applications/XX-Net` 启动，监听 1080、8085、8086。该进程变化不是本会话命令触发，启动来源未确认，因此没有再次重启。
- 15 个本批部署文件再次与开发快照及 `DEPLOYED-SHA256SUMS` 逐一核对，全部一致。新 PID 的启动时间晚于部署，且以下行为检查均针对该进程。
- 正式 `tests/acceptance_production_blackbox.py` 连续运行三轮，均在 48 个并发 `/debug` 请求之后进入模块日志视图检查，并在 Smart Router 的 `/module/smart_router/control/log?cmd=get_last&max_line=4` 抛出 `RemoteDisconnected`，脚本中途退出，**没有取得 10/10 PASS**。仅增加一秒请求间隔的诊断运行仍在响应读取阶段失败；未修改正式脚本或生产代码。
- 拆分专项序列另跑五轮：每轮默认 HTTP/1.1 `/debug` 24/24、显式 `Connection: close` 24/24，随后 `get_last` 与 `startup` 日志导出均返回 200。即两种 `/debug` 各累计 120 次成功，但这不能抵消完整黑盒失败，也不能宣称间歇长连接问题已治愈。
- 独立经 127.0.0.1:8086 请求 `http://www.gstatic.com/generate_204`，25 秒后超时，重启后的代理连通项失败。
- 重启后的 `launcher.log` 未扫描到 ERROR/CRITICAL；存在验收脚本故意传入非法 `max_bytes` 的预期 WARNING，以及一次 SwitchyOmega 下载失败 WARNING。
- 11:06:46–11:07:05，`x_tunnel.log` 连续记录访问本地 8085 debug/status/log 接口时 `AttributeError("'NoneType' object has no attribute 'text'")`，与本地 Web API 失败相互印证。DNS、front 建连、TLS 和远端超时 WARNING 也仍存在；11:03:11 的条目属于旧进程停止阶段，不计入新进程告警。
- 最终新 PID 仍存在且三个端口仍监听，说明不是 Launcher 整体退出。现有证据尚不足以把 Web API 断开或代理超时归因于某一个本轮改动。

本轮只进行正常请求和只读诊断，未注入生产故障、未删除日志、未修改配置。完整失败输出、五轮专项结果、告警筛选和进程采样保存在本路线图 SDD 工作区。

## 失败根因诊断（2026-11-03 22:26–22:41）

### 模块 Web API：已确认回归根因

- 逐模块标记证明：完整黑盒已完成 Launcher 的全部日志视图请求，实际失败点是 **Smart Router** 的 `get_last`。此前仅拆分 Launcher 的专项测试因此不能覆盖该失败。
- `simple_http_server.send_response()` 在本轮响应头修复中新增读取 `self.close_connection` 和 `self.request_version`；完整 HTTP handler 会在请求解析时设置它们，但模块使用的轻量 `ControlHandler` 不调用基类构造函数，也没有初始化这两个属性。
- 使用真实 `smart_router.local.web_control.ControlHandler` 做最小复现，调用 `send_response()` 稳定得到 `AttributeError: 'ControlHandler' object has no attribute 'close_connection'`；仅为诊断临时补齐两个属性后，同一对象立即生成正常 200 响应。未修改源码。
- Smart Router 的 log/status、X-Tunnel 的 log/status 均复现断开或超时。`x_tunnel.log` 中 `NoneType.text` 是本地模块 API 无响应后的下游表现，不是独立根因。
- Smart Router、X-Tunnel、GAE Proxy 以及各 front 的七个 `ControlHandler` 都采用相同轻量构造模式，影响范围应按全部模块控制接口处理。
- 既有 keepalive 测试只覆盖经过完整 `handle_one_request()` 的 handler，没有覆盖轻量 `ControlHandler`，所以离线 26 个测试文件全绿仍未发现此回归。

**建议修复顺序：**先增加真实轻量控制器的 RED 回归测试，再在公共响应出口或基类中提供兼容默认状态，避免逐模块复制修复；随后运行完整离线套件、生产全模块日志/状态黑盒和多轮 F04。当前只完成诊断，尚未实施修复。

### 代理超时：当前未持续复现

- 早前一次 25 秒超时之后，相同代理 `generate_204` 连续两次返回 204。
- 最新直连/代理对照中，代理返回 **204、约 0.598 秒**，直连反而在约 5 秒后被断开，说明当前本地代理转发链路可用。
- 早前超时与 DNS、front、TLS 远端告警同时出现，但历史现场不足以确定唯一原因；不把该瞬时超时归入已确认的模块 Web API 回归，也不宣称代理问题已经修复。

## 轻量控制器回归修复与复验（2026-11-03 22:48–23:01）

### 测试先行与公共修复

- 在 `tests/test_simple_http_server_keepalive.py` 直接使用真实 `smart_router.local.web_control.ControlHandler`，不补齐完整 HTTP 请求状态。修复前运行 10 项测试，新增用例如期以 `AttributeError: 'ControlHandler' object has no attribute 'close_connection'` 失败。
- 公共修复只在 `send_response()` 读取两个请求状态时提供兼容默认值：轻量控制器维持原有无 `Connection` 响应头的行为；完整 handler 的显式 close、HTTP/1.0 keep-alive 和 HTTP/1.1 状态保持原路径。未在七个模块中复制补丁。
- 修复后同一文件 10/10 通过，3 项性能基线自验通过，`git diff --check` 和修改文件编译检查通过。代码与回归测试提交为 `2c1d0ff fix: 兼容轻量控制器响应状态`。
- 其余 25 个 `tests/test_*.py` 文件通过；旧 `test_ipv6_filter.py` 因直接加载生产 TLS Relay 文件、配置替身缺少此前评分功能新增的 `ip_cal_expect_time_package_size` 而失败。该失败与本次响应修复无关，本轮未借修测试扩大范围，因此不能写作完整 26 文件全绿。

### 部署、重启与生产结果

- 新备份：`backups/pre-rsync-20261103-control-handler-response/`。只部署 `code/default/lib/noarch/simple_http_server.py`，原文件、差异、原/新哈希和来源提交均已记录，生产文件与开发快照逐字节一致；SHA256 为 `67f00882b2fac7b4551f1fd66c7cb7d287d4192510dc4520aeff021aa95cc752`。
- 旧 PID 67758 正常退出；新 PID **89760** 于 **2026-11-03 22:55:21** 启动，监听 1080、8085、8086。首次黑盒在启动窗口的 Launcher 配置请求被重置，单独复查配置返回 200 后重新验收。
- 随后两次完整黑盒均 **9/10 PASS**：模块日志视图 `A22-http-contract` 和代理 `generate_204=204` 通过，显式 `Connection: close` 的 24 路 `/debug` 通过；唯一失败仍是默认 HTTP/1.1 并发 `/debug` 的重置/超时。不能把本次结果写成生产全量验收通过。
- 五轮拆分专项中，Smart Router、X-Tunnel、Cloudflare、Seley、TLS Relay 的日志与状态请求共 **50/50** 成功；显式 close `/debug` 累计 **120/120** 成功。默认 `/debug` 当时累计 **112/120** 成功，五轮均出现至少一次重置或超时；后续诊断已确认这是客户端代理路由差异，见下节。
- 重启后的生产日志未新增 `close_connection` 或 `NoneType.text` 目标异常；扫描到的同类条目均属于旧进程 11:06–11:07 的历史记录，未删除旧日志。

## 默认 `/debug` 失败路由诊断（2026-11-03 23:10–23:25）

**结论：此前“默认 HTTP/1.1 长连接失败”的归因需要纠正。失败来自验收客户端经本机代理访问 Launcher 的路径，不是 Launcher 默认 HTTP/1.1 响应缺陷。**

- 原始请求抓取证明 `urllib.request.urlopen` 自动发送 `Connection: close`，因此黑盒中的“默认 HTTP/1.1”并不是与显式 close 相对的 keep-alive 用例。
- 同并发矩阵中，`/debug`、`/log` 和 `/config` 都只有 `urllib` 路径出现重置/超时，直接 `http.client` 均为 24/24 成功；失败并不取决于 `/debug` 的线程栈或 FD 收集逻辑。
- 独立标准库 HTTP 服务器和项目 `simple_http_server` 都能复现相同差异。失败请求未进入服务器处理函数，扩大监听队列到 200 也没有改善，说明不是 Launcher 业务处理或响应写出阶段的问题。
- 当前 Python 的脱敏代理配置为 HTTP/HTTPS 均经 `127.0.0.1:8086`，且对 `127.0.0.1` 和 `localhost` 的 `proxy_bypass` 都返回 False。直接 `http.client` 不读取该系统代理，而 `urllib` 会读取，这才是两组黑盒请求的关键路径差异。
- 对独立服务器仅禁用环境代理后，共享 opener 和独立 opener 均为 **96/96** 成功；默认环境代理路径继续出现重置。共享 opener 不是根因，串行 `urlopen` 也曾 96/96 成功。
- 正式黑盒只在诊断进程中让本地请求使用 `ProxyHandler({})`，其余文件来源、模块接口、并发请求和显式生产代理检查全部不变，连续三轮均 **10/10 PASS**，显式代理 `generate_204` 也均为 204。

因此，现有证据支持“本地验收应显式绕过环境代理”，不支持继续修改 Launcher 长连接代码。原脚本经 8086 转发到 8085 时仍有重置/超时，可作为独立的代理转发问题另行评估；三轮诊断性 10/10 不宣称该代理路径已治愈。本轮未修改运行代码、配置或生产进程。

## 当前已完成状态

- 路线图 Task 1–8 全部完成：HTTP 长连接、front 等待隔离、DNS 历史隔离、Direct 资源边界、X-Tunnel 背压、front 绝对 deadline、异步日志 writer、离线性能基线。
- 整支新鲜上下文审查及一轮修复完成：包括 body/Direct deadline、manager 级建连上限、会话注册表竞争、无 ACK 背压恢复、DNS 迟到代际、日志异常和退出尾账。
- 此前两项上游 P1 迁移也已部署：Linux iptables 幂等、TLS Relay 综合选线；本地 IPv6 过滤保留。
- 此前最终离线验证曾记录 26 个测试文件、30 项独立模型和 3 项性能自验全部通过；本次修复复跑为其余 25 个测试文件及 3 项性能自验通过，旧 IPv6 测试配置替身问题单独保留。
- 生产 `simple_http_server.py` 已更新为 `2c1d0ff` 的修复版本并由 22:55:21 启动的 PID 89760 加载；模块 Web API 回归已通过生产复验。原始黑盒因本地 `urllib` 经 8086 代理而保持 9/10，诊断性本地禁用代理后三轮均为 10/10，未 push。

## 后续工作优先级

1. **验收脚本本地请求禁用环境代理（下一步修复）**：已证明默认 `urllib` 经 8086 才产生重置/超时，应让 8085 本地检查显式直连，同时保留显式生产代理检查；不应再据此修改 Launcher 长连接逻辑。
2. **代理转发本地地址另项评估**：原脚本经 8086 访问 8085 的失败可作为独立代理路径问题研究，不与本地 Web API 验收混同。
3. **旧测试环境耦合另项处理**：`test_ipv6_filter.py` 直接加载生产文件且配置替身未同步 TLS Relay 评分字段；需要单独决定改为开发快照并更新配置契约，不与本次运行修复混入。
4. **生产只读观察**：按正常负载观察异步日志尾账、writer 错误计数、front 超时、DNS 和会话背压；不主动注入生产故障，不以拆分用例 PASS 宣称间歇问题治愈。
5. **真实模块离线性能测量**：保留合成账本，增加由离线替身驱动 ConnectManager、ProxySession、front 和 xlog 的测量，采集真实在途数、缓冲字节、writer 等待与丢弃计数。
6. **持续回归与资源门槛**：在同机、同 Python、同种子下比较长 soak 的 p95/p99、线程、FD 和内存；只有 profiling 证明瓶颈后才考虑零复制、时间轮、asyncio 或窗口/worker 调参。
7. **分支集成另行决定**：当前保留 `fix/h-log-dns-backoff`，未合并 main、未 push、未创建 PR。

## 未覆盖边界

生产故障路径、特殊远端行为、GUI、诊断包实际上传和长时间生产 soak 未覆盖。部署文件一致不等于运行进程已加载，更不等于生产疗效已验证。
