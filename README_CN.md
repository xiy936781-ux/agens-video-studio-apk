# Agens 视频工作室 Android APK 外壳项目

这是一个 Android WebView 壳应用项目。它会打开同一台手机 Termux 中运行的 `http://127.0.0.1:8090`。

## 重要限制
- 这是安装到手机上的应用外壳，不是独立后端。
- 每次使用前，仍需在 Termux 启动原有服务：
  `cd ~/termux_ai_video_studio/termux_ai_video_studio && ./start.sh`
- API Key 仍由原来的本机 Python 服务管理；不要把 API Key 写进 APK 或上传到 GitHub。
- 此项目尚未在你的手机上实机编译/测试，生成 APK 后需先安装验证。
- 项目通过 GitHub Actions 云端构建 APK，不需要电脑；GitHub 账户需允许使用 Actions。

## 手机端构建步骤
1. 解压 ZIP。
2. 在 GitHub 创建一个新仓库，例如 `agens-video-studio-apk`。
3. 在仓库页面上传解压后的所有文件，必须包含隐藏目录 `.github/workflows`。
4. 打开仓库的 Actions，运行 `Build Android APK` 工作流（也可上传到 `main` 分支后自动触发）。
5. 构建完成后进入该次运行页面，在 Artifacts 下载 `agens-video-studio-apk`，解压得到 `app-debug.apk`。
6. 安装 APK。使用前先在 Termux 启动服务，再打开应用并点“连接并打开工作室”。

如果 Actions 报错，请把构建日志发来；不要上传 API Key。
