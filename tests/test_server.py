from __future__ import annotations

from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest

from tcp_http.server import HttpServer, ServerConfig


def read_response(host: str, port: int, request: bytes) -> bytes:
    with socket.create_connection((host, port), timeout=3) as sock:
        sock.sendall(request)
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


def parse_response(response: bytes) -> tuple[str, dict[str, str], bytes]:
    headers_blob, _, body = response.partition(b"\r\n\r\n")
    lines = headers_blob.decode("iso-8859-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name:
            headers[name.lower()] = value.strip()
    return lines[0], headers, body


class RunningServer:
    def __init__(self, root: Path, *, threaded: bool = True) -> None:
        self.server = HttpServer(
            ServerConfig(host="127.0.0.1", port=0, root=root, threaded=threaded, timeout=1)
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "RunningServer":
        self.thread.start()
        deadline = time.time() + 3
        while self.server.address[1] == 0 and time.time() < deadline:
            time.sleep(0.01)
        if self.server.address[1] == 0:
            raise RuntimeError("Server did not bind to an ephemeral port")
        return self

    def __exit__(self, *_: object) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)

    @property
    def host(self) -> str:
        return self.server.address[0]

    @property
    def port(self) -> int:
        return self.server.address[1]


class ServerTests(unittest.TestCase):
    def test_get_existing_file_returns_body_and_headers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "HelloWorld.html").write_text("<h1>Hello</h1>", encoding="utf-8")

            with RunningServer(root) as running:
                response = read_response(
                    running.host,
                    running.port,
                    b"GET /HelloWorld.html HTTP/1.1\r\nHost: localhost\r\n\r\n",
                )

        status, headers, body = parse_response(response)
        self.assertEqual(status, "HTTP/1.1 200 OK")
        self.assertEqual(headers["content-type"], "text/html")
        self.assertEqual(headers["content-length"], str(len(b"<h1>Hello</h1>")))
        self.assertEqual(body, b"<h1>Hello</h1>")

    def test_server_survives_idle_accept_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with RunningServer(Path(directory), threaded=False) as running:
                time.sleep(0.7)
                self.assertTrue(running.thread.is_alive())

    def test_head_existing_file_returns_headers_without_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data.txt").write_text("abc", encoding="utf-8")

            with RunningServer(root) as running:
                response = read_response(
                    running.host,
                    running.port,
                    b"HEAD /data.txt HTTP/1.1\r\nHost: localhost\r\n\r\n",
                )

        status, headers, body = parse_response(response)
        self.assertEqual(status, "HTTP/1.1 200 OK")
        self.assertEqual(headers["content-length"], "3")
        self.assertEqual(body, b"")

    def test_missing_file_returns_404(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with RunningServer(Path(directory)) as running:
                response = read_response(
                    running.host,
                    running.port,
                    b"GET /missing.txt HTTP/1.1\r\nHost: localhost\r\n\r\n",
                )

        status, _, body = parse_response(response)
        self.assertEqual(status, "HTTP/1.1 404 Not Found")
        self.assertIn(b"The requested file was not found.", body)

    def test_directory_traversal_is_forbidden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root.parent / "outside-secret.txt"
            outside.write_text("secret", encoding="utf-8")
            self.addCleanup(outside.unlink, missing_ok=True)

            with RunningServer(root) as running:
                response = read_response(
                    running.host,
                    running.port,
                    b"GET /../outside-secret.txt HTTP/1.1\r\nHost: localhost\r\n\r\n",
                )

        status, _, body = parse_response(response)
        self.assertEqual(status, "HTTP/1.1 403 Forbidden")
        self.assertNotIn(b"secret", body)

    def test_encoded_directory_traversal_is_forbidden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root.parent / "outside-secret-encoded.txt"
            outside.write_text("secret", encoding="utf-8")
            self.addCleanup(outside.unlink, missing_ok=True)

            with RunningServer(root) as running:
                response = read_response(
                    running.host,
                    running.port,
                    b"GET /..%2Foutside-secret-encoded.txt HTTP/1.1\r\nHost: localhost\r\n\r\n",
                )

        status, _, body = parse_response(response)
        self.assertEqual(status, "HTTP/1.1 403 Forbidden")
        self.assertNotIn(b"secret", body)

    def test_root_path_serves_index_html_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_text("<h1>index</h1>", encoding="utf-8")

            with RunningServer(root) as running:
                response = read_response(
                    running.host,
                    running.port,
                    b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n",
                )

        status, _, body = parse_response(response)
        self.assertEqual(status, "HTTP/1.1 200 OK")
        self.assertEqual(body, b"<h1>index</h1>")

    def test_unsupported_method_returns_405_with_allow_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with RunningServer(Path(directory)) as running:
                response = read_response(
                    running.host,
                    running.port,
                    b"POST /file.txt HTTP/1.1\r\nHost: localhost\r\n\r\n",
                )

        status, headers, _ = parse_response(response)
        self.assertEqual(status, "HTTP/1.1 405 Method Not Allowed")
        self.assertEqual(headers["allow"], "GET, HEAD")

    def test_head_missing_file_returns_empty_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with RunningServer(Path(directory)) as running:
                response = read_response(
                    running.host,
                    running.port,
                    b"HEAD /missing.txt HTTP/1.1\r\nHost: localhost\r\n\r\n",
                )

        status, headers, body = parse_response(response)
        self.assertEqual(status, "HTTP/1.1 404 Not Found")
        self.assertEqual(headers["content-length"], "0")
        self.assertEqual(body, b"")

    def test_http_1_1_without_host_header_returns_400(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with RunningServer(Path(directory)) as running:
                response = read_response(
                    running.host,
                    running.port,
                    b"GET / HTTP/1.1\r\nUser-Agent: test\r\n\r\n",
                )

        status, _, body = parse_response(response)
        self.assertEqual(status, "HTTP/1.1 400 Bad Request")
        self.assertIn(b"Host header", body)

    def test_path_too_long_returns_414(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with RunningServer(Path(directory)) as running:
                long_path = "/" + ("a" * 2049)
                request = f"GET {long_path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode(
                    "iso-8859-1"
                )
                response = read_response(running.host, running.port, request)

        status, _, body = parse_response(response)
        self.assertEqual(status, "HTTP/1.1 414 URI Too Long")
        self.assertIn(b"Request path is too long.", body)

    def test_server_requires_existing_document_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing_root = Path(directory) / "does-not-exist"
            server = HttpServer(ServerConfig(root=missing_root, port=0))
            with self.assertRaises(FileNotFoundError):
                server.serve_forever()

    def test_malformed_request_returns_400(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with RunningServer(Path(directory)) as running:
                response = read_response(running.host, running.port, b"not-http\r\n\r\n")

        status, _, body = parse_response(response)
        self.assertEqual(status, "HTTP/1.1 400 Bad Request")
        self.assertIn(b"Request line", body)

    def test_breakout_returns_410_and_stops_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            running = RunningServer(Path(directory))
            with running:
                response = read_response(
                    running.host,
                    running.port,
                    b"GET /BreakOut HTTP/1.1\r\nHost: localhost\r\n\r\n",
                )
                running.thread.join(timeout=2)

        status, _, body = parse_response(response)
        self.assertEqual(status, "HTTP/1.1 410 Gone")
        self.assertIn(b"Server shutdown requested.", body)
        self.assertFalse(running.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
