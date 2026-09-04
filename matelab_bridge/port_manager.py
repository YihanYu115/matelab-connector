"""Loopback port discovery shared by launchers and the native application."""

from __future__ import annotations

import json
import socket
from urllib.error import URLError
from urllib.request import urlopen

from .config import BridgeConfig

NATIVE_CONSOLE_API_VERSION = 1


def bridge_health(port: int) -> dict[str, object] | None:
    """Return a verified Connector health document, never another service's payload."""
    try:
        with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.4) as response:
            payload = json.loads(response.read(4096))
    except (OSError, URLError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("service") != "matelab-desktop-bridge":
        return None
    return payload


def is_running_bridge(port: int) -> bool:
    return bridge_health(port) is not None


def port_is_available(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((host, port))
    except OSError:
        return False
    return True


def select_connector_port(
    config: BridgeConfig, *, required_console_api: int | None = None
) -> tuple[int, bool]:
    """Return a usable port and whether an existing Connector owns it."""
    for port in range(config.port, min(config.port + 20, 65536)):
        health = bridge_health(port)
        if health is not None:
            if (
                required_console_api is not None
                and health.get("console_api") != required_console_api
            ):
                raise RuntimeError(
                    f"An older Connector is already running on port {port}. "
                    "Close its window or CMD launcher, then start this application again."
                )
            return port, True
        if port_is_available(config.host, port):
            return port, False
    raise RuntimeError(f"No free Connector port was found from {config.port} to {config.port + 19}")
