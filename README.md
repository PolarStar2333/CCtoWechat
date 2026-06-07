# CCtoWechat

Claude Code 接入个人微信。纯 Python 实现，消息注入终端，微信继承终端聊天记录，支持远程权限审核，支持文件和图片微信->CC（读图需要模型支持），支持/resume时list会话标题为AI提炼标题而不是原生语句截取，支持随时/stop打断模型，门槛低，0花费！（AI提炼需要花费Claude接入的AI的tokens，但是极低）

作者没有Linux和MacOS，可能存在bug！

作者没有Linux和MacOS，可能存在bug！

作者没有Linux和MacOS，可能存在bug！

**无需公网 IP · 无需 API Key · 无需服务器 · 腾讯官方通道 · 无 Node.js 依赖**

## 优势

- **继承终端对话** — 不依赖 Agent SDK，直接模拟键盘注入 Claude Code 终端。已有的对话上下文、权限设置、模型选择原封不动，微信只是换了个对话框
- **远程权限审核** — 人不在电脑前，Claude 弹权限时微信收到通知，回复 `/yes` 批准、`/no` 拒绝。同一权限自动去重，不会重复骚扰
- **纯 Python，零门槛** — `pip install` + `python bridge.py` + 微信扫码，三步完成。不需要 Node.js、npm、Go 等运行时
- **约 1400 行，一眼看完** — bridge.py + inject.py + sessions.py + approval_hook.py，想改什么直接改
- **跨平台** —支持 Windows / macOS / Linux。由于HarmonyOS缺失组件过多，HMPC暂不支持
- **AI 摘要会话列表** — `/resume` 显示的摘要由 AI 生成，比 Claude Code 自带的更可读

## 原理

```
微信 ↔ 腾讯 iLink ↔ bridge.py ↔ Claude Code 终端
                    ↕
              approval_hook.py → 权限审核通知
```

## 快速开始

### 1. 安装依赖
```bash
pip install httpx pyperclip cryptography
```
> Linux 用户需额外安装 `xdotool`：`sudo apt install xdotool`
> macOS 用户需确保终端有辅助功能权限（系统设置 → 隐私与安全性 → 辅助功能）
> 可选：`pip install pytesseract pillow` + 安装 Tesseract-OCR 以支持图片文字识别（OCR）

### 2. 启动桥接
```bash
python bridge.py
```

### 3. 微信扫码
终端打印二维码 URL → 发到微信打开授权

### 4. 开始对话
看到 `监听中...` 后，微信发消息即可

## 远程权限审核

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

Claude 弹出权限确认时，微信会收到通知。回复 `/yes` 批准，`/no` 拒绝。同一权限去重。

## 远程命令

离开电脑时，直接在微信里发以下命令控制 Claude Code：

| 命令 | 作用 |
|------|------|
| `/resume` | AI 摘要会话列表 + 回复数字方向键选会话 |
| `/stop` | Ctrl+C 中断当前操作 + 清空输入栏 |
| `/clear` | 清空上下文 |
| `/compact` | 压缩上下文省 token |
| `/status` | 查看 token 用量 |
| `/usage` | 查看详细用量 |
| `/cost` | 查看费用 |
| `/model` | 切换模型 |
| `/permissions` | 查看/切换权限模式 |
| `/agents` | 查看已注册 Agents |
| `/plugins` | 查看已安装插件 |
| `/mcp` | 查看 MCP 服务器 |
| `/help` | 命令帮助 |
| `/summaries` | 开关 AI 会话摘要 |
| `/imageloc [路径]` | 查看 / 设置图片和文件保存路径 |

全部命令可混用，不注入终端的本地命令（`/summaries` `/imageloc`）桥接本地处理。

## 图片与文件

微信发图或文件后，桥接自动下载并 AES 解密，回复：

- **`/yes`** — 仅注入图片/文件路径到终端，等 Claude 自然回复
- **其他文字** — 图片/文件路径 + 文字一并注入，等 Claude 回复

支持 JPEG、PNG、PDF、docx 等常见格式。图片可配合 OCR（可选）识别图中文字。

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
├── bridge.py          # 主程序（iLink 通信 + 消息处理 + 命令分发）
├── inject.py          # 跨平台键盘注入（Win/Mac/Linux）
├── sessions.py        # 会话管理 + AI 摘要 + JSONL 统计
├── approval_hook.py   # 远程权限审核 Hook
├── session_summaries.json   # AI 会话摘要缓存
├── index.html               # 完整部署教程
├── run.bat                  # Windows 一键启动
├── requirements.txt         # Python 依赖
└── LICENSE                  # MIT
```

## 系统要求

| 项目 | 要求 |
|------|------|
| OS | Windows ✅ \| macOS 🧪 \| Linux 🧪 |
| Python | ≥ 3.10 |
| Claude Code | 已安装并登录 |
| 微信 | iOS微信 ≥ 8.0.70 / Android微信 ≥ 8.0.69 /HarmonyOS＞6.0,HMOS微信≥8.0.18 |

> 🧪 = 社区贡献，作者仅有 Windows 设备。如有问题请提交 [Issue](https://gitee.com/Polarstar2333/ccto-wechat/issues)

## License

MIT
