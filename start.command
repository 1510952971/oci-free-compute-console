#!/bin/zsh

set -u

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR" || exit 1

clear
echo "OCI Free Compute Console"
echo "========================"
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "错误：没有找到 Python 3。请先从 https://www.python.org/downloads/ 安装 Python 3.11 或更高版本。"
  echo
  read "?按回车键退出..."
  exit 1
fi

if [[ ! -s "$HOME/.oci/config" ]]; then
  echo "首次运行需要在 OCI 控制台授权 API 密钥。"
  echo "详细中文步骤已经在浏览器中打开。完成下载后回到这里。"
  open "$SCRIPT_DIR/API_KEY_SETUP.html"
  echo
  read "?下载 OCI 配置文件和 PEM 私钥后，按回车键选择文件..."

  CONFIG_DOWNLOAD=$(osascript -e 'POSIX path of (choose file with prompt "选择从 OCI 下载的配置文件")') || exit 1
  PRIVATE_KEY=$(osascript -e 'POSIX path of (choose file with prompt "选择从 OCI 下载的 PEM 私钥")') || exit 1
  python3 install_oci_credentials.py "$CONFIG_DOWNLOAD" "$PRIVATE_KEY" || {
    echo
    read "?安装失败。按回车键退出..."
    exit 1
  }
  echo "OCI API 密钥安装完成。"
  echo
fi

if [[ ! -x .venv/bin/python ]]; then
  echo "正在创建 Python 虚拟环境..."
  python3 -m venv .venv || {
    echo "创建虚拟环境失败。"
    read "?按回车键退出..."
    exit 1
  }
fi

if ! .venv/bin/python -c 'import oci' >/dev/null 2>&1; then
  echo "正在安装 OCI 官方 SDK（首次运行需要联网）..."
  .venv/bin/python -m pip install -r requirements.txt || {
    echo "依赖安装失败，请检查网络后重试。"
    read "?按回车键退出..."
    exit 1
  }
fi

if [[ ! -f config.toml ]] || grep -q '<ocid1\.' config.toml; then
  echo "正在自动查询区域子网、ARM/AMD 镜像和可用域..."
  .venv/bin/python auto_configure.py || {
    echo "自动配置失败。请检查 OCI 控制台是否已有区域子网。"
    read "?按回车键退出..."
    exit 1
  }
fi

echo
echo "正在启动。状态面板将自动打开："
echo "http://127.0.0.1:8787"
echo
echo "保持此窗口运行；按 Ctrl+C 可停止。"
echo

(sleep 3; open http://127.0.0.1:8787) &
exec caffeinate -dimsu .venv/bin/python grab_a1.py
