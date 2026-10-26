# Tsukikage

**当前版本 / Version：1.6.0**

一套可**完全离线**运行的月球计算内核与观测模拟器：以 JPL DE440/DE441 星历为基础，提供月球的
**位置、月相、升落、天平动等信息**，并能用 NASA SVS 的 LOLA 高程与
LROC 色彩数据**渲染模拟从地面看到的月面**——含真实地形阴影、明暗界线光照与地照。算法与判据全部自建，**不使用任何在线天文服务**；按「计算层 / 服务层 /
表现层」分层，对外是稳定的 JSON 数据契约。

---

## 安装

需要 Python 3.9+ 与 git。

```bash
git clone https://github.com/10deg/tsukikage.git Tsukikage
cd Tsukikage
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade "setuptools>=68" wheel
./.venv/bin/python -m pip install -e ".[full]"
```

`[full]` 会装上 numpy、spiceypy、timezonefinder 与 matplotlib。后两个是可选的：
没有 timezonefinder 时按经度推算时区，没有 matplotlib 时只有绘图类功能不可用。

确认装好了：

```bash
./.venv/bin/python -c "import moonlib; print(moonlib.__version__)"
```

> 上面建了虚拟环境，所以下文一律用 `./.venv/bin/python`。没有做可编辑安装时，
> `tools/` 与 `examples/` 下的脚本也会自己把 `src/` 加进 `sys.path`。

## 简单运行

```bash
./.venv/bin/python -m moonlib info                          # 内核与历表覆盖范围
./.venv/bin/python -m moonlib moon --at 2026-05-25T15:30:00 --language zh
./.venv/bin/python -m moonlib sunpoint --moon-lon 0 --moon-lat 0 --lat 30 --lon 120 --at 2026-05-25T15:30:00 --language zh
```

```python
import moonlib

site = moonlib.Observer(lat=30, lon=120)
report = moonlib.moon_report(site, "2026-05-25T15:30:00")
print(moonlib.reporting.format_moon_report(report))
```

```
  月相 / Phase            : 盈凸月 / Waxing Gibbous
  照明率(地心) / Illum (geo) : 72.0381 %
  月龄 / Moon age         : 8.811 d   (视觉月龄 / visual 9.481 d)
  月面地球直下点 / Sub-Earth point: lon +6.5246°  lat +2.9005°
  月出 / Moonrise         : 2026-05-25 13:33:05 (UTC+08:00)   az 90.31°
  中天 / Transit          : 2026-05-25 19:36:47 (UTC+08:00)   el +58.26°
  月落 / Moonset          : 2026-05-26 01:38:07 (UTC+08:00)   az 266.24°
  下次月出/月落 / Next rise / set: 14:29:30 / 01:38:07
  中天上边缘高度 / Upper limb at transit: +58.515°
```

本地网页：

```bash
./.venv/bin/python web/server.py --port 8765     # 打开 http://127.0.0.1:8765/
```

第二个本地服务：月面地图（独立进程，可与模拟器同时开）：

```bash
./.venv/bin/python web/map_server.py --port 8766  # 打开 http://127.0.0.1:8766/
```

地图是 2:1 等距圆柱投影的离线瓦片金字塔：按缩放级别从同一份 DEM/色彩数据渲染，
带地貌多图层标注、搜索、坐标与高程读数。页面与接口都是模板，新增图层不需要改渲染器。

每个子命令都支持 `--json`，输出与 HTTP API 完全相同的载荷。

## 运行所需的最少文件

| 路径 | 为什么需要 |
|---|---|
| `src/moonlib/` | 库本身 |
| `pyproject.toml` | 安装、依赖声明、`moon` 命令行入口 |
| `kernels/` 的 7 个默认内核 | 位置、月相、升落、事件、可见性、天平动；约 51 MB，随仓库发布 |

**要开网页**再加 `web/`。
其余目录：`tools/`、`examples/`、`assets/`都不是运行必需。
**要实现高分辨率月面模拟**需要额外下载高程栅格数据，见下一节。

---

## 模拟月面要下载的数据

月面栅格与长时段历表有十几 GB，因此不入版本库。按 [`kernels/README.md`](kernels/README.md)
下载所需的那几个文件即可；脚本会自己找到它们，也可以指到别处：

```bash
export MOONLIB_KERNEL_DIR=/data/spice          # 内核目录（含 naif0012.tls）
export MOONLIB_DEM=/data/LDEM_118m.tif         # 高程：一个文件
export MOONLIB_ALBEDO=/data/lroc_color.exr     # 色彩：一个文件
export MOONLIB_TERRAIN_DIR=/data/moon-rasters  # 或一个目录，两个文件都从这里找
export MOONLIB_CACHE_DIR=/data/moon-cache      # 金字塔缓存写到哪（默认 <仓库>/.cache）
```

最低限度的一套是「`ldem_64.tif` + `lroc_color.exr`」（约 1.4 GB）；要做望远镜特写再补
USGS 的全球 256 像素/度高程：

```bash
./.venv/bin/python tools/fetch_lola_global.py     # 8.5 GB，一次性，之后全离线
```

**无需手动初始化**：第一次渲染时会自动把栅格烘焙成金字塔缓存
（`ldem_64` + EXR 约 350 MB；换成 256 像素/度的全球高程约 3 GB）。

色彩镶嵌优先用线性的 `lroc_color.exr`（自带解码器，无需额外依赖）。若手头只有
`lroc_color_16bit_srgb.tif`，则需要 Pillow（`pip install -e '.[tiff]'`；装了
matplotlib 就有），两条路径结果一致。

### 高程数据源与切换

月面颜色缓存默认使用 `albedo_detail=2`（19 px/deg，约 1.6 km/px，约 2.8 GB），
这是不额外占盘的默认值。想要更细的颜色，可以显式烘焙高精度缓存：

```bash
./.venv/bin/python -m moonlib terrain --bake --albedo-detail 0
```

`albedo_detail=0` 是源图完整的 76 px/deg（约 0.4 km/px），缓存约 **5.4 GB**；
网页服务启动时会自动选用本地已经存在的最高精度颜色缓存，代码里的默认值仍是 2，
不会让新安装悄悄花掉几个 GB。旧的 2.8 GB 缓存可以删除。

月面高程可以放多份，程序按**实测像素宽度**给它们排序（不看文件名），网页面板的
「模拟设置 → 高级设置 → 数据源」里列出本机能用的全部产品，选哪份就用哪份画。

| 产品 | 网格 | 像素 | 体积 | 说明 |
|---|---|---|---|---|
| `ldem_64.tif`（SVS） | 64 ppd | 474 m | 1.0 GB | 最低限度的一套 |
| `Lunar_LRO_LOLA_Global_LDEM_118m_Mar2014.tif`（USGS） | 256 ppd | 118.5 m | 8.5 GB | 长期以来的默认件 |
| `ldem_512.tif`（PDS LOLA GDR V3.0 拼接） | 512 ppd | **59.2 m** | 34 GB | 本仓库脚本可下可拼 |
| `SLDEM2015_512.tif`（SLDEM2015 + LOLA 极区） | 512 ppd | **59.2 m** | 34 GB | ±60° 内由辉夜号 TC 立体填缝，极区复用 LOLA 512 |

前三份各有一条命令以供下载、合并数据文件：

```bash
./.venv/bin/python tools/fetch_lola_global.py     # 8.5 GB，USGS 256 ppd
./.venv/bin/python tools/fetch_lola_512.py        # 34 GB，PDS LOLA GDR 512 ppd
./.venv/bin/python tools/fetch_sldem_512.py       # 45 GB 下载 → 34 GB，SLDEM2015 + 极区
```

`fetch_lola_512.py` 逐张下载 16 块瓦片、写进预分配的 `kernels/ldem_512.tif`、删掉瓦片，
磁盘峰值占用是 34 GB 加一块瓦片；支持断点续传。

`fetch_sldem_512.py` 处理的是 **32 块 ±60° 瓦片（约 45 GB）**：SLDEM2015 没有 512 ppd
的极区产品，所以 ±60–90° 从本机已有的 `ldem_512.tif` 直接复制，结果仍是 59.2 m 的全球
2:1 栅格。它按 512 行一块（45 MB）边下边写进预分配的 34 GB BigTIFF，不落整块瓦片；
写完后半成品带 `.incomplete` 边车，网页在完成前不会把它列成数据源。支持断点续传。

如有临时需求，只想在某一处实现高分辨率模拟时，也可不下载整份产品：高级设置里的「区域高清补片」按**当前选框**可从在线产品匹配所需的那一块（`POST /api/patch`，后台任务带进度），落在 `<cache>/regions/`，
近景会自动改用它。可选的在线产品见 `moonlib.online.catalogue()`。

**换了数据源不会重烘焙整月面的金字塔**：全月面渲染从本地金字塔加载，最高只采样到 每像素 474 m，不会动用更高分辨率的高程数据，默认的全月面渲染缓存最高支持 2300 px 分辨率。而高分辨率模拟是从源文件直接裁窗口的，不需要金字塔。故更换高分辨率数据源不影响全月面预览。

**默认不会自动改用它。** 装好之后程序仍然沿用原来那份已烘焙的高程，直到在「数据源」里
选中它（或有 `MOONLIB_DEM` 指定）；选过之后会记在缓存目录的 `dem_source.json` 里，
下次启动沿用。物理分辨率菜单里那些比当前数据源更细的档位（比如 118 m 档位下的 60 m）
会显示为不可用，换成更细的数据源才会亮起——所以「画面能细到多少」和「菜单让选什么」
永远说的是同一件事。

---

## 界面

![Tsukikage 本地网页：左栏观测信息与月相，中间全月面与 1:1 实时特写，右栏模拟设置、后期处理与事件/动画栏](assets/screenshot.png)

---

## 能力一览

| 能力 | 入口 |
|---|---|
| 月球位置、距离、视直径、赤经赤纬、方位高度 | `moonlib.moon_state` |
| 月相（地心 + 站心）、月龄、朔望弦搜索与全年历表 | `moon_state().phase`、`moonlib.phase_calendar` |
| 月出 / 中天 / 月落（含下次升落与中天上边缘高度） | `moonlib.moon_report` |
| 天平动、月面地球直下点与日下点 | `moon_state().libration` |
| 按照明率反查月亮（按视直径排名） | `moonlib.find_moons_by_illumination` |
| 极限月牙拍摄窗口与可见性判据 | `moonlib.crescent` |
| 边缘／极区目标的天平动可见性预报 | `moonlib.libration` |
| 太阳视位置与八字曲线；天区图、月相图与图表 | `moonlib.sun`、`moonlib.analemma`、`moonlib.plotting` |
| **月面点太阳高度/方位角** | `moonlib.lunar_surface_solar`、`moonlib.lunar_surface_solar_series`、`moon sunpoint` |
| **观测模拟** | `moonlib.moonview` |
| **月背视角模拟**（假想对称地球的地心） | `moonlib.view_geometry(..., viewpoint="far_side")`、全月面标题栏「月背」按钮 |
| **3D 地形切片（STL，可直接打印）** | `moonlib.selection_tile`、网页「3D模型」 |
| **带纹理 GLB（Blender 直接查看）** | `TerrainTile.glb_bytes()`、网页「导出 GLB」 |
| **Blender 预览场景（.blend，含三个光照场景）** | 网页「生成 Blender 预览场景」、`POST /api/mesh/blender` |
| **全月面 GLB / Blender 场景（当前月相 / 低角度）** | 网页「全月面」范围、`POST /api/moon/glb`、`POST /api/moon/blender` |
| **可打印的投影修正选框** | `moonlib.selection_correction`、网页「使用带投影修正的选框」 |
| **交互式月面地图（第二个本地服务）** | `web/map_server.py`、`web/map/` |
| **月面特写与自动取景** | `moonlib.moonview.render_region`、`moonlib.render_feature` |
| **月食模拟** | `moonlib.eclipse`、`moonlib.moonview` |
| **月面 X / V / L预报** | `moonlib.events` |
| **最高／最低月亮** | `moonlib.events` |
| **地貌标注** | `moonlib.annotations` |
| **曲线图** | `web/static/app.js`、`moonlib.analemma`、`moonlib.moonview` |
| **模拟实拍效果** | `moonlib.instrument` |
| **生成动画**；**HDR 图片** | `moonlib.anim`、`moonlib.encode`、`moonlib.hdr` |
| **本地网页客户端** | `web/server.py` |
| JSON 服务契约 | `moonlib.service` |

---

## 自检

仓库自带这几道护栏，都不需要额外下载数据：

```bash
./.venv/bin/python tools/check_layering.py     # 分层与依赖规则
./.venv/bin/python tools/selfcheck.py          # 端到端自检（含一次月面渲染）
node web/test_dom.js && node web/test_page.js && node web/test_projection.js
```

页面的三个 Node 脚本共 774 项，覆盖静态接线、投影与绘制。需要栅格或内核的检查在文件
缺失时**自动跳过**并说明原因，所以没有下载任何栅格也能跑完。

---

## 重要说明

**已知边界**：

- 高度角是几何量，不含逐点大气折射模型；`observational` 升落约定用固定的
  +0.125° 合成阈值。
- Danjon / Odeh / Yallop 判据是已发表的实测模型，作为**参考输出**提供，不参与筛选，
  其适用性随站点与观测者变化。
- 网页与 `chinese_lunar_date` 显示的是**按 GB/T 33661 算出的真实农历**（新月定朔、
  冬至定月、无中气置闰，时间为北京时间）；`lunar_day_label` 保留为旧接口，仍是月龄
  近似，与真实农历可能差一天，新代码不要再用它。
- `pck00011.tpc` 与 `de442s.bsp` 随仓库发布但默认不加载（前者与 `pck00010.tpc` 同时
  加载会产生常数歧义）。

**月面数据与渲染的边界**（详见 [`kernels/README.md`](kernels/README.md)）：

- 高程有两档，**用哪一档由盘上的文件决定**，分辨率从文件本身量而不是从名字猜：
  SVS 的 `ldem_64.tif`（64 像素/度，474 m）够画整月面；USGS 的全球 256 像素/度产品
  （118 m）用来做特写。两档都在时取最细的那份。
- 只有 64 像素/度那一份时，垂直精度约 1 m，但轨道间隙（1–4 km）由插值填补，
  **有效水平分辨率低于 473.8 m 的像素尺度**；`render_region` 会用 `terrain_limited`
  说明某张特写是否已经越过了数据本身的极限。
- 色彩图是 Hapke 光度**归一化**的相对反射率，不是照片也不是法向反照率；其绝对值经
  人为调整，因此 `normal_albedo` 取「全球平均法向反照率」这一约定（默认 0.136，
  Lane & Irvine 1973）。**地照与日照的比值不受该约定影响**。地球/月球反照率的
  具体数值范围与文献引用见 [`ACKNOWLEDGEMENTS.md`](ACKNOWLEDGEMENTS.md) 的
  「反照率与光度参数」一节。
- 色彩图 70°N–70°S 之外是单色填补，极区因此偏灰。
- `tone="srgb"`/`"linear"` 是辐射度忠实的「拍摄」结果；由于暗面比亮面暗约 12 个星等，
  单次曝光必然看不到地照。`tone="visual"` 是**目视近似**（自适应 asinh 压缩），属感知
  重建而非辐射度测量。`sunlit` 与 `earthshine` 两个分量始终单独返回，便于自行合成。
- **月背视角**（`viewpoint="far_side"`）的相机是真实地球地心的月心对称点，取地心距；
  月背的日照是真实的，**地照来自同一颗假想地球**（相位与真实地球互补），不是真实地球
  的光穿透月球。满月时月背是夜面，只有假想地照；新月时月背全亮。
- **3D 地形切片**（`selection_tile` / 网页「3D模型」）在局部切平面里按**地面公里**建网格，
  不按经纬度：极区不会因经度线收敛而东西向压扁。顶面是地形，四周竖直、底面削平；
  STL 是毫米、z 向上的二进制文件。选框越过月缘会被拒绝；模型上限 768 采样。

**分辨率上限**：默认缓存让月面直径约 2300 px 以内有真实细节；要再往上必须重建更细的
缓存（`--detail 0 --albedo-detail 0`，约 3.5 GB）。数据本身的极限是月面直径约 7300 px
——再大只是插值放大。

**局部特写需要更高分辨率的高程。** `ldem_64` 的 474 m/px 在特写下明显不够：
`render_region` 会报告 `terrain_limited`，为真时说明输出像素已小于地形像素、图像只是
在插值放大。装上 USGS 的全球高程后这一切自动解决：

```bash
./.venv/bin/python tools/fetch_lola_global.py     # 8.5 GB 下载到本地

# 或者只取需要的那一块（range 请求，约 60 MB / 1 分钟）：
./.venv/bin/python tools/fetch_lola_global.py --name clavius --lat -66.5 -50.5 --lon -29 1
```

`render_region` 会自己发现它、按需从本地文件里裁出画幅所需的那一小块，于是任意目标
都能拿到 118 m/px，无需联网。

换一份自己的 DEM 也可以：
`export MOONLIB_DEM=/path/to/dem.tif`，单位按文件名识别（见 `moonlib.terrain._dem_scale`）。

---

Tsukikage 以 [MIT 许可](LICENSE)发布。数据出处与致谢见
[`ACKNOWLEDGEMENTS.md`](ACKNOWLEDGEMENTS.md)；内核文件清单、出处 URL 与校验方式见
[`kernels/README.md`](kernels/README.md)。
