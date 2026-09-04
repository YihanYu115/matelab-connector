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

## 给已有记录追加描述

调用 `POST /v1/records/{record_uid}/descriptions`。`record_uid` 可从同步完成回执的
`matelab_ref.record_uid` 取得。请求体是 JSON：

```json
{
  "title": "复测说明",
  "content": "更换衰减器后复测，峰位不变。",
  "notebook_id": "记录本稳定 ID",
  "notebook_name": "自动实验记录"
}
```

`content` 必填，长度为 1–100000 个字符；`title` 可选。`notebook_id` 与
`notebook_name` 必须同时提供；两者都省略时使用 API 默认记录本。每次逻辑追加都应发送一个新的 `Idempotency-Key`，网络超时重试时复用原值：

```python
from uuid import uuid4

import httpx

response = httpx.post(
    "http://127.0.0.1:8765/v1/records/RECORD_UID/descriptions",
    headers={"Idempotency-Key": str(uuid4())},
    json={
        "title": "复测说明",
        "content": "更换衰减器后复测，峰位不变。",
    },
    timeout=60,
)
response.raise_for_status()
print(response.json())
```

Connector 会把描述写成一个名称稳定的 MatElab `richtext` 模块。同一幂等键和相同内容返回原回执；同一键换内容返回 `409 idempotency_conflict`。成功状态
`matelab_acknowledged` 表示 MatElab 更新接口已确认请求。根据 MatElab 官方文档，如果另一位用户正在协同编辑同一记录，内容会进入当前编辑版本，需要编辑者手动保存；因此该状态不承诺协同编辑者尚未保存的内容已经落入最终版本。

## 监听记录更新

事件是先写入 SQLite、再返回给监听方的持久事件。当前事件类型：

| 类型 | 触发时机 |
| --- | --- |
| `matelab.record.synced` | 新记录已上传、导出回读通过并保存本地映射 |
| `matelab.record.description_added` | 补充描述已被 MatElab 更新接口接受 |

先用普通 JSON 接口补读：

```text
GET /v1/events?after=0&limit=100
GET /v1/events?after=42&type=matelab.record.synced
```

响应中的 `next_cursor` 是下一次请求的 `after`：

```json
{
  "events": [
    {
      "cursor": 42,
      "id": "evt-...",
      "type": "matelab.record.synced",
      "occurred_at": "2026-09-04T13:00:00+00:00",
      "subject": "capture:capture-...",
      "data": {
        "capture_id": "capture-...",
        "sync_id": "sync-...",
        "capture_kind": "experiment_run",
        "manifest_sha256": "sha256:...",
        "matelab_ref": {}
      }
    }
  ],
  "next_cursor": 42
}
```

实时监听使用 Server-Sent Events：

```powershell
curl.exe -N "http://127.0.0.1:8765/v1/events/stream?after=42"
```

SSE 帧的 `id` 是数字游标。监听方应只在自己的业务处理成功后保存该游标；断线时用
`after=<已保存游标>` 重连，也可发送标准 `Last-Event-ID` 请求头。流会先重放遗漏事件，再等待新事件，并定期发送注释心跳。`type` 查询参数可以重复出现以监听多种事件。Token 模式下，这两个读取接口都要求状态作用域的 `X-Bridge-Token`。

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
