# XX-Net macOS ARM64 修复记录

**日期**：2026-02-26  
**系统**：macOS ARM64 (Apple Silicon)  
**XX-Net 版本**：5.16.6  
**安装路径**：`/Applications/XX-Net/`  
**背景时间线**：完整演进脉络（5.16.6 根因 → Rosetta → 5.16.7 release → pyBoringSSL 补齐 → 符号级实证）见同目录 `XX-Net-版本演进时间线.md`；本文档是操作细节唯一真相源，两者冲突时以本文档为准

---

## 问题一：macOS 托盘（系统菜单栏）无法正常显示

### 现象
- 启动后托盘菜单为英文，中文本地化失效
- 终端启动时 `applicationDidFinishLaunching_` 未触发
- 偶发崩溃（PyObjC 11.x 兼容性问题）

### 根本原因
PyObjC 11.x 将 `objc` 从 `AppKit.objc` 中移出，独立为顶层模块。

### 修复文件
`/Applications/XX-Net/code/default/launcher/mac_tray.py`

### 修改内容
```python
# 1. 文件顶部添加独立 import
import objc

# 2. 装饰器修复（约第 496 行）
# 修改前：
@AppKit.objc.callbackFor(AppKit.NSApplication.sharedApplication().delegate())
# 修改后：
@objc.callbackFor(...)

# 3. 语言检测移至 import AppKit 之后，添加备选方案
lang_code = NSLocale.preferredLanguages()[0][:2] if NSLocale.preferredLanguages() else "en"

# 4. setupHelper 改为守护线程（避免阻塞 AppKit 主事件循环）
threading.Thread(target=self.setupHelper, daemon=True).start()

# 5. serve_forever() 中添加 finishLaunching
app.finishLaunching()
```

---

## 问题二：中文翻译缺失

### 现象
Web UI 状态页面的系统信息标签（Python Version、OpenSSL Version 等）显示英文

### 修复文件
- `code/default/launcher/lang/zh_CN/LC_MESSAGES/messages.po`
- `code/default/smart_router/lang/zh_CN/LC_MESSAGES/messages.po`
- `code/default/launcher/web_ui/status.html`

### 修改内容
- `messages.po`：补充 15+ 条缺失的中文翻译（配置、代理设置、系统信息标签等）
- `status.html`：对系统信息表格中未国际化的标签添加 `{{ _() }}` 包裹

---

## 问题三：X-Tunnel 连接极不稳定（最核心问题）

### 现象
- 同一 WiFi 下，Windows 运行 XX-Net 网络流畅，macOS ARM64 极不稳定
- 频繁出现 TLS 握手超时、602 错误、大量连接异常
- Web UI 状态页重启后数值不更新

### 排查过程

#### 第一步：确认不是代码逻辑问题
检查 Smart Router 与 X-Tunnel 的调用链，确认 `create_conn()` → `login_process()` 的触发机制正常。

#### 第二步：发现 SSL 实现差异
```bash
python3 -c "import ssl; print(ssl.OPENSSL_VERSION)"
# 输出：LibreSSL 2.8.3
```
系统 Python (`/usr/bin/python3`) 使用 Apple 内置的 **LibreSSL 2.8.3**，该版本有老旧且特征明显的 TLS 指纹，GFW 可识别并针对性干扰。

#### 第三步：发现 python3.zip 未解压
XX-Net 安装包中包含 `python3.zip`，其中内嵌了带有 **BoringSSL** 的 Python 3.10，但该 zip **从未被解压**。start 脚本检查 `python3/bin/python3` 是否存在，不存在则回退到系统 Python。

```bash
unzip -l /Applications/XX-Net/python3.zip | grep boringssl
# 输出：python3/lib/python3.10/site-packages/boringssl.abi3.so
```

**Windows 稳定的原因**：Windows 版本的预编译包内含 BoringSSL，TLS 指纹模拟 Chrome，GFW 无法识别。

#### 第四步：确认架构兼容性
```bash
unzip -p /Applications/XX-Net/python3.zip python3/bin/python3 > /tmp/test && file /tmp/test
# 输出：Mach-O 64-bit executable x86_64
```
内嵌 Python 是 **x86_64** 架构，ARM64 Mac 需要 **Rosetta 2** 才能运行。

### 最终解决方案

#### 方案：安装 Rosetta 2 + 解压 python3.zip（推荐，已验证稳定）

```bash
# 步骤 1：安装 Rosetta 2（让 ARM64 Mac 运行 x86_64 程序）
softwareupdate --install-rosetta --agree-to-license

# 步骤 2：解压内嵌 Python（含 BoringSSL）
unzip /Applications/XX-Net/python3.zip -d /Applications/XX-Net/
```

**验证方法**：
```bash
/Applications/XX-Net/python3/bin/python3 -c "
import sys; sys.path.insert(0, '/Applications/XX-Net/code/default/lib/noarch')
from front_base import openssl_wrap
print('SSL implementation:', openssl_wrap.implementation)
# 应输出：SSL implementation: BoringSSL
"
```

重启 XX-Net 后，日志应显示：
```
Darwin using embedded python.
TLS implementation: BoringSSL
```

#### 效果对比

| | 修复前 | 修复后 |
|---|---|---|
| Python | `/usr/bin/python3` (系统) | `python3/bin/python3` (嵌入式) |
| 架构 | ARM64 原生 | x86_64（Rosetta 转译） |
| SSL | LibreSSL 2.8.3 | BoringSSL |
| TLS 指纹 | 老旧，易被 GFW 识别 | 模拟 Chrome，GFW 无法识别 |
| 连接稳定性 | 极差 | 正常（与 Windows 一致） |

### 背景注记（2026-10-27 补充，arm64 原生库来源与钉定依据）

- **官方包内不含任何 TLS 库（实证）**：官方 5.16.7 包（`upstream/XX-Net-5.16.7.zip` 解包核验）内没有 `python3.zip`、没有 `lib/darwin/`、没有任何 BoringSSL 二进制；"原生支持 ARM64" 仅指程序可用系统 Python 运行。本节上表描述的 Rosetta + python3.zip 方案只适用于 5.16.6 时代，5.16.7 起 arm64 原生 `boringssl.abi3.so` 需用户侧自行提供并放入 `code/default/lib/darwin/`。
- **arm64 库来源与钉定依据**：当前部署的 `boringssl.abi3.so`（shasum `ebebfc77…`）按 pyBoringSSL 钉定三元组构建（主仓库 `01d935f` / boringssl `5e4925c` / brotli `d35319e`），构建配方见 `boringssl-arm64/BUILD-macOS-ARM64.md`。该钉定经三重实证：Q0 两架构符号对照（交集 99.8%）、Q1-Q2 源码树逐文件比对（零差异）、Q3 四级证据链含金标准重建（符号 0 缺失、独立进程加载两库指纹逐字段一致）。完整验证数据见 BUILD 文档「源码-产物匹配性验证（2026-10-27）」章节；演进脉络见 `XX-Net-版本演进时间线.md` 第四、五、六、八章。
- 注意：主仓库默认 boringssl gitlink 为 `e8ece8ec`（与钉定的 `5e4925c` 分叉），重建时必须按 BUILD 文档人工钉定，勿依赖 submodule 默认指针。

---

## 问题四：proxy_session.py 中误加 watchdog 线程

### 背景
在分析连接不稳定问题时，曾错误地在 `proxy_session.py` 中添加了一个每 60 秒重试的 watchdog 线程作为临时补丁。在找到 BoringSSL 根本原因后，该 watchdog 已不再需要。

### 处理
已从 `proxy_session.py` 中删除 `_session_watchdog` 方法及其启动代码，恢复为与官方代码一致的状态。Session 的启动和恢复依赖 `create_conn()` 的正常触发机制。

---

## 附：ARM64 原生方案（实验性，不推荐）

> 尝试为 ARM64 原生编译 BoringSSL，但最终因 TLS 行为差异导致连接不稳定，已回退。记录供参考。

### 思路
1. 克隆 `XX-net/boringssl` + `XX-net/brotli`
2. 用 cmake + ninja 编译 ARM64 静态库
3. 编写 cffi 包装（`bssl_wrapper.c` + `build_boringssl.py`）
4. 生成 `boringssl.cpython-39-darwin.so`（原生 ARM64）

### 失败原因
- 克隆的是最新 master，与 `python3.zip` 中的固定版本存在 TLS ClientHello 差异
- `SSL_CTX_set_permute_extensions` 和 `SSL_CTX_set_num_tickets` 在新版中实际生效（原版未暴露该接口），导致 relay 服务器返回 400/500

### 正确路径（若将来继续）
1. 找到 `python3.zip` 中 `boringssl.abi3.so` 对应的精确 git commit
2. 基于该 commit 编译，而不是 latest master
3. 或直接向 XX-Net 官方提交 ARM64 原生包支持的 PR

---

## 问题五：每次重启后弹出管理员权限申请框

### 现象
每次 macOS 重启后首次启动 XX-Net，都会弹出「XX-Net 想要进行更改」的系统授权对话框。

### 根本原因
`mac_tray.py` 将 helper 二进制存放在 `/tmp/helper`。macOS 每次重启都会清空 `/tmp/`，导致每次启动时 `setupHelper()` 检测不到 helper，触发重新安装流程（`chown root` + `chmod 4755`），从而弹出授权框。

`mac_helper` 本身是 ARM64 原生二进制，架构没有问题，问题纯粹是存储位置选择不当。

### 修复方案
将 helper 路径从 `/tmp/helper` 改为应用目录内的持久路径，并在 `setupHelper()` 中增加权限检查，已正确安装时直接跳过。

**修改文件**：`code/default/launcher/mac_tray.py`

```python
# 修改前
helper_path = os.path.join('/tmp', 'helper')

# 修改后
helper_path = os.path.join(current_path, 'mac_helper_root')
```

```python
# setupHelper() 新增检查逻辑
def setupHelper():
    # 已存在且为 setuid-root，直接返回，不再弹授权框
    if os.path.exists(helper_path):
        try:
            st = os.stat(helper_path)
            if st.st_uid == 0 and (st.st_mode & 0o4000):
                return
        except Exception:
            pass
    # 首次安装或文件损坏时才执行（只弹一次）
    ...
```

**一次性迁移**（已执行，无需再操作）：
```bash
sudo cp /tmp/helper /Applications/XX-Net/code/default/launcher/mac_helper_root
sudo chown root /Applications/XX-Net/code/default/launcher/mac_helper_root
sudo chmod 4755 /Applications/XX-Net/code/default/launcher/mac_helper_root
```

### 效果
- 首次安装后弹一次授权框（仅限全新安装或手动删除 `mac_helper_root` 时）
- 此后无论重启多少次，启动 XX-Net 均不再弹框

---

## 总结

macOS ARM64 上 XX-Net 不稳定的**根本原因**是：

> `python3.zip`（含 BoringSSL 的嵌入式 Python）**存在但从未被解压**，导致 XX-Net 使用系统 LibreSSL，TLS 指纹被 GFW 识别并干扰。

**一行修复**：安装 Rosetta 2，然后 `unzip /Applications/XX-Net/python3.zip -d /Applications/XX-Net/`

这是 XX-Net macOS 安装包的一个 **bug**（安装时应自动解压 `python3.zip` 但未执行），建议向官方反馈。

---

## 后续：ARM64 原生方案成功（2026-10，取代上文“附：ARM64 原生方案”）

上文“实验性，不推荐”的结论已被推翻。当时失败的根本原因是**克隆了最新 master**：新版 BoringSSL 默认启用扩展排列与 session ticket 控制，而官方 `python3.zip` 里的 `boringssl.abi3.so` 对应 2022-12-30 的固定版本。钉住正确 commit 后，ARM64 原生编译完全可行，且无需 Rosetta 2。

### 成功配方（源自作者仓库 theol-git/pyBoringSSL）

```bash
# 1. 源码与版本钉位
git clone https://github.com/theol-git/pyBoringSSL
cd pyBoringSSL
git checkout 01d935f                   # 主仓库 HEAD（2023-01-12，arm32 Android 构建支持）
git -C boringssl checkout 5e4925c        # 2022-12-30 "fix the iOS issue"
git -C brotli checkout d35319e           # xxnet 分支 2022-12-27 "force brotli to generate static lib."

# 2. 编译 arm64 静态库 (ninja + cmake)
cmake -GNinja -DCMAKE_OSX_ARCHITECTURES=arm64 -DBORINGSSL_PREFIX=BSSL \
      -DBORINGSSL_ALLOW_CXX_RUNTIME=1 boringssl && ninja -C build

# 3. cffi 编译包装 (build_pyBoringSSL.py)
#    cffi 自动启用 Py_LIMITED_API，产出 abi3 稳定 ABI
#    → boringssl.abi3.so，一个文件兼容 Python 3.10 ~ 3.15+
```

关键点：
- `BORINGSSL_PREFIX=BSSL` 避免与系统 OpenSSL 符号冲突
- 指纹与官方 x86_64 版**完全一致**（同一源码 commit + 同一 cdef 包装，一致性是构造出来的）

### 指纹验证（tls.peet.ws/api/all 实测）

| 指标 | 官方 x86_64 版 (Rosetta) | 新编译 arm64 版 |
|---|---|---|
| ja3_hash | `cd08e31494f9531f560d64c695473da9` | `cd08e31494f9531f560d64c695473da9` |
| ja4 | `t13d1516h2_8daaf6152771_e5627efa2ab1` | `t13d1516h2_8daaf6152771_e5627efa2ab1` |
| peetprint_hash | `22a4f858cc83b9144c829ca411948a88` | `22a4f858cc83b9144c829ca411948a88` |

### 一键安装器（交付包）

`~/Downloads/XX-Net-arm64-package/` 三个文件：`install_xxnet_arm64.sh` + `XX-Net-5.16.7.zip`（官方 release）+ `boringssl.abi3.so`。脚本功能：

- 检查 arm64 架构与 Python 3.10~3.15（无 Python 时打印安装指引后中止）
- 依赖安装需对 Homebrew Python 加 `--break-system-packages`（PEP 668；旧 pip 不认识该参数，脚本先带参尝试再退回）
- 可选安装位置：`/Applications/XX-Net` 或当前目录；替换旧版前自动 zip 备份到 `~/Downloads/`（排除 `*.log`），拒绝替换则原样不动
- 旧版 `data/` 迁入新版时排除 `*.log`（避免把数 GB 运行日志带入新装）
- `xattr -c -r` 清除 Gatekeeper 隔离标记；仅安装不启动

### 实机验收

- `/Applications/XX-Net` 从 5.16.6（Rosetta）真实替换为 5.16.7（arm64 原生）
- 运行日志出现 `TLS implementation: BoringSSL`；`boringssl.abi3.so` 已映射进运行进程（lsof 确认）
- 代理 127.0.0.1:8086 访问 `google.com/generate_204` 返回 204
- 官方 `start` 包装脚本在找不到捆绑 py39 时打印的 "Using system python, TLS will fall back to system SSL" 只是过时提示——abi3 库在系统/brew Python 上照常加载，以运行日志 `TLS implementation: BoringSSL` 为准

### 给官方的建议

1. 官方可直接复用本构建产物（`boringssl.abi3.so`）为 macOS 提供 arm64 原生 TLS 库，免去 Rosetta 2 依赖
2. `start` 脚本建议在 abi3 库可加载时不再打印误导性的 system SSL 提示
3. 安装/升级时 `data/` 迁移应排除 `*.log`（本次实测 x_tunnel 目录日志累积达 19 GB）

---

## 后续：启动期异常提示盘点与源码级修复（2026-10）

### 盘点结论

当前（5.16.7 + arm64 原生 BoringSSL + 依赖已装齐）实际会打印的异常提示只剩三类：

| 提示 | 出处 | 根因 |
|---|---|---|
| `Using system python, TLS will fall back to system SSL instead of BoringSSL.` | `start:166` | 只要没有内嵌 `python3/` 就无条件打印，不检测 abi3 库实际可加载与否，属误导 |
| 自动执行 `sudo pip3 install -U PyObjC Pillow` | `start:168` | 缺 PyObjC 时弹 sudo；PEP 668 (Homebrew Python) 下必失败 |
| `request /module/... no module in path` (WARNING) | `launcher/web_control.py:192,269` | 模块未启用时 WebUI 仍会请求其端点，正常条件却记 WARN，噪音 |
| `[ERROR] debug:KeyError(...)` | `launcher/web_control.py:954` | `sys._current_frames()[thread.ident]` 竞态：enumerate 到取帧之间线程已退出 |

历史遗留（新装不再出现）：`mac_tray AttributeError('objc')`（旧版缺 PyObjC）、`helper failed: OSError(86 Bad CPU type)`（旧 helper 仅 x86_64；5.16.7 为 universal）、`/tmp/helper` 相关 FileNotFoundError/PermissionError（5.16.7 已改用 `/Library/Application Support/XX-Net/mac_helper`）、`check_update ... fail`（纯网络环境）。

### 源码级修复（已实测验证）

**1. `start`：探测真实加载能力后再打印**

```sh
if ${PYTHON} -c "import sys; sys.path.insert(0, 'code/${VERSION}/lib/darwin'); import boringssl" 2> /dev/null; then
  echo "Using system python, BoringSSL (abi3) is available."
else
  echo "Using system python, TLS will fall back to system SSL instead of BoringSSL."
  echo "  (put boringssl.abi3.so into code/${VERSION}/lib/darwin/ to enable it)"
fi
```

同时 PyObjC/Pillow 缺失时改为先试用户级安装（`pip install --user --break-system-packages`，兼容 PEP 668），不再一上来就 `sudo pip3`。

**2. `web_control.py`：噪音降级 + 竞态修复**

- 两处 `xlog.warn("request %s no module in path")` → `xlog.debug(...)`（模块未启用属正常条件）
- `req_debug_handler` 改为先取快照再用 `.get()` 查帧：

```python
current_frames = sys._current_frames()
for thread in threading.enumerate():
    stack = current_frames.get(thread.ident)
    if stack is None:
        continue
```

### 验证结果

- `sh -n start` / `py_compile web_control.py` 通过
- 启动输出变为 `Using system python, BoringSSL (abi3) is available.`；运行日志仍为 `TLS implementation: BoringSSL`
- 代理 generate_204 返回 204；/debug 页返回 200 且不再产生 KeyError
- 手动请求未启用模块的端点返回 404，warning log 零新增（历史上每次访问都会 +2）

注：以上 3 处补丁直接修在本机 `/Applications/XX-Net`；交付包里的 `XX-Net-5.16.7.zip` 仍是官方原包，建议作者将补丁合入官方仓库后随官方包发布。

---

## 后续：根修——按模块启用状态过滤轮询（2026-10-26）

### 现状

上一章把 web_control.py 两处 `no module in path` WARN 降为 DEBUG 只是治标：轮询者本身仍无条件请求未启用模块的端点。本机 gae_proxy 未启用（x_tunnel、smart_router 启用），存在两个无条件轮询者：

- **x_tunnel 进程内**：`upload_logs.py` 的 `collect_debug_and_log()` 硬编码 14 个 debug/log URL，其中含 gae_proxy 两条，对未启用模块每次收集必然 404。
- **浏览器端**：`status.html` 三处硬编码 `GET /module/gae_proxy/control/status`（OneKeyReport 按钮、updateDetailStatus、updateNoobStatus，后者由 1 秒定时器驱动），gae_proxy 未启用时每秒一次 404。

启用状态来源：`GET /config?cmd=get_config` 返回扁平 JSON，含 `gae_proxy_enable` / `x_tunnel_enable` / `smart_router_enable`（实测 `0, 1, 1`）。跨进程统一走该 API（`module_init.proc_handler` 仅 launcher 进程内可用）。

### 改动 1：`code/default/x_tunnel/local/upload_logs.py`

- 新增 `get_module_enable_flags(port)`：拉取 get_config（timeout=1，复用 simple_http_client），返回三模块布尔 dict；任一 key 缺失、网络失败或解析异常时返回 `None`。
- `collect_debug_and_log()` 构建 debug_infos 后按标志删除未启用模块条目：gae_proxy → `gae_info`/`gae_log`；x_tunnel → `xtunnel_*` 及 cloudflare/tls/seley front 共 9 条；smart_router → `smartroute_log`。launcher 自身 `/debug`、`/log`（`system_info`/`launcher_log`）无条件保留。
- **失败安全**：flags 为 `None`（拉取/解析失败）时保持原行为轮询全部，不因新逻辑让诊断收集整体失效。
- 已知影响：被过滤模块在 `data/downloads/*.txt` 保留上次内容不刷新（诊断 zip 中为旧文件；模块未启用时本无新日志，collect 又非高频路径，可接受）。

### 改动 2：`code/default/launcher/web_ui/status.html`

- 新增 `window.gaeEnabled/xtunnelEnabled/smartRouterEnabled`（默认 true：配置拉取失败时保持原轮询行为）；`loadModuleEnableFlags()` 在 documentReadyToRun 回调开头调 `GET /config?cmd=get_config`。
- `updateDetailStatus()`：`!gaeEnabled` 时跳过轮询，detail 表 Status 行显示 `disable` 占位。
- `updateNoobStatus()`：`!gaeEnabled` 时直接跳过轮询，`#noob-info` 保持不动（倾向不更新）。
- `OneKeyReport()`：未启用时在诊断文本框显示中性提示 `gae_proxy is not enabled, diagnostic info is not available.`，不再发必然 404 的请求。
- 模块启用但启动失败仍走原 error 分支（`failed getting started` 提示未动）；新增文案均按 Django 模板 `{{ _( ) }}` 语法包裹，模板未破坏。

### 可选改动 3（未做）

web_control.py 两处 `no module in path` 维持 DEBUG 作为纵深防御（噪音源消失后不会触发）。若需恢复 WARN：仅改 191-193 与 268-271 两处 `xlog.debug` → `xlog.warn`（273-276 行 `start fail` 的 WARN 一直未动，勿碰）。

### 验证结果

- `python3 -m py_compile upload_logs.py` 通过；渲染后的 status.html 内联 JS（12 段合并）`node --check` 通过。
- launcher 经 LaunchAgent 自动重启（新进程 17:35:28 启动，晚于代码落盘时间）加载新代码，`get_config` 接口恢复正常。
- 触发 `GET /log_files` 收集：13 个输出文件中 11 个 mtime 刷新为当前时间，仅 `gae_info.txt`/`gae_log.txt` 保持旧 mtime——未启用模块不再被轮询。
- `launcher_warning.log` 最后一条 `no module in path` 停留在重启前（12:23 历史条目），重启并触发收集后零新增。
- `http://127.0.0.1:8085/status.html` 与 `/?module=launcher&menu=status` 返回 200，渲染产物含新逻辑；代理回归 `curl -x 127.0.0.1:8086 google.com/generate_204` 返回 204，TLS 实现仍为 BoringSSL。

### 局限

- 改动仅存在于本机 `/Applications/XX-Net`（5.16.7 实装目录），交付包 zip 仍为官方原包；根修逻辑（调用方先查启用状态）建议上游合并，优于 log 降级方案。
- 启用状态是“点快照”语义：收集器每次收集前查一次，页面加载时查一次；页面开着期间切换模块启用状态需刷新页面才生效。

## 日志高频告警分析与四项修复（2026-10-26 晚）

### 背景

对 `data/*/**_warning.log` 做频率统计（数字归一化后排序去重），识别出 4 类高频问题。修复前完整备份于 `~/Downloads/XX-Net-backup-20261026-181910/`（code.tar.zip + data.tar.zip）。

### 问题清单（按优先级）

1. **高｜DoT AttributeError**（smart_router，20 次）：`DnsOverTlsQuery.connect` 失败路径上 `s` 为 None 仍调 `getsockopt`。
2. **高｜try_add fd 竞态**（smart_router，~13 次 ERROR）：socket 关闭后 selector register 报 `ValueError(-1)` / `KeyError(already registered)`。
3. **中｜IPv6 No route to host**（tls_relay，1,431 次 `_connect_ssl` 失败 + 965 次 `not enough ip`）：本机无全球单播 IPv6（仅 link-local + utun 默认路由），而服务器下发的 `relay_host.json` 中 6/14 为 IPv6 地址；seley 的 `getaddrinfo(AF_UNSPEC)` 也可能解析出不可达 v6。
4. **中｜BlockingIOError 日志噪音**（smart_router，433 次/10 秒突发，DingTalk 流量）：非阻塞 send 缓冲满属良性（数据进缓冲队列不丢失），纯日志噪音。

### 改动 1：DoT socket 判空（`smart_router/local/dns_query.py`）

`DnsOverTlsQuery.connect`：初始化 `s = None`；`super().connect()` 后 `if not s: return None`；异常路径关闭半开 socket 再返回 None。消除 NoneType `getsockopt`，顺带修复 socket 泄漏。

### 改动 2：try_add fd 竞态（`smart_router/local/pipe_socks.py`）

`try_add`：register 前先 `fileno() < 0` 预检（OSError/ValueError 均按已关闭处理）；新增 `except ValueError`（fd 在预检与 register 之间关闭的竞态）与 `except KeyError`（并发路径重复注册）两个 DEBUG 级处理器。数据转发逻辑未动。

### 改动 3：无 IPv6 时前端池过滤 IPv6（x_tunnel）

- `tls_relay_front/ip_manager.py` 新增模块级 `ipv6_available()`：UDP connect `2001:4860:4860::8888` 探测，取本地地址须为全球单播（2xxx:/3xxx:，排除 fe80::/::1/fc00::/7），结果缓存 300 秒。
- `get_ip_sni_host()` 迭代 host_manager 时跳过 IPv6 key（key 为裸 IP，无端口，`":" in ip` 判定安全）。
- `seley_front/ip_manager.py` 复用同一探测（`from ..tls_relay_front.ip_manager import ipv6_available`），在 `_get_ip_dict` 中：`getaddrinfo` 结果跳过 v6 答案（优先 IPv4）、IPv6 字面量直接拒绝；`host` 经 `utils.to_str()` 规范化（`get_ip_port` 返回 bytes，直接 `":" in host` 会 TypeError——首次实现踩坑并已修复）。
- 两处均支持 front 配置覆盖：`"use_ipv6": "force_ipv6"` 强制保留 v6、`"force_ipv4"` 强制过滤，默认 `auto`（探测决定）。探测失败按无 IPv6 处理（fail-safe 向低走）。

### 改动 4：BlockingIOError 日志限流（`smart_router/local/pipe_socks.py`）

新增 `warn_send_block(s, e)`：同一 socket 每秒最多 1 条，窗口内抑制的条数聚合为 `(suppressed N)` 后缀；字典超 512 条目时清理 60 秒前的旧键。三处 warn 点（split-SNI flush、直发、写循环）统一走该助手。**只限日志，不改 send/重试/缓冲逻辑**——数据路径零影响。

### 验证结果

- 4 个改动文件 `py_compile` 通过；独立冒烟测试确认 `ipv6_available()` 本机返回 False、seley 相对导入共享探测函数、force_ipv4/force_ipv6 覆盖生效。
- launcher 经 LaunchAgent 两次重启（18:37、18:43）加载新代码；第二次重启后：
  - `get_config` HTTP 200；代理回归 `curl -x 127.0.0.1:8086 http://www.google.com/generate_204` → 204。
  - tls_relay：重启后 3 次建连全部 IPv4（167.179.91.205 / 167.179.113.207 / 45.76.215.135），0 次 IPv6 连接尝试，warning 日志零 `No route to host`、零 `not enough ip`。
  - smart_router：零 AttributeError、零 try_add ERROR；BlockingIOError 由 61 条/秒突发降为 15-18 秒 1 条（含 suppressed 聚合）。
  - seley：首版实现的 bytes/str TypeError（1,499 次）修复后归零；剩余 handshake timeout 为远端主机网络质量所致（与改动前同类，非回归）。

### 局限与说明

- IPv6 探测缓存 5 分钟：期间本机接入 IPv6 网络需等缓存过期（或重启）才会恢复使用 v6 前端；服务器每轮下发 `set_ips` 会重建 host 池，无需持久化过滤状态。
- `not enough ip` 不会完全消失：若 IPv4 前端也全部失败（如代理环境网络差），仍会周期性出现——这是资源耗尽的真实信号，保留是合理的。
- seley 自身的 TLS handshake timeout（随机 SNI 的 198.18.x 假地址探测失败）属前端池正常淘汰机制，不在本次范围。
- 改动仅在本机实装目录，建议上游合并：DoT 判空、try_add 竞态处理、`ipv6_available` 探测（可放 `front_base` 供各 front 复用）、BlockingIOError 限流。

# 质检与边界完善（2026-10-26 晚）
对上一章 4 项修复做全面质检与边界加固。质检项 A1-A7、改动项 B1-B6、离线测试 4 套全部通过。测试脚本存于 `~/Downloads/XX-Net-qc-tests-20261026/`，均以 stub 包 + 按文件加载方式离线运行，不触碰运行中进程。

## A1 实证结论：wrap_socket 失败的 fd 语义
`probe_wrap_socket_fd.py` 在系统 python3（CPython 3.15，macOS）实测：本地明文 TCP server 逼 TLS 握手失败（ConnectionResetError[54]），失败后 raw socket `fileno()==-1`、`_closed=True`、`os.fstat(原fd)` EBADF、事后 `s.close()` 幂等成功。
结论：**ssl 层在 wrap_socket 失败时已代为关闭/脱离 fd**，Bug 4 修复中的 `s.close()` 在该路径是无害幂等 no-op，代码保留，注释补充实证依据（防止后人误删或误判双关闭风险）。

## 改动清单（B1-B6）
- **B1**（dns_query.py DnsOverTlsQuery.connect）：按 A1 结论保留 close，注释记录实证。
- **B2**（dns_query.py DnsOverTcpQuery 基类，均为既有隐患非本次引入）：
  - `direct_connect` 的 `except Exception`：补关闭已创建的 s（防 fd 泄漏）；补齐 warn 日志缺失的第 4 个参数 `e`（原 `%r` 占位符无参，触发即 TypeError）。
  - 代理路径 `connect`：`sock.connect` 包 try/except，失败时 close 后原样 re-raise（对外异常语义不变，消除泄漏）。
- **B3**（pipe_socks.py try_remove）：比照 try_add，ValueError（fd 已关）/KeyError（未注册）降为 DEBUG 并注释竞态语义；通用 `except Exception` 保留全量报警。历史 0 次触发，预防性。
- **A4**（只读确认）：try_add 的 modify/register 与 fileno 预检在同一 try 块内，ValueError/KeyError 处理器天然覆盖，无需改动。
- **B4**（tls_relay ip_manager.py ipv6_available）：移除 `or local == "::"`——未指定源地址判不可用（此前误判可用）；注释说明并发双探测无害、不加锁。seley 经相对导入自动继承。
- **B5**（tls_relay get_ip_sni_host）：IPv6 过滤发生时每 5 分钟探测周期记一条 INFO「skipped N IPv6 fronts (no usable local IPv6)」，静默过滤可观测且低噪。
- **B6**（pipe_socks.py close）：入口处 `pop(id(s1))`，挂账 suppressed>0 补打 `(final suppressed N)` 后清账——连接关闭时限流账本必然闭环；注释单线程不变量（warn/close 均在 pipe 线程）与 id 复用已知边界（新连接可能继承旧账，影响首行时点 ±1 秒窗口，接受）。

## 测试结果（全部通过）
- `test_dns_dot.py`：DoT connect 三路径（基类 None / 基类异常 / wrap 失败）；wrap 失败时 raw socket 恰好 close 一次；direct 路径非 socket.error 异常 → close 一次 + 日志参数齐全（修复前会 TypeError）；代理路径 connect 失败 → close 一次 + OSError 原样上抛。
- `test_try_add_remove_races.py`：closed socket 预检跳过（fileno -1 / OSError）；register 间 fd 失效 ValueError → DEBUG；重复注册 KeyError → DEBUG；try_remove 对称场景（含 macOS kqueue 语义实证：已注册后关闭的 fd unregister 吞 OSError 不抛 ValueError，真实 ValueError 场景为已关闭且未注册）；未知异常仍 ERROR。A4 结构静态断言（modify/register 同 try 块）。
- `test_ipv6_filter.py`：探测语义 6 例（:: 拒绝〔B4 修复点〕/ 2001::/2606:: 接受 / fe80、::1、fc00 拒绝 / 异常 → False / 缓存生效）；tls_relay 混合池只返回 v4 + B5 INFO 每 5 分钟恰一条；全 v6 池返回 None 且无 INFO（由 not enough ip 兜底）；seley 三路径 + force_ipv4/force_ipv6 覆盖 + bytes host 回归（零 bytes-like TypeError）。
- `test_blockio_ratelimit.py`：首行立即、同窗口累计、跨秒结算（suppressed 2）；close 补打 final 行并清账；无挂账 close 无 final 行；600 条触发 512 剪枝且活跃条目保留；键维度为 id(s) 每连接独立配额。
- py_compile ×4 通过。

## 既有隐患清单（本次顺手修复）
1. `direct_connect` warn 日志缺参数（触发即 TypeError，幸被外层 except Exception 兜住，但异常本身被吞）。
2. `direct_connect` except Exception 路径 fd 泄漏。
3. 基类代理路径 connect 失败 fd 泄漏。
三者与 Bug 4 同文件同模式，均为既有代码，非 4 项修复引入。

## 已知残留边界（评估后接受，不做改造）
- 限流键 `id(s)`：socket GC 后 id 可被复用，新连接可能继承旧账（首行时点/计数 ±1 秒窗口）。弱引用改造收益不抵复杂度。
- `ipv6_available` 无锁：并发双探测无害（两次 UDP connect，缓存最终一致）。
- 探测缓存 300 秒：期间接入 IPv6 网络需等缓存过期。
- 全 IPv6 前端池：`not enough ip` 周期出现，是真实的无可用前端信号，保留。
- macOS kqueue 已注册后关闭的 fd：unregister 不抛 ValueError（内核已清理），DEBUG 分支主要覆盖 Linux epoll 及未注册场景。
- **D 阶段预期差异**：实测本机 auto 探测在部分网络下返回 True（存在全局 v6 源），则 tls_relay 会继续尝试 v6 前端——过滤只在「确实无 v6」时生效，属正确行为而非回归。

## 上线状态
代码已全部就位并通过离线测试；重启 launcher 后回归与审计已完成，见下章。

# D 阶段：重启上线与回归审计（2026-10-27 晨）
## 进程与回归
- 用户手动重启：launcher PID 55155，本地 2026-10-27 05:50:10（data/*/start_log_*_2026-10-27_05-50-10.log 系列为重启后新日志）。
- 代理回归 `curl -x 127.0.0.1:8086 http://www.google.com/generate_204` → **204 通过**。
- Web 控制台 `http://127.0.0.1:8085/` → 200。注意：`get_config` 无独立路径，正确调用为 `GET /config?cmd=get_config`（见前文 status.html 章节）；直接访问 `/get_config` 返回 404 属路径差异，非故障。
## 日志审计（窗口：本地 2026-10-27 ≥05:50，grep 计数，未整读大日志）
- **smart_router_warning.log**：零 AttributeError（Bug 4 ✅）；零 `try_add/try_remove e:` ERROR（Bug 5/B3 ✅）；零 BlockingIOError、零 final suppressed（Bug 6 ✅——重启后无 send 阻塞发生，限流账本为空属正常）。
- **tls_relay_warning.log**：零 `No route to host`；v4/v6 前端均有成功建立并处理流量的连接（v6 close 行 processed:2），与「D 阶段预期差异」一致——本机存在全球 v6 源时 auto 放行，过滤仅在无 v6 时生效。B5 skipped INFO 零条 = 符合设计（skip 计数为 0 时不打日志）。
- **seley_front_warning.log**：重启后零 ERROR/TypeError。当日早些的 29 条 `http_dispatcher %b NoneType` TypeError 全部发生在 03:57（旧进程期），属本轮范围外的既有遗留。
- **cloudflare_front_warning.log**：仅 1 条 `not enough ip` 与常规连接生命周期 close，无新问题。
## 新发现（遗留，低优先级，本轮未改动路径）
- smart_router 重启后 64 条 `send_request except:OSError(65, 'No route to host')`（来源 `dns_query.py:270`，UDP 本地 DNS 转发的 send_request）。根因：`/etc/resolv.conf` 含 link-local IPv6 DNS `fe80::4ec6:4cff:fe4f:ae64%en0`，sendto 裸 v6 地址无 scope/路由 → 65。05:50-05:56 集中 64 条后降为约 3 分钟 1 条。环境性遗留，与四项修复无关；其他 DNS/DoT 兜底，无功能影响。可选后续：send_request 失败一次后暂禁该 server 本轮重试，或加载时过滤 link-local nameserver。
## 上线判定
QC 计划 A1-A7/B1-B6/C/E/D 全部完成；四项修复 + 质检加固在生产环境零回归。

# 会话交接与新会话引导（2026-10-27）
背景：本会话历史极度膨胀，多次陷入压缩-失败循环。结论：**新会话不读任何旧会话历史**，只靠以下三份工件恢复上下文。
## 三份权威工件
1. 本文档 `~/Downloads/XX-Net-macOS-ARM64-修复记录.md`：完整修复史（本次全部章节）；新会话先读目录与末三章即可。
2. `~/Downloads/XX-Net-qc-tests-20261026/`：probe_wrap_socket_fd.py + 4 个可重跑离线测试（stub 包方式，不触运行中进程）。
3. `~/Downloads/XX-Net-backup-20261026-181910/`：改动前完整备份（code.tar.zip + data.tar.zip），回滚用。
## 当前系统状态快照
- XX-Net 5.16.7 于 `/Applications/XX-Net`；2026-10-27 12:47 重启（PID 27738）运行中，四项修复 + DNS nameserver 过滤（E 阶段）+ B1-B6 已生效，审计零回归。
- 累计改动文件仅 3 个：`smart_router/local/dns_query.py`、`smart_router/local/pipe_socks.py`、`x_tunnel/local/tls_relay_front/ip_manager.py`（seley 经相对导入继承）。
- 已知遗留（均已评估）：① seley http_dispatcher `%b NoneType` TypeError——已定界为旧进程期边界遗留（03:57 server is down → host=None，重启后零新增，仅文档记录，见 E 阶段）；② resolv.conf fe80:: DNS → `send_request OSError(65)`——已修复（E 阶段 nameserver 过滤）；③ `get_config` 正确路径为 `/config?cmd=get_config`。
## 新会话引导 prompt（首条消息直接粘贴，尖括号内替换）
```
继续维护 /Applications/XX-Net（macOS ARM64，XX-Net 5.16.7）。全部背景与历史见
~/Downloads/XX-Net-macOS-ARM64-修复记录.md（先读目录与末三章），离线测试在
~/Downloads/XX-Net-qc-tests-20261026/，回滚备份在 ~/Downloads/XX-Net-backup-20261026-181910/。
当前状态：2026-10-27 12:47 重启后，四项修复 + DNS nameserver 过滤（E 阶段）+ 质检 B1-B6 已上线且回归审计零回归（见文档末三章）。
请勿读取任何旧会话历史，以该文档为唯一真相源。本次任务：<新任务描述>
```
## 经验教训（防压缩循环复发）
- 不整读大文件/大日志：一律 `grep -c` 计数 + 小窗口行读取；本机 shell 的 `ls` 为 eza 别名，脚本内用 `/bin/ls`。
- 工具结果 >4KB 会被摘要：关键数据应在下一步立即落盘到本文档，不要依赖留在会话历史里。
- 长任务跨会话：文档是唯一真相源，会话只做执行不做存储；单会话工作量大时主动建议提前收尾移交。
# E 阶段：日志收敛专项（2026-10-27 午）
本轮任务「XX-Net完善重构-日志收敛质检」。审计窗口 2026-10-27 05:50-06:xx（05:50 重启后）：五份日志 ERROR 全零，四项修复 + B1-B6 零回归。唯一高频噪音为 smart_router `send_request except:OSError(65)`：窗口合计 120 条（05:50 突发 51 条后降为约 1-7 条/分钟，06:15 小突峰 18 条），占该窗口 warning 日志 100%。launcher_warning 仅 2 条网络环境性（raw.githubusercontent.com 不可达，与既有结论一致）；tls_relay 7 条 `not enough ip`（真实信号，保留）+ IPv6 前端正常使用（20 条 [2401:...] close、多条 processed:2，与 D 阶段预期差异一致，实际 v4 前端 209.33.166.19/108.160.139.248/45.76.178.142 等）；cloudflare_front 1 条 not enough ip + 常规 close；x_tunnel/seley 窗口零行。
## seley %b NoneType 遗留根因（只定界，未改代码）
- 触发点：`lib/noarch/front_base/http_dispatcher.py:350` `url = b"%s %s%s" % (method, host, path)`，host=None（bytes % 格式化参数为 None → "format argument 2: %b requires a bytes-like object, not NoneType"，已本机复现机制）。
- 链路：x_tunnel 服务器 504 后 `proxy_session.py:950` 置 `g.server_host = None`，`front_dispatcher.py:186` 以 host=None 传入各 front。
- 实证：x_tunnel_warning.log（点分毫秒时间戳）03:57:01.691/697 两条 `X-tunnel server is down`，01.702 起大量 `front retry None/data?tid=15198`——host=None 直接见于重试日志，同源同刻。
- 分布定界：253,744 条 18:37-18:43（旧进程期 bytes-like）、20 条 22:54-22:56（`http_dispatcher.py:505` connection_checker NoneType）、29 条 03:57（%b NoneType）——全部旧进程窗口，05:50 重启后零新增。判定：边界遗留随重启自愈，仅文档记录；若未来根治应在 `front_dispatcher.request` 加 host=None 防御，不动数据路径。
- 顺带记录（未修）：`http1.py pack_headers`（9-17 行）`b'%s: %s\r\n' % (utils.to_bytes(k), utils.to_bytes(v))` 在 header 值为 None 时报同款错误（`utils.to_bytes(None)` 原样返回 None，`utils.py:315-316`）；当前无调用方传 None header。
## 改动：dns_query.py nameserver 过滤（本轮唯一代码改动）
- 新增 `LocalDnsQuery._is_usable_nameserver()` staticmethod：放行 IPv4（除 127.0.0.1）与全球单播 IPv6（2000::/3，即 2/3 开头，同 B4 `ipv6_available` 语义）；拒绝 fe80::/10、fc00::/7（含 fd00::/8）、::1、ff00::/8；str 输入自动转 bytes。
- `get_local_dns_server()`：过滤循环前移至公共 DNS 兔底之前（fail-safe）——resolv.conf 全被滤空时落入 CN/通用公共 DNS 列表；剔除动作记 INFO `skip unusable DNS server:<ip>`。send_request 数据路径未动。
## 验证
- py_compile 通过。离线测试第 5 套 `test_dns_nameserver_filter.py`（stub + mock resolv.conf）：语义表（6 keep + 7 drop，含本机真实噪音源 fe80::4ec6:...%en0）、本机实际形态（fe80 + 内网 v4 → 只留 v4 不触发兔底）、仅 fe80 → CN 兔底、非 CN → 通用兔底、全球单播 v6 混合保序、127.0.0.1 仍剔除、resolv.conf 缺失兔底、local_ips 剔除交互兔底——全部通过。
## 重启与回归（2026-10-27 12:47）
- 用户手动重启：launcher PID 27738（12:47:40 启动）。
- 启动日志实证新逻辑生效：start_log_smart_router_2026-10-27_12-47-40.log 内 `skip unusable DNS server:fe80::1%en0` + `Local DNS server:192.168.1.1`（注意：本轮 fe80 地址为 fe80::1%en0，与 10-26 观察的 fe80::4ec6:... 不同——link-local 随网络会话变化，过滤对任意 fe80::/10 均生效）。
- 回归审计（窗口 12:47-12:5x）：smart_router warning 207 行，`send_request except` 零新增（此前每窗口 120 条），零 ERROR；其余为既有良性（reserv frag 147 条、tosv.byted.org DoT/DoH 查询失败兜底链路）。x_tunnel 21 行/零 ERROR；launcher/seley/cloudflare 窗口零行。
- 代理回归 `curl -x 127.0.0.1:8086 http://www.google.com/generate_204` → **204（0.4s）**，DNS 解析正常。
## 上线判定
五项修复（四项 + 本轮 nameserver 过滤）+ B1-B6 全部在线，零回归。累计改动文件仍 3 个：`smart_router/local/dns_query.py`、`smart_router/local/pipe_socks.py`、`x_tunnel/local/tls_relay_front/ip_manager.py`（seley 经相对导入继承）。离线测试共 5 套存于 `~/Downloads/XX-Net-qc-tests-20261026/`。
# F 阶段：日志治理（2026-10-27 晚—10-28 午）
## T1 历史日志归档与止损
- 全量归档 7 份 warning 日志（共约 237MB）到 `backups/logs-20261027/`，打 zip 入库 commit `7738967`（.gitignore 仅排除 `*.log`，zip/gz 可入库）。
- seley_front_warning.log 225,444,543B / 3,564,091 行，gzip -9 后 2,353,574B（约 1% 压缩率）；归档前 cmp 字节级校验一致。
- 对运行进程（launcher PID 27738，12:47 启动，lsof 确认持全部 7 个 warning fd，O_APPEND）持有的 5 个热日志 cp 后 `: >` truncate：seley/tls_relay/smart_router/x_tunnel/cloudflare_front 归零；truncate 后 3 秒复核 fd 继续写入（smart_router 870B、tls_relay 856B），O_APPEND 止损安全成立。launcher/gae_proxy 小日志仅归档不 truncate。
- 路径勘误：warning 日志实际在各模块子目录 `data/<module>/` 下；`connect_manager.py` 实际位于 `lib/noarch/front_base/`（计划写的 tls_relay_front 下不存在），行号 :299 与计划一致。
## T2 补充分析：真问题清单（三分类）
### 程序 bug（残留，低优先级）
1. seley `trace:` 行主源 `front_base/http_dispatcher.py:400` `retry_task_cb` 任务轨迹 WARN（POST ny3.xx-net.org/ 主链路任务悬挂约 30 秒后重试，trace 显示 get_worker 耗时）；次源 `http1.py:287-288` keep-alive 探测失败 WARN+trace 对。重试/探测机制本身逻辑正常，属告警洪水而非逻辑 bug——本次以节流处置（T3）。
2. `xlog.py:385` 顶层 `warning()` 笔误调用 `warnin`（官方 5.16.7 既有），当前无调用方触达，未修（超本轮范围，建议上游同步修复）。
3. tls_relay `_connect_ssl` 对 `*.distravia.com` SNI 前端的 Timeout/OSError(65) 告警：前端池正常淘汰机制，日志量与远端健康度成正比，未改（not enough ip 节流已覆盖主要洪水）。
### 环境现象（非 bug）
4. smart_router `send_request except:OSError(65/51)` 合计 4,771 条（65×900、51×3,871）：10-26 20:00-23:00 占 73%（3,507 条，当晚网络切换窗口），10-27 起降为 70-273 条/小时稳态背景噪声。均为 UDP 本地 DNS 转发 sendto 不可达 nameserver 失败，DNS 兜底链路正常工作。定性：环境网络波动，非 bug；与 E 阶段 fe80 过滤修复兼容。
### 上游服务波动（不在修复范围）
5. tls_relay `not enough ip` 4,404 条/约 33 小时：时段聚集明显——10-26 13:00-15:00（821）与 19:00-10-27 01:00（1,624）两个高发窗；10-27 08:00-11:00 近乎消失（5-9 条/小时）；16:00-18:00 再聚集（905）。定性：上游 front 池健康度波动（服务器下发 IP 池质量+远端可达性），本地代码无法根治；每线程每循环打一条的洪水部分由 T3 节流处置（信号保留、速率限制）。
6. x_tunnel `get_front fail` 21,035 条（`front_dispatcher.py:167`）：与 not enough ip 同源（front 池耗尽的上层表现），属上游波动链路，语义保留未节流（量级小于 seley）。
7. cloudflare_front：`not enough ip` 766 条 + `get ip for www.{hostraise,haldersonmarine}.com fail:gaierror(8)` 1,226 条（本机 DNS 解析失败）+ ConnectionReset close（正常网络现象）。gaierror 为 DNS 环境问题，非 bug。
### 结论
本轮无需修复级变更的真 bug 为零；三类洪水型告警（reserved/frag、not enough ip、retry/HEAD trace）按定性分别降噪/节流；OSError(65/51) 与上游波动项仅记录监测。
## T3 日志机制改造（5 文件，均在 Coding/XX-Net-5.16.7.1 快照内）
1. **`lib/noarch/xlog.py`**：
   - warning_log 10MB 单份滚动：新增 `_roll_warning_log_if_needed()`（沿 roll_log 模式，仅保留一代 `.1`，磁盘占用上限约 2×10MB）；挂点四处：`__init__` 开文件后、`reset_log_files()` 重开后、`log()` 写入路径（buffer_lock 内、写前检查）；roll 失败静默保持写路径存活（与主 log 异常风格一致）。
   - 新增 `LogThrottle` 工具类：`allow(key) -> (allowed, suppressed_count)`，带锁、512 键过期剪枝（同 pipe_socks warn_send_block 策略），供多线程调用方复用。
2. **`smart_router/local/dns_server.py:161-166`**：reserved/frag `xlog.warn` → `xlog.debug`（附依据注释：14,897 条/天的包字段检查非运行故障；DEBUG 仍进 start_log 可查）。
3. **`lib/noarch/front_base/connect_manager.py`**：`not enough ip` 加 60s 节流（沿用既有 no_ip_lock，串行语义不变；新增 `last_no_ip_warn`/`suppressed_no_ip_warn` 属性；`no_ip_time` 赋值语义保持，`_need_more_ip` 轮询行为不变）；输出行附 `(suppressing for 60s after this line)` 标记；窗口内抑制次数入计数器。
4. **`lib/noarch/front_base/http_dispatcher.py`**：`retry_task_cb` WARN 按 task.url 60s 节流（`LogThrottle(60)` 实例 `retry_log_throttle`；首条立即打、窗口内计数、下一条允许时附 `(suppressed N)`）；重试状态机零改动。
5. **`lib/noarch/front_base/http1.py`**：`head_request` 探测失败 WARN+trace 对按 worker 60s 节流（`last_head_fail_warn`/`suppressed_head_fail_warn` 属性；warn/close 仅在该 worker 自己的 work_loop 线程调用，无需加锁）；trace 行扩展 `(suppressed N)` 后缀；探测/recheck/close 逻辑零改动。send/recv 主链路 trace 对未动（诊断价值高、量级低）。
## T3 离线验证（全部通过，未触运行进程）
- `python3 -m py_compile` ×5 改动文件通过。
- 新增第 7 套离线测试 `Coding/XX-Net-5.16.7.1/test_xlog_roll_f.py`（stub 按文件加载，同既有风格）：roll 行为 4 项（未超限不滚、超限滚出 `.1` 且主文件重开变小、滚动后写入继续、二次滚动 `.1` 单代替换无 `.2`）+ LogThrottle 语义 6 项（首条放行/窗口抑制/计数累计/键隔离/窗口过期/并发 512 剪枝）= 10/10 PASS。
- 仓库既有 6 套测试回归：test_dns_dot / test_try_add_remove_races / test_ipv6_filter / test_blockio_ratelimit / test_dns_nameserver_filter 全部 ALL PASSED；probe_wrap_socket_fd 结论不变（A detached）。
## 上线状态
- rsync 到 /Applications/XX-Net：覆盖前 diff 备份于 `backups/pre-rsync-20261027/`（5 份 `.orig` + 5 份 `.diff`，diff 行数 121/14/35/42/37 与改动一致），rsync 后逐文件 diff 校验与快照完全一致。
- launcher 未由本任务重启；用户于 2026-10-28 10:44:04 亲自重启（新 PID 14268，启动时刻晚于 rsync 完成 10-27 21:16 → 新代码确认加载；`nc -z 127.0.0.1 8086` 端口正常，代理回归见下）。
## T4 重启后审计窗口（2026-10-28 10:44-11:19，约 35 分钟）
基线勘误：T1 truncate 后旧进程（PID 27738）持续写至重启（约 22 小时）曾污染初始基线；最终统计全部按时间戳切分（`awk '$1=="2026-10-28" && $2>="10:44:00"'`），下述新窗口数字不含旧进程输出。旧窗口对照 = 旧代码 10-27 12:47→10-28 10:44（约 22 小时）。
### 实测 vs 预期
- **代理功能**：`curl -x 127.0.0.1:8086 http://www.google.com/generate_204` → **204（0.37s）**。
- **seley_front**（总行 4,972 → 112）：`retry_task_cb` trace 846 → 3（-99.6%；3 条均为 /ping 任务、间隔 ≥97 秒，60s 节流正确生效）；`not enough ip` 513（0.39 条/分）→ 6（0.17 条/分），全部带 `(suppressing for 60s after this line)` 标记；HEAD 探测失败 186（0.14 条/分）→ 14（0.40 条/分，10:55/11:12/11:19 三簇）——未降反升，定性见下节。
- **tls_relay**（总行 5,536 → 146）：`not enough ip` 2,066（1.56 条/分）→ 7（0.20 条/分）全部带抑制标记，≤1 条/分达标；其余为 `_connect_ssl` 超时（distravia 病态前端）与连接 close 生命周期行（真实信号，T2 定性 3，未节流）。
- **smart_router**（总行 32,652 → 9）：`reserved/frag` 32,596 → **0**（降 debug 生效）；余 9 行为 1.1.1.1 DoT/DoH 查询失败兜底链路（良性网络环境）；尾行 11:11:54 后静默。
- **x_tunnel**：`get_front fail`（T2 归档窗口 21,035 条/33h）→ 新窗口 **0**；58 行均为 `roundtrip status:602` 超时告警（真实信号，本轮范围外）。
- **cloudflare_front**：`not enough ip` 57 → 1（带标记）；总 11 行（task timeout 真实信号）。
- **launcher**：ERROR 12 → **0**；2 行 check_update 网络环境告警（既有已知）。
- **零 ERROR 回归**：新窗口 7 份日志 ERROR 合计 **0**。
### HEAD 未降的定性（如实记录，非回归）
- 代码核实：`head_request()` 失败后 worker 立即 `close("keep alive")`（`http1.py:112-115`），每个 worker 生命周期至多产生 1 条失败告警——60s 节流的抑制分支在生产中结构性不可达（全窗口 0 个 `(suppressed N)` 标记属必然结果，与旧代码单 worker 行为等价，无回归；离线测试 B 组已验证实现本身正确）。
- 数量上升源于当前 27895/cqobdcyunkzadstj 前端探针持续 recv timeout → worker 失败-重建循环，属上游服务波动（与 T2 定性 5 同源）。若未来需进一步降噪，可做前端级失败聚合（超出本轮范围）。
### 10MB 滚动与增速
- 生产未触达 10MB 阈值（seley 自 truncate 起累计 <1MB）；滚动行为由 `test_xlog_roll_f.py` 10/10 离线覆盖。
- 窗口后 20 分钟（11:19→11:39）增速：smart_router/x_tunnel/gae_proxy/launcher 均 0 B；seley +28.5KB（病态前端 connect 失败告警，真实信号）；tls_relay +5.4KB；cloudflare +2.6KB。按此速率 10MB 阈值在最坏日志上亦需数天，磁盘占用可控。
### 审计判定
seley trace 完全达标（-99.6%）；not enough ip ≤1 条/分/实例达标且全部带抑制标记；reserved/frag 归零；零 ERROR；增速受控。唯一未降项 seley HEAD 已定性为上游波动 + 结构必然，非回归非缺陷。F 阶段至此收尾：时间线回填与代码/文档随本 commit 入库。
