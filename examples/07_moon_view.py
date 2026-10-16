#!/usr/bin/env python3
"""模拟在地面观测到的月球 —— 从几何、光照到地照的完整链路。

这个示例演示 M2 的「观测模拟」能力：

1. 指定观测点与时刻，计算天平动、日下点、地球直下点等几何量；
2. 用 LOLA 高程与 LROC 色彩纹理渲染月面，含真实地形阴影与明暗界线；
3. 单独取出地照分量，看清暗面的亮度与纹理；
4. 导出一份可导入三维软件的高程网格。

输出写入 ``out/``（该目录不入版本库）。

    ./.venv/bin/python examples/07_moon_view.py
    ./.venv/bin/python examples/07_moon_view.py --at 2026-05-17T04:00:00

首次运行会先烘焙地形金字塔缓存（约 350 MB，十余秒），之后秒开。
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from _cli import add_output_argument, moonlib, shown  # noqa: E402
from moonlib.models import MoonRenderSettings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lat", type=float, default=30)
    parser.add_argument("--lon", type=float, default=120)
    parser.add_argument("--at", default="2026-05-25T15:30:00", help="ISO-8601 时刻")
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--samples", type=int, default=2, help="每轴子采样数")
    add_output_argument(parser)
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    site = moonlib.Observer(lat=args.lat, lon=args.lon, name="示例观测点")
    view = moonlib.view_geometry(site, args.at)

    print(moonlib.reporting.format_moon_view(view))

    settings = MoonRenderSettings(
        width=args.width,
        height=args.width,
        samples_per_pixel=args.samples,
        shadow_steps=48,
        # 日光与地照相差约 12 个星等，单次曝光不可能同时显示；
        # asinh（对数）拉伸近似暗适应后的目视观感。
        tone="visual",
        asinh_softening=1.0e-4,
    )

    started = time.time()
    render = moonlib.render_moon(view=view, settings=settings)
    elapsed = time.time() - started
    print(moonlib.reporting.format_render_summary(render))
    print(f"  渲染耗时 / elapsed      : {elapsed:.2f} s")

    # 目视观感（自适应 asinh 压缩，亮面与地照同时可见）
    visual = moonlib.plotting.write_radiance_png(render, out_dir / "moon_visual.png")
    # 单一线性曝光：只有被照亮的一侧，地照几乎全黑——这才是「拍到」的样子
    photographic = moonlib.apply_tone_mapping(
        render.sunlit + render.earthshine,
        tone="srgb",
        auto_exposure=True,
        reference=render.sunlit,
    )
    photo = moonlib.plotting.write_array_png(
        photographic, out_dir / "moon_photographic.png"
    )
    # 地照分量单独曝光，可以看清暗面的纹理
    earthshine = moonlib.plotting.write_radiance_png(
        render, out_dir / "moon_earthshine.png", component="earthshine"
    )
    # 线性 I/F 辐亮度堆栈，供 HDR 合成或定量分析
    hdr = moonlib.plotting.write_radiance_npy(render, out_dir / "moon_radiance.npy")

    print("\n  输出 / written")
    for path in (visual, photo, earthshine, hdr):
        print(f"    {shown(path)}  ({path.stat().st_size / 1024:.0f} KB)")

    # 三维数据：可见半球的高程网格，带顶点法线与纹理坐标
    mesh = moonlib.open_terrain().visible_mesh(view, level=3)
    ply = mesh.export_ply(out_dir / "moon_visible.ply")
    print(f"    {shown(ply)}  "
          f"({mesh.n_vertices} 顶点 / {mesh.n_faces} 面, "
          f"{ply.stat().st_size / 1024 ** 2:.1f} MB)")

    print(
        "\n  提示 / notes\n"
        "    · moon_visual.png       —— 目视近似（自适应 asinh 压缩）\n"
        "    · moon_photographic.png —— 单一 sRGB 曝光；地照低约 12 个星等，必然不可见\n"
        "    · moon_earthshine.png   —— 地照分量单独曝光，暗面纹理清晰\n"
        "    · moon_radiance.npy     —— 线性 I/F 堆栈 (h, w, 3, 3)：总、日照、地照\n"
        "    · 后两者按各自曝光相加，即真实地照照片的 HDR 合成做法"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
