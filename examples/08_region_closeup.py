#!/usr/bin/env python3
"""月球局部特写 —— 以克拉维斯环形山（Clavius）为例。

演示 M2 的「局部特写」能力：

1. 指定月面坐标与视场，算出该处的出射角、入射角与地元分辨率；
2. 判断现有高程数据够不够——不够时输出只是在插值放大；
3. 分别用 64 像素/度与 256 像素/度的 LOLA 高程渲染同一取景，直接对比。

用法：

    ./.venv/bin/python examples/08_region_closeup.py
    ./.venv/bin/python examples/08_region_closeup.py --span-km 60 --width 1000

区域高分辨率高程需要先取一次（约 60 MB，1 分钟）：

    ./.venv/bin/python tools/fetch_region_dem.py \\
        --name clavius --lat -66.5 -50.5 --lon -29 1

缺失时会自动跳过对比，只用全局高程渲染。
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from _cli import add_output_argument, moonlib, shown  # noqa: E402
from moonlib.models import MoonRenderSettings  # noqa: E402

# 克拉维斯环形山：直径 231 km，是月面南半球最大的环形山之一，
# 坑底那道由大到小排列的撞击坑链是它的标志。
CLAVIUS_LON, CLAVIUS_LAT = -14.4, -58.4
# 这一时刻太阳在目标处高约 19°，坑壁投影最长，地形最易读。
WHEN = "2026-05-28T14:00:00"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lat", type=float, default=30)
    parser.add_argument("--lon", type=float, default=120)
    parser.add_argument("--at", default=WHEN)
    parser.add_argument("--target-lon", type=float, default=None)
    parser.add_argument("--target-lat", type=float, default=None)
    parser.add_argument("--width", type=int, default=1200)
    parser.add_argument("--samples", type=int, default=2)
    parser.add_argument("--feature", default="Clavius",
                        help="catalogue name; the frame is sized to fit it")
    parser.add_argument("--span-km", type=float, default=None,
                        help="overrides the automatic framing")
    add_output_argument(parser)
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    site = moonlib.Observer(lat=args.lat, lon=args.lon, name="示例观测点")
    view = moonlib.view_geometry(site, args.at)

    feature = moonlib.find_feature(args.feature)
    target_lon = feature.lon_deg if args.target_lon is None else args.target_lon
    target_lat = feature.lat_deg if args.target_lat is None else args.target_lat
    if args.span_km is None:
        span = moonlib.required_sky_span_km(
            view, target_lon, target_lat, feature.diameter_km, margin=1.25
        )
        how = f"自动取景 / auto, fits {feature.diameter_km:.0f} km"
    else:
        span = args.span_km
        how = "手动 / manual"

    print(f"观测地 / site       : {site.name}  illum {view.illuminated_percent:.1f}%")
    print(f"目标 / target       : {feature.label}  "
          f"{feature.diameter_km:.0f} km  ({target_lon:+.2f}, {target_lat:+.2f})")
    print(f"跨度 / span         : {span:.0f} km 天球 / sky  ({how})")

    # 两套高程：烘焙好的整月面金字塔（ldem_64，约 474 m/px）
    # 与本地完整 LOLA 全球高程按需裁出的窗口（256 像素/度，约 118 m/px）。
    sources = moonlib.global_dem_sources()
    coarse = moonlib.open_terrain()
    fine = None
    print("  本地高程 / local DEMs:")
    for path, km in sources:
        print(f"    {path.name:<48} {km * 1000:7.1f} m/px")
    if sources and sources[0][1] < coarse.dem.km_per_pixel(0) * 0.9:
        fine = "auto"
    else:
        print("    （未找到更细的全球高程；用 tools/fetch_lola_global.py 获取，"
              "之后任意目标都能自动取到 118 m/px）")

    settings = MoonRenderSettings(
        width=args.width, height=args.width,
        samples_per_pixel=args.samples, shadow_steps=64, tone="srgb",
    )

    results = []
    for tag, terrain in (("ldem64_cached", coarse), ("lola256_offline", fine)):
        if tag.startswith("lola") and terrain is None:
            continue
        terrain = None if terrain == "auto" else terrain  # None -> auto window
        try:
            # Cheap pre-flight: region_geometry is what rejects a far-side
            # target, so asking it first avoids paying for a doomed render.
            moonlib.region_geometry(view, target_lon, target_lat, span, terrain=terrain)
        except moonlib.MoonlibError as error:
            print(f"  {tag}: {error}")
            continue

        started = time.time()
        render = moonlib.render_region(
            view, target_lon, target_lat, span_km=span,
            settings=settings, terrain=terrain,
        )
        elapsed = time.time() - started

        print(f"\n  === {tag} ===")
        print(moonlib.reporting.format_region_summary(render))
        print(f"  渲染 / render         : {elapsed:.1f} s")

        path = moonlib.plotting.write_radiance_png(
            render, out_dir / f"{feature.name_en.lower()}_{tag}.png"
        )
        print(f"  wrote {shown(path)}")
        results.append((tag, render, path))

    if len(results) == 2:
        # 同一取景、同一像素的 1:1 并排对比，最能说明分辨率差别。
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            print("\n  （未安装 Pillow，跳过并排对比图）")
            return 0
        Image.MAX_IMAGE_PIXELS = None
        images = [Image.open(str(p)) for _tag, _r, p in results]
        size = min(i.size[0] for i in images)
        box = (int(size * 0.22), int(size * 0.28))
        side = int(size * 0.44)
        crops = [i.crop((box[0], box[1], box[0] + side, box[1] + side)) for i in images]
        gap, label = 12, 26
        canvas = Image.new(
            "RGB", (side * 2 + gap, side + label), (12, 14, 20)
        )
        draw = ImageDraw.Draw(canvas)
        for index, (tag, _r, _p) in enumerate(results):
            x = index * (side + gap)
            canvas.paste(crops[index], (x, label))
            ratio = results[index][1].region.resolution_ratio
            draw.text((x + 6, 7),
                      f"{tag}  (terrain/sample = {ratio:.2f}x)", fill=(230, 230, 230))
        compare = out_dir / f"{feature.name_en.lower()}_compare.png"
        canvas.save(compare, optimize=True)
        print(f"\n  wrote {shown(compare)}  (1:1 side-by-side)")

    print(
        "\n  读法 / how to read\n"
        "    · resolution_ratio > 1 表示输出像素小于地形像素，即在插值放大\n"
        "    · surface span 在倾斜方向上大于 span_km 是正常的：地表越倾斜，\n"
        "      同样的天球跨度覆盖的地面越多"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
