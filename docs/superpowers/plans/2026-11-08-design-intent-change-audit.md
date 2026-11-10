# XX-Net 设计意图与变更损益审计实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完整入账并审计 2026-10-27 起除日志压缩修复外的逻辑变化，还原功能契约和跨层执行流，用双向证据确认优化损益、最小修复确认缺陷，并以独立 QA、完整回归和条件式生产验收收敛。

**Architecture:** 以逻辑变化 ID 为审计单位，以功能契约和资源所有权为主线。先冻结不可变输入、hunk 分母、核心流程和测试清单，再分组还原执行流并筛查损益，随后用确定性交错验证高风险项；跨层平衡点单独复审，最后才进入修复、完整回归和条件式部署。

**Tech Stack:** Python 3、unittest、Git、CodeGraph、threading/queue/selectors、本地 socketpair、事件门、受控时钟和故障替身。

**Spec:** `docs/superpowers/specs/2026-11-08-design-intent-change-audit-design.md`

## Global Constraints

- 沿用当前普通 checkout 和分支；不新建 worktree，不自行 commit、push、合并或创建 PR。
- 主行为基线为 `68d8506`，已提交范围为 `68d8506..5dc3f12`，当前工作树单独入账；日期边界核查覆盖 `f1aa5bc..68d8506`，根快照另与上游参照树对照。
- 产品范围包括整个 `Coding/XX-Net-5.16.7.1/` 的真实安装、启动和运行入口，不限于 `code/default/`；顶层 `start` 必须入账。
- 日志压缩修复本身不作为目标；纯日志内部实现只登记排除项。其他日志变化若影响调用线程、公共接口、资源或生命周期，仍审计其外部损益；混合提交必须拆分。
- 优先使用 CodeGraph 定位符号和调用链，再读真实实现；`upstream/`、备份和第三方库只作必要参照。
- Python 使用 `python3 -I`；不读取或输出敏感配置，不执行真实 iptables，相关测试只用临时 fake 命令。
- 原始生产日志和完整配置响应不得进入工具输出、报告或证据目录；URL/query、Host、DNS、IP、cookie/authorization、账号/token/session_id 按规范禁止或脱敏。
- 不删除旧日志、备份和 SDD 证据；无效运行保留且不计入。独立审查只产生候选，重要发现必须由主线程核实。
- 历史基线对照只读取 Git 对象并写入新的空临时目录；不得 checkout、覆盖或清理当前工作树。
- 每项产品修复先准确 RED，再最小 GREEN；不得放宽断言、放大超时或只测替身制造通过，不擅改规范列出的 C/D 级契约。
- 只有本轮实际产生产品修复、批准的执行范围包含部署且全部离线门槛通过时才进入任务 8；纯审计、有意保持或未获部署授权不部署。

## Review Focus

- 公共入口的参数、返回值和完成语义可能被内部测试掩盖；任务 2—5 必须加入旧调用方/兼容面断言。
- 为异常路径增加的期限、容量和背压可能损伤慢网络或正常高并发；任务 2—6 必须保留正常路径和边界恢复正例。
- 停止、替换和晚到回调可能跨层污染新对象；任务 2—5 必须分别验证事件发生与实际执行之间的交错。
- 性能与资源收益可能来自合成指标、不可比环境或修复前的旧代码；任务 2—6 必须预注册协议并复验最终代码。
- 文件级覆盖、重复 GREEN 或局部卡片关闭可能掩盖漏审 hunk、漏跑测试及未覆盖高风险项；任务 1、6、7 必须用分母和终态矩阵机检。
- 部署哈希、生产漂移或敏感配置读取可能使离线 GREEN 失效；任务 8 必须绑定验收快照、检测漂移并使用安全黑盒。

## File and Artifact Structure

执行证据统一位于 `.superpowers/sdd/2026-11-08-design-intent-change-audit/`：

- `baseline.json`：不可变输入快照、Git 与生产版本事实。
- `changes.json`：逻辑变化、hunk、文件映射及 revision。
- `flows.json`：核心流程分母和契约覆盖状态。
- `test-manifest.json`：测试命令、代码根、哈希、超时、预期类型和结果。
- `validate_coverage.py`、`run_suite.py`：只读覆盖校验和 manifest 驱动验收工具。
- `contracts/`：front、Smart Router、X-Tunnel、生命周期四组契约。
- `risks.md`、`balance.md`：风险状态与预注册损益矩阵。
- `progress.md`：裁定、RED/GREEN、无效证据、修复与审查台账。
- `coverage.json`：最终覆盖、排除、deferred 和缺口。

产品文件只在确认缺陷后按对应任务修改；最终报告为 `docs/2026-11-08-design-intent-change-audit.md`。证据、报告和审计工具写入不改变原始被审计快照；产品修复以原变化项的 revision 记录。

---

### Task 1: 冻结基线并建立完整变化、流程和测试分母

**Files:**
- Create: `.superpowers/sdd/2026-11-08-design-intent-change-audit/baseline.json`
- Create: `.superpowers/sdd/2026-11-08-design-intent-change-audit/changes.json`
- Create: `.superpowers/sdd/2026-11-08-design-intent-change-audit/flows.json`
- Create: `.superpowers/sdd/2026-11-08-design-intent-change-audit/test-manifest.json`
- Create: `.superpowers/sdd/2026-11-08-design-intent-change-audit/risks.md`
- Create: `.superpowers/sdd/2026-11-08-design-intent-change-audit/balance.md`
- Create: `.superpowers/sdd/2026-11-08-design-intent-change-audit/progress.md`
- Create: `.superpowers/sdd/2026-11-08-design-intent-change-audit/validate_coverage.py`
- Create: `.superpowers/sdd/2026-11-08-design-intent-change-audit/run_suite.py`

**Interfaces:**
- Produces: `CHG-C-<nnn>`、`CHG-W-<nnn>`、`EXC-<nnn>` 稳定 ID；每项含 `sources`、`hunks`、`files`、`symbols`、`intent`、`code_facts`、`status`、`revisions`。同一行为跨已提交/工作树时拆成 C/W 并用 `related` 关联。
- Produces: hunk ID 为来源、新旧路径、起止行和 hunk 内容的 SHA-256；删除/重命名也有独立记录。flows ID 和测试 manifest key 后续不得重编号复用。

- [ ] **Step 1: 冻结不可变被审计输入**

运行 `git rev-parse HEAD`、`git branch --show-current`、`git status --porcelain=v1`、`git diff --stat`、`git diff --name-only`，记录命令时间；对产品路径、测试和声明范围内的 untracked 文件计算 SHA-256，同时记录生产目标文件版本。不得读取敏感配置。

Expected: `baseline.json` 区分原始输入、证据输出和后续 revision；若执行前树已变化，以新事实重建账本，不沿用规划快照数字。

- [ ] **Step 2: 核查日期边界、根快照和真实入口**

逐提交检查 `f1aa5bc..68d8506`，确认没有漏掉非日志行为；列出根快照与 `upstream/XX-Net-5.16.7` 的产品差异并标记不可还原意图。范围扫描必须包含 `Coding/XX-Net-5.16.7.1/start`、installer、启动包装和其他真实入口。

Expected: `baseline.json` 明确 2026-10-27 当天已做边界核查、主基线、生产哈希来源、根快照限制和所有入口的纳入/排除证据。

- [ ] **Step 3: 建立 hunk 级逻辑变化总账**

枚举 `68d8506..5dc3f12` 每个提交触过的路径与功能 hunk，包括后续反转的净零变化；加入工作树 tracked/untracked 差异、删除、重命名和根快照补充差异。混合提交和混合文件拆分，纯日志内部项记为 EXC，外部运行时影响不得随日志内部项排除。

Expected: 每个分母 hunk/删除/重命名恰好映射一个 CHG-C、CHG-W 或 EXC；每项变化有真实源码或历史锚点，文档、测试和证据使用独立分母。

- [ ] **Step 4: 固定核心流程和测试 manifest**

将规范第 5.2 节的八组核心流程逐项写入 `flows.json`，每项预留入口、锁顺序、condition 谓词/唤醒者、持锁回调、重入、资源终点及正常/异常/停止/代际矩阵。盘点 `tests/test_*.py`、离线独立 acceptance/probe 和本轮新增测试位置，逐项记录命令、加载代码根、源码哈希、超时、用例/场景计数方式、允许的既有 skip；生产黑盒与合成性能工具单列。

Expected: 每个测试文件恰有一个 manifest 分类；离线集合无生产黑盒，所有后续新增测试必须登记，漏项可由集合比较检出。

- [ ] **Step 5: 实现并验证覆盖校验器和套件运行器**

`validate_coverage.py` 使用标准库 JSON 校验 ID、路径、hunk 双向映射、变化 revision、flows 状态、manifest 集合、终态阻断和快照一致性；`run_suite.py` 按 manifest 逐项用 `python3 -I` 独立进程运行，保存输出、退出码、超时、计数、skip 和重试，不因单项失败提前停止。用临时合成夹具验证未映射 hunk、NO TESTS RAN、未捕获后台异常和漏跑均会失败。

Run: `python3 -I .superpowers/sdd/2026-11-08-design-intent-change-audit/validate_coverage.py`

Expected: 工具自测的负例均被拒绝；正式入账后输出零未映射文件/hunk/变化/流程/测试，重复 ID 为零。

### Task 2: front 与独立 Client 的契约、损益和确认缺陷

**Files:**
- Create: `.superpowers/sdd/2026-11-08-design-intent-change-audit/contracts/front.md`
- Modify only after RED: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/front_base/http_common.py`
- Modify only after RED: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/front_base/http_dispatcher.py`
- Modify only after RED: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/front_base/http1.py`
- Modify only after RED: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/front_base/http2_connection.py`
- Modify only after RED: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/front_base/http2_stream.py`
- Modify only after RED: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/simple_http_client.py`
- Test: `tests/test_front_body_deadline.py`, `tests/test_front_dispatcher_wait_isolation.py` and task-specific regression files

**Interfaces:**
- Consumes: Task 1 的变化、hunk、flows 和测试 manifest。
- Produces: Task 的 attempt/retry/finish 契约、worker/stream 所有权、H1/H2 协议不变量、Client socket 所有权；每个关联变化 ID 的损益和风险处置。

- [ ] **Step 1: 还原执行流和公共契约**

用 CodeGraph 和真实源码记录 `request → dispatcher → worker → response/finish/retry`、H2 send/recv、connect callback 和 Client 调用方；明确共享绝对期限、attempt 身份、关闭责任和停止边界，并更新 flows 的锁/等待/重入矩阵。

Expected: `contracts/front.md` 能回答谁提交、谁关闭、谁接回及旧 attempt 为何不能生效，核心流程没有“尚未知”却被标为关闭。

- [ ] **Step 2: 建立双边损益卡片并预注册验收协议**

逐项核对流控锁、发送资格、请求期限、keep-alive、Client 完整读写等变化。每个性能/资源项在测试前写入真实工作负载、环境、基线/修改前/最终指纹位置、指标、oracle、重复次数、噪声预算、无效规则、可接受损失和资源边界。

Expected: 所有关联变化均有处置；高风险项有正常、异常和交错命题。没有真实产品测量的项目标“收益未证实”，合成性能工具不作提升证据。

- [ ] **Step 3: 对高风险项执行准确 RED**

复用受控时钟、事件门和真实 socket；至少覆盖合法 H2 对端、正常 retry/keep-alive、旧 attempt 晚到、停止/替换、部分写入与超时。新测试先在未修复实现上因目标行为失败，并登记到 manifest。

Expected: 每个候选记录为 R、被证伪、仍属 S 或转 C/D；夹具错误不计 RED，S 级高影响项不得 deferred 或关闭。

- [ ] **Step 4: 最小修复确认的 Critical/Important 并回归**

只修改根因所在边界，保持公共接口；运行目标异常、正常路径、相邻异常及关联文件：

Run: `python3 -I tests/test_front_body_deadline.py` 和 `python3 -I tests/test_front_dispatcher_wait_isolation.py`

Expected: 新回归 GREEN，既有测试无失败；RED/GREEN、revision 和未覆盖项写入台账。无确认缺陷时记录“不修改”。

### Task 3: Smart Router、DNS 与 PipeSocks 的契约、损益和确认缺陷

**Files:**
- Create: `.superpowers/sdd/2026-11-08-design-intent-change-audit/contracts/smart-router.md`
- Modify only after RED: `Coding/XX-Net-5.16.7.1/code/default/smart_router/local/`
- Test: `tests/test_dns_query_history_isolation.py`, `tests/test_dns_transport.py`, `tests/test_dns_response_ownership.py`, `tests/test_pipe_socks_condition_race.py`, `tests/test_smart_router_handoff.py`, `tests/test_smart_router_connect_bounds.py`

**Interfaces:**
- Consumes: Task 1 分母；Task 2 的共享 Client/Task 契约。
- Produces: DNS 全调用期限与别名历史、cache 代际、Direct/X-Tunnel/PipeSocks 交接、selector 与连接池所有权；关联变化的损益和风险处置。

- [ ] **Step 1: 还原查询和连接交接执行流**

记录 UDP/TCP/DoH/DoT/XX-Net 平行查询、handler 线程、共享队列、CNAME 历史、NS 退避、Direct 竞速和 PipeSocks selector；区分当前启用与支持但未启用路径，并更新锁/等待/回调矩阵。

Expected: 每个 socket、waiter、缓存项和 handler 都有明确所有者及停止归宿。

- [ ] **Step 2: 筛查变化损益并预注册资源边界**

重点检查历史隔离、缓存 epoch、建连上限、背压、监听重启和异常收尾是否损伤合法 CNAME、正常直连、慢上游或并发查询；性能/资源项在测试前按 Task 2 Step 2 的协议字段登记。既有 DNS 全调用期限和 NS single-flight 不擅改。

Expected: 每个关联变化都有收益、受损路径、证据状态、双向 oracle 和处置。

- [ ] **Step 3: 确定性验证高风险项**

使用 GatedCondition、故障 selector、PartialSendSocket、受控时钟和假 DNS 响应；正例与异常例成对，测试必须证明真实资源关闭/保留和恰好一次交付。

Expected: 准确 RED 或明确证伪；不把故障注入产生的预期日志当成新缺陷，S 级高影响项保持阻断。

- [ ] **Step 4: 最小修复并运行关联套件**

逐个运行上述六个测试文件，命令均为 `python3 -I tests/<file>`；新增/修改测试先 RED 后 GREEN 并更新 manifest。

Expected: 六文件及新增回归返回 0，正常 CNAME、合法多地址监听和正常 Direct/GAE/X-Tunnel 回退不退化。

### Task 4: X-Tunnel 会话、连接与背压的契约、损益和确认缺陷

**Files:**
- Create: `.superpowers/sdd/2026-11-08-design-intent-change-audit/contracts/x-tunnel.md`
- Modify only after RED: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/`
- Test: `tests/test_x_tunnel_memory_backpressure.py`, `tests/test_front_dispatcher_wait_isolation.py`, `tests/test_proxy_protocol_eof.py` and task-specific regression files

**Interfaces:**
- Consumes: Task 1 分母；front 与 Smart Router 的公共交接契约。
- Produces: Session/Conn 代际、首包所有权、roundtrip/login/reporter 提交资格、SendBuffer/ReceivePool 和背压恢复语义；关联变化的风险处置。

- [ ] **Step 1: 还原 Session/Conn 生命周期**

记录 `create_conn/start_conn/send/stop/remove`、worker、Pipe、timeout_checker、reporter、login/API 回调和 front 选择；明确锁顺序、condition 谓词/唤醒者、持锁外部回调、重入及锁外关闭要求。

Expected: 旧 Session/Conn/attempt 不能提交新代状态的机制有源码证据；首包和 socket 所有权无空档。

- [ ] **Step 2: 评估代际防护和资源上限的损益**

核对新增身份参数对所有调用方的兼容性；背压、容量和期限项在测试前预注册真实负载、指标、噪声、重复、最大损失和资源边界。同 transfer attempt 身份、quota 回滚和 body_queue 背压继续列为 C/D，不自行改变。

Expected: 优化收益与最大容忍损失同时入账；慢连接、大流量、正常乱序和停止恢复均有正例或明确未覆盖状态。

- [ ] **Step 3: 验证高风险交错**

用事件门固定 start/stop/reset、晚到 login/API、旧 worker、首包部分读取、背压暂停与恢复；正常顺序连接和合法乱序必须作为正例。

Expected: 每项重要候选取得准确 RED、被证伪或保留为 S/C/D；无仅凭静态推断的修复。

- [ ] **Step 4: 最小修复并运行关联套件**

Run: `python3 -I tests/test_x_tunnel_memory_backpressure.py`、`python3 -I tests/test_front_dispatcher_wait_isolation.py`、`python3 -I tests/test_proxy_protocol_eof.py`

Expected: 新增回归和既有套件返回 0；台账记录旧代无副作用、正常连接不退化和背压可恢复的证据。

### Task 5: 顶层入口、launcher、共享 HTTP、配置与未启用模块

**Files:**
- Create: `.superpowers/sdd/2026-11-08-design-intent-change-audit/contracts/lifecycle.md`
- Modify only after RED: `Coding/XX-Net-5.16.7.1/start`
- Modify only after RED: `Coding/XX-Net-5.16.7.1/code/default/launcher/`
- Modify only after RED: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/simple_http_server.py`
- Modify only after RED: `Coding/XX-Net-5.16.7.1/code/default/lib/noarch/xconfig.py`
- Modify only after RED: 经变化账本确认的其他安装、包装或支持模块文件
- Test: `tests/test_module_lifecycle.py`, `tests/test_config_concurrency.py`, `tests/test_ip_manager_lifecycle.py`, `tests/test_simple_http_server_keepalive.py`, `tests/test_tls_relay_adjust.py`, `tests/test_iptables_idempotent.py`

**Interfaces:**
- Consumes: 前四项任务的变化 ID、公共接口和所有权模型。
- Produces: 顶层安装/iptables、模块 start/stop/restart、控制面并发、半启动回滚、handler 接回、配置原子性和支持模块离线契约。

- [ ] **Step 1: 还原安装、启动、停止与控制面顺序**

记录顶层 `start`、installer/包装入口、launcher 后台 wrapper、web_control handler、模块 ready 发布、必要监听、缓存最终保存和 HTTPServer shutdown；覆盖 stop→restart 与并发停止。

Expected: iptables 创建/清理和退出语义有变化记录；启动参数原样传递、失败可回滚重试、已登记 handler 被接回、最终持久化在生产者停止之后。

- [ ] **Step 2: 审计共享接口与支持模块损益**

检查配置原子替换、keep-alive、IpManager 代际、TLS Relay 选线和 iptables 幂等；性能/资源项先登记验收协议。未启用模块做离线契约与调用兼容审计，iptables 仅使用临时 fake 命令。

Expected: 当前生产未启用不作为排除理由；参照树和第三方库不计入变化分母。

- [ ] **Step 3: 验证并最小修复确认缺陷**

对顶层退出语义、启动参数类型、半启动资源、快速重启、慢 handler、并发 save/load 和支持模块关键正例先 RED 后 GREEN；无缺陷则不修改。

Expected: 启动失败不发布 ready，正常启动/停止可重入，旧格式配置与正常 keep-alive 不退化，真实系统 iptables 未执行。

- [ ] **Step 4: 运行关联套件**

逐个运行上述六个测试文件，命令均为 `python3 -I tests/<file>`；新增测试更新 manifest。

Expected: 全部返回 0；结果和任何环境限制写入台账。

### Task 6: 跨层平衡点、设计裁定与覆盖闭合

**Files:**
- Modify: `.superpowers/sdd/2026-11-08-design-intent-change-audit/risks.md`
- Modify: `.superpowers/sdd/2026-11-08-design-intent-change-audit/balance.md`
- Create: `.superpowers/sdd/2026-11-08-design-intent-change-audit/coverage.json`
- Modify: `.superpowers/sdd/2026-11-08-design-intent-change-audit/changes.json`
- Modify: `.superpowers/sdd/2026-11-08-design-intent-change-audit/flows.json`

**Interfaces:**
- Consumes: 四组契约、变化总账、R/S/C 证据和修复结果。
- Produces: 每项变化的最终处置、跨层平衡点矩阵、C/D 清单、deferred 和未覆盖清单。

- [ ] **Step 1: 做跨层反例复审**

沿 launcher→模块→front/Client、Smart Router→X-Tunnel→front、DNS→共享 Client 的链路检查局部正确但组合错误的情况；特别复核公共参数、停止顺序、共享期限和锁/回调交错。

Expected: 新发现回到对应任务补 RED/GREEN；不能由局部 GREEN 直接关闭跨层风险。

- [ ] **Step 2: 完成平衡点矩阵并对最终代码复验**

按预注册协议比较基线、修复前和最终代码，记录统计量、离散度、无效运行、正常功能断言和资源上限；噪声超预算判 INCONCLUSIVE。修复撤销原收益时更新裁定，不沿用修复前数据。

Expected: 每个性能/资源项为 PASS、FAIL 或 INCONCLUSIVE；无有效真实产品测量时标“收益未证实”，每个容量/期限损失有边界或进入 C/D。

- [ ] **Step 3: 按终态矩阵关闭覆盖分母**

将变化标为已确认修复、有意保持、证伪、deferred、未覆盖或需设计裁定；S 级高影响、核心契约未知和依赖未覆盖证据的高风险项不得关闭。真实 H2、多阶段 Smart Router、Client 二次复用等缺口必须向下关联到受影响卡片。

Run: `python3 -I .superpowers/sdd/2026-11-08-design-intent-change-audit/validate_coverage.py`

Expected: 无未映射项、重复 ID、无证据完成状态或被错误关闭的高风险项；C/D 项单独列出，不擅自实施。

### Task 7: 独立 QA 与完整离线验收

**Files:**
- Create: `.superpowers/sdd/2026-11-08-design-intent-change-audit/final-review.md`
- Create: `.superpowers/sdd/2026-11-08-design-intent-change-audit/full-suite-final/`
- Create: `.superpowers/sdd/2026-11-08-design-intent-change-audit/race-repeat/`
- Modify: `.superpowers/sdd/2026-11-08-design-intent-change-audit/test-manifest.json`
- Modify: `.superpowers/sdd/2026-11-08-design-intent-change-audit/progress.md`

**Interfaces:**
- Consumes: 完整规范、计划、真实 diff、变化总账、契约、风险、flows 和测试证据。
- Produces: 独立审查结论、与最终产品快照绑定的完整验收结果和测试哈希清单。

- [ ] **Step 1: 独立 QA 审查**

交给未参与实现的新审查者，要求寻找未入账 hunk、契约误读、平衡点反例、测试漏项和证据外推；主线程逐项核实 Critical/Important，必要时返回对应任务做 RED→GREEN。

Expected: `final-review.md` 分列确认、证伪、deferred 和无法裁定项；没有未处理 Critical/Important 才继续。

- [ ] **Step 2: 冻结最终快照并按 manifest 运行完整套件**

QA 后重新计算所有产品和测试哈希；冻结 manifest 条目后使用运行器执行，每项独立进程、独立日志和预登记超时，失败累计而非提前停止：

Run: `python3 -I .superpowers/sdd/2026-11-08-design-intent-change-audit/run_suite.py --manifest .superpowers/sdd/2026-11-08-design-intent-change-audit/test-manifest.json --out .superpowers/sdd/2026-11-08-design-intent-change-audit/full-suite-final`

Expected: manifest 中每项实际执行且返回 0，用例/场景数、skip、超时和重试均有记录；无 FAILED、NO TESTS RAN、未捕获 `Exception in thread` 或未声明 skip。首次失败永久保留，生产黑盒不在此运行。

- [ ] **Step 3: 重复关键竞态并做静态检查**

所有新增/修改竞态文件及 front、DNS、PipeSocks、X-Tunnel、生命周期关键文件各至少 10 轮，逐轮写入运行记录；全部修改/新增 Python 文件运行 `py_compile`，运行 `git diff --check`。若此时产品文件再变，作废 Step 2 并返回重跑。

Expected: 每轮真实执行且返回 0；无后台异常或格式错误，最终测试哈希与产品快照一致。无效循环保留但标记不计入。

### Task 8: 条件式生产部署与运行验收

**Files:**
- Create: `.superpowers/sdd/2026-11-08-design-intent-change-audit/deployment/atomic_deploy.py`（仅在部署时）
- Create: `.superpowers/sdd/2026-11-08-design-intent-change-audit/deployment/production_blackbox_safe.py`（仅在部署时）
- Create: `.superpowers/sdd/2026-11-08-design-intent-change-audit/deployment/sanitize_logs.py`（仅在部署时）
- Create: `backups/pre-rsync-20261108-design-intent-change-audit/`（仅在部署时）
- Modify: `.superpowers/sdd/2026-11-08-design-intent-change-audit/progress.md`

**Interfaces:**
- Consumes: Task 7 的最终测试快照、部署授权和最终实际产品文件集合。
- Produces: 部署/不部署决定、哈希清单、进程代际、安全功能验收和必要回滚证据。

- [ ] **Step 1: 判定是否部署并核对三方哈希**

若本轮没有产品修复或执行授权不含部署，记录“不部署，未做部署后验收”并跳到任务 9。若部署，开发清单必须等于 Task 7 完整验收快照；部署瞬间重新读取生产哈希，与冻结时比较，任何未知漂移均中止。建立独立备份/证据目录和实际文件清单。

Expected: 备份与回滚清单覆盖全部会更改路径；开发待部署哈希等于最终已测试快照，生产无未解释漂移。

- [ ] **Step 2: 原子部署并验证真实进程**

使用经核对的专用脚本原子替换，正常停止旧进程，以监听 socket 反查真实 PID，启动新进程并确认旧进程退出、新代际及必要端口：

```bash
python3 -I .superpowers/sdd/2026-11-08-design-intent-change-audit/deployment/atomic_deploy.py prepare --backup backups/pre-rsync-20261108-design-intent-change-audit --evidence .superpowers/sdd/2026-11-08-design-intent-change-audit/deployment
python3 -I .superpowers/sdd/2026-11-08-design-intent-change-audit/deployment/atomic_deploy.py deploy --backup backups/pre-rsync-20261108-design-intent-change-audit --evidence .superpowers/sdd/2026-11-08-design-intent-change-audit/deployment
```

Expected: 运行文件全部匹配新清单；真实 PID 来源明确；若未就绪或出现本轮新异常，恢复原文件并验证恢复后的文件和真实进程代际。

- [ ] **Step 3: 使用安全黑盒验证控制接口和真实代理路径**

`production_blackbox_safe.py` 只读取安全状态接口的状态码和固定探针结果，禁止请求 `/config?cmd=get_config`、读取完整响应配置或输出 URL/Host。验证 8086 HTTP/HTTPS 和 1080 域名式 SOCKS5；`sanitize_logs.py` 在本地进程内读取部署窗口日志，只输出规范允许的脱敏派生计数，原始日志不进入工具输出或证据。

Expected: 控制接口和实际代理请求成功，脱敏日志无指向本轮修改的新异常；失败触发回滚。短时 204 只证明当次路径，不宣称永久治愈。

### Task 9: 最终报告与有界收敛

**Files:**
- Create: `docs/2026-11-08-design-intent-change-audit.md`
- Modify: `.superpowers/sdd/2026-11-08-design-intent-change-audit/coverage.json`
- Modify: `.superpowers/sdd/2026-11-08-design-intent-change-audit/progress.md`

**Interfaces:**
- Consumes: 所有任务的账本、审查、测试、部署或回滚证据。
- Produces: 对用户可读的覆盖结论、平衡点、修复清单、deferred、设计裁定和未覆盖边界。

- [ ] **Step 1: 最终一致性检查**

再次运行覆盖校验器，核对报告中的 hunk/文件/变化/流程/测试/部署数量均来自原始证据；保留失败运行、无效证据和未覆盖项，检查局部卡片状态与总报告一致。

Run: `python3 -I .superpowers/sdd/2026-11-08-design-intent-change-audit/validate_coverage.py` 和 `git diff --check`

Expected: 校验返回 0，报告无无证据的“全部”“永久”“完全无堆栈”等外推表述。

- [ ] **Step 2: 发布最终结论**

报告分别列出已确认并修复、已证伪、有意权衡、Minor deferred、C/D 设计事项、生产验证和未覆盖范围；不自行 commit、push、合并或创建 PR。

Expected: 只有规范第 12 节全部满足才写“已覆盖模型内没有未处理高优先级项”；否则明确剩余阻断项并返回相应任务。
