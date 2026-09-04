# 原生应用构建与分发

## 用户拿到什么

- Windows: `MatElabConnector.exe`，双击即可运行，不需要安装 Python。
- macOS: `MatElab Connector.app`，分别提供 Apple Silicon 和 Intel 版本，作为标准应用程序运行，不需要安装 Python。

应用会启动原生窗口，并在后台启动仅监听 `127.0.0.1` 的 HTTP API。数据、任务队列和端口设置存放在操作系统的用户应用数据目录；MatElab Token 由系统凭据库保存，密码不落盘。

## 构建

PyInstaller 不是交叉编译器，因此 Windows 版本必须在 Windows 构建，macOS 版本必须在 macOS 构建。仓库的 GitHub Actions 工作流会同时构建两种产物：

```powershell
python -m pip install ".[build]"
python tools/build_native.py
```

Windows 本地产物是 `dist/MatElabConnector.exe`。macOS 本地产物是 `dist/MatElab Connector.app`。

## 签名说明

当前自动产物未进行 Windows Authenticode 签名，也未进行 Apple Developer ID 签名和公证。内部试用时，系统可能显示“未知发布者”或 Gatekeeper 提示。正式向全所分发前，应在 CI 中配置机构证书并完成签名；不要通过关闭系统安全功能绕过提示。

## 浏览器登录的边界

桌面登录页提供“在默认浏览器登录 / 找回密码”按钮，方便使用浏览器中保存的密码、统一登录入口或密码找回功能。

但是，根据 MatElab 当前公开接口：

- ELN API 的 Access/Refresh Token 通过账号密码调用 `/tokens` 获取；
- 第三方网页二维码登录需要为系统预先分配固定 `key`，结果只包含用户身份信息，并不返回 ELN API Token。

因此浏览器中的登录状态目前不能安全地自动回传给 Connector。应用不会抓取浏览器 Cookie，也不会代理或保存密码。要实现真正的一键浏览器授权，MatElab 服务端需要提供面向桌面客户端的 OAuth 2.0 Authorization Code + PKCE（或等价）流程，至少包括：

1. 注册 Connector 的客户端标识和允许的 loopback callback URI；
2. 浏览器授权端点返回一次性 authorization code；
3. Token 端点用 code + PKCE verifier 换取 ELN Access/Refresh Token；
4. 明确 Token scope、有效期、撤销和错误契约。

在该契约可用前，Connector 中实际授权 API 的方式仍是输入一次 MatElab 账号密码；密码只参与本次 `/tokens` 请求，获得的 Token 交给操作系统凭据库保管。

参考：[MatElab API 账号认证](https://matelab.iphy.ac.cn/doc_detail?id=24155)、[MatElab 第三方登录说明](https://matelab.iphy.ac.cn/doc_detail?id=24134)。
