# AgnesVideo 专用资源清单

> 本文档仅记录已核实的项目资源、端口和服务名，不包含任何密钥、令牌或秘密内容。

## 本地项目目录

| 资源 | 路径 / 值 | 说明 |
|---|---|---|
| 项目根目录 | `~/termux_ai_video_studio/termux_ai_video_studio` | AgnesVideo 源码 |
| 前端页面 | `index.html` | 单页前端，由后端 `GET /` 直接提供 |
| 后端入口 | `server.py` | Python 标准库 HTTP 服务 |
| 本地数据 | `data/config.json` | 本地调试用上游 API Key 存储（600 权限） |
| 本地令牌 | `~/.agens/video-site-token` | 网站访问令牌（600 权限，不在此文件记录内容） |

## 本地端口

| 端口 | 用途 | 归属 |
|---|---|---|
| `18090` | SSH 隧道本地端口（转发到远端 8090） | AgnesVideo 专用 |
| `8090` | 本地后端端口（仅本地调试时开启） | AgnesVideo 专用 |
| `11435` | Gemini 协议翻译代理（其他项目） | **非 AgnesVideo** |

## 远端资源（210.126.238.200）

| 资源 | 路径 / 值 | 权限 | 说明 |
|---|---|---|---|
| 远端代码目录 | `/opt/agnes-video/` | 755 root:root | 含 `server.py` 和 `index.html` |
| 远端环境变量 | `/etc/agnes-video/agnestudio.env` | 600 root:root | 含 `AGNES_API_TOKEN` 等，不在此记录内容；`AGNES_PLAY_SECRET`（可选，签名播放/下载）是否已配置**待远端核对** |
| 远端数据目录 | `/var/lib/agnes-video/data/` | 700 agnestudio:agnestudio | 含 `config.json`（上游 API Key） |
| 远端任务目录 | `/var/lib/agnes-video/outputs/` | 700 agnestudio:agnestudio | 视频任务输出 |
| 远端服务 | `agnestudio.service` | systemd | 运行用户：`agnestudio`，监听 `127.0.0.1:8090` |
| 远端监听地址 | `127.0.0.1:8090` | 仅回环 | 不暴露公网 |

## 远端端口

| 端口 | 用途 | 归属 |
|---|---|---|
| `8090` | AgnesVideo 后端（仅 127.0.0.1） | AgnesVideo 专用 |
| `80` | Nginx（静态站点 + 其他服务） | **非 AgnesVideo** |
| `443` / `8443` | Xray | **非 AgnesVideo** |
| `8889` | 宝塔面板 | **非 AgnesVideo** |
| `11435` | Gemini 代理（回环） | **非 AgnesVideo** |

## 服务进程

| 进程 | 用户 | 说明 |
|---|---|---|
| `/usr/bin/python3 /opt/agnes-video/server.py` | `agnestudio` | AgnesVideo 后端，systemd 管理 |
| `nginx` | `root` / `www` | 静态站点，非 AgnesVideo |
| `xray` | `nobody` | 代理，非 AgnesVideo |
| `node /root/agens/gemini-proxy.js` | `root` | Gemini 代理，非 AgnesVideo |

## 明确不属于 AgnesVideo 的资源

- Nginx 站点（`/www/server/panel/vhost/nginx/210.126.238.200.conf`）
- Xray（`/usr/local/etc/xray/config.json`）
- Gemini 代理（`/root/agens/gemini-proxy.js`）
- 宝塔面板（`0.0.0.0:8889`）
- 任何 `11435` 端口的服务
