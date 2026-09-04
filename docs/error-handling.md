# MatElab Desktop Connector 错误处理规范

## 统一错误格式

原生控制台会把本地 HTTP API 的失败按同一结构展示：

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
| 403 | `notebook_not_writable` | 选择了公开或只读记录本 | 选择自己的记录本，或申请共享记录本的编辑权限 |
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

## 原生控制台必须遵守的状态行为

1. 未登录时停留在独立登录页，不能进入手动提交页。
2. “在默认浏览器登录 / 找回密码”只打开 MatElab 官方页面，不得声称网页登录已经授权 Connector；在 MatElab 提供桌面授权回调前，API 授权仍使用应用内账号密码登录。
3. 登录成功后必须从 `/eln_api/elns` 加载记录本，不能要求用户手填记录本名称。
4. 提交前再次读取记录本列表，并同时校验记录本稳定 ID 与名称。
5. 控制台收到 `202` 表示本机已经可靠接收，不表示 MatElab 已完成。任务页必须继续显示回执，直到 `complete`、`auth_required` 或 `needs_attention`。
6. `retry_wait` 时保留本机数据并显示自动重试，不要求用户重新选择附件。
7. `auth_required` 时保留本机数据；再次登录成功后，连接器将所有仅因认证暂停的任务恢复为 `ready`。
8. `needs_attention` 和 `matelab_conflict` 不得自动覆盖远端内容。

## 桌面启动与端口错误

| 场景 | 控制台行为 | 用户动作 |
| --- | --- | --- |
| 设定端口被 Manager 等程序占用 | 在设定端口之后最多探测 19 个端口，使用第一个空闲端口并显示实际 API 地址 | 通常无需处理；需要固定端口时在“API 设置”修改 |
| 检测到旧版 Connector | 拒绝并行启动第二个同步 Worker，登录页显示旧实例所在端口 | 关闭旧窗口或旧 CMD，再启动新版应用 |
| 20 个候选端口都不可用 | 不进入功能控制台，显示启动失败和建议 | 释放端口或修改 `desktop-settings.json` 后重启 |
| 用户输入的端口不是 1024–65535 | 不保存、不重启 API | 修正端口值 |
| API 重启失败 | 保留错误文本并提示重启应用 | 记录错误信息，关闭占用端口的程序后重启 |

## 凭证与日志

- 控制台密码只存在于登录请求内存中，登录返回后立即从密码框清除，不写数据库、日志或诊断包。
- Access Token 和 Refresh Token 只存入操作系统凭证库。
- API 错误不得回显 Token、密码或完整敏感请求体。
- MatElab 数字签名密码不属于 Connector 的输入范围，Connector 不自动调用定稿接口。
