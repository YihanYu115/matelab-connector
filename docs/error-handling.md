# MatElab Desktop Connector 错误处理规范

## 统一错误格式

GUI 和本地 HTTP API 的失败响应使用同一结构：

```json
{
  "error": {
    "code": "matelab_auth_required",
    "message": "MatElab 登录已失效或账号密码不正确。",
    "retryable": false,
    "action": "请在 GUI 中重新登录 MatElab。",
    "request_id": "req-...",
    "details": {"reason": "..."}
  }
}
```

- `code`：供程序稳定判断的机器错误码。
- `message`：可以直接展示给人的中文说明。
- `retryable`：调用方现在重试是否可能成功。后台队列的自动重试不要求调用方再次上传原文件。
- `action`：用户或调用方下一步应做什么。
- `request_id`：诊断和日志关联编号。调用方可用 `X-Request-ID` 提供合法的 ASCII 编号。
- `details`：安全清理后的补充信息，不包含密码和 Token。

## HTTP 状态与处理动作

其他本地程序若只需上传标题、正文和附件，可调用 `POST /v1/manual-submissions`；需要完整实验语义和幂等控制时使用三步 Capture API。

| HTTP | `code` | 含义 | 规定动作 |
| --- | --- | --- | --- |
| 400 | `invalid_json` | JSON 编码、BOM、NaN 或语法不合法 | 修正请求，不重试原请求 |
| 401 | `local_auth_required` | 局域网 API Token 缺失或错误 | 使用该接口要求的作用域 Token |
| 401 | `matelab_auth_required` | 未登录、密码错误、Access/Refresh Token 失效 | GUI 重新登录；已落盘任务保留并在登录成功后自动恢复 |
| 403 | `http_error` | GUI 会话校验失败 | 刷新 GUI 后重新操作 |
| 404 | `capture_not_found` | 本机没有该提交编号 | 检查 `capture_id` |
| 404 | `notebook_not_found` | 记录本被改名、删除或权限已变化 | 刷新记录本并重新选择；不得猜测或回退到默认记录本 |
| 409 | `capture_identity_conflict` | 同一 `capture_id` 对应不同内容 | 为新内容生成新的 `capture_id` |
| 409 | `artifact_conflict` | 附件策略或状态冲突 | 核对 manifest 与附件编号 |
| 409 | `invalid_state` | 当前同步状态不接受该操作 | 先查询回执；失败任务使用维护重试接口 |
| 409 | `matelab_conflict` | 远端 UID、导出内容或附件哈希不一致 | 停止自动覆盖，人工检查 MatElab 记录 |
| 413 | `payload_too_large` | 清单、单附件或本次附件总量超限 | 缩小或拆分提交 |
| 422 | `request_validation_error` | HTTP 字段缺失或格式错误 | 按 `details` 修正字段 |
| 422 | `schema_validation_error` | Capture Envelope 语义不符合协议 | 按 `details` 修正清单 |
| 422 | `artifact_hash_mismatch` | 实际附件 SHA-256 与声明不同 | 重新读取原文件并重新创建提交 |
| 422 | `artifact_size_mismatch` | 实际附件大小与声明不同 | 重新读取原文件并重新创建提交 |
| 502 | `matelab_contract_error` | MatElab 拒绝参数或响应格式与文档不符 | 刷新记录本后重试；持续失败生成诊断包 |
| 503 | `matelab_retryable` | 网络超时、MatElab 5xx 或限流 | 不重复创建 capture；查询原回执并等待后台重试 |
| 500 | `internal_error` | 未分类的本地异常 | 保存 `request_id`，生成诊断包 |

## GUI 必须遵守的状态行为

1. 未登录时禁用“上传到所选记录本”，但允许填写内容。
2. 登录成功后必须从 `/eln_api/elns` 加载记录本，不能要求用户手填记录本名称。
3. 提交前再次读取记录本列表，并同时校验记录本稳定 ID 与名称。
4. 浏览器收到 `202` 表示本机已经可靠接收，不表示 MatElab 已完成。必须轮询回执直到 `complete`、`auth_required` 或 `needs_attention`。
5. `retry_wait` 时保留本机数据并显示自动重试，不要求用户重新选择附件。
6. `auth_required` 时保留本机数据；再次登录成功后，连接器将所有仅因认证暂停的任务恢复为 `ready`。
7. `needs_attention` 和 `matelab_conflict` 不得自动覆盖远端内容。

## 凭证与日志

- GUI 密码只存在于登录请求内存中，登录返回后立即从密码框清除，不写数据库、日志或诊断包。
- Access Token 和 Refresh Token 只存入操作系统凭证库。
- API 错误不得回显 Token、密码或完整敏感请求体。
- MatElab 数字签名密码不属于 Connector 的输入范围，Connector 不自动调用定稿接口。
