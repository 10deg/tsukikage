# Tsukikage Windows 本地服务（月面模拟 + 月面地图）

没有 Qt 窗口，也没有内置 Chromium。`Tsukikage.exe` 在本机 `127.0.0.1` 上启动
一个或多个 HTTP 服务，然后用系统默认浏览器打开页面。关闭控制台窗口或按 Ctrl+C
停止服务。

主程序现在同时包含两套页面：

| 服务 | 默认端口 | 页面 |
|---|---:|---|
| 月面模拟（main） | 8765 | `/` |
| 月面地图（map，含照片库） | 8766 | `/` |
| 全部（both） | 两个端口 | 打开两个标签页 |

启动时控制台会询问启动哪个服务，也可以直接指定：

```bat
Tsukikage.exe
Tsukikage.exe --service main
Tsukikage.exe --service map
Tsukikage.exe --service both
Tsukikage.exe --map-server        （旧参数，等同 --service map）
```

端口被占用时会自动向后找，控制台打印实际地址。

## 目录布局：kernels 是必需的附件

单文件 exe 旁边必须有一个 `kernels\` 目录：

```text
Tsukikage.exe
kernels\
  README.txt
  naif0012.tls
  de440s.bsp
  pck00011.tpc
  gm_de440.tpc
  moon_pa_de440_200625.bpc
  moon_de440_220930.tf
  earth_latest_high_prec.bpc
  （用户添加的 .tif / .exr）
```

7 个 SPICE 内核不再打进 exe，而是和 exe 一起发布。启动时程序会自动检查；缺文件
会列出具体文件名，补齐后按 Enter 会重新检查并继续：

```text
内核附件不完整。
需要的目录：...\kernels
需要的文件：
  - naif0012.tls
  ...
补齐后按 Enter 重新检查，输入 q 退出：
```

月面 DEM 也放在同一个 `kernels\` 目录。**推荐的两份**是：

- `ldem_64.tif`：整月面金字塔用它，启动快、占用小；
- `SLDEM2015_512.tif`（即 SLDEM_512）：高倍缩放、测量剖面和近景用它，59 m 网格。

也可以只放其中一份。其余可识别的全球高程 `.tif`：
`ldem_*.tif`、`Lunar_LRO_LOLA_*.tif`。

色彩可选：`lroc_color*.tif` 或 `lroc_color.exr`。只有高程 `.tif` 时渲染为灰度。
把文件复制进目录后，启动时会自动扫描；程序运行期间目录发生变化也会自动重新加载。

下载 tif / exr：
  https://svs.gsfc.nasa.gov/4720

### 内置 DEM 下载

地图页“设置 → 下载高分辨率 DEM”（LOLA 256 ppd，约 8.5 GB）现在由 exe 内置的
下载器完成，不再依赖源码目录里的工具。下载文件直接保存到 `kernels\`：

```text
Lunar_LRO_LOLA_Global_LDEM_118m_Mar2014.tif
```

支持断点续传；下载完成后地图会自动重载。也可以只下载不启动服务：

```bat
Tsukikage.exe --download-dem
```

## 月面地图与照片库

地图服务已经并入主程序，不再需要另开一个 exe。照片库的“修改默认目录”保留了：

- 默认照片目录：`%APPDATA%\Tsukikage\map_photos`（空目录，不含示例照片）
- 页面上的“修改默认目录”会把选择保存到
  `%APPDATA%\Tsukikage\map_photos_dir.json`
- 下次启动会优先使用这个目录；目录不存在时回退到默认目录

地图页本身不依赖 DEM 才能打开：没有全球 DEM 时会使用平坦底图，仍可看坐标、
地貌标签和照片库；放入/下载 DEM 后自动切换。

## 数据自动识别

- **启动时**：检查 `kernels\` 的 7 个必需内核；主服务/全部还要求至少一个全球
  2:1 高程 TIFF。缺少时会提示具体文件和目录。
- **运行中**：轮询 `kernels\*.tif/.tiff/.exr`（忽略下载用的 `.part`/
  `.incomplete`），文件稳定后自动清空地形缓存并重新加载；主页面和地图都会生效。
- **下载完成**：内置下载器写入最终 `.tif`，地图立即重载，主服务的 watcher 也会
  在下一轮检测到并重载。

## 用户需要准备什么

**随包提供：**

- `Tsukikage.exe`；
- `kernels\` 里的 7 个近期模式 SPICE 内核和附件说明；
- `moonlib`、`web/static`、`web/map`、地图服务、默认设置。

**用户提供：**

- 至少一个全球 2:1 高程 TIFF，放进 `kernels\`；
- 色彩可选；
- 地图照片库可选，通过页面“修改默认目录”指定。

**刻意不打包：**

| 去掉的东西 | 结果 |
|---|---|
| Qt / PySide6 / QtWebEngine | 不再有内嵌 Chromium；浏览器由系统提供 |
| kernel 文件本体 | 改为 exe 同目录的 `kernels\` 必需附件，exe 更小 |
| `timezonefinder` + `h3` | 本地时区按经度/15 估算，页面时区字段可手填 |
| `imageio-ffmpeg` | MP4 动画需要系统 `ffmpeg` 在 PATH 上 |
| 地图示例照片 | 照片库默认空；用户可自选目录 |
| 月面栅格 `.tif` / `.exr` | 由用户提供，或由内置下载器下载 |
| `matplotlib`、`scipy`、`pandas` 等 | 服务不导入；PyInstaller 显式排除 |

## 命令行

```bat
Tsukikage.exe --service both
Tsukikage.exe --service map --no-browser
Tsukikage.exe --download-dem
Tsukikage.exe --data-dir D:\moon\raster       （高级：DEM 不放在 kernels）
Tsukikage.exe --kernel-dir D:\tsukikage\kernels
Tsukikage.exe --port 8765 --map-port 8766
Tsukikage.exe --kernel-mode long             （需要 de441 分卷）
```

- 默认只监听 `127.0.0.1`；`--host 0.0.0.0` 才会对局域网开放。
- `--no-browser` 只启动服务，适合调试。
- `--smoke-seconds` 是测试用隐藏参数。
- 长周期模式需要额外的 `de441_part-1.bsp`、`de441_part-2.bsp`。

## 目录与日志

| 用途 | 路径 |
|---|---|
| 设置 | `%APPDATA%\Tsukikage\service.json` |
| 地图照片目录设置 | `%APPDATA%\Tsukikage\map_photos_dir.json` |
| 默认照片目录 | `%APPDATA%\Tsukikage\map_photos` |
| 地形缓存 | `%LOCALAPPDATA%\Tsukikage\cache` |
| 日志 | `%LOCALAPPDATA%\Tsukikage\logs\service.log` |

控制台输出同时写入 `service.log`。设置文件损坏时会备份成
`service.json.bad-<时间戳>`。

## 构建

在 Windows 上：

```bat
packaging\build_windows.bat
```

脚本只安装 `numpy`、`spiceypy`、`Pillow`、`PyInstaller`，输出：

```text
out\windows-service\dist\Tsukikage.exe
out\windows-service\dist\kernels\        （7 个内核 + README）
out\windows-service\dist\README.txt
out\windows-service\Tsukikage-windows-x64.zip
```

构建后会把 7 个内核从仓库 `kernels\` 复制到 `dist\kernels\`，并检查 exe 归档里
没有 `.tif/.tiff/.exr` 或内核文件。`.github/workflows/windows.yml` 使用同样布局。
