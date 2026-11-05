"""Built-in resumable downloader for the global LOLA 118 m DEM.

The packaged service has no Python interpreter on the user's machine and no
``tools/`` checkout, so the map page's "下载高分辨率 DEM" button cannot run the
source-tree fetch script.  This module is the in-exe replacement: standard
library only, parallel HTTP Range requests, resume via a sidecar chunk list, and
it writes the finished ``.tif`` into the external ``kernels/`` directory beside
the executable.  The map service reloads the terrain when the download finishes.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, Optional

DEM_URL = (
    "https://asc-pds-services.s3.us-west-2.amazonaws.com/mosaic/"
    "Lunar_LRO_LOLA_Global_LDEM_118m_Mar2014.tif"
)
DEM_FALLBACK_URL = (
    "https://planetarymaps.usgs.gov/mosaic/"
    "Lunar_LRO_LOLA_Global_LDEM_118m_Mar2014.tif"
)
DEM_FILENAME = "Lunar_LRO_LOLA_Global_LDEM_118m_Mar2014.tif"
DEM_BYTES = 8_494_203_833
DEFAULT_CHUNK = 64 * 1024 * 1024
DEFAULT_WORKERS = 16
USER_AGENT = "Tsukikage/1.7.1 (+https://github.com/10deg/tsukikage)"


class DemDownloadError(RuntimeError):
    pass


def _content_length(url: str, timeout: float = 30.0) -> Optional[int]:
    request = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.headers.get("Content-Length")
            return int(raw) if raw else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _format_bytes(value: int) -> str:
    if value >= 1e9:
        return "%.2f GB" % (value / 1e9)
    if value >= 1e6:
        return "%.1f MB" % (value / 1e6)
    return "%.0f KB" % (value / 1e3)


class GlobalDemDownload:
    """One background global-DEM download, with resumable chunks.

    ``start()`` and ``status()`` are what the map page calls.  The status dict
    has exactly the keys the page already renders (``running``, ``last_line``,
    ``error``, ``returncode``).
    """

    def __init__(
        self,
        target_dir: Path,
        *,
        url: str = DEM_URL,
        fallback_url: str = DEM_FALLBACK_URL,
        filename: str = DEM_FILENAME,
        expected_bytes: int = DEM_BYTES,
        workers: int = DEFAULT_WORKERS,
        chunk_size: int = DEFAULT_CHUNK,
        on_complete: Optional[Callable[[], None]] = None,
    ) -> None:
        self.target_dir = Path(target_dir)
        self.target = self.target_dir / filename
        self.part = self.target.with_name(self.target.name + ".part")
        self.marker = self.target.with_name(self.target.name + ".incomplete")
        self.state_path = self.target.with_name(self.target.name + ".chunks.json")
        self.url = str(url)
        self.fallback_url = str(fallback_url)
        self.expected_bytes = int(expected_bytes)
        self.workers = max(1, int(workers))
        self.chunk_size = max(1024 * 1024, int(chunk_size))
        self.on_complete = on_complete
        self._lock = threading.Lock()
        self._file_lock = threading.Lock()
        self._status: Dict[str, Any] = {
            "running": False,
            "last_line": "",
            "error": "",
            "returncode": None,
        }
        self._thread: Optional[threading.Thread] = None
        self._active_url = self.url

    # -- public ----------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def start(self) -> Dict[str, Any]:
        with self._lock:
            if self._status.get("running"):
                return dict(self._status)
            if self.target.is_file():
                size = self.target.stat().st_size
                if size == self.expected_bytes:
                    self._status = {
                        "running": False,
                        "last_line": "文件已存在：%s" % self.target.name,
                        "error": "",
                        "returncode": 0,
                    }
                    done = True
                else:
                    self._status = {
                        "running": False,
                        "last_line": "",
                        "error": "目标文件已存在但大小不符：%s（%d 字节，应为 %d）"
                                 % (self.target.name, size, self.expected_bytes),
                        "returncode": 1,
                    }
                    done = False
                    return dict(self._status)
            else:
                done = False
            if done:
                return dict(self._status)
            self._status = {
                "running": True,
                "last_line": "准备下载…",
                "error": "",
                "returncode": None,
            }
            thread = threading.Thread(target=self._run, name="tsukikage-dem-download",
                                      daemon=True)
            self._thread = thread
            thread.start()
            return dict(self._status)

    # -- worker ----------------------------------------------------------
    def _set(self, **values: Any) -> None:
        with self._lock:
            self._status.update(values)

    def _line(self, text: str) -> None:
        self._set(last_line=text[-300:])

    def _load_state(self, total: int) -> set:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if int(payload.get("total")) != total:
                return set()
            chunks = {int(value) for value in payload.get("chunks", [])}
            return chunks
        except (OSError, ValueError, TypeError, KeyError):
            return set()

    def _save_state(self, total: int, done: set) -> None:
        payload = {"total": total, "url": self._active_url,
                   "chunks": sorted(done)}
        temporary = self.state_path.with_name(self.state_path.name + ".tmp")
        try:
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temporary, self.state_path)
        except OSError:
            pass

    def _choose_url(self) -> Optional[int]:
        for url in (self.url, self.fallback_url):
            length = _content_length(url)
            if length:
                self._active_url = url
                return length
        return None

    def _run(self) -> None:
        try:
            total = self._choose_url()
            if total is None:
                # A HEAD may be blocked while ranged GETs still work.  Use the
                # known product length and let the per-chunk length check catch
                # a mismatch.
                total = self.expected_bytes
                self._line("服务器未返回文件大小，按已知长度继续…")
            if total != self.expected_bytes:
                raise DemDownloadError(
                    "服务器文件大小 %d 与预期 %d 不符" % (total, self.expected_bytes)
                )
            self.target_dir.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(self.target_dir).free
            if free < total + 256 * 1024 * 1024:
                raise DemDownloadError(
                    "磁盘空间不足：需要约 %s，可用 %s"
                    % (_format_bytes(total + 256 * 1024 * 1024), _format_bytes(free))
                )

            ranges = [
                (start, min(start + self.chunk_size, total) - 1)
                for start in range(0, total, self.chunk_size)
            ]
            if not self.part.is_file() or self.part.stat().st_size != total:
                with open(self.part, "wb") as handle:
                    handle.truncate(total)
                done: set = set()
            else:
                done = self._load_state(total)
                done = {index for index in done if 0 <= index < len(ranges)}
            self.marker.write_text("downloading\n", encoding="utf-8")
            self._save_state(total, done)

            already = sum(ranges[index][1] - ranges[index][0] + 1 for index in done)
            completed = {"bytes": already}
            started_at = time.time()

            def report() -> None:
                elapsed = max(time.time() - started_at, 1e-6)
                gained = completed["bytes"]
                percent = gained / total * 100.0
                speed = gained / elapsed
                self._line(
                    "下载中 %.1f%%（%s / %s，%s/s）"
                    % (percent, _format_bytes(gained), _format_bytes(total),
                       _format_bytes(int(speed)))
                )

            def fetch(index: int) -> int:
                start, stop = ranges[index]
                expected = stop - start + 1
                last_error: Optional[BaseException] = None
                for attempt in range(4):
                    try:
                        request = urllib.request.Request(
                            self._active_url,
                            headers={
                                "User-Agent": USER_AGENT,
                                "Range": "bytes=%d-%d" % (start, stop),
                            },
                        )
                        with urllib.request.urlopen(request, timeout=180) as response:
                            data = response.read()
                        if len(data) != expected:
                            raise DemDownloadError(
                                "分块 %d 长度 %d，预期 %d"
                                % (index, len(data), expected)
                            )
                        with self._file_lock:
                            with open(self.part, "r+b") as handle:
                                handle.seek(start)
                                handle.write(data)
                                handle.flush()
                            done.add(index)
                            self._save_state(total, done)
                            completed["bytes"] += expected
                        return index
                    except Exception as error:  # network errors are retried
                        last_error = error
                        time.sleep(min(2.0 * (attempt + 1), 8.0))
                raise DemDownloadError(
                    "分块 %d 下载失败：%s" % (index, last_error)
                )

            pending = [index for index in range(len(ranges)) if index not in done]
            report()
            if pending:
                with ThreadPoolExecutor(max_workers=self.workers) as pool:
                    futures = [pool.submit(fetch, index) for index in pending]
                    for future in as_completed(futures):
                        future.result()
                        report()
            else:
                report()

            if self.part.stat().st_size != total:
                raise DemDownloadError("下载文件大小不完整")
            try:
                os.replace(self.part, self.target)
            except OSError as error:
                raise DemDownloadError("无法完成文件：%s" % error) from error
            for path in (self.marker, self.state_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            self._set(running=False, last_line="下载完成：%s" % self.target.name,
                      error="", returncode=0)
            if self.on_complete is not None:
                try:
                    self.on_complete()
                except Exception:
                    pass
        except Exception as error:
            self._set(
                running=False,
                error="%s: %s" % (type(error).__name__, error),
                last_line="下载中断；保留 .part 文件，可重试续传",
                returncode=1,
            )
        finally:
            with self._lock:
                self._status["running"] = False
