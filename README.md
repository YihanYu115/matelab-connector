# MatElab Desktop Connector

> 本项目是社区开发的非官方 MatElab 客户端，与 MatElab 官方或中国科学院物理研究所
> 不构成官方授权、背书或隶属关系。请仅在你有权访问的账号和记录本上使用。

这是一个带原生中文控制台的本地 MatElab 连接器。它可以：

- 登录自己的 MatElab 账号，并自动安全保存短期 Token；密码不会落盘；
- 把登录页和功能控制台分开，显示当前账号可以访问的记录本；
- 手动填写标题、正文和附件，一键提交到所选记录本；
- 在可设置的 `127.0.0.1` 端口向其他本地程序提供 HTTP API；
- 在断网、登录过期或程序重启后保留任务，并给出明确的错误原因和处理建议。

## 直接使用原生应用（推荐）

普通用户只需要下载对应系统的发布包：

- Windows: 解压后双击 `MatElabConnector.exe`；
- macOS: 解压后把 `MatElab Connector.app` 拖入“应用程序”并打开。

两种版本都不要求用户安装 Python 或拉取源码。打开后先看到独立登录页；登录后进入 Connector 控制台，可查看 API 状态、手动提交、任务/错误和端口设置。默认端口为 `8765`；如被占用会安全选用后续空闲端口，控制台显示实际地址。同一用户只能运行一个原生 Connector；重复双击时会提示使用已有窗口并退出第二个实例。

登录页的“在默认浏览器登录 / 找回密码”可以打开 MatElab 官方网站，使用浏览器保存的账号或找回密码。受 MatElab 现有认证接口限制，网页登录暂时不能自动把 ELN API Token 回传给 Connector；实际授权仍需在应用里输入一次账号密码，密码不会保存。技术边界见 [原生应用构建与分发](docs/native-distribution.md)。

源码目录中的 `start-matelab-bridge.cmd` 会优先打开已构建的 EXE，只在开发环境没有 EXE 时创建 Python 环境。开发者也可运行：

```powershell
.\.venv\Scripts\matelab-connector.exe
```

使用顺序是：登录 → 进入控制台 → 选择目标记录本 → 填写记录/添加附件 → 提交。任务页会持续显示“本机已接收、上传附件、创建记录、回读核验、完成”等阶段和详细错误。

## 供其他程序调用

连接器启动时，本地 API 默认为 `http://127.0.0.1:8765`，实际地址以控制台为准：

- 交互式 API 文档：`http://127.0.0.1:8765/docs`
- 健康检查：`GET /healthz`
- 简单的正文/附件上传：`POST /v1/manual-submissions`（multipart）
- 创建结构化记录：`POST /v1/captures`
- 上传附件：`PUT /v1/captures/{capture_id}/artifacts/{artifact_id}`
- 提交同步：`POST /v1/captures/{capture_id}/commit`
- 查询回执：`GET /v1/captures/{capture_id}`

完整错误格式与调用方处理规则见 [错误处理规范](docs/error-handling.md)。默认只允许本机访问；如需监听局域网，必须在 `.env` 中启用三种彼此不同的作用域 Token。

自动调用 `POST /v1/manual-submissions` 时应为一次逻辑提交生成 `Idempotency-Key` 请求头，并在超时重试时原样复用。同一个 Key 和完全相同的字段/附件会返回原 `capture_id`；同一个 Key 携带不同内容会返回 HTTP 409。没有该请求头时，每次调用都视为一条新记录。

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

本仓库实现了原生桌面控制台、可靠本地队列、外部 API 和基础运维命令。浏览器版 `/ui` 仅为向后兼容保留。真实账号、隔离记录本、历史版本/协作编辑/权限行为仍需在 MatElab 测试环境执行 Gate 0；项目不会把文档未保证的行为冒充为已验证能力。

## 安全与许可证

默认本地 API 仅监听 `127.0.0.1`，面向单台电脑上的可信用户和可信程序。不要直接暴露到局域网或公网；完整边界和漏洞报告方式见 [SECURITY.md](SECURITY.md)。项目采用 [MIT License](LICENSE)，打包程序包含的依赖声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
