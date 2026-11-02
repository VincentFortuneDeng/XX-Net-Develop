# 独立验收修复部署记录

日期：2026-11-03  
授权：用户明确回复“请进行部署”。  
范围：仅部署独立验收修复的 6 个 Python 运行文件，不包含测试、文档或其他历史文件。

## 备份

目录：`backups/pre-rsync-20261103-independent-acceptance/`

- 6 份生产原始文件：对应路径下 `*.orig`。
- 6 份部署差异：对应路径下 `*.diff`。
- 原始文件清单：`SHA256SUMS`。
- 部署后文件清单：`DEPLOYED-SHA256SUMS`。
- 未覆盖或删除任何既有备份。

## 已部署文件

1. `code/default/lib/noarch/front_base/http_dispatcher.py`
2. `code/default/lib/noarch/xlog.py`
3. `code/default/smart_router/local/dns_query.py`
4. `code/default/smart_router/local/pipe_socks.py`
5. `code/default/x_tunnel/local/front_dispatcher.py`
6. `code/default/x_tunnel/local/upload_logs.py`

## 部署时核验与边界（重启前）

- 6 份备份均与原始 SHA-256 清单匹配。
- 6 个 `/Applications/XX-Net` 生产文件均与开发快照逐字节一致。
- 部署清单已写入 `DEPLOYED-SHA256SUMS`。
- **未重启 Launcher，当前运行进程尚未加载新文件。**
- 未把文件部署等同于生产疗效；重启后的加载、日志、重试、DNS 和诊断包行为仍需核验。
- 未 push、未提交、未修改 F04 旧 HTTP/1.1 长连接问题。

## 重启后生产核验

- 用户授权核验后确认 Launcher 已从旧 PID 5445 切换为 **PID 33775**，启动时间 **2026-11-03 06:29:55**；6 个部署文件哈希仍全部匹配。
- 生产黑盒：**10/10 PASS**。包括 arm64 BoringSSL、13 个最新部署文件来源、配置契约、禁用模块零请求、六模块视图、并发 `/debug` 和代理 `generate_204=204`。
- 默认 HTTP/1.1 的 24 路并发 `/debug` 首次复验全部 200；另外重复三轮也均为 **24/24 HTTP 200**。显式 `Connection: close` 的 24 个请求同样全部 200。
- 六个模块在 06:29:55 之后的 warning 视图中均无 ERROR/CRITICAL。`seley_front` 搜到的一条 Traceback 时间为 00:19:03，属于重启前旧记录。
- **F04 状态：** 历史 FAIL，本次重启后未复现；未修改其根因，因此只能记录“本轮 PASS”，不能宣称旧长连接问题已修复。
- **仍为 UNVERIFIED：** 非 UTF-8 业务 bytes、DNS close 异常、stop 清理异常和畸形配置脱敏的生产故障路径未主动注入；GUI、诊断包实际上传和特殊远端故障也未覆盖。
