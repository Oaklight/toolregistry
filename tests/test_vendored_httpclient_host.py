"""The vendored HTTP client sends the authority in ``Host`` (#486).

zerodep 0.7.4 makes the async path include a non-default port, matching the
sync path and RFC 7230 section 5.4.  A local raw-socket server captures the
request line so both paths are checked against the bytes on the wire.
"""

from __future__ import annotations

import socket
import threading

import pytest

from toolregistry._vendor.httpclient import AsyncClient, Client


class _CaptureServer:
    """One-shot server that records each request's head and answers 200."""

    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(2)
        self.port = self._socket.getsockname()[1]
        self.requests: list[bytes] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            while True:
                conn, _ = self._socket.accept()
                with conn:
                    data = b""
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    self.requests.append(data)
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Length: 2\r\n"
                        b"Connection: close\r\n\r\nok"
                    )
        except OSError:
            return

    def host_header(self, index: int) -> str:
        """Return the ``Host`` value of the *index*-th captured request."""
        head = self.requests[index].decode("latin-1")
        for line in head.split("\r\n"):
            if line.lower().startswith("host:"):
                return line.split(":", 1)[1].strip()
        raise AssertionError(f"no Host header in {head!r}")

    def close(self) -> None:
        self._socket.close()
        self._thread.join(timeout=5)


def test_sync_client_includes_a_non_default_port() -> None:
    """The sync path already did this; pin it before comparing the async path."""
    server = _CaptureServer()
    try:
        response = Client().get(f"http://127.0.0.1:{server.port}/ping")
        assert response.status_code == 200
        assert server.host_header(0) == f"127.0.0.1:{server.port}"
    finally:
        server.close()


@pytest.mark.asyncio
async def test_async_client_matches_the_sync_host_header() -> None:
    """Before 0.7.4 the async path dropped the port and could hit a wrong vhost."""
    server = _CaptureServer()
    try:
        async with AsyncClient() as client:
            response = await client.get(f"http://127.0.0.1:{server.port}/ping")
        assert response.status_code == 200
        assert server.host_header(0) == f"127.0.0.1:{server.port}"
    finally:
        server.close()
