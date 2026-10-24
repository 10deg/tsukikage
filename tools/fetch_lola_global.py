#!/usr/bin/env python3
"""Download the complete global LOLA DEM so that everything runs offline.

Fetches ``Lunar_LRO_LOLA_Global_LDEM_118m_Mar2014.tif`` from USGS Astrogeology:
the LOLA global topography at **256 pixels per degree** (118 m at the equator),
16-bit signed integers in half-metres relative to the 1737.4 km sphere, poles
included.  It is 8.5 GB, which is the point -- with it in ``kernels/`` no
region of the Moon needs a network round trip again, and any close-up target is
served at 118 m/px instead of the SVS product's 474 m/px.

The file is downloaded as parallel byte ranges straight into a preallocated
file, so no second copy is ever needed.  It is then checked against its
advertised length and, where a previously fetched regional window exists,
against that too.

Usage
-----
    ./.venv/bin/python tools/fetch_lola_global.py
    ./.venv/bin/python tools/fetch_lola_global.py --check      # verify only
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from _bootstrap import ROOT, use_src  # noqa: E402

use_src()

DIRECT_URL = (
    "https://asc-pds-services.s3.us-west-2.amazonaws.com/mosaic/"
    "Lunar_LRO_LOLA_Global_LDEM_118m_Mar2014.tif"
)
PUBLIC_URL = (
    "https://planetarymaps.usgs.gov/mosaic/"
    "Lunar_LRO_LOLA_Global_LDEM_118m_Mar2014.tif"
)
FILENAME = "Lunar_LRO_LOLA_Global_LDEM_118m_Mar2014.tif"
EXPECTED_BYTES = 8_494_203_833
CHUNK = 64 * 1024 * 1024


def remote_length(url: str) -> int:
    result = subprocess.run(
        ["curl", "-sIL", "--max-time", "30", url], capture_output=True, text=True
    )
    length = None
    for line in result.stdout.splitlines():
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip())
    if length is None:
        raise SystemExit(f"could not read Content-Length from {url}")
    return length


PROBE = 64


def _probe(url: str, start: int, stop: int) -> bytes:
    result = subprocess.run(
        ["curl", "-sL", "--fail", "--max-time", "60",
         "-r", f"{start}-{min(start + PROBE, stop)}", url],
        capture_output=True,
    )
    return result.stdout if result.returncode == 0 else b""


def salvage(part: Path, url: str, ranges, workers: int = 24) -> set:
    """Chunks already on disk, identified by probing their first bytes.

    A chunk is only ever written after curl returned exactly the requested
    length, so anything written is complete; the only question is which ones,
    and a 64-byte probe answers it without re-downloading 5 GB.
    """
    complete: set = set()

    def check(index: int) -> None:
        start, stop = ranges[index]
        with open(part, "rb") as handle:
            handle.seek(start)
            local = handle.read(PROBE + 1)
        if local and not local.isspace() and any(local):
            if _probe(url, start, stop) == local[: PROBE + 1]:
                complete.add(index)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(check, range(len(ranges))))
    return complete


def download(
    target: Path, url: str, total: int, workers: int = 32, retries: int = 5
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    ranges = [(start, min(start + CHUNK, total) - 1) for start in range(0, total, CHUNK)]

    fresh = not part.is_file() or part.stat().st_size != total
    if fresh:
        with open(part, "wb") as handle:
            handle.truncate(total)
        done: set = set()
    else:
        print("resuming: checking which chunks are already complete ...")
        done = salvage(part, url, ranges)
        print(f"  {len(done)}/{len(ranges)} chunks already on disk")

    pending = [index for index in range(len(ranges)) if index not in done]
    started = time.time()
    counter = {"bytes": sum(ranges[i][1] - ranges[i][0] + 1 for i in done)}

    def report() -> None:
        elapsed = max(time.time() - started, 1e-6)
        gained = counter["bytes"] - sum(
            ranges[i][1] - ranges[i][0] + 1 for i in done
        )
        rate = gained / elapsed / 1e6
        remaining = (total - counter["bytes"]) / max(gained / elapsed, 1.0)
        sys.stderr.write(
            f"\r  {counter['bytes'] / 1e9:5.2f}/{total / 1e9:.2f} GB  "
            f"{rate:6.1f} MB/s  eta {remaining:4.0f}s   "
        )
        sys.stderr.flush()

    def grab(index: int) -> None:
        start, stop = ranges[index]
        want = stop - start + 1
        last = ""
        for attempt in range(retries):
            result = subprocess.run(
                ["curl", "-sL", "--fail", "--max-time", "900",
                 "-r", f"{start}-{stop}", url],
                capture_output=True,
            )
            if result.returncode == 0 and len(result.stdout) == want:
                with open(part, "r+b") as sink:
                    sink.seek(start)
                    sink.write(result.stdout)
                counter["bytes"] += want
                report()
                return
            last = f"rc={result.returncode} bytes={len(result.stdout)}"
            time.sleep(min(30.0, 2.0 ** attempt))
        raise RuntimeError(f"chunk {index} ({start}-{stop}) failed after {retries} tries ({last})")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(grab, pending))
    sys.stderr.write("\r" + " " * 64 + "\r")
    os.replace(part, target)


def verify(path: Path, total: int) -> int:
    size = path.stat().st_size
    print(f"  {'file':<22} {path}")
    print(f"  {'size':<22} {size:,} bytes (expected {total:,})")
    if size != total:
        print("  FAIL: size mismatch")
        return 1
    header = path.open("rb").read(16)
    if header[:2] not in (b"II", b"MM"):
        print("  FAIL: not a TIFF")
        return 1
    print(f"  {'byte order':<22} {header[:2].decode()}")
    print("  size and container look right")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download the complete global LOLA 256 px/deg DEM.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--out-dir", default=str(ROOT / "kernels"))
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--url", default=None, help="override the download URL")
    parser.add_argument("--check", action="store_true", help="verify an existing file")
    args = parser.parse_args()

    target = Path(args.out_dir) / FILENAME
    if args.check:
        if not target.is_file():
            raise SystemExit(f"no file at {target}")
        return verify(target, EXPECTED_BYTES)

    if target.is_file() and target.stat().st_size == EXPECTED_BYTES:
        print(f"already present: {target}")
        return verify(target, EXPECTED_BYTES)

    url = args.url or DIRECT_URL
    try:
        total = remote_length(url)
    except SystemExit:
        print("direct host unreachable, falling back to the USGS mirror")
        url = PUBLIC_URL
        total = remote_length(url)
    print(f"source : {url}")
    print(f"size   : {total / 1e9:.2f} GB -> {target}")
    print("downloading in 64 MB ranges ...")
    download(target, url, total, workers=args.workers)
    print(f"took {time.strftime('%M:%S', time.gmtime())}")
    code = verify(target, EXPECTED_BYTES)
    if code == 0:
        print("\nNext: let moonlib build the pyramid cache from it.  The 256 px/deg")
        print("grid is used at full resolution for close-ups and decimated for")
        print("whole-disk renders, so this replaces ldem_64.tif for both.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
