import socket

import httpcore
import httpx
import pytest

from app import core
from app.core import validate_public_url


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "http://user:pass@example.com/item",
    "http://example.com:8080/item",
])
def test_rejects_unsafe_url_shapes(url):
    with pytest.raises(ValueError):
        validate_public_url(url)


def test_rejects_private_dns_result(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="private or reserved"):
        validate_public_url("https://internal.example/item")


def test_accepts_public_dns_result(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    validate_public_url("https://example.com/item")


def test_network_backend_connects_to_validated_numeric_ip(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    captured = {}

    def connect(self, host, port, timeout=None, local_address=None, socket_options=None):
        captured["host"] = host
        return object()

    monkeypatch.setattr(core.SyncBackend, "connect_tcp", connect)
    core.PublicNetworkBackend().connect_tcp("example.com", 443)
    assert captured["host"] == "93.184.216.34"


def test_network_backend_refuses_private_resolution(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443))])
    with pytest.raises(httpcore.ConnectError):
        core.PublicNetworkBackend().connect_tcp("rebind.example", 443)


def test_response_body_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(core, "MAX_RESPONSE_BYTES", 4)
    monkeypatch.setattr(core, "validate_public_url", lambda url: None)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"12345"))
    monkeypatch.setattr(core, "_public_transport", lambda: transport)
    with pytest.raises(ValueError, match="size limit"):
        core._safe_get("https://example.com/item")
