# SPICE 内核目录 / SPICE kernel directory

本目录存放 JPL SPICE 内核文件。**默认那一套（`mode='recent'`，共约 51 MB）随仓库发布**，
所以克隆下来就能算位置、月相、升落、天平动与可见性，不必先下载任何东西。其余仍是下载：
完整的 DE441 长历表约 3.2 GB、SVS 栅格 1 GB / 940 MB、USGS 全球高程 8.5 GB，
都在 `.gitignore` 里。`moonlib_s` 会自动在此寻找内核；也可以通过
`moonlib_s.init_kernels(kernel_dir=...)`、环境变量 `MOONLIB_KERNEL_DIR` 或
`~/.config/moonlib_s/config.json` 指定其它位置。

**高程与色彩栅格不必都放在这里。** 目录可以只放内核，栅格用环境变量指过去：

```bash
export MOONLIB_DEM=/data/Lunar_LRO_LOLA_Global_LDEM_118m_Mar2014.tif  # 一个文件
export MOONLIB_ALBEDO=/data/lroc_color.exr
# 或者一个目录（下面的 ldem_64.tif / lroc_color.exr 都由它找到）
export MOONLIB_TERRAIN_DIR=/data/moon-rasters
```

`MOONLIB_DEM` 指定的就是渲染用的那一份：页面能选的最细尺度、裁窗口用的源、画出来的
像素都跟着它走，分辨率**从文件本身量**（不是从文件名猜）。没有它时，
`kernels/` 里**任何**单波段、等经纬（宽 = 2 × 高）的栅格都会被认出来，
**全部列进网页面板的「数据源」菜单**，默认用其中已经烘焙过金字塔的那一份
（都在仓库的 `.cache` 里时，也就是原来那份），直到有人显式切换。

多放一份更细的是常见需求，于是有第二份下载脚本：

```bash
./.venv/bin/python tools/fetch_lola_512.py   # PDS LOLA GDR 512 像素/度 → ldem_512.tif
```

它下 16 块瓦片、拼成一张 184320 × 92160 的 BigTIFF（34 GB）再删瓦片，峰值占用 34 GB
加一块瓦片。拼接时做的三件事值得记下来，因为它们都是本项目特有的约定：

- **不缩放**：PDS 的样本本身就是「半米为单位的高度、相对 1737.4 km 球」，
  与 `ldem_<ppd>.tif` 的约定一致，所以是字节拷贝；
- **转经度**：PDS 从 0°E 起排，本项目按 180°W 起排（`x = (lon + 180) * ppd`），
  每个 90° 块因此落在旋转过的列上；
- **分条带、不压缩、非 tiled**：`_StrippedTiff` 按 strip 偏移逐行读。

脚本还会修一处产品自带的瑕疵：每张瓦片（也就是每条 45° 纬带）的**首行**有约 2020 个
零值样本（4° 经度）。那是 PDS 自己在 label 里提示过的 band 边界产物，但它落在赤道与南北
回归线上，在 59 m 上是三根「海平面」细线，掠射光照下会亮成一道光带。`repair_band_seams()`
只在「本行是 0、上下都有真实数据」时用两侧均值补上，跨零的真实高程不会被误伤。

放好之后在页面「高级设置 → 数据源」里选中 `ldem_512.tif`，物理分辨率菜单里更细的
档位才会亮起。近景是从源文件里现裁窗口的，选中即生效，**不会**为整月面重烘焙金字塔：
10000 px 画幅只采样到 474 m 那一层，更细的高程在那里看不出差别。

## 必需内核（`mode='recent'`，默认）

**这 7 个文件随仓库发布**（合计约 51 MB），克隆后立即可用；下表的大小是它们在本目录里的实际体积。

| 文件 | 大小 | 作用 |
|---|---|---|
| `naif0012.tls` | 5 KB | 闰秒表（UTC ↔ TDB 转换的基础） |
| `de440s.bsp` | 32 MB | JPL DE440 行星历表（短版，约 1850–2150） |
| `pck00010.tpc` | 126 KB | 行星常数：半径、IAU 自转模型 |
| `gm_de440.tpc` | 12 KB | 与 DE440 一致的 GM 常数 |
| `moon_pa_de440_200625.bpc` | 12.9 MB | 月球物理天平动（指向历表） |
| `moon_de440_220930.tf` | 20 KB | `IAU_MOON` / `MOON_PA` / `MOON_ME` 参考系链 |
| `earth_latest_high_prec.bpc` | 5.1 MB | 高精度地球定向参数（岁差、章动、极移） |

## 长时段内核（`mode='long'`，按需）

| 文件 | 大小 | 作用 |
|---|---|---|
| `de441_part-1.bsp` | 1.65 GB | JPL DE441 第 1 部分，覆盖约公元前 13200 年起 |
| `de441_part-2.bsp` | 1.66 GB | JPL DE441 第 2 部分，覆盖至约公元 17191 年 |

加载 DE441 需要数秒并占用大量内存，因此**默认不加载**。做千年级搜索时再调用：

```python
moonlib_s.init_kernels(mode="long")
```

## 目录中随仓库发布的另外两个文件

| 文件 | 说明 |
|---|---|
| `de442s.bsp` | DE442 短版历表。若要使用，通过 `extra_kernels=["de442s.bsp"]` 显式指定，并注意它与 DE440 的优先级关系。 |
| `pck00011.tpc` | 更新版行星常数。与 `pck00010.tpc` 同时加载会产生歧义，故默认不加载。 |

两者都**没有被 `mode='recent'` 加载**，但体积很小且同属 NAIF 的同一套产品，所以和默认集一起发布。

## 来源

以上文件均来自 NASA/JPL 与 NAIF：

- 历表（`.bsp`）：<https://naif.jpl.nasa.gov/pub/naif/generic_kernels/spk/>
- 行星常数（`.tpc`）：<https://naif.jpl.nasa.gov/pub/naif/generic_kernels/pck/>
- 闰秒（`.tls`）：<https://naif.jpl.nasa.gov/pub/naif/generic_kernels/lsk/>
- 月球参考系与物理天平动（`.tf` / `.bpc`）：<https://naif.jpl.nasa.gov/pub/naif/generic_kernels/fk/satellites/>、<https://naif.jpl.nasa.gov/pub/naif/generic_kernels/pck/>

## 校验

```bash
python -m moonlib_s info            # 列出已加载内核与历表覆盖范围
python -m moonlib_s info --json     # 同样的信息，JSON 格式
```

---

## 月面高程与地貌纹理（NASA SVS "CGI Moon Kit"）

`ldem_64.tif`、`lroc_color.exr`（线性）与 `lroc_color_16bit_srgb.tif`（sRGB）
来自 NASA SVS 的 CGI Moon Kit，由 LRO 的 LOLA 与 LROC 数据制作，供三维渲染使用。
`moonlib_s.terrain` 直接读取它们；下面的结论均已在本仓库中实测复核。

| 文件 | 尺寸 | 内容 |
|---|---|---|
| `ldem_64.tif` | 23040 × 11520（64 像素/度） | LOLA 高程，float32 **千米**，相对 1737.4 km 参考球 |
| `lroc_color.exr` | 27360 × 13680（76 像素/度） | LROC WAC 彩色镶嵌，half-float **线性** RGB，B/G/R 通道序 |
| `lroc_color_16bit_srgb.tif` | 27360 × 13680 | 同一图像，16-bit sRGB（LZW）；作为后备色彩源 |

### 必须知道的四点

1. **高程是「高度」不是「半径」，单位是千米。** PDS 的同名产品是「半米为单位的
   int16 半径」，SVS 的浮点版本把它除以 2000，于是得到千米高度：
   `表面半径 = 1737.4 km + 像素值`。数值是 0.5 m 的整数倍，因此金字塔缓存是无损的。
   实测全球范围 −9.115 … +10.757 km，与已发表的 LOLA 极值（−9.13 / +10.79）一致，
   极值位置也吻合（Antoniadi 坑 172.5°W 70.4°S；SPA 盆地边缘 158.6°W 5.4°N）。

2. **两张图都以 0° 经线为中心，而不是 PDS 的 0°E 起始。** 列 0 = 180°，经度向东
   递增，接缝在背面正中：
   `lon = (col + 0.5) / width × 360 − 180`，`lat = 90 − (row + 0.5) / height × 180`。
   按 PDS 约定解读会把所有月海放到错误的一侧。

3. **彩色图已经做过光度归一化，不要再除掉明暗。** 底层是 Hapke 归一化的 WAC 镶嵌
   （Sato et al. 2014），归一到入射角 60°、出射角 0°、相位角 60°。它既不是照片，
   也不是法向反照率，且经过曝光/白平衡调整，绝对值没有物理意义——只能当作相对反射率。
   两图网格不同（76 与 64 像素/度，比值 19/16），不构成整数抽取关系，须各自采样。

4. **彩色图的极区不是 LROC 数据。** LROC 覆盖 70°N–70°S，之外用较低分辨率的
   单色 LOLA 反照率图填补（占图幅 20% 以上像素），因此极区偏灰、无彩色。

### 为什么默认用 EXR

EXR 是线性、未截断的原始版本，正是渲染需要的；sRGB TIFF 需要额外假设传递函数，
而且是 LZW 压缩、无法按行随机读取。EXR 采用 ZIPS 压缩（每扫描线一个 zlib 流），
纯 Python + zlib 即可逐行解码，实测全图约 11 秒（TIFF 走 Pillow 约 18 秒）。

**两者等价，已实测。** 把 EXR 的线性值转成 sRGB 后与 TIFF 逐像素比对，
均方偏差约 0.009–0.013（8 位尺度）；用两条完全独立的解码路径各自烘焙金字塔后，
Mare Tranquillitatis / Tycho / Mare Imbrium / 背面高地的采样值在小数点后 3 位完全一致，
全球均值 0.4821（TIFF）与 0.4811（EXR）相差 0.2 %。因此 EXR 缺席时会自动改用 TIFF，
只是需要 Pillow（`pip install 'moonlib-s[tiff]'`；装了 matplotlib 就已经有）。

### 来源

- NASA SVS CGI Moon Kit：<https://svs.gsfc.nasa.gov/4720>
- PDS LOLA GDR 柱面网格：<https://pds-geosciences.wustl.edu/lro/lro-l-lola-3-rdr-v1/lrolol_1xxx/data/lola_gdr/cylindrical/>
- LROC WAC Hapke 归一化镶嵌：<https://data.lroc.im-ldi.com/lroc/view_rdr/WAC_HAPKE>
- Sato, Robinson, Hapke, Denevi, Boyd (2014), *JGR Planets* 119, 1775–1805,
  <https://doi.org/10.1002/2013JE004580>

首次使用时，`moonlib_s` 会把两张图预处理成金字塔缓存（`ldem_64` + EXR 约 350 MB；
换成 256 像素/度的全球高程则会到 3 GB 量级，写入 `<仓库>/.cache/moonlib_s/terrain/`，
可用 `MOONLIB_CACHE_DIR` 改位置）。

**不带任何栅格也能跑。** 星历、事件、可见性、天平动这些计算只依赖 SPICE 内核；
只有"画月面"需要高程。没有栅格时渲染会以一句说得清的错话停下——不是崩溃，
也不是悄悄降级成球面——那句话里就写着要下载哪个文件、放到哪里。


---

## 区域高分辨率高程（`regions/`，按需下载）

`ldem_64.tif` 的 64 像素/度（474 m）对整月面图完全够用，但对望远镜特写不够：
放大到 100 m/px 量级时它只是在插值。`tools/fetch_lola_global.py` 会从 USGS 的
**LOLA 全球 256 像素/度（赤道 118 m）** 产品里，用 HTTP range 请求只取需要的那一块，
存成 `regions/<name>.npy` + `.json`：

```bash
./.venv/bin/python tools/fetch_lola_global.py \
    --name clavius --lat -66.5 -50.5 --lon -29 1
./.venv/bin/python tools/fetch_lola_global.py --name clavius --verify
```

源文件 `Lunar_LRO_LOLA_Global_LDEM_118m_Mar2014.tif`（USGS Astrogeology，8.5 GB）
是每行一条 strip 的 BigTIFF，因此可以按行取；17° × 30° 的窗口约 60 MB、耗时约 1 分钟。
像素值是「半米为单位的 int16」，与 SVS 的高程同一基准球（1737.4 km），
所以解析后与 `ldem_64.tif` 直接可比——这一点由 `--verify` 逐点核对，而不是假设。

| 文件 | 尺寸 | 分辨率 | 覆盖 |
|---|---|---|---|
| `regions/clavius.npy` | 7680 × 4096 | 118 m/px | lon −29…1，lat −66.5…−50.5 |

实测与 `ldem_64` 的一致性：均值偏差 **0.2 m**、RMS **21 m**、最大 127 m。
均值偏差接近零是关键——经度或纬度差一个像素（118 m）会带来公里级的系统偏差，
而 21 m 的 RMS 正是「64 像素/度的平滑栅格」与「256 像素/度栅格」应有的差别。
