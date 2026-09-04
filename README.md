# MatElab Desktop Bridge

MatElab Desktop Bridge 是一个默认只监听 `127.0.0.1` 的可靠提交网关。它接收调用方已经整理好的实验、分析、推导和桌面短记录，将 manifest 与 Artifact 先可靠写入本机，再异步同步到 MatElab。

项目严格保持两条边界：它不会监听或扫描 Data Vault，也不会调用 Manager。实验语义、Git/dirty 状态、环境与文件传输策略均由调用方显式提交。

## 快速开始

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
matelab-bridge serve
```

开发环境可先使用内置 fake MatElab：

```powershell
matelab-bridge fake-matelab --port 8766
$env:MATELAB_BRIDGE_MATELAB_URL = "http://127.0.0.1:8766"
matelab-bridge auth set-token  # 在隐藏输入提示中填写 dev-access / dev-refresh
matelab-bridge serve
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

HTTP 契约、配置、安全边界及真实 MatElab 上线前检查见 [docs/operations.md](docs/operations.md) 与 [docs/matelab-contract.md](docs/matelab-contract.md)。

## 当前交付边界

本仓库实现 Phase 1–4 的可运行桌面 MVP 和 Phase 5 的基础运维命令。真实账号、隔离记录本、模板建立、历史版本/协作编辑/权限行为必须在 MatElab 测试环境执行 Gate 0；项目不会把文档未保证的行为冒充为已验证能力。
