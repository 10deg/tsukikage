"""Browser service launcher for Windows/Linux/macOS.

The packaged build is one console executable next to a required ``kernels/``
folder.  Users choose whether to start the simulator, the map, or both; both
servers share the same external kernels/DEM directory and open in the system
browser.  No Qt, no embedded Chromium.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import socket
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from . import __version__
from .dem_download import DEM_FILENAME, GlobalDemDownload
from .logging import install as install_logging
from .settings import (
    AppPaths,
    KERNEL_MODE_LABELS,
    KERNEL_MODES,
    apply_environment,
    attachment_dir,
    has_global_dem,
    load_settings,
    missing_kernel_files,
    required_kernels,
    resource_root,
    save_settings,
    seed_settings,
)

SERVICE_CHOICES = ("main", "map", "both")
SERVICE_LABELS = {
    "main": "月面模拟",
    "map": "月面地图",
    "both": "月面模拟 + 月面地图",
}


@dataclass
class ServerHandle:
    role: str
    httpd: object
    port: int
    service: Optional[object] = None


def _configure_console() -> None:
    """Never crash on a console code page that cannot encode Chinese.

    A Windows console normally uses the user's code page; Wine and redirected
    output can be cp1252.  File names and the required-kernel list must still be
    readable instead of raising UnicodeEncodeError before the log is installed.
    """
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _bootstrap() -> None:
    if getattr(sys, "frozen", False):
        return
    root = Path(__file__).resolve().parent.parent
    for path in (root, root / "src"):
        text = str(path)
        if text not in sys.path:
            sys.path.insert(0, text)


def _parse(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tsukikage",
        description="Tsukikage 月面模拟：本地服务 + 浏览器（主页面/地图/全部）",
    )
    parser.add_argument("--service", choices=SERVICE_CHOICES,
                        help="启动 main（月面模拟）、map（月面地图）或 both（全部）")
    parser.add_argument("--map-server", action="store_true",
                        help="兼容旧参数：等同 --service map")
    parser.add_argument("--data-dir",
                        help="DEM/色彩目录；默认是 exe 同目录下的 kernels")
    parser.add_argument("--cache-dir", help="地形金字塔缓存目录")
    parser.add_argument("--kernel-dir", help="覆盖 kernels 附件目录（高级）")
    parser.add_argument("--kernel-mode", choices=KERNEL_MODES,
                        help="recent（默认）或 long（需 DE441 分卷）")
    parser.add_argument("--host", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, help="主服务端口，默认 8765")
    parser.add_argument("--map-port", type=int, help="地图服务端口，默认 8766")
    parser.add_argument("--config", type=Path, help="设置文件路径")
    parser.add_argument("--reset-settings", action="store_true",
                        help="备份并忽略现有设置")
    parser.add_argument("--no-browser", action="store_true",
                        help="只启动服务，不打开浏览器")
    parser.add_argument("--download-dem", action="store_true",
                        help="下载全球 LOLA 118 m DEM 到 kernels 目录后退出")
    parser.add_argument("--port-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--smoke-seconds", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--version", action="store_true", help="显示版本并退出")
    return parser.parse_args(argv)


def _load_module(name: str, filename: Path):
    if not filename.is_file():
        raise FileNotFoundError("找不到服务模块：%s" % filename)
    spec = importlib.util.spec_from_file_location(name, filename)
    if spec is None or spec.loader is None:
        raise ImportError("无法加载服务模块：%s" % filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _pick_port(host: str, start: int) -> int:
    """Find a likely-free port before the expensive terrain build."""
    if start == 0:
        return 0
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    return start


def _build_on_port(build: Callable[[int], object], host: str, start: int) -> Tuple[object, int]:
    port = _pick_port(host, start)
    httpd = build(port)
    return httpd, int(httpd.server_address[1])


def _interactive() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        return False


def _open_browser(url: str, delay: float = 0.6) -> None:
    def open_it() -> None:
        try:
            opened = webbrowser.open(url, new=2)
        except Exception as error:
            print("无法自动打开浏览器：%s" % error)
            return
        if not opened:
            print("未能自动打开浏览器，请手动打开：%s" % url)

    if delay > 0:
        threading.Timer(delay, open_it).start()
    else:
        open_it()


def _write_port_file(path: Optional[Path], port: int) -> None:
    if path is None:
        return
    try:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text("http://127.0.0.1:%d/\n" % port, encoding="utf-8")
        os.replace(temporary, target)
    except OSError as error:
        print("无法写端口文件：%s" % error)


def _choose_service(args) -> Optional[str]:
    if args.map_server:
        if args.service not in (None, "map"):
            print("--map-server 与 --service %s 冲突。" % args.service)
            return None
        return "map"
    if args.service:
        return args.service
    if not _interactive():
        # Existing scripts and --no-browser smoke tests keep the old behaviour.
        return "main"
    print("启动哪个服务？")
    print("  1) 月面模拟（主页面）")
    print("  2) 月面地图（含照片库）")
    print("  3) 两个都启动")
    while True:
        try:
            answer = input("选择 [1/2/3]（默认 3）：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if answer in ("", "3", "both", "全部", "都"):
            return "both"
        if answer in ("1", "main", "主", "模拟"):
            return "main"
        if answer in ("2", "map", "地图"):
            return "map"
        print("请输入 1、2 或 3。")


def _print_missing_kernels(kernel_dir: Path, mode: str) -> Tuple[List[str], List[str]]:
    missing, empty = missing_kernel_files(kernel_dir, mode)
    if missing or empty:
        print("内核附件不完整。")
        print("需要的目录：%s" % kernel_dir)
        print("需要的文件：")
        for name in required_kernels(mode):
            print("  - %s" % name)
        if missing:
            print("缺少：%s" % "、".join(missing))
        if empty:
            print("为空：%s" % "、".join(empty))
    return missing, empty


def _wait_for_kernels(kernel_dir: Path, mode: str) -> bool:
    try:
        kernel_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print("无法创建内核目录：%s" % error)
        return False
    missing, empty = _print_missing_kernels(kernel_dir, mode)
    if not missing and not empty:
        return True
    if not _interactive():
        print("请补齐以上文件后重新运行。")
        return False
    while True:
        try:
            answer = input("补齐后按 Enter 重新检查，输入 q 退出：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if answer in ("q", "quit", "exit"):
            return False
        missing, empty = _print_missing_kernels(kernel_dir, mode)
        if not missing and not empty:
            print("内核检查通过。")
            return True


def _print_missing_dem(data_dir: Path) -> None:
    print("还没有全球 2:1 高程 DEM（.tif）。")
    print("请把至少一个下列文件放进：")
    print("  %s" % data_dir)
    print("  - ldem_*.tif（例如 ldem_64.tif）")
    print("  - SLDEM2015_512.tif")
    print("  - Lunar_LRO_LOLA_*.tif")
    print("色彩可选：lroc_color*.tif 或 lroc_color.exr。")
    print("也可以先启动月面地图，在地图页设置里点“下载高分辨率 DEM”；")
    print("下载会保存到同一个 kernels 目录，完成后地图自动重载。")


def _wait_for_dem(data_dir: Path, service: str) -> Optional[str]:
    if service == "map":
        return "map"
    if has_global_dem(data_dir):
        return service
    _print_missing_dem(data_dir)
    if not _interactive():
        print("补齐 DEM 后重新运行；或使用 --service map 只启动地图。")
        return None
    while True:
        try:
            answer = input("按 Enter 重新检查；输入 m 只启动地图；输入 q 退出：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if answer in ("q", "quit", "exit"):
            return None
        if answer in ("m", "map", "地图"):
            print("只启动月面地图（无 DEM 时使用平坦底图）。")
            return "map"
        if has_global_dem(data_dir):
            print("DEM 检查通过。")
            return service
        print("仍未找到全球 DEM；请再放一个 .tif 后按 Enter。")


def _configure_map_photos(module, paths: AppPaths) -> Path:
    """Use a persistent, user-changeable photo-library root.

    ``web/map_server.py`` alone still defaults beside its source file; the
    packaged service must not write into the one-file temporary directory and
    must remember the reader's custom directory between launches.
    """
    default = paths.config_file.parent / "map_photos"
    config = paths.config_file.parent / "map_photos_dir.json"
    default.mkdir(parents=True, exist_ok=True)
    module.PHOTO_DIR_DEFAULT = default
    module.PHOTO_DIR_CONFIG = config
    chosen = module._initial_photo_dir()
    if not chosen.is_dir():
        chosen = default
    module.PHOTO_DIR = chosen
    return chosen


def _build_main(
    data_dir: Path,
    kernel_dir: Path,
    cache: Path,
    host: str,
    start_port: int,
    settings_dir: Path,
) -> Optional[ServerHandle]:
    module = _load_module("tsukikage_web_server", resource_root() / "web" / "server.py")

    def build(port: int):
        return module.build_server(host, port, settings_dir)

    print("正在加载月面模拟，首次运行会烘焙缓存，请稍候…")
    try:
        httpd, port = _build_on_port(build, host, start_port)
    except Exception as error:
        print("月面模拟启动失败：%s" % error)
        print("请检查 DEM 目录：%s" % data_dir)
        return None
    service = getattr(httpd.RequestHandlerClass, "service", None)
    return ServerHandle("main", httpd, port, service)


def _build_map(
    data_dir: Path,
    cache: Path,
    host: str,
    start_port: int,
    paths: AppPaths,
    terrain=None,
) -> Optional[ServerHandle]:
    module = _load_module("tsukikage_map_server", resource_root() / "web" / "map_server.py")
    photos = _configure_map_photos(module, paths)
    print("照片库目录：%s" % photos)
    print("正在加载月面地图，首次运行会读取地形缓存，请稍候…")
    try:
        httpd, port = _build_on_port(
            lambda candidate: module.build_server(
                host, candidate, terrain=terrain
            ),
            host,
            start_port,
        )
    except Exception as error:
        print("月面地图启动失败：%s" % error)
        return None
    service = getattr(httpd.RequestHandlerClass, "service", None)
    if service is not None:
        downloader = GlobalDemDownload(
            data_dir,
            on_complete=getattr(service, "reload_terrain", None),
        )
        service.set_data_download(downloader)
    return ServerHandle("map", httpd, port, service)


def _shutdown_handles(handles: Sequence[ServerHandle]) -> None:
    for handle in handles:
        try:
            handle.httpd.shutdown()
        except Exception:
            pass


def _close_handles(handles: Sequence[ServerHandle]) -> None:
    for handle in handles:
        try:
            handle.httpd.server_close()
        except Exception:
            pass


def _raster_signature(directory: Path) -> Tuple[Tuple[str, int, int], ...]:
    entries = []
    try:
        for entry in directory.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in (".tif", ".tiff", ".exr"):
                continue
            if entry.name.endswith(".incomplete"):
                continue
            stat = entry.stat()
            entries.append((entry.name, stat.st_size, stat.st_mtime_ns))
    except OSError:
        return ()
    entries.sort()
    return tuple(entries)


def _watch_rasters(directory: Path, services: Sequence[object],
                   stop_event: threading.Event) -> None:
    """Reload both services after a stable change in kernels/*.tif.

    This is deliberately a small poller rather than a platform file watcher:
    it has to work identically in the Windows one-file build, on network
    shares, and while a large DEM is being copied.  Two equal scans in a row
    mean the writer has stopped for a moment; the download itself uses a
    ``.part``/``.incomplete`` pair that this scan ignores.
    """
    last = _raster_signature(directory)
    changed_at: Optional[float] = None
    while not stop_event.wait(1.0):
        current = _raster_signature(directory)
        if current != last:
            last = current
            changed_at = time.monotonic()
            continue
        if changed_at is None or time.monotonic() - changed_at < 2.0:
            continue
        changed_at = None
        reloaded = False
        for service in services:
            reload = getattr(service, "reload_terrain", None)
            if reload is None:
                continue
            try:
                reload()
                reloaded = True
            except Exception as error:
                print("数据更新后重新加载失败：%s" % error)
        if reloaded:
            print("检测到 kernels 数据变化，服务已自动重新加载。")


def _serve_many(
    handles: Sequence[ServerHandle],
    data_dir: Path,
    open_browser: bool,
    smoke_seconds: Optional[float],
    port_file: Optional[Path],
) -> int:
    urls: List[str] = []
    for index, handle in enumerate(handles):
        url = "http://%s:%d/" % (handle.httpd.server_address[0], handle.port)
        urls.append(url)
        label = SERVICE_LABELS.get(handle.role, handle.role)
        print("%s：%s" % (label, url))
        if index == 0:
            _write_port_file(port_file, handle.port)
    print("浏览器窗口关闭不会停止服务；关闭本控制台窗口或按 Ctrl+C 才会停止。")

    threads: List[threading.Thread] = []
    for handle in handles:
        thread = threading.Thread(
            target=handle.httpd.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="tsukikage-%s" % handle.role,
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    if open_browser:
        for index, url in enumerate(urls):
            threading.Timer(
                0.6 + 0.6 * index,
                _open_browser,
                args=(url, 0.0),
            ).start()

    def stop_all() -> None:
        _shutdown_handles(handles)

    timer = None
    if smoke_seconds:
        timer = threading.Timer(float(smoke_seconds), stop_all)
        timer.daemon = True
        timer.start()

    stop_event = threading.Event()
    services = [handle.service for handle in handles if handle.service is not None]
    watcher = threading.Thread(
        target=_watch_rasters,
        args=(data_dir, services, stop_event),
        name="tsukikage-data-watch",
        daemon=True,
    )
    watcher.start()
    try:
        while not stop_event.is_set() and any(thread.is_alive() for thread in threads):
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        stop_event.set()
        if timer is not None:
            timer.cancel()
        _shutdown_handles(handles)
        for thread in threads:
            thread.join(3.0)
        _close_handles(handles)
        watcher.join(2.0)
    return 0


def _download_dem(data_dir: Path) -> int:
    data_dir.mkdir(parents=True, exist_ok=True)
    print("全球 DEM 下载到：%s" % data_dir)
    print("文件：%s（约 8.5 GB，可断点续传）" % DEM_FILENAME)
    downloader = GlobalDemDownload(data_dir)
    state = downloader.start()
    if state.get("error"):
        print(state["error"])
        return 1
    if not state.get("running"):
        print(state.get("last_line") or "已完成")
        return 0
    last = ""
    while True:
        state = downloader.status()
        line = str(state.get("last_line") or "")
        if line and line != last:
            print("  " + line)
            last = line
        if not state.get("running"):
            break
        time.sleep(1.0)
    if state.get("returncode") not in (0, None):
        print("下载失败：%s" % (state.get("error") or "未知错误"))
        return 1
    print(state.get("last_line") or "下载完成")
    return 0


def _print_kernel(data_dir: Path, kernel_dir: Path, cache: Path, mode: str) -> None:
    print("Tsukikage %s" % __version__)
    print("内核：%s（%s）" % (kernel_dir, KERNEL_MODE_LABELS.get(mode, mode)))
    print("月面数据：%s" % data_dir)
    print("缓存：%s" % cache)


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_console()
    _bootstrap()
    args = _parse(argv)
    if args.version:
        print("Tsukikage service %s" % __version__)
        return 0

    paths = AppPaths.discover(args.config)
    try:
        paths.ensure()
    except OSError as error:
        print("无法创建配置/缓存目录：%s" % error)
        return 1

    if args.reset_settings and paths.config_file.is_file():
        backup = paths.config_file.with_name(
            paths.config_file.name + ".bak-%d" % int(time.time())
        )
        try:
            paths.config_file.replace(backup)
        except OSError as error:
            print("无法备份设置文件：%s" % error)

    settings, warning = load_settings(paths.config_file)
    if warning:
        print(warning)
    if args.cache_dir:
        settings.cache_dir = args.cache_dir
    if args.host:
        settings.host = args.host
    if args.port is not None:
        settings.port = args.port
    if args.map_port is not None:
        settings.map_port = args.map_port
    if args.kernel_mode:
        settings.kernel_mode = args.kernel_mode

    # Ambient MOONLIB_* variables from a shell should not silently override the
    # paths this launcher is about to choose.
    for name in ("MOONLIB_CONFIG", "MOONLIB_DEM", "MOONLIB_ALBEDO", "MOONLIB_PREFERRED_DEM"):
        os.environ.pop(name, None)

    kernel_dir = attachment_dir(args.kernel_dir)
    try:
        kernel_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print("无法创建 kernels 附件目录：%s" % error)
        return 1

    data_dir = Path(args.data_dir).expanduser() if args.data_dir else kernel_dir
    if data_dir.suffix.lower() in (".tif", ".tiff", ".exr"):
        data_dir = data_dir.parent
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print("无法创建 DEM 目录：%s" % error)
        return 1

    if args.download_dem:
        log = install_logging(paths)
        try:
            return _download_dem(data_dir)
        finally:
            try:
                log.close()
            except Exception:
                pass

    if not _wait_for_kernels(kernel_dir, settings.kernel_mode):
        return 2

    service = _choose_service(args)
    if service is None:
        return 2
    selected = _wait_for_dem(data_dir, service)
    if selected is None:
        return 2
    service = selected

    cache = Path(settings.cache_dir).expanduser() if settings.cache_dir else paths.cache_dir
    try:
        cache = cache.resolve()
        cache.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print("缓存目录不可用：%s" % error)
        return 1

    settings.data_dir = str(data_dir)
    settings.cache_dir = str(cache)
    settings.host = settings.host or "127.0.0.1"
    apply_environment(kernel_dir, data_dir, cache, settings.kernel_mode)
    seed_settings(paths)
    paths.ensure()
    try:
        save_settings(paths.config_file, settings)
    except OSError as error:
        print("无法保存设置：%s" % error)

    log = install_logging(paths)
    handles: List[ServerHandle] = []
    try:
        _print_kernel(data_dir, kernel_dir, cache, settings.kernel_mode)
        open_browser = settings.open_browser and not args.no_browser
        main_handle = None
        if service in ("main", "both"):
            main_handle = _build_main(
                data_dir, kernel_dir, cache, settings.host,
                args.port if args.port is not None else settings.port,
                paths.settings_dir,
            )
            if main_handle is None:
                return 1
            handles.append(main_handle)
        if service in ("map", "both"):
            if service == "map":
                map_start = (
                    args.map_port if args.map_port is not None
                    else args.port if args.port is not None
                    else settings.map_port
                )
            else:
                map_start = (
                    args.map_port if args.map_port is not None
                    else settings.map_port
                )
            terrain = None
            if main_handle is not None and main_handle.service is not None:
                try:
                    terrain = main_handle.service.terrain()
                except Exception:
                    terrain = None
            map_handle = _build_map(
                data_dir, cache, settings.host, map_start, paths, terrain=terrain
            )
            if map_handle is None:
                _close_handles(handles)
                return 1
            handles.append(map_handle)
        return _serve_many(
            handles, data_dir, open_browser, args.smoke_seconds, args.port_file
        )
    finally:
        _close_handles(handles)
        try:
            log.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
