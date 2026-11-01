# Smart Router 启动失败清理与测试隔离设计

## 目标

按用户批准的顺序，在生产观察之后完成两项小修改：

1. 当 Smart Router 的代理 HTTP Server 构造或启动失败时，回收此前已经启动的 DNS、UDP 中继、PipeSocks 与 ConnectManager 资源，并保留原始启动异常。
2. 让 `test_smart_router_startup_race.py` 在成功或失败退出时都恢复 `sys.modules`，避免替换模块泄漏到同一 Python 进程中的其他测试。

## 边界

- 正常启动顺序继续保持 K 阶段的 `g.local_ips → g.dns_query → g.dns_srv → g.proxy_server`。
- 不修改 DNS 查询、代理转发、端口配置、正常 stop 流程或传输逻辑。
- 不处理 DnsServer 自身 `start()` 的部分初始化失败；本轮只覆盖代理 HTTP Server 构造/启动失败。
- 不修复本轮生产观察发现的既有 `SocketWrap.close()` EBADF；它是独立低频问题，另行评估。
- 不新增独立事件统计，不制作交付包；等待本轮修改和未来部署后的稳定证据。
- 不部署、不重启、不 push。

## 验证

- 代理启动失败测试必须先在旧实现上证明资源未清理，再验证修复后按逆序清理且原异常类型和信息不变。
- 至少一项清理操作自身抛异常时，其余资源仍须清理，原始代理启动异常继续向调用方抛出。
- 测试隔离回归先在旧脚本上证明 `sys.modules` 被污染，再验证修复后原有模块对象与新插入模块都恢复。
- 运行 K、DNS、xlog、dispatcher、x_tunnel、SN 和五套既有回归，执行 py_compile 与 git diff 检查。
