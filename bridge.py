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

import argparse, asyncio, httpx, json, base64, random, time, traceback, re, logging
from pathlib import Path

from logger_setup import setup_logging, get_logger
from config import CONFIG
from inject import inject_to_terminal, select_session, send_interrupt
from sessions import (init as sessions_init, set_locked_jsonl,
                      get_session_list, get_last_reply, wait_reply,
                      find_session_dir, toggle_summaries, _jsonl as sessions_jsonl,
                      get_session_stats, format_stats, format_usage, format_cost)

logger = logging.getLogger("bridge")

API_BASE = CONFIG["api_base"]
CH_VERSION = CONFIG["channel_version"]

ROOT = Path(__file__).parent
CRED_FILE = ROOT / "credentials.json"
BUF_FILE = ROOT / "cursor.json"
LAST_USER_FILE = ROOT / "last_user.json"
APPROVAL_FILE = ROOT / "approval_pending.json"

SESSION_DIR = find_session_dir()
sessions_init(SESSION_DIR, ROOT)

# ── helpers ──
def _uin():
    return base64.b64encode(str(random.randint(0, 2**32 - 1)).encode()).decode()

def _hdrs(tok):
    return {"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {tok}", "X-WECHAT-UIN": _uin()}

# ── iLink ──
async def get_bot_qrcode():
    async with httpx.AsyncClient(timeout=CONFIG["timeouts"]["short"], proxy=None, trust_env=False) as c:
        r = await c.get(f"{API_BASE}/ilink/bot/get_bot_qrcode", params={"bot_type": 3})
        return r.json()["qrcode"], r.json().get("qrcode_img_content", "")

async def wait_qrcode_scan(qrcode):
    async with httpx.AsyncClient(timeout=CONFIG["timeouts"]["long_poll"]) as c:
        try:
            r = await c.get(f"{API_BASE}/ilink/bot/get_qrcode_status",
                            params={"qrcode": qrcode}, headers={"iLink-App-ClientVersion": "1"})
            return r.json()
        except httpx.TimeoutException:
            return {"status": "wait"}

QR_FILE = ROOT / "qr_url.txt"  # 当前登录二维码，HTTP /qr 端点提供手机端扫码

async def login():
    qrcode, qr_url = await get_bot_qrcode()
    QR_FILE.write_text(qr_url)
    print(f"\n 请用微信扫描：\n   {qr_url}\n  等待扫码...")
    logger.info("已生成登录二维码")
    refresh = 0
    while True:
        r = await wait_qrcode_scan(qrcode)
        s = r.get("status", "wait")
        if s == "confirmed":
            creds = {"token": r["bot_token"], "base_url": r.get("baseurl", API_BASE),
                     "bot_id": r.get("ilink_bot_id", ""), "user_id": r.get("ilink_user_id", "")}
            CRED_FILE.write_text(json.dumps(creds, indent=2), encoding="utf-8")
            QR_FILE.unlink(missing_ok=True)
            logger.info(f"登录成功 bot_id={creds['bot_id']}")
            return creds
        if s == "expired":
            refresh += 1
            if refresh >= CONFIG["max_qr_refresh"]: return None
            qrcode, qr_url = await get_bot_qrcode()
            QR_FILE.write_text(qr_url)
            print(f"   已刷新({refresh}/{CONFIG['max_qr_refresh']})：\n   {qr_url}")
        elif s == "scaned":
            print("   已扫码，手机上确认...")
        await asyncio.sleep(CONFIG["poll_interval"])

async def getupdates(client, tok, buf=""):
    try:
        r = await client.post(f"{API_BASE}/ilink/bot/getupdates",
            json={"get_updates_buf": buf, "base_info": {"channel_version": CH_VERSION}},
            headers=_hdrs(tok), timeout=CONFIG["timeouts"]["long_poll"])
        return r.json()
    except httpx.TimeoutException:
        return {"ret": 0, "msgs": [], "get_updates_buf": buf}

async def sendmsg(client, tok, to_user, text, ctx_token):
    cid = f"c{int(time.time()*1000)}"
    r = await client.post(f"{API_BASE}/ilink/bot/sendmessage", json={
        "msg": {"to_user_id": to_user, "client_id": cid, "message_type": 2,
                "message_state": 2, "context_token": ctx_token,
                "item_list": [{"type": 1, "text_item": {"text": text}}]},
        "base_info": {"channel_version": CH_VERSION}}, headers=_hdrs(tok), timeout=CONFIG["timeouts"]["short"])
    return r.status_code == 200


async def send_image(client, tok, to_user, ctx_token, image_bytes):
    """发送图片：AES-128-ECB 加密 → 上传 CDN → sendmessage"""
    import secrets
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    # 1) 生成 AES 密钥（16字节 = 128位）
    aes_key = secrets.token_bytes(16)
    # AES-128-ECB 加密（PKCS7 填充）
    pad_len = 16 - (len(image_bytes) % 16)
    padded = image_bytes + bytes([pad_len] * pad_len)
    cipher = Cipher(algorithms.AES(aes_key), modes.ECB())
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()

    # 2) 获取 CDN 上传 URL
    r = await client.post(f"{API_BASE}/ilink/bot/getuploadurl",
        json={"base_info": {"channel_version": CH_VERSION}},
        headers=_hdrs(tok), timeout=CONFIG["timeouts"]["short"])
    if r.status_code != 200:
        return False
    up_info = r.json()
    upload_url = up_info.get("upload_url", "")
    cdn_url = up_info.get("cdn_url", "")
    if not upload_url:
        return False

    # 3) 上传加密图片到 CDN
    r2 = await client.put(upload_url, content=encrypted, timeout=CONFIG["timeouts"]["upload"])
    if r2.status_code not in (200, 204):
        return False

    # 4) 发送图片消息
    cid = f"c{int(time.time()*1000)}"
    r3 = await client.post(f"{API_BASE}/ilink/bot/sendmessage", json={
        "msg": {"to_user_id": to_user, "client_id": cid, "message_type": 2,
                "message_state": 2, "context_token": ctx_token,
                "item_list": [{"type": 2, "image_item": {
                    "aes_key": base64.b64encode(aes_key).decode(),
                    "file_size": len(image_bytes),
                    "cdn_url": cdn_url,
                    "width": 0, "height": 0,
                }}]},
        "base_info": {"channel_version": CH_VERSION}}, headers=_hdrs(tok), timeout=CONFIG["timeouts"]["short"])
    return r3.status_code == 200

# ── HTTP /send ──
last_user = None; tok_g = None; cli_g = None

async def http_handler(reader, writer):
    global last_user, tok_g, cli_g
    try:
        data = await asyncio.wait_for(reader.read(65536), timeout=10)
        body = data.decode("utf-8", errors="replace")
        # GET /qr → 重定向到微信扫码 URL
        if body.startswith("GET /qr"):
            if QR_FILE.exists():
                qr_url = QR_FILE.read_text().strip()
                redirect = (
                    "HTTP/1.1 302 Found\r\n"
                    f"Location: {qr_url}\r\n"
                    "Content-Type: text/html; charset=utf-8\r\n"
                    "Content-Length: 0\r\n\r\n"
                ).encode()
                writer.write(redirect)
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 8\r\n\r\n(no qr)")
            await writer.drain(); writer.close()
            return
        # GET / → 简单状态页
        if body.startswith("GET / ") or body.startswith("GET / HTTP"):
            status = "运行中" if tok_g else "未登录"
            html = f"<html><meta charset=utf-8><body><h3>CCtoWechat Bridge</h3><p>{status}</p></body></html>"
            resp = f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(html.encode())}\r\n\r\n{html}"
            writer.write(resp.encode()); await writer.drain(); writer.close()
            return
        # POST /send → 主动发消息
        if "POST" in body and "/send" in body:
            for line in body.split("\r\n"):
                if not line.startswith("{"):
                    continue
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                if last_user and tok_g and cli_g:
                    to = last_user["to_user"]; ct = last_user["ctx_token"]
                    if "text" in payload:
                        ok = await sendmsg(cli_g, tok_g, to, payload["text"], ct)
                        logger.info(f"主动发送文本 {'OK' if ok else 'FAIL'} text={payload['text'][:60]}")
                    elif "image_path" in payload:
                        img = Path(payload["image_path"]).read_bytes()
                        ok = await send_image(cli_g, tok_g, to, ct, img)
                        logger.info(f"主动发图 {'OK' if ok else 'FAIL'} path={payload['image_path']}")
    except:
        logger.exception("HTTP handler 异常")
    finally:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK"); await writer.drain(); writer.close()

async def start_http():
    srv = await asyncio.start_server(http_handler, CONFIG["http_host"], CONFIG["http_port"])
    print(f" HTTP: http://{CONFIG['http_host']}:{CONFIG['http_port']}  /qr=scan")
    return srv

# ── API 响应通知 ──
async def _wait_with_think(client, tok, fu, ct, jsonl, text, sp, **kw):
    """等待 Claude 回复，检测到 API 开始输出时通知微信"""
    async def on_respond():
        try: await sendmsg(client, tok, fu, "API 开始返回", ct)
        except: pass
    return await wait_reply(jsonl, text, sp, on_first_respond=on_respond, **kw)
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
                    logger.info(f"审核请求已转发 tool={tool}")
            except Exception:
                logger.exception("检查审核请求失败")
        await asyncio.sleep(1)

# ── 消息 ──
awaiting_session_select = False     # /resume 后等待用户选号
awaiting_model_select = False       # /model 后等待用户选号
_model_map = {}                      # 编号 -> 别名 映射
awaiting_permissions_select = False  # /permissions 后等待选号
_permissions_map = {}                 # 编号 -> 模式名 映射
awaiting_image_action = False       # 发图/文件后等待 /yes /no
pending_image_path = ""             # 待处理的文件路径
pending_is_file = False             # True=文件, False=图片

IMAGES_DIR = ROOT / CONFIG["images_dir"]  # 默认保存位置

def _is_remote_cmd(text):
    """只匹配纯英文 slash 命令（/字母开头，不含中文等非ASCII）"""
    t = text.strip()
    if not t.startswith("/"):
        return False
    first = t.split()[0].lower() if t.strip() else ""
    return bool(re.match(r'^/[a-zA-Z][a-zA-Z0-9_-]*$', first))

async def handle(client, tok, raw):
    global last_user, pending_approval, awaiting_session_select, awaiting_model_select, _model_map, IMAGES_DIR
    global awaiting_image_action, pending_image_path, pending_is_file
    global awaiting_permissions_select, _permissions_map
    if isinstance(raw, str):
        try: msg = json.loads(raw)
        except:
            logger.debug(f"非JSON消息: {str(raw)[:100]}")
            return
    else: msg = raw
    if msg.get("message_type", 0) not in (1, 2): return
    fu = msg.get("from_user_id", "") or msg.get("from_user", "")
    ct = msg.get("context_token", "")
    for item in msg.get("item_list", []):
        item_type = item.get("type", 0)
        # ── 图片消息 ──
        if item_type == 2:
            last_user = {"to_user": fu, "ctx_token": ct}
            img = item.get("image_item", {})
            media = img.get("media", {})
            img_url = media.get("full_url", img.get("url", ""))
            aeskey_hex = img.get("aeskey", "")
            logger.info(f"收到图片 from={fu[:20]} url={img_url[:80]}")
            await sendmsg(client, tok, fu, "收到图片，下载解密中...", ct)
            img_path = IMAGES_DIR / f"{int(time.time())}.jpg"
            img_path.parent.mkdir(exist_ok=True)
            try:
                r = await client.get(img_url, timeout=CONFIG["timeouts"]["download"])
                if r.status_code != 200:
                    await sendmsg(client, tok, fu, f"下载失败: HTTP {r.status_code}", ct)
                    continue
                data = r.content
                # 调试：保存原始下载 + 密钥
                img_path.with_suffix(".raw").write_bytes(data)
                img_path.with_suffix(".key").write_text(aeskey_hex, encoding="utf-8")
                # AES 解密（iLink 图片加密）
                if aeskey_hex:
                    try:
                        key = bytes.fromhex(aeskey_hex)
                        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                        # 先 ECB 后 CBC（不同微信版本加密方式不同）
                        for mode_desc, cipher in [
                            ("ECB", Cipher(algorithms.AES(key), modes.ECB())),
                            ("CBC", Cipher(algorithms.AES(key), modes.CBC(bytes(16)))),
                        ]:
                            d = cipher.decryptor().update(data) + cipher.decryptor().finalize()
                            eoi = d.rfind(b'\xff\xd9')
                            if eoi > 0 and d[:2] == b'\xff\xd8':
                                data = d[:eoi + 2]; break
                            if d[:2] == b'\xff\xd8':
                                pad = d[-1]
                                if 0 < pad <= 16 and all(b == pad for b in d[-pad:]):
                                    data = d[:-pad]; break
                        else:
                            data = d
                    except ImportError:
                        logger.debug("cryptography 未安装，跳过图片AES解密")
                img_path.write_bytes(data)
                pending_image_path = str(img_path)
                pending_is_file = False
                awaiting_image_action = True
                await sendmsg(client, tok, fu,
                    f"图片已保存 ({len(data)//1024}KB)\n\n回复 /yes 仅注入图片地址\n回复其他内容将一并注入图片地址和文字", ct)
            except Exception as e:
                await sendmsg(client, tok, fu, f"处理失败: {e}", ct)
            continue
        # ── 文件消息 ──
        if item_type == 4:
            last_user = {"to_user": fu, "ctx_token": ct}
            fitem = item.get("file_item", {})
            media = fitem.get("media", {})
            file_url = media.get("full_url", "")
            file_name = fitem.get("file_name", "unknown")
            file_size = int(fitem.get("len", 0))
            aeskey_b64 = media.get("aes_key", "")
            logger.info(f"收到文件 from={fu[:20]} name={file_name} size={file_size//1024}KB")
            await sendmsg(client, tok, fu, f"收到文件: {file_name} ({file_size//1024}KB)\n下载解密中...", ct)
            file_path = Path(IMAGES_DIR) / "files" / file_name
            file_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                r = await client.get(file_url, timeout=CONFIG["timeouts"]["download"])
                if r.status_code != 200:
                    await sendmsg(client, tok, fu, f"下载失败: HTTP {r.status_code}", ct)
                    continue
                data = r.content
                # AES 解密（同图片）
                if aeskey_b64:
                    try:
                        # aes_key 是 base64(hex_string)，需两次解码
                        hex_str = base64.b64decode(aeskey_b64).decode("ascii")
                        key = bytes.fromhex(hex_str)
                        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                        # 先 ECB 后 CBC（同图片逻辑）
                        for mode_desc, cipher in [
                            ("ECB", Cipher(algorithms.AES(key), modes.ECB())),
                            ("CBC", Cipher(algorithms.AES(key), modes.CBC(bytes(16)))),
                        ]:
                            d = cipher.decryptor().update(data) + cipher.decryptor().finalize()
                            if d[:2] == b'PK':  # ZIP/docx header
                                pad = d[-1]
                                if 0 < pad <= 16 and all(b == pad for b in d[-pad:]):
                                    data = d[:-pad]; break
                                data = d; break
                        else:
                            data = d  # fallback
                    except ImportError:
                        logger.debug("cryptography 未安装，跳过文件AES解密")
                file_path.write_bytes(data)
                pending_image_path = str(file_path)
                pending_is_file = True
                awaiting_image_action = True
                await sendmsg(client, tok, fu,
                    f"文件已保存 ({len(data)//1024}KB)\n\n回复 /yes 仅注入路径\n回复其他内容一并注入", ct)
            except Exception as e:
                await sendmsg(client, tok, fu, f"处理失败: {e}", ct)
            continue
        if item_type != 1: continue
        text = item.get("text_item", {}).get("text", "").strip()
        if not text: continue

        logger.info(f"收到文本消息 from={fu[:20]} text={text[:120]}")
        last_user = {"to_user": fu, "ctx_token": ct}
        LAST_USER_FILE.write_text(json.dumps(last_user), encoding="utf-8")

        # ── 模型选择模式 ──
        if awaiting_model_select and text.strip().isdigit():
            num = int(text.strip())
            awaiting_model_select = False
            if num == 0 or num not in _model_map:
                await sendmsg(client, tok, fu, "已取消", ct)
            else:
                alias = _model_map[num]
                logger.info(f"注入模型切换: /model {alias}")
                inject_to_terminal(f"/model {alias}")
                await sendmsg(client, tok, fu, f"已切换至 {alias}，查看终端确认", ct)
            continue

        # ── 权限模式选择 ──
        if awaiting_permissions_select and text.strip().isdigit():
            num = int(text.strip())
            awaiting_permissions_select = False
            if num == 0 or num not in _permissions_map:
                await sendmsg(client, tok, fu, "已取消", ct)
            else:
                mode = _permissions_map[num]
                sp = Path.home() / ".claude" / "settings.local.json"
                try:
                    sp_data = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
                except:
                    sp_data = {}
                sp_data["permissionMode"] = mode
                sp.write_text(json.dumps(sp_data, indent=2, ensure_ascii=False), encoding="utf-8")
                await sendmsg(client, tok, fu, f"权限模式已切换至: {mode}", ct)
            continue

        # ── 会话选择模式：检测纯数字回复 ──
        if awaiting_session_select and text.strip().isdigit():
            num = int(text.strip())
            if 0 <= num <= 12:
                awaiting_session_select = False
                if num == 0:
                    select_session(0)
                    await sendmsg(client, tok, fu, "已取消", ct)
                    continue
                logger.info(f"选择会话 #{num}")
                select_session(num)
                time.sleep(3)
                set_locked_jsonl(None)
                last_reply = get_last_reply()
                logger.info(f"会话切换完成 num={num} reply_len={len(last_reply)}")
                if last_reply:
                    await sendmsg(client, tok, fu,
                        f"已切换到会话 #{num}\n\n上次回复：\n{last_reply[:1200]}", ct)
                else:
                    await sendmsg(client, tok, fu, f"已切换到会话 #{num}（无历史回复）", ct)
                continue

        # ── 图片/文件附带文字模式 ──
        if awaiting_image_action:
            awaiting_image_action = False
            t = text.strip().lower()
            label = "文件" if pending_is_file else "图片"
            safe_path = pending_image_path.replace("\\", "/")
            if t.startswith("/yes") or t.startswith("/y"):
                msg = f"收到一份微信{label}，已保存至 {safe_path}"
                inject_to_terminal(msg)
                logger.info(f"{label}路径已注入 path={pending_image_path}")
                jsonl = sessions_jsonl()
                sp = jsonl.stat().st_size if jsonl else 0
                reply = await _wait_with_think(client, tok, fu, ct, jsonl, msg, sp, phase1_timeout=120)
                if reply:
                    await sendmsg(client, tok, fu, reply, ct)
                continue
            # 其他回复 → 路径 + 文字一起注入
            inject_to_terminal(f"[微信{label}: {safe_path}]\n\n{text}")
            logger.info(f"{label}+文字已注入 path={pending_image_path}")
            jsonl = sessions_jsonl()
            sp = jsonl.stat().st_size if jsonl else 0
            reply = await _wait_with_think(client, tok, fu, ct, jsonl, text, sp)
            if reply:
                await sendmsg(client, tok, fu, reply, ct)
            continue

        # 审核模式：只有以 /yes 或 /no 开头的消息才当审核处理
        if pending_approval and text.strip().lower().startswith(("/yes", "/no", "/y", "/n")):
            pending_approval = False
            answer = "yes" if text.strip().lower().startswith(("/yes", "/y")) else "no"
            logger.info(f"审核回复已注入 answer={answer}")
            inject_to_terminal(answer)
            continue
        elif pending_approval:
            # 不是审核回复，清掉标记正常处理
            pending_approval = False

        # 远程命令：所有 / 开头直接穿透给终端
        if _is_remote_cmd(text):
            cmd = text.strip().lower()
            # ── CCtoWechat 本地命令（不注入终端）──
            if cmd.startswith("/stop"):
                logger.info("执行 /stop")
                send_interrupt()
                await sendmsg(client, tok, fu, "已发送中断信号", ct)
                continue
            if cmd.startswith("/imageloc"):
                parts = text.strip().split(maxsplit=1)
                if len(parts) > 1:
                    new_path = Path(parts[1]); new_path.mkdir(parents=True, exist_ok=True)
                    IMAGES_DIR = new_path
                    out = f"图片保存路径已设为：{IMAGES_DIR}"
                else:
                    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
                    out = f"当前图片保存路径：{IMAGES_DIR}"
                logger.info(f"执行 /imageloc path={IMAGES_DIR}")
                await sendmsg(client, tok, fu, out, ct)
                continue
            if cmd == "/model":
                logger.info("执行 /model")
                import os
                sp = Path.home() / ".claude" / "settings.json"
                cur_alias = "sonnet"
                if sp.exists():
                    try: cur_alias = json.loads(sp.read_text()).get("model", "sonnet")
                    except:
                        logger.debug("读取 settings.json 失败")
                cur_full = os.environ.get("ANTHROPIC_MODEL", cur_alias)
                out = f"当前: {cur_alias} ({cur_full})\n\n模型列表：\n0. 取消\n"
                model_map = {0: None}
                for i, (alias, label) in enumerate([("opus","Opus"),("sonnet","Sonnet"),("haiku","Haiku")], 1):
                    full = os.environ.get(f"ANTHROPIC_DEFAULT_{alias.upper()}_MODEL", alias)
                    mk = " ← 当前" if cur_alias.lower() == alias.lower() else ""
                    out += f"{i}. {label}: {full}{mk}\n"
                    model_map[i] = alias
                _model_map = model_map
                awaiting_model_select = True
                logger.info(f"模型列表已发送 cur={cur_alias}")
                await sendmsg(client, tok, fu, out, ct)
                continue
            if cmd.startswith("/status"):
                logger.info("执行 /status")
                try:
                    s = get_session_stats()
                    out = format_stats(s)
                    await sendmsg(client, tok, fu, out, ct)
                except Exception as e:
                    logger.exception("/status 失败")
                    await sendmsg(client, tok, fu, f"/status 失败: {e}", ct)
                continue
            if cmd.startswith("/usage"):
                logger.info("执行 /usage")
                try:
                    s = get_session_stats()
                    out = format_usage(s)
                    await sendmsg(client, tok, fu, out, ct)
                except Exception as e:
                    logger.exception("/usage 失败")
                    await sendmsg(client, tok, fu, f"/usage 失败: {e}", ct)
                continue
            if cmd.startswith("/cost"):
                logger.info("执行 /cost")
                try:
                    s = get_session_stats()
                    out = format_cost(s)
                    await sendmsg(client, tok, fu, out, ct)
                except Exception as e:
                    logger.exception("/cost 失败")
                    await sendmsg(client, tok, fu, f"/cost 失败: {e}", ct)
                continue
            if cmd.startswith("/permissions"):
                logger.info("执行 /permissions")
                try:
                    parts = text.strip().split(maxsplit=1)
                    sp = Path.home() / ".claude" / "settings.local.json"
                    if not sp.exists():
                        sp.write_text("{}", encoding="utf-8")
                    try:
                        sp_data = json.loads(sp.read_text(encoding="utf-8"))
                        perm = sp_data.get("permissions", {})
                    except:
                        logger.debug("读取 settings.local.json 失败，使用空配置")
                        sp_data = {}; perm = {}
                    allow = perm.get("allow", [])
                    deny = perm.get("deny", [])
                    cur_mode = sp_data.get("permissionMode", "default")

                    MODES = [
                        ("default", "每次询问权限"),
                        ("auto", "自动接受安全操作"),
                        ("acceptEdits", "自动接受编辑"),
                        ("bypassPermissions", "绕过所有权限"),
                        ("plan", "仅规划模式"),
                        ("dontAsk", "不询问"),
                    ]
                    MODE_MAP = {m[0]: m[1] for m in MODES}

                    if len(parts) > 1:
                        # /permissions <mode> — 直接切换
                        arg = parts[1].strip().lower()
                        matched = None
                        for m, _ in MODES:
                            if m.lower() == arg or m.lower().startswith(arg):
                                matched = m; break
                        if matched:
                            sp_data["permissionMode"] = matched
                            sp.write_text(json.dumps(sp_data, indent=2, ensure_ascii=False), encoding="utf-8")
                            await sendmsg(client, tok, fu, f"权限模式已切换至: {matched} ({MODE_MAP[matched]})", ct)
                        else:
                            await sendmsg(client, tok, fu, f"未知模式: {arg}\n可用: {', '.join(m[0] for m in MODES)}", ct)
                    else:
                        # /permissions — 显示当前状态 + 模式列表
                        lines = [f"当前权限模式: {cur_mode} ({MODE_MAP.get(cur_mode, '?')})"]
                        if allow:
                            lines.append(f"\n允许 ({len(allow)}): {', '.join(allow[:8])}")
                            if len(allow) > 8:
                                lines.append(f"  ... 还有 {len(allow)-8} 项")
                        if deny:
                            lines.append(f"\n禁止 ({len(deny)}): {', '.join(deny[:8])}")
                        lines.append("\n切换模式（回复数字）：\n0. 取消")
                        for i, (m, desc) in enumerate(MODES, 1):
                            mk = " ← 当前" if m == cur_mode else ""
                            lines.append(f"{i}. {m} - {desc}{mk}")
                        out = "\n".join(lines)
                        _perm_mode_map = {i: m for i, (m, _) in enumerate(MODES, 1)}
                        _perm_mode_map[0] = None
                        awaiting_permissions_select = True
                        _permissions_map = _perm_mode_map
                        await sendmsg(client, tok, fu, out, ct)
                except Exception as e:
                    logger.exception("/permissions 失败")
                    await sendmsg(client, tok, fu, f"/permissions 失败: {e}", ct)
                continue
            if cmd.startswith("/agents"):
                logger.info("执行 /agents")
                try:
                    import subprocess, shutil
                    claude = shutil.which("claude") or "claude"
                    proc = subprocess.run([claude, "agents"], capture_output=True,
                                          text=True, timeout=CONFIG["timeouts"]["subprocess"], encoding="utf-8")
                    out = proc.stdout.strip() or proc.stderr.strip() or "(无输出)"
                    await sendmsg(client, tok, fu, out, ct)
                except FileNotFoundError:
                    await sendmsg(client, tok, fu, "未找到 claude 命令", ct)
                except Exception as e:
                    logger.exception("/agents 失败")
                    await sendmsg(client, tok, fu, f"/agents 失败: {e}", ct)
                continue
            if cmd.startswith("/plugins"):
                logger.info("执行 /plugins")
                try:
                    import subprocess, shutil
                    claude = shutil.which("claude") or "claude"
                    proc = subprocess.run([claude, "plugin", "list"], capture_output=True,
                                          text=True, timeout=CONFIG["timeouts"]["subprocess"], encoding="utf-8")
                    out = proc.stdout.strip() or proc.stderr.strip() or "(无输出)"
                    await sendmsg(client, tok, fu, out, ct)
                except FileNotFoundError:
                    await sendmsg(client, tok, fu, "未找到 claude 命令", ct)
                except Exception as e:
                    logger.exception("/plugins 失败")
                    await sendmsg(client, tok, fu, f"/plugins 失败: {e}", ct)
                continue
            if cmd.startswith("/mcp"):
                logger.info("执行 /mcp")
                try:
                    import subprocess, shutil
                    claude = shutil.which("claude") or "claude"
                    proc = subprocess.run([claude, "mcp", "list"], capture_output=True,
                                          text=True, timeout=CONFIG["timeouts"]["subprocess"], encoding="utf-8")
                    out = proc.stdout.strip() or proc.stderr.strip() or "(无输出)"
                    await sendmsg(client, tok, fu, out, ct)
                except FileNotFoundError:
                    await sendmsg(client, tok, fu, "未找到 claude 命令", ct)
                except Exception as e:
                    logger.exception("/mcp 失败")
                    await sendmsg(client, tok, fu, f"/mcp 失败: {e}", ct)
                continue
            if cmd.startswith("/help"):
                logger.info("执行 /help")
                out = """CCtoWechat 命令

== 统计 ==
/status — 会话统计（模型、tokens）
/usage  — token 用量详情
/cost   — 费用估算

== 会话 ==
/resume  — 会话列表 + 选号切换
/clear   — 清空上下文
/compact — 压缩上下文

== 配置 ==
/model       — 查看/切换模型
/permissions — 查看/切换权限模式
/agents      — 查看可用 agent
/plugins     — 已安装插件
/mcp         — MCP 服务器

== 控制 ==
/stop — 中断 Claude 当前操作
/summaries — 开关 AI 会话摘要
/imageloc [路径] — 图片保存路径

== 其他 ==
/help — 此帮助""".strip()
                await sendmsg(client, tok, fu, out, ct)
                continue
            if cmd.startswith("/summaries"):
                on = toggle_summaries()
                logger.info(f"执行 /summaries 切换至 {'开启' if on else '关闭'}")
                await sendmsg(client, tok, fu, f"AI 摘要已{'开启' if on else '关闭'}", ct)
                continue
            # ── /resume 特殊处理 ──
            if cmd.startswith("/resume"):
                logger.info("执行 /resume")
                inject_to_terminal(text)
                cur_sid = sessions_jsonl().stem if sessions_jsonl() else ""
                out = "可用会话：\n" + get_session_list(exclude_sid=cur_sid)
                out += "\n\n回复 0 取消，回复数字选择会话"
                awaiting_session_select = True
                await sendmsg(client, tok, fu, out[:1500], ct)
                continue
            # ── 通用命令穿透：注入 → 等 Claude 回复 → 发回 ──
            logger.info(f"穿透命令: {cmd}")
            inject_to_terminal(text)
            jsonl = sessions_jsonl()
            sp = jsonl.stat().st_size if jsonl else 0
            reply = await _wait_with_think(client, tok, fu, ct, jsonl, text, sp, phase1_timeout=120)
            if reply:
                ok = await sendmsg(client, tok, fu, reply[:1500], ct)
                logger.info(f"命令回复已发送 {'OK' if ok else 'FAIL'} len={len(reply)}")
            else:
                await sendmsg(client, tok, fu, f"已执行 {text}", ct)
                logger.warning("命令回复超时")
            continue

        jsonl = sessions_jsonl()
        sp = jsonl.stat().st_size if jsonl else 0

        inject_to_terminal(text)
        reply = await _wait_with_think(client, tok, fu, ct, jsonl, text, sp)
        if reply:
            ok = await sendmsg(client, tok, fu, reply, ct)
            if ok:
                logger.info(f"回复已发送 len={len(reply)}")
            else:
                logger.warning("回复发送失败")
        else:
            logger.warning("回复超时 (文本消息)")

# ── main ──
async def main():
    global last_user, tok_g, cli_g
    setup_logging()
    logger.info("CCtoWechat 启动")
    parser = argparse.ArgumentParser()
    parser.add_argument("--session")
    args = parser.parse_args()

    print("=" * 36)
    print("  WeChat + Claude Bridge v4")
    print("=" * 36)

    # session lock（仅当显式指定 --session 时才锁定）
    if args.session:
        j = SESSION_DIR / f"{args.session}.jsonl"
        if j.exists():
            set_locked_jsonl(j)
            logger.info(f"会话已锁定: {args.session}")
        else:
            logger.warning(f"会话不存在: {args.session}")
    else:
        logger.info("自动跟随最新会话")

    print(f" 会话目录: {SESSION_DIR}")
    srv = await start_http()

    if CRED_FILE.exists():
        creds = json.loads(CRED_FILE.read_text(encoding="utf-8"))
        logger.info(f"已加载凭证 bot_id={creds.get('bot_id','?')}")
    else:
        creds = await login()
        if creds is None: return

    tok = creds["token"]; tok_g = tok

    if LAST_USER_FILE.exists():
        try: last_user = json.loads(LAST_USER_FILE.read_text(encoding="utf-8"))
        except:
            logger.debug("无法加载 last_user.json")

    buf = ""
    if BUF_FILE.exists():
        try: buf = json.loads(BUF_FILE.read_text()).get("buf", "")
        except:
            logger.debug("无法加载 cursor.json")

    async with httpx.AsyncClient(proxy=None, trust_env=False) as client:
        cli_g = client
        asyncio.create_task(check_approval())  # 启动审核监听
        print(" 监听中...\n")
        ec = 0
        net_ec = 0  # 网络异常计数（独立于 API 错误）
        short = CONFIG["retry"]["backoff_short"]
        long = CONFIG["retry"]["backoff_long"]
        thresh = CONFIG["retry"]["threshold"]
        while True:
            try:
                resp = await getupdates(client, tok, buf)
                errc = resp.get("errcode", resp.get("ret", 0))
                if errc != 0:
                    ec += 1
                    if errc == -14:
                        # 真正的 session 过期 → 必须重新登录
                        CRED_FILE.unlink(missing_ok=True)
                        logger.warning("Session 已过期，重新登录")
                        creds = await login()
                        if creds is None: logger.error("重新登录失败，退出"); return
                        tok = creds["token"]; tok_g = tok
                        ec = 0; net_ec = 0
                        logger.info("重新登录成功")
                        continue
                    # 其他 API 错误 → 退避重试，不重新登录
                    w = short if ec <= thresh else long
                    logger.warning(f"API 错误 errcode={errc} {w}s后重试 (连续{ec}次)")
                    await asyncio.sleep(w); continue
                ec = 0; net_ec = 0
                for msg in resp.get("msgs", []):
                    asyncio.create_task(handle(client, tok, msg))
                nb = resp.get("get_updates_buf", "")
                if nb and nb != buf: buf = nb; BUF_FILE.write_text(json.dumps({"buf": nb}))
            except KeyboardInterrupt: print("\n 停止"); break
            except Exception:
                ec += 1; net_ec += 1
                w = short if ec <= thresh else long
                logger.exception(f"主循环异常 (连续{ec}次, 网络{net_ec}次)")
                # 网络异常超过 30 次（约 15 分钟）→ 可能是 DNS/网络变更，尝试重新登录
                if net_ec >= 30:
                    CRED_FILE.unlink(missing_ok=True)
                    logger.warning("长时间网络异常，尝试重新登录")
                    creds = await login()
                    if creds is None: logger.error("重新登录失败，退出"); return
                    tok = creds["token"]; tok_g = tok
                    ec = 0; net_ec = 0; logger.info("重新登录成功")
                await asyncio.sleep(w)
    srv.close()

if __name__ == "__main__":
    asyncio.run(main())
