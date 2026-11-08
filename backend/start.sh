#!/bin/bash
set -e

# 启动前检查：AGNES_API_TOKEN 必须已设置且非空（仅检查是否为空，不打印其值）。
# 必须先于任何副作用（cd / mkdir / chmod / 文件写入 / 启动服务）执行。
if [ -z "${AGNES_API_TOKEN:-}" ]; then
    echo "错误：未设置 AGNES_API_TOKEN（网站访问令牌），无法启动。" >&2
    echo "请先在执行环境设置该变量，例如（值请自行生成，勿提交到版本库/聊天）：" >&2
    echo "  export AGNES_API_TOKEN=<your-website-access-token>" >&2
    echo "本脚本不会读取、打印或修改该令牌的存储。" >&2
    exit 1
fi

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
