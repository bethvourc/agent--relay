from __future__ import annotations

import http.client
import threading
import time
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import TestCase

from agent_relay.exporters.prometheus import serve_prometheus


def start_dashboard_server(
    test: TestCase,
    *,
    repo_root: Path,
    refresh_interval: float = 0.1,
    extractor: Any = None,
) -> tuple[ThreadingHTTPServer, threading.Thread, int]:
    captured: dict[str, ThreadingHTTPServer] = {}

    def factory(addr, handler):
        server = ThreadingHTTPServer(addr, handler)
        captured["server"] = server
        return server

    kwargs: dict[str, Any] = {
        "repo_root": repo_root,
        "host": "127.0.0.1",
        "port": 0,
        "refresh_interval": refresh_interval,
        "server_factory": factory,
    }
    if extractor is not None:
        kwargs["extractor"] = extractor

    thread = threading.Thread(
        target=serve_prometheus,
        kwargs=kwargs,
        daemon=True,
    )
    thread.start()

    for _ in range(50):
        if "server" in captured:
            break
        time.sleep(0.01)
    test.assertIn("server", captured, "server never started")
    port = captured["server"].server_address[1]
    return captured["server"], thread, port


def stop_dashboard_server(server: ThreadingHTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    thread.join(timeout=2.0)


def get_dashboard(port: int, path: str) -> tuple[int, str, str]:
    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=2)) as conn:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        return resp.status, resp.getheader("Content-Type", ""), body
