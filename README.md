# XX-Net-Develop — 本地开发仓库

XX-Net macOS ARM64 维护与开发的唯一工件仓库。此前散落 `~/Downloads` 的修复记录、测试、备份、安装器、官方包全部收纳于此，纳入 git 管理。

## 目录职责

| 目录 | 职责 |
|---|---|
| `Coding/XX-Net-5.16.7.1/` | 开发代码快照：官方 5.16.7 基线 + 本地补丁集（版本线 5.16.7.1 起）。补丁清单见其 `LOCAL-VERSION.md` |
| `docs/` | 四份权威文档：`XX-Net-macOS-ARM64-修复记录.md`（完整修复史，唯一真相源）、`XX-Net-版本演进时间线.md`（演进脉络与跨会话背景事实，新会话/子代理先读）、`XX-Net-macOS-修复-PR说明.md`、`XX-Net-根修接力-按启用状态过滤轮询.md` |
| `tests/` | 离线测试 6 个脚本（原 `XX-Net-qc-tests-20261026/` 平铺） |
| `backups/` | 改动前备份：`XX-Net-backup-20261026-113856.zip`、`XX-Net-backup-20261026-181933/`（code.tar.zip + data.tar.zip） |
| `installer/` | ARM64 一键安装器交付包：`install_xxnet_arm64.sh`、`README-安装说明.txt`、`XX-Net-5.16.7.zip`（官方原包）、`boringssl.abi3.so`、`XX-Net-arm64-package.zip`（原始归档） |
| `upstream/` | 官方原包存档：`XX-Net-5.16.7/`、`XX-Net-5.16.7.zip`、`XX-Net-master/`、`XX-Net-mac-5.16.6.zip` |
| `boringssl-arm64/` | ARM64 TLS 库构建区：钉定源码 `pyBoringSSL/`、产物 `boringssl.abi3.so`、构建配方 `BUILD-macOS-ARM64.md`、上游参考 `pyBoringSSL-main.zip` |

## 运行环境关系

- 本机实装运行目录：`/Applications/XX-Net`（只读参照源，迁移时仅 rsync 读取，未做任何改动）
- 代码快照（`Coding/`）与实装目录的差异仅为运行数据 `data/` 与缓存文件（快照有意排除）
- 改动流程：在 `Coding/` 内开发 → 验证 → 同步到 `/Applications/XX-Net` → git commit 按版本提交

## 离线测试运行方式

```bash
python3 tests/test_dns_dot.py
python3 tests/test_try_add_remove_races.py
python3 tests/test_ipv6_filter.py
python3 tests/test_blockio_ratelimit.py
python3 tests/test_dns_nameserver_filter.py
python3 tests/probe_wrap_socket_fd.py   # 探针，非断言测试
```

均为 stub 包 + 按文件加载方式离线运行，不触碰运行中进程。

**注意**：脚本内 `BASE` 为硬编码 `/Applications/XX-Net`——测试目标是本机运行中的实装安装（读取其代码做 stub 加载），而非本仓库的开发快照，这属预期设计，与既有文档描述一致。若将来要在其他机器运行或对快照跑测试，再议参数化。

## 工件索引（按用途）

- 恢复上下文/接力维护：先读 `docs/XX-Net-版本演进时间线.md` 建立时间线认知，再读 `docs/XX-Net-macOS-ARM64-修复记录.md` 的目录与末三章，然后按 `Coding/XX-Net-5.16.7.1/LOCAL-VERSION.md` 对照补丁
- 回滚：`backups/XX-Net-backup-20261026-181933/`（四项修复上线前完整备份）
- 重建 BoringSSL：`boringssl-arm64/BUILD-macOS-ARM64.md`（钉定 commit + 指纹核验值）
- 重装/分发：`installer/install_xxnet_arm64.sh`

## git 约定

首 commit 收纳全部内容；此后每轮修复按版本（如 5.16.7.1 → 5.16.7.2）提交。`.gitignore` 排除 `.DS_Store`、`__pycache__/`、`*.pyc`、`*.log`。
