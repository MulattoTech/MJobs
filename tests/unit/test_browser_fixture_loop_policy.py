"""The browser-only adapter must not change unrelated server entry points."""
import asyncio
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from tests.browser import conftest as transport


def test_fixture_adapter_only_selects_owned_loopback_worker_thread():
    original = Mock(return_value="original")
    server = uvicorn.Server(uvicorn.Config(FastAPI(), loop="auto"))
    run = transport.browser_fixture_run(original)
    with socket.socket() as bound, socket.socket() as unbound:
        bound.bind(("127.0.0.1", 0))
        with patch.object(transport, "run_owned_browser_server", return_value="selector") as selected:
            assert run(server, [bound]) == "original"  # main thread
            with ThreadPoolExecutor(max_workers=1) as worker:
                assert worker.submit(run, server, [bound]).result() == "selector"
                for sockets in (None, [], [unbound], [bound, unbound]):
                    assert worker.submit(run, server, sockets).result() == "original"
                server.config.loop = "asyncio"
                assert worker.submit(run, server, [bound]).result() == "original"
                server.config.loop = "auto"
                server.config.workers = 2
                assert worker.submit(run, server, [bound]).result() == "original"
            selected.assert_called_once_with(server, sockets=[bound])
            assert original.call_count == 7


def test_fixture_adapter_rejects_non_loopback_and_closed_sockets():
    with socket.socket() as sock:
        sock.bind(("0.0.0.0", 0))
        assert not transport.is_owned_fixture_socket(sock)
    assert not transport.is_owned_fixture_socket(sock)
    with socket.socket(type=socket.SOCK_DGRAM) as datagram:
        datagram.bind(("127.0.0.1", 0))
        assert not transport.is_owned_fixture_socket(datagram)


def test_fixture_adapter_disconnect_cancels_response_and_stops_real_server():
    app = FastAPI()
    disconnected = threading.Event()

    @app.get("/loop")
    async def loop():
        return {"selector": isinstance(asyncio.get_running_loop(), asyncio.SelectorEventLoop)}

    @app.get("/stream")
    async def stream():
        async def chunks():
            try:
                yield b"first chunk"
                await asyncio.sleep(60)
            finally:
                disconnected.set()
        return StreamingResponse(chunks())

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, loop="auto", log_level="error"))
        run = transport.browser_fixture_run(uvicorn.Server.run)
        thread = threading.Thread(target=run, args=(server,), kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                assert thread.is_alive() and time.monotonic() < deadline
                time.sleep(.02)
            with httpx.Client(trust_env=False, timeout=5) as client:
                assert client.get(origin + "/loop").json() == {"selector": True}
                with client.stream("GET", origin + "/stream") as response:
                    assert next(response.iter_bytes()) == b"first chunk"
                assert disconnected.wait(5), "Canceled response did not release its generator"
        finally:
            server.should_exit = True
            thread.join(timeout=5)
        assert not thread.is_alive(), "Owned API fixture did not stop"
