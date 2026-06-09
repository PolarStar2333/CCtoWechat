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

import argparse, asyncio, httpx, json, base64, hashlib, random, time, re, logging, sys, os, subprocess, shutil, secrets
from pathlib import Path

from logger_setup import setup_logging
from config import CONFIG
from inject import inject_to_terminal, select_session, send_interrupt, inject_enter
from sessions import (init as sessions_init, set_locked_jsonl,
                      get_session_list, get_last_reply, wait_reply,
                      find_session_dir, toggle_summaries, set_summaries,
                      get_jsonl as sessions_jsonl,
                      get_session_stats, format_stats, format_usage, format_cost,
                      reset_stream_buffer)
from selector import format_questions, validate_answer, inject_answers, format_confirmation
from auditlog import audit, get_log_text

logger = logging.getLogger("bridge")

API_BASE = CONFIG["api_base"]
CH_VERSION = CONFIG["channel_version"]

ROOT = Path(__file__).parent
CRED_FILE = ROOT / "credentials.json"
BUF_FILE = ROOT / "cursor.json"
LAST_USER_FILE = ROOT / "last_user.json"
APPROVAL_FILE = ROOT / "approval_pending.json"
STATE_FILE = ROOT / "state.json"

# ── 持久化状态 ──
def _load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            logger.debug("加载 state.json 失败", exc_info=True)
    return {}

def _save_state(**kw):
    state = _load_state()
    state.update(kw)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

SESSION_DIR = find_session_dir()
sessions_init(SESSION_DIR, ROOT)

# 从持久化状态恢复开关
_st = _load_state()
if not _st.get("ai_summaries", True):
    set_summaries(False)

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
    ok = r.status_code == 200
    audit("tx_msg", chars=len(text), lines=text.count("\n") + 1, ok=ok)
    return ok


def _decrypt_media(data, key_material):
    """AES 解密 iLink 媒体数据。自动识别密钥格式，尝试 ECB/CBC，PKCS7 去填充。
    解密失败返回原始数据。"""
    if not key_material:
        return data
    key = None
    try:
        key = bytes.fromhex(key_material)
    except Exception:
        pass
    if key is None:
        try:
            key = bytes.fromhex(base64.b64decode(key_material).decode("ascii"))
        except Exception:
            pass
    if key is None:
        try:
            key = base64.b64decode(key_material)
        except Exception:
            pass
    if key is None:
        return data
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        return data
    last = data
    for cipher in [
        Cipher(algorithms.AES(key), modes.ECB()),
        Cipher(algorithms.AES(key), modes.CBC(bytes(16))),
    ]:
        try:
            d = cipher.decryptor().update(data) + cipher.decryptor().finalize()
            pad = d[-1]
            if 0 < pad <= 16 and all(b == pad for b in d[-pad:]):
                return d[:-pad]
            last = d
        except Exception:
            continue
    return last


async def _upload_media(client, tok, to_user, file_bytes, media_type):
    """上传媒体到 CDN，返回 (download_param, aes_key, raw_size, enc_size) 或 (None, None, 0, 0)

    media_type (iLink): 1=IMAGE 2=VIDEO 3=FILE
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    raw_size = len(file_bytes)
    raw_md5 = hashlib.md5(file_bytes).hexdigest()
    filekey = secrets.token_hex(16)
    aes_key = secrets.token_bytes(16)

    # AES-128-ECB + PKCS7 加密
    pad_len = 16 - (raw_size % 16)
    padded = file_bytes + bytes([pad_len] * pad_len)
    cipher = Cipher(algorithms.AES(aes_key), modes.ECB())
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()

    # getuploadurl：aeskey = hex(raw_key) 对所有类型
    r = await client.post(f"{API_BASE}/ilink/bot/getuploadurl",
        json={
            "base_info": {"channel_version": CH_VERSION},
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user,
            "rawsize": raw_size,
            "rawfilemd5": raw_md5,
            "filesize": len(encrypted),
            "no_need_thumb": True,
            "aeskey": aes_key.hex(),
        },
        headers=_hdrs(tok), timeout=CONFIG["timeouts"]["upload"])
    if r.status_code != 200:
        logger.warning(f"getuploadurl 失败 HTTP {r.status_code}")
        return None, None, 0, 0
    up_info = r.json()
    logger.info(f"getuploadurl 响应: {json.dumps(up_info, default=str)[:500]}")

    # 上传 URL：upload_full_url 优先，其次 upload_param 拼接
    upload_url = up_info.get("upload_full_url", "")
    if not upload_url:
        upload_param = up_info.get("upload_param", "")
        if upload_param:
            cdn_base = up_info.get("cdn_base_url", "https://novac2c.cdn.weixin.qq.com/c2c")
            upload_url = f"{cdn_base.rstrip('/')}/upload?encrypted_query_param={upload_param}&filekey={filekey}"
    if not upload_url:
        logger.warning("getuploadurl 未返回有效上传 URL")
        return None, None, 0, 0

    # POST 加密数据到 CDN
    logger.info(f"开始上传 CDN size={len(encrypted)}")
    r2 = await client.post(upload_url, content=encrypted,
        headers={"Content-Type": "application/octet-stream"},
        timeout=CONFIG["timeouts"]["upload"])
    logger.info(f"CDN 上传响应 HTTP {r2.status_code}")
    if r2.status_code != 200:
        logger.warning(f"CDN 上传失败 HTTP {r2.status_code} body={r2.text[:200]}")
        return None, None, 0, 0

    download_param = r2.headers.get("x-encrypted-param", "")
    if not download_param:
        logger.warning("CDN 上传响应缺少 x-encrypted-param")
        return None, None, 0, 0

    return download_param, aes_key, raw_size, len(encrypted)


def _aeskey_for_msg(aes_key):
    """sendmessage 中的 aes_key 格式：base64(hex(raw_key))，所有类型统一"""
    return base64.b64encode(aes_key.hex().encode()).decode()


async def send_image(client, tok, to_user, ctx_token, image_bytes):
    """发送图片：AES-128-ECB 加密 → 上传 CDN → sendmessage"""
    download_param, aes_key, raw_size, enc_size = await _upload_media(
        client, tok, to_user, image_bytes, 1)  # 1 = IMAGE
    if not download_param:
        return False

    cid = f"c{int(time.time()*1000)}"
    r = await client.post(f"{API_BASE}/ilink/bot/sendmessage", json={
        "msg": {"to_user_id": to_user, "client_id": cid, "message_type": 2,
                "message_state": 2, "context_token": ctx_token,
                "item_list": [{"type": 2, "image_item": {
                    "media": {
                        "encrypt_query_param": download_param,
                        "aes_key": _aeskey_for_msg(aes_key),
                        "encrypt_type": 1,
                    },
                    "mid_size": enc_size,
                }}]},
        "base_info": {"channel_version": CH_VERSION}}, headers=_hdrs(tok), timeout=CONFIG["timeouts"]["short"])
    return r.status_code == 200


async def send_file(client, tok, to_user, ctx_token, file_bytes, file_name):
    """发送文件：AES-128-ECB 加密 → 上传 CDN → sendmessage"""
    download_param, aes_key, raw_size, enc_size = await _upload_media(
        client, tok, to_user, file_bytes, 3)  # 3 = FILE
    if not download_param:
        return False

    cid = f"c{int(time.time()*1000)}"
    r = await client.post(f"{API_BASE}/ilink/bot/sendmessage", json={
        "msg": {"to_user_id": to_user, "client_id": cid, "message_type": 2,
                "message_state": 2, "context_token": ctx_token,
                "item_list": [{"type": 4, "file_item": {
                    "media": {
                        "encrypt_query_param": download_param,
                        "aes_key": _aeskey_for_msg(aes_key),
                        "encrypt_type": 1,
                    },
                    "file_name": file_name,
                    "len": str(raw_size),
                }}]},
        "base_info": {"channel_version": CH_VERSION}}, headers=_hdrs(tok), timeout=CONFIG["timeouts"]["short"])
    return r.status_code == 200

async def send_video(client, tok, to_user, ctx_token, video_bytes):
    """发送视频：AES-128-ECB 加密 → 上传 CDN → sendmessage"""
    download_param, aes_key, raw_size, enc_size = await _upload_media(
        client, tok, to_user, video_bytes, 2)  # 2 = VIDEO
    if not download_param:
        return False

    cid = f"c{int(time.time()*1000)}"
    r = await client.post(f"{API_BASE}/ilink/bot/sendmessage", json={
        "msg": {"to_user_id": to_user, "client_id": cid, "message_type": 2,
                "message_state": 2, "context_token": ctx_token,
                "item_list": [{"type": 5, "video_item": {
                    "media": {
                        "encrypt_query_param": download_param,
                        "aes_key": _aeskey_for_msg(aes_key),
                        "encrypt_type": 1,
                    },
                    "video_size": enc_size,
                }}]},
        "base_info": {"channel_version": CH_VERSION}}, headers=_hdrs(tok), timeout=CONFIG["timeouts"]["short"])
    return r.status_code == 200

# ── HTTP /send ──
last_user = None
tok_g = None
cli_g = None

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
                        audit("push", action="主动发文本", chars=len(payload["text"]), lines=payload["text"].count("\n") + 1)
                        logger.info(f"主动发送文本 {'OK' if ok else 'FAIL'} chars={len(payload['text'])}")
                    elif "image_path" in payload:
                        img = Path(payload["image_path"]).read_bytes()
                        ok = await send_image(cli_g, tok_g, to, ct, img)
                        audit("push", action="主动发图", chars=len(img), lines=0)
                        logger.info(f"主动发图 {'OK' if ok else 'FAIL'} path={payload['image_path']}")
                    elif "file_path" in payload:
                        fp = Path(payload["file_path"])
                        data = fp.read_bytes()
                        name = payload.get("file_name", fp.name)
                        ok = await send_file(cli_g, tok_g, to, ct, data, name)
                        audit("push", action="主动发文件", chars=len(data), lines=0)
                        logger.info(f"主动发文件 {'OK' if ok else 'FAIL'} path={payload['file_path']}")
                    elif "video_path" in payload:
                        vid = Path(payload["video_path"]).read_bytes()
                        ok = await send_video(cli_g, tok_g, to, ct, vid)
                        audit("push", action="主动发视频", chars=len(vid), lines=0)
                        logger.info(f"主动发视频 {'OK' if ok else 'FAIL'} path={payload['video_path']}")
    except Exception:
        logger.exception("HTTP handler 异常")
    finally:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK"); await writer.drain(); writer.close()

async def start_http():
    srv = await asyncio.start_server(http_handler, CONFIG["http_host"], CONFIG["http_port"])
    print(f" HTTP: http://{CONFIG['http_host']}:{CONFIG['http_port']}  /qr=scan")
    return srv

# ── API 响应通知 ──
async def _wait_with_think(client, tok, fu, ct, jsonl, text, **kw):
    """等待 Claude 回复，三阶段信号通知微信 + 自动流式推送"""
    reset_stream_buffer()  # 新一轮监听开始
    _st = _load_state()
    sitpull = _st.get("sitpulltime", 2000)
    async def on_user():
        try: await sendmsg(client, tok, fu, "思考中...", ct)
        except Exception: pass
    async def on_tool():
        try: await sendmsg(client, tok, fu, "使用工具...", ct)
        except Exception: pass
    async def on_respond():
        try: await sendmsg(client, tok, fu, "回复中...", ct)
        except Exception: pass
    async def on_stream(delta):
        try: await sendmsg(client, tok, fu, delta, ct)
        except Exception: pass
    async def on_question(questions):
        global awaiting_question_answer, _pending_questions
        _pending_questions = questions
        msg = format_questions(questions)
        try: await sendmsg(client, tok, fu, msg, ct)
        except Exception: pass
        awaiting_question_answer = True
    return await wait_reply(jsonl, text,
        on_user_found=on_user, on_tool_use=on_tool,
        on_first_respond=on_respond,
        on_ask_user_question=on_question,
        on_stream_chunk=on_stream, stream_interval=sitpull, **kw)


async def _wait_and_reply(client, tok, fu, ct, text, **kw):
    """等待 Claude 回复 → 发回微信（注入由调用方完成）"""
    jsonl = sessions_jsonl()
    reply = await _wait_with_think(client, tok, fu, ct, jsonl, text, **kw)
    if reply:
        ok = await sendmsg(client, tok, fu, reply[:1500], ct)
        logger.info(f"回复已发送 {'OK' if ok else 'FAIL'} len={len(reply)}")
    else:
        logger.warning(f"回复超时 chars={len(text)}")
    return reply


async def _inject_and_wait(client, tok, fu, ct, text, **kw):
    """注入终端 → 等待回复 → 发回微信"""
    inject_to_terminal(text)
    return await _wait_and_reply(client, tok, fu, ct, text, **kw)


async def _claude_subcmd(client, tok, fu, ct, args, label):
    """运行 claude 子命令并发送输出到微信"""
    logger.info(f"执行 {label}")
    try:
        claude = shutil.which("claude") or "claude"
        proc = subprocess.run([claude] + args, capture_output=True,
                              text=True, timeout=CONFIG["timeouts"]["subprocess"], encoding="utf-8")
        out = proc.stdout.strip() or proc.stderr.strip() or "(无输出)"
        await sendmsg(client, tok, fu, out, ct)
    except FileNotFoundError:
        await sendmsg(client, tok, fu, "未找到 claude 命令", ct)
    except Exception as e:
        logger.exception(f"{label} 失败")
        await sendmsg(client, tok, fu, f"{label} 失败: {e}", ct)


async def _send_stats(client, tok, fu, ct, formatter, label):
    """获取会话统计 → 格式化 → 发送到微信"""
    logger.info(f"执行 {label}")
    try:
        s = get_session_stats()
        out = formatter(s)
        await sendmsg(client, tok, fu, out, ct)
    except Exception as e:
        logger.exception(f"{label} 失败")
        await sendmsg(client, tok, fu, f"{label} 失败: {e}", ct)


# ── 审核模式 ──
pending_approval = False
last_notified = ""  # 上次审核通知内容，用于去重
# ── 交互式选项 ──
awaiting_question_answer = False  # Claude AskUserQuestion 等待用户回答
_pending_questions = []            # 当前问题列表，供答案映射用
_answers_injected = False          # 答案已注入，等待确认提交/取消
_debug_mode = _load_state().get("debug_mode", False)  # debug 模式开关（持久化）
_awaiting_debug_confirm = False    # 等待确认 /debug

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


async def _receive_media(client, tok, fu, ct, url, aes_key, suffix, *,
                          media_type="图片", is_file=False, subdir=None,
                          process=None):
    """下载 → 解密 → 写盘 → 审计 → 通知（图片/文件接收共享流程）"""
    save_dir = IMAGES_DIR
    if subdir:
        save_dir = save_dir / subdir
    save_dir.mkdir(parents=True, exist_ok=True)
    save_path = save_dir / f"{int(time.time())}{suffix}"

    logger.info(f"收到{media_type} url={url[:80]}")
    await sendmsg(client, tok, fu, f"收到{media_type}，下载解密中...", ct)

    try:
        r = await client.get(url, timeout=CONFIG["timeouts"]["download"])
        if r.status_code != 200:
            await sendmsg(client, tok, fu, f"下载失败: HTTP {r.status_code}", ct)
            return False
        data = r.content
        if media_type == "图片":
            save_path.with_suffix(".raw").write_bytes(data)
            save_path.with_suffix(".key").write_text(aes_key, encoding="utf-8")
        data = _decrypt_media(data, aes_key)
        if process:
            data = process(data)
        save_path.write_bytes(data)

        audit("media", action="收到", type=media_type, size=len(data), encoding="utf-8")
        global pending_image_path, pending_is_file, awaiting_image_action
        pending_image_path = str(save_path)
        pending_is_file = is_file
        awaiting_image_action = True
        prompt = f"{media_type}已保存 ({len(data)//1024}KB)\n\n回复 /yes 仅注入{'路径' if is_file else '图片地址'}\n回复其他内容将一并注入"
        await sendmsg(client, tok, fu, prompt, ct)
        return True
    except Exception as e:
        await sendmsg(client, tok, fu, f"处理失败: {e}", ct)
        return False


def _is_remote_cmd(text):
    """只匹配纯英文 slash 命令（/字母开头，不含中文等非ASCII）"""
    t = text.strip()
    if not t.startswith("/"):
        return False
    first = t.split()[0].lower()
    return re.match(r'^/[a-zA-Z][a-zA-Z0-9_-]*$', first) is not None

async def handle(client, tok, raw):
    global last_user, pending_approval, awaiting_question_answer, _pending_questions, _answers_injected, _debug_mode, _awaiting_debug_confirm
    global awaiting_session_select, awaiting_model_select, _model_map, IMAGES_DIR
    global awaiting_image_action, pending_image_path, pending_is_file
    global awaiting_permissions_select, _permissions_map
    if isinstance(raw, str):
        try: msg = json.loads(raw)
        except Exception:
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

            def _jpeg_trim(data):
                eoi = data.rfind(b'\xff\xd9')
                if eoi > 0 and data[:2] == b'\xff\xd8':
                    return data[:eoi + 2]
                return data

            await _receive_media(client, tok, fu, ct, img_url, aeskey_hex, ".jpg",
                                 media_type="图片", is_file=False, process=_jpeg_trim)
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

            subdir = "files"
            suffix = Path(file_name).suffix or ".bin"
            await _receive_media(client, tok, fu, ct, file_url, aeskey_b64, suffix,
                                 media_type="文件", is_file=True, subdir=subdir)
            continue
        if item_type != 1: continue
        text = item.get("text_item", {}).get("text", "").strip()

        # 引用消息处理
        ref = item.get("ref_msg", {})
        if ref:
            ref_item = ref.get("message_item", {})
            ref_type = ref_item.get("type", 0)
            if ref_type == 1:
                ref_text = ref_item.get("text_item", {}).get("text", "")
                if ref_text:
                    prefix = f"「{ref_text.strip()}」"
                    text = f"{prefix}{text}" if text else prefix
            elif ref_type in (2, 4):
                # 引用文件/图片 → 下载解密，路径拼到消息前
                try:
                    is_img = ref_type == 2
                    if is_img:
                        ri = ref_item.get("image_item", {})
                        media = ri.get("media", {})
                        dl_url = media.get("full_url", ri.get("url", ""))
                        aeskey_raw = ri.get("aeskey", "")
                        suffix = ".jpg"
                        label = "图片引用"
                    else:
                        ri = ref_item.get("file_item", {})
                        media = ri.get("media", {})
                        dl_url = media.get("full_url", "")
                        aeskey_raw = media.get("aes_key", "")
                        suffix = Path(ri.get("file_name", "unknown")).suffix or ".bin"
                        label = "文件引用"
                    if dl_url:
                        rdl = await client.get(dl_url, timeout=CONFIG["timeouts"]["download"])
                        if rdl.status_code == 200:
                            data = rdl.content
                            data = _decrypt_media(data, aeskey_raw)
                            # JPEG 引用截断到 EOI
                            if is_img:
                                eoi = data.rfind(b'\xff\xd9')
                                if eoi > 0 and data[:2] == b'\xff\xd8':
                                    data = data[:eoi + 1]
                            ref_path = IMAGES_DIR / f"ref_{int(time.time())}{suffix}"
                            ref_path.parent.mkdir(exist_ok=True)
                            ref_path.write_bytes(data)
                            prefix = str(ref_path)
                            text = f"{prefix} {text}" if text else prefix
                            logger.info(f"{label}已下载 path={ref_path}")
                except Exception:
                    logger.debug("引用文件下载失败", exc_info=True)

        audit("rx_msg", type="text", chars=len(text), lines=text.count("\n") + 1,
             encoding="utf-8", has_ref=bool(item.get("ref_msg")))
        logger.info(f"收到文本消息 from={fu[:20]} chars={len(text)} lines={text.count(chr(10))+1} has_ref={bool(item.get('ref_msg'))}")
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
                except Exception:
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
                await _wait_and_reply(client, tok, fu, ct, msg, phase1_timeout=120)
                continue
            # 其他回复 → 路径 + 文字一起注入
            inject_to_terminal(f"[微信{label}: {safe_path}]\n\n{text}")
            logger.info(f"{label}+文字已注入 path={pending_image_path}")
            await _wait_and_reply(client, tok, fu, ct, text)
            continue

        # ── /debug 确认 ──
        if _awaiting_debug_confirm:
            t = text.strip().lower()
            if t.startswith("/yes") or t.startswith("/y"):
                _awaiting_debug_confirm = False
                _debug_mode = True
                _save_state(debug_mode=True)
                await sendmsg(client, tok, fu, "Debug 模式已开启\n\n/restart — 重启桥\n/debugoff — 退出 debug 模式", ct)
                continue
            elif t.startswith("/no") or t.startswith("/n"):
                _awaiting_debug_confirm = False
                await sendmsg(client, tok, fu, "已取消", ct)
                continue
            else:
                _awaiting_debug_confirm = False
                # 不是 yes/no，照常处理

        # 审核模式：只有以 /yes 或 /no 开头的消息才当审核处理
        if pending_approval and text.strip().lower().startswith(("/yes", "/no", "/y", "/n")):
            pending_approval = False; last_notified = ""
            answer = "yes" if text.strip().lower().startswith(("/yes", "/y")) else "no"
            logger.info(f"审核回复已注入 answer={answer}")
            inject_to_terminal(answer)
            continue
        elif pending_approval:
            # 不是审核回复，清掉标记正常处理
            pending_approval = False; last_notified = ""

        # 远程命令：所有 / 开头直接穿透给终端
        if _is_remote_cmd(text):
            cmd = text.strip().lower()
            cmd_word = cmd.split()[0]
            audit("cmd", cmd=cmd)
            # ── CCtoWechat 本地命令（不注入终端）──
            if cmd_word == "/stop":
                logger.info("执行 /stop")
                send_interrupt()
                awaiting_question_answer = False
                _pending_questions = []
                _answers_injected = False
                awaiting_session_select = False
                awaiting_model_select = False
                awaiting_permissions_select = False
                pending_approval = False; last_notified = ""
                awaiting_image_action = False
                await sendmsg(client, tok, fu, "已发送中断信号", ct)
                continue
            if cmd_word == "/imageloc":
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
                sp = Path.home() / ".claude" / "settings.json"
                cur_alias = "sonnet"
                if sp.exists():
                    try: cur_alias = json.loads(sp.read_text()).get("model", "sonnet")
                    except Exception:
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
            if cmd_word == "/status":
                await _send_stats(client, tok, fu, ct, format_stats, "/status")
                continue
            if cmd_word == "/usage":
                await _send_stats(client, tok, fu, ct, format_usage, "/usage")
                continue
            if cmd_word == "/cost":
                await _send_stats(client, tok, fu, ct, format_cost, "/cost")
                continue
            if cmd_word == "/permissions":
                logger.info("执行 /permissions")
                try:
                    parts = text.strip().split(maxsplit=1)
                    sp = Path.home() / ".claude" / "settings.local.json"
                    if not sp.exists():
                        sp.write_text("{}", encoding="utf-8")
                    try:
                        sp_data = json.loads(sp.read_text(encoding="utf-8"))
                        perm = sp_data.get("permissions", {})
                    except Exception:
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
            if cmd_word == "/agents":
                await _claude_subcmd(client, tok, fu, ct, ["agents"], "/agents")
                continue
            if cmd_word == "/plugins":
                await _claude_subcmd(client, tok, fu, ct, ["plugin", "list"], "/plugins")
                continue
            if cmd_word == "/mcp":
                await _claude_subcmd(client, tok, fu, ct, ["mcp", "list"], "/mcp")
                continue
            if cmd_word == "/help":
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
/submit — 提交交互式选项答案
/summaries — 开关 AI 会话摘要
/sitpulltime [ms] — 流式推送间隔
/imageloc [路径] — 图片保存路径

== 诊断 ==
/log — 发送审计日志（过去24h，仅元数据不含内容）

== 其他 ==
/help — 此帮助""".strip()
                await sendmsg(client, tok, fu, out, ct)
                continue
            if cmd_word == "/summaries":
                on = toggle_summaries()
                _save_state(ai_summaries=on)
                logger.info(f"执行 /summaries 切换至 {'开启' if on else '关闭'}")
                await sendmsg(client, tok, fu, f"AI 摘要已{'开启' if on else '关闭'}", ct)
                continue
            if cmd_word == "/sitpulltime":
                logger.info("执行 /sitpulltime")
                parts = text.strip().split(maxsplit=1)
                if len(parts) > 1:
                    try:
                        val = int(parts[1])
                        if 1000 <= val <= 60000:
                            _save_state(sitpulltime=val)
                            await sendmsg(client, tok, fu, f"流式推送间隔已设为 {val}ms", ct)
                        else:
                            await sendmsg(client, tok, fu, "间隔范围 1000-60000ms", ct)
                    except ValueError:
                        await sendmsg(client, tok, fu, "格式: /sitpulltime <毫秒数>  (1000-60000)", ct)
                else:
                    cur = _load_state().get("sitpulltime", 2000)
                    await sendmsg(client, tok, fu, f"当前流式推送间隔: {cur}ms\n\n修改: /sitpulltime <毫秒数>  (1000-60000)", ct)
                continue
            if cmd_word == "/log":
                logger.info("执行 /log")
                log_text = get_log_text()
                await sendmsg(client, tok, fu, log_text[:1500], ct)
                continue
            if cmd_word == "/submit":
                logger.info("执行 /submit")
                if awaiting_question_answer:
                    awaiting_question_answer = False
                    _pending_questions = []
                    _answers_injected = False
                    inject_enter()
                    await sendmsg(client, tok, fu, "已提交答案", ct)
                else:
                    await sendmsg(client, tok, fu, "当前没有待提交的选项", ct)
                continue
            if cmd_word == "/debug":
                logger.info("执行 /debug")
                _awaiting_debug_confirm = True
                await sendmsg(client, tok, fu, "确认启动 /debug 模式？这将同步开启 log\n\n回复 /yes 确认  /no 取消", ct)
                continue
            if cmd_word == "/debugoff":
                logger.info("执行 /debugoff")
                _debug_mode = False
                _save_state(debug_mode=False)
                await sendmsg(client, tok, fu, "Debug 模式已关闭", ct)
                continue
            if cmd_word == "/restart":
                logger.info("执行 /restart")
                if not _debug_mode:
                    await sendmsg(client, tok, fu, "当前模式不支持 /restart\n请先 /debug 开启 debug 模式", ct)
                    continue
                else:
                    await sendmsg(client, tok, fu, "正在重启桥...", ct)
                    audit("lifecycle", action="restart")
                    logger.info("用户触发 /restart，正在重启")
                    await asyncio.sleep(0.5)
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                continue
            # ── /resume 特殊处理 ──
            if cmd_word == "/resume":
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
            reply = await _inject_and_wait(client, tok, fu, ct, text, phase1_timeout=120)
            if not reply:
                await sendmsg(client, tok, fu, f"已执行 {text}", ct)
            continue

        # ── 交互式选项答案（非命令文本）──
        if awaiting_question_answer:
            t = text.strip()

            # 确认阶段快捷回复：1=提交 2=取消（优先于答案校验）
            if _answers_injected:
                if t == "1":
                    inject_enter()
                    awaiting_question_answer = False
                    _pending_questions = []
                    _answers_injected = False
                    try: await sendmsg(client, tok, fu, "已提交", ct)
                    except Exception: pass
                    continue
                if t == "2":
                    send_interrupt()
                    awaiting_question_answer = False
                    _pending_questions = []
                    _answers_injected = False
                    try: await sendmsg(client, tok, fu, "已取消", ct)
                    except Exception: pass
                    continue

            # 校验答案格式（A2B13C4 或纯数字）
            answers = validate_answer(t, _pending_questions)
            if answers:
                special = inject_answers(answers, _pending_questions)
                if special:
                    q_idx, kind = special
                    prefix = chr(ord('A') + q_idx)
                    awaiting_question_answer = False
                    _pending_questions = []
                    _answers_injected = False
                    try: await sendmsg(client, tok, fu, f"[{prefix}] 已选择「{kind}」", ct)
                    except Exception: pass
                else:
                    _answers_injected = True
                    try: await sendmsg(client, tok, fu, format_confirmation(answers, _pending_questions), ct)
                    except Exception: pass
                continue

            # 不匹配 → /stop 取消选择器，作为普通文本注入并等待回复
            send_interrupt()
            awaiting_question_answer = False
            _pending_questions = []
            _answers_injected = False
            time.sleep(0.5)
            logger.info(f"选择器取消，文本注入 chars={len(text)}")
            await _inject_and_wait(client, tok, fu, ct, text)
            continue

        await _inject_and_wait(client, tok, fu, ct, text)

# ── main ──
async def _relogin():
    """重新登录，返回新 token，失败返回 None"""
    global tok_g
    CRED_FILE.unlink(missing_ok=True)
    creds = await login()
    if creds is None:
        logger.error("重新登录失败，退出")
        return None
    tok_g = creds["token"]
    logger.info("重新登录成功")
    return creds["token"]


async def main():
    global last_user, tok_g, cli_g
    setup_logging()
    audit("lifecycle", action="start")
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
        except Exception:
            logger.debug("无法加载 last_user.json")

    buf = ""
    if BUF_FILE.exists():
        try: buf = json.loads(BUF_FILE.read_text()).get("buf", "")
        except Exception:
            logger.debug("无法加载 cursor.json")

    async with httpx.AsyncClient(proxy=None, trust_env=False) as client:
        cli_g = client
        asyncio.create_task(check_approval())  # 启动审核监听

        # 启动通知
        if last_user and tok:
            try:
                await sendmsg(client, tok, last_user.get("to_user", ""),
                              "桥已启动成功", last_user.get("ctx_token", ""))
                logger.info("启动通知已发送")
            except Exception:
                logger.debug("启动通知发送失败", exc_info=True)

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
                        logger.warning("Session 已过期，重新登录")
                        tok = await _relogin()
                        if tok is None: return
                        ec = 0; net_ec = 0
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
                    logger.warning("长时间网络异常，尝试重新登录")
                    tok = await _relogin()
                    if tok is None: return
                    ec = 0; net_ec = 0
                    continue
                await asyncio.sleep(w)
    srv.close()

if __name__ == "__main__":
    asyncio.run(main())
