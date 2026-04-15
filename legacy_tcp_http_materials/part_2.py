"""Threaded server compatibility entry point."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tcp_http.server import HttpServer, ServerConfig


if __name__ == "__main__":
    server = HttpServer(ServerConfig(root=Path(__file__).resolve().parent, threaded=True))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
