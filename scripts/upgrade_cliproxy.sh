#!/bin/bash
set -e

echo "=================================================="
echo " 开始升级 CLIProxyAPI 至 v7.3.20 (Darwin ARM64)"
echo "=================================================="

TMP_DIR=$(mktemp -d)
echo "1. 临时解压目录: $TMP_DIR"

echo "2. 正在从 GitHub 下载 v7.3.20 安装包..."
curl -fSL -H "User-Agent: curl/7.88.1" -o "$TMP_DIR/cliproxy.tar.gz" "https://github.com/router-for-me/CLIProxyAPI/releases/download/v7.3.20/CLIProxyAPI_7.3.20_darwin_aarch64.tar.gz"

echo "3. 正在解压安装包..."
tar -xzf "$TMP_DIR/cliproxy.tar.gz" -C "$TMP_DIR"

if [ ! -f "$TMP_DIR/cli-proxy-api" ]; then
    echo "❌ 解压失败，未找到 cli-proxy-api 可执行文件"
    rm -rf "$TMP_DIR"
    exit 1
fi

echo "4. 备份当前 7.3.8 版本..."
cp -f /Users/scr/.local/bin/cli-proxy-api /Users/scr/.local/bin/cli-proxy-api.backup-v7.3.8
echo "   已备份至 /Users/scr/.local/bin/cli-proxy-api.backup-v7.3.8"

echo "5. 替换新版本二进制..."
chmod +x "$TMP_DIR/cli-proxy-api"
xattr -c "$TMP_DIR/cli-proxy-api" 2>/dev/null || true
mv -f "$TMP_DIR/cli-proxy-api" /Users/scr/.local/bin/cli-proxy-api
rm -rf "$TMP_DIR"

echo "6. 重启 launchd 服务..."
launchctl kickstart -k "gui/$(id -u)/com.zhijian.codex-cli-model-bridge-cliproxyapi"

sleep 2

echo "=================================================="
echo " 验证新版本状态"
echo "=================================================="
/Users/scr/.local/bin/cli-proxy-api -version 2>&1 | head -n 1
echo ""
echo "检查 8317 端口健康度:"
curl -s -o /dev/null -w "HTTP 状态码: %{http_code}\n" http://127.0.0.1:8317/v1/models
echo "=================================================="
echo "🎉 升级完成！"
