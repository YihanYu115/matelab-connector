# MatElab Desktop Connector

这是一个带中文操作界面的本地 MatElab 连接器。它可以：

- 登录自己的 MatElab 账号，并自动安全保存短期 Token；密码不会落盘；
- 显示当前账号可以访问的记录本；
- 手动填写标题、正文和附件，一键提交到所选记录本；
- 在 `127.0.0.1:8765` 向其他本地程序提供 HTTP API；
- 在断网、登录过期或程序重启后保留任务，并给出明确的错误原因和处理建议。

## 直接使用 GUI

首次运行要求 Windows 已安装 Python 3.11 或更新版本。双击项目根目录里的：

**`start-matelab-bridge.cmd`**

首次启动会自动安装依赖，随后浏览器会打开 GUI。默认地址为 `http://127.0.0.1:8765/ui`；如果该端口已被 Manager 等其他程序占用，启动器会自动选择后续空闲端口并打开正确地址。之后仍然双击同一个文件即可。

也可以在 PowerShell 中启动：

```powershell
.\.venv\Scripts\matelab-bridge.exe gui
```

界面中的使用顺序就是：登录 → 选择目标记录本 → 填写记录/添加附件 → 上传。提交后不要立即关闭启动窗口；界面会显示“本机已接收、上传附件、创建记录、回读核验、完成”五个阶段。

## 供其他程序调用

连接器启动时，本地 API 与 GUI 共用 `http://127.0.0.1:8765`：

- 交互式 API 文档：`http://127.0.0.1:8765/docs`
- 健康检查：`GET /healthz`
- 简单的正文/附件上传：`POST /v1/manual-submissions`（multipart）
- 创建结构化记录：`POST /v1/captures`
- 上传附件：`PUT /v1/captures/{capture_id}/artifacts/{artifact_id}`
- 提交同步：`POST /v1/captures/{capture_id}/commit`
- 查询回执：`GET /v1/captures/{capture_id}`

完整错误格式与调用方处理规则见 [错误处理规范](docs/error-handling.md)。默认只允许本机访问；如需监听局域网，必须在 `.env` 中启用三种彼此不同的作用域 Token。

## 开发和离线验证

开发环境可使用内置 fake MatElab：

```powershell
matelab-bridge fake-matelab --port 8766
$env:MATELAB_BRIDGE_MATELAB_URL = "http://127.0.0.1:8766"
matelab-bridge auth set-token  # 在隐藏输入提示中填写 dev-access / dev-refresh
matelab-bridge gui
```

另一个终端提交示例：

```powershell
matelab-bridge capture submit .\examples\experiment.json
matelab-bridge analysis submit .\examples\analysis-bundle.json `
  -a analysis-script=.\examples\artifacts\analysis.py `
  -a analysis-result=.\examples\artifacts\analysis-result.json
matelab-bridge note "放大器没开，打开后信号恢复"
matelab-bridge doctor
```

HTTP 契约、配置、安全边界及真实 MatElab 上线前检查见 [运行手册](docs/operations.md) 与 [MatElab 契约说明](docs/matelab-contract.md)。

## 当前交付边界

本仓库实现了可运行的桌面 GUI、可靠本地队列、外部 API 和基础运维命令。真实账号、隔离记录本、历史版本/协作编辑/权限行为仍需在 MatElab 测试环境执行 Gate 0；项目不会把文档未保证的行为冒充为已验证能力。
