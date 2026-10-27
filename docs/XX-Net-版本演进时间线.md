# XX-Net macOS ARM64 版本演进时间线（2025 → 2026-10）

> 用途：跨会话/跨代理共享的背景事实文档。任何新会话或子代理接手前，先读本文档建立时间线认知，避免把「官方 5.16.7 自带 TLS 库」等错误预设带入工作。
> 关系：本文档是 `XX-Net-macOS-ARM64-修复记录.md`（操作细节唯一真相源）的时间线索引层，两者冲突时以修复记录为准；本文档新增的实证结论以本文档为准并回写修复记录。

## 一、问题根源（5.16.6 时代，2025-2026 初）

- 官方 macOS 包 `XX-Net-mac-5.16.6.zip` 内含 `python3.zip`（x86_64 嵌入式 Python 3.10 + `boringssl.abi3.so`），但**安装流程从不解压**它
- `start` 脚本检测不到 `python3/bin/python3` → 回退系统 Python → 系统 Python 用 LibreSSL 2.8.3 → TLS 指纹老旧，被 GFW 识别并干扰 → x_tunnel 连接极不稳定（TLS 握手超时、602 错误）
- Windows 版稳定的原因：Windows 包内 BoringSSL 正常生效（指纹模拟 Chrome）

## 二、临时方案：Rosetta 2（2026 初，勉强可用）

- `softwareupdate --install-rosetta` + 手动 `unzip python3.zip` → x86_64 转译运行
- 效果：连接恢复正常，但性能有转译开销，且每次安装需手工补这步

## 三、期间本机修复集（详见 `XX-Net-macOS-修复-PR说明.md` 与修复记录）

- PyObjC 11.x 兼容（托盘崩溃）、托盘 delegate 未触发、helper 持久化（授权弹窗）、中文翻译补齐
- 一次**失败的** ARM64 原生 BoringSSL 编译尝试：克隆最新 master，新版默认启用扩展排列（`SSL_CTX_set_permute_extensions`）与 session ticket 控制（`SSL_CTX_set_num_tickets`），与官方固定版本 TLS ClientHello 不一致 → relay 返回 400/500 → 已回退
  - 注意：这两个 API 在官方钉定版（2022-12-30）中**已存在**，失败真因是**新 master 默认启用**（2026-10-27 符号级实证修正，此前误记为"API 不存在"）

## 四、邮件 → 官方 5.16.7 release（2026-09-29）

- 用户 2026 年 9 月底向作者发邮件反馈 macOS ARM64 问题，作者回复并于 2026-09-29 release XX-Net 5.16.7
- **关键事实（2026-10-27 实证，upstream/XX-Net-5.16.7.zip 解包核验）**：
  - 官方 5.16.7 包内**没有** `python3.zip`、**没有** `lib/darwin/`、**没有任何 BoringSSL 二进制**
  - "原生支持 ARM64" 指程序本身可用系统 Python（arm64 原生）直接运行，不再强制 Rosetta
  - **TLS 库缺口官方并未补齐**——需要用户侧自行提供 arm64 原生 `boringssl.abi3.so` 放入 `code/default/lib/darwin/`
  - 官方 `start` 脚本仍保留 `python3/bin/python3` 逻辑（面向 Linux 桌面发行版），macOS 场景下找不到嵌入式 Python 时打印 "Using system python…" 提示

## 五、pyBoringSSL 补齐与当前部署（2026-10-26，Warp 会话完成）

- 某 Warp 会话定位到 **theol-git/pyBoringSSL**（作者仓库，即官方 python3.zip 内 BoringSSL 的构建来源），**对照官方 x86_64 包内 `.so` 反推钉定 commit**：
  - pyBoringSSL 主仓库 `01d935f`（2023-01-12，HEAD）
  - boringssl 子模块 `5e4925c`（2022-12-30 "fix the iOS issue."——与官方 `.so` 对应的源码版本）
  - brotli 子模块 `d35319e`（2022-12-27，XX-net fork "force brotli to generate static lib."）
  - 早期笔记曾把 `01d935f` 误记为 brotli 的 commit（子代理已按 git log 实证勘误）
- ARM64 原生编译成功：cmake + ninja + cffi（Py_LIMITED_API → abi3），`BORINGSSL_PREFIX=BSSL`
- 指纹验证：ja3_hash / ja4 / peetprint_hash 与官方 x86_64 版完全一致（`cd08e31494f9531f560d64c695473da9` 等）
- pip 补齐 Python 依赖（PyObjC/Pillow 等，PEP 668 用 `--break-system-packages`）→ 完成 `/Applications/XX-Net` 5.16.7 + arm64 原生库的部署
- 产物落位 `code/default/lib/darwin/boringssl.abi3.so`，运行日志 `TLS implementation: BoringSSL`
- 后续本机修复持续在该部署上进行：四项修复、B1-B6 质检、E 阶段 DNS nameserver 过滤（详见修复记录末三章与 `Coding/XX-Net-5.16.7.1/LOCAL-VERSION.md`）

## 六、符号级实证（2026-10-27，Q0 验证）

证明「本地 arm64 库与官方 x86_64 库同一源码 commit」的硬核证据：

- 从 `upstream/XX-Net-mac-5.16.6.zip` 提取官方 x86_64 `boringssl.abi3.so`（shasum `ca44af73…`）与本地 arm64 库（shasum `ebebfc77…`）做全量 `BSSL_` 符号名集合对照
- 结果：官方 3344 个 / 本地 3288 个，**交集 3281（99.8%）**
- 差异逐条可解释，无一指向版本不同：
  - x86 独有 63 个：`CRYPTO_rdrand`、`OPENSSL_ia32cap_P`、`RSAZ_1024_mod_exp_avx2`、`*_once`/`*_storage` 等——x86 专属汇编与延迟初始化实现差异（arm64 侧 rdrand/avx2 零命中）
  - arm64 独有 7 个：`__ZZ25BSSL_SSL_SESSION_to_bytesE20kNotResumableSession` 等——C++ 名字修饰长度差异导致的提取碎片，双侧同源
- 版本定性 API 双侧计数一致：`permute_extensions` 2/2、`set_num_tickets` 1/1
- 结论：**钉定三元组正确**；`Coding/…/boringssl-arm64/pyBoringSSL/` 入库源码即为正确版本

## 七、仓库与工件现状（2026-10-27 迁移后）

- `~/Downloads/XX-Net-Develop/`：本地开发仓库（git master），结构见其 `README.md`
- `~/Downloads/MIGRATION-20261027.md`：迁移前后对照说明
- 网页下载的 `pyBoringSSL-main.zip`（2023-01-13 GitHub 快照）：**子模块目录为空、单独不可构建**，仅作包装层上游参考归档于 `boringssl-arm64/`
- `/tmp/xxnet57`：2026-10-25 安装测试残留临时文件，可清理
- 运行环境：`/Applications/XX-Net`（5.16.7 + 全部本地补丁），launcher PID 27738（2026-10-27 12:47 启动）

## 八、遗留验证（进行中）

「入库源码 == 运行中 .so」的完整验证链（Q1 zip↔01d935f diff、Q2 入库树↔三元组复核、Q3 源码↔.so 证据链与金标准重建）方案与结论见 `boringssl-arm64/BUILD-macOS-ARM64.md` 的「源码-产物匹配性验证」章节（完成后回填）。
