#!/usr/bin/env python3
# MIT License - Copyright (c) 2026 CCtoWechat
# https://github.com/CCtoWechat/CCtoWechat
"""
CCtoWechat — Claude Code 接入个人微信

将微信消息通过腾讯 iLink Bot API 收发，注入本机 Claude Code 终端，
并监控 JSONL 日志抓取 Claude 回复发回微信。

用法:
  python bridge.py                        # 自动跟随最新会话
  python bridge.py --session <SESSION_ID> # 锁定特定会话

依赖: pip install httpx pyperclip pygetwindow
平台: Windows ✅ | macOS 🧪 | Linux 🧪（🧪 = 社区支持，作者无设备测试）
"""

import argparse, asyncio, ctypes, httpx, json, base64, os, random, subprocess, time, traceback
from pathlib import Path

try:
    import pyperclip
except ImportError:
    pyperclip = None

API_BASE = "https://ilinkai.weixin.qq.com"
CH_VERSION = "1.0.2"

ROOT = Path(__file__).parent
CRED_FILE = ROOT / "credentials.json"
BUF_FILE = ROOT / "cursor.json"
LAST_USER_FILE = ROOT / "last_user.json"
APPROVAL_FILE = ROOT / "approval_pending.json"
SESSION_FILE = ROOT / "session_id.txt"
def _find_session_dir():
    """自动探测 Claude Code 项目目录（找最近有 JSONL 文件的那个）"""
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return base / "C--Users-zshyl"  # 兜底
    # 找包含 JSONL 文件的最近修改的目录
    best = None
    best_time = 0
    for d in base.iterdir():
        if d.is_dir():
            files = list(d.glob("*.jsonl"))
            if files:
                mt = max(f.stat().st_mtime for f in files)
                if mt > best_time:
                    best_time = mt
                    best = d
    return best or (base / "C--Users-zshyl")

SESSION_DIR = _find_session_dir()
LOCKED_JSONL = None

# ── helpers ──
def _uin():
    return base64.b64encode(str(random.randint(0, 2**32 - 1)).encode()).decode()

def _hdrs(tok):
    return {"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {tok}", "X-WECHAT-UIN": _uin()}

# ── iLink ──
async def get_bot_qrcode():
    async with httpx.AsyncClient(timeout=15, proxy=None, trust_env=False) as c:
        r = await c.get(f"{API_BASE}/ilink/bot/get_bot_qrcode", params={"bot_type": 3})
        return r.json()["qrcode"], r.json().get("qrcode_img_content", "")

async def wait_qrcode_scan(qrcode):
    async with httpx.AsyncClient(timeout=42) as c:
        try:
            r = await c.get(f"{API_BASE}/ilink/bot/get_qrcode_status",
                            params={"qrcode": qrcode}, headers={"iLink-App-ClientVersion": "1"})
            return r.json()
        except httpx.TimeoutException:
            return {"status": "wait"}

async def login():
    qrcode, qr_url = await get_bot_qrcode()
    print(f"\n 请用微信扫描：\n   {qr_url}\n  等待扫码...")
    refresh = 0
    while True:
        r = await wait_qrcode_scan(qrcode)
        s = r.get("status", "wait")
        if s == "confirmed":
            creds = {"token": r["bot_token"], "base_url": r.get("baseurl", API_BASE),
                     "bot_id": r.get("ilink_bot_id", ""), "user_id": r.get("ilink_user_id", "")}
            CRED_FILE.write_text(json.dumps(creds, indent=2), encoding="utf-8")
            print(f" 登录成功 ({creds['bot_id']})")
            return creds
        if s == "expired":
            refresh += 1
            if refresh >= 3: return None
            qrcode, qr_url = await get_bot_qrcode()
            print(f"   已刷新({refresh}/3)：\n   {qr_url}")
        elif s == "scaned":
            print("   已扫码，手机上确认...")
        await asyncio.sleep(1)

async def getupdates(client, tok, buf=""):
    try:
        r = await client.post(f"{API_BASE}/ilink/bot/getupdates",
            json={"get_updates_buf": buf, "base_info": {"channel_version": CH_VERSION}},
            headers=_hdrs(tok), timeout=42)
        return r.json()
    except httpx.TimeoutException:
        return {"ret": 0, "msgs": [], "get_updates_buf": buf}

async def sendmsg(client, tok, to_user, text, ctx_token):
    cid = f"c{int(time.time()*1000)}"
    r = await client.post(f"{API_BASE}/ilink/bot/sendmessage", json={
        "msg": {"to_user_id": to_user, "client_id": cid, "message_type": 2,
                "message_state": 2, "context_token": ctx_token,
                "item_list": [{"type": 1, "text_item": {"text": text}}]},
        "base_info": {"channel_version": CH_VERSION}}, headers=_hdrs(tok), timeout=15)
    return r.status_code == 200

# ── 注入（剪贴板 + 模拟粘贴回车，三平台支持）──
# NOTE: Linux / macOS 支持由社区贡献，作者仅有 Windows 设备，未实际测试。
#       如遇问题请提交 Issue：https://gitee.com/Polarstar2333/ccto-wechat/issues
_WIN = os.name == "nt"
_OSX = os.uname().sysname == "Darwin" if hasattr(os, "uname") else False

def _inject_win(text):
    """Windows: keybd_event 模拟 Ctrl+V + Enter"""
    user32 = ctypes.windll.user32
    VK_CTRL, VK_V, VK_RET, KEYUP = 0x11, 0x56, 0x0D, 0x0002
    user32.keybd_event(VK_CTRL, 0, 0, 0)
    user32.keybd_event(VK_V, 0, 0, 0); time.sleep(0.03)
    user32.keybd_event(VK_V, 0, KEYUP, 0); time.sleep(0.01)
    user32.keybd_event(VK_CTRL, 0, KEYUP, 0)
    time.sleep(0.1)
    user32.keybd_event(VK_RET, 0, 0, 0); time.sleep(0.03)
    user32.keybd_event(VK_RET, 0, KEYUP, 0)
    return True

def _inject_osx(text):
    """macOS: osascript 模拟 Cmd+V + Return"""
    script = f'''
    tell application "System Events"
        keystroke "v" using command down
        delay 0.1
        keystroke return
    end tell
    '''
    subprocess.run(["osascript", "-e", script], check=False)
    return True

def _inject_linux(text):
    """Linux: xdotool 模拟 Ctrl+V + Return（需 apt install xdotool）"""
    subprocess.run(["xdotool", "key", "ctrl+v"], check=False)
    time.sleep(0.1)
    subprocess.run(["xdotool", "key", "Return"], check=False)
    return True

def inject_to_terminal(text):
    # 1) 写入剪贴板
    if pyperclip is not None:
        try:
            pyperclip.copy(text)
        except Exception:
            _clip_fallback(text)
    else:
        _clip_fallback(text)
    time.sleep(0.15)

    # 2) 模拟粘贴 + 回车
    if _WIN:
        return _inject_win(text)
    elif _OSX:
        return _inject_osx(text)
    else:
        return _inject_linux(text)

def _clip_fallback(text):
    """剪贴板备用方案"""
    if _WIN:
        try:
            subprocess.run(["clip"], input=text.encode("utf-16-le", errors="replace"), check=False)
        except Exception:
            pass
    elif _OSX:
        try:
            subprocess.run(["pbcopy"], input=text.encode(), check=False)
        except Exception:
            pass
    else:
        try:
            subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(), check=False)
        except Exception:
            pass

# ── JSONL ──
def _jsonl():
    """全局扫描所有项目目录，找最近修改的 JSONL"""
    global LOCKED_JSONL
    if LOCKED_JSONL: return LOCKED_JSONL
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return None
    best = None; best_time = 0
    for d in base.iterdir():
        if not d.is_dir(): continue
        for f in d.glob("*.jsonl"):
            mt = f.stat().st_mtime
            if mt > best_time:
                best_time = mt; best = f
    return best

def _text(msg):
    """只提取 type=text 的内容，过滤掉 thinking/tool_use"""
    c = msg.get("content", [])
    if isinstance(c, str): return c
    if isinstance(c, list):
        texts = []
        for it in c:
            if not isinstance(it, dict): continue
            if it.get("type") == "text":
                texts.append(it.get("text", ""))
        return "".join(texts)
    return ""


def _is_text_response(msg):
    """只接受 stop_reason=end_turn 且有文本内容的回复"""
    if msg.get("role") != "assistant": return False
    if msg.get("stop_reason") != "end_turn": return False
    content = msg.get("content", [])
    if not isinstance(content, list): return bool(_text(msg))
    return any(isinstance(c, dict) and c.get("type") == "text" for c in content)

def _read_new(jsonl_path, pos):
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(max(0, pos))
            return f.readlines(), f.tell()
    except: return [], pos

def _has_user(lines, fragment):
    frag = fragment.strip()[:50]
    for ln in lines:
        try: obj = json.loads(ln)
        except: continue
        msg = obj.get("message") or obj
        if msg.get("role") == "user" and frag in _text(msg):
            return True
    return False

def _collect_all_text(lines):
    """收集所有 assistant 文本（含 tool_use 前的文字，只过滤掉纯 thinking）"""
    texts = []
    for ln in lines:
        try: obj = json.loads(ln)
        except: continue
        msg = obj.get("message") or obj
        if msg.get("role") != "assistant": continue
        if msg.get("stop_reason") == "end_turn" or msg.get("stop_reason") == "tool_use":
            t = _text(msg)
            if t and len(t) > 2:
                texts.append(t)
    return texts


def _find_end_turn(lines):
    """找到最后一条 end_turn 的 assistant 消息，返回其位置索引"""
    for i in range(len(lines) - 1, -1, -1):
        try: obj = json.loads(lines[i])
        except: continue
        msg = obj.get("message") or obj
        if msg.get("role") == "assistant" and msg.get("stop_reason") == "end_turn":
            if any(isinstance(c, dict) and c.get("type") == "text" for c in msg.get("content", [])):
                return i
    return None


def _all_jsonl():
    """返回所有项目目录下的 JSONL 文件列表"""
    base = Path.home() / ".claude" / "projects"
    if not base.exists(): return []
    files = []
    for d in base.iterdir():
        if d.is_dir():
            files.extend(d.glob("*.jsonl"))
    return files


async def wait_reply(jsonl, inject_text, start_pos, phase1_timeout=600):
    """两阶段等待：同时监控所有 JSONL，谁先出现用户消息就用谁"""
    # 注入前快照：记录所有 JSONL 的当前大小
    snapshots = {}
    for f in _all_jsonl():
        try: snapshots[str(f)] = f.stat().st_size
        except: pass

    found = False; all_lines = []

    # 阶段1：轮询所有 JSONL
    for _i in range(phase1_timeout):
        for f in _all_jsonl():
            key = str(f)
            sp = snapshots.get(key, 0)
            lines, _new_pos = await asyncio.to_thread(_read_new, f, sp)
            if _has_user(lines, inject_text):
                jsonl = f; found = True
                all_lines = lines[:]
                snapshots[key] = _new_pos  # 更新位置
                print(f" [ph1->ph2:{f.name[:12]}]", flush=True)
                ei = _find_end_turn(all_lines)
                if ei is not None:
                    texts = _collect_all_text(all_lines[:ei + 1])
                    if texts: return "\n\n".join(texts)
                break
            # 有新行就更新位置
            if lines: snapshots[key] = _new_pos
        if found: break
        await asyncio.sleep(1)
    else:
        return None  # 阶段1超时

    # 阶段2：只跟踪找到的那个 JSONL，等 end_turn（无上限）
    while True:
        sp = snapshots.get(str(jsonl), 0)
        lines, new_pos = await asyncio.to_thread(_read_new, jsonl, sp)
        if lines:
            snapshots[str(jsonl)] = new_pos
            all_lines.extend(lines)
            ei = _find_end_turn(all_lines)
            if ei is not None:
                texts = _collect_all_text(all_lines[:ei + 1])
                if texts: return "\n\n".join(texts)
        await asyncio.sleep(1)

# ── HTTP /send ──
last_user = None; tok_g = None; cli_g = None

async def http_handler(reader, writer):
    global last_user, tok_g, cli_g
    try:
        data = await asyncio.wait_for(reader.read(8192), timeout=5)
        body = data.decode("utf-8", errors="replace")
        if "POST" in body and "/send" in body:
            for line in body.split("\r\n"):
                if line.startswith("{") and '"text"' in line:
                    try:
                        txt = json.loads(line)["text"]
                        if last_user and tok_g and cli_g:
                            ok = await sendmsg(cli_g, tok_g, last_user["to_user"], txt, last_user["ctx_token"])
                            print(f"   主动发送: {txt[:60]} {'OK' if ok else 'FAIL'}")
                    except: pass
    except: pass
    finally:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK"); await writer.drain(); writer.close()

async def start_http():
    srv = await asyncio.start_server(http_handler, "127.0.0.1", 9876)
    print(" HTTP: http://127.0.0.1:9876")
    return srv

# ── 审核模式 ──
pending_approval = False
last_notified = ""  # 上次通知内容，用于去重

async def check_approval():
    """后台协程：检测权限请求，内容去重后通知微信"""
    global pending_approval, last_user, last_notified
    while True:
        if APPROVAL_FILE.exists() and last_user:
            try:
                raw = APPROVAL_FILE.read_text(encoding="utf-8").strip()
                if raw == last_notified:
                    await asyncio.sleep(1); continue  # 内容相同，跳过
                info = json.loads(raw)
                tool = info.get("tool", "?")
                inp = info.get("input", "")[:120]
                tip = f"🔐 Claude 请求权限\n工具: {tool}\n内容: {inp}\n\n回复 /yes 批准，/no 拒绝"
                if tok_g and cli_g:
                    await sendmsg(cli_g, tok_g, last_user["to_user"], tip, last_user["ctx_token"])
                    pending_approval = True; last_notified = raw
                    print(f"\n 📩 审核请求已转发微信: {tool}", flush=True)
            except Exception:
                pass
        await asyncio.sleep(1)

# ── 消息 ──
async def handle(client, tok, raw):
    global last_user, pending_approval
    if isinstance(raw, str):
        try: msg = json.loads(raw)
        except: return
    else: msg = raw
    if msg.get("message_type", 0) != 1: return
    fu = msg.get("from_user_id", "") or msg.get("from_user", "")
    ct = msg.get("context_token", "")
    for item in msg.get("item_list", []):
        if item.get("type") != 1: continue
        text = item.get("text_item", {}).get("text", "").strip()
        if not text: continue

        print(f"\n [{fu[:20]}]: {text[:120]}")
        last_user = {"to_user": fu, "ctx_token": ct}
        LAST_USER_FILE.write_text(json.dumps(last_user), encoding="utf-8")

        # 审核模式：只有以 /yes 或 /no 开头的消息才当审核处理
        if pending_approval and text.strip().lower().startswith(("/yes", "/no", "/y", "/n")):
            pending_approval = False
            answer = "yes" if text.strip().lower().startswith(("/yes", "/y")) else "no"
            print(f"   审核回复: {answer}", flush=True)
            inject_to_terminal(answer)
            continue
        elif pending_approval:
            # 不是审核回复，清掉标记正常处理
            pending_approval = False

        jsonl = _jsonl()
        sp = jsonl.stat().st_size if jsonl else 0

        print("   注入...", end="", flush=True)
        inject_to_terminal(text)
        print(" 等待回复...", end="", flush=True)
        reply = await wait_reply(jsonl, text, sp)
        if reply:
            ok = await sendmsg(client, tok, fu, reply, ct)
            print(f"\r   已回复: {reply[:100]}" if ok else f"\r   发送失败!")
        else:
            print(f"\r   超时")

# ── main ──
async def main():
    global last_user, tok_g, cli_g
    parser = argparse.ArgumentParser()
    parser.add_argument("--session")
    args = parser.parse_args()

    print("=" * 36)
    print("  WeChat + Claude Bridge v4")
    print("=" * 36)

    # session lock（仅当显式指定 --session 时才锁定）
    global LOCKED_JSONL
    if args.session:
        j = SESSION_DIR / f"{args.session}.jsonl"
        if j.exists():
            LOCKED_JSONL = j
            print(f" 锁定: {args.session}")
        else:
            print(f" [警告] 会话 {args.session} 不存在")
    else:
        print(" 自动跟随最新会话")

    print(f" 会话目录: {SESSION_DIR}")
    srv = await start_http()

    if CRED_FILE.exists():
        creds = json.loads(CRED_FILE.read_text(encoding="utf-8"))
        print(f" 凭证: {creds.get('bot_id','?')}")
    else:
        creds = await login()
        if creds is None: return

    tok = creds["token"]; tok_g = tok

    if LAST_USER_FILE.exists():
        try: last_user = json.loads(LAST_USER_FILE.read_text(encoding="utf-8"))
        except: pass

    buf = ""
    if BUF_FILE.exists():
        try: buf = json.loads(BUF_FILE.read_text()).get("buf", "")
        except: pass

    async with httpx.AsyncClient(proxy=None, trust_env=False) as client:
        cli_g = client
        asyncio.create_task(check_approval())  # 启动审核监听
        print(" 监听中...\n")
        ec = 0
        while True:
            try:
                resp = await getupdates(client, tok, buf)
                errc = resp.get("errcode", resp.get("ret", 0))
                if errc != 0:
                    if errc == -14: print(" Session过期"); CRED_FILE.unlink(missing_ok=True); return
                    ec += 1; w = 2 if ec < 3 else 30
                    print(f" 错误{errc} {w}s重试"); await asyncio.sleep(w); continue
                ec = 0
                for msg in resp.get("msgs", []):
                    asyncio.create_task(handle(client, tok, msg))
                nb = resp.get("get_updates_buf", "")
                if nb and nb != buf: buf = nb; BUF_FILE.write_text(json.dumps({"buf": nb}))
            except KeyboardInterrupt: print("\n 停止"); break
            except Exception as e:
                ec += 1; w = 2 if ec < 3 else 30
                print(f" 异常: {e}"); traceback.print_exc(); await asyncio.sleep(w)
    srv.close()

if __name__ == "__main__":
    asyncio.run(main())
