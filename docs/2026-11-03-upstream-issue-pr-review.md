# XX-Net 官方近期 Issues / PR 对照评估

日期：2026-11-03
官方仓库：https://github.com/XX-net/XX-Net
本地基线：官方 5.16.7 发布包 + 本地补丁，快照 `Coding/XX-Net-5.16.7.1`。
范围：只读调研，不修改运行代码、不部署、不合并。

## 结论摘要

官方 `master` 相对本地 5.16.7 基线只有两项实质提交尚未纳入：**TLS Relay 选线评分调整**和 **Linux iptables 幂等清理**。两项都值得进入后续实施计划，但应分别写回归测试并做语义迁移，不应整分支覆盖。

另有两项可条件纳入：`encrypt.py` 的 Python 3 bytes 修复（仅影响可选应用层加密）和 GAE 服务端 Flask 3.1.3 安全更新（当前 GAE 禁用）。GAE 连接池归零、智能路由慢等开放 Issue 尚无根因或补丁，不宜直接改成代码。

官方大型 PR #14184 已被官方回退，不能整体采用；安全审计 Issue #14183 主要是自动扫描结果，必须逐项验证，不能按“Critical”标签批量修改。

## 检索范围与限制

- 官方默认分支：`master`；最新发布：5.16.7（2026-09-28）。
- 官方比较 [`5.16.7...master`](https://github.com/XX-net/XX-Net/compare/5.16.7...master)：`ahead_by=2`，涉及 4 个文件。
- 拉取并按更新时间检查最近 60 个 Issues 和 PR 列表；重点阅读了有明确错误、补丁或本地相关性的条目。Issue 列表覆盖 2025-04-29 至 2026-10-27，PR 列表还包含更早历史项。
- 深读的主要来源：PR #14184、#14004、#14170、#14155；Issue #14188、#14183、#14169、#14128、#14132、#14127、#14117、#14118、#14185、#14163、#14171、#14159、#14186、#14134。
- 本地对照了 25 个相对官方 5.16.7 发布包有差异的代码文件，以及独立验收报告和后续路线图。
- 开放 Issue 的用户描述只是问题线索，不等于已复现根因；本报告没有把用户附件中的日志当成独立验证。

## 一、建议纳入后续计划

### 1. Linux `set_iptables` / `unset_iptables` 幂等化 — 建议 P1

- 官方来源：[Issue #14188](https://github.com/XX-net/XX-Net/issues/14188)、[提交 054daa66](https://github.com/XX-net/XX-Net/commit/054daa66)。
- 问题：重复执行 `set_iptables` 会追加重复 NAT/PREROUTING 规则；旧 `unset_iptables` 每条只删一次，残留跳转可能让流量在 unset 后仍被重定向到 8086。
- 本地状态：未包含 `iptables -C` 检查和循环清理逻辑；此前本地启动修复没有覆盖此项。
- 价值：故障后果明确，官方报告者已确认修复；对 Linux/OpenWrt 的可靠性收益高。
- 当前 macOS 影响：无直接运行时收益，但属于同一代码库的跨平台正确性修复。
- 纳入方式：只迁移 `start` 中两个函数和退出码，不带入其他 shell 改动。用临时 fake `iptables` 命令记录调用，覆盖首次、重复、历史重复规则、多接口、无规则清理；不在本机执行真实 iptables。

### 2. TLS Relay 使用 RTT、预期传输成本和服务端 `adjust` 选线 — 建议 P1

- 官方来源：[提交 8613cd7b](https://github.com/XX-net/XX-Net/commit/8613cd7b)。
- 改动：`front.py` 接收每条线路的 `adjust`；`ip_manager.py` 从“速度最大”改为 `rtt + 预期包成本 + adjust` 最小；`dispather_work_max_score` 从 1 调为 2。
- 本地状态：未包含；本地同文件另有 IPv6 可用性过滤补丁，不能整文件覆盖。
- 价值：让中心下发的线路修正值真正参与 front 选择，比单看吞吐更符合实际请求延迟；也与官方 Issue [#14117](https://github.com/XX-net/XX-Net/issues/14117) 的“自动选线不是最优”方向一致，但该 Issue 不能作为本提交疗效证明。
- 风险：`adjust` 来自服务端，需验证缺省、负数、非数字和极大值；评分公式需测试 speed=0/缺失边界，worker max score 改动可能影响线路切换频率。
- 纳入方式：手工迁移三个语义点，保留本地 IPv6 过滤；以固定 RTT/speed/adjust 表做选择顺序测试，再做正常请求 A/B 观察，不宣称必然提速。

## 二、条件纳入或拆取局部修复

### 3. `encrypt.py` Python 3 bytes 兼容 — 建议 P2，拆取最小修复

- 官方来源：[PR #14184](https://github.com/XX-net/XX-Net/pull/14184) 中的 `encrypt.py` 差异；该 PR 整体已回退，见下文。
- 本地确认：`Encryptor.encrypt()` 第一次发送 IV 后把 `head` 设为字符串 `""`，后续与 bytes 密文拼接会触发 `TypeError`；纯 Python RC4 fallback 仍使用 `ord/chr` 和字符串结果，也不符合 Python 3 bytes 数据流。
- 价值：是真实代码级缺陷，不是扫描器猜测。
- 当前优先级较低的原因：X-Tunnel `encrypt_data` 默认关闭，当前生产主路径未启用可选应用层加密。
- 纳入方式：只处理 bytes head 和 RC4 bytes 输入/输出，使用独立已知向量、多次连续 encrypt、空输入和分包 decrypt 测试；不引入该 PR 的 Launcher/config 改动，也不把协议要求的 MD5 EVP_BytesToKey 擅自替换成 SHA-256。

### 4. GAE 服务端 Flask 3.0.0 → 3.1.3 — 建议 P2/条件纳入

- 官方来源：[开放 PR #14155](https://github.com/XX-net/XX-Net/pull/14155)。
- 本地确认：`gae_proxy/server/gae/requirements.txt` 仍固定 `Flask==3.0.0`。
- 价值：上游 Flask 3.1.3 是安全修复版本，改动只有一行依赖。
- 限制：影响 GAE 服务端部署，不是当前 macOS 客户端热路径；本地 GAE 当前禁用，PR 也尚未由官方合并。
- 纳入方式：如果仍维护/部署 GAE 服务端，则先核对 AppEngine Python 版本、Werkzeug 等依赖并做服务端请求回归，再升级；若只运行当前客户端，可暂记观察，不为“版本新”而立即改。

## 三、继续观察，不直接形成代码修复

### 5. GAE 连接池归零 / 睡死

- 来源：[Issue #14128](https://github.com/XX-net/XX-Net/issues/14128)、[Issue #14132](https://github.com/XX-net/XX-Net/issues/14132)。
- 状态：均开放，无根因分析、修复 PR 或可迁移补丁。
- 判断：当前本地 GAE 禁用，不应凭两条报告重写 front_base；如果以后启用 GAE，应先采集连接池、worker 生命周期、no-IP 和创建失败指标，再判断是否与本地 front 锁/deadline/资源边界任务共用根因。

### 6. 智能路由慢、连接异常、not enough IP

- 来源：[Issue #14118](https://github.com/XX-net/XX-Net/issues/14118)、[#14171](https://github.com/XX-net/XX-Net/issues/14171)、[#14163](https://github.com/XX-net/XX-Net/issues/14163)、[#14185](https://github.com/XX-net/XX-Net/issues/14185)。
- 判断：这些报告支持继续重视 DNS、Direct 建连、front 恢复和性能观测，但没有证明一个共同根因。本地路线图已覆盖 HTTP 状态、front 锁、DNS 历史、建连资源、deadline 和 profiling，无需另起“全面提速”任务。
- #14171 日志同时出现 Direct 失败、front 602 和多类 DNS 路径，不能只凭日志选择修改某一个模块。

### 7. Linux 服务显示 active 但代理端口未监听

- 来源：[Issue #14159](https://github.com/XX-net/XX-Net/issues/14159)。
- 判断：更像 service 启动方式/进程退出与状态报告不一致，Issue 没有 Launcher 日志，无法确认与本地启动竞态相同。可后续增加 Linux service 就绪检查文档，不建议直接改 Launcher。

## 四、明确不建议纳入

### 8. 不整体采用 PR #14184 的 Launcher/config 改造

- 官方来源：[PR #14184](https://github.com/XX-net/XX-Net/pull/14184)、[官方回退 12bb406c](https://github.com/XX-net/XX-Net/commit/12bb406c)。
- 官方回退原因：`start.py` 引入 PR 未提供、仓库也不存在的 `audit` 模块，导致全平台启动时 `ModuleNotFoundError`。
- 该 PR 还一次修改 3 个核心文件、数百行，包括配置迁移、信号处理、端口预绑定和退出行为，回归面远大于局部收益。
- 结论：当前官方 5.16.7/master 已不包含这套 Launcher/config 改造；本地也不应复活。仅把其中可独立证明的 encrypt 问题作为参考。

### 9. 不按安全扫描 Issue #14183 批量修改

- 来源：[Issue #14183](https://github.com/XX-net/XX-Net/issues/14183)。
- 报告自称由 bandit/semgrep 等自动生成并建议人工验证，未附逐条可利用性证明。
- 本地核实：`gae_proxy/server/gae/main.py` 的 `debug=True` 位于绑定 `127.0.0.1` 的直接运行入口，不能直接等同于 AppEngine 生产远程 debugger；MD5/SHA1 也包含协议兼容和 TLS 实现用途，不能全局替换成 SHA-256。
- `cert_util.py` 的 `os.system`、旧 TLS 版本、更新文件权限和 urllib3 可分别做人工安全审计，但不应以该 Issue 的“Critical”数量作为合并依据。

## 五、已覆盖或无需重复纳入

| 官方条目 | 本地判断 |
|---|---|
| [PR #14004](https://github.com/XX-net/XX-Net/pull/14004) IPv6 listen 地址线程名格式化 | 本地 `simple_http_server.py` 已含 `%s:%d` 修复，不重复。 |
| [Issue #14169](https://github.com/XX-net/XX-Net/issues/14169) / [PR #14170](https://github.com/XX-net/XX-Net/pull/14170) WebSocket MASK bit | 本地已含 `is_masked` 分支，不重复；这不等于 HTTP 长连接 F04 已修复。 |
| [Issue #14186](https://github.com/XX-net/XX-Net/issues/14186) M4 无托盘、PyObjC `AttributeError('objc')` | 本地 `mac_tray.py` 已做 PyObjC 11.x 兼容；生产 GUI 操作仍未独立验收。 |
| [Issue #14134](https://github.com/XX-net/XX-Net/issues/14134) Apple Silicon 启动无反应 | 本地已有 ARM64 BoringSSL、启动错误显示、quarantine/Rosetta 处理；不重复迁移。 |
| [Issue #14127](https://github.com/XX-net/XX-Net/issues/14127) 5.16.5 `host_info` tuple/dict 错误 | 官方在 5.16.6 修复，本地 5.16.7 基线已覆盖。 |

## 推荐后续顺序

1. 在现有 P0 正确性任务之外，新增 **iptables 幂等清理**独立任务；它不干扰 macOS 主路径。
2. 新增 **TLS Relay adjust 选线**任务，与 front 锁/deadline 修改分开提交，便于判断效果与回退。
3. 将 **可选加密 bytes 修复**记为 P2，只有测试证明连续加密路径后再改。
4. Flask 更新只在仍维护 GAE 服务端时实施；GAE 睡死和泛化性能 Issue 继续依赖测量，不直接合入猜测性修复。
