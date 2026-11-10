# 故障机理驱动的网络可靠性复审

日期：2026-11-08

## 范围与方法

本轮依据 `docs/superpowers/specs/2026-11-08-mechanism-first-reaudit-design.md`，先归纳历史故障机理，再按执行流、共享状态、跨层顺序、平行协议和资源所有权检查当前工作树。不是按文件平均扫描，也不以既有测试通过代替交错审计。

基线 HEAD 为 `5dc3f12`，分支为 `fix/h-log-dns-backoff`，保留此前未提交的网络可靠性修复。当前 33 个测试文件基线全部通过，共 262 项 unittest 和 11 个独立脚本；日志保存在 `.superpowers/sdd/2026-11-08-mechanism-first-reaudit/baseline-suite/`。

## 执行流审计分组

1. front/独立 Client：dispatcher、H1/H2、Task/Stream、retry/finish、共享连接和绝对期限。
2. Smart Router/DNS：请求回退、socket 交接、PipeSocks、DNS 平行来源、waiter、缓存和退避。
3. X-Tunnel：handler、Session、Conn/Pipe、roundtrip、checker、login、缓冲区和代际。
4. 跨模块生命周期：launcher、模块 start/stop、全局对象替换、后台线程和配置重载。

各组独立审查只产生候选。主线程将逐项核对真实实现，重要候选使用事件门或故障替身复现；R/S/C 等级严格区分。

## 检查结果

### 基线可重复性

八个 DNS/front/Smart Router/X-Tunnel/PipeSocks 关键测试文件连续 10 轮全部通过。该结果说明基线测试稳定，但不覆盖下文新增的异常与生命周期交错。

### Smart Router / DNS / PipeSocks

主线程已核对独立分组的十条源码路径，确认当前实现存在相应行为；正式严重度与 R 级结论仍以主线程后续 RED 测试为准：

1. PipeSocks 的非 ValueError selector 异常可留下 `running=True` 且无消费者；首次注册失败仍被调用方当成已交接。
2. 缓存 epoch 不绑定 DomainRecords 实例，旧 worker 可在模块换实例后写入新缓存。
3. TCP/DoH 跨响应 CNAME 环缺少调用级历史与跳数上限，递归层累积池连接。
4. 监听自动重启可让旧 serve 循环与新线程并存；多地址部分启动失败未完整回收 listener。
5. DomainRecords 持久化记录的 key/schema 与运行期不一致；DomainRecords/IpRecord 的 save 可在锁外抹掉并发脏位。
6. Direct 首包或发布异常时未关闭仍由本函数持有的 remote；TCP/DoH 过期池项直接丢弃而未关闭。

另有一项 C 级契约：全部 NS 冷却时是否应全局 single-flight 恢复探测。该行为可能影响上游压力，但现有设计也可能有意允许每个调用方 fail-open，本轮不擅自改成全局阻塞。

### front / 独立 Client

独立分组报告见 `.superpowers/sdd/2026-11-08-mechanism-first-reaudit/front.md`。主线程已核对其关键源码路径：

1. H2 把 HEADERS 错计入 DATA 连接窗口，连接窗口的检查、扣减、阻塞队列和迟到帧恢复缺少共同锁。
2. H2 初始正文发送与 WINDOW_UPDATE 可并发推进同一请求体游标；HPACK 编码与 HEADERS 入队也可倒置，破坏编码顺序和线上顺序的一致性。
3. H2 连接发送窗口初值不是协议默认 65535；SETTINGS 对既有 stream 的 max frame 使用累加，合法负 stream 窗口仍进入发送循环。
4. H1 在 close/retry 和新 attempt 接受后，旧发送执行流仍缺少发送资格复核；dispatcher 等待 worker 时不携带当前 Task 期限。
5. worker 创建与 stop 之间可在清理后重新登记；独立 Client 的大请求头使用单次 send，Response 析构与 Client 缓存对 socket 的关闭责任冲突，粘滞 EOF 可使头部读取忙循环。

这些候选证明的是可控输入和事件交错下的本地代码行为，不证明生产某次重试或连接重置由它们造成。

### X-Tunnel / 模块生命周期

X-Tunnel 与生命周期分组分别覆盖 Session/handler/worker 和 launcher/front/module 的跨代边界。主线程已确认以下路径在当前实现中成立：

1. SOCKS5 端口通过私有缓冲 reader 读取，而后续首包只移交自建 `read_buffer`，提前应用字节可滞留并丢失。
2. 旧上传 worker 在最终代际拒绝前可扫描并修改新代接收池的下载重试时间；旧登录/API 响应还可在启动资格复核前修改当前全局 server、quota 和 front 配置。
3. Session 在 worker/Pipe 启动前发布 `running=True`，中途线程启动失败没有回滚；发送前的打包/加密异常位于 on_road 递减 finally 之外。
4. launcher 后台初始化异常不传回等待方，可能永久等待并留下注册残留；GAE、X-Tunnel 和 Smart Router DNS 存在必要监听失败与 ready 发布分离的路径。
5. front 停止与按 host 懒创建 dispatcher 缺少共同停止边界，停止后请求仍可排队；X-Tunnel 统计、reporter、上传和 quota 线程依赖共享 `g.running`，快速重启可跨代存活或提前退出。
6. 配置文件的并发 save/load 没有同步和原子替换；部分初始化、连接池停止和 cache 保存顺序仍有资源/状态收尾缺口。

完整候选和静态边界见 `lifecycle.md`。其中 NS 全局 single-flight、同 transfer 多 attempt 的协议身份、强制退出是否刷日志等继续保留为 C 级契约，不直接按缺陷修改。

### 资源耗尽与运行期时钟

资源与时钟专项报告见 `resource-time.md`。主线程已读取报告并核对其与既有候选的去重关系；独立探针仍须在正式 RED 测试中由主线程复现：

1. 无 worker 时的队头阻塞会使已超时 Task 继续滞留，属于 FR-06 的资源放大维度；H2 已关闭 stream 的阻塞 DATA 仍留在连接级队列。
2. 建连超时后的迟到 SSL socket 缺少显式接收者；ConnectManager 停止不清池是 LC-12 的动态补强。
3. DNS 每 UDP 包创建一个无上限 handler 线程，并行成功连接可全部留池且组合停止不清池；UDP 查询的本地等待使用墙钟，回拨可放大剩余期限。
4. X-Tunnel Conn 的永久命令序号缺口可滞留后续 payload，stop 清空后仍有迟到入队窗口；小缓冲 flush 和部分重传/回收期限使用墙钟。

这些结果证明可控条件下的资源与期限行为，不证明生产曾发生相同触发，也不把依赖 GC 的最终回收夸大成永久泄漏。Task.body_queue 是否增加响应字节背压保留为 C 级契约。

## 修复与离线验收

Task 1—9 均按先准确 RED、再最小 GREEN 的顺序完成。修复覆盖 H2/H1 发送与流控、Client socket 所有权、PipeSocks/HTTP 监听交接、DNS 缓存实例与递归历史、配置原子替换、X-Tunnel 首包和 session 代际、模块启动回滚、front/连接池停止边界及 IpManager 扫描代际。每项 RED/GREEN 和夹具裁定见 `.superpowers/sdd/2026-11-08-mechanism-first-reaudit/progress.md`。

独立复核返回 0 Critical、7 Important、1 Minor。主线程逐项核对后，七项 Important 均先取得准确 RED 再最小修复：Conn 事件注册失败的普通锁自死锁、H2 SETTINGS 与新 stream 的状态交错、IpManager 旧代阻塞结果提交、UDP CNAME 缺少共享历史、SSL 成功/超时入队边界、PipeSocks 缓冲后的 WRITE 注册失败，以及 HTTPServer shutdown 未接回 handler。七项关联套件和每项十轮竞态重复均通过；唯一 Minor 为 ConnectPool 单个 socket close 异常可能跳过后续清理，本轮保留 deferred，不顺手扩大修复范围。

首次最终离线套件为 36/36 个文件、352 项 unittest 和 11 个独立脚本，七项修复所在文件 70/70 轮重复通过。首次生产部署随后暴露 launcher 后台 wrapper 把选项字典包成列表的集成缺陷，并暴露 Smart Router 半启动停止的次级异常；两项均按生产脱敏堆栈建立准确 RED 后修复。修复后的完整套件为 36/36 个文件、353 项 unittest 和 11 个独立脚本，启动生命周期、配置、Smart Router 启动和独立模型再重复 40/40 轮通过；额外 `acceptance_independent_models.py` 为 30/30。全部修改及新增 Python 文件通过 `py_compile`，完整 `git diff --check` 返回 0。故障注入日志仍有 4 个文件合计 10 处已捕获 Traceback，但没有 FAILED、NO TESTS RAN 或未捕获 `Exception in thread`，不能表述为“完全无堆栈”。

## 生产部署、回滚与重新验收

第一次部署的 29 个文件原子替换成功，旧 PID 35330 正常退出，但新 PID 72650 只有 8085，1080/8086 未在 60 秒内监听，已按预案回滚。失败窗口脱敏堆栈定位到 `_local.start([xargs])` 的参数类型错误；29 个原文件恢复并核对哈希后，回滚进程的三端口、控制接口、黑盒 10/10 和三种真实代理入口均恢复。

启动契约修复并完成全量回归后，第二次部署使用新的独立备份和证据目录，没有覆盖第一次失败证据。回滚进程正常退出，新 PID 75167 监听 1080/8085/8086，29 个运行文件全部匹配新部署清单。生产黑盒最终 10/10，8086 HTTP/HTTPS 和 1080 域名式 SOCKS5 请求均返回 204；完整部署窗口的 14 个脱敏日志无 Traceback、后台线程异常或启动回滚错误。详细证据见 `.superpowers/sdd/2026-11-08-mechanism-first-reaudit/deployment/README.md`。

## 收敛结论与边界

在当前已覆盖的故障模型、离线交错和生产验收路径内，没有未处理的 Critical/Important 项。该结论不等于系统不存在未知问题；真实 H2 peer、多阶段 Smart Router 故障注入和 Client 第二次真实复用仍未完整覆盖。NS 全局 single-flight、同 transfer 多 attempt 协议身份、quota 回滚等 C 级契约未擅自改变。短时 204 和重复测试成功只证明当次路径与已覆盖模型，不证明间歇故障永久治愈，也不反填历史生产根因。
