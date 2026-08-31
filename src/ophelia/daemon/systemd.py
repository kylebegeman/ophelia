"""Minimal systemd notification support without a runtime dependency."""

from __future__ import annotations

import os
import socket


def notify_systemd(message: str) -> bool:
    """Send one bounded state update when systemd supplied ``NOTIFY_SOCKET``."""

    target = os.environ.get("NOTIFY_SOCKET")
    if not target:
        return False
    if not isinstance(message, str) or not message or "\x00" in message:
        raise ValueError("systemd notification must be non-empty text without NUL bytes.")
    encoded = message.encode("utf-8")
    if len(encoded) > 4096:
        raise ValueError("systemd notification exceeds 4096 bytes.")
    address = "\x00" + target[1:] if target.startswith("@") else target
    channel = socket.socket(
        socket.AF_UNIX,
        socket.SOCK_DGRAM | getattr(socket, "SOCK_CLOEXEC", 0),
    )
    try:
        channel.connect(address)
        channel.sendall(encoded)
    finally:
        channel.close()
    return True
