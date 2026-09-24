# MiMo Link

将 **Xiaomi MiMo Desktop** 的本地能力 API 接到 **Hermes / Codex** 的桌面小工具（苹果风格 Web UI）。

## 功能

- **同步端点**：发现 Desktop 实时端口、校验 / 签发引擎 scoped token（`MIMO_LLM_SERVER_TOKEN`）
- **路由**（两步，启动/同步**不会**自动改配置）
  - Hermes：写入 `mimo-desktop` 模型别名
  - Codex：合并写入 `~/.codex`（`wire_api=responses`，经本机桥接 `:8765`）
- **API Key 管理**：生成 `mlk_…` Key、token / 元额度、入出用量计量
- **费用折算**：按定价（元 / 1M tokens）换算
- **导出**：CC Switch `AppConfig` 格式 JSON

## 快速开始

```bat
cd app
启动 MiMo Link.bat
```

或：

```bash
python app/server.py --port 8765 --open
```

打开 `http://127.0.0.1:8765`。

**顺序**：先在「同步」页生成 **统一 scoped token** → 再生成 API Key → ① 同步端点 → ② 路由至 Hermes / Codex。

## 本地状态文件（插件目录）

| 文件 | 说明 |
|------|------|
| `mimo-link.env` | 引擎 `MIMO_LLM_SERVER_TOKEN` |
| `mimo-link-keys.json` | 受管 API Key 与用量 |
| `mimo-link-pricing.json` | 定价（元 / 1M tokens） |

以上文件已 `.gitignore`，不会提交。

## Codex 协议

Codex 只接受 `wire_api=responses`；MiMo 仅有 Chat Completions。  
`POST /v1/responses`（及 `/v1/chat/completions`）由桥接转换后转发，**使用 Codex 时需保持本应用运行**。

## License

MIT
