"""Run local tests with outbound network connections denied (loopback remains usable)."""

from __future__ import annotations

import ipaddress
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_getaddrinfo = socket.getaddrinfo


def _check(address):
    if not isinstance(address, tuple):  # AF_UNIX
        return
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode("ascii")
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise RuntimeError("Outbound network disabled for offline tests")


def _guard(self, address):
    _check(address)
    return _connect(self, address)


def _guard_ex(self, address):
    _check(address)
    return _connect_ex(self, address)


def _guard_dns(host, port, *args, **kwargs):
    _check((host, port))
    return _getaddrinfo(host, port, *args, **kwargs)


socket.getaddrinfo = _guard_dns
socket.socket.connect = _guard
socket.socket.connect_ex = _guard_ex

if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main(sys.argv[1:]))
