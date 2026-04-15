"""Client helpers for sending simple HTTP requests."""

from __future__ import annotations

from dataclasses import dataclass
import socket

ENCODING = "iso-8859-1"
BUFFER_SIZE = 64 * 1024


@dataclass(frozen=True)
class HttpResponse:
    """Parsed HTTP response returned by :func:`fetch`."""

    status_code: int
    reason: str
    headers: dict[str, str]
    body: bytes
    raw: bytes

    @property
    def text(self) -> str:
        charset = "utf-8"
        content_type = self.headers.get("content-type", "")
        for part in content_type.split(";"):
            key, _, value = part.strip().partition("=")
            if key.lower() == "charset" and value:
                charset = value
        return self.body.decode(charset, errors="replace")


def fetch(
    host: str,
    port: int,
    path: str,
    *,
    timeout: float = 5.0,
    method: str = "GET",
) -> HttpResponse:
    """Send a single HTTP request and return the parsed response.

    The function intentionally stays small and dependency-free because this
    project is about socket fundamentals. It still sends a valid HTTP/1.1
    request and reads until the server closes the connection.
    """

    if not host:
        raise ValueError("host is required")
    if not (0 <= port <= 65535):
        raise ValueError("port must be between 0 and 65535")

    method_token = method.upper().strip()
    if not method_token or any(ch in method_token for ch in "\r\n\t "):
        raise ValueError("method must be a valid HTTP token")

    normalized_path = path if path.startswith("/") else f"/{path}"
    if "\r" in normalized_path or "\n" in normalized_path:
        raise ValueError("path must not contain CR or LF characters")

    request = (
        f"{method_token} {normalized_path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode(ENCODING)

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(request)
        chunks: list[bytes] = []
        while True:
            try:
                chunk = sock.recv(BUFFER_SIZE)
            except socket.timeout:
                raise TimeoutError("Timed out while waiting for response data")
            if not chunk:
                break
            chunks.append(chunk)

    raw = b"".join(chunks)
    header_blob, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        raise ValueError("Server response did not contain an HTTP header terminator")

    header_lines = header_blob.decode(ENCODING, errors="replace").split("\r\n")
    status_line = header_lines[0].split(" ", 2)
    if len(status_line) < 2 or not status_line[1].isdigit():
        raise ValueError(f"Invalid HTTP status line: {header_lines[0]!r}")

    headers: dict[str, str] = {}
    for line in header_lines[1:]:
        name, delimiter, value = line.partition(":")
        if delimiter:
            headers[name.strip().lower()] = value.strip()

    return HttpResponse(
        status_code=int(status_line[1]),
        reason=status_line[2] if len(status_line) > 2 else "",
        headers=headers,
        body=body,
        raw=raw,
    )


def main() -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Request a file from the TCP HTTP server.")
    parser.add_argument("server_host", help="Server host or IP address")
    parser.add_argument("server_port", type=int, help="Server TCP port")
    parser.add_argument("filename", help="File path to request, with or without a leading slash")
    parser.add_argument("--method", default="GET", help="HTTP method to use; default: GET")
    args = parser.parse_args()

    try:
        response = fetch(args.server_host, args.server_port, args.filename, method=args.method)
    except OSError as exc:
        print(f"Connection failed: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"Invalid response: {exc}", file=sys.stderr)
        return 3

    print(f"Status: {response.status_code} {response.reason}")
    print("----------------------------------------")
    if response.body:
        print(response.text, end="" if response.text.endswith("\n") else "\n")
    return 0 if response.status_code < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())
