#!/usr/bin/env python3
"""模拟一次月面拍摄：一条命令，一张图。

从观测地、时刻与地貌出发，自动取景，按给定的望远镜与相机成像，再做后期锐化与
成片处理。中间结果不落盘，也不打印说明性文字。

    ./.venv/bin/python examples/09_photograph.py --feature 蛇谷 --at 2026-09-23T14:00:00

常用参数：

    --aperture-in 16 --focal-ratio 10     口径与焦比（或用 --focal-length-mm）
    --barlow 2                            增倍镜倍率
    --obstruction 0.34                    副镜遮挡比
    --fov-radius-arcsec 160               视场半径（或用 --margin / --cover-km）
    --rotation-deg 90                     相机旋转角
    --width 1200                          出图尺寸（或用 --arcsec-per-pixel）
    --seeing 1.0 --frame-selection-gain 3 视宁度与帧选择增益
    --blur-strength 1.0                   模糊强弱总开关（0 = 不模糊）
    --sharpen-radius 1.5 --sharpen-strength 1.0
    --saturation 0.0 --gain 0.9           成片处理
"""

from __future__ import annotations

import argparse
import re
import unicodedata
from pathlib import Path

from _cli import add_output_argument, moonlib_s, shown  # noqa: E402
from moonlib_s.models import MoonRenderSettings  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    where = parser.add_argument_group("观测 / where and when")
    where.add_argument("--lat", type=float, default=30)
    where.add_argument("--lon", type=float, default=120)
    where.add_argument("--site-name", default="Test Site")
    where.add_argument("--at", default="2026-09-23T14:00:00", help="UTC")

    frame = parser.add_argument_group("取景 / framing")
    frame.add_argument("--feature", default="蛇谷", help="目录中的地貌名")
    frame.add_argument("--target-lon", type=float, default=None)
    frame.add_argument("--target-lat", type=float, default=None)
    frame.add_argument("--margin", type=float, default=1.25,
                       help="目标直径之外的余量倍数")
    frame.add_argument("--cover-km", type=float, default=None,
                       help="直接指定要装下的直径，覆盖 --margin")
    frame.add_argument("--fov-radius-arcsec", type=float, default=None,
                       help="视场半径（角秒），覆盖上面的自动取景")
    frame.add_argument("--rotation-deg", type=float, default=0.0,
                       help="相机绕视轴旋转，画面内逆时针为正")
    frame.add_argument("--width", type=int, default=1200, help="出图边长（像素）")
    frame.add_argument("--arcsec-per-pixel", type=float, default=None,
                       help="出图采样，覆盖 --width")

    optics = parser.add_argument_group("仪器 / optics")
    optics.add_argument("--aperture-in", type=float, default=16.0)
    optics.add_argument("--focal-ratio", type=float, default=10.0)
    optics.add_argument("--focal-length-mm", type=float, default=None,
                        help="直接给焦距，覆盖 --focal-ratio")
    optics.add_argument("--barlow", type=float, default=1.0)
    optics.add_argument("--obstruction", type=float, default=0.34)

    sky = parser.add_argument_group("大气 / atmosphere")
    sky.add_argument("--seeing", type=float, default=1.0,
                     help="站点长曝光视宁度 FWHM（角秒），0 表示无大气")
    sky.add_argument("--frame-selection-gain", type=float, default=3.0,
                     help="帧选择相对长曝光视宁度的改善倍数；1.0 为单帧")

    post = parser.add_argument_group("后期 / processing")
    post.add_argument("--blur-strength", type=float, default=1.0,
                      help="模糊强弱总开关：1 按模型，0 完全不模糊")
    post.add_argument("--sharpen-radius", type=float, default=1.2,
                      help="锐化半径 = 该倍数 x 有效视宁度")
    post.add_argument("--sharpen-radius-arcsec", type=float, default=None,
                      help="直接给锐化半径（角秒），覆盖上面的倍数")
    post.add_argument("--sharpen-strength", type=float, default=1.0)
    post.add_argument("--saturation", type=float, default=1.0, help="0 为完全去色")
    post.add_argument("--gain", type=float, default=1.0, help="成片增益，<1 变暗")

    add_output_argument(parser, default=None)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    feature = moonlib_s.find_feature(args.feature)
    lon = feature.lon_deg if args.target_lon is None else args.target_lon
    lat = feature.lat_deg if args.target_lat is None else args.target_lat
    site = moonlib_s.Observer(lat=args.lat, lon=args.lon, name=args.site_name)
    view = moonlib_s.view_geometry(site, args.at)

    telescope = moonlib_s.Telescope(
        aperture_mm=args.aperture_in * 25.4,
        focal_ratio=None if args.focal_length_mm else args.focal_ratio,
        focal_length_mm=args.focal_length_mm,
        barlow=args.barlow,
        obstruction=args.obstruction,
        name=f'{args.aperture_in:g}in f/{args.focal_ratio:g}',
    )
    # The pixel size feeds the blur budget below -- a 2.4 um pixel at this plate
    # scale is itself a term in the total point-spread -- which is why the
    # sensor is built here even though the picture's own size comes from
    # --width.  This renderer is told how many pixels to trace and picks the
    # plate scale to suit the feature; the sensor describes the camera, not the
    # file.
    # No Camera here, deliberately.  The blur budget is parameterised by the
    # *plate scale* the picture was rendered at, not by a pixel pitch, so a
    # --sensor/--pixel-um pair that fed nothing would be two knobs that silently
    # did nothing -- which is what they were.  The frame's size comes from
    # --width; the instrument's resolution enters through arcsec_per_pixel.
    # --- 取景 -------------------------------------------------------------
    frame_kwargs = {}
    if args.fov_radius_arcsec is not None:
        frame_kwargs["fov_radius_arcsec"] = args.fov_radius_arcsec
    elif args.cover_km is not None:
        frame_kwargs["cover_km"] = args.cover_km
    else:
        frame_kwargs["cover_km"] = feature.diameter_km

    render = moonlib_s.render_region(
        view, lon, lat,
        settings=MoonRenderSettings(
            width=args.width, height=args.width, samples_per_pixel=2,
            shadow_steps=64, tone="linear", keep_components=True,
        ),
        margin=args.margin,
        rotation_deg=args.rotation_deg,
        width=args.width,
        height=args.width,
        arcsec_per_pixel=args.arcsec_per_pixel,
        **frame_kwargs,
    )

    # --- 成像与后期（全部在线性光上）---------------------------------------
    exposure = render.stats["exposure_scale"]
    linear = (render.sunlit + render.earthshine) * exposure

    blurred = moonlib_s.instrument_blur(
        linear, telescope,
        arcsec_per_pixel=render.stats["arcsec_per_pixel"],
        seeing_fwhm_arcsec=args.seeing,
        frame_selection_gain=args.frame_selection_gain,
        strength=args.blur_strength,
    )
    sharpened = moonlib_s.instrument_sharpen(
        blurred, telescope,
        arcsec_per_pixel=render.stats["arcsec_per_pixel"],
        seeing_fwhm_arcsec=args.seeing,
        frame_selection_gain=args.frame_selection_gain,
        radius_factor=args.sharpen_radius,
        radius_arcsec=args.sharpen_radius_arcsec,
        strength=args.sharpen_strength,
    )
    display = moonlib_s.apply_tone_mapping(
        sharpened, tone="srgb", auto_exposure=False, exposure_ev=0.0
    )
    finished = moonlib_s.finish_image(
        display, saturation=args.saturation, gain=args.gain
    )

    # --- 输出 -------------------------------------------------------------
    slug = unicodedata.normalize("NFKD", feature.name_en)
    slug = "".join(c for c in slug if not unicodedata.combining(c))
    slug = re.sub(r"[^a-z0-9]+", "_", slug.lower()).strip("_")
    out = Path(args.out) if args.out else Path("out") / f"{slug}_{args.at[:10]}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    moonlib_s.plotting.write_array_png(finished, out)

    report = moonlib_s.instrument_report(
        telescope, arcsec_per_pixel=render.stats["arcsec_per_pixel"],
        seeing_fwhm_arcsec=args.seeing,
        frame_selection_gain=args.frame_selection_gain,
        sharpen_radius_factor=args.sharpen_radius,
    )
    print(
        "{feature} {when}  {span:.0f} km  {w}x{h} px  {scale:.4f}\"/px  "
        "alt {alt:+.1f} deg  sun {sun:+.1f} deg  "
        "blur sigma {bsig:.2f} px (x{strength:g})  "
        "sharpen r {rsig:.3f}\" = {rfac:g}x seeing  "
        "FWHM {fwhm:.3f}\"  -> {path}".format(
            feature=feature.name_en,
            when=args.at,
            span=render.stats["span_km"],
            w=finished.shape[1],
            h=finished.shape[0],
            scale=render.stats["arcsec_per_pixel"],
            alt=moonlib_s.az_el("MOON", moonlib_s.to_et(args.at), site).elevation_deg,
            sun=render.region.solar_elevation_deg,
            bsig=report["blur_sigma_px"],
            strength=args.blur_strength,
            rsig=report["sharpen_sigma_arcsec"],
            rfac=report["sharpen_radius_factor"],
            fwhm=report["fwhm_total_arcsec"],
            path=shown(out),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
