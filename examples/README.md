# 示例 / Examples

这些脚本由原仓库的 notebook 提炼而来，**只调用公共 API**，没有任何复制粘贴的算法。
每个脚本都可以直接运行：

```bash
./.venv/bin/python examples/01_moon_report.py
```

用哪个解释器：`.venv` 里装了 numpy / spiceypy / matplotlib，建议一律用
`./.venv/bin/python`。脚本开头有一段 bootstrap：如果 `import moonlib` 失败，
会自动把仓库的 `src/` 加进 `sys.path`，所以**没有做可编辑安装**也能直接运行；
如果连解释器本身都缺依赖，会打印出应当使用的解释器路径。

需要出图的脚本会把 PNG 写到 `out/`（该目录不入版本库）。

`07_moon_view.py` 首次运行前会先把月面栅格烘焙成金字塔缓存（约 350 MB，十余秒），
写入仓库内的 `.cache/`（同样不入版本库）。也可以先手动烘焙：

```bash
./.venv/bin/python tools/bake_terrain.py            # 默认层级，约 350 MB
./.venv/bin/python tools/bake_terrain.py --full     # 保留 64 像素/度的高程
./.venv/bin/python -m moonlib terrain --clear       # 清理
```

局部特写（`08_region_closeup.py`）还需要一块区域高分辨率高程，按需下载一次：

```bash
./.venv/bin/python tools/fetch_lola_global.py \
    --name clavius --lat -66.5 -50.5 --lon -29 1     # 约 60 MB，1 分钟
```

| 脚本 | 对应重构前的 notebook | 演示内容 |
|---|---|---|
| `01_moon_report.py` | `moon.ipynb` | 综合月况报告、轻量状态查询、JSON 载荷 |
| `02_phase_calendar.py` | `moon_phase_query.ipynb` | 全年朔望弦历表、视直径统计与散点图 |
| `03_crescent_windows.py` | `crescent_plot.ipynb`、`extreme_crescent.ipynb` | 月牙窗口搜索、评分排名、天区图 |
| `04_libration_targets.py` | `libration_targets_prediction.ipynb` | 边缘目标扫描、统计、最佳窗口 |
| `05_illumination_search.py` | `moon_calc.ipynb` | 按照明率反查、超级月亮/微月排名 |
| `06_zenith_full_moons.py` | `moon_calc.ipynb` cell 8 | 中天最接近天顶的满月（可用 DE441 搜索千年尺度） |
| `07_moon_view.py` | — （M2 新增） | **月面观测模拟**：几何 + 渲染 + 地照分量 + 三维网格导出 |
| `08_region_closeup.py` | — （M2 新增） | **月面局部特写**：以克拉维斯环形山为例，对比 64 与 256 像素/度高程 |
| `09_photograph.py` | — （M2 新增） | **一次拍摄**：一条命令出一张图，仪器与后期全参数化 |

> 第二列指的是**重构前 moonlib 自带的那批示例笔记本**。它们是那个旧库的输出，不随本仓库
> 发布；这一列只说清每个示例替换了哪一段旧用法，不是在指仓库里的文件。
