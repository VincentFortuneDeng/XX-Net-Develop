# BoringSSL ARM64 构建配方（macOS，Apple Silicon）

本文档记录本仓库 `boringssl-arm64/` 的来源与完整重建配方。产物 `boringssl.abi3.so` 为 ARM64 原生 TLS 库，指纹与官方 x86_64 版完全一致，免 Rosetta 2。

## 钉定 commit（2026-10-27 实测核验）

构建前必须钉定以下版本，勿用 master（新版 BoringSSL 默认启用扩展排列与 session ticket 控制，会导致 relay 返回 400/500）：

| 层 | commit | 日期 | 说明 |
|---|---|---|---|
| pyBoringSSL 主仓库 | `01d935f` | 2023-01-12 | HEAD；"add support building on arm 32bit Android." |
| boringssl 子模块 | `5e4925c` | 2022-12-30 | "fix the iOS issue."（与官方 python3.zip 内 .so 对应版本） |
| brotli 子模块 | `d35319e` | 2022-12-27 | XX-net fork `xxnet` 分支；"force brotli to generate static lib." |

> 转写勘误：早期文档笔记曾把 `01d935f` 记为 brotli 的 commit，经 git log 实证，`01d935f` 属 pyBoringSSL 主仓库，brotli 在该 HEAD 处的 gitlink 即 `d35319e`。

## 源码获取与钉位

```bash
git clone https://github.com/theol-git/pyBoringSSL
cd pyBoringSSL
git checkout 01d935f                     # 主仓库 HEAD
# 子模块 .gitmodules 原为 git@github.com: SSH URL，无 SSH 凭据时改为 HTTPS：
git -c url."https://github.com/".insteadOf="git@github.com:" submodule update --init --recursive
git -C boringssl checkout 5e4925c        # 2022-12-30 "fix the iOS issue."
git -C brotli checkout d35319e           # 2022-12-27 "force brotli to generate static lib."
git -C boringssl log -1                  # 核验
git -C brotli log -1                     # 核验
```

本仓库 `pyBoringSSL/` 即上述钉定状态的源码快照（三层 .git 已剥除，以普通文件入库；`.gitmodules` 的 SSH→HTTPS 修改随剥除一并消除）。上游参考归档：`pyBoringSSL-main.zip`。

## 编译

```bash
# 1. arm64 静态库（ninja + cmake）
cmake -GNinja -DCMAKE_OSX_ARCHITECTURES=arm64 -DBORINGSSL_PREFIX=BSSL \
      -DBORINGSSL_ALLOW_CXX_RUNTIME=1 boringssl && ninja -C build

# 2. cffi 编译包装（build_pyBoringSSL.py）
#    cffi 自动启用 Py_LIMITED_API，产出 abi3 稳定 ABI
#    → boringssl.abi3.so，一个文件兼容 Python 3.10 ~ 3.15+
python3 build_pyBoringSSL.py
```

关键点：

- `BORINGSSL_PREFIX=BSSL`：符号前缀，避免与系统 OpenSSL 冲突。
- cffi + `Py_LIMITED_API`：产出 abi3 稳定 ABI，单一 .so 跨 Python 版本。
- 指纹与官方 x86_64 版完全一致：同一源码 commit + 同一 cdef 包装，一致性是构造出来的。

## 指纹验证（tls.peet.ws/api/all 实测，构建时记录）

| 指标 | 官方 x86_64 版 (Rosetta) | 本配方 arm64 版 |
|---|---|---|
| ja3_hash | `cd08e31494f9531f560d64c695473da9` | `cd08e31494f9531f560d64c695473da9` |
| ja4 | `t13d1516h2_8daaf6152771_e5627efa2ab1` | `t13d1516h2_8daaf6152771_e5627efa2ab1` |
| peetprint_hash | `22a4f858cc83b9144c829ca411948a88` | `22a4f858cc83b9144c829ca411948a88` |

## 产物指纹（2026-10-27 迁移时三方核验）

`boringssl.abi3.so` 三份副本 shasum -a 256 一致：

- `/Applications/XX-Net/code/default/lib/darwin/boringssl.abi3.so`（实装部署）
- `../installer/boringssl.abi3.so`（交付包）
- `./boringssl.abi3.so`（本目录留存产物）

```
ebebfc77e96e132ef2eb83ef2e34623041bb70efebaaa12d49d8aac7ff9a9bc0
```
