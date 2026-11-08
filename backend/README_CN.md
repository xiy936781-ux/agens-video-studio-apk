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
| `AGNES_PLAY_SECRET` | 可选：签名播放/下载秘密（独立于 `AGNES_API_TOKEN`，至少 32 字节高熵随机值，**服务端机密，不是浏览器配置**）。未设置时 `GET /api/jobs/<jid>/playlink` 返回 503、`/api/download` 的签名播放 URL 一律 403 fail closed；Bearer 下载不受影响 | 空 |
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
# 可选：签名播放/下载秘密（启用后 <video> 与"下载合并视频"可不带 Bearer 直接播放/下载）
# 空值 = 功能关闭，playlink 返回 503、签名 URL 一律 403（fail closed）。
AGNES_PLAY_SECRET=
```

### 启用 AGNES_PLAY_SECRET（签名播放/下载）

`AGNES_PLAY_SECRET` 是**服务端秘密**（写入 `agnestudio.env`，由 systemd 加载），不是浏览器配置，绝不能写进网页源码或放进 URL；它独立于 `AGNES_API_TOKEN`，两者不得混用。

1. **在服务器本地生成高熵随机值**（至少 32 字节；64 位十六进制）：
   ```bash
   python3 -c "import secrets; print(secrets.token_hex(32))"
   ```
   仅在服务器终端执行一次并立即写入 env 文件；不要把生成的值粘贴到聊天、文档、日志或剪贴板。
2. **追加到 `/etc/agnes-video/agnestudio.env`**（格式 `AGNES_PLAY_SECRET=<hex值>`，单行，无引号）：
   - 文件权限保持 `0600`；属主/组 `agnestudio:agnestudio`（与运行用户一致，`systemctl show agnesvideo.service -p User` 可核对）：
     ```bash
     chmod 0600 /etc/agnes-video/agnestudio.env
     chown agnestudio:agnestudio /etc/agnes-video/agnestudio.env
     ```
   - 不得把该值复制到 `/opt/agnes-video/`、`/www/wwwroot/` 或任何版本库/备份快照。
3. **由管理员在维护窗口重启服务**才会加载新环境：
   ```bash
   systemctl restart agnesvideo.service   # 需要管理员批准并在维护窗口执行
   ```
   仅修改 env 文件不重启，`AGNES_PLAY_SECRET` 不会生效。
4. **验证（不打印 secret 值）**：
   ```bash
   # 只确认变量已加载（输出 true/false），不输出值本身
   systemctl show agnesvideo.service -p Environment | grep -q AGNES_PLAY_SECRET= && echo loaded || echo not_loaded
   # 带 Bearer 令牌请求 playlink：200 = 已启用；503 "播放签名未配置" = 未生效（检查 env/重启）
   curl -s -o /dev/null -w "%{http_code}\n" \
     -H "Authorization: Bearer <token>" http://127.0.0.1:8090/api/jobs/<jid>/playlink
   ```

**行为约定：**
- **未配置时 fail closed**：`GET /api/jobs/<jid>/playlink` 返回 503；`/api/download` 的签名 URL 一律 403。此时**不得**通过关闭鉴权、把 Bearer 令牌放进 URL、或放宽 `/api/download` 鉴权来"绕过"——应配置 secret。
- **更换 secret 会使此前签发的所有短时签名链接立即失效**（HMAC 密钥变更后旧签名无法通过校验），这是预期行为，不需要清理旧链接；播放中的前端会在到期续签时自动拿到新签名的链接。
- 签名 URL 有效期固定 600 秒；排查播放/下载问题时，先看响应状态码（503/403/410）即可定位是"未配置"、"鉴权/签名不匹配"还是"已过期"，**不需要也不允许**把 secret 打印到终端、日志或聊天中。

### 签名 URL 的 inline / attachment 契约（disposition）

`disposition` 是签名载荷的受控字段，**不是客户端可自由指定的响应头参数**：

- `GET /api/jobs/<jid>/playlink` 默认 `disposition=inline`，供网页内 `<video>` 播放器使用；返回的签名 URL 形如
  `/api/download/<file>.mp4?purpose=play&method=GET&exp=<秒级时间戳>&disposition=inline&sig=<HMAC>`。
- 网页上的"下载合并视频"按钮请求 `GET /api/jobs/<jid>/playlink?disposition=attachment`，拿到 `Content-Disposition: attachment` 的签名 URL 触发下载；文件名仍为原始 basename。
- `disposition` 只允许 `inline` / `attachment` 两个值，且**被 HMAC 签名绑定**：把已签名的 `disposition` 参数改成另一值会让验签失败（403 `播放签名无效（invalid）`）；非白名单值直接 403 `播放签名无效（malformed）`。客户端无法借此覆盖服务端响应头。
- 旧的四字段签名（`purpose|basename|method|exp`，disposition 加入之前签发）仍兼容，但**只能按 inline 生效**——旧链接不能升级为 attachment；升级需重新签发。
- `/api/download` 的 Bearer 路径固定 inline，**不**接受 URL 参数改变 disposition；`disposition` 仅对签名 URL 有效。
- 未配置 `AGNES_PLAY_SECRET` 时上述全部签名能力 fail closed（503 签发 / 403 下载）；更换 secret 会使所有已签发的 URL（含 inline 与 attachment）立即失效，属预期行为。

### /api/download 的 GET / HEAD / Range 契约

- **GET**：无 `Range` 返回 `200` + 完整正文；带合法 `Range: bytes=…` 返回 `206` + `Content-Range` + 对应片段；越界/无法满足的 `Range` 返回 `416`；非法/多段/非 `bytes=` 的 `Range` 按 `200` 完整返回。
- **HEAD**：仅用于**元数据探测**。复用生产签发的 **GET 签名**（`?method=GET` 的 playlink URL）即可 HEAD；响应携带完整资源头（`Content-Type`、`Accept-Ranges: bytes`、`Content-Length` = 完整文件大小、`Content-Disposition`、`Referrer-Policy: no-referrer`），但**不发送任何正文**，且**忽略请求里的 `Range`**（始终报告完整大小）。
- **不存在“HEAD 专用签名”**：签发端只签 `method=GET`；验证端强制签名 `method` 必须为 `GET`，因此 `method=HEAD` 的签名对 GET 与 HEAD 都无效（一律 403），HEAD 签名绝不能执行 GET 下载，也不削弱 GET 方法绑定。
- **鉴权**：`/api/download` 支持 Bearer（固定 inline）或短时签名 URL（inline/attachment 由签名绑定）；未配置 `AGNES_PLAY_SECRET` 且无 Bearer 时 fail closed（403）。
- **其它路径的 HEAD**：`/api/download` 以外的 `/api/*` 路由仍不支持 HEAD，返回 `501`。

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
