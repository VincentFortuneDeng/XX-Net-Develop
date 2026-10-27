#!/bin/bash
# =============================================================
#  XX-Net 5.16.7 macOS ARM64 原生安装脚本
#  用途: 将 XX-Net-5.16.7.zip 源码 + arm64 版 BoringSSL 库
#        安装到 /Applications/XX-Net 或当前目录下的 XX-Net/
#  仅安装, 不启动。启动方式见脚本结束时的提示。
# =============================================================
set -u

if [ -t 1 ]; then
    C_GREEN=$'\033[0;32m'; C_YELLOW=$'\033[0;33m'; C_RED=$'\033[0;31m'; C_CYAN=$'\033[0;36m'; C_OFF=$'\033[0m'
else
    C_GREEN=''; C_YELLOW=''; C_RED=''; C_CYAN=''; C_OFF=''
fi
say_ok()   { printf "  ${C_GREEN}[OK]${C_OFF} %s\n" "$1"; }
say_warn() { printf "  ${C_YELLOW}[警告]${C_OFF} %s\n" "$1"; }
say_err()  { printf "  ${C_RED}[错误]${C_OFF} %s\n" "$1"; }
say_head() { printf "\n${C_CYAN}== %s ==${C_OFF}\n" "$1"; }

ZIP_NAME="XX-Net-5.16.7.zip"
LIB_NAME="boringssl.abi3.so"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "============================================================"
echo "   XX-Net 5.16.7  macOS ARM64 (Apple Silicon) 原生安装器"
echo "   原生 BoringSSL TLS 指纹库 - 无需 Rosetta 2"
echo "============================================================"

# ---------- 1. 平台检查 ----------
say_head "步骤 1/6  系统环境检查"
ARCH="$(uname -m)"
if [ "$ARCH" != "arm64" ]; then
    say_err "本脚本仅适用于 Apple Silicon (arm64) Mac, 当前架构: $ARCH"
    printf "  Intel Mac 请使用官方发布包。按回车退出..."; read _
    exit 1
fi
say_ok "Apple Silicon (arm64) Mac"

OSVER="$(sw_vers -productVersion 2>/dev/null || echo unknown)"
say_ok "macOS $OSVER"

# ---------- 2. Python 检查 ----------
PY3=""
for c in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if command -v "$c" >/dev/null 2>&1; then
        v="$("$c" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)"
        case "$v" in
            3.1[0-9]|3.[2-9]) PY3="$c"; PYVER="$v"; break ;;
        esac
    fi
done
if [ -z "$PY3" ]; then
    say_err "未找到 Python 3.10 ~ 3.15"
    printf "
  请先安装 Python (任选其一):

    方式一 (推荐): 安装 Homebrew 后执行
        /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"
        brew install python

    方式二: 官网下载安装包
        https://www.python.org/downloads/macos/

  装好后重新运行本脚本即可。
"
    printf "  按回车退出..."; read _
    exit 1
fi
say_ok "Python $PYVER  ($PY3)"

# ---------- 3. 检查安装源 ----------
say_head "步骤 2/6  检查安装文件"
ZIP_PATH="$SCRIPT_DIR/$ZIP_NAME"
if [ ! -f "$ZIP_PATH" ]; then
    say_err "未找到 $ZIP_NAME (脚本要求它与本脚本放在同一目录)"
    printf "  当前脚本目录: %s\n" "$SCRIPT_DIR"
    printf "  请从 https://github.com/XX-net/XX-Net/releases 下载 5.16.7 的 Source code (zip)\n"
    printf "  按回车退出..."; read _
    exit 1
fi
say_ok "$ZIP_NAME"
if [ ! -f "$SCRIPT_DIR/$LIB_NAME" ]; then
    say_err "未找到 $LIB_NAME (arm64 BoringSSL 库必须与本脚本同目录)"
    printf "  按回车退出..."; read _
    exit 1
fi
say_ok "$LIB_NAME (arm64 原生 TLS 指纹库)"

# ---------- 4. 选择安装位置 ----------
say_head "步骤 3/6  选择安装位置"
TARGET_ROOT="$SCRIPT_DIR/XX-Net"
MODE="cwd"
echo "  [1] /Applications/XX-Net    (标准应用位置, 可开机自动启动)"
echo "  [2] 当前目录/XX-Net          ($SCRIPT_DIR/XX-Net)"
printf "\n  请输入选择 [1/2, 默认 2]: "
read choice
case "$choice" in
    1) TARGET_ROOT="/Applications/XX-Net"; MODE="app" ;;
    *) TARGET_ROOT="$SCRIPT_DIR/XX-Net"; MODE="cwd" ;;
esac

REPLACE="no"
old_ver=""
if [ -d "$TARGET_ROOT" ]; then
    echo ""
    old_ver=""
    [ -f "$TARGET_ROOT/code/default/version.txt" ] && old_ver="$(cat "$TARGET_ROOT/code/default/version.txt" 2>/dev/null)"
    say_warn "目标已存在: $TARGET_ROOT ${old_ver:+(版本 $old_ver)}"
    printf "  是否替换? 旧版会先自动备份到 ~/Downloads/ (您的配置数据会保留)\n"
    printf "  输入 y 替换 / 其他任意键放弃安装: "
    read ans
    case "$ans" in
        y|Y) REPLACE="yes" ;;
        *)  say_ok "已放弃安装, 未做任何修改。"; exit 0 ;;
    esac
fi

# ---------- 5. 安装依赖 ----------
say_head "步骤 4/6  安装 Python 依赖 (cffi/pyobjc/pillow/pycryptodome)"
# Homebrew Python (PEP 668 externally-managed) 需要 --break-system-packages;
# 旧版 pip 不认识该参数, 故先带参尝试再退回普通方式
if "$PY3" -m pip install --user --quiet --disable-pip-version-check --break-system-packages \
      cffi pyobjc pillow pycryptodome 2>/dev/null \
   || "$PY3" -m pip install --user --quiet --disable-pip-version-check \
      cffi pyobjc pillow pycryptodome 2>/dev/null; then
    say_ok "依赖安装完成"
else
    say_warn "部分依赖安装失败 (不影响安装继续)"
    printf "   ${C_YELLOW}-${C_OFF} XX-Net 对缺失依赖有纯 Python 降级方案, 仅影响性能/托盘\n"
    printf "   ${C_YELLOW}-${C_OFF} 可稍后手动执行: %s -m pip install --user --break-system-packages cffi pyobjc pillow pycryptodome\n" "$PY3"
fi

# ---------- 6. 执行安装 ----------
say_head "步骤 5/6  安装 XX-Net"
WORK="$(mktemp -d /tmp/xxnet_install.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
OLD_DATA=""

if [ "$REPLACE" = "yes" ]; then
    # --- 备份旧版 (排除巨大的 warning 日志) ---
    BK="$HOME/Downloads/XX-Net-backup-$(date +%Y%m%d-%H%M%S).zip"
    printf "  备份旧版到: %s (排除运行日志, 可能需要几分钟)...\n" "$BK"
    STAGE="$WORK/backup_stage"
    mkdir -p "$STAGE"
    if rsync -a \
        --exclude '*.log' \
        "$TARGET_ROOT/" "$STAGE/XX-Net/" 2>/dev/null \
       && (cd "$STAGE" && zip -qr "$BK" XX-Net) \
       && [ -s "$BK" ]; then
        say_ok "旧版备份完成"
    else
        rm -f "$BK"
        say_warn "zip 备份失败, 改为完整文件夹备份 (含日志, 较慢)"
        BK_DIR="$HOME/Downloads/XX-Net-backup-$(date +%Y%m%d-%H%M%S)"
        if ditto "$TARGET_ROOT" "$BK_DIR" 2>/dev/null; then
            say_ok "旧版备份完成 (文件夹): $BK_DIR"
        else
            say_err "备份失败, 已中止安装 (原版未动)"
            printf "  按回车退出..."; read _
            exit 1
        fi
    fi

    # --- 迁移数据 (排除运行日志, 避免把数 GB 日志带入新版) ---
    if [ -d "$TARGET_ROOT/data" ]; then
        say_ok "复制旧配置数据 (data/, 排除运行日志) 用于迁移"
        mkdir -p "$WORK/migrate_data"
        rsync -a --exclude '*.log' "$TARGET_ROOT/data/" "$WORK/migrate_data/" 2>/dev/null \
            || cp -R "$TARGET_ROOT/data" "$WORK/migrate_data"
        OLD_DATA="yes"
    fi
    rm -rf "$TARGET_ROOT"
fi

# --- 解压新版 ---
say_ok "解压 $ZIP_NAME ..."
mkdir -p "$WORK/unzip"
if ! ditto -x -k "$ZIP_PATH" "$WORK/unzip/" 2>/dev/null; then
    say_err "解压失败, 安装中止"
    [ -n "$OLD_DATA" ] && say_warn "旧数据保留在: $WORK/migrate_data (临时目录, 请尽快手动转移)"
    printf "  按回车退出..."; read _
    exit 1
fi
SRC="$(command ls -d "$WORK/unzip"/*/ 2>/dev/null | command head -1)"
[ -z "$SRC" ] && SRC="$WORK/unzip"
if [ ! -f "$SRC/start" ]; then
    say_err "压缩包内容异常 (未找到 start), 安装中止"
    printf "  按回车退出..."; read _
    exit 1
fi

# --- 放置 ARM64 BoringSSL ---
mkdir -p "$SRC/code/default/lib/darwin"
cp "$SCRIPT_DIR/$LIB_NAME" "$SRC/code/default/lib/darwin/$LIB_NAME"
say_ok "安装 arm64 BoringSSL 库到 lib/darwin/"

# --- 合入数据迁移 ---
if [ -d "$WORK/migrate_data" ]; then
    cp -R "$WORK/migrate_data" "$SRC/data"
    say_ok "旧配置数据 (data/) 已迁入新版"
fi

# --- 清除 macOS 隔离标记 ---
xattr -c -r "$SRC" 2>/dev/null
say_ok "已清除 macOS 隔离标记"

# --- 就位 ---
if [ "$MODE" = "app" ]; then
    mv "$SRC" "$TARGET_ROOT"
else
    mv "$SRC" "$TARGET_ROOT"
fi
say_ok "已安装到: $TARGET_ROOT"

# ---------- 7. 完成 ----------
say_head "步骤 6/6  完成"
cat <<EOF

  安装完成!  $TARGET_ROOT
  $( [ -n "$old_ver" ] && echo "  已从 $old_ver 升级, 您的配置与 IP 池已保留" )

  ${C_GREEN}如何启动:${C_OFF}
    打开 终端 (Terminal), 执行:

        cd "$TARGET_ROOT" && ./start

  启动后:
    - 控制台出现  TLS implementation: BoringSSL  表示指纹库加载成功
    - 浏览器代理设置: HTTP 127.0.0.1:8086  (或自动配置模式)
    - 管理页面: http://127.0.0.1:8086/module/launcher

  停止: 在运行 XX-Net 的终端里按 Ctrl+C

EOF
[ "$MODE" = "app" ] && printf "  提示: 也可直接双击 %s/start 运行 (推荐终端启动, 便于查看日志)\n\n" "$TARGET_ROOT"
printf "  按回车关闭..."; read _
exit 0
