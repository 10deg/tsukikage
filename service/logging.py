"""A tiny tee so the console and a log file see the server's output.

The service is a console program now; its window is the control surface.  A log
file still helps when a user closes the window or a start fails before the
browser opens.
"""

from __future__ import annotations

import io
import sys
import threading
import traceback
from pathlib import Path
from .settings import AppPaths


class _Tee(io.TextIOBase):
    def __init__(self, mirror, handle) -> None:
        self._mirror = mirror
        self._handle = handle
        self._lock = threading.Lock()
        self._buffer = ""

    @property
    def encoding(self) -> str:
        return "utf-8"

    @property
    def errors(self) -> str:
        return "replace"

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:  # pragma: no cover - libraries probe this
        raise io.UnsupportedOperation("fileno")

    def write(self, text) -> int:
        if not isinstance(text, str):
            text = str(text)
        if not text:
            return 0
        lines = []
        with self._lock:
            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                lines.append(line.rstrip("\r"))
        for line in lines:
            self._emit(line)
        return len(text)

    def flush(self) -> None:
        line = None
        with self._lock:
            if self._buffer:
                line, self._buffer = self._buffer, ""
                line = line.rstrip("\r")
            try:
                self._handle.flush()
            except Exception:
                pass
        if line is not None:
            self._emit(line)

    def _emit(self, line: str) -> None:
        with self._lock:
            try:
                self._handle.write(line + "\n")
                self._handle.flush()
            except Exception:
                pass
            if self._mirror is not None:
                try:
                    self._mirror.write(line + "\n")
                    self._mirror.flush()
                except Exception:
                    pass


class LogHandle:
    def __init__(self, path: Path, handle) -> None:
        self.path = path
        self._handle = handle

    def close(self) -> None:
        for stream in (sys.stdout, sys.stderr):
            if isinstance(stream, _Tee):
                try:
                    stream.flush()
                except Exception:
                    pass
        try:
            self._handle.close()
        except Exception:
            pass


def _rotate(path: Path, limit: int = 4 * 1024 * 1024) -> None:
    try:
        if path.is_file() and path.stat().st_size > limit:
            backup = path.with_name(path.name + ".1")
            try:
                backup.unlink()
            except OSError:
                pass
            path.replace(backup)
    except OSError:
        pass


def install(paths: AppPaths) -> LogHandle:
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    path = paths.log_dir / "service.log"
    _rotate(path)
    handle = path.open("a", encoding="utf-8", buffering=1)
    original_out = sys.__stdout__
    original_err = sys.__stderr__
    for stream in (original_out, original_err):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    sys.stdout = _Tee(original_out, handle)
    sys.stderr = _Tee(original_err, handle)

    def excepthook(exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            sys.stderr.write(text)
            sys.stderr.flush()
        except Exception:
            pass

    sys.excepthook = excepthook
    try:
        threading.excepthook = lambda args: excepthook(  # type: ignore[assignment]
            args.exc_type, args.exc_value, args.exc_traceback
        )
    except Exception:
        pass
    return LogHandle(path, handle)
