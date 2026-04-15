"""A small static-file HTTP server built directly on Python sockets."""

from __future__ import annotations

from dataclasses import dataclass
from email.utils import formatdate
import logging
import mimetypes
from pathlib import Path
import socket
import threading
from typing import Iterable
from urllib.parse import unquote, urlsplit

ENCODING = "iso-8859-1"
DEFAULT_BUFFER_SIZE = 64 * 1024
MAX_HEADER_BYTES = 16 * 1024
SUPPORTED_METHODS = {"GET", "HEAD"}
REASONS = {
    200: "OK",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    410: "Gone",
    414: "URI Too Long",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
}

LOGGER = logging.getLogger(__name__)


def _is_valid_method_token(method: str) -> bool:
    separators = set("()<>@,;:\\<>/[]?={} \t")
    return bool(method) and all(ord(ch) < 128 and ch not in separators for ch in method)


@dataclass(frozen=True)
class ServerConfig:
    """Runtime configuration for :class:`HttpServer`."""

    host: str = "127.0.0.1"
    port: int = 6789
    root: Path = Path(".")
    threaded: bool = True
    shutdown_path: str = "/BreakOut"
    backlog: int = 50
    timeout: float = 5.0

    def __post_init__(self) -> None:
        if not (0 <= self.port <= 65535):
            raise ValueError("port must be between 0 and 65535")
        if self.backlog < 1:
            raise ValueError("backlog must be at least 1")
        if self.timeout <= 0:
            raise ValueError("timeout must be greater than 0")
        if not self.shutdown_path.startswith("/"):
            raise ValueError("shutdown_path must start with '/'")

    def normalized_root(self) -> Path:
        return self.root.expanduser().resolve()


@dataclass(frozen=True)
class HttpRequest:
    method: str
    target: str
    version: str
    headers: dict[str, str]


class BadRequest(ValueError):
    """Raised when an incoming request cannot be parsed safely."""


class RequestTooLarge(ValueError):
    """Raised when headers exceed the configured safety limit."""


class UriTooLong(ValueError):
    """Raised when the request target exceeds safety limits."""


class HttpServer:
    """Serve static files from a configured directory over HTTP/1.1."""

    def __init__(self, config: ServerConfig):
        self.config = config
        self.root = config.normalized_root()
        self._shutdown = threading.Event()
        self._socket: socket.socket | None = None
        self._threads: list[threading.Thread] = []

    @property
    def address(self) -> tuple[str, int]:
        if self._socket is None:
            return (self.config.host, self.config.port)
        return self._socket.getsockname()[:2]

    def serve_forever(self) -> None:
        if not self.root.exists():
            raise FileNotFoundError(f"Document root does not exist: {self.root}")
        if not self.root.is_dir():
            raise NotADirectoryError(f"Document root is not a directory: {self.root}")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.bind((self.config.host, self.config.port))
            server_socket.listen(self.config.backlog)
            server_socket.settimeout(0.5)
            self._socket = server_socket
            LOGGER.info("Serving %s at http://%s:%s", self.root, self.address[0], self.address[1])

            try:
                while not self._shutdown.is_set():
                    try:
                        client_socket, address = server_socket.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        if self._shutdown.is_set():
                            break
                        raise

                    if self.config.threaded:
                        thread = threading.Thread(
                            target=self._handle_connection,
                            args=(client_socket, address),
                            daemon=True,
                        )
                        self._threads.append(thread)
                        thread.start()
                    else:
                        self._handle_connection(client_socket, address)
            finally:
                self._shutdown.set()
                for thread in self._threads:
                    thread.join(timeout=1.0)
                self._socket = None

    def shutdown(self) -> None:
        self._shutdown.set()
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass

    def _handle_connection(self, client_socket: socket.socket, address: tuple[str, int]) -> None:
        with client_socket:
            client_socket.settimeout(self.config.timeout)
            request: HttpRequest | None = None
            try:
                request = self._read_request(client_socket)
                LOGGER.info("%s %s %s", address[0], request.method, request.target)
                self._route_request(client_socket, request)
            except UriTooLong:
                include_body = request is None or request.method != "HEAD"
                self._send_error(client_socket, 414, "Request path is too long.", include_body=include_body)
            except RequestTooLarge:
                include_body = request is None or request.method != "HEAD"
                self._send_error(
                    client_socket,
                    431,
                    "Request headers are too large.",
                    include_body=include_body,
                )
            except BadRequest as exc:
                include_body = request is None or request.method != "HEAD"
                self._send_error(
                    client_socket,
                    400,
                    str(exc) or "Malformed request.",
                    include_body=include_body,
                )
            except socket.timeout:
                include_body = request is None or request.method != "HEAD"
                self._send_error(
                    client_socket,
                    400,
                    "Timed out while reading the request.",
                    include_body=include_body,
                )
            except OSError as exc:
                if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
                    LOGGER.debug("Client disconnected while processing request: %s", exc)
                    return
                raise
            except Exception as exc:
                include_body = request is None or request.method != "HEAD"
                LOGGER.exception("Unhandled server error")
                self._send_error(
                    client_socket,
                    500,
                    "The server could not complete the request.",
                    include_body=include_body,
                )

    def _read_request(self, client_socket: socket.socket) -> HttpRequest:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = client_socket.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_HEADER_BYTES:
                raise RequestTooLarge()

        if not data:
            raise BadRequest("Empty request.")
        if b"\r\n\r\n" not in data:
            raise BadRequest("Request headers were not terminated.")

        header_blob = bytes(data).split(b"\r\n\r\n", 1)[0]
        lines = header_blob.decode(ENCODING, errors="replace").split("\r\n")
        request_line = lines[0].split()
        if len(request_line) != 3:
            raise BadRequest("Request line must be '<METHOD> <PATH> <VERSION>'.")

        method, target, version = request_line
        if not _is_valid_method_token(method):
            raise BadRequest("HTTP method token is invalid.")
        if not version.startswith("HTTP/"):
            raise BadRequest("Unsupported request version format.")
        if version not in {"HTTP/1.1", "HTTP/1.0"}:
            raise BadRequest("Only HTTP/1.0 and HTTP/1.1 are supported.")

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            name, delimiter, value = line.partition(":")
            if not delimiter:
                raise BadRequest("Header lines must contain ':'.")
            headers[name.strip().lower()] = value.strip()

        if version == "HTTP/1.1" and "host" not in headers:
            raise BadRequest("HTTP/1.1 requests must include a Host header.")

        return HttpRequest(method=method.upper(), target=target, version=version, headers=headers)

    def _route_request(self, client_socket: socket.socket, request: HttpRequest) -> None:
        path = urlsplit(request.target).path or "/"
        if path == self.config.shutdown_path:
            self._send_error(client_socket, 410, "Server shutdown requested.")
            self.shutdown()
            return

        if request.method not in SUPPORTED_METHODS:
            self._send_error(
                client_socket,
                405,
                f"Method {request.method} is not supported.",
                extra_headers=[("Allow", ", ".join(sorted(SUPPORTED_METHODS)))],
            )
            return

        file_path = self._resolve_request_path(path)
        if file_path is None:
            self._send_error(
                client_socket,
                403,
                "Requested path is outside the document root.",
                include_body=request.method != "HEAD",
            )
            return
        if file_path.is_dir():
            index_file = file_path / "index.html"
            if index_file.exists() and index_file.is_file():
                file_path = index_file
            else:
                self._send_error(
                    client_socket,
                    403,
                    "Directory listings are disabled.",
                    include_body=request.method != "HEAD",
                )
                return
        if not file_path.exists():
            self._send_error(
                client_socket,
                404,
                "The requested file was not found.",
                include_body=request.method != "HEAD",
            )
            return

        self._send_file(client_socket, file_path, include_body=request.method != "HEAD")

    def _resolve_request_path(self, raw_path: str) -> Path | None:
        if len(raw_path) > 2048:
            raise UriTooLong()
        decoded_path = unquote(raw_path)
        if "\x00" in decoded_path:
            raise BadRequest("Request path contains an invalid null byte.")
        relative_path = decoded_path.lstrip("/")
        candidate = (self.root / relative_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None
        return candidate

    def _send_file(self, client_socket: socket.socket, file_path: Path, *, include_body: bool) -> None:
        content_type, _ = mimetypes.guess_type(file_path.name)
        headers = [
            ("Content-Type", content_type or "application/octet-stream"),
            ("Content-Length", str(file_path.stat().st_size)),
            ("Last-Modified", formatdate(file_path.stat().st_mtime, usegmt=True)),
        ]
        self._send_headers(client_socket, 200, headers)
        if include_body:
            with file_path.open("rb") as file_handle:
                while chunk := file_handle.read(DEFAULT_BUFFER_SIZE):
                    client_socket.sendall(chunk)

    def _send_error(
        self,
        client_socket: socket.socket,
        status_code: int,
        message: str,
        *,
        extra_headers: Iterable[tuple[str, str]] = (),
        include_body: bool = True,
    ) -> None:
        reason = REASONS.get(status_code, "Error")
        body = (
            "<!doctype html>\n"
            "<html><head><title>{status_code} {reason}</title></head>"
            "<body><h1>{status_code} {reason}</h1><p>{message}</p></body></html>\n"
        ).format(status_code=status_code, reason=reason, message=message).encode("utf-8")
        response_body = body if include_body else b""
        headers = [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(response_body))),
            *extra_headers,
        ]
        self._send_headers(client_socket, status_code, headers)
        if response_body:
            client_socket.sendall(response_body)

    def _send_headers(
        self,
        client_socket: socket.socket,
        status_code: int,
        headers: Iterable[tuple[str, str]],
    ) -> None:
        reason = REASONS.get(status_code, "OK")
        response_headers = [
            f"HTTP/1.1 {status_code} {reason}",
            f"Date: {formatdate(usegmt=True)}",
            "Server: tcp-http/1.0",
            "Connection: close",
            *[f"{name}: {value}" for name, value in headers],
            "",
            "",
        ]
        client_socket.sendall("\r\n".join(response_headers).encode(ENCODING))


def main() -> int:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Run the TCP HTTP static-file server.")
    parser.add_argument("--host", default="127.0.0.1", help="Address to bind; default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=6789, help="Port to bind; default: 6789")
    parser.add_argument("--root", type=Path, default=Path("."), help="Directory to serve; default: cwd")
    parser.add_argument(
        "--mode",
        choices=("threaded", "single"),
        default="threaded",
        help="Connection handling mode; default: threaded",
    )
    args = parser.parse_args()

    server = HttpServer(
        ServerConfig(
            host=args.host,
            port=args.port,
            root=args.root,
            threaded=args.mode == "threaded",
        )
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
