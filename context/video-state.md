# AgnesVideo Web 状态记录

## 项目维护分类
- 分类：活跃本地 Web 服务项目。
- 依据：`~/termux_ai_video_studio/termux_ai_video_studio/README_CN.md` 将该项目描述为手机端本地 Web 控制面板；`start.sh` 与 `server.py` 为本地启动与后端入口（此前只读核验，证据路径见下方“已证实事实”）。

## 服务运行状态
- 状态：未知。
- 说明：文件存在不等于服务正在运行。本轮未检查本地或远端进程、未做端口探活、未启动/停止/重启任何服务，因此不把运行状态写成“运行中”或“未运行”。

## 最近验证
- 最近验证：未知。
- 说明：本轮未运行测试，未读取密钥，未访问远端，未执行 SSH；因此不记录任何实时验证结论。

## 已证实事实及证据路径
- 项目根目录为 `~/termux_ai_video_studio/termux_ai_video_studio`，前端为 `index.html`，后端入口为 `server.py`。
  证据：`/data/data/com.termux/files/home/termux_ai_video_studio/termux_ai_video_studio/docs/AGNES_VIDEO_ISOLATION.md`（本地项目目录表，第 1-20 行）
- 本地默认监听端口为 `PORT`，默认值 `8090`；本地仅监听 `127.0.0.1`。
  证据：`/data/data/com.termux/files/home/termux_ai_video_studio/termux_ai_video_studio/README_CN.md`（环境变量表与安装说明，第 15-45 行）
- 本地启动脚本为 `start.sh`，使用 `python server.py` 启动。
  证据：`/data/data/com.termux/files/home/termux_ai_video_studio/termux_ai_video_studio/start.sh`（第 1-19 行）
- `server.py` 为 Python 标准库 HTTP 服务入口，读取 `AGNES_API_TOKEN` 作为网站访问令牌，默认数据/输出目录为项目内 `data/` 与 `outputs/`。
  证据：`/data/data/com.termux/files/home/termux_ai_video_studio/termux_ai_video_studio/server.py` 第 1-20 行
- 文档将 `18090` 记录为 SSH 隧道本地端口，用于转发到远端 `127.0.0.1:8090`；远端服务名为 `agnestudio.service`，运行用户为 `agnestudio`。
  证据：`/data/data/com.termux/files/home/termux_ai_video_studio/termux_ai_video_studio/docs/AGNES_VIDEO_ISOLATION.md`（本地/远端端口与服务进程表，第 20-60 行）
- 运维文档将本地/远端操作命令写为 `systemctl start agnesvideo` 等，但隔离文档的远端服务名为 `agnestudio.service`；二者关系未经当前验证。
  证据：`/data/data/com.termux/files/home/termux_ai_video_studio/termux_ai_video_studio/docs/AGNES_VIDEO_OPERATIONS.md`（服务管理表，第 1-20 行）、`/data/data/com.termux/files/home/termux_ai_video_studio/termux_ai_video_studio/docs/AGNES_VIDEO_ISOLATION.md`（远端资源表）

## 推断
- 推断：本地默认使用 `8090`，远端默认使用 `18090` SSH 隧道转发到远端 `8090`。
  依据：来自 `README_CN.md` 与 `docs/AGNES_VIDEO_ISOLATION.md` 的文档描述；本轮未做实时端口/隧道验证。
- 推断：`agnestudio.service` 可能是远端 systemd 服务名，`agnesvideo` 可能是运维文档中的别名或旧名；具体关系待确认。
  依据：两份 docs 服务名不一致（`agnestudio.service` 与 `systemctl ... agnesvideo`），本轮未 SSH 验证。

## 待确认项
- 本地 `8090` 当前是否监听：待确认（本轮未探活）。
- 本地 `18090` SSH 隧道当前是否存活：待确认（本轮未检查进程/端口）。
- 远端服务名（`agnestudio.service` / `agnesvideo`）与运行状态：待确认。远端 IP `210.126.238.200` 来源为 `docs/AGNES_VIDEO_ISOLATION.md` L23 与 `docs/AGNES_VIDEO_OPERATIONS.md` L58，二者关系未经当前 SSH 验证。
- 最近一次本地/远端验证时间与结论：待确认（无当前证据）。
- `data/config.json` 与 `~/.agens/video-site-token`：仅在用户批准后核对文件存在性与权限/属主等元数据，绝不读取其内容；当前有效性待确认。

## 下一步
- 若需确认本地运行状态：在用户批准后单独执行只读端口/进程检查（如 `ss -ltn` 观察 8090、`ps` 观察 `python server.py`），并将结果与时间回填到“最近验证”。
- 若需确认远端服务名：在用户批准后单独 SSH 核对 `systemctl status`，确认 `agnestudio.service` 与 `agnesvideo` 的映射关系。
- 状态更新须先提交“差异 + 证据”，经用户批准后再写入本文件。

## 禁止触碰边界
- 服务：不得启动、停止、重启本地或远端服务。
- 端口/隧道：不得操作或修改 8090、18090、11435 及其他非本项目端口。
- 密钥/令牌：不得读取、打印或输出 `data/config.json`、`~/.agens/video-site-token`、`/etc/agnes-video/agnestudio.env`、`/var/lib/agnes-video/data/config.json` 等密钥/令牌内容。
- 网络：不得联网、不得 SSH、不得运行测试、不得访问远端。
- 备份/其他文件：不得修改其他项目文件；不得整理备份。
- 文档中的端口、远端服务名与部署信息仅作为来源记录，未经当前验证前不得写成实时事实。
