# Termux AI Video Studio — Agens 版

这是手机端本地 Web 控制面板，后端直接调用 Agens 视频 API，不再调用 Replicate。

## 功能
- 中文手机网页 UI
- Agens 上游 API Key 本机保存（data/config.json 权限 600，目录 700）
- **网站访问令牌（AGNES_API_TOKEN）认证所有 /api/ 路由**
- 令牌只存于浏览器 sessionStorage，不写入网页源码或后端配置
- 认证测试（不会创建视频任务）
- 文生视频 / 参考图片转视频任务提交
- 使用返回的 `video_id` 查询 `https://apihub.agnes-ai.com/agnesapi?video_id=...&model_name=...`
- 5 秒刷新一次真实状态，展示服务端返回的 progress（若有）与视频 URL
- 任务历史与失败信息（不再保存完整上游响应）
- 仅监听 127.0.0.1，不对局域网或公网开放

## 环境变量

| 变量 | 说明 | 默认值 |
|---|---|---|
| `AGNES_DATA_DIR` | 数据目录（config.json、jobs.json） | `<代码目录>/data` |
| `AGNES_OUTPUT_DIR` | 输出目录 | `<代码目录>/outputs` |
| `AGNES_API_TOKEN` | 网站访问令牌（必须设置，否则所有 /api/ 路由返回 401） | 空 |
| `PORT` | 监听端口 | `8090` |

## 安装（本地 Termux）
1. 下载 ZIP 并解压到 Termux 可访问的目录。
2. 在 Termux 运行：
   ```bash
   pkg update
   pkg install python unzip
   ```
3. 设置网站访问令牌并启动：
   ```bash
   export AGNES_API_TOKEN=<your-website-access-token>
   bash start.sh
   ```
4. 保持 Termux 会话运行，在手机浏览器打开 `http://127.0.0.1:8090`。
5. 进入「API 设置」，先粘贴 **网站访问令牌（AGNES_API_TOKEN）** 并保存。
6. 再粘贴 Agens 上游 API Key 并保存。
7. 先点击「测试 API 认证」。该测试不会生成视频；如果模型列表接口不支持，可能会报错，仍需根据错误进一步验证。
8. 确认账户套餐、模型可用性与剩余额度后，再提交测试视频。

## Debian 12 部署（推荐目录分离）

代码目录与私有数据目录分离：

```
/opt/agnes-video/          # 代码（server.py、index.html、start.sh）
/var/lib/agnes-video/      # 私有数据（data/、outputs/）
```

权限要求：

```
/opt/agnes-video            root:root   0755
/opt/agnes-video/*         root:root   0644
/var/lib/agnes-video       agnestudio:agnestudio  0700
/var/lib/agnes-video/data  agnestudio:agnestudio  0700
/var/lib/agnes-video/data/config.json  agnestudio:agnestudio  0600
/var/lib/agnes-video/outputs  agnestudio:agnestudio  0700
```

systemd 单元示例：

```ini
[Unit]
Description=Agnes Video Studio
After=network.target

[Service]
Type=simple
User=agnestudio
Group=agnestudio
WorkingDirectory=/opt/agnes-video
EnvironmentFile=/etc/agnes-video/agnestudio.env
ExecStart=/usr/bin/python3 /opt/agnes-video/server.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`/etc/agnes-video/agnestudio.env` 内容（权限 0600）：

```bash
AGNES_API_TOKEN=<your-website-access-token>
AGNES_DATA_DIR=/var/lib/agnes-video/data
AGNES_OUTPUT_DIR=/var/lib/agnes-video/outputs
PORT=8090
```

Nginx 反代配置（仅本机访问）：

```nginx
server {
    listen 80;
    server_name your-domain.example;

    location /api/ {
        proxy_pass http://127.0.0.1:8090;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
    }
}
```

## API 说明
- 创建：`POST https://apihub.agnes-ai.com/v1/videos`
- 查询：`GET https://apihub.agnes-ai.com/agnesapi?video_id=VIDEO_ID&model_name=MODEL`
- 默认模型：`agnes-video-v2.0`
- `agnes-video-v2.0`：保留旧版文档路径，默认请求 1152x768、81 帧、24 fps，图生视频使用 `image` 字段；未验证当前官方 schema 是否仍接受这些参数。
- `agnes-video-2.5` 与 `agnes-video-2.5-flash`：使用官方确认参数，文生视频发送 `mode="text"`，图生视频发送 `mode="keyframe"` 和 `first_frame`；不发送 `width`、`height`、`num_frames`、`frame_rate`；`seconds` 使用字符串，默认 `"5"`，限制 `4` 到 `12`；Flash 固定 `size="720P"`。

## 验证
运行以下本地命令，不调用真实视频生成 API，不消耗额度：
```bash
python -m py_compile server.py
bash -n start.sh
grep -n -e 'AGNES_API_TOKEN' -e 'AGNES_DATA_DIR' -e 'AGNES_OUTPUT_DIR' -e 'hmac.compare_digest' server.py
```

## 重要提示
- 仅创建任务成功并返回 video_id 才会显示任务已提交。
- 任务进度完全来自服务端，不会模拟进度或渲染帧。
- 网站访问令牌（AGNES_API_TOKEN）与 Agens 上游 API Key 完全分离，不得混用。
- 网站访问令牌只存于浏览器 sessionStorage，不会写入网页源码或后端配置。
- Agens 上游 API Key 会留在本机 `data/config.json` 中（权限 0600）。不要把服务监听改成 `0.0.0.0`，不要分享 `config.json`。
- 视频生成会消耗账户配额。不要因"每日 500 秒"而认为每个模型或每个请求都必定可用；以账户控制台与接口响应为准。
- 图生视频的不同模型字段可能不同。此版本的 legacy `agnes-video-v2.0` 使用 `image` 字段；2.5 系列使用 `mode=keyframe`、`first_frame`。如果返回参数错误，需按该模型最新 API schema 调整。
