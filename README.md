# CCtoWechat

Claude Code 接入个人微信。纯 Python 实现，消息注入终端，回复自动发回微信。支持远程权限审核。

**无需公网 IP · 无需 API Key · 无需服务器 · 腾讯官方通道 · 无 Node.js 依赖**

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
> Linux 用户需额外安装 `xdotool`：`sudo apt install xdotool`（Wayland 用户改用 `ydotool` 或 `wtype`）
> macOS 用户需确保终端有辅助功能权限（系统设置 → 隐私与安全性 → 辅助功能）

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
| OS | Windows ✅ | macOS 🧪 | Linux 🧪 |
> 🧪 = 社区贡献，作者无设备实测。如有问题请提交 [Issue](https://gitee.com/Polarstar2333/ccto-wechat/issues)
| Python | ≥ 3.10 |
| Claude Code | 已安装并登录 |
| 微信 | iOS ≥ 8.0.70 / Android ≥ 8.0.69 |

## License

MIT

## 同类对比

| 项目 | 语言 | 部署 | 特点 |
|------|------|------|------|
| **CCtoWechat** | 🐍 Python | pip + 扫码 | 无 Node.js 依赖，约 500 行，带完整 HTML 教程，Windows 一键 bat |
| cc-weixin | Node.js | npx 一键 | 最轻量，约 200 行 |
| claude-code-wechat | Node.js | npx + setup | 功能最全（图片/文件/语音/心跳） |
| cc-connect | Go | go install | 多平台桥接（10+ 聊天平台 + 7 个 AI Agent） |
| claude-plugin-weixin | Plugin | claude plugin install | Claude Code 原生插件形式 |

**CCtoWechat 的优势：** Python 生态，不用装 Node.js；代码短，一眼看完想改就改；HTML 教程图文并茂零基础也能部署。

**其他项目的优势：** cc-weixin 更短更快；claude-code-wechat 功能更多；cc-connect 支持十几个平台；claude-plugin-weixin 安装最简单。
