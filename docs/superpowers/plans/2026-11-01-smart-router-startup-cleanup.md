# Smart Router 启动失败清理与测试隔离 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 代理启动失败时完整回收已启动资源，并消除启动竞态测试的 `sys.modules` 污染。

**Architecture:** 只在 `start()` 的代理 Server 构造/启动边界增加异常清理，按启动逆序停止既有资源；清理异常记录但不替换原始启动异常。测试脚本用模块表快照恢复全局状态，新增独立回归证明隔离有效。

**Tech Stack:** Python 3、现有独立测试脚本、unittest/pytest 风格断言。

**Spec:** `docs/superpowers/specs/2026-11-01-smart-router-startup-cleanup-design.md`

## Global Constraints

- 正常启动顺序和正常 stop 语义不变。
- 不部署、不重启、不 push。
- 新生产行为与测试隔离均先 RED 后 GREEN。
- 提交信息末尾使用 `Co-Authored-By: Kimi <noreply@moonshot.cn>`。

## Review Focus

- 代理 Server 构造失败与 `start()` 失败都应进入同一清理边界；测试覆盖后者，并检查构造边界代码。
- 某个 stop 抛异常不能阻止后续资源清理，也不能遮蔽原始启动异常；Task 1 覆盖。
- 清理顺序应为 `dns_srv → dns_query → pipe_socks → connect_manager`；Task 1 断言完整顺序。
- 测试恢复必须保留原有模块对象身份并移除新模块；Task 2 覆盖。
- 正常 S1/S2 启动不得因清理边界改变；Task 1 回归覆盖。

---

### Task 1: 代理启动失败资源清理

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/smart_router/local/__init__.py`
- Test: `tests/test_smart_router_startup_race.py`

**Interfaces:**
- Consumes: 既有 `g.dns_srv.stop()`、`g.dns_query.stop()`、`g.pipe_socks.stop()`、`g.connect_manager.stop()`。
- Produces: 代理 Server 构造/启动异常时，四项资源各尝试停止一次，原异常继续抛出。

- [ ] **Step 1: 写 S3/S4 失败测试**

S3 令 `HTTPServer.start()` 抛 `RuntimeError("proxy bind failed")`，断言旧实现未调用任何 stop；修复后断言完整逆序清理、原异常信息一致且 `ready=False`。S4 令首个 `dns_srv.stop()` 抛异常，断言其余三项仍清理且抛出的仍是原始代理异常。

- [ ] **Step 2: 运行并确认目标 RED**

Run: `python3 tests/test_smart_router_startup_race.py`
Expected: S1/S2 通过，S3 因资源未清理失败。

- [ ] **Step 3: 实现最小清理边界**

仅包裹代理 Server 构造与 `start()`；`except Exception` 中依次尝试四项 stop，清理异常写 `xlog.exception()`，最后裸 `raise`。不增加后台线程或新资源抽象。

- [ ] **Step 4: 运行并确认 GREEN**

Run: `python3 tests/test_smart_router_startup_race.py`
Expected: S1/S2/S3/S4 全部通过，输出 `RESULT: ALL PASS`。

- [ ] **Step 5: 提交 Task 1**

提交生产文件、测试和本计划/规格，信息为 `fix: N 阶段清理代理启动失败资源`。

### Task 2: 恢复测试模块表

**Files:**
- Create: `tests/test_smart_router_startup_test_isolation.py`
- Modify: `tests/test_smart_router_startup_race.py`

**Interfaces:**
- Consumes: Task 1 后 S1–S4 全部通过的启动竞态脚本。
- Produces: 脚本成功执行后，`sys.modules` 与执行前一致。

- [ ] **Step 1: 写隔离回归**

用 `runpy.run_path()` 在同一进程执行启动竞态脚本；预置两个 sentinel 模块并记录 `smart_router.local` 不存在。旧脚本执行后断言 sentinel 被替换且 `smart_router.local` 残留，确认目标 RED；测试自身最后恢复模块表。

- [ ] **Step 2: 运行并确认目标 RED**

Run: `python3 tests/test_smart_router_startup_test_isolation.py`
Expected: 因 sentinel 未恢复或 `smart_router.local` 残留而失败。

- [ ] **Step 3: 快照并恢复模块表**

在首次替换模块前保存 `saved_modules = sys.modules.copy()`；在现有 `finally` 中先恢复 `sys.modules`，再删除临时目录。

- [ ] **Step 4: 运行并确认 GREEN**

Run: `python3 tests/test_smart_router_startup_test_isolation.py && python3 tests/test_smart_router_startup_race.py`
Expected: 隔离回归和 S1–S4 全部通过。

- [ ] **Step 5: 完整验证并提交**

运行规格中列出的全部回归、六个近期生产文件及本次文件的 py_compile、fd 探针和 `git diff --check`；独立审查后提交 `test: N 阶段隔离启动竞态测试模块`。
