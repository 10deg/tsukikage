Tsukikage 月面模拟 1.6.6 — macOS arm64（独立 App）

安装：
  把 Tsukikage.app 拖到左侧的 Applications（应用程序）文件夹。

首次启动：
  在“应用程序”里右键 Tsukikage → 打开（程序未签名/未公证，直接双击可能被
  Gatekeeper 拦截）。第一次会弹出原生文件夹选择框，请选择包含全球高程 .tif
  的目录。

这是一个独立 App：不打开系统浏览器，也不需要手动访问本地服务。
月面页面显示在 App 自己的窗口里，由 macOS 自带的 WebKit 渲染；Python 月面
后端作为 App 的内部组件运行。7 个 SPICE 内核已经打包在 App 内。

需要什么：
  只需要月面 .tif 数据（ldem_*.tif、SLDEM2015_512.tif 或 Lunar_LRO_LOLA_*.tif）。
  色彩可选（lroc_color*.tif 或 lroc_color.exr）。只给高程 .tif 也能渲染灰度。

操作：
  - 关闭窗口即停止内部服务并退出。
  - 文件 → 重新加载 / 打开下载文件夹。
  - 设置… 可以重新选择月面数据目录。
  - 帮助 → 打开日志 / 打开月面数据目录。
  - 下载的图片、模型、动画会保存到 ~/Downloads。

日志：~/Library/Logs/Tsukikage/app.log
后端日志：~/Library/Logs/Tsukikage/service.log
设置：~/Library/Application Support/Tsukikage/

没有内置 ffmpeg：动画 MP4 需要系统 PATH 上有 ffmpeg。
没有内置时区数据库：本地时区按经度推算，页面上的时区字段仍可手填。
没有示例照片：地图照片库为空（地图服务可通过命令行后端单独启动）。

如果 macOS 提示“无法验证开发者”：
  在“系统设置 → 隐私与安全性”里点“仍要打开”，或者对 app 执行
  xattr -dr com.apple.quarantine /Applications/Tsukikage.app

本 App 为 Apple Silicon（arm64）构建；Intel Mac 需要另建 x86_64 版本。
