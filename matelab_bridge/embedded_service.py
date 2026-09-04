"""Run the local FastAPI bridge inside the native desktop process."""

from __future__ import annotations

import threading
import time
from dataclasses import replace

import uvicorn

from .api import create_app
from .config import BridgeConfig
from .port_manager import NATIVE_CONSOLE_API_VERSION, is_running_bridge, select_connector_port
from .single_instance import SingleInstanceLock


class ConnectorAlreadyRunningError(RuntimeError):
    """Raised when another native Connector owns this user's data directory."""


class EmbeddedBridgeService:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.port = config.port
        self.owned = False
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._instance_lock = SingleInstanceLock(config.data_dir / "native-instance.lock")

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        if not self._instance_lock.acquire():
            raise ConnectorAlreadyRunningError(
                "MatElab Connector 已经在运行。请使用已打开的窗口; "
                "若看不到窗口, 请先在任务管理器中结束旧实例后再启动。"
            )
        try:
            selected_port, already_running = select_connector_port(
                self.config, required_console_api=NATIVE_CONSOLE_API_VERSION
            )
            self.port = selected_port
            if already_running:
                raise ConnectorAlreadyRunningError(
                    f"已有 MatElab Connector 正在端口 {selected_port} 运行。"
                    "请使用已打开的窗口, 或先关闭旧实例。"
                )
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
            raise RuntimeError(f"Connector API failed to start on port {selected_port}")
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        if server is not None:
            server.should_exit = True
            if thread is not None:
                thread.join(timeout=10)
        self._server = None
        self._thread = None
        self.owned = False
        self._instance_lock.release()

    def restart(self, config: BridgeConfig) -> None:
        if not self.owned:
            raise RuntimeError("The running API belongs to another Connector process")
        self.stop()
        self.config = config
        self.port = config.port
        self.start()
