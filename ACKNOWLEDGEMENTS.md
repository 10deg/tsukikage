# 致谢与算法来源 / Acknowledgements

本文件记录 Tsukikage 使用或**借鉴**的外部来源。凡"借鉴"都只取**方法与思路**，代码为独立实现：
Tsukikage 以 **MIT** 许可发布（见 [`LICENSE`](LICENSE)），不包含任何 GPL 代码，
也不因借鉴而受 GPL 约束。

## MoonLabeler（Fluorine7）— 标注避让算法思路

- 来源：<https://github.com/Fluorine7/MoonLabeler>，**GPL**，作者 Fluorine7。
- 借鉴范围：`moonlabeler/layout.py` 中标签**避让（label placement / collision avoidance）**
  的算法思路，用于 `moonlib.annotations` 的标注排版。**未复制任何代码**，仅按下述方法
  独立实现；接口、数据结构与命名均为 Tsukikage 自有。
- 借鉴的方法，归纳起来是四点：
  1. **候选位与起算距离**：以若干方向 × 若干距离档生成候选，斜角方向优先；带轮廓的地貌先把
     引线投射到轮廓之外，caption 从轮廓外缘起算，该方向无交点则弃用。
  2. **空间索引 + 硬约束 + 评分**：文字框、引线、中心标记各用一套均匀网格索引，只查询邻域
     内的候选，避免全对全比较；候选须同时满足"框不压框、框不压标记、引线不穿框、引线不
     交叉、引线不穿标记"，再按冲突数与引线长度评分，其中净空奖励**封顶**，以免 caption
     被吸到画面空白的角落。
  3. **整体重排与预算**：贪心放置后，对未固定标签再做有界轮次的重定位；标签数量受预算限制
     时从储备池按优先级补位，且补位者不得挤掉已放置的高优先级 caption。
  4. **重要标注优先**：重要标注先在**空画面**上排布——此时只有其他重要者的标记作为障碍，
     因此一个无名小点无法否决地标的位置；普通标注随后排布，冲突时让位的是普通点。重要标注
     在近距档全部失败后，还可尝试远距档。
- Tsukikage 侧落点：`src/moonlib/annotations.py`（`plan_annotations` 及其布局函数）。
- 说明：以上为算法思想的独立实现。若日后要直接引用它的代码，合并后的作品须整体按 GPL
  发布——MIT 与 GPL 兼容，这样做是允许的——目前**没有**这样做。

## 其他方法参考

- **LTVT（Lunar Terminator Visualization Tool）**，Jim Mosher 与 Henrik Bondo：
  面向业余观测者的月面几何、已知地形校准与高程光照模拟。Tsukikage 的**明暗界线与地形阴影**
  做法参考了这些公开方法，以 Python 独立实现，**未使用其 Delphi 源码**，也不是原作者发布的
  LTVT 版本。<https://github.com/pavolgaj/ltvt>

## 数据来源

| 来源 | 用途 | 出处与许可 |
| --- | --- | --- |
| **IAU 行星命名录** | 地貌的名称、位置、直径与官方分类（含着陆点 Statio） | IAU Working Group for Planetary System Nomenclature / USGS Astrogeology，<https://planetarynames.wr.usgs.gov/Page/MOON/target>，公有领域 |
| **Virtual Moon Atlas** | 地貌**分类**（TYPE / SUBTYPE / IAU_FEATURE_TYPE）与目录覆盖 | <https://www.ap-i.net/avl/en/start>，Christian Legrand & Patrick Chevalley；VMA 免费发布 |
| **Virtual Moon Atlas 的月球地貌数据分类** | **目录扩充**：穹丘、火成碎屑、坑洞、IMP、撞击盆地、蜿蜒月溪、历史遗址等共 1 178 条并入数据库 | 同 VMA；这些条目**暂时以英文原名显示**，`Note` 列写明「待译名」 |
| **LPND 月球与行星地名库** | 中文译名（`zh`）与完整词条名（`term_zh`，如「维萨里坑」） | 中国科学院国家天文台 国家天文科学数据中心 <https://lpnd.bao.ac.cn/>；部分条目附 NADC 天文学名词词典链接 |
| **The Lunar 100** | 月面目视观测目标清单，以及“Lunar 100”分类标注（编号、名称、中文名、经纬度、直径） | Charles A. Wood, “Lunar Notebook: Introducing the Lunar 100”, *Sky & Telescope*, 2004 年 4 月号, p. 113；回顾文 <https://skyandtelescope.org/observing/the-lunar-100/>。本仓库按用户提供的 `out/Lunar 100.xlsx` 整理为 `src/moonlib/data/lunar100.csv`；列表是事实性目录，署名与引用归原作者 |
| **LOLA 全球地形 256 像素/度** | 月面高程与阴影（特写） | NASA / GSFC / MIT，USGS Astrogeology 发布，公有领域；int16 半米，相对 1737.4 km 参考球 |
| **SLDEM2015 512 像素/度** | ±60° 内的月面高程（轨道缝由立体像对填实） | NASA / GSFC / MIT 的 LOLA 团队与 JAXA / SELENE Terrain Camera 团队；Barker et al. (2016), “A new lunar digital elevation model from the Lunar Orbiter Laser Altimeter and SELENE Terrain Camera”, *Icarus* 273, 346–355；PDS 产品目录 <https://imbrium.mit.edu/DATA/SLDEM2015/>。本仓库的全球 2:1 版本在 ±60–90° 复用上面的 LOLA 512 极区 |
| **NASA SVS CGI Moon Kit** | 整月面高程（float32 千米）与色彩 | NASA SVS <https://svs.gsfc.nasa.gov/4720>（Ernie Wright / USRA）及 LRO / LROC / LOLA 团队，公有领域；色彩是 LROC WAC 的 Hapke 归一化镶嵌（Sato et al. 2014, *JGR Planets* 119, 1775–1805, <https://doi.org/10.1002/2013JE004580>），**相对反射率**，不是照片 |
| SPICE 星历内核（DE440/DE441、`moon_pa`、地球定向等） | 月球与太阳的位置、天平动 | NAIF/JPL <https://naif.jpl.nasa.gov/pub/naif/generic_kernels/>、ESA SPICE Service |

各数据文件的生成过程、匹配规则与未匹配项记录在同目录的 `*.provenance.json` 中；
中文名一律以 LPND 为准，目录的匹配与再分类由 `tools/merge_out_catalogue.py` 完成。

## 反照率与光度参数

这一节集中记录代码里实际用到的反照率/光度数值，避免把“几何反照率”“法向反照率”“Bond 反照率”混在一起。

| 量 | 本项目取值/范围 | 说明 | 文献与来源 |
| --- | --- | --- | --- |
| 地球几何反照率 \(p_E\) | **0.367**（默认；界面允许 0–1） | 与 Lambert 球相位函数配合使用的可见光“有效几何反照率”；不是现代地球反照率测量值 | Allen (1976), *Astrophysical Quantities*, 3rd ed., Athlone Press；0.367 的出处与历史背景见 Robinson (2026), *PSJ* 7, 12, <https://doi.org/10.3847/PSJ/ae2ec6>（arXiv:2507.22258） |
| 地球几何反照率（文献范围） | 约 **0.20–0.43**（可见光/V 波段） | 随波段、相位函数、Earthshine/卫星数据处理方式而变；不是一个常数 | 0.20：Mallama (2009) 对宽带 Earthshine 的外推；0.367：Allen (1976)；0.434：Mallama, Krobusek & Pavlov (2017) V 波段及 NASA Fact Sheet；0.242：Robinson (2026) |
| 地球几何反照率（现代参考） | **0.434**（V 波段）；Bond 反照率 **0.294** | 现代常引用的几何反照率；与上面的 0.367 相差约 0.07 | NASA Earth Fact Sheet（D. R. Williams, 2024-11-15），<https://nssdc.gsfc.nasa.gov/planetary/factsheet/earthfact.html>；Mallama, Krobusek & Pavlov (2017), *Icarus* 282, 19, <https://doi.org/10.1016/j.icarus.2016.09.023>；Mallama (2009), *Icarus* 204, 11, <https://doi.org/10.1016/j.icarus.2009.07.010> |
| 地球视觉几何反照率（最新重估） | **0.242** \(^{+0.005}_{-0.004}\)（0.4–0.7 μm） | 2025/2026 年基于 Earthshine、DSCOVR、EPOXI、Galileo、LCROSS 的联合重估，比旧值低 30–40% | Robinson (2026), *PSJ* 7(1), 12, <https://doi.org/10.3847/PSJ/ae2ec6>；arXiv:2507.22258 |
| 地球相位函数 | Lambert 球：\(\Phi(\beta)=[\sin\beta+(\pi-\beta)\cos\beta]/\pi\) | 代码中的解析近似，不是实测地球相位曲线；换用实测相位曲线时，几何反照率需要重新归一化 | 标准 Lambert 球；实测地球相位曲线见 Robinson (2026)；Goode et al. (2001), *GRL* 28, 1671–1674, <https://doi.org/10.1029/2000GL012580> |
| 地球 HG 相位拟合 | \(f=0.23,\ g=-0.33\) | Robinson (2026) 的“临时解析相位函数”：\(f P_{HG}(180^\circ-\alpha;g)/P_{HG}(180^\circ;g)\)，在满地球归一化；地照曲线的 `robinson_hg` 选项使用它 | Robinson (2026), *PSJ* 7(1), 12, §V.3 |
| 月球全球平均法向反照率 | **0.136**（可见光，全球平均） | 把 LROC 相对反射率图换算成绝对反射率的唯一标定常数；在“日照/地照”比值中会抵消 | Lane & Irvine (1973), “Monochromatic phase curves and albedos for the lunar disk”, *AJ* 78, 267–277 |
| 月球几何/Bond 反照率 | 几何 **0.12**；Bond **0.11** | 整月面参考值，和上面的“法向反照率”定义不同，不能直接互换 | NASA Moon Fact Sheet（D. R. Williams, 2024-01-11），<https://nssdc.gsfc.nasa.gov/planetary/factsheet/moonfact.html> |
| 月面法向反照率的地表范围 | 月海约 **0.07–0.10**，高地约 **0.15–0.20**，新鲜溅射物约 **0.25–0.30** | 这是地貌尺度的典型范围；代码只使用全球平均 0.136，地貌差异来自 LROC 相对反射率图 | *Lunar Sourcebook* (Heiken, Vaniman & French, 1991, Cambridge Univ. Press)；Sato et al. (2014), *JGR Planets* 119, 1775–1805, <https://doi.org/10.1002/2013JE004580> |
| 月球-Lambert 光度系数 | \(A_1=-0.019,\ A_2=0.000242,\ A_3=-0.00000146\) | 默认光度模型 `lunar_lambert` 的相位权重 \(L(\alpha)\) | McEwen (1991), “Photometric functions for photoclinometry and other applications”, *Icarus* 92, 298–311 |
| Hapke 参数 | \(w=0.382,\ g=0.042,\ B_0=0.145,\ h=0.060,\ B_c=1.011,\ h_c=0.0154\) | **本仓库按 Clementine 冲效应锚点重新拟合的值**，不是 Hapke 原文的直接推荐值；不要把它们当成文献常数 | Hapke (2002), *Icarus* 157, 523–534；Hapke (1984), *Icarus* 59, 41–59；Buratti, Hillier & Wang (1996), *Icarus* 124, 490–499；拟合过程见 `docs/07` 第 7.1 节 |
| 月球整盘星等拟合 | \(m=-12.73+0.026|\alpha|+4\times10^{-9}\alpha^4+5\log_{10}(d/d_0)\) | 经验拟合，不是反照率；新月附近不可用 | Allen (1976), *Astrophysical Quantities*；Russell (1916), *ApJ* 43, 103 |
| LROC WAC 色彩镶嵌 | Hapke 归一化到 \(i=60^\circ,e=0^\circ,g=60^\circ\)；**相对**反射率 | 不是绝对法向反照率，也不是照片；SVS 还调整过曝光和白平衡 | Sato et al. (2014), *JGR Planets* 119, 1775–1805；NASA SVS CGI Moon Kit, <https://svs.gsfc.nasa.gov/4720> |

**完整引用**：

- Allen, C. W. 1976, *Astrophysical Quantities*, 3rd ed. (London: Athlone Press).
- Buratti, B. J., Hillier, J. K., & Wang, M. 1996, “The lunar opposition effect: A test of alternative models”, *Icarus* 124, 490.
- Goode, P. R., Qiu, J., Yurchyshyn, V., et al. 2001, “Earthshine observations of the Earth’s reflectance”, *Geophysical Research Letters* 28, 1671–1674, <https://doi.org/10.1029/2000GL012580>.
- Hapke, B. 1984, “Bidirectional reflectance spectroscopy: 3. Correction for macroscopic roughness”, *Icarus* 59, 41–59.
- Hapke, B. 2002, “Bidirectional reflectance spectroscopy: 5. The coherent backscatter opposition effect and anisotropic scattering”, *Icarus* 157, 523–534.
- Lane, A. P., & Irvine, W. M. 1973, “Monochromatic phase curves and albedos for the lunar disk”, *Astronomical Journal* 78, 267–277.
- Mallama, A. 2009, “Characterization of terrestrial exoplanets based on the phase curves and albedos of Mercury, Venus and Mars”, *Icarus* 204, 11, <https://doi.org/10.1016/j.icarus.2009.07.010>.
- Mallama, A., Krobusek, B., & Pavlov, H. 2017, “Comprehensive wide-band magnitudes and albedos for the planets, with applications to exo-planets and Planet Nine”, *Icarus* 282, 19, <https://doi.org/10.1016/j.icarus.2016.09.023>.
- McEwen, A. S. 1991, “Photometric functions for photoclinometry and other applications”, *Icarus* 92, 298–311.
- Robinson, T. D. 2026, “Inferring and Interpreting the Visual Geometric Albedo and Phase Function of Earth”, *The Planetary Science Journal* 7(1), 12, <https://doi.org/10.3847/PSJ/ae2ec6>；arXiv:2507.22258.
- Russell, H. N. 1916, “The stellar magnitudes of the Sun, Moon and planets”, *Astrophysical Journal* 43, 103.
- Sato, H., Robinson, M. S., Hapke, B., et al. 2014, “Resolved Hapke parameter maps of the Moon”, *Journal of Geophysical Research: Planets* 119, 1775–1805, <https://doi.org/10.1002/2013JE004580>.
- NASA Earth Fact Sheet, D. R. Williams (curator), 2024-11-15, <https://nssdc.gsfc.nasa.gov/planetary/factsheet/earthfact.html>.
- NASA Moon Fact Sheet, D. R. Williams (curator), 2024-01-11, <https://nssdc.gsfc.nasa.gov/planetary/factsheet/moonfact.html>.

地照曲线提供的地球模型选项：

- `simulation`：跟随“模拟设置”里的地球几何反照率，相位函数为 Lambert；
- `allen`：Allen (1976) 的 0.367，Lambert；
- `vband`：现代 V 波段 0.434，Lambert；
- `robinson`：Robinson (2026) 的 0.242，Lambert；
- `robinson_hg`：Robinson (2026) 的 HG 拟合 \(f=0.23,\ g=-0.33\)。

这些选项只改变地照曲线，不改变主界面的月面渲染。

**使用注意**：

- 地球的 0.367 是旧 Allen 表中的“有效几何反照率”，与 Lambert 球配合可复现“满地球约 -16.8 等”的常用说法；它不是 Bond 反照率，也不等于最新的 0.242 或 0.434。若要做绝对测光，必须明确选择哪一套值并说明波段/相位函数。
- 月球的 0.136 是**法向反照率**；NASA 的 0.12/0.11 分别是**几何/Bond 反照率**，定义不同，不能直接拿来做同一个归一化。
- 地照计算中，月球法向反照率 0.136 在“地照/阳光”比值里会抵消；地球几何反照率不会抵消，它直接线性缩放地照强度。

## 软件依赖

| 依赖 | 用途 | 许可 |
| --- | --- | --- |
| [SpiceyPy](https://github.com/AndrewAnnex/SpiceyPy) | SPICE 内核的 Python 绑定 | MIT |
| [NumPy](https://numpy.org/) | 全部数值计算与渲染 | BSD-3 |
| [Matplotlib](https://matplotlib.org/)（可选） | 天区图、月相图、图表 | PSF/BSD |
| [Pillow](https://python-pillow.org/)（可选） | 读 sRGB TIFF 色彩源、写 HDR 增益图 JPEG | MIT-CMU |
| [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg)（可选） | 动画导出；自带静态 ffmpeg | BSD-2 / ffmpeg 为 LGPL/GPL |
| [ffmpeg](https://ffmpeg.org/)（可选） | 同上，若系统已装则优先用系统的 | LGPL/GPL |

**没有任何 GPL 代码进入 `src/`**：ffmpeg 只作为**外部可执行文件**被调用，
不链接、不打包进库。上面那条"独立实现"的说明同样适用于所有借鉴来源。

## 关于本文档

`docs/` 目录（架构、API 契约、逐条缺陷证据与实测数据、交付记录）是开发过程的记录，
**不随仓库发布**；本文件与 `README.md`、`kernels/README.md` 是仓库里保留的说明文字。
