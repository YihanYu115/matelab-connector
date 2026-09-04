# MatElab Connector 本地 API 快速接入

这份文档面向需要把实验记录上传到 MatElab 的本机程序。普通调用方不需要了解 MatElab 自身的登录接口，也不需要保存 MatElab 账号或密码。

## 使用前准备

1. 启动 MatElab Connector，并在原生窗口中登录。
2. 在“手动提交”页面选择一个可写记录本。
3. 点击“设为 API 默认记录本”。
4. 在控制台顶部确认实际 API 地址。下文使用 `http://127.0.0.1:8765`；如果控制台显示的是 `8766` 或其他端口，请替换它。

可以先检查服务和默认记录本：

```text
GET http://127.0.0.1:8765/healthz
GET http://127.0.0.1:8765/v1/default-notebook
```

默认记录本设置成功时，第二个接口返回：

```json
{
  "configured": true,
  "notebook": {
    "id": "记录本 ID",
    "name": "记录本名称"
  }
}
```

## 最简单的上传

调用 `POST /v1/manual-submissions`，使用 `multipart/form-data` 发送以下字段：

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `title` | 是 | 记录标题，1–255 个字符 |
| `content` | 是 | 记录正文，1–100000 个字符 |
| `attachments` | 否 | 附件；多个附件重复使用同一个字段名 |
| `actor_id` | 否 | 记录人或调用程序中的操作者标识 |
| `notebook_id` | 否 | 与 `notebook_name` 一起提供时覆盖默认记录本 |
| `notebook_name` | 否 | 与 `notebook_id` 一起提供时覆盖默认记录本 |

PowerShell 示例：

```powershell
$key = [guid]::NewGuid().ToString()
curl.exe -X POST "http://127.0.0.1:8765/v1/manual-submissions" `
  -H "Idempotency-Key: $key" `
  -F "title=自动测量完成" `
  -F "content=本轮扫描完成，数据与脚本见附件。" `
  -F "attachments=@D:\data\result.csv" `
  -F "attachments=@D:\data\analysis.py"
```

Python 示例（项目需安装 `httpx`）：

```python
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import httpx

base_url = "http://127.0.0.1:8765"
paths = [Path("result.csv"), Path("analysis.py")]

with ExitStack() as stack:
    files = [
        ("attachments", (path.name, stack.enter_context(path.open("rb"))))
        for path in paths
    ]
    response = httpx.post(
        f"{base_url}/v1/manual-submissions",
        headers={"Idempotency-Key": str(uuid4())},
        data={
            "title": "自动测量完成",
            "content": "本轮扫描完成，数据与脚本见附件。",
        },
        files=files,
        timeout=60,
    )

response.raise_for_status()
accepted = response.json()
print(accepted["capture_id"], accepted["status_url"])
```

## 正确判断是否上传完成

HTTP `202` 只表示 Connector 已把请求可靠接收到本地队列，并不表示 MatElab 已经写入完成。响应示例：

```json
{
  "capture_id": "manual-...",
  "sync_id": "sync-...",
  "state": "ready",
  "status_url": "/v1/captures/manual-...",
  "duplicate": false
}
```

调用方应轮询 `base_url + status_url`：

- `complete`：MatElab 写入和回读核验完成。
- `ready`、`uploading_files`、`creating_record`、`populating_record`、`verifying_export`、`retry_wait`：仍在处理，稍后再查。
- `auth_required`：请用户打开 Connector 重新登录；本地任务不会丢失。
- `needs_attention` 或 `abandoned`：停止自动重试，并显示响应中的 `last_error`。

建议轮询间隔为 1–3 秒，并为整个业务操作设置合理的总等待时间。

## 防止重复记录

每一次业务上的“同一条提交”都应生成一个唯一的 `Idempotency-Key`。连接超时后重试时，必须复用原来的 Key 和完全相同的正文、字段及附件。

- 相同 Key、相同内容：返回原来的 `capture_id`，`duplicate` 为 `true`。
- 相同 Key、不同内容：返回 HTTP 409 和 `capture_identity_conflict`。
- 没有 Key：每次请求都会被当成一条新记录。

当请求使用默认记录本时，Connector 会在第一次接收时固定实际目标。即使用户随后修改默认记录本，使用原 Key 重试仍会返回原任务，不会改投新记录本。

## 常见错误

所有 API 错误都使用统一结构：

```json
{
  "error": {
    "code": "default_notebook_not_configured",
    "message": "尚未设置供本地 API 使用的默认记录本。",
    "retryable": false,
    "action": "打开 Connector 的“手动提交”页面，选择记录本并点击“设为 API 默认记录本”。",
    "request_id": "req-...",
    "details": {}
  }
}
```

调用方应优先依据稳定的 `error.code` 分支处理，不要解析中文 `message`：

| HTTP | `error.code` | 处理方式 |
| --- | --- | --- |
| 401 | `matelab_auth_required` | 提醒用户在 Connector 中重新登录 |
| 409 | `default_notebook_not_configured` | 提醒用户设置 API 默认记录本 |
| 404 | `notebook_not_found` | 默认记录本可能被删除、改名或失去权限；请用户重新设置 |
| 403 | `notebook_not_writable` | 重新选择可写记录本 |
| 409 | `capture_identity_conflict` | 不要覆盖；为新内容生成新 Key |
| 503 | `matelab_retryable` | 保留原 Key，等待 Connector 后台重试并查询状态 |

完整错误表见 [错误处理规范](error-handling.md)，完整机器契约见 [OpenAPI JSON](openapi.json)。连接器运行时还可在 `http://127.0.0.1:端口/docs` 中交互式查看和试调用全部接口。

## 本地 Token 模式

默认配置只监听 `127.0.0.1`，同一台电脑上的调用无需 Token。如果管理员主动把服务开放到非回环地址，必须启用 Token 模式；提交、状态查询和维护接口使用不同作用域的 `X-Bridge-Token`。普通个人电脑不应把 Connector 直接暴露到局域网或公网。
