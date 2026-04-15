"""Small educational HTTP server package."""

__all__ = ["HttpResponse", "HttpServer", "ServerConfig", "fetch"]


def __getattr__(name: str):
    if name in {"HttpResponse", "fetch"}:
        from . import client

        return getattr(client, name)
    if name in {"HttpServer", "ServerConfig"}:
        from . import server

        return getattr(server, name)
    raise AttributeError(f"module 'tcp_http' has no attribute {name!r}")
