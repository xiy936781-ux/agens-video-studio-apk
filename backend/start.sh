#!/bin/bash
set -e
cd "$(dirname "$0")"
mkdir -p data outputs
chmod 700 data outputs 2>/dev/null || true

# 如需使用 Debian 分离目录，可设置以下环境变量（Debian 部署说明见 README_CN.md）：
#   export AGNES_DATA_DIR=/var/lib/agnes-video/data
#   export AGNES_OUTPUT_DIR=/var/lib/agnes-video/outputs
# 网站访问令牌（必须设置，且不能与 Agnes 上游 API Key 相同）：
#   export AGNES_API_TOKEN=<your-website-access-token>

echo "启动 Agens 视频工作室。保持此窗口运行。"
echo "手机浏览器打开：http://127.0.0.1:8090"
echo "提示：所有 /api/ 路由需要 Authorization: Bearer <AGNES_API_TOKEN>"
python server.py
