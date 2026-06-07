# CCtoWechat

Claude Code 接入个人微信。消息注入终端，回复自动发回微信。支持远程权限审核。

**无需公网 IP · 无需 API Key · 无需服务器 · 腾讯官方通道**

## 原理

```
微信 ↔ 腾讯 iLink ↔ bridge.py ↔ Claude Code 终端
                    ↕
              approval_hook.py → 权限审核通知
```

## 快速开始

### 1. 安装依赖
```bash
pip install httpx pyperclip pygetwindow
```

### 2. 启动桥接
```bash
python bridge.py
```

### 3. 微信扫码
终端打印二维码 URL → 发到微信打开授权

### 4. 开始对话
看到 `监听中...` 后，微信发消息即可

## 远程权限审核（可选）

在 Claude Code `~/.claude/settings.json` 中配置：

```json
{
  "hooks": {
    "PermissionRequest": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "python 你的路径/approval_hook.py"
      }]
    }]
  }
}
```

Claude 弹出权限确认时，微信会收到通知。回复 `/yes` 批准，`/no` 拒绝。同一权限去重，不会重复骚扰。

## 主动发微信

```bash
curl -X POST http://127.0.0.1:9876/send \
  -H "Content-Type: application/json" \
  -d '{"text": "你好"}'
```

## 命令行

```bash
python bridge.py                        # 自动跟随最新会话（推荐）
python bridge.py --session <SESSION_ID> # 锁定特定会话
```

## 项目结构

```
├── bridge.py              # 主程序 (~450 行)
├── approval_hook.py        # 权限审核 Hook
├── index.html              # 完整部署教程
├── run.bat                 # Windows 一键启动
├── requirements.txt        # Python 依赖
└── LICENSE                 # MIT
```

## 系统要求

| 项目 | 要求 |
|------|------|
| OS | Windows 10 / 11 |
| Python | ≥ 3.10 |
| Claude Code | 已安装并登录 |
| 微信 | iOS ≥ 8.0.70 / Android ≥ 8.0.69 |

## License

MIT
