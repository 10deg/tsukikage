# 致谢与算法来源 / Acknowledgements

本文件记录 Tsukikage 使用或**借鉴**的外部来源。凡"借鉴"都只取**方法与思路**，代码为独立实现：
Tsukikage 以 **MIT** 许可发布（见 [`LICENSE`](LICENSE)），不包含任何 GPL 代码，
也不因借鉴而受 GPL 约束。

## MoonLabeler（Fluorine7）— 标注避让算法思路

- 来源：<https://github.com/Fluorine7/MoonLabeler>，**GPL**，作者 Fluorine7。
- 借鉴范围：`moonlabeler/layout.py` 中标签**避让（label placement / collision avoidance）**
  的算法思路，用于 `moonlib_s.annotations` 的标注排版。**未复制任何代码**，仅按下述方法
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
- Tsukikage 侧落点：`src/moonlib_s/annotations.py`（`plan_annotations` 及其布局函数）。
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
| **The Lunar 100** | 月面目视观测目标清单，以及“Lunar 100”分类标注（编号、名称、中文名、经纬度、直径） | Charles A. Wood, “Lunar Notebook: Introducing the Lunar 100”, *Sky & Telescope*, 2004 年 4 月号, p. 113；回顾文 <https://skyandtelescope.org/observing/the-lunar-100/>。本仓库按用户提供的 `out/Lunar 100.xlsx` 整理为 `src/moonlib_s/data/lunar100.csv`；列表是事实性目录，署名与引用归原作者 |
| **LOLA 全球地形 256 像素/度** | 月面高程与阴影（特写） | NASA / GSFC / MIT，USGS Astrogeology 发布，公有领域；int16 半米，相对 1737.4 km 参考球 |
| **NASA SVS CGI Moon Kit** | 整月面高程（float32 千米）与色彩 | NASA SVS <https://svs.gsfc.nasa.gov/4720>（Ernie Wright / USRA）及 LRO / LROC / LOLA 团队，公有领域；色彩是 LROC WAC 的 Hapke 归一化镶嵌（Sato et al. 2014, *JGR Planets* 119, 1775–1805, <https://doi.org/10.1002/2013JE004580>），**相对反射率**，不是照片 |
| SPICE 星历内核（DE440/DE441、`moon_pa`、地球定向等） | 月球与太阳的位置、天平动 | NAIF/JPL <https://naif.jpl.nasa.gov/pub/naif/generic_kernels/>、ESA SPICE Service |

各数据文件的生成过程、匹配规则与未匹配项记录在同目录的 `*.provenance.json` 中；
中文名一律以 LPND 为准，目录的匹配与再分类由 `tools/merge_out_catalogue.py` 完成。

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
