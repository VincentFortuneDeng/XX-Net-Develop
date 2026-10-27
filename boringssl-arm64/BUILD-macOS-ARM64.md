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

## 源码-产物匹配性验证（2026-10-27，Q0-Q3）

> 验证环境：`/tmp/bssl-verify`（全程不触碰 `/Applications/XX-Net` 与运行中进程）；x86_64 参照物为官方 5.16.6 包内 `boringssl.abi3.so`（shasum `ca44af73…`）。

### Q0 两架构符号对照（摘要）

官方 x86_64 .so 3344 个 `BSSL_` 符号 vs 本地 arm64 .so 3288 个，**交集 3281（99.8%）**；差异逐条可解释：x86 独有 63 个（CRYPTO_rdrand/AVX2 等专属汇编 + once/storage 实现差异），arm64 独有 7 个（`__ZZ…` C++ 修饰提取碎片）。特征 API 双侧计数一致：`permute_extensions` 2/2、`set_num_tickets` 1/1。结论：**同源码 commit，钉定三元组正确**。

### Q1 zip 快照 == 主仓库 01d935f？——✅ 一致

`pyBoringSSL-main.zip`（2023-01-13 GitHub 网页快照）解压后与全新 clone 检出 `01d935f` 逐文件 `diff -rq`（排除 .git）：**零差异**。zip 快照与钉定提交同源，可继续作为上游参考归档。

### Q2 入库源码树 == 钉定三元组？——✅ 一致（附一处勘误性发现）

全新 clone + submodule init + 钉定检出后与 `pyBoringSSL/` 入库树 `diff -rq`（排除 .git）：**唯一差异为 `.gitmodules` 的 SSH→HTTPS URL**（源码入库剥除时的预期修改，BUILD 文档已有记载），其余全部文件（含两个子模块全树）零差异。

三层 git log 实证：

| 层 | commit | 日期 | 提交标题 |
|---|---|---|---|
| pyBoringSSL 主仓库 | `01d935f` | 2023-01-12 | "add support building on arm 32bit Android." |
| boringssl 子模块 | `5e4925c` | 2022-12-30 | "fix the iOS issue." |
| brotli 子模块 | `d35319e` | 2022-12-27 | "force brotli to generate static lib." |

> **勘误性发现**：主仓库 `01d935f` HEAD 树中 boringssl 的 gitlink 实际记录为 `e8ece8ec`（2022-12-21 "ignore ecdsa cmp for ARM64 to avoid bug in iOS"），与钉定的 `5e4925c` 是**分叉关系**（非先后）。即钉定的 `5e4925c` 并非主仓库默认子模块指针，而是对照官方 x86_64 .so 反推的结果——该反推由 Q0 符号实证独立支撑，钉定结论不受影响；但按主仓库默认 gitlink 构建会得到不同源码版本，需继续人工钉定 `5e4925c`。

### Q3 源码 == 运行中 .so？——✅ 四级证据链全部通过

**Q3a 符号集对照**（源码清单 `boringssl_symbols.txt` 3547 个裸名 → `BSSL_` 前缀）：

| 集合 | 数量 | 说明 |
|---|---|---|
| 运行 .so 符号 | 3288 | 与 Q0 一致 |
| 源码清单符号 | 3547 | |
| 交集 | 3182 | |
| 仅 .so 有 | 106 | 99 个为编译器为函数内静态变量生成的延迟初始化符号（`*_init/_once/_storage/_do_init`，基础名全部在源码清单中）+ 7 个 C++ 修饰名提取碎片（如 `BSSL_…E20kNotResumableSession`）|
| 仅清单有 | 365 | **官方 x86_64 .so 同样全部缺失**（365/365）——两架构一致的链接裁剪（cffi bundle 只拉入被包装引用的目标文件），非版本差异 |

cffi cdef 声明的 43 个 `BSSL_` 函数在运行 .so 中全部有对应符号；cdef 中 7 个非 BSSL_ 声明（Brotli* 5 个 + 辅助模块函数）对应 brotli/cert_decompress 的无前缀符号，经 nm 确认存在于库中。特征 API 符号齐备：`SSL_CTX_set_permute_extensions`、`SSL_set_permute_extensions`、`SSL_CTX_set_num_tickets`、`SSL_CTX_get_num_tickets`。

**Q3b 版本特征定性**：入库 boringssl 源码（5e4925c 等价）中 API 声明命中——`ssl.h:4485/4489`（permute_extensions）、`ssl.h:2289`（set_num_tickets）；默认值 `permute_extensions(false)`（`ssl/ssl_lib.cc:523`）。**API 存在但默认不启用**，与官方 ClientHello 无扩展排列一致。

**Q3c 字符串指纹**：运行 .so 中 4 条 CRYPTOGAMS ARMv8 汇编标记（ChaCha20 / SHA1 / SHA256 / SHA512 block transform for ARMv8）在源码树 perlasm 源 `chacha-armv8.pl`、`sha1-armv8.pl`、`sha512-armv8.pl`（`SHA$BITS` 参数化）中逐条命中。

**Q3d 金标准重建**（/tmp 按 BUILD 配方全新重建）：

配方实测勘误（相对本文档上文配方，重建时需补齐）：
- cmake 需加 `-DBORINGSSL_PREFIX_SYMBOLS=<仓库>/boringssl_symbols.txt`（5e4925c 的 CMakeLists 强制两者成对，经 Go 的 `make_prefix_headers.go` 生成前缀头）；
- brotli cmake target 实名为 `brotlicommon`/`brotlidec`（产物 `libbrotlicommon.a` 等，需复制为 `libbrotlicommon-static.a`/`libbrotlidec-static.a` 对齐 `build_pyBoringSSL.py` 链接名）；
- boringssl 产物 `libssl.a`/`libcrypto.a` 需复制为 `libbssl.a`/`libbcrypto.a` 对齐链接名；
- `cert_decompress`/`getpeercert` 独立 cmake 时其 CMakeLists 的 `${CMAKE_BINARY_DIR}/boringssl/symbol_prefix_include` 需 symlink 桥接；
- 打包需 setuptools（cffi ≥2 在 Python ≥3.12 强制要求），验证使用隔离 venv。

重建结果（Python 3.15 + AppleClang 21 编译，产物 `boringssl.cpython-315-darwin.so`，Mach-O arm64 bundle）：

- **符号对照**：运行 .so 3288 个符号**全部**被重建 .so（3510 个）覆盖，**0 缺失**。重建多出 222 个全部归类为工具链差异：173 个为编译器修饰/内联实例（C++ 构析 C1/C2/D1/D2、`_bss_get` helper、内联 static 变量实例），49 个为同一 5e4925c 源码的真实符号（均在本次静态库符号表中；旧工具链将其内联或链接器裁剪）。
- **TLS 行为/指纹对照**（独立 python 进程分别加载重建库与运行库副本，经本地代理 CONNECT 隧道端到端测 TLS）：
  - 最小配置（TLS_method 默认）：两库 ja3_hash 一致（`c318b1ae5d7abaaa0265af514737f2b8`）、ja4 一致（`t13d181000_e8a523a41297_78e6aca7449b`），逐字段相同；
  - XX-Net 实际配置（复刻 `boringssl_wrap.py` 序列：grease + `ALL:!aPSK:!ECDSA+SHA1:!3DES` + ALPN h2/http1.1 + OCSP/ SCT + 8 项 verify prefs + min TLS1.2 + SetCompression）：两库握手均成功（0.4s 级）、ALPN 均协商 h2、后续 h2 交互失败模式一致（browserleaks 服务器 GOAWAY(0)）；
  - `tls.peet.ws` 经当前链路不可达（两库及 curl 失败模式一致，握手即复位）——基准 `cd08e314…` 的 peet.ws 实测以构建时记录（见上文指纹表）与 Q0 同源性证据为准。
- **附带实证（对指纹语义的澄清）**：经 `127.0.0.1:8086` 的 CONNECT 隧道 TLS 为端到端透传，探测站看到的指纹由发起端栈决定（curl 经代理测得 curl/LibreSSL 指纹）——`cd08e314…` 指纹属于 BoringSSL 库自身发起的连接（x_tunnel 前端连接），与 curl 无关。另实证：运行部署的 cdef 未声明 `set_num_tickets`/`set_permute_extensions`，`boringssl_wrap.py` 中这两个调用实际触发 AttributeError 走 except 分支**未生效**——指纹面与官方"无排列"行为自洽。

### 判定

**可用（可复现级）**。Q1/Q2/Q3a-c 全过（归档级成立），Q3d 金标准重建成立：源码可从钉定三元组独立复现产物，符号 0 缺失、多出全部归因工具链，独立进程加载两库的 TLS 行为与指纹逐字段一致。钉定三元组与入库源码树即为运行中 `boringssl.abi3.so` 的可信来源；重建时按上文勘误补齐配方参数。
