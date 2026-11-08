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
| 签名播放秘密（远端） | `/etc/agnes-video/agnestudio.env` 中的 `AGNES_PLAY_SECRET` 行（可选，未配置 = 签名播放/下载关闭，fail closed） | 至少 32 字节高熵随机 hex；文件 0600 且属主 `agnestudio:agnestudio`；不得复制到 `/opt/agnes-video/`、日志、聊天或版本库；变更需在维护窗口重启 `agnesvideo` 才生效；更换后旧签名链接立即失效（预期行为）。**disposition 契约**：签名 URL 的 `disposition` 字段只允许 `inline`/`attachment` 且被 HMAC 绑定——`/playlink` 默认签发 inline（播放器），下载按钮签发 attachment；客户端篡改 `disposition` 参数即验签失败（invalid/malformed → 403），不得为"方便"把 disposition 改成未签名的自由参数；旧四字段（无 disposition）签名兼容但仅按 inline 生效；`/api/download` 的 Bearer 路径固定 inline，`disposition` 参数对其无效；启用步骤与无密验证方法见 `README_CN.md`「启用 AGNES_PLAY_SECRET」及「签名 URL 的 inline / attachment 契约」小节 |

**禁止事项：**
- 不得在任何文档、日志、聊天消息中记录令牌、API Key 或 `AGNES_PLAY_SECRET` 的内容
- 不得通过剪贴板、截图等方式将令牌暴露给非授权应用
- 不得为了"让签名播放链接可用"而关闭 `/api/download` 鉴权、把 Bearer 令牌写进 URL、或把 `AGNES_API_TOKEN` 复用为 `AGNES_PLAY_SECRET`

**/api/download 的 GET / HEAD / Range 与 method 绑定：**
- GET：无 Range → 200 完整正文；合法 `Range: bytes=…` → 206 + `Content-Range`；越界/无法满足 → 416；非法/多段/非 `bytes=` → 按 200 完整返回。
- HEAD：仅元数据探测，**复用生产签发的 GET 签名**（`?method=GET` 的 playlink URL）；响应含完整资源头（`Content-Type`/`Accept-Ranges`/`Content-Length`=完整大小/`Content-Disposition`/`Referrer-Policy: no-referrer`），**不发送正文**，且**忽略请求中的 Range**。
- 无"HEAD 专用签名"：签发端只签 `method=GET`，验证端强制签名 `method` 必须为 `GET`；`method=HEAD` 的签名对 GET 与 HEAD 一律 403，HEAD 签名绝不能执行 GET 下载，也不削弱 GET 方法绑定。
- Bearer 路径固定 inline；未配置 `AGNES_PLAY_SECRET` 且无 Bearer 时 fail closed（403）。
- `/api/download` 以外的 `/api/*` 路由 HEAD 仍返回 501。详见 `README_CN.md`「/api/download 的 GET / HEAD / Range 契约」小节。

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
