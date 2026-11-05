# AgnesVideo 运维约定

> 本文件仅记录操作规范和禁止事项，不包含任何密钥、令牌或秘密内容。

## 一、服务管理

| 操作 | 规范 |
|---|---|
| 启动服务 | `systemctl start agnesvideo` |
| 停止服务 | `systemctl stop agnesvideo` |
| 重启服务 | `systemctl restart agnesvideo` |
| 查看状态 | `systemctl status agnesvideo` |
| 查看日志 | `journalctl -u agnesvideo -n 50 --no-pager` |
| 开机自启 | `systemctl enable agnesvideo`（已启用） |

**禁止事项：**
- 不得用 `kill` 直接终止 `python3 /opt/agnes-video/server.py` 进程
- 不得删除或重命名 `/etc/systemd/system/agnesvideo.service`
- 不得修改 `agnestudio.service` 中的 `User`、`Group`、`ExecStart` 字段

## 二、文件与目录权限

| 路径 | 要求 | 说明 |
|---|---|---|
| `/opt/agnes-video/` | 750 以上，属主 `root:root` 或 `agnestudio:agnestudio` | 代码目录 |
| `/opt/agnes-video/server.py` | 640 以上，属主同上 | 后端入口 |
| `/opt/agnes-video/index.html` | 640 以上，属主同上 | 前端页面 |
| `/etc/agnes-video/agnestudio.env` | 600 root:root | 含 `AGNES_API_TOKEN`，**不得**放宽到 644 |
| `/var/lib/agnes-video/data/` | 700 agnestudio:agnestudio | 含 `config.json`（上游 API Key） |
| `/var/lib/agnes-video/outputs/` | 700 agnestudio:agnestudio | 任务输出 |

**禁止事项：**
- 不得将 `agnestudio.env` 权限放宽到 644 或以上
- 不得将 `/var/lib/agnes-video/` 属主改为非 `agnestudio` 用户
- 不得将任何密钥文件复制到 `/opt/agnes-video/` 或 `/www/wwwroot/`

## 三、端口与网络

| 端口 | 归属 | 操作规范 |
|---|---|---|
| `8090` | AgnesVideo 专用，仅 `127.0.0.1` | 不得改为 `0.0.0.0:8090` |
| `18090` | AgnesVideo SSH 隧道本地端口 | 仅用于本机访问，不得映射到公网 |
| `80` / `8889` / `443` / `8443` / `11435` | 其他项目 | **不得**在 AgnesVideo 操作中修改 |

**禁止事项：**
- 不得在 Nginx 中为 `/api/` 添加指向 `127.0.0.1:8090` 的反代（除非单独批准）
- 不得修改 Xray 或 Agnes 代理的监听端口
- 不得修改防火墙规则

## 四、SSH 隧道规范

```bash
# 启动（带 keepalive，防止 Android 系统回收）
ssh -N \
  -o ServerAliveInterval=15 \
  -o ServerAliveCountMax=3 \
  -o TCPKeepAlive=yes \
  -L 18090:127.0.0.1:8090 root@210.126.238.200
```

| 检查项 | 命令 |
|---|---|
| 隧道是否存活 | `ps -ef \| grep "ssh.*18090" \| grep -v grep` |
| 本地端口是否监听 | `(echo > /dev/tcp/127.0.0.1/18090) 2>/dev/null && echo open` |
| 根路径是否可达 | `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:18090/` |

**禁止事项：**
- 不得将 SSH 隧道映射到公网端口
- 不得修改 `~/.ssh/` 下的认证配置

## 五、密钥与令牌管理

| 文件 | 位置 | 操作规范 |
|---|---|---|
| 网站访问令牌（本地） | `~/.agens/video-site-token` | 600 权限，不得复制到日志或聊天 |
| 上游 API Key（远端） | `/var/lib/agnes-video/data/config.json` | 700 权限，仅 `agnestudio` 用户可读 |
| 上游 API Key（本地调试） | `data/config.json`（项目目录） | 600 权限，不得提交到版本库 |

**禁止事项：**
- 不得在任何文档、日志、聊天消息中记录令牌或 API Key 内容
- 不得通过剪贴板、截图等方式将令牌暴露给非授权应用

## 六、更新部署规范

1. 修改本地 `index.html` 或 `server.py` 前，先备份到 `backups/YYYYMMDD_<说明>/`
2. 上传前确认远端目标文件是否存在；若存在，先创建带时间戳的 `.bak` 备份
3. 上传后校验 SHA-256 与本地一致
4. 不重启服务（除非明确需要）；`index.html` 修改后无需重启即可生效
5. 不得在部署过程中修改 `/etc/agnes-video/agnestudio.env`

## 七、与其他项目的边界

| 项目 | 归属 | 与 AgnesVideo 的关系 |
|---|---|---|
| Nginx 静态站点（`/www/wwwroot/video/`） | 共享服务器 | 仅静态文件，不含 API 后端 |
| Gemini 代理（`/root/agens/`，端口 11435） | 其他项目 | 完全独立，不得在 AgnesVideo 操作中介入 |
| Xray（`/usr/local/etc/xray/`） | 其他项目 | 完全独立 |
| 宝塔面板（端口 8889） | 其他项目 | 完全独立 |

**核心原则：AgnesVideo 的所有操作不得影响上述项目的配置、进程或端口。**
