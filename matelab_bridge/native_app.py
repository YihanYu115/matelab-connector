"""Native cross-platform Connector console built with the Python Tk toolkit."""

# ruff: noqa: RUF001

from __future__ import annotations

import getpass
import json
import os
import platform
import re
import subprocess
import sys
import tkinter as tk
import webbrowser
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from functools import partial
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, TypeVar

from tkinterdnd2 import COPY, DND_FILES, TkinterDnD  # type: ignore[import-untyped]

from .config import BridgeConfig
from .desktop_settings import DesktopSettings
from .embedded_service import ConnectorAlreadyRunningError, EmbeddedBridgeService
from .native_client import ConnectorApiError, NativeConnectorClient

MATELAB_PORTAL_URL = "https://matelab.iphy.ac.cn/eln/"
T = TypeVar("T")

COLORS = {
    "canvas": "#EEF1EA",
    "panel": "#FAFBF7",
    "panel_alt": "#F4F6F0",
    "ink": "#17251F",
    "muted": "#64716B",
    "green": "#176B4B",
    "green_hover": "#11563C",
    "green_soft": "#DCEBE2",
    "line": "#D9DED8",
    "error": "#A43C32",
    "error_soft": "#FBE9E6",
    "warning": "#9B6816",
    "warning_soft": "#FFF3D8",
    "white": "#FFFFFF",
}

STATE_LABELS = {
    "receiving": "接收中",
    "validating": "校验中",
    "accepted": "已接收",
    "ready": "等待上传",
    "uploading_files": "上传附件",
    "creating_record": "创建记录",
    "populating_record": "写入记录",
    "verifying_export": "回读核验",
    "matelab_verified": "已核验",
    "complete": "已完成",
    "retry_wait": "等待重试",
    "auth_required": "需要登录",
    "needs_attention": "需要处理",
    "abandoned": "已终止",
}


def _format_problem(exc: BaseException) -> str:
    if isinstance(exc, ConnectorApiError):
        problem = exc.problem
        lines = [str(problem.get("message") or str(exc))]
        if problem.get("action"):
            lines.extend(("", f"建议：{problem['action']}"))
        if problem.get("code"):
            lines.append(f"错误代码：{problem['code']}")
        if problem.get("request_id"):
            lines.append(f"请求编号：{problem['request_id']}")
        if problem.get("details") is not None:
            lines.extend(
                (
                    "",
                    "详细信息：",
                    json.dumps(problem["details"], ensure_ascii=False, indent=2),
                )
            )
        return "\n".join(lines)
    return f"{exc}\n\n建议：重启 Connector；若仍失败，请保留此信息并生成诊断包。"


def _human_time(value: Any) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    return parsed.astimezone().strftime("%m-%d %H:%M:%S")


def _windows_clipboard_files() -> list[Path]:
    """Read Explorer's CF_HDROP file list without copying file contents."""
    if platform.system() != "Windows":
        return []

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    is_format_available = user32.IsClipboardFormatAvailable
    is_format_available.argtypes = [wintypes.UINT]
    is_format_available.restype = wintypes.BOOL
    open_clipboard = user32.OpenClipboard
    open_clipboard.argtypes = [wintypes.HWND]
    open_clipboard.restype = wintypes.BOOL
    get_clipboard_data = user32.GetClipboardData
    get_clipboard_data.argtypes = [wintypes.UINT]
    get_clipboard_data.restype = wintypes.HANDLE
    close_clipboard = user32.CloseClipboard
    close_clipboard.argtypes = []
    close_clipboard.restype = wintypes.BOOL
    drag_query_file = shell32.DragQueryFileW
    drag_query_file.argtypes = [
        wintypes.HANDLE,
        wintypes.UINT,
        wintypes.LPWSTR,
        wintypes.UINT,
    ]
    drag_query_file.restype = wintypes.UINT

    cf_hdrop = 15
    if not is_format_available(cf_hdrop) or not open_clipboard(None):
        return []
    try:
        handle = get_clipboard_data(cf_hdrop)
        if not handle:
            return []
        count = int(drag_query_file(handle, 0xFFFFFFFF, None, 0))
        paths: list[Path] = []
        for index in range(count):
            length = int(drag_query_file(handle, index, None, 0))
            buffer = ctypes.create_unicode_buffer(length + 1)
            drag_query_file(handle, index, buffer, length + 1)
            paths.append(Path(buffer.value))
        return paths
    finally:
        close_clipboard()


class NativeConnectorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("MatElab Connector")
        self.root.geometry("1180x760")
        self.root.minsize(980, 650)
        self.root.configure(bg=COLORS["canvas"])
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._configure_styles()

        base_config = BridgeConfig.from_env()
        settings = DesktopSettings.load(base_config.data_dir, default_port=base_config.port)
        self.settings = settings
        self.config = replace(base_config, port=settings.api_port)
        self.service = EmbeddedBridgeService(self.config)
        self.client: NativeConnectorClient | None = None
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="connector-ui")
        self.closed = False

        self.container = tk.Frame(self.root, bg=COLORS["canvas"])
        self.container.pack(fill="both", expand=True)
        self.login_page = LoginPage(self.container, self)
        self.console_page = ConsolePage(self.container, self)
        for page in (self.login_page, self.console_page):
            page.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.show_login()
        self._run(self._start_service, self._service_ready, self._service_start_failed)

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        for candidate in ("vista", "clam"):
            if candidate in style.theme_names():
                style.theme_use(candidate)
                break
        style.configure(
            "Connector.TEntry",
            fieldbackground=COLORS["white"],
            foreground=COLORS["ink"],
            padding=10,
        )
        style.configure(
            "Connector.TCombobox",
            fieldbackground=COLORS["white"],
            foreground=COLORS["ink"],
            padding=9,
        )
        style.configure("Connector.Treeview", rowheight=34, font=("Segoe UI", 10))
        style.configure(
            "Connector.Treeview.Heading", font=("Segoe UI Semibold", 10), padding=(8, 8)
        )
        style.map("Connector.Treeview", background=[("selected", COLORS["green_soft"])])

    def _start_service(self) -> dict[str, Any]:
        self.service.start()
        self.config = self.service.config
        self.client = NativeConnectorClient(self.service.base_url, self.config)
        return self.client.session()

    def _service_ready(self, session: dict[str, Any]) -> None:
        self.login_page.set_service_ready(
            self.service.base_url, self.service.port, self.config.port
        )
        if session.get("authenticated"):
            self.login_page.set_status("发现已保存的安全登录，正在读取记录本…")
            self._run(
                self.require_client().notebooks,
                self._resume_console,
                self.login_page.set_error,
            )
        else:
            self.login_page.set_status("Connector API 已就绪，请登录 MatElab")

    def _service_start_failed(self, exc: BaseException) -> None:
        if isinstance(exc, ConnectorAlreadyRunningError):
            messagebox.showinfo("MatElab Connector 已在运行", str(exc), parent=self.root)
            self.close()
            return
        self.login_page.set_error(exc)

    def _resume_console(self, notebooks: list[dict[str, Any]]) -> None:
        self.console_page.activate(notebooks)
        self.show_console()

    def require_client(self) -> NativeConnectorClient:
        if self.client is None:
            raise RuntimeError("Connector API 尚未启动，请稍候。")
        return self.client

    def _run(
        self,
        operation: Callable[[], T],
        success: Callable[[T], None] | None = None,
        failure: Callable[[BaseException], None] | None = None,
    ) -> None:
        future = self.executor.submit(operation)

        def finished(value: Future[T]) -> None:
            if self.closed:
                return
            try:
                result = value.result()
            except BaseException as exc:  # UI boundary: render every operational failure.
                callback = failure or self.show_error
                self.root.after(0, callback, exc)
            else:
                if success is not None:
                    self.root.after(0, success, result)

        future.add_done_callback(finished)

    def show_login(self) -> None:
        self.login_page.tkraise()
        self.login_page.focus_username()

    def show_console(self) -> None:
        self.console_page.tkraise()
        self.console_page.refresh_all()

    def show_error(self, exc: BaseException) -> None:
        messagebox.showerror("MatElab Connector", _format_problem(exc), parent=self.root)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.client is not None:
            self.client.close()
        self.service.stop()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


class LoginPage(tk.Frame):
    def __init__(self, parent: tk.Widget, app: NativeConnectorApp) -> None:
        super().__init__(parent, bg=COLORS["canvas"])
        self.app = app
        self.username = tk.StringVar(value=app.settings.matelab_username or "")
        self.password = tk.StringVar()
        self.status = tk.StringVar(value="正在启动本地 Connector API…")
        self.error = tk.StringVar()

        shell = tk.Frame(self, bg=COLORS["canvas"])
        shell.place(relx=0.5, rely=0.5, anchor="center", width=980, height=610)
        left = tk.Frame(shell, bg=COLORS["green"], padx=48, pady=48)
        left.place(x=0, y=0, width=390, height=610)
        right = tk.Frame(shell, bg=COLORS["panel"], padx=58, pady=42)
        right.place(x=390, y=0, width=590, height=610)

        tk.Label(
            left,
            text="M",
            font=("Segoe UI Black", 30),
            fg=COLORS["green"],
            bg=COLORS["white"],
            width=2,
            height=1,
        ).pack(anchor="w")
        tk.Label(
            left,
            text="MatElab\nConnector",
            font=("Segoe UI Semibold", 30),
            justify="left",
            fg=COLORS["white"],
            bg=COLORS["green"],
        ).pack(anchor="w", pady=(32, 18))
        tk.Label(
            left,
            text=(
                "本地实验记录连接器\n\n登录、选择记录本、提交记录，"
                "\n同时为其他程序提供可靠的本地 API。"
            ),
            font=("Microsoft YaHei UI", 11),
            justify="left",
            fg="#D9EEE4",
            bg=COLORS["green"],
            wraplength=290,
        ).pack(anchor="w")
        tk.Label(
            left,
            textvariable=self.status,
            font=("Microsoft YaHei UI", 9),
            justify="left",
            fg="#D9EEE4",
            bg=COLORS["green"],
            wraplength=290,
        ).pack(side="bottom", anchor="w")

        tk.Label(
            right,
            text="连接你的 MatElab",
            font=("Microsoft YaHei UI", 22, "bold"),
            fg=COLORS["ink"],
            bg=COLORS["panel"],
        ).pack(anchor="w")
        tk.Label(
            right,
            text="先在浏览器确认账号或找回密码，再回到这里授权 Connector。",
            font=("Microsoft YaHei UI", 10),
            fg=COLORS["muted"],
            bg=COLORS["panel"],
            wraplength=470,
            justify="left",
        ).pack(anchor="w", pady=(8, 20))
        self.browser_button = self._button(
            right,
            "在默认浏览器登录 / 找回密码  ↗",
            lambda: webbrowser.open(MATELAB_PORTAL_URL),
            primary=True,
        )
        self.browser_button.pack(fill="x")
        tk.Label(
            right,
            text="MatElab 目前未开放浏览器登录回传 API Token，因此网页登录不会自动授权本应用。",
            font=("Microsoft YaHei UI", 9),
            fg=COLORS["warning"],
            bg=COLORS["warning_soft"],
            padx=12,
            pady=9,
            wraplength=440,
            justify="left",
        ).pack(fill="x", pady=(10, 22))

        divider = tk.Frame(right, bg=COLORS["line"], height=1)
        divider.pack(fill="x", pady=(0, 18))
        tk.Label(
            right,
            text="授权 Connector API",
            font=("Microsoft YaHei UI", 11, "bold"),
            fg=COLORS["ink"],
            bg=COLORS["panel"],
        ).pack(anchor="w", pady=(0, 10))
        self.username_entry = self._field(right, "账号", self.username)
        self.password_entry = self._field(right, "密码", self.password, show="●")
        self.password_entry.bind("<Return>", lambda _event: self.login())
        self.login_button = self._button(right, "授权并进入控制台  →", self.login, primary=True)
        self.login_button.pack(fill="x", pady=(14, 0))
        self.error_label = tk.Label(
            right,
            textvariable=self.error,
            font=("Microsoft YaHei UI", 9),
            fg=COLORS["error"],
            bg=COLORS["panel"],
            wraplength=460,
            justify="left",
        )
        self.error_label.pack(anchor="w", pady=(12, 0))

    @staticmethod
    def _button(
        parent: tk.Widget, text: str, command: Callable[[], object], *, primary: bool
    ) -> tk.Button:
        background = COLORS["green"] if primary else COLORS["panel_alt"]
        foreground = COLORS["white"] if primary else COLORS["ink"]
        return tk.Button(
            parent,
            text=text,
            command=command,
            relief="flat",
            bd=0,
            padx=16,
            pady=12,
            cursor="hand2",
            font=("Microsoft YaHei UI", 10, "bold"),
            bg=background,
            fg=foreground,
            activebackground=COLORS["green_hover"] if primary else COLORS["green_soft"],
            activeforeground=foreground,
        )

    @staticmethod
    def _field(
        parent: tk.Widget, label: str, variable: tk.StringVar, *, show: str | None = None
    ) -> ttk.Entry:
        tk.Label(
            parent,
            text=label,
            font=("Microsoft YaHei UI", 9),
            fg=COLORS["ink"],
            bg=COLORS["panel"],
        ).pack(anchor="w", pady=(5, 5))
        entry = ttk.Entry(parent, textvariable=variable, show=show or "", style="Connector.TEntry")
        entry.pack(fill="x", ipady=3)
        return entry

    def focus_username(self) -> None:
        self.after(100, self.username_entry.focus_set)

    def set_status(self, text: str) -> None:
        self.status.set(text)

    def set_service_ready(self, api_url: str, actual_port: int, requested_port: int) -> None:
        if actual_port == requested_port:
            self.status.set(f"本地 API 已启动：{api_url}")
        else:
            self.status.set(f"设定端口被占用，已安全改用：{api_url}")

    def set_error(self, exc: BaseException) -> None:
        self.login_button.configure(state="normal", text="授权并进入控制台  →")
        self.password.set("")
        self.error.set(_format_problem(exc))

    def login(self) -> None:
        username = self.username.get().strip()
        password = self.password.get()
        if not username or not password:
            self.error.set("请填写 MatElab 账号和密码。")
            return
        self.error.set("")
        self.login_button.configure(state="disabled", text="正在授权并读取记录本…")

        def operation() -> list[dict[str, Any]]:
            result = self.app.require_client().login(username, password)
            return [dict(item) for item in result.get("notebooks", [])]

        def success(notebooks: list[dict[str, Any]]) -> None:
            self.password.set("")
            self.login_button.configure(state="normal", text="授权并进入控制台  →")
            self.app.settings = replace(
                self.app.settings,
                matelab_username=username,
            )
            self.app.settings.save(self.app.config.data_dir)
            self.app.console_page.activate(notebooks)
            self.app.show_console()

        self.app._run(operation, success, self.set_error)


class ConsolePage(tk.Frame):
    def __init__(self, parent: tk.Widget, app: NativeConnectorApp) -> None:
        super().__init__(parent, bg=COLORS["canvas"])
        self.app = app
        self.notebooks: list[dict[str, Any]] = []
        self.attachments: list[Path] = []
        self.tasks_data: dict[str, dict[str, Any]] = {}
        self.current_page = "overview"
        self.api_url = tk.StringVar(value="—")
        self.auth_state = tk.StringVar(value="正在检查")
        self.queue_summary = tk.StringVar(value="尚无任务")
        self.notice = tk.StringVar()
        self.default_notebook_status = tk.StringVar(value="尚未设置 API 默认记录本")
        self.account_value = tk.StringVar(value="未登录")

        sidebar = tk.Frame(self, bg="#122D23", width=235, padx=22, pady=28)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(
            sidebar,
            text="M  MatElab",
            font=("Segoe UI Semibold", 18),
            fg=COLORS["white"],
            bg="#122D23",
        ).pack(anchor="w")
        tk.Label(
            sidebar,
            text="CONNECTOR CONSOLE",
            font=("Segoe UI", 8),
            fg="#92B1A4",
            bg="#122D23",
        ).pack(anchor="w", pady=(2, 34))
        self.nav_buttons: dict[str, tk.Button] = {}
        for key, label in (
            ("overview", "总览"),
            ("upload", "手动提交"),
            ("update", "追加描述"),
            ("tasks", "任务与错误"),
            ("settings", "API 设置"),
        ):
            button = tk.Button(
                sidebar,
                text=label,
                anchor="w",
                command=partial(self.show_page, key),
                font=("Microsoft YaHei UI", 10, "bold"),
                relief="flat",
                bd=0,
                padx=14,
                pady=11,
                bg="#122D23",
                fg="#C8D8D1",
                activebackground="#214738",
                activeforeground=COLORS["white"],
                cursor="hand2",
            )
            button.pack(fill="x", pady=2)
            self.nav_buttons[key] = button
        account_footer = tk.Frame(sidebar, bg="#122D23")
        account_footer.pack(side="bottom", fill="x")
        tk.Frame(account_footer, bg="#315446", height=1).pack(fill="x", pady=(0, 14))
        tk.Label(
            account_footer,
            text="当前登录账号",
            font=("Microsoft YaHei UI", 8),
            fg="#92B1A4",
            bg="#122D23",
        ).pack(anchor="w", padx=14)
        tk.Label(
            account_footer,
            textvariable=self.account_value,
            font=("Microsoft YaHei UI", 9, "bold"),
            fg=COLORS["white"],
            bg="#122D23",
            justify="left",
            wraplength=175,
        ).pack(anchor="w", padx=14, pady=(3, 8))
        tk.Button(
            account_footer,
            text="退出 MatElab 账号",
            anchor="w",
            command=self.logout,
            font=("Microsoft YaHei UI", 9),
            relief="flat",
            bd=0,
            padx=14,
            pady=10,
            bg="#122D23",
            fg="#92B1A4",
            activebackground="#214738",
            activeforeground=COLORS["white"],
            cursor="hand2",
        ).pack(fill="x")

        body = tk.Frame(self, bg=COLORS["canvas"])
        body.pack(side="left", fill="both", expand=True)
        topbar = tk.Frame(body, bg=COLORS["canvas"], padx=38, pady=18)
        topbar.pack(fill="x")
        tk.Label(
            topbar,
            textvariable=self.notice,
            font=("Microsoft YaHei UI", 9),
            fg=COLORS["green"],
            bg=COLORS["canvas"],
        ).pack(side="left")
        tk.Label(
            topbar,
            textvariable=self.api_url,
            font=("Consolas", 9),
            fg=COLORS["muted"],
            bg=COLORS["canvas"],
        ).pack(side="right")
        self.page_host = tk.Frame(body, bg=COLORS["canvas"])
        self.page_host.pack(fill="both", expand=True, padx=38, pady=(0, 32))
        self.pages: dict[str, tk.Frame] = {}
        self._build_overview()
        self._build_upload()
        self._build_update()
        self._build_tasks()
        self._build_settings()
        self.show_page("overview")

    def _new_page(self, key: str) -> tk.Frame:
        page = tk.Frame(self.page_host, bg=COLORS["canvas"])
        page.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.pages[key] = page
        return page

    @staticmethod
    def _heading(parent: tk.Widget, title: str, subtitle: str) -> None:
        tk.Label(
            parent,
            text=title,
            font=("Microsoft YaHei UI", 24, "bold"),
            fg=COLORS["ink"],
            bg=COLORS["canvas"],
        ).pack(anchor="w")
        tk.Label(
            parent,
            text=subtitle,
            font=("Microsoft YaHei UI", 10),
            fg=COLORS["muted"],
            bg=COLORS["canvas"],
        ).pack(anchor="w", pady=(5, 24))

    @staticmethod
    def _card(parent: tk.Widget, **pack: Any) -> tk.Frame:
        card = tk.Frame(parent, bg=COLORS["panel"], padx=22, pady=20)
        card.pack(**pack)
        return card

    @staticmethod
    def _action_button(parent: tk.Widget, text: str, command: Callable[[], object]) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            relief="flat",
            bd=0,
            padx=14,
            pady=9,
            font=("Microsoft YaHei UI", 9, "bold"),
            bg=COLORS["green"],
            fg=COLORS["white"],
            activebackground=COLORS["green_hover"],
            activeforeground=COLORS["white"],
            cursor="hand2",
        )

    def _build_overview(self) -> None:
        page = self._new_page("overview")
        self._heading(page, "Connector 总览", "本地 API、MatElab 会话和后台同步队列一目了然。")
        row = tk.Frame(page, bg=COLORS["canvas"])
        row.pack(fill="x")
        auth = self._card(row, side="left", fill="both", expand=True, padx=(0, 9))
        tk.Label(
            auth,
            text="MATELAB 会话",
            font=("Segoe UI Semibold", 9),
            fg=COLORS["muted"],
            bg=COLORS["panel"],
        ).pack(anchor="w")
        tk.Label(
            auth,
            textvariable=self.auth_state,
            font=("Microsoft YaHei UI", 18, "bold"),
            fg=COLORS["green"],
            bg=COLORS["panel"],
        ).pack(anchor="w", pady=(9, 2))
        api = self._card(row, side="left", fill="both", expand=True, padx=(9, 0))
        tk.Label(
            api,
            text="本地 API",
            font=("Segoe UI Semibold", 9),
            fg=COLORS["muted"],
            bg=COLORS["panel"],
        ).pack(anchor="w")
        tk.Label(
            api,
            text="● 正常运行",
            font=("Microsoft YaHei UI", 18, "bold"),
            fg=COLORS["green"],
            bg=COLORS["panel"],
        ).pack(anchor="w", pady=(9, 2))

        queue = self._card(page, fill="both", expand=True, pady=(18, 0))
        tk.Label(
            queue,
            text="同步队列",
            font=("Microsoft YaHei UI", 13, "bold"),
            fg=COLORS["ink"],
            bg=COLORS["panel"],
        ).pack(anchor="w")
        tk.Label(
            queue,
            textvariable=self.queue_summary,
            font=("Microsoft YaHei UI", 11),
            fg=COLORS["muted"],
            bg=COLORS["panel"],
            justify="left",
        ).pack(anchor="w", pady=(9, 18))
        actions = tk.Frame(queue, bg=COLORS["panel"])
        actions.pack(anchor="w")
        self._action_button(actions, "打开 API 文档", self.open_docs).pack(side="left")
        self._action_button(actions, "复制 API 地址", self.copy_api_url).pack(side="left", padx=10)
        self._action_button(actions, "打开数据目录", self.open_data_dir).pack(side="left")

    def _build_upload(self) -> None:
        page = self._new_page("upload")
        self._heading(page, "手动提交记录", "记录先可靠保存到本机队列，再由后台上传并核验。")
        form = self._card(page, fill="both", expand=True)
        labels = tk.Frame(form, bg=COLORS["panel"])
        labels.pack(fill="x")
        tk.Label(
            labels,
            text="目标记录本",
            font=("Microsoft YaHei UI", 9, "bold"),
            fg=COLORS["ink"],
            bg=COLORS["panel"],
        ).pack(side="left")
        tk.Button(
            labels,
            text="刷新记录本",
            command=self.refresh_notebooks,
            relief="flat",
            bd=0,
            fg=COLORS["green"],
            bg=COLORS["panel"],
            activebackground=COLORS["panel"],
            cursor="hand2",
        ).pack(side="right")
        self.notebook_value = tk.StringVar()
        self.notebook_combo = ttk.Combobox(
            form,
            textvariable=self.notebook_value,
            state="readonly",
            style="Connector.TCombobox",
        )
        self.notebook_combo.pack(fill="x", pady=(6, 6), ipady=2)
        default_row = tk.Frame(form, bg=COLORS["panel"])
        default_row.pack(fill="x", pady=(0, 12))
        self.default_notebook_label = tk.Label(
            default_row,
            textvariable=self.default_notebook_status,
            font=("Microsoft YaHei UI", 9),
            fg=COLORS["muted"],
            bg=COLORS["panel"],
            anchor="w",
        )
        self.default_notebook_label.pack(side="left", fill="x", expand=True)
        self.save_default_button = self._action_button(
            default_row,
            "设为 API 默认记录本",
            self.save_default_notebook,
        )
        self.save_default_button.pack(side="right")
        self.title_value = tk.StringVar()
        self.actor_value = tk.StringVar(value=getpass.getuser())
        for label, variable in (("标题", self.title_value), ("记录人（可选）", self.actor_value)):
            tk.Label(
                form,
                text=label,
                font=("Microsoft YaHei UI", 9, "bold"),
                fg=COLORS["ink"],
                bg=COLORS["panel"],
            ).pack(anchor="w", pady=(2, 5))
            ttk.Entry(form, textvariable=variable, style="Connector.TEntry").pack(
                fill="x", pady=(0, 10), ipady=2
            )
        tk.Label(
            form,
            text="记录内容",
            font=("Microsoft YaHei UI", 9, "bold"),
            fg=COLORS["ink"],
            bg=COLORS["panel"],
        ).pack(anchor="w", pady=(2, 5))
        self.content_text = tk.Text(
            form,
            height=7,
            wrap="word",
            font=("Microsoft YaHei UI", 10),
            relief="solid",
            bd=1,
            highlightthickness=0,
            bg=COLORS["white"],
            fg=COLORS["ink"],
            insertbackground=COLORS["ink"],
            padx=10,
            pady=8,
        )
        self.content_text.pack(fill="both", expand=True)
        tk.Label(
            form,
            text="附件（可选）",
            font=("Microsoft YaHei UI", 9, "bold"),
            fg=COLORS["ink"],
            bg=COLORS["panel"],
        ).pack(anchor="w", pady=(12, 5))
        self.attachment_text = tk.StringVar()
        self.attachment_label = tk.Label(
            form,
            textvariable=self.attachment_text,
            font=("Microsoft YaHei UI", 9),
            fg=COLORS["muted"],
            bg=COLORS["panel_alt"],
            anchor="center",
            justify="center",
            relief="solid",
            bd=1,
            padx=12,
            pady=12,
            cursor="hand2",
        )
        self.attachment_label.pack(fill="x")
        self.attachment_label.bind("<Button-1>", lambda _event: self.choose_attachments())
        drop_target: Any = self.attachment_label
        drop_target.drop_target_register(DND_FILES)
        drop_target.dnd_bind("<<DropEnter>>", self._attachment_drag_enter)
        drop_target.dnd_bind("<<DropLeave>>", self._attachment_drag_leave)
        drop_target.dnd_bind("<<Drop>>", self._drop_attachments)
        self.bind_all("<Control-v>", self._paste_attachments, add="+")
        self._update_attachment_label()

        attach_row = tk.Frame(form, bg=COLORS["panel"])
        attach_row.pack(fill="x", pady=(10, 0))
        self._action_button(attach_row, "选择附件", self.choose_attachments).pack(side="left")
        tk.Button(
            attach_row,
            text="清空附件",
            command=self.clear_attachments,
            relief="flat",
            bd=0,
            padx=14,
            pady=9,
            font=("Microsoft YaHei UI", 9),
            bg=COLORS["panel_alt"],
            fg=COLORS["muted"],
            activebackground=COLORS["green_soft"],
            activeforeground=COLORS["ink"],
            cursor="hand2",
        ).pack(side="left", padx=(10, 0))
        self.submit_button = self._action_button(attach_row, "提交到本地队列", self.submit)
        self.submit_button.pack(side="left", padx=(10, 0))

    def _build_update(self) -> None:
        page = self._new_page("update")
        self._heading(
            page,
            "给已有记录追加描述",
            "根据记录 UID 向 MatElab 中的已有记录新增一段富文本说明。",
        )
        form = self._card(page, fill="both", expand=True)
        target_row = tk.Frame(form, bg=COLORS["panel"])
        target_row.pack(fill="x")
        tk.Label(
            target_row,
            text="记录所在的记录本",
            font=("Microsoft YaHei UI", 9, "bold"),
            fg=COLORS["ink"],
            bg=COLORS["panel"],
        ).pack(side="left")
        tk.Button(
            target_row,
            text="刷新记录本",
            command=self.refresh_notebooks,
            relief="flat",
            bd=0,
            fg=COLORS["green"],
            bg=COLORS["panel"],
            activebackground=COLORS["panel"],
            cursor="hand2",
        ).pack(side="right")
        self.update_notebook_value = tk.StringVar()
        self.update_notebook_combo = ttk.Combobox(
            form,
            textvariable=self.update_notebook_value,
            state="readonly",
            style="Connector.TCombobox",
        )
        self.update_notebook_combo.pack(fill="x", pady=(6, 14), ipady=2)

        self.update_uid_value = tk.StringVar()
        self.update_title_value = tk.StringVar()
        for label, variable in (
            ("记录 UID", self.update_uid_value),
            ("说明标题（可选）", self.update_title_value),
        ):
            tk.Label(
                form,
                text=label,
                font=("Microsoft YaHei UI", 9, "bold"),
                fg=COLORS["ink"],
                bg=COLORS["panel"],
            ).pack(anchor="w", pady=(2, 5))
            ttk.Entry(form, textvariable=variable, style="Connector.TEntry").pack(
                fill="x", pady=(0, 10), ipady=2
            )
        tk.Label(
            form,
            text="新增描述",
            font=("Microsoft YaHei UI", 9, "bold"),
            fg=COLORS["ink"],
            bg=COLORS["panel"],
        ).pack(anchor="w", pady=(2, 5))
        self.update_content_text = tk.Text(
            form,
            height=9,
            wrap="word",
            font=("Microsoft YaHei UI", 10),
            relief="solid",
            bd=1,
            highlightthickness=0,
            bg=COLORS["white"],
            fg=COLORS["ink"],
            insertbackground=COLORS["ink"],
            padx=10,
            pady=8,
        )
        self.update_content_text.pack(fill="both", expand=True)
        action_row = tk.Frame(form, bg=COLORS["panel"])
        action_row.pack(fill="x", pady=(12, 0))
        self.update_result = tk.StringVar(
            value="记录 UID 可从 MatElab 页面或“任务与错误”的任务详情中复制。"
        )
        tk.Label(
            action_row,
            textvariable=self.update_result,
            font=("Microsoft YaHei UI", 9),
            fg=COLORS["muted"],
            bg=COLORS["panel"],
            anchor="w",
            justify="left",
            wraplength=570,
        ).pack(side="left", fill="x", expand=True)
        self.update_button = self._action_button(
            action_row,
            "追加到 MatElab 记录",
            self.add_description,
        )
        self.update_button.pack(side="right", padx=(12, 0))

    def _build_tasks(self) -> None:
        page = self._new_page("tasks")
        header = tk.Frame(page, bg=COLORS["canvas"])
        header.pack(fill="x")
        text = tk.Frame(header, bg=COLORS["canvas"])
        text.pack(side="left")
        tk.Label(
            text,
            text="任务与错误",
            font=("Microsoft YaHei UI", 24, "bold"),
            fg=COLORS["ink"],
            bg=COLORS["canvas"],
        ).pack(anchor="w")
        tk.Label(
            text,
            text="查看每条提交的状态、错误原因与建议操作。",
            font=("Microsoft YaHei UI", 10),
            fg=COLORS["muted"],
            bg=COLORS["canvas"],
        ).pack(anchor="w", pady=(5, 20))
        self._action_button(header, "刷新", self.refresh_tasks).pack(side="right", pady=(10, 0))

        card = self._card(page, fill="both", expand=True)
        columns = ("state", "updated", "attempt", "error")
        self.task_tree = ttk.Treeview(
            card,
            columns=columns,
            show="tree headings",
            style="Connector.Treeview",
            selectmode="browse",
        )
        self.task_tree.heading("#0", text="提交编号")
        self.task_tree.heading("state", text="状态")
        self.task_tree.heading("updated", text="更新时间")
        self.task_tree.heading("attempt", text="尝试")
        self.task_tree.heading("error", text="最近错误")
        self.task_tree.column("#0", width=230, stretch=True)
        self.task_tree.column("state", width=100, stretch=False)
        self.task_tree.column("updated", width=125, stretch=False)
        self.task_tree.column("attempt", width=55, stretch=False, anchor="center")
        self.task_tree.column("error", width=260, stretch=True)
        self.task_tree.pack(fill="both", expand=True)
        self.task_tree.bind("<<TreeviewSelect>>", self._show_task_details)
        self.task_details = tk.Text(
            card,
            height=6,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
            relief="flat",
            bg=COLORS["panel_alt"],
            fg=COLORS["ink"],
            padx=10,
            pady=8,
        )
        self.task_details.pack(fill="x", pady=(12, 0))
        self.retry_button = self._action_button(card, "重试所选任务", self.retry_selected)
        self.retry_button.pack(anchor="e", pady=(10, 0))

    def _build_settings(self) -> None:
        page = self._new_page("settings")
        self._heading(page, "API 设置", "其他本地程序可通过此端口调用 Connector 上传记录。")
        card = self._card(page, fill="x")
        tk.Label(
            card,
            text="监听地址",
            font=("Microsoft YaHei UI", 9, "bold"),
            fg=COLORS["ink"],
            bg=COLORS["panel"],
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            card,
            text="127.0.0.1（仅本机访问）",
            font=("Consolas", 10),
            fg=COLORS["muted"],
            bg=COLORS["panel"],
        ).grid(row=1, column=0, sticky="w", pady=(5, 20))
        tk.Label(
            card,
            text="API 端口",
            font=("Microsoft YaHei UI", 9, "bold"),
            fg=COLORS["ink"],
            bg=COLORS["panel"],
        ).grid(row=2, column=0, sticky="w")
        self.port_value = tk.StringVar(value=str(self.app.config.port))
        ttk.Entry(card, textvariable=self.port_value, width=18, style="Connector.TEntry").grid(
            row=3, column=0, sticky="w", pady=(5, 8), ipady=2
        )
        tk.Label(
            card,
            text=(
                "允许范围 1024–65535。保存后本地 API 会立即重启；"
                "如端口被占用会自动使用后续空闲端口。"
            ),
            font=("Microsoft YaHei UI", 9),
            fg=COLORS["muted"],
            bg=COLORS["panel"],
            wraplength=650,
            justify="left",
        ).grid(row=4, column=0, sticky="w", pady=(0, 18))
        self.save_port_button = self._action_button(card, "保存并重启 API", self.save_port)
        self.save_port_button.grid(row=5, column=0, sticky="w")
        tk.Label(
            card,
            text=(
                "安全说明：默认只监听本机回环地址，不会向局域网暴露。"
                "高级 Token 配置仍可通过环境变量管理。"
            ),
            font=("Microsoft YaHei UI", 9),
            fg=COLORS["warning"],
            bg=COLORS["warning_soft"],
            padx=12,
            pady=10,
            wraplength=650,
            justify="left",
        ).grid(row=6, column=0, sticky="ew", pady=(24, 0))
        card.columnconfigure(0, weight=1)

    def activate(self, notebooks: list[dict[str, Any]]) -> None:
        self.notebooks = notebooks
        self.account_value.set(self.app.settings.matelab_username or "账号名未记录，请重新登录一次")
        self._update_notebook_combo()
        self.port_value.set(str(self.app.service.port))
        self.api_url.set(f"API  {self.app.service.base_url}")
        self.notice.set("已连接")

    def show_page(self, key: str) -> None:
        self.current_page = key
        self.pages[key].tkraise()
        for name, button in self.nav_buttons.items():
            active = name == key
            button.configure(
                bg="#285642" if active else "#122D23",
                fg=COLORS["white"] if active else "#C8D8D1",
            )
        if self.app.client is None:
            return
        if key == "tasks":
            self.refresh_tasks()
        elif key == "overview":
            self.refresh_summary()

    def _update_notebook_combo(self) -> None:
        editable = [item for item in self.notebooks if item.get("editable")]
        values = [f"{item['name']}  ·  {item.get('access', 'accessible')}" for item in editable]
        self.notebook_combo.configure(values=values)
        self.update_notebook_combo.configure(values=values)
        default_index = next(
            (
                index
                for index, item in enumerate(editable)
                if item["id"] == self.app.settings.default_notebook_id
                and item["name"] == self.app.settings.default_notebook_name
            ),
            None,
        )
        if default_index is not None:
            self.notebook_combo.current(default_index)
            self.update_notebook_combo.current(default_index)
        elif values and self.notebook_value.get() not in values:
            self.notebook_combo.current(0)
        if values and self.update_notebook_value.get() not in values:
            self.update_notebook_combo.current(0)
        if not values:
            self.notebook_value.set("")
            self.update_notebook_value.set("")
        self._update_default_notebook_status(default_index is not None)

    def _update_default_notebook_status(self, available: bool) -> None:
        name = self.app.settings.default_notebook_name
        if available and name:
            self.default_notebook_status.set(f"API 默认：{name}")
            self.default_notebook_label.configure(fg=COLORS["green"])
        elif self.app.settings.default_notebook_id:
            self.default_notebook_status.set("已保存的 API 默认记录本当前不可用，请重新设置")
            self.default_notebook_label.configure(fg=COLORS["warning"])
        else:
            self.default_notebook_status.set("尚未设置 API 默认记录本")
            self.default_notebook_label.configure(fg=COLORS["muted"])

    def selected_notebook(self) -> dict[str, Any] | None:
        index = self.notebook_combo.current()
        editable = [item for item in self.notebooks if item.get("editable")]
        if 0 <= index < len(editable):
            return editable[index]
        return None

    def selected_update_notebook(self) -> dict[str, Any] | None:
        index = self.update_notebook_combo.current()
        editable = [item for item in self.notebooks if item.get("editable")]
        if 0 <= index < len(editable):
            return editable[index]
        return None

    def refresh_all(self) -> None:
        self.api_url.set(f"API  {self.app.service.base_url}")
        self.refresh_summary()
        self.refresh_tasks()

    def refresh_summary(self) -> None:
        def success(summary: dict[str, Any]) -> None:
            session = summary.get("matelab", {})
            self.auth_state.set("已登录" if session.get("authenticated") else "需要登录")
            states = summary.get("queue", {}).get("states", {})
            total = sum(int(value) for value in states.values())
            complete = int(states.get("complete", 0))
            attention = sum(
                int(states.get(name, 0))
                for name in ("auth_required", "needs_attention", "abandoned")
            )
            pending = max(0, total - complete - int(states.get("abandoned", 0)))
            self.queue_summary.set(
                f"共 {total} 条任务   ·   已完成 {complete}   ·   "
                f"处理中/等待 {pending}   ·   需处理 {attention}"
            )

        self.app._run(self.app.require_client().summary, success, self._quiet_error)

    def refresh_notebooks(self) -> None:
        self.notice.set("正在刷新记录本…")

        def success(notebooks: list[dict[str, Any]]) -> None:
            self.notebooks = notebooks
            self._update_notebook_combo()
            editable = sum(1 for item in notebooks if item.get("editable"))
            readonly = len(notebooks) - editable
            self.notice.set(f"已读取 {editable} 个可写记录本、{readonly} 个只读记录本")

        self.app._run(self.app.require_client().notebooks, success, self._show_operation_error)

    def save_default_notebook(self) -> None:
        notebook = self.selected_notebook()
        if notebook is None:
            self._show_operation_error(ValueError("请先选择一个可写记录本。"))
            return
        self.save_default_button.configure(state="disabled", text="正在保存…")
        self.notice.set("正在验证并保存 API 默认记录本…")

        def success(result: dict[str, Any]) -> None:
            saved = dict(result["notebook"])
            self.app.settings = replace(
                self.app.settings,
                default_notebook_id=str(saved["id"]),
                default_notebook_name=str(saved["name"]),
            )
            self.save_default_button.configure(state="normal", text="设为 API 默认记录本")
            self._update_default_notebook_status(True)
            self.notice.set(f"API 默认记录本已设为：{saved['name']}")

        def failure(exc: BaseException) -> None:
            self.save_default_button.configure(state="normal", text="设为 API 默认记录本")
            self._show_operation_error(exc)

        self.app._run(
            lambda: self.app.require_client().set_default_notebook(notebook),
            success,
            failure,
        )

    def choose_attachments(self) -> None:
        values = filedialog.askopenfilenames(title="选择记录附件", parent=self)
        if not values:
            return
        self._add_attachment_paths(values, source="文件选择器")

    def clear_attachments(self) -> None:
        self.attachments = []
        self._update_attachment_label()
        self.notice.set("已清空待提交附件")

    def _update_attachment_label(self) -> None:
        if not self.attachments:
            self.attachment_text.set(
                "把文件拖到这里，或在资源管理器复制文件后按 Ctrl+V\n"
                "也可以点击此区域或下方按钮选择文件"
            )
            return
        names = "、".join(path.name for path in self.attachments[:4])
        suffix = "…" if len(self.attachments) > 4 else ""
        self.attachment_text.set(f"已选择 {len(self.attachments)} 个附件\n{names}{suffix}")

    def _add_attachment_paths(self, values: Iterable[str | Path], *, source: str) -> int:
        existing = {os.path.normcase(str(path)) for path in self.attachments}
        added = 0
        ignored = 0
        for value in values:
            try:
                path = Path(str(value)).expanduser().resolve()
            except (OSError, RuntimeError):
                ignored += 1
                continue
            key = os.path.normcase(str(path))
            if not path.is_file() or key in existing:
                ignored += 1
                continue
            self.attachments.append(path)
            existing.add(key)
            added += 1
        self._update_attachment_label()
        if added:
            detail = f"，忽略 {ignored} 个目录、无效路径或重复文件" if ignored else ""
            self.notice.set(f"通过{source}加入 {added} 个附件{detail}")
        elif ignored:
            self.notice.set("没有加入文件：目录、无效路径和重复文件会被忽略")
        return added

    def _attachment_drag_enter(self, _event: Any) -> str:
        self.attachment_label.configure(bg=COLORS["green_soft"], fg=COLORS["green"])
        return str(COPY)

    def _attachment_drag_leave(self, _event: Any) -> str:
        self.attachment_label.configure(bg=COLORS["panel_alt"], fg=COLORS["muted"])
        return str(COPY)

    def _drop_attachments(self, event: Any) -> str:
        self.attachment_label.configure(bg=COLORS["panel_alt"], fg=COLORS["muted"])
        values = self.tk.splitlist(str(event.data))
        self._add_attachment_paths(values, source="拖放")
        return str(COPY)

    def _paste_attachments(self, _event: Any) -> str | None:
        if self.current_page != "upload":
            return None
        paths = _windows_clipboard_files()
        if not paths:
            try:
                clipboard_text = self.clipboard_get().strip()
            except tk.TclError:
                return None
            try:
                values = self.tk.splitlist(clipboard_text)
            except tk.TclError:
                values = (clipboard_text,)
            paths = [Path(value) for value in values if Path(value).is_file()]
        if not paths:
            return None
        self._add_attachment_paths(paths, source="剪贴板")
        return "break"

    def submit(self) -> None:
        notebook = self.selected_notebook()
        title = self.title_value.get().strip()
        content = self.content_text.get("1.0", "end-1c").strip()
        if notebook is None:
            self._show_operation_error(ValueError("请先选择一个可写记录本。"))
            return
        if not title or not content:
            self._show_operation_error(ValueError("标题和记录内容不能为空。"))
            return
        attachments = list(self.attachments)
        self.submit_button.configure(state="disabled", text="正在接收…")
        self.notice.set("正在把记录安全写入本地队列…")

        def operation() -> dict[str, Any]:
            return self.app.require_client().submit_note(
                notebook=notebook,
                title=title,
                content=content,
                actor_id=self.actor_value.get().strip() or None,
                attachments=attachments,
            )

        def success(result: dict[str, Any]) -> None:
            self.submit_button.configure(state="normal", text="提交到本地队列")
            self.title_value.set("")
            self.content_text.delete("1.0", "end")
            self.attachments = []
            self._update_attachment_label()
            self.notice.set(f"本机已接收：{result['capture_id']}，后台正在同步")
            self.show_page("tasks")

        def failure(exc: BaseException) -> None:
            self.submit_button.configure(state="normal", text="提交到本地队列")
            self._show_operation_error(exc)

        self.app._run(operation, success, failure)

    def add_description(self) -> None:
        notebook = self.selected_update_notebook()
        record_uid = self.update_uid_value.get().strip()
        title = self.update_title_value.get().strip()
        content = self.update_content_text.get("1.0", "end-1c").strip()
        if notebook is None:
            self._show_operation_error(ValueError("请先选择记录所在的可写记录本。"))
            return
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", record_uid):
            self._show_operation_error(
                ValueError("请输入有效的记录 UID，只能使用字母、数字、点、下划线、冒号和连字符。")
            )
            return
        if len(title) > 120:
            self._show_operation_error(ValueError("说明标题不能超过 120 个字符。"))
            return
        if not content:
            self._show_operation_error(ValueError("新增描述不能为空。"))
            return
        if len(content) > 100_000:
            self._show_operation_error(ValueError("新增描述不能超过 100000 个字符。"))
            return
        self.update_button.configure(state="disabled", text="正在更新…")
        self.update_result.set("正在验证记录并提交到 MatElab…")
        self.notice.set(f"正在更新记录 {record_uid}…")

        def operation() -> dict[str, Any]:
            return self.app.require_client().add_record_description(
                notebook=notebook,
                record_uid=record_uid,
                title=title or None,
                content=content,
            )

        def success(result: dict[str, Any]) -> None:
            self.update_button.configure(state="normal", text="追加到 MatElab 记录")
            self.update_title_value.set("")
            self.update_content_text.delete("1.0", "end")
            self.update_result.set(
                f"更新成功 · 新增模块：{result['module_name']} · 事件游标：{result['event_cursor']}"
            )
            self.notice.set(f"记录 {record_uid} 已追加描述")

        def failure(exc: BaseException) -> None:
            self.update_button.configure(state="normal", text="追加到 MatElab 记录")
            self.update_result.set("更新失败，请根据错误提示检查记录本和记录 UID。")
            self._show_operation_error(exc)

        self.app._run(operation, success, failure)

    def refresh_tasks(self) -> None:
        def success(tasks: list[dict[str, Any]]) -> None:
            selected = self.task_tree.selection()
            selected_id = selected[0] if selected else None
            self.tasks_data = {str(item["capture_id"]): item for item in tasks}
            self.task_tree.delete(*self.task_tree.get_children())
            for item in tasks:
                capture_id = str(item["capture_id"])
                error = item.get("last_error") or {}
                self.task_tree.insert(
                    "",
                    "end",
                    iid=capture_id,
                    text=capture_id,
                    values=(
                        STATE_LABELS.get(str(item.get("state")), str(item.get("state"))),
                        _human_time(item.get("updated_at")),
                        item.get("attempt", 0),
                        error.get("message", "") if isinstance(error, dict) else str(error),
                    ),
                )
            if selected_id in self.tasks_data:
                self.task_tree.selection_set(selected_id)

        self.app._run(self.app.require_client().tasks, success, self._quiet_error)

    def _show_task_details(self, _event: object | None = None) -> None:
        selected = self.task_tree.selection()
        if not selected:
            return
        task = self.tasks_data.get(selected[0], {})
        error = task.get("last_error")
        details = [
            f"提交编号：{task.get('capture_id', '—')}",
            f"同步编号：{task.get('sync_id', '—')}",
            f"状态：{STATE_LABELS.get(str(task.get('state')), str(task.get('state', '—')))}",
        ]
        reference = task.get("matelab_ref")
        if isinstance(reference, dict):
            details.append(f"MatElab 记录：{reference.get('record_uid', '—')}")
        if error:
            details.extend(("", "错误详情：", json.dumps(error, ensure_ascii=False, indent=2)))
        self.task_details.configure(state="normal")
        self.task_details.delete("1.0", "end")
        self.task_details.insert("1.0", "\n".join(details))
        self.task_details.configure(state="disabled")

    def retry_selected(self) -> None:
        selected = self.task_tree.selection()
        if not selected:
            self._show_operation_error(ValueError("请先在列表中选择一条任务。"))
            return
        capture_id = selected[0]
        self.notice.set(f"正在重试 {capture_id}…")

        def success(_result: dict[str, Any]) -> None:
            self.notice.set(f"已请求重试：{capture_id}")
            self.refresh_all()

        self.app._run(
            lambda: self.app.require_client().retry(capture_id),
            success,
            self._show_operation_error,
        )

    def save_port(self) -> None:
        try:
            port = int(self.port_value.get())
        except ValueError:
            self._show_operation_error(ValueError("API 端口必须是数字。"))
            return
        if not 1024 <= port <= 65535:
            self._show_operation_error(ValueError("API 端口必须在 1024–65535 之间。"))
            return
        if port == self.app.service.port:
            self.app.settings = replace(self.app.settings, api_port=port)
            self.app.settings.save(self.app.config.data_dir)
            self.notice.set("API 端口设置已保存")
            return
        if not self.app.service.owned:
            self._show_operation_error(
                RuntimeError("当前 API 由另一个 Connector 进程运行，请关闭另一个实例后再修改端口。")
            )
            return
        self.save_port_button.configure(state="disabled", text="正在重启…")
        old_client = self.app.require_client()
        new_config = replace(self.app.config, port=port)

        def operation() -> tuple[NativeConnectorClient, int, DesktopSettings]:
            old_client.close()
            self.app.service.restart(new_config)
            client = NativeConnectorClient(self.app.service.base_url, self.app.service.config)
            settings = replace(self.app.settings, api_port=port)
            settings.save(new_config.data_dir)
            return client, self.app.service.port, settings

        def success(result: tuple[NativeConnectorClient, int, DesktopSettings]) -> None:
            client, actual_port, settings = result
            self.app.client = client
            self.app.config = self.app.service.config
            self.app.settings = settings
            self.save_port_button.configure(state="normal", text="保存并重启 API")
            self.port_value.set(str(actual_port))
            self.api_url.set(f"API  {self.app.service.base_url}")
            if actual_port == port:
                self.notice.set(f"API 已切换到端口 {port}")
            else:
                self.notice.set(f"端口 {port} 被占用，API 已改用 {actual_port}")
            self.refresh_all()

        def failure(exc: BaseException) -> None:
            self.save_port_button.configure(state="normal", text="保存并重启 API")
            self._show_operation_error(exc)

        self.app._run(operation, success, failure)

    def logout(self) -> None:
        def success(_value: object) -> None:
            self.notebooks = []
            self.account_value.set("未登录")
            self.app.settings = replace(
                self.app.settings,
                matelab_username=None,
            )
            self.app.settings.save(self.app.config.data_dir)
            self.app.login_page.password.set("")
            self.app.login_page.error.set("")
            self.app.login_page.set_status(f"本地 API 已启动：{self.app.service.base_url}")
            self.app.show_login()

        self.app._run(self.app.require_client().logout, success, self._show_operation_error)

    def open_docs(self) -> None:
        webbrowser.open(f"{self.app.service.base_url}/docs")

    def copy_api_url(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.app.service.base_url)
        self.notice.set("API 地址已复制")

    def open_data_dir(self) -> None:
        path = self.app.config.data_dir.resolve()
        path.mkdir(parents=True, exist_ok=True)
        if platform.system() == "Windows":
            os.startfile(path)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def _show_operation_error(self, exc: BaseException) -> None:
        self.notice.set("操作失败")
        self.app.show_error(exc)

    def _quiet_error(self, exc: BaseException) -> None:
        self.notice.set(f"刷新失败：{exc!s}")


def main() -> None:
    if platform.system() == "Windows":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "cn.ac.iphy.matelab.connector"
            )
        except (AttributeError, OSError):
            pass
    try:
        root = TkinterDnD.Tk()
    except (RuntimeError, tk.TclError) as exc:
        print(
            f"MatElab Connector could not initialize its native interface: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    NativeConnectorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
