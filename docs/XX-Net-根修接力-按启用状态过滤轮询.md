# XX-Net 根修接力文档 — 按模块启用状态过滤轮询（2026-10-26）

> 用途：新会话接力。原会话上下文过大多次异常停止，此文档是唯一需要读取的外部记忆。
> 工作目录：`/Applications/XX-Net`（真实安装，版本 5.16.7，已有前序修复在运行）。

## 目标（用户原始需求）

用户指出：launcher 配置里已明确哪些模块启用/不启用（`enable_gae_proxy` / `enable_x_tunnel` / `enable_smart_router`）。当前有两个"轮询者"**无条件** GET 所有模块端点，对未启用模块必然得到 404，在 web_control.py 触发大量 "no module in path" 噪音日志。之前只是把 WARN 降为 DEBUG（治标），现在要做根修：**只在模块已启用时才轮询其端点**。

## 已知事实（前序研究已验证，勿重复调研）

### 启用状态的来源
- `code/default/launcher/config.py:70`：`all_modules = ["launcher", "gae_proxy", "x_tunnel", "smart_router"]`；每模块一个布尔 `config.enable_*`。
- **现成 API**：`GET http://127.0.0.1:8085/config?cmd=get_config`（web_control.py:433 `req_config_handler`）返回 JSON，含 `gae_proxy_enable` / `x_tunnel_enable` / `smart_router_enable`。
- `module_init.proc_handler` 只在 launcher 进程内可用，跨进程（x_tunnel local 线程、浏览器 JS）不可达——所以统一走 get_config API。

### 轮询者 #1：`code/default/x_tunnel/local/upload_logs.py`（运行在 x_tunnel 进程内）
- `collect_debug_and_log()`（约 60-104 行）：硬编码 14 个 debug_infos URL，端口来自 `get_launcher_port()`（读 `data/launcher/config.json` 的 `control_port`，默认 8085）。其中含：
  - `/module/gae_proxy/control/debug`、`/module/gae_proxy/control/log?cmd=get_new&last_no=1`（两处 x_tunnel 自身变体同理）
  - `/module/smart_router/control/log?cmd=get_new&last_no=1`
  - launcher 自身的 `/debug`、`/log`（应保持无条件）
- 循环内每个 URL GET timeout=1，失败 `xlog.warn("fetch info %s fail:%r", url, e)` —— 这就是 404 噪音来源。
- 调用入口：`pack_logs()`（132 行）← `upload_logs_thread()`（164 行）和 web handler `req_log_files`。

### 轮询者 #2：`code/default/launcher/web_ui/status.html`（浏览器端）
- 三处硬编码 `GET /module/gae_proxy/control/status`：
  - `OneKeyReport()` 约 170-173 行（用户点按钮触发）
  - `updateDetailStatus()` 约 257-260 行（1 秒定时器 + 页面加载即调，见 228-244 行 documentReadyToRun）
  - `updateNoobStatus()` 约 313-316 行（同一 1 秒定时器）
- 页面目前未加载过 get_config（config 页是另一个页面）。

### 本机环境（测试用）
- gae_proxy **未启用**；x_tunnel、smart_router 启用；launcher 端口 8085。
- LaunchAgent `com.xxnet.launcher` 会自动重启实例；当前实例 PID 19952（已加载前序修复）。
- 修复记录文档：`/Users/solvan/Downloads/XX-Net-macOS-ARM64-修复记录.md`（完成后追加本章）。

### Shell 注意
zsh：`grep/ls/find` 被别名成 rg/bat/fd/eza，用 `command grep ...` 走原版；双引号内的 grep 模式避免未转义管道符；`echo "============"` 需引号。

## 实现方案（用户认可的方向 A：调用方先查启用状态）

### 改动 1：upload_logs.py
在 `collect_debug_and_log()` 构建 URL dict 之前：
1. `GET http://127.0.0.1:{port}/config?cmd=get_config`（timeout=1，用已有的 simple_http_client）。
2. `json.loads` 取 `gae_proxy_enable`、`x_tunnel_enable`、`smart_router_enable`。
3. 按标志决定是否把对应模块的 URL 放入 debug_infos；launcher 自身 `/debug`、`/log` 无条件保留。
4. **失败安全**：config 拉取/解析失败时回退为现状（轮询全部），并保持现有 warn 行为——不能因新逻辑让诊断收集整体失效。
5. 注意 `get_config` 返回结构：先 curl 一下确认 JSON 形状（可能直接是字段平铺，也可能是嵌套），再写解析。

### 改动 2：status.html
1. 页面初始化时（documentReadyToRun 回调开头或单独一次请求）`GET /config?cmd=get_config`，把三个 enable 标志存到 JS 变量（如 `window.gaeEnabled`）。
2. `updateDetailStatus()` / `updateNoobStatus()`：`if (!gaeEnabled) { 跳过 gae_proxy 轮询 }`。Noob 区可显示一条中性信息（如"gae_proxy 未启用"或直接不更新，倾向不更新 + detail 表格显示占位）。
3. `OneKeyReport()`（按钮触发）：未启用时提示用户 gae_proxy 未启用、无法生成诊断（而不是报错弹窗）。
4. 保持现有 error 回调逻辑（模块启用但启动失败仍会走 error → 现有的"启动失败"提示保留）。
5. 该文件是 Django 模板（有 `{{ _( ) }}`），改动别破坏模板语法。

### 可选改动 3（默认保持现状，不主动做）
web_control.py 两处 "no module in path" 保持 DEBUG（已降级）。根修完成后噪音源消失，DEBUG 作为纵深防御即可。若用户要求恢复 WARN，仅改 191-193 与 268-271 两处 `xlog.debug` → `xlog.warn`（273-276 行的 `request module:%s start fail` WARN 一直没动，勿碰）。

## 验证步骤

1. `command python3 -m py_compile code/default/x_tunnel/local/upload_logs.py`
2. 触发收集器：`curl "http://127.0.0.1:8085/log_files"`（先 grep web_control.py 确认 `req_log_files` 的路由名），或用一键报告触发。
3. 检查 `data/launcher/logs/`（实际路径可 grep）launcher 日志：改动后不再出现新的 "no module in path"。
4. 浏览器开 `http://127.0.0.1:8085/status.html`（或经 WebUI 入口）：无 JS 报错，Noob/Detail 区行为正常；临时把 enable 改 true/false 各验一次更佳。
5. 回归：代理 204 检查（`curl -x http://127.0.0.1:8086 http://www.google.com/generate_204` -o /dev/null -w "%{http_code}"），TLS 行仍为 BoringSSL。
6. 完成后在 `~/Downloads/XX-Net-macOS-ARM64-修复记录.md` 追加"根修：按启用状态过滤模块轮询"一章（现状/改动/验证/局限：仅本地 /Applications/XX-Net，建议作者上游合并）。

## 历史背景（一段话）

macOS ARM64 上官方无包 → 本地编译 arm64 BoringSSL（指纹与官方 x86_64 一致）+ 一键安装脚本完成真实替换为 5.16.7；随后异常提示盘点修复了 4 处（start 启动提示、PyObjC/Pillow 安装提示、web_control 两处 WARN 降 DEBUG、req_debug_handler KeyError 竞态）。本任务是其延续的根修部分。前置文档 `~/Downloads/XX-Net-macOS-ARM64-修复记录.md` 有全部细节。
