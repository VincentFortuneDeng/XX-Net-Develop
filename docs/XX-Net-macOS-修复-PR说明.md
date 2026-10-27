# XX-Net macOS (Apple Silicon) 修复说明

## 概述
本文档记录 XX-Net 在 macOS（Apple Silicon）上的一组修复，按文件组织，附变更前后 diff、问题背景、影响与验证方法。变更分为三类：

1. 代码修复（含 diff，可直接合入）
2. 打包/分发问题（无代码 diff，独立说明）
3. 可选的健壮性改进

## 环境
- 系统：macOS ARM64（Apple Silicon）
- 版本：XX-Net 5.16.6
- 涉及模块：`launcher`（托盘、启动）、翻译资源、macOS 分发包

---

## 一、代码修复

### 文件：`code/default/launcher/mac_tray.py`

#### 变更 1：PyObjC 11.x 兼容性
PyObjC 11.x 将 `objc` 从 `AppKit.objc` 独立为顶层模块，`@AppKit.objc.callbackFor(...)` 在新版本下不可用，导致托盘初始化失败。

新增顶层导入：

```diff
 import os
 import shutil
 import sys
+import objc
 import locale
```

回调装饰器改用顶层 `objc`：

```diff
-@AppKit.objc.callbackFor(AppKit.CFNotificationCenterAddObserver)
+@objc.callbackFor(AppKit.CFNotificationCenterAddObserver)
 def networkChanged(center, observer, name, object, userInfo):
```

**影响**：在 PyObjC 11.x 下托盘可正常初始化；对旧版本 PyObjC 无回归（`objc` 顶层模块长期存在）。

#### 变更 2：终端（非 .app bundle）启动时 delegate 未触发
以非 bundle 方式（终端脚本）启动时，`NSApp.run()` 不会自动触发 `applicationDidFinishLaunching_`，托盘与菜单不显示。

在事件循环启动前显式调用 `finishLaunching()`：

```diff
     fetchCurrentService('IPv4')
+    # Explicitly trigger applicationDidFinishLaunching_ when process is started
+    # from terminal (non-bundle), where NSApp.run() may not fire the delegate
+    app.finishLaunching()
     AppHelper.runEventLoop()
```

**影响**：终端启动与 bundle 启动行为一致。

#### 变更 3：helper 安装避免阻塞主线程
`setupHelper()` 可能触发管理员授权对话框，在 AppKit 主线程同步执行会阻塞事件循环。改为守护线程执行：

```diff
     def applicationDidFinishLaunching_(self, notification):
-        setupHelper()
+        # Run setupHelper in background to avoid blocking the main thread
+        import threading
+        threading.Thread(target=setupHelper, daemon=True).start()
         loadConfig()
         self.setupUI()
         self.registerObserver()
```

#### 变更 4：语言检测回退
终端启动时 `locale.getdefaultlocale()` 可能返回 `None`（如 `LANG=C.UTF-8`），导致本地化判断失效。增加基于 `NSLocale.preferredLanguages()` 的回退：

```diff
-lang_code, code_page = locale.getdefaultlocale()
+lang_code, code_page = locale.getdefaultlocale()
+if not lang_code:
+    try:
+        preferred = AppKit.NSLocale.preferredLanguages()[0]
+        if preferred.startswith('zh'):
+            lang_code = 'zh_CN'
+    except Exception:
+        pass
```

#### 变更 5：helper 持久化，消除每次重启后的授权弹窗
helper 二进制原安装至 `/tmp/helper`。macOS 每次重启清空 `/tmp/`，导致每次开机首次启动都重新安装（`chown root` + `chmod 4755`），弹出管理员授权框，影响无人值守场景。

helper 路径改为应用内持久路径：

```diff
 current_path = os.path.dirname(os.path.abspath(__file__))
-helper_path = os.path.join('/tmp', 'helper')
+helper_path = os.path.join(current_path, 'mac_helper_root')
```

`setupHelper()` 增加前置检查：已存在且为 setuid-root 时直接返回，仅在首次安装或文件缺失/损坏时才请求授权：

```diff
 def setupHelper():
-    try:
-        with open(os.devnull) as devnull:
-            subprocess.check_call(helper_path, stderr=devnull)
-    except:
-        if os.path.exists(helper_path):
-            os.remove(helper_path)
-        shutil.copyfile(os.path.join(current_path, 'mac_helper'), helper_path)
-
-        chownCommand = "chown root \\\"%s\\\"" % helper_path
-        chmodCommand = "chmod 4755 \\\"%s\\\"" % helper_path
-        executeCommand = 'do shell script "%s;%s" with administrator privileges' % (
-            chownCommand,
-            chmodCommand
-        )
-
-        xlog.info("try setup helper:%s", executeCommand)
-        subprocess.call(['osascript', '-e', executeCommand])
+    # If helper already exists with setuid-root permissions, skip setup entirely.
+    # This avoids the admin dialog on every reboot, since the helper is now stored
+    # at a persistent path instead of /tmp/.
+    if os.path.exists(helper_path):
+        try:
+            st = os.stat(helper_path)
+            if st.st_uid == 0 and (st.st_mode & 0o4000):
+                return
+        except Exception:
+            pass
+
+    # First-time install or helper was removed/corrupted
+    if os.path.exists(helper_path):
+        try:
+            os.remove(helper_path)
+        except Exception:
+            pass
+    shutil.copyfile(os.path.join(current_path, 'mac_helper'), helper_path)
+
+    chownCommand = "chown root \\\"%s\\\"" % helper_path
+    chmodCommand = "chmod 4755 \\\"%s\\\"" % helper_path
+    executeCommand = 'do shell script "%s;%s" with administrator privileges' % (
+        chownCommand,
+        chmodCommand
+    )
+
+    xlog.info("try setup helper:%s", executeCommand)
+    subprocess.call(['osascript', '-e', executeCommand])
```

**影响**：授权仅在首次安装（或手动删除 helper）时出现一次，之后重启不再弹窗。
**备选实现方向**：`LaunchDaemon` 或 `SMJobBless` 可提供更规范的特权 helper 生命周期管理；上述改动为最小改动方案。

---

### 文件：`code/default/launcher/start.py`

#### 变更：darwin 原生库路径支持
在 `noarch_lib` 之后插入 macOS 专用库搜索路径，仅在目录存在时加入，便于加载平台相关的原生库：

```diff
 noarch_lib = os.path.abspath(os.path.join(default_path, 'lib', 'noarch'))
 sys.path.append(noarch_lib)
+
+if sys.platform == "darwin":
+    darwin_lib = os.path.abspath(os.path.join(default_path, 'lib', 'darwin'))
+    if os.path.isdir(darwin_lib):
+        sys.path.insert(0, darwin_lib)
```

**影响**：`lib/darwin` 缺失时无副作用；存在时优先加载。

---

### 翻译资源

涉及文件：
- `code/default/launcher/lang/zh_CN/LC_MESSAGES/messages.po`
- `code/default/smart_router/lang/zh_CN/LC_MESSAGES/messages.po`
- `code/default/launcher/web_ui/status.html`

变更内容：
- `messages.po`：补充缺失的中文翻译条目（配置、代理设置、系统信息标签、路由相关字符串等）。
- `status.html`：系统信息表格中部分标签为硬编码英文、未纳入国际化，补加 `{{ _() }}` 包裹。

`status.html` 变更示例（表示变更模式，实际条目以对应标签为准）：

```diff
-<td>Python Version</td>
+<td>{{ _("Python Version") }}</td>
```

**影响**：中文环境下 Web UI 状态页与相关字符串完整本地化。

---

## 二、打包/分发问题

### macOS 分发包未解压 `python3.zip`，回退至系统 LibreSSL

**现象**：相同网络环境下，macOS 版连接稳定性明显低于 Windows 版，出现 TLS 握手超时与大量 602 错误。

**根因链**：
- TLS 层优先使用内置 BoringSSL；不可用时回退至系统 `ssl` 模块。
- macOS 分发包内含带 BoringSSL 的嵌入式 Python（`python3.zip` 内含 `boringssl.abi3.so`），但该压缩包在分发/安装流程中未被解压。
- `start` 脚本在 `python3/bin/python3` 不存在时回退至系统 Python，而系统 Python 使用 LibreSSL 2.8.3，TLS 指纹特征明显，稳定性差。

**验证**：

```
python3 -c "from front_base import openssl_wrap; print(openssl_wrap.implementation)"
# 回退系统 Python：ssl (LibreSSL 2.8.3)
# 使用内置 Python：BoringSSL
```

**补充事实**：`python3.zip` 内嵌 Python 为 x86_64 架构，在 Apple Silicon 上运行需 Rosetta 2。

**可行的处理方向**：
1. 安装流程或 `start` 脚本在检测不到内置 Python 时自动解压 `python3.zip`。
2. 提供 ARM64 原生的嵌入式 Python 与 BoringSSL，去除对 Rosetta 的依赖。

---

## 三、可选的健壮性改进

### 文件：`start`（macOS 分支）
`start` 脚本在首次安装标记生成前执行 `xattr -c -r *`。当目录中存在 root 属主文件（如持久化后的特权 helper）时，该命令对这些文件会返回 `Permission denied`。此为非致命错误，不影响启动，但会在终端输出报错。

**可选改进**：对该行增加错误容错，或将清理范围限定到需要去除隔离属性的具体文件，避免对特权文件报错。

---

## 四、已排除的尝试（记录备查，不纳入变更）

- **定时重连 watchdog**：排查初期曾在 `x_tunnel/local/proxy_session.py` 加入每 60 秒触发的重连补丁。确认根因为 BoringSSL 缺失后已完全移除，恢复至与上游一致的状态。
- **自行编译 ARM64 原生 BoringSSL**：使用 master 版本编译时，`SSL_CTX_set_permute_extensions`、`SSL_CTX_set_num_tickets` 等接口的行为与分发包内固定版本不一致，导致 relay 返回 400/500。原生方案需与现有 `boringssl.abi3.so` 对应的固定 commit 保持一致，否则 TLS ClientHello 存在差异。该方案未采用。

---

## 验证清单

- 托盘：终端与 bundle 两种方式启动均能显示菜单，中文环境下菜单本地化正常。
- 授权弹窗：首次安装弹一次；重启后再次启动不再弹窗（helper 为 setuid-root 且位于持久路径）。
- TLS：`openssl_wrap.implementation` 输出 `BoringSSL`；连接稳定性与预期一致。
- 翻译：Web UI 状态页系统信息标签在中文环境下完整显示中文。
