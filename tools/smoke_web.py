#!/usr/bin/env python3
"""Start the page's server, drive the real API, and report what came back.

Run::

    ./.venv/bin/python tools/smoke_web.py [--port 8791] [--big]

``--big`` renders the case that used to abort the process: a 3000 px close-up
on the terminator, where several band threads hit the accelerator at once.  It
takes about half a minute, so it is off by default.

This is an end-to-end check, not a unit test: it runs the same
:class:`http.server.ThreadingHTTPServer` the user runs, over a real socket, and
reads the bytes back the way the page does.  Nothing here is mocked, which is
the point -- the failure it exists to catch was a native abort that no unit test
could see.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.error
import urllib.request

from _bootstrap import use_web  # noqa: E402

use_web()

import server  # noqa: E402

WHEN = {"at": "2026-01-10T16:00:00", "tz_offset_hours": 8,
        "observer": {"lat": 30, "lon": 120}}


def post(port: int, path: str, body: dict) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=900) as response:
        return json.loads(response.read().decode("utf-8"))


def get(port: int, path: str) -> tuple:
    """``(status, body)`` for a GET; a 404 comes back as a 404, not an exception."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=60) as reply:
            return reply.status, reply.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--big", action="store_true",
                        help="also render the 3000 px terminator close-up")
    args = parser.parse_args(argv)

    httpd = server.build_server("127.0.0.1", args.port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    failures = []
    try:
        status, body = get(args.port, "/")
        print(f"GET  /                 {status}  {len(body)} bytes")
        if status != 200 or b"<html" not in body.lower():
            failures.append("the index page did not come back")

        status, body = get(args.port, "/api/health")
        health = json.loads(body)
        print(f"GET  /api/health       {status}  limits={sorted(health.get('limits', {}))}")
        if "compute" in health:
            failures.append("/api/health still advertises a compute backend")

        status, body = get(args.port, "/api/compute")
        print(f"GET  /api/compute      {status}  (expected 404: the route is gone)")
        if status != 404:
            failures.append("/api/compute is still routed")

        started = time.time()
        reply = post(args.port, "/api/disk", {"when": WHEN, "preview": {"width": 320},
                                              "samples": 1, "shadow_steps": 24})
        image = reply.get("image") or ""
        print(f"POST /api/disk         {time.time() - started:6.2f}s  "
              f"{len(image)} bytes of data URL, "
              f"{reply.get('geometry', {}).get('bands')} bands")
        if not image.startswith("data:image/png;base64,"):
            failures.append("/api/disk did not return a PNG data URL")

        request = {"when": WHEN, "telescope": {}, "camera": {}, "processing": {},
                   "selection": {"lon": -5.0, "lat": -12.0, "fov_radius_arcsec": 200.0,
                                 "rotation_deg": 0.0},
                   "output": {"width": 400, "height": 400, "samples": 1},
                   "shadow_steps": 48}
        started = time.time()
        reply = post(args.port, "/api/region", request)
        print(f"POST /api/region       {time.time() - started:6.2f}s  "
              f"{len(reply.get('image') or '')} bytes, "
              f"render_id={reply.get('render_id')}")
        if not (reply.get("image") or "").startswith("data:image/png;base64,"):
            failures.append("/api/region did not return a PNG data URL")

        if args.big:
            request["output"] = {"width": 3000, "height": 3000, "samples": 1}
            started = time.time()
            reply = post(args.port, "/api/region", request)
            print(f"POST /api/region 3000² {time.time() - started:6.2f}s  "
                  f"{len(reply.get('image') or '')} bytes  "
                  f"band_workers={reply.get('info', {}).get('region', {}).get('band_workers')}")
            if not (reply.get("image") or "").startswith("data:image/png;base64,"):
                failures.append("the 3000 px region render did not come back")
    finally:
        httpd.shutdown()
        httpd.server_close()

    for problem in failures:
        print(f"FAIL  {problem}")
    print("smoke: " + ("FAILED" if failures else "ok"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
