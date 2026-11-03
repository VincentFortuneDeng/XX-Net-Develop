# 上游 iptables 与 TLS Relay 修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将官方两项已确认修复语义迁移到本地版本，同时保留现有 macOS、IPv6 和日志补丁。

**Architecture:** 两个任务完全独立。Shell 修复只迁移 `start` 的 iptables 函数和退出码，并用临时 fake 命令做行为测试；TLS Relay 手工合并服务端 `adjust`、综合评分和 worker score 阈值，保留本地 IPv6 过滤。

**Tech Stack:** Bash、Python 3 标准库 `unittest`、现有 Smart Router/X-Tunnel front_base。

**Spec:** `docs/2026-11-03-upstream-issue-pr-review.md`；上游提交 `054daa66`、`8613cd7b`。

## Global Constraints

- 中文说明和注释，代码标识符保持英文。
- 每项必须先观察目标测试失败，再做最小实现，独立提交。
- 不处理 `encrypt.py`、Flask、HTTP 长连接或 front 锁，不部署、不重启、不 push。
- 不执行真实 `iptables`，不读取敏感配置，不删除既有备份和日志。
- TLS Relay 合并必须保留 `_ipv6_enabled()`、`skipped_ipv6` 和限流日志行为。

## Review Focus

- 重复 set、历史重复 jump、多接口和无规则 unset 均应收敛且不操作真实防火墙。
- `adjust` 缺失、负值、非数字、极大值以及 speed=0 不得让选线崩溃或选择被禁用线路。
- 本地 IPv6 不可用时仍须跳过 IPv6 front，并保留原有日志和链接计数。

---

### Task 1: iptables 幂等设置与清理

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/start`
- Test: `tests/test_iptables_idempotent.py`

**Interfaces:**
- Consumes: 现有 `set_iptables [interface]`、`unset_iptables [interface]` 命令。
- Produces: 重复执行幂等；unset 清除本接口全部历史重复 jump，仅在其他接口不再引用时删除 REDSOCKS，成功返回 0。

- [x] **Step 1: 编写 fake-iptables 行为失败测试**

测试从 `start` 抽取两个 shell 函数，在临时目录中定义有状态的 fake `iptables`，不运行脚本其他部分。覆盖首次/重复 set、历史重复 jump、多接口保留 chain、无规则 unset 和退出码。

- [x] **Step 2: 运行测试确认 RED**

Run: `python3 -I tests/test_iptables_idempotent.py`
Expected: 重复 set 或重复 jump 清理用例失败。

- [x] **Step 3: 迁移官方 054daa66 的最小 shell 修复**

使用 `iptables -C` 防重、循环删除历史 jump、按其他接口引用决定 flush/delete，并修正 unset 退出码；不扩大其他启动逻辑。

- [x] **Step 4: 运行行为和语法验证**

Run: `python3 -I tests/test_iptables_idempotent.py`
Expected: 全部 PASS。

Run: `bash -n Coding/XX-Net-5.16.7.1/start`
Expected: exit 0。

- [x] **Step 5: 独立提交**

```bash
git add Coding/XX-Net-5.16.7.1/start tests/test_iptables_idempotent.py
git commit -m "fix: 幂等处理 Linux iptables 规则"
```

### Task 2: TLS Relay 综合评分与服务端 adjust

**Files:**
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/tls_relay_front/config.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/tls_relay_front/front.py`
- Modify: `Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/tls_relay_front/ip_manager.py`
- Test: `tests/test_tls_relay_adjust.py`

**Interfaces:**
- Consumes: `set_ips(ips)` 的每个 `dat`、`IpManager.get_speed(ip_str) -> (speed, rtt)`、`config.ip_cal_expect_time_package_size`。
- Produces: `get_ip_sni_host()` 选择 `rtt + ip_cal_expect_time_package_size / speed + float(adjust)` 最小的可用 IPv4/IPv6 线路，并返回所选 `adjust`；缺省为 0。

- [x] **Step 1: 编写固定评分失败测试**

构造两条 host：速度最高但 RTT/adjust 成本更高者不应胜出；验证缺省 adjust、返回值、IPv6 过滤、links/last_try 和 `dispather_work_max_score == 2`。另以 speed=0、非数字/极大 adjust 明确 fail-safe 行为，不让异常逃逸。

- [x] **Step 2: 运行测试确认 RED**

Run: `python3 -I tests/test_tls_relay_adjust.py`
Expected: 当前 best_speed 选择错误或返回字典缺少 `adjust`。

- [x] **Step 3: 手工迁移官方 8613cd7b 并保护输入边界**

合并 config 阈值、front 的 IPv4/IPv6 `adjust` 传递和综合评分；保留本地 IPv6 分支与日志。speed<=0 或非法 adjust 跳过该候选并记录 DEBUG，不扩大到其他 front。

- [x] **Step 4: 运行新测试和现有 front 回归**

Run: `python3 -I tests/test_tls_relay_adjust.py`
Expected: 全部 PASS。

Run: `python3 -I tests/acceptance_independent_models.py`
Expected: 30/30 PASS。

- [x] **Step 5: 独立提交**

```bash
git add Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/tls_relay_front/config.py Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/tls_relay_front/front.py Coding/XX-Net-5.16.7.1/code/default/x_tunnel/local/tls_relay_front/ip_manager.py tests/test_tls_relay_adjust.py
git commit -m "fix: 按综合成本选择 TLS Relay 线路"
```

## 最终验证

- `python3 -I tests/test_iptables_idempotent.py`
- `python3 -I tests/test_tls_relay_adjust.py`
- `python3 -I tests/acceptance_independent_models.py`
- `python3 -I tests/acceptance_production_blackbox.py`：只读正常请求，记录真实结果；F04 单次通过或失败均不扩大结论。
- `git diff --check HEAD~2` 和新鲜审查；不部署、不重启、不 push。

## 2026-11-03 执行记录

- Task 1 提交：`850a855`；最终 fake iptables 行为测试 **10/10 PASS**，`bash -n` 通过。
- Task 2 提交：`a86c7fd`；最终 TLS Relay 测试 **10/10 PASS**，既有独立模型 **30/30 PASS**。
- 既有 IPv6 独立模型使用手工配置，按 `ConfigBase` 真实默认值补 `ip_cal_expect_time_package_size=40000`，未在生产代码中增加静默默认值。
- 新鲜整体审查的三项 Important（超大整数 adjust、异常 speed/RTT、iptables 操作失败返回成功）已各以失败测试复现并在一轮集中修复中转绿；未进行第二轮扩大修改。
- 最终生产只读黑盒 **9/10 PASS**：历史 F04 的默认 HTTP/1.1 24 路 `/debug` 中一路超时、一路连接重置；显式 `Connection: close` 组和代理 `generate_204` 通过。未据此扩大结论。
- 不执行真实 iptables，不部署、不重启、不 push。
