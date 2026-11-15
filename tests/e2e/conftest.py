"""Shared fixture of the browser tests: the API (and the frontend it serves) on a free local port."""
from __future__ import annotations

import os
import socket
import threading
import time

import pytest


@pytest.fixture(scope="module")
def base_url(request):
    """Serves the test module's `SERVICE` (an ApiService) with uvicorn in dev auth mode; yields its URL."""
    uvicorn = pytest.importorskip("uvicorn")
    from app.api.app import create_app

    os.environ["AF_AUTH_MODE"] = "dev"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(request.module.SERVICE),
                                           host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
