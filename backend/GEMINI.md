# AgnesVideo Web 项目上下文

- 项目：AgnesVideo Web（本地视频工作室）。

@context/video-state.md

## 项目文档（路径指针，按需读取，不导入整篇文档）
- README_CN.md
- docs/AGNES_VIDEO_ISOLATION.md
- docs/AGNES_VIDEO_OPERATIONS.md

## 说明
- 项目维护分类不等于服务运行状态；文件或文档存在不能证明服务当前运行。
- 任何状态更新必须先给出差异和证据，未经用户批准不得写入。

## 边界
- 禁止未经明确批准启动、停止或重启服务。
- 禁止访问 SSH/网络。
- 禁止读取或输出密钥、令牌、日志、备份内容。
- 禁止修改其他项目文件。
- 不要跨项目导入；不要加入 hooks 或 Auto Memory。
