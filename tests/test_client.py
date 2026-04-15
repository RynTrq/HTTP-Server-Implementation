from __future__ import annotations

import socketserver
import threading
import unittest

from tcp_http.client import fetch


class FixedResponseHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.recv(4096)
        self.request.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Length: 5\r\n"
            b"\r\n"
            b"hello"
        )


class ClientTests(unittest.TestCase):
    def test_fetch_sends_valid_request_and_parses_response(self) -> None:
        with socketserver.TCPServer(("127.0.0.1", 0), FixedResponseHandler) as server:
            thread = threading.Thread(target=server.handle_request, daemon=True)
            thread.start()

            response = fetch("127.0.0.1", server.server_address[1], "index.txt")

            thread.join(timeout=1)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.reason, "OK")
        self.assertEqual(response.headers["content-type"], "text/plain; charset=utf-8")
        self.assertEqual(response.text, "hello")

    def test_fetch_rejects_invalid_method_token(self) -> None:
        with self.assertRaises(ValueError):
            fetch("127.0.0.1", 80, "/", method="GET POST")

    def test_fetch_rejects_newline_in_path(self) -> None:
        with self.assertRaises(ValueError):
            fetch("127.0.0.1", 80, "/index.html\r\nInjected: true")

    def test_fetch_rejects_invalid_port(self) -> None:
        with self.assertRaises(ValueError):
            fetch("127.0.0.1", 70000, "/")


if __name__ == "__main__":
    unittest.main()
