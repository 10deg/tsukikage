"""Encoding rendered frames to MP4, and reading back what was written.

Two jobs live here, and neither of them needs matplotlib or a video library:

* :func:`write_mp4` pipes a stream of frames into an ffmpeg process one at a
  time.  Nothing is materialised: a generator that produces a ten-thousand-frame
  animation is consumed frame by frame, so peak memory is one frame plus the
  encoder's own buffers, not the whole film.
* :func:`probe_mp4` answers "what did we just write?" by parsing the ISO base
  media file itself.  ``ffprobe`` is *not* part of an ffmpeg build distributed
  as a single static binary (the one :mod:`imageio_ffmpeg` ships, for one), so
  the box walk is written out here rather than shelled out to a tool that may
  not exist.

The dependency on ffmpeg is deliberately optional.  ``shutil.which("ffmpeg")``
is tried first, then ``imageio_ffmpeg.get_ffmpeg_exe()``; when neither is
available the encoder raises with instructions instead of writing something
else (a PNG sequence, say) that the caller did not ask for.

HDR
---
``hdr=True`` treats each input frame as **scene-linear light where 1.0 is
diffuse white**, encodes it with the SMPTE ST 2084 (PQ) transfer function at a
1000-nit peak into BT.2020 primaries, and writes 10-bit HEVC tagged as HDR10.
The mapping is the one in the deliverable's specification::

    y = clip(linear, 0, None) * (203 / 10000)
    code = pq_encode(y)

so linear 1.0 -- SDR diffuse white -- lands on the 203-nit HDR reference white,
and anything above 1000/203 ~ 4.93 times diffuse white clips.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

import numpy as np

from .errors import MoonlibError

from .errors import MoonlibError

__all__ = ["find_ffmpeg", "have_ffmpeg", "write_mp4", "probe_mp4",
           "frame_rgb8", "resize_frame"]

# ---------------------------------------------------------------------------
# ffmpeg discovery
# ---------------------------------------------------------------------------
def find_ffmpeg() -> Optional[str]:
    """Path to an ffmpeg executable, or ``None``.

    A binary already on ``PATH`` wins, because a user who installed one has
    opinions about which build is used.  Otherwise the static build that the
    optional :mod:`imageio_ffmpeg` package ships is used.  That import is done
    here rather than at module import time, so ``moonlib.encode`` stays usable
    (for :func:`probe_mp4`, say) without the package installed.
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        executable = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # pragma: no cover - a broken optional installation
        return None
    return str(executable) if executable else None


def have_ffmpeg() -> bool:
    """True when :func:`find_ffmpeg` can find an encoder."""
    return find_ffmpeg() is not None


# ---------------------------------------------------------------------------
# frames
# ---------------------------------------------------------------------------
def _resample_axis(values: np.ndarray, out_size: int, axis: int) -> np.ndarray:
    """Resample one axis to *out_size* samples, averaging over the source box.

    Done by integrating along the axis and differencing at the output box edges,
    which is exact area-averaging when the frame shrinks and linear
    interpolation when it grows -- and costs one cumulative sum either way.
    """
    source = int(values.shape[axis])
    if source == out_size:
        return values
    moved = np.moveaxis(np.asarray(values, dtype=np.float64), axis, 0)
    flat = moved.reshape(source, -1)
    integral = np.concatenate(
        [np.zeros((1, flat.shape[1]), dtype=np.float64), np.cumsum(flat, axis=0)],
        axis=0,
    )
    edges = np.linspace(0.0, float(source), out_size + 1)
    lower = np.floor(edges).astype(np.int64)
    upper = np.minimum(lower + 1, source)
    weight = (edges - lower)[:, None]
    corners = integral[lower] * (1.0 - weight) + integral[upper] * weight
    boxes = np.diff(corners, axis=0) / (edges[1:] - edges[:-1])[:, None]
    return np.moveaxis(boxes.reshape((out_size,) + moved.shape[1:]), 0, axis)


def resize_frame(frame: Any, size: Any) -> np.ndarray:
    """Resample one frame, area-averaging when it shrinks.

    *size* is one number for a square frame -- which is what a whole-disk frame
    is -- or a ``(width, height)`` pair for one that is not, which is what a
    close-up of a box the reader dragged out to 3:1 is.  The aspect is part of
    what was rendered, so it is part of what gets written.

    This is the second half of "render what the physics can resolve, write the
    size the reader asked for": a frame rendered at 420 px because that is what
    the telescope resolves can be written at 1200 px without pretending there is
    detail in it.
    """
    width, height = _frame_size(size)
    values = np.asarray(frame, dtype=np.float64)
    if values.ndim != 3:
        raise MoonlibError(
            f"resize_frame wants a (height, width, channels) frame, got shape "
            f"{values.shape}."
        )
    if values.shape[0] == height and values.shape[1] == width:
        return values.astype(np.float32)
    out = _resample_axis(values, height, 0)
    out = _resample_axis(out, width, 1)
    return out.astype(np.float32)


def _frame_size(size: Any) -> tuple:
    """``(width, height)`` from one number or a ``(width, height)`` pair."""
    if isinstance(size, (int, float)) and not isinstance(size, bool):
        width = height = int(size)
    else:
        try:
            pair = [int(part) for part in size]
        except (TypeError, ValueError) as exc:
            raise MoonlibError(
                f"a frame size must be a number or a (width, height) pair; got {size!r}."
            ) from exc
        if len(pair) != 2:
            raise MoonlibError(
                f"a frame size pair must have two elements; got {len(pair)}."
            )
        width, height = pair
    if width <= 0 or height <= 0:
        raise MoonlibError(f"a frame size must be positive; got {width}x{height}.")
    return width, height


#: The largest Gaussian a frame smooth may use, in pixels.  Beyond this it is
#: not "remove the terrain grid from the shadows" any more, it is blurring the
#: picture, and the reader has 模糊强度 for that.
MAX_SMOOTH_PX = 2.0


def smooth_frame(frame: Any, sigma_px: float) -> np.ndarray:
    """Blur one frame by a Gaussian of ``sigma_px`` pixels, edges held.

    This is 极小尺度平滑: at the finest terrain level the shadow tips show the
    DEM's own cell structure, because a pixel there is smaller than the sample
    spacing that produced it and the interpolation edges line up with the grid.
    A sub-pixel blur is the honest answer to an artefact at the sampling scale
    -- it costs no detail the renderer resolved, because there is none there.

    The frame's *own* pixels are the unit, so the same number means the same
    thing whether the run is written at 600 px or 4000.  Edges are replicated
    rather than treated as black: a whole-disk frame's limb often touches the
    border, and darkening it would be a second artefact.
    """
    sigma = float(sigma_px)
    if not math.isfinite(sigma) or sigma <= 0.0:
        return np.asarray(frame)
    sigma = min(sigma, MAX_SMOOTH_PX)
    radius = max(1, int(math.ceil(3.0 * sigma)))
    offsets = np.arange(-radius, radius + 1, dtype=np.float64)
    kernel = np.exp(-0.5 * (offsets / sigma) ** 2)
    kernel /= kernel.sum()
    values = np.asarray(frame, dtype=np.float64)
    padded = np.pad(values, ((radius, radius), (0, 0), (0, 0)), mode="edge")
    out = np.zeros_like(values)
    for offset, weight in zip(offsets.astype(int), kernel):
        out += weight * padded[radius + offset:radius + offset + values.shape[0]]
    padded = np.pad(out, ((0, 0), (radius, radius), (0, 0)), mode="edge")
    out = np.zeros_like(values)
    for offset, weight in zip(offsets.astype(int), kernel):
        out += weight * padded[:, radius + offset:radius + offset + values.shape[1]]
    if np.issubdtype(np.asarray(frame).dtype, np.integer):
        limit = float(np.iinfo(np.asarray(frame).dtype).max)
        return np.clip(np.rint(out), 0, limit).astype(np.asarray(frame).dtype)
    return out.astype(np.asarray(frame).dtype if np.asarray(frame).dtype.kind == "f"
                      else np.float32)


def frame_rgb8(frame: Any) -> np.ndarray:
    """Coerce one frame to a contiguous ``(h, w, 3)`` uint8 array.

    Accepted inputs are exactly the two the renderer produces: a ``uint8``
    ``(h, w, 3)`` array, or a float ``(h, w, 3)`` / ``(h, w)`` array in
    ``[0, 1]`` (values outside the range are clipped, non-finite values are
    treated as black or white).  Anything else -- wrong rank, wrong channel
    count, a non-8-bit integer dtype -- raises :class:`MoonlibError`, because
    guessing at the layout is how a silently transposed or saturated frame gets
    encoded.
    """
    data = np.asarray(frame)
    if data.dtype == np.uint8:
        if data.ndim != 3 or data.shape[2] != 3:
            raise MoonlibError(
                "frame_rgb8 expects uint8 (h, w, 3); got "
                f"{data.dtype} with shape {data.shape}."
            )
        return np.ascontiguousarray(data)
    if data.dtype.kind != "f":
        raise MoonlibError(
            "frame_rgb8 expects a uint8 (h, w, 3) frame or a float (h, w, 3) / "
            f"(h, w) frame in [0, 1]; got {data.dtype} with shape {data.shape}."
        )
    if data.ndim == 3:
        if data.shape[2] != 3:
            raise MoonlibError(
                "frame_rgb8 expects float (h, w, 3) or (h, w); got shape "
                f"{data.shape}."
            )
        values = data
    elif data.ndim == 2:
        values = np.repeat(data[:, :, None], 3, axis=2)
    else:
        raise MoonlibError(
            "frame_rgb8 expects float (h, w, 3) or (h, w); got shape "
            f"{data.shape}."
        )
    finite = np.nan_to_num(values, nan=0.0, posinf=1.0, neginf=0.0)
    return np.ascontiguousarray(
        np.rint(np.clip(finite, 0.0, 1.0) * 255.0).astype(np.uint8)
    )


# ---------------------------------------------------------------------------
# PQ (SMPTE ST 2084)
# ---------------------------------------------------------------------------
#: ST 2084 constants, exactly as published (the arithmetic forms are the ones
#: in the standard: 2610/16384 and so on, not their decimal expansions).
_PQ_M1 = 2610.0 / 16384.0
_PQ_M2 = 2523.0 / 4096.0 * 128.0
_PQ_C1 = 3424.0 / 4096.0
_PQ_C2 = 2413.0 / 4096.0 * 32.0
_PQ_C3 = 2392.0 / 4096.0 * 32.0

#: Luminance of HDR reference white, in nits.  The value the specification
#: pins: diffuse white in an SDR image is displayed at 203 cd/m^2 in HDR10.
PQ_REFERENCE_WHITE_NITS = 203.0

#: The PQ signal is defined over 0..10000 cd/m^2.
PQ_PEAK_NITS = 10000.0


def pq_encode(value: Any) -> np.ndarray:
    """SMPTE ST 2084 (PQ) encode of *value*, normalised to a 10000-nit peak.

    ``value`` is linear luminance divided by 10000 cd/m^2, so ``1.0`` is the
    peak and ``203/10000`` is HDR reference white.  The return is the
    non-linear signal in ``[0, 1]`` -- multiply by the code range (1023 for
    10-bit) to get the code value.  Values outside ``[0, 1]`` are clipped.
    """
    signal = np.clip(np.asarray(value, dtype=np.float64), 0.0, 1.0)
    powered = signal ** _PQ_M1
    return ((_PQ_C1 + _PQ_C2 * powered) / (1.0 + _PQ_C3 * powered)) ** _PQ_M2


def pq_decode(signal: Any) -> np.ndarray:
    """Inverse of :func:`pq_encode`: an ST 2084 signal back to linear light.

    Returned normalised the same way -- divide by ``PQ_REFERENCE_WHITE_NITS /
    PQ_PEAK_NITS`` for the HDR10 diffuse-white scale.
    """
    values = np.clip(np.asarray(signal, dtype=np.float64), 0.0, 1.0)
    powered = values ** (1.0 / _PQ_M2)
    numerator = np.maximum(powered - _PQ_C1, 0.0)
    denominator = np.maximum(_PQ_C2 - _PQ_C3 * powered, 1e-12)
    return np.power(numerator / denominator, 1.0 / _PQ_M1)


def _hdr_rgb16(frame: Any) -> np.ndarray:
    """One linear-light frame to the 16-bit PQ samples fed to ffmpeg.

    The samples are ``rgb48le``: full-range 16-bit RGB.  The *values* are the
    PQ signal of the 203-nit-relative linear light, so the pixels ffmpeg turns
    into YCbCr already carry the HDR10 transfer curve; ffmpeg only has to
    convert the colour space, not the transfer.
    """
    data = np.asarray(frame)
    if data.dtype == np.uint8:
        raise MoonlibError(
            "hdr=True needs float linear-light frames; an 8-bit frame is display "
            "data whose transfer curve has already been applied."
        )
    if data.dtype.kind != "f":
        raise MoonlibError(
            f"hdr=True needs float linear-light frames; got {data.dtype}."
        )
    if data.ndim == 2:
        data = data[:, :, None]
    if data.ndim != 3 or data.shape[2] not in (1, 3):
        raise MoonlibError(
            "hdr=True expects float (h, w, 3) / (h, w) frames; got shape "
            f"{data.shape}."
        )
    if data.shape[2] == 1:
        data = np.repeat(data, 3, axis=2)
    linear = np.nan_to_num(
        np.asarray(data, dtype=np.float64), nan=0.0, posinf=np.inf, neginf=0.0
    )
    normalised = np.clip(linear, 0.0, None) * (PQ_REFERENCE_WHITE_NITS / PQ_PEAK_NITS)
    codes = np.rint(pq_encode(normalised) * 65535.0)
    return np.ascontiguousarray(codes.astype("<u2"))


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------
#: x265 parameters for HDR10.  ``hdr-opt`` turns on the HDR-specific rate
#: control tweaks, ``repeat-headers`` puts the VUI on every keyframe so a
#: stream can be joined mid-way, and the colour parameters are signalled in the
#: bitstream's own VUI as well as in the container tags.
_X265_HDR_PARAMS = (
    "log-level=error"
    ":hdr-opt=1"
    ":repeat-headers=1"
    ":colorprim=bt2020"
    ":transfer=smpte2084"
    ":colormatrix=bt2020nc"
    ":master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)"
    "L(10000000,100)"
    ":max-cll=1000,400"
)

#: swscale names its output matrix by the ``AVColorSpace`` enumeration number
#: (9 is ``BT2020_NC``) rather than by name, so the value is spelled out with
#: the name in the comment.  ``setparams`` carries the same information as
#: frame properties, which is what ffmpeg actually propagates into the
#: container's ``colr`` box when a filtergraph is present.
_HDR_FILTER = (
    "scale=out_color_matrix=9:out_range=tv"  # 9 = BT.2020 non-constant
    ",setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc"
)


def _rate_text(fps: float) -> str:
    """A frame rate ffmpeg will parse, without a trailing ``.0``."""
    text = f"{float(fps):.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _command(
    executable: str,
    target: Path,
    width: int,
    height: int,
    fps: float,
    hdr: bool,
    crf: int,
) -> list:
    """The ffmpeg command line for one encode.

    Frames arrive on stdin as raw video, so there is no temporary directory of
    PNGs and no inter-process buffering of the whole animation:

    SDR
        ``-f rawvideo -pix_fmt rgb24 -s WxH -r FPS -i -`` then
        ``-c:v libx264 -pix_fmt yuv420p -crf N -preset medium``.
    HDR
        ``-f rawvideo -pix_fmt rgb48le`` (full-range 16-bit RGB carrying the PQ
        signal) then ``-c:v libx265 -pix_fmt yuv420p10le`` with the BT.2020/PQ
        tags and an HDR10 mastering-display block.
    """
    command = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb48le" if hdr else "rgb24",
        "-s",
        f"{int(width)}x{int(height)}",
        "-r",
        _rate_text(fps),
        "-i",
        "-",
        "-an",
    ]
    if hdr:
        command += [
            "-vf",
            _HDR_FILTER,
            "-c:v",
            "libx265",
            "-pix_fmt",
            "yuv420p10le",
            "-crf",
            str(int(crf)),
            "-x265-params",
            _X265_HDR_PARAMS,
            "-color_primaries",
            "bt2020",
            "-color_trc",
            "smpte2084",
            "-colorspace",
            "bt2020nc",
            "-movflags",
            "+faststart+write_colr",
        ]
    else:
        command += [
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            str(int(crf)),
            "-preset",
            "medium",
            "-movflags",
            "+faststart",
        ]
    command.append(str(target))
    return command


def _stderr_tail(text: str, lines: int = 12, characters: int = 4000) -> str:
    tail = "\n".join(text.strip().splitlines()[-lines:])
    return tail[-characters:]


def _discard(target: Path) -> None:
    """Remove a half-written file, ignoring the reasons it may not be there."""
    try:
        target.unlink()
    except OSError:
        pass


def write_mp4(
    path: Any,
    frames: Iterable[Any],
    *,
    fps: float = 24.0,
    hdr: bool = False,
    crf: int = 18,
    ffmpeg: Optional[str] = None,
) -> Dict[str, Any]:
    """Encode *frames* to an MP4 at *path*, one frame at a time.

    Parameters
    ----------
    path
        Output file.  Overwritten if it exists; **removed** if the encode
        fails, so a caller never picks up a truncated file.
    frames
        Any iterable of frames.  It is consumed lazily and each frame is
        written to ffmpeg's stdin before the next is fetched, so a generator
        is the natural input for an animation that does not fit in memory.
        Every frame must have the same ``(h, w)`` shape.
    fps
        Output frame rate; frames are played back one per ``1/fps`` seconds.
    hdr
        Interpret the frames as scene-linear light (1.0 = diffuse white) and
        write 10-bit HDR10 HEVC with the PQ transfer.  ``False`` (the default)
        treats them as display-referred SDR data and writes 8-bit H.264.
    crf
        Constant-rate-factor quality knob for either encoder (lower is better;
        18 is visually lossless for the H.264 path).
    ffmpeg
        Explicit executable, overriding :func:`find_ffmpeg`.  Mainly for tests.

    Returns
    -------
    dict
        ``{"path", "frames", "fps", "seconds", "width", "height", "codec",
        "hdr"}``.  ``codec`` is ``"h264"`` or ``"hevc"``.

    Raises
    ------
    MoonlibError
        When no ffmpeg is found, when *frames* is empty, when the frames do not
        share a shape, or when ffmpeg itself fails (the message carries the
        tail of its stderr).
    """
    rate = float(fps)
    if not math.isfinite(rate) or rate <= 0.0:
        raise MoonlibError("write_mp4: fps must be a positive finite number.")
    quality = int(crf)
    executable = ffmpeg or find_ffmpeg()
    if not executable:
        raise MoonlibError(
            "write_mp4: no ffmpeg executable was found. Install imageio-ffmpeg "
            "or put ffmpeg on PATH; moonlib does not fall back to a frame "
            "sequence."
        )

    target = Path(path)
    iterator = iter(frames)
    try:
        first = next(iterator)
    except StopIteration:
        raise MoonlibError(
            "write_mp4: needs at least one frame; the iterable was empty."
        ) from None

    convert = _hdr_rgb16 if hdr else frame_rgb8
    blueprint = convert(first)
    height, width = int(blueprint.shape[0]), int(blueprint.shape[1])
    command = _command(str(executable), target, width, height, rate, bool(hdr), quality)

    encoded = 0
    with tempfile.TemporaryFile() as error_log:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=error_log,
            )
        except OSError as exc:
            raise MoonlibError(
                f"write_mp4: cannot run ffmpeg at {executable!r}: {exc}"
            ) from exc
        try:
            stream = process.stdin
            if stream is None:  # pragma: no cover - PIPE guarantees it
                raise MoonlibError("write_mp4: ffmpeg was started without a pipe.")
            stream.write(blueprint.tobytes())
            encoded = 1
            for frame in iterator:
                data = convert(frame)
                if data.shape[0] != height or data.shape[1] != width:
                    raise MoonlibError(
                        "write_mp4: every frame must have the same shape; frame "
                        f"{encoded} is {data.shape[:2]} against "
                        f"{(height, width)}."
                    )
                stream.write(data.tobytes())
                encoded += 1
            stream.close()
        except BrokenPipeError as exc:
            _terminate(process)
            error_log.seek(0)
            detail = _stderr_tail(error_log.read().decode("utf-8", "replace"))
            _discard(target)
            raise MoonlibError(
                "write_mp4: ffmpeg closed the input pipe before the animation "
                f"ended. Last lines of its stderr:\n{detail}"
            ) from exc
        except BaseException:
            _terminate(process)
            _discard(target)
            raise
        status = process.wait()
        error_log.seek(0)
        message = error_log.read().decode("utf-8", "replace")

    if status != 0:
        _discard(target)
        raise MoonlibError(
            f"write_mp4: ffmpeg exited with status {status}. Last lines of its "
            f"stderr:\n{_stderr_tail(message)}"
        )
    if not target.exists():
        raise MoonlibError(
            f"write_mp4: ffmpeg reported success but wrote no file at {target}."
        )
    return {
        "path": str(target),
        "frames": int(encoded),
        "fps": rate,
        "seconds": float(encoded) / rate,
        "width": int(width),
        "height": int(height),
        "codec": "hevc" if hdr else "h264",
        "hdr": bool(hdr),
    }


def _terminate(process: Any) -> None:
    """Stop a failed encode, whatever state it is in."""
    try:
        process.kill()
    except Exception:  # pragma: no cover - already gone
        pass
    try:
        process.wait()
    except Exception:  # pragma: no cover - already reaped
        pass


# ---------------------------------------------------------------------------
# probing
# ---------------------------------------------------------------------------
#: Boxes whose payload is more boxes, so a search may descend into them.
_CONTAINER_BOXES = frozenset(
    {
        b"moov",
        b"trak",
        b"mdia",
        b"minf",
        b"stbl",
        b"edts",
        b"dinf",
        b"udta",
        b"mvex",
        b"sinf",
        b"schi",
        b"ipro",
        b"wave",
    }
)


def _box(data: bytes, offset: int, end: int) -> Optional[Tuple[bytes, int, int]]:
    """``(type, payload_start, box_end)`` for the box at *offset*, or ``None``.

    Sizes are read defensively: a 64-bit ``largesize`` (``size == 1``) is
    honoured, ``size == 0`` means "to the end of the enclosing box", and a
    size that would run past the parent, or a header that does not fit, ends
    the walk instead of raising -- a truncated tail should not hide the
    metadata that precedes it.
    """
    if offset + 8 > end:
        return None
    size = int.from_bytes(data[offset : offset + 4], "big")
    kind = data[offset + 4 : offset + 8]
    header = 8
    if size == 1:
        if offset + 16 > end:
            return None
        size = int.from_bytes(data[offset + 8 : offset + 16], "big")
        header = 16
    elif size == 0:
        size = end - offset
    if size < header or offset + size > end:
        return None
    return kind, offset + header, offset + size


def _children(data: bytes, start: int, end: int) -> list:
    """Every box directly inside ``[start, end)``."""
    found = []
    offset = start
    while offset < end:
        box = _box(data, offset, end)
        if box is None:
            break
        found.append(box)
        offset = box[2]
    return found


def _find(data: bytes, start: int, end: int, kind: bytes) -> Optional[Tuple]:
    for box in _children(data, start, end):
        if box[0] == kind:
            return box
    return None


def _search(data: bytes, start: int, end: int, kind: bytes) -> Optional[Tuple]:
    """Depth-first search for *kind* through the known container boxes."""
    for box in _children(data, start, end):
        if box[0] == kind:
            return box
        if box[0] in _CONTAINER_BOXES:
            deeper = _search(data, box[1], box[2], kind)
            if deeper is not None:
                return deeper
    return None


def _full_box(data: bytes, box: Tuple) -> Tuple[int, int]:
    """``(version, payload_start)`` of a full box (version byte + 3 flags)."""
    start = box[1]
    if start + 4 > box[2]:
        raise MoonlibError("probe_mp4: a full box is shorter than its header.")
    return int(data[start]), start + 4


def _movie_header(data: bytes, moov: Tuple) -> Tuple[int, float]:
    """``(timescale, duration_in_seconds)`` from ``mvhd``."""
    box = _find(data, moov[1], moov[2], b"mvhd")
    if box is None:
        raise MoonlibError("probe_mp4: the moov box has no mvhd (movie header).")
    version, start = _full_box(data, box)
    if version == 1:
        if start + 28 > box[2]:
            raise MoonlibError("probe_mp4: truncated 64-bit mvhd.")
        timescale = int.from_bytes(data[start + 16 : start + 20], "big")
        duration = int.from_bytes(data[start + 20 : start + 28], "big")
    else:
        if start + 16 > box[2]:
            raise MoonlibError("probe_mp4: truncated mvhd.")
        timescale = int.from_bytes(data[start + 8 : start + 12], "big")
        duration = int.from_bytes(data[start + 12 : start + 16], "big")
    if timescale <= 0:
        raise MoonlibError("probe_mp4: the movie timescale is zero.")
    return timescale, duration / float(timescale)


def _media_timing(data: bytes, mdia: Tuple, stbl: Optional[Tuple]) -> Tuple[int, float, int]:
    """``(timescale, duration_seconds, frame_count)`` for the video track.

    ``mdhd`` gives the media timescale and duration; ``stts`` gives the sample
    count and the summed decode deltas, which is the number the frame rate
    should be measured against (a track's ``mvhd``-style duration may include
    edit-list padding).  ``stsz`` (or the compact ``stz2``) gives the sample
    count when ``stts`` is absent.
    """
    mdhd = _find(data, mdia[1], mdia[2], b"mdhd")
    if mdhd is None:
        raise MoonlibError("probe_mp4: the video track has no mdhd.")
    version, start = _full_box(data, mdhd)
    if version == 1:
        if start + 28 > mdhd[2]:
            raise MoonlibError("probe_mp4: truncated 64-bit mdhd.")
        timescale = int.from_bytes(data[start + 16 : start + 20], "big")
        duration = int.from_bytes(data[start + 20 : start + 28], "big")
    else:
        if start + 16 > mdhd[2]:
            raise MoonlibError("probe_mp4: truncated mdhd.")
        timescale = int.from_bytes(data[start + 8 : start + 12], "big")
        duration = int.from_bytes(data[start + 12 : start + 16], "big")
    if timescale <= 0:
        raise MoonlibError("probe_mp4: the media timescale is zero.")

    if stbl is None:
        return timescale, duration / float(timescale), 0

    samples = 0
    seconds = duration / float(timescale)
    stts = _find(data, stbl[1], stbl[2], b"stts")
    if stts is not None:
        _, start = _full_box(data, stts)
        if start + 4 <= stts[2]:
            entries = int.from_bytes(data[start : start + 4], "big")
            offset = start + 4
            ticks = 0
            for _ in range(entries):
                if offset + 8 > stts[2]:
                    break
                count = int.from_bytes(data[offset : offset + 4], "big")
                delta = int.from_bytes(data[offset + 4 : offset + 8], "big")
                samples += count
                ticks += count * delta
                offset += 8
            if ticks > 0:
                seconds = ticks / float(timescale)
    return timescale, seconds, samples


def _sample_count(data: bytes, stbl: Tuple) -> int:
    """Sample count from ``stsz`` (or the compact ``stz2`` variant)."""
    stsz = _find(data, stbl[1], stbl[2], b"stsz")
    if stsz is None:
        stsz = _find(data, stbl[1], stbl[2], b"stz2")
    if stsz is None:
        return 0
    _, start = _full_box(data, stsz)
    if start + 8 > stsz[2]:
        return 0
    if stsz[0] == b"stsz":
        # sample_size (4), sample_count (4); a per-sample size table of
        # ``sample_count`` 32-bit entries follows when sample_size is zero.
        # The count is what is wanted, so the table is never walked.
        return int.from_bytes(data[start + 4 : start + 8], "big")
    # stz2: reserved (3), field_size (1), sample_count (4).
    return int.from_bytes(data[start + 4 : start + 8], "big")


def _colr_transfer(data: bytes, box: Tuple) -> Optional[int]:
    """Transfer characteristics of a ``colr`` box, or ``None``.

    Only the ``nclx`` form carries the number; an ICC profile (``rICC`` /
    ``prof``) may or may not be PQ and is not claimed to be.
    """
    start = box[1]
    if box[2] - start < 10:
        return None
    if data[start : start + 4] != b"nclx":
        return None
    return int.from_bytes(data[start + 6 : start + 8], "big")


def _visual_track(data: bytes, trak: Tuple) -> Optional[Dict[str, Any]]:
    """Everything :func:`probe_mp4` needs from one ``trak``, if it is video."""
    mdia = _find(data, trak[1], trak[2], b"mdia")
    if mdia is None:
        return None
    hdlr = _find(data, mdia[1], mdia[2], b"hdlr")
    if hdlr is not None:
        _, start = _full_box(data, hdlr)
        if start + 8 <= hdlr[2] and data[start + 4 : start + 8] != b"vide":
            return None
    minf = _find(data, mdia[1], mdia[2], b"minf")
    if minf is None:
        return None
    stbl = _find(data, minf[1], minf[2], b"stbl")
    if stbl is None:
        return None
    stsd = _find(data, stbl[1], stbl[2], b"stsd")
    if stsd is None:
        return None
    _, start = _full_box(data, stsd)
    if start + 4 > stsd[2]:
        return None
    entries = int.from_bytes(data[start : start + 4], "big")
    if entries < 1:
        return None
    entry = _box(data, start + 4, stsd[2])
    if entry is None:
        return None
    kind, body, entry_end = entry
    if entry_end - body < 78:
        return None

    width = int.from_bytes(data[body + 24 : body + 26], "big")
    height = int.from_bytes(data[body + 26 : body + 28], "big")
    depth_field = int.from_bytes(data[body + 74 : body + 76], "big")
    children = _children(data, body + 78, entry_end)

    if kind in (b"avc1", b"avc3"):
        codec = "h264"
    elif kind in (b"hvc1", b"hev1"):
        codec = "hevc"
    else:
        codec = kind.decode("latin1", "replace").strip()

    depth: Optional[int] = None
    for child in children:
        if child[0] == b"hvcC" and child[2] - child[1] >= 18:
            # HEVCDecoderConfigurationRecord: bitDepthLumaMinus8 is the low
            # three bits of the byte after chromaFormat -- and it, unlike the
            # sample entry's ``depth`` field, is right.  ffmpeg writes 0x0018
            # (24) there for 8-bit and 10-bit alike.
            depth = 8 + (data[child[1] + 17] & 0x07)
            break
        if child[0] == b"avcC" and child[2] - child[1] >= 2:
            # AVCDecoderConfigurationRecord: profile_idc 110 / 122 / 244 are
            # the High 10 / High 4:2:2 / High 4:4:4 Predictive profiles, all
            # of which carry more than eight bits per sample.
            depth = 10 if data[child[1] + 1] in (110, 122, 244) else 8
            break
    if depth is None:
        depth = depth_field if depth_field in (8, 10, 12, 16) else 8

    transfer = None
    for child in children:
        if child[0] == b"colr":
            transfer = _colr_transfer(data, child)
            if transfer is not None:
                break
    if transfer is None:
        colr = _search(data, trak[1], trak[2], b"colr")
        if colr is not None:
            transfer = _colr_transfer(data, colr)

    timescale, seconds, stts_samples = _media_timing(data, mdia, stbl)
    samples = _sample_count(data, stbl) or stts_samples
    return {
        "track_type": b"vide" if hdlr is not None else b"",
        "codec": codec,
        "depth": int(depth),
        "width": int(width),
        "height": int(height),
        "transfer": transfer,
        "hdr": transfer == 16,
        "timescale": int(timescale),
        "media_seconds": float(seconds),
        "frames": int(samples),
    }


def probe_mp4(path: Any) -> Dict[str, Any]:
    """Read the metadata of an MP4 back out of its own boxes.

    There is no ``ffprobe`` in a single-binary ffmpeg distribution, so the
    ISO base media file format is walked here: ``moov`` for the movie duration,
    the video ``trak`` for ``mdhd``/``stts`` (frame rate), ``stsz`` (sample
    count) and the visual sample entry (codec and bit depth).  The depth comes
    from the codec configuration record -- the visual sample entry's own
    ``depth`` field is 24 for 10-bit HEVC as well, so it cannot be trusted --
    and ``hdr`` from a ``colr``/``nclx`` box whose transfer characteristics
    are 16 (SMPTE ST 2084, PQ).

    Parameters
    ----------
    path
        The file to read.

    Returns
    -------
    dict
        ``{"path", "frames", "fps", "seconds", "width", "height", "codec",
        "depth", "hdr"}``.  ``seconds`` is the movie duration from ``mvhd``;
        ``fps`` is the video sample count over the media duration.

    Raises
    ------
    MoonlibError
        When the file is unreadable or is not an MP4 with a video track.
    """
    target = Path(path)
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise MoonlibError(f"probe_mp4: cannot read {target}: {exc}") from exc
    if len(data) < 16:
        raise MoonlibError(f"probe_mp4: {target} is too short to be an MP4.")

    moov = _find(data, 0, len(data), b"moov")
    if moov is None:
        raise MoonlibError(f"probe_mp4: {target} has no moov box.")

    timescale, seconds = _movie_header(data, moov)

    chosen: Optional[Dict[str, Any]] = None
    for trak in _children(data, moov[1], moov[2]):
        if trak[0] != b"trak":
            continue
        info = _visual_track(data, trak)
        if info is None:
            continue
        if info["track_type"] == b"vide":
            chosen = info
            break
        if chosen is None:
            chosen = info
    if chosen is None:
        raise MoonlibError(f"probe_mp4: {target} has no video track.")

    frames = int(chosen["frames"])
    media_seconds = float(chosen["media_seconds"])
    if frames > 0 and media_seconds > 0.0:
        fps = frames / media_seconds
    else:
        fps = float(frames) / seconds if seconds > 0.0 else 0.0

    return {
        "path": str(target),
        "frames": frames,
        "fps": float(fps),
        "seconds": float(seconds),
        "width": int(chosen["width"]),
        "height": int(chosen["height"]),
        "codec": str(chosen["codec"]),
        "depth": int(chosen["depth"]),
        "hdr": bool(chosen["hdr"]),
        "movie_timescale": int(timescale),
        "media_seconds": media_seconds,
    }
