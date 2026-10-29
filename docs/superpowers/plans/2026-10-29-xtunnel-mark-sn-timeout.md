# x_tunnel mark_sn_timeout 日志聚合实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保留每个下载 SN 的首次超时信号，同时收敛同一 SN 在 60 秒内反复出现的 `mark_sn_timeout renew` WARN。

**Architecture:** 在 `BlockReceivePool` 生命周期内持有既有 `xlog.LogThrottle(60)`，只包裹两条日志输出。`reset()` 同时重建节流账本，与 `timeout_sn_list` 的 SN 生命周期一致；所有状态更新仍在原锁和原分支中执行。

**Tech Stack:** Python 3、既有 `xlog.LogThrottle`、离线 stub 测试。

**Spec:** 用户 2026-10-29 指令“暂不部署，记录后续验证；继续其他优先级任务规划并执行”，以及本计划 Global Constraints。生产证据见 `docs/XX-Net-macOS-ARM64-修复记录.md` 的 I 阶段后续验证章节和本次复评：H 运行窗口中 `mark_sn_timeout` 首报 363 条、renew 379 条。

## Global Constraints

- 只改日志，不改 `timeout_sn_list` 的写入/更新条件、`next_sn`、`block_list`、重试、Queue、传输或返回路径；reset 复用既有 `self.lock` 保证 SN 状态与日志账本的原子生命周期，不新增锁。
- 不改 `get_timeout_list()`，不改 I 阶段三个生产文件，不做 `req get response timeout`、微旱态、完整 episode 或公共容量改造。
- 每个不同 SN 的首次 WARN 保留；同 SN 的首次/renew 共用一个 60 秒窗口，下一窗放行附 `(suppressed N)`。
- `reset()` 后 SN 生命周期重新开始，同号 SN 必须重新拥有首报；不引入 session/generation 框架。
- 不部署、不重启、不 push；代码、测试、文档、完整回归和独立审查完成后提交。

## Review Focus

- 同 SN 的日志被抑制时，`server_send_time` 仍必须更新到较新的 t：M1 精确断言。
- 不同 SN 不得共享窗口：M2 断言各自首报。
- reset 后 SN 复用不得继承旧窗口：M4 断言同号 SN 首报且池状态重置。
- 并发事件不得破坏节流计数或状态更新：M5 断言 20 线程一条 WARN、最终保存最大 t。
- 不同接收池不得共享账本：M6 断言两个真实实例对同号 SN 分别首报。
- 静默重复/旧 t 不得消费窗口或虚增 suppressed：M7 断言跨窗 renew 精确结算。
- reset 与 mark 交错不得让新生命周期事件落入旧账本：M8 以可控构造阻塞断言首报保留。

---

### Task 1: BlockReceivePool 按 SN 聚合超时日志

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/base_container.py:11-14,286-360`
- Create: `tests/test_mark_sn_timeout_throttle.py`
- Modify: `docs/XX-Net-macOS-ARM64-修复记录.md`
- Modify: `Coding/XX-Net-5.16.7.1/LOCAL-VERSION.md`

**Interfaces:**
- Consumes: `xlog.LogThrottle.allow(key) -> (allowed: bool, suppressed_count: int)`；既有 `BlockReceivePool.mark_sn_timeout(sn, t, server_time)`。
- Produces: `BlockReceivePool.timeout_log_throttle = LogThrottle(60)`；`mark_sn_timeout` 签名、返回值和状态语义不变。

- [ ] **Step 1: 编写失败测试**

新测试按相对路径加载真实 `LogThrottle` 和真实 `BlockReceivePool`，仅 stub 外部依赖；用可控时钟避免真实等待。M1：同 SN 首报+renew 仅一条 WARN，且 `server_send_time` 更新到较新的 t。M2：不同 SN 各自首报。M3：下一窗保留原字段并结算 `(suppressed N)`。M4：reset 后同号 SN 重新首报，池状态按原逻辑清空。M5：20 线程同 SN 仅一条 WARN，最终保存最大 t。M6：两个真实池实例对同号 SN 分别首报。

- [ ] **Step 2: 运行测试确认目标 RED**

Run: `python3 tests/test_mark_sn_timeout_throttle.py`
Expected: M1/M3/M5 因旧实现逐条 WARN、没有 suppressed 结算而失败；测试不得因加载或夹具错误失败。

- [ ] **Step 3: 最小实现**

在 `base_container.py` 引入 `LogThrottle`，于 `reset()` 创建 `self.timeout_log_throttle = LogThrottle(60)`，并在既有 `self.lock` 内与 SN 状态一起原子替换。仅在原本会输出日志的首报和较新 t 的 renew 分支调用一次 `allow(sn)`；allowed 时保留原格式，suppressed>0 时附后缀。相同/较旧 t 的静默事件不进入账本，两个状态写分支的条件和语句保持原样。

- [ ] **Step 4: 验证 GREEN 与编译**

Run: `python3 tests/test_mark_sn_timeout_throttle.py && python3 -m py_compile Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/base_container.py`
Expected: M1–M6 全 PASS，编译退出码 0。

- [ ] **Step 5: 完整回归、独立审查和文档**

运行 I 阶段全部九项测试、fd 探针及 `git diff --check`。独立审查重点核对：reset 生命周期、锁顺序、被抑制事件仍更新状态、无跨 SN/跨实例误抑制。Critical/Important 必须按 RED→GREEN 修复并复跑完整套件；Minor 记录延后。文档记录复评数据、RED/GREEN、审查结论和未部署边界。

- [ ] **Step 6: 提交**

明确暂存本任务生产文件、新测试、计划、修复记录和版本说明；I 阶段后续验证清单一并提交。提交信息末尾添加既定 Claude attribution；不 push、不部署。
