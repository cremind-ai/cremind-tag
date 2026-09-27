"""The fake Cremind over real HTTP (a minimal HTTP/1.1 server on its own event loop thread).

For tests of code that builds its own HTTP client — the CLI (``connect``,
``daemon run --once``, ``doctor``) — where an ``httpx.MockTransport`` cannot be
injected. ``with FakeCremindServer() as server: server.url, server.fake``.
Calls into ``server.fake`` from the test thread go through :meth:`call`.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable

import httpx

from fake_cremind import FakeCremind  # type: ignore[import-not-found]  # loaded by conftest

REASONS = {200: "OK", 201: "Created", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
           409: "Conflict", 410: "Gone", 422: "Unprocessable Entity", 500: "Internal Server Error",
           502: "Bad Gateway", 503: "Service Unavailable"}


class FakeCremindServer:
    def __init__(self) -> None:
        self.fake: FakeCremind | None = None
        self.port = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.base_events.Server | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> FakeCremindServer:
        self._thread = threading.Thread(target=self._main, name="fake cremind", daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise TimeoutError("fake Cremind did not start")
        return self

    def __exit__(self, *exc: object) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(5)

    def call[T](self, fn: Callable[[FakeCremind], T]) -> T:
        """Run ``fn(fake)`` on the server's loop (the fake is not thread-safe)."""
        assert self._loop is not None and self.fake is not None
        fake = self.fake

        async def wrapper() -> T:
            return fn(fake)

        return asyncio.run_coroutine_threadsafe(wrapper(), self._loop).result(10)

    def _main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self.fake = FakeCremind(url="http://127.0.0.1")

        async def start() -> None:
            self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
            self.port = self._server.sockets[0].getsockname()[1]

        loop.run_until_complete(start())
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            assert self._server is not None
            self._server.close()
            loop.close()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                method, target, _ = line.decode("latin-1").split(" ", 2)
                headers: dict[str, str] = {}
                while True:
                    header = (await reader.readline()).decode("latin-1").rstrip("\r\n")
                    if not header:
                        break
                    name, _, value = header.partition(":")
                    headers[name.strip().lower()] = value.strip()
                body = await reader.readexactly(int(headers.get("content-length") or 0))
                assert self.fake is not None
                request = httpx.Request(method, f"http://127.0.0.1:{self.port}{target}", headers=headers,
                                        content=body)
                response = await self.fake.handle(request)
                payload = response.content
                writer.write((f"HTTP/1.1 {response.status_code} {REASONS.get(response.status_code, 'Status')}\r\n"
                              f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
                              f"Connection: keep-alive\r\n\r\n").encode("latin-1") + payload)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):
            return
        finally:
            writer.close()


__all__ = ["FakeCremindServer"]

