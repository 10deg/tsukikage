Tsukikage 月面模拟 1.7.2 — Windows x64（本地服务 + 浏览器）

目录结构
--------
  Tsukikage.exe        主程序
  kernels\             运行必需的附件目录（不能和 exe 分开）
    README.txt         本附件说明
    naif0012.tls ...   7 个必需 SPICE 内核
    *.tif              用户添加的月面 DEM/彩色数据

双击 Tsukikage.exe
------------------
程序会在控制台询问启动哪个服务：
  1) 月面模拟（主页面）
  2) 月面地图（含照片库）
  3) 两个都启动（默认）

选择后在本机 127.0.0.1 上启动服务，并打开系统默认浏览器；选择“全部”会打开两个
标签页。控制台窗口必须保持打开，关闭窗口或按 Ctrl+C 停止。

内核和数据
----------
7 个近期模式 SPICE 内核不再打进 exe，而是放在同目录的 kernels\ 里一起发布。
启动时会自动检查；缺文件会列出具体文件名，补齐后按 Enter 会重新检查并继续。

推荐的全球高程组合（放进 kernels\）：
  ldem_64.tif + SLDEM2015_512.tif
前者给整月面金字塔，后者给高倍缩放与测量剖面；也可以只放其中一份。
其余可识别的产品：ldem_*.tif、Lunar_LRO_LOLA_*.tif。
色彩可选：lroc_color*.tif 或 lroc_color.exr；只有高程时渲染为灰度。
手动添加/替换的 .tif 会被自动识别；地图页“下载高分辨率 DEM”下载的文件也保存
到这里，下载完成后地图会自动重载。

下载 tif / exr：
  https://svs.gsfc.nasa.gov/4720

地图与照片库
------------
地图服务现在是主程序的一部分，可在启动菜单选择，也可以单独启动：
  Tsukikage.exe --service map
  Tsukikage.exe --service both
  Tsukikage.exe --map-server        （旧参数，等同 --service map）

地图页“照片”标签里的“修改默认目录”可以指定照片库根目录；设置保存在
%APPDATA%\Tsukikage\map_photos_dir.json，下次启动仍然生效。示例照片没有打包，
默认照片库为空。

命令行
------
  Tsukikage.exe --service both
  Tsukikage.exe --service map --no-browser
  Tsukikage.exe --download-dem        只下载全球 LOLA 118 m DEM 到 kernels\ 后退出
  Tsukikage.exe --data-dir D:\moon\raster   （高级：DEM 不放在 kernels\）
  Tsukikage.exe --port 8765 --map-port 8766
  Tsukikage.exe --kernel-dir D:\tsukikage\kernels

端口被占用时会自动向后找，控制台会打印实际地址。

没有内置 ffmpeg、timezonefinder 或示例照片
-------------------------------------------
MP4 动画需要系统 PATH 上有 ffmpeg；本地时区按经度推算，页面“时区”字段可手填。
地图照片库默认为空。

日志/设置
---------
日志：%LOCALAPPDATA%\Tsukikage\logs\service.log
设置：%APPDATA%\Tsukikage\service.json
照片目录：%APPDATA%\Tsukikage\map_photos_dir.json

程序未做代码签名/公证，Windows SmartScreen 可能提示「更多信息 → 仍要运行」。
SHA256 见 Tsukikage.exe.sha256。
