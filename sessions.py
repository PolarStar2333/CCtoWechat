#!/usr/bin/env python3
# MIT License - Copyright (c) 2026 CCtoWechat
"""会话管理模块：AI 摘要、JSONL 读取、会话列表、回复抓取"""

import asyncio, json, re, time
from pathlib import Path


# ── 全局状态（由 bridge.py 初始化）──
_session_dir = None   # 当前项目目录
_root = None          # CCtoWechat 根目录
_locked_jsonl = None  # 锁定的 JSONL 文件
_use_ai_summaries = True  # AI 摘要开关


def init(session_dir, root, locked_jsonl=None):
    """由 bridge.py 调用来初始化模块状态"""
    global _session_dir, _root, _locked_jsonl
    _session_dir = session_dir
    _root = root
    _locked_jsonl = locked_jsonl


def set_locked_jsonl(path):
    global _locked_jsonl
    _locked_jsonl = path


def toggle_summaries():
    """切换 AI 摘要开关，返回新状态"""
    global _use_ai_summaries
    _use_ai_summaries = not _use_ai_summaries
    return _use_ai_summaries


# ── JSONL 基础操作 ──

def _jsonl():
    """全局扫描所有项目目录，找最近修改的 JSONL"""
    global _locked_jsonl
    if _locked_jsonl:
        return _locked_jsonl
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
    """提取 type=text 的内容，过滤 thinking/tool_use"""
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


def _read_new(jsonl_path, pos):
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(max(0, pos))
            return f.readlines(), f.tell()
    except Exception:
        return [], pos


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
    texts = []
    for ln in lines:
        try: obj = json.loads(ln)
        except: continue
        msg = obj.get("message") or obj
        if msg.get("role") != "assistant": continue
        if msg.get("stop_reason") in ("end_turn", "tool_use"):
            t = _text(msg)
            if t and len(t) > 2:
                texts.append(t)
    return texts


def _find_end_turn(lines):
    for i in range(len(lines) - 1, -1, -1):
        try: obj = json.loads(lines[i])
        except: continue
        msg = obj.get("message") or obj
        if msg.get("role") == "assistant" and msg.get("stop_reason") == "end_turn":
            if any(isinstance(c, dict) and c.get("type") == "text" for c in msg.get("content", [])):
                return i
    return None


def _all_jsonl():
    base = Path.home() / ".claude" / "projects"
    if not base.exists(): return []
    files = []
    for d in base.iterdir():
        if d.is_dir():
            files.extend(d.glob("*.jsonl"))
    return files


# ── 会话列表 ──

def get_session_list(exclude_sid=""):
    """返回格式化的会话列表字符串"""
    if not _session_dir or not _session_dir.exists():
        return "无会话"

    # 加载 AI 摘要
    summaries = {}
    if _use_ai_summaries and _root:
        sf = _root / "session_summaries.json"
        if sf.exists():
            try:
                summaries = json.loads(sf.read_text(encoding="utf-8"))
            except Exception:
                pass

    sessions = []
    for f in _session_dir.glob("*.jsonl"):
        if exclude_sid and f.stem == exclude_sid:
            continue
        # AI 摘要优先
        desc = summaries.get(f.stem, "")
        if not desc:
            # 回退：首条用户消息
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        obj = json.loads(line)
                        msg = obj.get("message") or obj
                        if msg.get("role") == "user":
                            content = msg.get("content", [{}]) or [{}]
                            if isinstance(content, list) and content:
                                text = content[0].get("text", "") if isinstance(content[0], dict) else str(content[0])
                            elif isinstance(content, str):
                                text = content
                            else:
                                continue
                            desc = re.sub(r'<[^>]+>', '', text).replace("\n", " ").strip()[:90]
                            break
            except Exception:
                pass
        if desc:
            sessions.append((f.stem, desc, f.stat().st_mtime))

    sessions.sort(key=lambda x: x[2], reverse=True)
    secs = time.time()
    lines = []
    for i, (sid, desc, mt) in enumerate(sessions[:10], 1):
        diff = secs - mt
        if diff < 60: ago = "刚刚"
        elif diff < 3600: ago = f"{int(diff/60)}分钟前"
        elif diff < 86400: ago = f"{int(diff/3600)}小时前"
        else: ago = f"{int(diff/86400)}天前"
        lines.append(f"  {i}. [{ago}] {desc}")
    return "\n".join(lines) if lines else "无会话"


# ── 回复读取 ──

def get_last_reply():
    """读取当前 JSONL 中最后一条 assistant end_turn 回复"""
    jsonl = _jsonl()
    if not jsonl: return ""
    try:
        with open(jsonl, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for i in range(len(lines) - 1, -1, -1):
            try:
                obj = json.loads(lines[i])
            except Exception:
                continue
            msg = obj.get("message") or obj
            if msg.get("role") == "assistant" and msg.get("stop_reason") == "end_turn":
                texts = []
                for c in msg.get("content", []):
                    if isinstance(c, dict) and c.get("type") == "text":
                        texts.append(c.get("text", ""))
                reply = "".join(texts)
                if reply.strip():
                    return reply.strip()
        return ""
    except Exception:
        return ""


# ── 回复等待（两阶段轮询）──

async def wait_reply(jsonl, inject_text, start_pos, phase1_timeout=600):
    snapshots = {}
    for f in _all_jsonl():
        try: snapshots[str(f)] = f.stat().st_size
        except: pass

    found = False; all_lines = []

    for _i in range(phase1_timeout):
        for f in _all_jsonl():
            key = str(f)
            sp = snapshots.get(key, 0)
            lines, _new_pos = await asyncio.to_thread(_read_new, f, sp)
            if _has_user(lines, inject_text):
                jsonl = f; found = True
                all_lines = lines[:]
                snapshots[key] = _new_pos
                ei = _find_end_turn(all_lines)
                if ei is not None:
                    texts = _collect_all_text(all_lines[:ei + 1])
                    if texts: return "\n\n".join(texts)
                break
            if lines: snapshots[key] = _new_pos
        if found: break
        await asyncio.sleep(1)
    else:
        return None

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


def find_session_dir():
    """自动探测 Claude Code 项目目录"""
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return base / "C--Users-zshyl"
    best = None; best_time = 0
    for d in base.iterdir():
        if d.is_dir():
            files = list(d.glob("*.jsonl"))
            if files:
                mt = max(f.stat().st_mtime for f in files)
                if mt > best_time:
                    best_time = mt; best = d
    return best or (base / "C--Users-zshyl")
