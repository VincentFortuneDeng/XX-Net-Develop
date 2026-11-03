# 可靠性与性能路线图部署及状态记录

日期：2026-11-03  
运行代码来源提交：`7e888ae`  
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
- 正式 `tests/acceptance_production_blackbox.py` 连续运行三轮，均在 48 个并发 `/debug` 请求之后的首个 `/log?cmd=get_last&max_line=4` 抛出 `RemoteDisconnected`，脚本中途退出，**没有取得 10/10 PASS**。仅增加一秒请求间隔的诊断运行仍在响应读取阶段失败；未修改正式脚本或生产代码。
- 拆分专项序列另跑五轮：每轮默认 HTTP/1.1 `/debug` 24/24、显式 `Connection: close` 24/24，随后 `get_last` 与 `startup` 日志导出均返回 200。即两种 `/debug` 各累计 120 次成功，但这不能抵消完整黑盒失败，也不能宣称间歇长连接问题已治愈。
- 独立经 127.0.0.1:8086 请求 `http://www.gstatic.com/generate_204`，25 秒后超时，重启后的代理连通项失败。
- 重启后的 `launcher.log` 未扫描到 ERROR/CRITICAL；存在验收脚本故意传入非法 `max_bytes` 的预期 WARNING，以及一次 SwitchyOmega 下载失败 WARNING。
- 11:06:46–11:07:05，`x_tunnel.log` 连续记录访问本地 8085 debug/status/log 接口时 `AttributeError("'NoneType' object has no attribute 'text'")`，与本地 Web API 失败相互印证。DNS、front 建连、TLS 和远端超时 WARNING 也仍存在；11:03:11 的条目属于旧进程停止阶段，不计入新进程告警。
- 最终新 PID 仍存在且三个端口仍监听，说明不是 Launcher 整体退出。现有证据尚不足以把 Web API 断开或代理超时归因于某一个本轮改动。

本轮只进行正常请求和只读诊断，未注入生产故障、未删除日志、未修改配置。完整失败输出、五轮专项结果、告警筛选和进程采样保存在本路线图 SDD 工作区。

## 当前已完成状态

- 路线图 Task 1–8 全部完成：HTTP 长连接、front 等待隔离、DNS 历史隔离、Direct 资源边界、X-Tunnel 背压、front 绝对 deadline、异步日志 writer、离线性能基线。
- 整支新鲜上下文审查及一轮修复完成：包括 body/Direct deadline、manager 级建连上限、会话注册表竞争、无 ACK 背压恢复、DNS 迟到代际、日志异常和退出尾账。
- 此前两项上游 P1 迁移也已部署：Linux iptables 幂等、TLS Relay 综合选线；本地 IPv6 过滤保留。
- 最终离线验证：26 个测试文件、30 项独立模型和 3 项性能自验全部通过。
- 当前运行代码提交 `7e888ae` 已部署到生产目录，并由 11:03:15 启动的新进程加载；重启后验收未通过，未 push。

## 后续工作优先级

1. **重启验收已执行但未通过（下一步先诊断）**：优先复现并定位完整黑盒中的本地 Web API 断开，同时区分代理超时是本地转发故障还是远端/front 不可用；未定位前不继续修改生产代码。
2. **生产只读观察**：按正常负载观察异步日志尾账、writer 错误计数、front 超时、DNS 和会话背压；不主动注入生产故障，不以拆分用例 PASS 宣称间歇问题治愈。
3. **真实模块离线性能测量**：保留合成账本，增加由离线替身驱动 ConnectManager、ProxySession、front 和 xlog 的测量，采集真实在途数、缓冲字节、writer 等待与丢弃计数。
4. **持续回归与资源门槛**：在同机、同 Python、同种子下比较长 soak 的 p95/p99、线程、FD 和内存；只有 profiling 证明瓶颈后才考虑零复制、时间轮、asyncio 或窗口/worker 调参。
5. **分支集成另行决定**：当前保留 `fix/h-log-dns-backoff`，未合并 main、未 push、未创建 PR。

## 未覆盖边界

生产故障路径、特殊远端行为、GUI、诊断包实际上传和长时间生产 soak 未覆盖。部署文件一致不等于运行进程已加载，更不等于生产疗效已验证。
