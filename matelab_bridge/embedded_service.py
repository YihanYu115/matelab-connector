"""Run the local FastAPI bridge inside the native desktop process."""

from __future__ import annotations

import threading
import time
from dataclasses import replace

import uvicorn

from .api import create_app
from .config import BridgeConfig
from .port_manager import NATIVE_CONSOLE_API_VERSION, is_running_bridge, select_connector_port


class EmbeddedBridgeService:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.port = config.port
        self.owned = False
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        selected_port, already_running = select_connector_port(
            self.config, required_console_api=NATIVE_CONSOLE_API_VERSION
        )
        self.port = selected_port
        if already_running:
            self.owned = False
            return
        self.config = replace(self.config, port=selected_port)
        application = create_app(self.config)
        server_config = uvicorn.Config(
            application,
            host=self.config.host,
            port=selected_port,
            log_config=None,
            access_log=False,
        )
        self._server = uvicorn.Server(server_config)
        self._thread = threading.Thread(
            target=self._server.run,
            name="matelab-connector-api",
            daemon=True,
        )
        self._thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if is_running_bridge(selected_port):
                self.owned = True
                return
            if not self._thread.is_alive():
                break
            time.sleep(0.1)
        self.stop()
        raise RuntimeError(f"Connector API failed to start on port {selected_port}")

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        if self.owned and server is not None:
            server.should_exit = True
            if thread is not None:
                thread.join(timeout=10)
        self._server = None
        self._thread = None
        self.owned = False

    def restart(self, config: BridgeConfig) -> None:
        if not self.owned:
            raise RuntimeError("The running API belongs to another Connector process")
        self.stop()
        self.config = config
        self.port = config.port
        self.start()
