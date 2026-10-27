==========================================================
 XX-Net 5.16.7  macOS ARM64 (Apple Silicon) 原生安装包
 原生 BoringSSL TLS 指纹库 - 无需 Rosetta 2
==========================================================

【包内容】
  install_xxnet_arm64.sh   一键安装脚本 (本包唯一需要执行的东西)
  XX-Net-5.16.7.zip        XX-Net 官方 5.16.7 源码包
  boringssl.abi3.so        ARM64 原生编译的 BoringSSL 库
                           (TLS 指纹与官方 x86_64 版完全一致, 兼容
                            Python 3.10 ~ 3.15)

三个文件必须放在同一个目录里。


【前提条件】
  1. Apple Silicon Mac (M1/M2/M3/M4/M5/M6, arm64)
  2. Python 3.10 ~ 3.15
     推荐用 Homebrew 安装:  brew install python
     或官网下载: https://www.python.org/downloads/macos/
     (没有 Python 时脚本会打印安装指引并退出, 装好再运行即可)


【运行方式】二选一

  方式一: 双击
    在访达中双击 install_xxnet_arm64.sh
    (若提示无法打开, 请在"系统设置-隐私与安全性"中允许)

  方式二: 终端 (推荐)
    打开 终端 (Terminal), 执行:
        cd 拖入本目录
        ./install_xxnet_arm64.sh


【安装过程】
  脚本会依次:
  1. 检查系统架构与 Python 环境
  2. 自动安装 Python 依赖 (cffi/pyobjc/pillow/pycryptodome)
  3. 询问安装位置:
       [1] /Applications/XX-Net   标准应用位置
       [2] 当前目录/XX-Net        便携位置
     直接回车默认选 [2]
  4. 若目标位置已有旧版本, 会询问是否替换:
       - 替换前自动把旧版完整备份为
         ~/Downloads/XX-Net-backup-日期.zip (不含运行日志)
       - 旧版的配置和 IP 池 (data/ 目录) 会迁入新版
       - 不替换则放弃安装, 原样不动
  5. 安装完成, 不自动启动。


【启动方法】
  打开 终端 (Terminal), 执行 (二选一):

      cd /Applications/XX-Net && ./start
      (选了安装到 /Applications 时)

      cd 你选择的目录/XX-Net && ./start
      (选了安装到当前目录时)

  启动后:
    - 控制台出现  TLS implementation: BoringSSL
      表示原生指纹库加载成功
    - 浏览器代理: HTTP 127.0.0.1:8086
    - 管理页面: http://127.0.0.1:8086/module/launcher
    - 停止: 在运行 XX-Net 的终端里按 Ctrl+C

  注: 若启动时打印 "Using system python, TLS will fall back
  to system SSL" 属官方 start 脚本的过时提示, 可忽略;
  以运行日志中的 "TLS implementation: BoringSSL" 为准。


【常见问题】
  Q: 提示 "operation not permitted" / 无法运行?
  A: 若从网上下载的文件被 Gatekeeper 拦截, 执行:
         xattr -c -r 解压出的目录路径
     (脚本安装时也会自动清一次隔离标记)

  Q: Python 版本不对?
  A: 脚本支持 3.10~3.15。更旧/更新请先 brew install python。

  Q: 想卸载?
  A: 直接删除安装目录即可; 依赖装在
     ~/Library/Python/3.x/, 不影响系统。

  Q: 换机/重装, 旧配置如何保留?
  A: 备份安装目录下的 data/ 文件夹, 装好新版后放回原位即可。
