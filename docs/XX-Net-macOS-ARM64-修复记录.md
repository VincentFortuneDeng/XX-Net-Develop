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
# G 预研：节流机制盘点与尾账分析（2026-10-28 午，无代码改动）
背景：用户追问 F 阶段节流是否存在「旧机制（内存累计、无后续日志导致不落盘/丢失）」同款风险。本章为全库盘点与机制分析，供后续 Agent 直接接手；结论已获用户确认的改进方案见末节「待实施提案」。
## 全库节流点盘点（共 5 处 + 1 个公共工具类）
排查方法：grep throttle/suppress/ratelimit/_last_warn 等，逐文件读原文排除假阳性（`hyper/ssl_compat.py` 的 `suppress_ragged_eofs` 是 SSL 库参数；`web_ui/js/bootstrap.min.js` 的 throttle 是前端防抖，均与日志无关）。
1. **`pipe_socks.py` `warn_send_block`（47-68）+ B6 临终结算（189-198）**：1 秒窗，按 socket（`id(s)`）记账，有抑制计数，close 时强制结算。10-26 四项修复 4 + B6。
2. **`connect_manager.py` `not enough ip`（309-313）**：60 秒窗，全局唯一账本（`last_no_ip_warn`/`suppressed_no_ip_warn`）。F 阶段。
3. **`http1.py` `head_request`（291-306）**：60 秒窗，按 worker 记账（`last_head_fail_warn`/`suppressed_head_fail_warn`）。F 阶段。
4. **`http_dispatcher.py` `retry_task_cb`（411）**：60 秒窗，按 task.url 记账，用 `xlog.py:351` `LogThrottle`。F 阶段。
5. **`ip_manager.py` `get_ip_sni_host`（119-125）**：IPv6 前端跳过 INFO，按探测周期（300s）限频，**无计数器账本**（每条现算现打），不存在丢账问题。10-26 B5。
## 尾账（tail account）机制分析：B6 为何不丢 vs F 阶段三处为何会丢
核心结论：**B6 的账户（socket）有必然到来的葬礼（close），F 阶段的账户（url/全局状态）没有**。每笔抑制账有两个结算机会，谁先到谁结算：① 下一次同类事件来时随放行行结算（正常还款）；② 账户消亡时临终清算（B6 的 `(final suppressed N)`）。两条路必走其一 → 不丢账。F 阶段三处只有机会①，事件永久沉默时尾账随进程消失。
- **处 1（B6）**：严密。事实一：每个 socket 必走一次 close（正常/异常/停机 `stop()` 74-80 与 pipe 线程退出前 455-457 都会逐个 close，葬礼迟到不缺席）。事实二：`warn_send_block` 与 `close` 同在 pipe 单线程（单线程不变式，pop 无需锁）。已知边界：`id(s)` 复用 ±1 秒窗口（文档既有记录）。
- **处 4（LogThrottle）**：url 是字符串非对象，无「url 关闭」事件，沉默即尾账。改进不能抄 B6（无葬礼可用），只能脉冲结算（分期还款）。
- **处 2（not enough ip）**：有「半个葬礼」可用而 F 阶段未挂——池恢复（`get_ip_sni_host()` 重新返回非 None）即可观测，可补打「枯竭结束共抑制 N 条」（B6 的镜像）。F 阶段未做的原因：该计数器注释即声明 "currently a counter only, for diagnosis"，设计上只做内部诊断从不打印。但 grep 证实全库无读取者，按「累计即应结算」标准应补。
- **处 3（HEAD）**：有葬礼（worker.close）但账本结构性永远为空——`head_request` 失败后调用方 `http1.py:112-115` 立即 `close("keep alive")`，每 worker 生命周期至多失败 1 次，抑制分支不可达（T4 全窗口 0 个 `(suppressed N)` 实证）。加临终结算是给永远为空的账本办清算，F 阶段选择不加、记录在案。
## 两处专题图解（用户追问的通俗版）
### not enough ip：功能状态与日志账本是两套互不相干的状态
- 功能作用：`no_ip_time = now`（`_connect_process` 309-315 行所在分支）让其他连接线程在 `_connect_thread:282` 判断 `time.time() - self.no_ip_time > 10` 而歇 10 秒，避免空转烧 CPU——这是官方既有功能，与日志无关。
- 日志价值：前端池枯竭时连接线程一圈圈空转，若无此告警表现为「代理变慢但一切静默」；这行 WARN 是枯竭的唯一可见证据，T2 定性为真实信号，值得保留。
- 计数器现状：`suppressed_no_ip_warn` 只写不读（grep 全库仅定义+自增两处），无消费者，随进程消失——方案 5（恢复即结算）就是给它接上消费者。
- 时序：池枯竭期间每轮循环（约 1 秒）走一遍「get_ip_sni_host 返回 None → no_ip_time=now → 日志判定（满 60s 写盘/未满 suppressed+1）→ sleep(1)」；池恢复后循环不再进入该分支，故事结束但正字永不结算。
### HEAD worker 节流：抑制分支生产中不可达，属零成本保险
- 时序：keep_alive 定时器隔几秒发 ping → worker 调 `head_request()`（GET / 探活）→ 成功返回 True 无日志；失败进 except 分支 → 判 60s（事实上永远满足，因 worker 即将死亡）→ 写 1 条 WARN+trace → `recheck_ip` + `close("keep alive")` worker 终结 → 调度器新建 worker（新账本归零）。
- WARN 本身有用：每个探活失败的 worker 恰留 1 条真实告警（前端连不上、IP 已进复核）。T4 中 HEAD 186→14 未降系上游坏前端多、worker 频繁重建，每 worker 死一次留一条，量与上游波动成正比，与节流无关。
- 节流判定：今天不可达但无害（两个整数属性）；若未来 head_request 改为同 worker 重试多次（常见改动），保险自动生效。离线测试 B 组已验证实现正确。结论：保留并在文档标明「当前不可达」，不做删改（用户可选删除，代码极简主义同样成立）。
## 与旧机制（B6 前）的本质区别（用户原问「旧问题是否复现」的最终答案）
旧缺陷（B6 前 warn_send_block）：挂账的 suppressed 结算需未来事件偿还，账期无上限，且 `id(s)` 复用会把旧账错挂到不相干新 socket（错账，非丟账）。B6 补 close 结算后闭环。F 阶段机制：时间窗过期账目自动作废，无错账可能；最坏损失收窄为「最后窗口的量级注解」，且每窗口首行信号任何情况下已实时落盘。无内容级丢失，无内存累计。
## 待实施提案 v2（A+B 已于 2026-10-28 午后实施，原文留档）
v1 两方案经用户两轮追问后修订：核心洞察 ① not enough ip 每分钟放行行不带计数（比 LogThrottle 少一层），旱灾期间「在旱」可见但「旱多重」不可见；② HEAD 节流按 worker 记账是结构性不可达的摆设，而实证（T4 的 186→14 条均为同一坏前端被一茬茬新 worker 重复探测）表明按前端聚合才是节流的真正目标。
- **提案 A（处 2 not enough ip：补严重度 + 恢复通告）**：每分钟放行行改为携带上一窗口计数（`(suppressed N last window)`，滞后 ≤60s，同 LogThrottle 语义）；恢复分支（`get_ip_sni_host()` 成功且处于旱态）补打 `not enough ip recovered (suppressed X, total N)` 并清账。旱灾在日志中成为完整故事：开始/过程速率/尾窗/总数四要素，行数成本不变（旱灾期间仍 ≤1 条/分 + 结束 1 条）。该分支被 no_ip_lock 串行化且每圈 sleep(1)，窗口内正字理论上限约 60——这只是防洪天花板推演，非常态：实测事件率（T4 旧代码无节流基线）tls_relay 1.56 条/分、seley 0.39 条/分，典型窗口计数 0-2，详见下方「追问澄清」。约 +8 行。
- **提案 B（处 3 HEAD：按前端聚合，用户提出）**：删 per-worker 属性，改类级 `LogThrottle(60)` 实例、key= `ssl_sock.ip_str`；放行时打 warn+trace（trace 取窗口内首个失败 worker）。保证：行数数学上不增（每条放行 ≥1 次死亡，新行数=有死亡的窗口数 ≤ 死亡数）；突发场景 50 死/分 → 1 行 + suppressed 49；计数器变为活的「按前端 worker 死亡率」诊断。改约 10 行 + 测试用例更新。
- **提案 C（处 4 LogThrottle 脉冲结算）：降级为可选**。理由：每次放行行已带上一窗口计数（滞后一窗），滞后对诊断可接受；尾账损失已收窄为「沉默 key 的最后一窗计数」，实测 per-key 速率低，脉冲的边际价值小。若用户要求窗口内实时量级再实施（N=100，`xlog.py` 约 5 行）。
- 处 1（B6）完备不动；处 5 无账本不涉及。
- 实施清单（A+B 为默认范围）：改 connect_manager.py / http1.py → py_compile → `test_xlog_roll_f.py` 更新（LogThrottle 语义未变，新增/调整 HEAD 聚合用例）+ 既有 6 套回归 → 更新本文档与 `LOCAL-VERSION.md` 补丁表 → commit（快照与 /Applications 同步需 rsync + 用户重启，另行安排）。
## 提案确认与交接（2026-10-28 午后，用户拍板）
用户四项决策：① 范围 = **A+B**（C 不做，LogThrottle 维持现状）；② 提案 B **覆盖 head_request 全部三条失败出口**（`http1.py` send len 274-278 / status 283-285 / exception 291-306——现状仅 exception 出口有节流，其余两条裸 warn；聚合应完整）；③ 本轮**只改代码与测试**，rsync 部署另行安排（重启时机用户定）；④ 文档与 `LOCAL-VERSION.md` 由实施方一并更新并 commit，单次交付闭环。
边界补充（给实施方的精确锚点）：B 的类级节流实例放在 `Http1Worker`（`from xlog import LogThrottle`，同 `http_dispatcher.py:26` 风格）——http2 探活走 H2 PingFrame（`http2_connection.py:424-440`），无 HEAD 失败告警，不涉及；三出口统一后原 worker 属性 `last_head_fail_warn`/`suppressed_head_fail_warn`（`http1.py:31-32`）删除；A 的属性初始化在 `connect_manager.py:169-173`，放行分支 309-311、抑制分支 312-313，恢复判断加在 `get_ip_sni_host()` 成功路径（300-301 之后）。验收命令：`python3 -m py_compile` ×2 → `python3 Coding/XX-Net-5.16.7.1/test_xlog_roll_f.py` → 既有 6 套回归（`tests/test_dns_dot.py`、`test_try_add_remove_races.py`、`test_ipv6_filter.py`、`test_blockio_ratelimit.py`、`test_dns_nameserver_filter.py`、`tests/probe_wrap_socket_fd.py` 结论不变 A detached）。部署时 rsync 覆盖前 diff 备份至 `backups/pre-rsync-<日期>/` 模式。
## 追问澄清（第二轮，2026-10-28 午后，用户两问触发）
- **上限 ≠ 常态（<60 的计时行是常态而非例外）**：用户推导「计时行只活在旱灾期间、恢复走独立分支」正确（代码事实见下条）。「每窗口约 60 个正字」只是天花板推演：sleep(1) 在 no_ip_lock 内 → 全局每秒至多 1 次进入计时段（60/窗）。常态远低于此：连接线程碰旱即歇 ≥10 秒（`_connect_thread:282` 要求 `time.time() - self.no_ip_time > 10` 才继续干活）、keep_connection_daemon 在池量充足时每圈 sleep(5) 才调一次 `_connect_process`（`connect_manager.py:223-231`）。实测旱灾事件率（T4 旧代码无节流基线）tls_relay 1.56 条/分、seley 0.39 条/分 → 典型窗口计数 0-2。提案 A 的计时放行行 `(suppressed N last window)` 将常见 0-2，大数罕见——此前回复举「suppressed 47」属极端洪水示意，有误导性，已向用户认账修正。
- **计时行/恢复行分工（备查）**：60 秒计时行只存在于旱灾期间（首条到恢复前）；恢复前最后一个不足 60 秒的尾窗由恢复行结算（total 含尾窗）。一次 `_connect_process` 调用内两分支互斥：恢复 = `get_ip_sni_host()` 返回非 None → 走正常连接路径，根本不进入计时段（`connect_manager.py:296-318`）。
- **HEAD 与 url 节流的关系**：同一病根（坏前端/上游波动）在两层楼各报的症状，互补不可互代。retry_task_cb 按 task.url 记账（含调度器 /ping 心跳任务），回答「哪些任务地址在失败」（T4 剩余 3 条全是 /ping）；HEAD 探测没有业务 url——探测动作本身就是向前端发 GET / 探活，记账钥匙用前端 ip_str（提案 B），回答「哪条前端线路在杀 worker」。同一坏前端会同时触发两族日志；改后两族行均带 ip_str，可互相印证。
- **稀疏事件不被节流压缩（闸门语义）**：节流保证「每 key 每 60 秒至多 1 条」，不是「每小时 1 条」。每小时零星死 10 个 worker = 每次死亡到来时上一窗口已过期 → 各自开新窗放行，仍 10 条。节流压缩的是「同窗口内的重复挤兑」（50 死挤进同一分钟 → 1 行 + suppressed 49），不压缩稀疏独立事件（各值得及时上报，10 条/小时不构成洪水）。比喻：闸门每分钟放 1 人——人群挤来放 1 拦 49，零星来客每次直接过。若确需「每前端每小时 1 条」可把窗口加长到 3600s，不建议：对病态高频前端 60s 窗已封顶，加长只牺牲稀疏事件及时性、收益趋零。
## G 实施：提案 A+B 落盘（2026-10-28 午后，无部署）
范围严格按用户拍板执行：仅 A+B；C 未做，`LogThrottle` 公共语义未变；http2 未动；未 rsync、未重启任何进程。
### 改动摘要
- **提案 A（`connect_manager.py:169-173, 296-325`）**：60 秒放行行改为 `not enough ip (suppressed N last window, suppressing for 60s after this line)`，输出上一窗口计数并清零；新增本轮旱态累计计数。`get_ip_sni_host()` 成功且账本处于旱态时，在同一 `no_ip_lock` 内原子结算并打一次 `not enough ip recovered (suppressed X, total N)`，其中 X 为恢复前尾窗抑制数、N 为本轮旱态累计抑制总数（含尾窗），随后全部清账。`no_ip_time` 赋值、`_need_more_ip` 轮询、`sleep(1)` 位置与连接数据路径均未改。
- **提案 B（`http1.py:5, 21-23, 259-305`）**：删除 per-worker `last_head_fail_warn`/`suppressed_head_fail_warn`，改为 `Http1Worker.head_fail_throttle = LogThrottle(60)`，以 `ssl_sock.ip_str` 为 key 跨 worker 聚合。send len / status / exception 三条失败出口全部统一：放行时保留原 WARN 并输出 trace，trace 在有上一窗计数时附 `(suppressed N)`；未放行仅静默日志。探测、`recheck_ip`、`close`、返回值及 `request_onway` 语义未改。
- **测试（`test_xlog_roll_f.py`）**：既有滚动与 `LogThrottle` 用例原样保留；新增 C 组 6 项（HEAD 三出口、同 ip 抑制与过期结算、不同 ip 隔离、20 线程并发及计数结算）与 D 组 4 项（放行行上一窗计数、恢复尾窗+总数、恢复仅一次、账本清零）。测试按文件路径加载真实模块，仅 stub 外部依赖，不触运行进程。
### 测试结果
- `python3 -m py_compile`：`connect_manager.py`、`http1.py` 均通过。
- `python3 Coding/XX-Net-5.16.7.1/test_xlog_roll_f.py`：**23/23 PASS**（滚动 7 + LogThrottle 6 + HEAD 聚合 6 + not enough ip 恢复 4），新增用例在旧实现上 9 项 RED、实施后全部 GREEN。
- 既有回归：`tests/test_dns_dot.py`、`test_try_add_remove_races.py`、`test_ipv6_filter.py`、`test_blockio_ratelimit.py`、`test_dns_nameserver_filter.py` 均 **ALL PASSED**；`tests/probe_wrap_socket_fd.py` 结论保持 **A(detached)**。
### 预期效果与边界
`not enough ip` 现在具备开始、上一窗速率、恢复尾窗、本轮累计四要素，恢复行补齐最后不足 60 秒的账目且只出现一次。HEAD 从「每 worker 一条、按 worker 重置」变为「每前端每 60 秒至多一条」，同一病态前端的 worker 密集死亡会折成放行行 + 下一窗 `(suppressed N)`，N 可直接作为该前端窗口内 worker 死亡率的诊断量级；稀疏独立失败仍逐条放行。
### 部署（2026-10-28 19:46，待用户重启）
- 交叉验收（Oz 复核）：diff 审查（提案 A 账本三字段/锁内原子恢复通告、提案 B 三出口全覆盖/旧属性删净/返回值与探测逻辑零改动）+ 独立复跑 py_compile ×2、test_xlog_roll_f 23/23、五套回归全 PASS、probe 结论 A(detached)——全部通过，结论验收通过。
- rsync 前备份：`backups/pre-rsync-20261028/`（2 份 `.orig` + 2 份 `.diff`，diff 行数 connect_manager 51 / http1 81，与 G 阶段改动一致）。
- rsync 覆盖：`connect_manager.py`、`http1.py` 两文件到 `/Applications/XX-Net/code/default/lib/noarch/front_base/`，rsync 后逐文件 diff 校验与快照完全一致。
- 运行进程（10-28 10:44 启动 PID 14268）仍跑 F 阶段代码；**待用户重启后 G 阶段生效**，重启后可复核：not enough ip 放行行应带 `(suppressed N last window)`、旱灾结束时出现一条 `not enough ip recovered (suppressed X, total N)`、同坏前端 HEAD 密集死亡折为每 60 秒一组 WARN+trace。
### 重启后审计（2026-10-28 21:48 重启，PID 94572）
- **新进程确认**：launcher PID 94572，2026-10-28 21:48:30 启动（晚于 rsync 完成 19:46）→ G 阶段代码确认加载；`nc -z 127.0.0.1 8086` OPEN。
- **代理回归**：`curl -x 127.0.0.1:8086 http://www.google.com/generate_204` → **204（0.55s）**。
- **零 ERROR**：统计窗口 21:48:30→22:25（`awk '$1=="2026-10-28" && $2>="21:48:30"'`，约 37 分钟），7 份日志（launcher/gae_proxy/x_tunnel/seley/tls_relay/cloudflare/smart_router）ERROR 合计 **0**。
- **提案 A 实证（not enough ip 新格式，三份日志全部生效）**：
  - 放行行携带上一窗计数：`not enough ip (suppressed 0 last window, suppressing for 60s after this line)`；恢复通告 `not enough ip recovered (suppressed X, total N)` 与放行行近乎 1:1 成对。最终计数（截至 22:25）：tls_relay 56 放行 / 55 recovered、seley 35 / 35、cloudflare 5 / 4——tls/cloudflare 各 1 条差额为窗口尾部旱态仍在进行中（cloudflare 尾行 22:21:55 放行后未恢复），恢复行尚未到来，非丢账。
  - **尾账闭环实证**：cloudflare `21:49:37 not enough ip recovered (suppressed 5, total 5)`——窗口内 5 次抑制被恢复行一次性结算清账，「开始/过程速率/尾窗/总数」四要素完整。
  - tls_relay 样例呈旱灾-恢复短周期交替：21:48:43 放行(suppressed 0) → 21:49:34 recovered (suppressed 4, total 4) → 21:49:36 放行(suppressed 0) → 21:49:46 recovered (suppressed 0, total 0)。旱灾事件率低，典型窗口计数 0-5，与「追问澄清」推演（常态远低于 60 正字天花板）一致。
- **提案 B 实证（HEAD 按前端聚合）**：seley 新窗口 HEAD WARN 仅 5 条，来自 3 个不同前端（56.155.82.86 / 58.20.254.31 / 209.33.166.19×3）；同前端 209.33.166.19 三条间隔 12min / 4.6min，均远超 60s 闸门——密集死亡折行达标，稀疏失败各自放行（闸门语义）。tls_relay / cloudflare 新窗口 0 条。trace 行无 `(suppressed N)` 尾缀属正常（同窗无第二次失败，N=0 不加尾缀）。
- **背景流量（真实信号未节流，符合 T3 设计）**：seley 新窗口 `_connect_ssl ... fail:OSError TimeoutError`（病态前端 connect 失败）与 x_tunnel `POST ny3.xx-net.org / response_fail:retry timeout:30`（上游波动链路）仍逐条落盘。
- **审计判定**：G 阶段（提案 A+B）全部生效——not enough ip 四要素齐全、恢复清账闭环、HEAD 折行达标、零 ERROR、代理回归 204。F/G 两阶段日志治理至此全部收尾。

# H 阶段：建连失败聚合、DNS 退避与 total 语义修正（2026-10-29）
## 问题澄清与范围
- **`total 0` 的旧口径**：G 阶段的 `total_suppressed_no_ip_warn` 只累计被抑制事件，首条放行不计，因此单次旱态事件后恢复会显示 `total 0`。这符合旧实现定义，但 `total` 名称容易让人理解为总事件数。用户确认 H 阶段改为**本轮总事件数（放行+抑制）**，首次事件即为 1；G 章历史语义保留不改写。
- **`LogThrottle` 的用途**：它是带锁的通用 60 秒节流工具，每个 key 的首个事件放行，窗口内后续事件只计数，下一窗口放行时返回上一窗 suppressed 数。H 前生产调用方为 `http_dispatcher.py` 的 `retry_task_cb`（按 URL）与 `http1.py` 的 HEAD 三出口（按前端 IP）；`not enough ip` 与 `pipe_socks` 各有独立账本，不使用该类。
- 本轮只实施三项：total 语义修正、`_connect_ssl` 按前端聚合、DNS 路由不可达退避。不做 x_tunnel episode、微旱态降级、`LogThrottle` 容量改造；不 rsync、不重启、不部署。
## 实施改动
1. **total 改为总事件数（`connect_manager.py`）**：字段改名 `total_no_ip_events`，每次进入 no-IP 分支都在 `no_ip_lock` 内、放行判断前递增。恢复行格式不变，但 N 现在包含全部放行与抑制事件，恢复后清零。D 组 100/101/160/161 四次旱态事件在 162 恢复时输出 `not enough ip recovered (suppressed 1, total 4)`；`no_ip_time`、60 秒闸门、尾窗 suppressed、`sleep(1)` 和连接数据路径未改。
2. **`_connect_ssl` 按前端与异常类别聚合（同文件）**：引入实例级 `connect_ssl_log_throttle = LogThrottle(60)`，key 为 `(ip_str, type(e).__name__)`，避免不同 front 共享配额。首条保留完整原始 WARN，窗口内仅计数，下一放行仅在 N>0 时附 `(suppressed N)`。无论是否放行，`queue.put(e)` 均执行；建连、异常回传、评分、重试及 `report_connect_fail()` 路径未改。
3. **DNS per-nameserver 60 秒退避（`dns_query.py`）**：`LocalDnsQuery` 新增带锁的 `ns_backoff` 账本。仅 `OSError` 的 `EHOSTUNREACH`/`ENETUNREACH` 写入 next-retry 时间；`query()` 跳过冷却 server，全部冷却时只选最早到期的一台做 fail-open 探测。冷却期在途重复失败仅 DEBUG 且不延长原截止时间，发送成功清账，到期后允许重试；其他 errno 保持原 WARN 且不退避。waiter/Queue、UDP receiver、查询超时和其他 DNS 路径未改。
## TDD 与验证
- **RED**：旧实现上 `test_xlog_roll_f.py` 有 8 项目标失败（D1/D3/E2-E7），修正测试夹具后无无关属性异常；`tests/test_dns_ns_backoff.py` 有 6 项目标失败，N3「其他 errno 不退避」作为旧行为保护通过。
- **GREEN**：`test_xlog_roll_f.py` **30/30 PASS**（滚动 7 + LogThrottle 6 + HEAD 6 + no-IP 4 + `_connect_ssl` 7）；`tests/test_dns_ns_backoff.py` **7/7 PASS**（首错 WARN+冷却、重复不延长、其他 errno、冷却跳过、全冷却单点 fail-open、到期重试、成功清账）。
- `python3 -m py_compile`：`connect_manager.py`、`dns_query.py` 均通过。
- 既有回归：`test_dns_dot.py`、`test_try_add_remove_races.py`、`test_ipv6_filter.py`、`test_blockio_ratelimit.py`、`test_dns_nameserver_filter.py` 均 **ALL PASSED**；`probe_wrap_socket_fd.py` 结论保持 **A(detached)**；`git diff --check` 通过。
- 新测试按相对路径加载 Coding 快照；既有根目录测试仍加载 `/Applications/XX-Net` 当前安装，未部署时用于证明实装既有行为无回归。DNS 测试加载时出现的 `dnslib/lex.py` 转义 `SyntaxWarning` 为既有依赖代码，本轮未改。
## 生效边界
H 阶段代码与测试完成时仅落盘仓库快照，尚未影响当时运行进程。`_connect_ssl` 沿用 `LogThrottle` 的下一放行结算边界，不增加成功清账或后台尾账；DNS 的「发送成功」只表示本地 `sendto` 成功，不扩展为 nameserver 健康判定。
## 部署与重启后审计（2026-10-29）
- **部署范围**：仅覆盖 `connect_manager.py` 与 `dns_query.py` 到 `/Applications/XX-Net` 对应路径；覆盖前备份为 `backups/pre-rsync-20261029-h/`，含两份 `.orig` 与两份 `.diff`。rsync 后快照与安装版 SHA-256 分别一致：connect_manager `aad05766b18db85e38f2dab92c1bafd3c8906423ba22c6fcb196d488f4c50c2f`，dns_query `a6c797e750492dbdb419db4bb30c963d204088febc0ca80f44da254da1cf5460`。
- **新进程确认**：用户重启后 launcher PID 2066，启动时间 `2026-10-29 19:00:28`，命令为 `Python code/default/launcher/start.py`，8086 端口 LISTEN/OPEN，确认晚于部署加载 H 代码。
- **`_connect_ssl` 生效**：重启后日志已出现新 `(suppressed N)` 尾缀；同 IP+异常类别的重复失败被聚合，不同 key 独立放行。
- **total 新口径生效**：多条恢复日志为 `not enough ip recovered (suppressed 0, total 1)`，首次事件已计为 1。微旱态在最初约 16 分钟内约 5 对、每轮约 10–11 秒，频率不高，继续保留及时 WARN，不做降级或延迟。
- **DNS 延长观察**：19:00:28–20:22:15（约 82 分钟）检查 smart_router warning/start 日志，`EHOSTUNREACH`、`ENETUNREACH`、Errno 65/51 及 `send_request` 冷却记录均为 0。本窗口未触发目标错误，只能确认无相关日志，不能据此声称退避的现场疗效；旧进程时期的 DNS 洪水停止也不归因于 H。

# I 阶段：前端重试与 x_tunnel 故障链日志聚合（2026-10-29）
## 重启后复评与范围
- H 部署后的生产日志改变了剩余事项优先级：一次前端/x_tunnel 波动可产生约百条关联 WARN。cloudflare 的 `retry_task_cb` 12 条日志有 12 个不同 tid URL，却全部来自同一前端 `198.18.0.159`；同窗口另有 task timeout 12 条、req response timeout 12 条。x_tunnel 约 69 行中包括 retry transfer_no 17、check_upload_not_acked 15、front retry 13、roundtrip status:602 12、roundtrip failed 12。
- 因此 P1 调整为修正 dispatcher 重试日志 key，并按 session/transfer 聚合 x_tunnel 重复告警；P2 仅补强节流、DNS 和实例隔离测试。DNS 继续观察；微旱态不改；`LogThrottle` 不做生产容量改造。
- 硬边界：只改日志，不改重试、状态、Queue、连接评分、传输或返回路径；不做四个 front.py 的 `req get response timeout`、完整 episode 框架、公共脉冲结算或全局 ERROR 限流。I 阶段部署、rsync 与重启需用户另行确认。
## 实施改动
1. **dispatcher 重试 key 修正（`http_dispatcher.py`）**：新增内部日志 key/helper，复用现有实例级 `retry_log_throttle = LogThrottle(60)`。key 为 `(task.method, task.host, 去查询参数 endpoint bytes, front ip)`，去掉递增 tid；endpoint 全程使用 bytes，避免非 UTF-8 path 因日志解码中断重试或丢弃响应。`retry_task_cb()` 与 `request()` 的 task timeout/status 共用同一窗口。放行行仍保留完整 URL/tid 与 trace，仅在 suppressed>0 时附计数。fail 计数、重试次数、Queue、notify、响应与返回路径未改。
2. **x_tunnel 按 transfer 聚合（`proxy_session.py`）**：后台线程启动前创建实例级 `transfer_log_throttle = LogThrottle(60)`；`transfer_warn()` 只负责日志，显式接收事件所属 session，key 为 `(session_id, transfer_no, kind)`。roundtrip 使用网络请求前捕获的 `request_session_id`，retry/ack timeout 使用对应 `data_info["session_id"]`，避免 reset 后旧响应按新 session 错账。接入 `check_upload_not_acked`、get_upload_data retry、非 200 roundtrip 和最终 `roundtrip failed` 四处，kind 相互独立。原始字段保留，下一窗附 suppressed；`data_info["stat"]`、retry、`target_on_roads`、notify、sleep 与传输逻辑未改。
3. **front retry 聚合（`front_dispatcher.py`）**：模块级 `LogThrottle(60)` 按完整 `(host, path)` 聚合。相同 tid 的重复重试只放行一条，不同 tid 各自保留首条；front 选择、状态码处理、sleep、重试与请求内容未改。
4. **测试语义补强（仅测试）**：`LogThrottle` 契约改为真实语义——超过 512 个活跃 key 可保留，触发剪枝只删过期 key，并精确验证并发计数；DNS 同时覆盖 EHOSTUNREACH/ENETUNREACH 及冷却期 DEBUG；`_connect_ssl` 通过真实 `ConnectManager.__init__`、stub 后台线程验证实例隔离。未为满足测试修改生产代码。
## TDD 与完整验证
- **RED**：Task 1 新测试在旧实现上 8/8 按目标失败；Task 2 在生产快照上因真实初始化缺少 `transfer_log_throttle` 触发预期断言失败。Task 3 是对既有正确行为的覆盖补强，新断言首次运行即通过，不伪称 RED，也未发现生产缺陷。
- **GREEN**：`test_retry_log_throttle.py` **9/9 PASS**；`test_xtunnel_log_throttle.py` **8/8 PASS**；`test_xlog_roll_f.py` **33/33 PASS**（滚动 7 + LogThrottle 8 + HEAD 6 + no-IP 4 + `_connect_ssl` 8）；`test_dns_ns_backoff.py` **9/9 PASS**（N1/N2 各覆盖两种 errno，另含 N3–N7）。
- 三个 I 阶段生产文件 `py_compile` 全部通过；既有 `test_dns_dot.py`、`test_try_add_remove_races.py`、`test_ipv6_filter.py`、`test_blockio_ratelimit.py`、`test_dns_nameserver_filter.py` 均 **ALL PASSED**；`probe_wrap_socket_fd.py` 结论保持 **A(detached)**；`git diff --check` 通过。
- 测试验证了聚合不会减少重试入队、异常/响应送达、状态迁移或 notify；20 线程下同 key 仅一条 WARN。DNS 测试加载时仍可能出现既有 `dnslib/lex.py` 转义 `SyntaxWarning`，本轮未修改依赖。
## 独立审查与修复
- 独立全量审查首次因 API 限流中断，接续后完成。无 Critical；两项 Important 均经最小真实路径复现并在一轮修复中完成：R9 证明旧实现遇到非 UTF-8 path 时重试次数为 0、响应变 `None`，改用 bytes key 后重试/响应/fail/notify 全部保持；X8 证明 reset 后旧响应会与新 session 同 transfer 错账，改用请求捕获的 session 后两类状态 WARN 各自放行。两项均先 RED 后 GREEN，随后完整回归再次全部通过。
- Minor 延后：非 200 roundtrip 原格式串末尾已有空格，suppressed 结算行会出现双空格；不影响语义，本轮不做格式润色。明确排除的微旱态、生产容量、完整 episode、全局 ERROR 限流、其他 front timeout 和既有传输并发设计均不在本轮扩展实施。
## 生效边界
I 阶段代码于 2026-10-29 完成，2026-10-31 已与 J 一并部署到 `/Applications/XX-Net`，并由用户重启加载（PID 24668，18:16:13）。部署前的结果均为离线测试证据；重启初审与后续疗效观察见下文。
## 重启验证清单（2026-10-31 已部署并重启）
- **部署记录**：I 三个生产文件与 J 的 `base_container.py` 已按既有模式保存 `.orig`/`.diff` 到 `backups/pre-rsync-20261031-ij/`，rsync 后逐文件 cmp 与 SHA-256 校验一致。不得以重启前的 H 运行日志评估 I/J 疗效。
- **重启确认**：已记录新 PID、启动时间、8086 监听、BoringSSL 与两次代理 204；除下述 Smart Router 启动竞态外无新增 ERROR。
- **dispatcher 疗效**：在可比的故障窗口统计 `retry_task_cb`、task timeout/status。相同 front/host/endpoint 的不同 tid 应聚为每 60 秒一条，下一窗放行附 suppressed，完整 URL/tid 与 trace 仍在；不同 front/host/endpoint 应独立。没有对应故障时只记“未触发”。
- **x_tunnel 疗效**：核对 ack timeout、retry、非 200 roundtrip、最终 failed 四类日志按 `(session_id, transfer_no, kind)` 收敛；不同 transfer/kind 不错并。若发生 reset，检查旧 session 的延迟响应不与新 session 同号 transfer 错账。`front retry` 同 tid 应聚合、不同 tid 应独立。
- **行为回归**：观察正常上下行、重试与恢复，确认无新增传输异常、响应丢失或连接停滞；日志 suppressed 不代表事件被丢弃。离线已验证 Queue/notify/状态路径，现场仍需核对代理可用性。
- **收尾**：比较重启前后同长度、同类故障窗口的 WARN 数量并保留样例；更新本文件与 `LOCAL-VERSION.md` 后再决定是否进入后续日志治理。DNS 继续只在实际出现 EHOSTUNREACH/ENETUNREACH 时核对 H 退避，未触发不宣称疗效。

# J 阶段：x_tunnel 下载 SN 重复超时日志聚合（2026-10-29 实施，2026-10-31 已部署并重启）
## 复评与范围
- 用户决定暂缓 I 部署，并要求记录后续验证、继续下一优先级。H 运行窗口 `2026-10-29 19:21:27–21:08:42` 中，`mark_sn_timeout` 首报 363 条、renew 379 条，共 742 条、363 个不同 SN；同 SN 最多重复 6 次。它是 I 未覆盖的最大剩余 x_tunnel 日志簇。
- `req get response timeout` 在 cloudflare/tls_relay 共 121 条，与同数量 task timeout 及 I 的 dispatcher/retry 故障链同源，留待 I 部署审计后复评，不在 J 重复治理。微旱态、公共容量、完整 episode 仍不扩展。
- J 仅改 `BlockReceivePool.mark_sn_timeout` 的日志：每个不同 SN 首报保留，同 SN 首报/renew 共用 60 秒窗口，下一窗结算 suppressed。不改 `get_timeout_list`、重试、状态或传输。
## 实施与 TDD
- `base_container.py` 引入实例级 `timeout_log_throttle = LogThrottle(60)`，在 `reset()` 中随 SN 状态生命周期重建；reset 后同号 SN 重新首报。`allow(sn)` 只位于原本会输出日志的首报和较新 t 的 renew 分支；被抑制 renew 仍更新 `server_send_time`，相同/较旧 t 的静默事件不进入节流账本。
- `reset()` 使用接收池已有的 `self.lock` 同时清空 SN 状态和替换节流账本，与 `mark_sn_timeout()` 互斥，避免新生命周期事件落入旧窗口。`get_timeout_list()`、两个 `timeout_sn_list` 写入条件、重试与传输路径不变。
- 新增 `tests/test_mark_sn_timeout_throttle.py` M1–M8：同 SN 聚合且状态仍更新、不同 SN/池实例独立、跨窗 suppressed、reset 后重新首报、20 线程单 WARN 且保存最大 t、静默旧事件不消耗窗口、reset/mark 可控交错下新生命周期首报。
- **初版 RED**：旧实现 M1/M3/M5 按目标失败，分别表现为首报+renew 全放行、无跨窗结算、20 线程 20 条 WARN；M2/M4/M6 作为既有行为保护通过。初版实现后 M1–M6 **6/6 PASS**。
- 独立审查发现两项 Important，均在同一轮修复：① 静默重复/旧 t 事件无条件 `allow(sn)`，会虚增 suppressed 并消费跨窗放行；M7 先 RED，再将 `allow()` 移入两个实际日志分支至 GREEN。② reset 清空 SN 状态与替换账本非原子，mark 可在中间使用旧窗口并吞掉新生命周期首报；M8 以可控交错复现 `old_allow=True`，随后 reset 加同一把锁至 GREEN，复验为 `old_allow=False`。无 Critical、Minor 或待裁定项。
- 最终完整回归：J **8/8**、I dispatcher 9/9、I x_tunnel 8/8、综合节流 33/33、DNS 9/9，五套既有回归全部通过，fd 探针 **A(detached)**，生产文件 `py_compile` 与 `git diff --check` 通过。
## 后续验证与边界
- J 已于 2026-10-31 与 I 一并部署并由用户重启加载。部署覆盖 I 三个生产文件及 J 的 `base_container.py`，覆盖前备份位于 `backups/pre-rsync-20261031-ij/`。
- 重启后按相同故障窗口比较：不同 SN 首报不得减少，同 SN 60 秒内 renew 应收敛并在下一窗附 suppressed；若发生 reset，同号 SN 应重新首报。没有对应事件只记未触发，不把零日志当疗效。

## I/J 重启后初次审计（2026-10-31）
- 用户重启后 launcher PID 24668，启动时间 `2026-10-31 18:16:13`，晚于四文件部署；8086 正常 LISTEN，启动日志确认 `TLS implementation: BoringSSL`。两次代理 `generate_204` 分别约 0.465s、0.486s，均返回 204。
- 初审窗口内 x_tunnel/cloudflare/tls_relay warning 零新增；seley 仅两条不同 IP 的 `_connect_ssl` handshake 首报。dispatcher retry/timeout/status、transfer、front retry、roundtrip、`mark_sn_timeout` 均未触发，因此只确认新代码已随进程加载，不能宣称 I/J 生产收敛疗效。
- smart_router 在 `18:16:13.364` 出现一条 `NoneType.query_recursively` ERROR。堆栈与源码确认是既有启动顺序竞态：`local/__init__.py` 在 152 行先启动 proxy_server，164 行才给 `g.dns_query` 赋值，启动瞬间请求可落入中间窗口。I/J 未修改 smart_router 或该初始化路径，本条与本次四文件无关；后续可单独按 RED→GREEN 修复初始化顺序或增加未就绪保护。
- 后续 `18:19:51–18:21:14` 出现首批 I 目标首报：transfer 102/114 的 roundtrip status、final failed、retry、ack timeout 按不同 kind/transfer 各自放行，front retry tid=114 首报保留；seley 的 status 与 retry 样例相隔约 81 秒。窗口内没有同 key 重复事件，因此没有 suppressed 属预期，尚不能据此测量收敛比例。J 的 `mark_sn_timeout` 仍未触发；除启动竞态外未出现新增 ERROR。

# K 阶段：Smart Router DNS 初始化启动竞态（2026-10-31，已部署待重启）
## 根因与最小修复
- 生产堆栈为 proxy_handler → smart_route `g.dns_query.query_recursively()`，在重启首秒得到 None。真实 `start()` 的顺序是先 `proxy_server.start()`，再执行 `get_local_ips()` 与 `CombineDnsQuery()`，代理线程可在 DNS 对象赋值前受理请求。
- 独立审查补充发现同类 Important：SOCKS5 UDP ASSOCIATE 会在代理启动后、DNS 服务启动前读取 `g.dns_srv.udp_relay_port`。因此将 DNS 地址计算、`g.dns_srv` 构造与 `start()` 也前移到代理 `start()` 之前，并把代理监听地址改用独立变量，避免与 DNS 地址日志混用。
- 最终只调整启动顺序：`g.local_ips` → `g.dns_query` → `g.dns_srv` → `g.proxy_server`。`local_ips` 是 LocalDnsQuery 的前置依赖，必须先初始化；其余配置、PipeSocks、端口参数和 DNS/代理业务逻辑不变，不增加未就绪分支。
## TDD、审查与验证
- 新增 `tests/test_smart_router_startup_race.py`：加载真实 `start()`，仅在 HTTPServer.start 的网络边界模拟“启动即收到域名请求/UDP ASSOCIATE”，真实启动顺序必须让请求看到已初始化的 DNS 对象和 UDP 中继端口。
- **S1 RED**：旧实现稳定复现 `AttributeError("'NoneType' object has no attribute 'query_recursively'")`；前移 DNS 查询对象后 GREEN。**S2 RED**：仅前移查询对象时复现 `AttributeError("'NoneType' object has no attribute 'udp_relay_port'")`；再前移 DNS 服务后 GREEN。最终 S1/S2 通过，ready=True，目标文件 py_compile 通过。
- 独立审查无 Critical；唯一 Important 的 UDP ASSOCIATE 窗口已按 S2 RED→GREEN 修复，未做第二轮审查。两项 Minor 延后：代理绑定失败时，提前创建的 DNS 资源需要进程退出回收（原启动失败语义亦未提供完整清理）；测试的进程内 `sys.modules` 替身在与其他 pytest 用例同进程运行时应恢复（当前脚本独立执行）。
- 最终完整回归：S1/S2、J 8/8、I dispatcher 9/9、I x_tunnel 8/8、综合节流 33/33、DNS 9/9，五套既有回归及 fd 探针全部通过，`git diff --check` 通过。
## 生效边界
- K/L 已于 2026-10-31 一并部署到 `/Applications/XX-Net`，当前待用户重启，未 push。三个文件覆盖前均有 `.orig`/`.diff`，位于 `backups/pre-rsync-20261031-kl/`；rsync 后 cmp 与 SHA-256 一致。
- K 的生产验证目标：启动首秒不再出现 `NoneType.query_recursively` 或 `NoneType.udp_relay_port`，并确认 8086、DNS 与代理 204 正常。L 的验证目标：后续 host=None 请求不再出现 bytes 格式化 TypeError，顶层 warning 委托由 B9 保证；无目标事件只记未触发。

# L 阶段：剩余推荐错误修复与尾账方案取舍（2026-10-31，已部署待重启）
## 证据与范围
- `host=None` 的生产堆栈来自 cloudflare dispatcher：默认诊断 URL 在 `b"%s %s%s" % (method, host, path)` 处对 None 做 bytes 格式化，导致 request 进入异常出口。修复只在未显式传 url 时用 `b"<unknown>"` 作为展示占位；原始 task.host、last_host、重试、响应、fail/notify 与 I 的聚合 key 不变。
- `xlog.warning()` 官方笔误调用了不存在的 `default_log.warnin`；改为与 `warn()` 一致委托 `default_log.warn`，完整传递 fmt/args/kwargs。
- `req get response timeout` 在四份 x_tunnel 当前 warning 日志中总量及 I/J 重启后均为 0，未达到此前“仍高频再聚合”的条件，本轮不修改四个 front.py。
- 尾账选择：有明确 close/reset/recovered 终点时优先生命周期 flush，只结算一次、无后台定时器，资源与退出语义更优；长期模块级、无终点账本才考虑脉冲，但需要额外线程/定时器、锁与退出协调。本轮不实施全局脉冲或扩张 flush 框架。
## TDD 与验证
- R10 在旧实现稳定复现 bytes 格式化 TypeError、fail_num=0；修复后 host 仍为 None，正常走 timeout 日志与 fail/notify 路径，dispatcher 10/10 PASS。
- B9 在旧实现复现 `WarningCapture` 无 `warnin` 的 AttributeError；修复后默认 logger.warn 收到完整参数，xlog 34/34 PASS。
- 独立审查无 Critical、无 Important；唯一 Minor 为 R10 未直接覆盖 host=None 的响应/重试路径和 Task.host/notify 断言。当前局部 url_host 实现未改变这些语义，按规则记录延后，不做第二轮审查或扩张修复。
- 最终完整回归：K S1/S2、J 8/8、dispatcher 10/10、I x_tunnel 8/8、xlog 34/34、DNS 9/9，五套既有回归及 fd 探针全部通过，目标文件 py_compile 与 `git diff --check` 通过。
## 生效边界
- K/L 均已部署，但当前进程未重启、尚未加载；部署文件 cmp/SHA-256 已校验，备份与生产验证目标见上节。
