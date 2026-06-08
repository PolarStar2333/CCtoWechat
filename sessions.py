#!/usr/bin/env python3
# MIT License - Copyright (c) 2026 CCtoWechat
"""会话管理模块：AI 摘要、JSONL 读取、会话列表、回复抓取"""

import asyncio, json, logging, re, time
from pathlib import Path

logger = logging.getLogger("sessions")


# ── 全局状态（由 bridge.py 初始化）──
_session_dir = None   # 当前项目目录
_root = None          # CCtoWechat 根目录
_locked_jsonl = None  # 锁定的 JSONL 文件
_use_ai_summaries = True  # AI 摘要开关

# ── /now 实时监听缓冲区 ──
_now_buffer = []       # 阶段2累积的 assistant 内容（含 tool_use 摘要）
_now_cursor = 0         # get_now_snapshot 已输出的位置，只返回增量
_now_active = False     # 是否正在监听（阶段2中）


def init(session_dir, root, locked_jsonl=None):
    """由 bridge.py 调用来初始化模块状态"""
    global _session_dir, _root, _locked_jsonl
    _session_dir = session_dir
    _root = root
    _locked_jsonl = locked_jsonl
    logger.debug(f"sessions.init: dir={session_dir}, root={root}, locked={locked_jsonl}")


def set_locked_jsonl(path):
    global _locked_jsonl
    _locked_jsonl = path


def set_summaries(on):
    """设置 AI 摘要开关（用于启动时恢复状态），不保存文件"""
    global _use_ai_summaries
    _use_ai_summaries = on


def toggle_summaries():
    """切换 AI 摘要开关，返回新状态"""
    global _use_ai_summaries
    _use_ai_summaries = not _use_ai_summaries
    return _use_ai_summaries


def reset_now_buffer():
    """信号1触发时清空缓冲区，开始新一轮监听"""
    global _now_buffer, _now_cursor, _now_active
    _now_buffer = []
    _now_cursor = 0
    _now_active = True


def is_now_active():
    """/now 是否可用（阶段2进行中）"""
    global _now_active
    return _now_active


def get_now_snapshot():
    """返回 /now 快照：仅返回上次查询后新增的内容（去重）"""
    global _now_buffer, _now_cursor
    if not _now_buffer:
        return "暂无内容，Claude 仍在思考中..."
    new_items = _now_buffer[_now_cursor:]
    _now_cursor = len(_now_buffer)
    if not new_items:
        return "(无新内容)"
    full = "\n".join(new_items)
    if len(full) > 800:
        return "...\n" + full[-800:]
    return full


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


def _assistant_snippet(msg):
    """提取 assistant 消息的可读摘要（text + tool_use 名称/输入）"""
    c = msg.get("content", [])
    if isinstance(c, str): return c[:300]
    parts = []
    if isinstance(c, list):
        for it in c:
            if not isinstance(it, dict): continue
            t = it.get("type", "")
            if t == "text":
                parts.append(it.get("text", ""))
            elif t == "tool_use":
                name = it.get("name", "?")
                inp = it.get("input", {})
                inp_str = str(inp)[:200] if inp else ""
                parts.append(f"[工具调用: {name}]\n{inp_str}")
    return "\n".join(parts)


def _read_new(jsonl_path, pos):
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(max(0, pos))
            return f.readlines(), f.tell()
    except Exception:
        logger.warning(f"读取JSONL失败: {jsonl_path}")
        return [], pos


def _has_user(lines, fragment):
    frag = fragment.strip()[:50]
    for ln in lines:
        try: obj = json.loads(ln)
        except Exception:
            logger.debug(f"跳过非JSON行: {ln[:80]}")
            continue
        msg = obj.get("message") or obj
        if msg.get("role") == "user" and frag in _text(msg):
            return True
    return False


def _collect_all_text(lines):
    texts = []
    for ln in lines:
        try: obj = json.loads(ln)
        except Exception:
            logger.debug(f"跳过非JSON行(_collect): {ln[:80]}")
            continue
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
        except Exception:
            logger.debug(f"跳过非JSON行(_end_turn): {lines[i][:80]}")
            continue
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
                logger.debug("读取会话摘要文件失败", exc_info=True)

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
                logger.debug(f"读取首条消息失败: {f.name}", exc_info=True)
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

async def wait_reply(jsonl, inject_text, phase1_timeout=600,
                    on_first_respond=None, on_user_found=None, on_tool_use=None,
                    on_ask_user_question=None):
    """
    两阶段轮询：阶段1找到用户消息，阶段2等待 assistant end_turn。
    on_user_found: 阶段1找到用户消息时触发（信号1：注入成功确认）。
    on_tool_use: 阶段2首次检测到 tool_use 时触发（信号2：工具调用）。
    on_ask_user_question: 阶段2检测到 AskUserQuestion 时触发（交互式选项）。
    on_first_respond: 阶段2首次检测到 assistant 文本输出时触发（信号3：API开始返回）。
    """
    global _now_buffer, _now_active
    logger.debug(f"阶段1: 扫描全部JSONL查找用户消息 (inject_text={inject_text[:50]}...)")
    snapshots = {}
    for f in _all_jsonl():
        try: snapshots[str(f)] = f.stat().st_size
        except Exception:
            logger.debug("无法获取JSONL文件大小快照", exc_info=True)

    found = False; all_lines = []

    for _i in range(phase1_timeout):
        for f in _all_jsonl():
            key = str(f)
            sp = snapshots.get(key, 0)
            lines, new_pos = await asyncio.to_thread(_read_new, f, sp)
            if _has_user(lines, inject_text):
                jsonl = f; found = True
                all_lines = lines[:]
                snapshots[key] = new_pos
                ei = _find_end_turn(all_lines)
                if ei is not None:
                    texts = _collect_all_text(all_lines[:ei + 1])
                    if texts:
                        _now_active = False
                        return "\n\n".join(texts)
                break
            if lines: snapshots[key] = new_pos
        if found: break
        await asyncio.sleep(1)
    else:
        logger.warning(f"回复超时: 阶段1未找到用户消息 (inject_text={inject_text[:50]}...)")
        _now_active = False
        return None

    # 信号1：用户消息已确认 → 注入成功
    if on_user_found:
        try: await on_user_found()
        except Exception: pass

    logger.debug(f"阶段2: 在 {jsonl.name} 中等待assistant回复...")
    poll_count = 0
    first_notified = False
    tool_notified = False
    ask_notified = False
    while True:
        sp = snapshots.get(str(jsonl), 0)
        lines, new_pos = await asyncio.to_thread(_read_new, jsonl, sp)
        if lines:
            snapshots[str(jsonl)] = new_pos
            all_lines.extend(lines)
            # 累积 assistant 内容到 /now 缓冲区
            for ln in lines:
                try: obj = json.loads(ln)
                except Exception: continue
                msg = obj.get("message") or obj
                if msg.get("role") == "assistant":
                    snippet = _assistant_snippet(msg)
                    if snippet.strip():
                        _now_buffer.append(snippet.strip())
                    # 信号2：首次检测到 tool_use
                    if not tool_notified and on_tool_use:
                        if any(isinstance(c, dict) and c.get("type") == "tool_use"
                               for c in msg.get("content", [])):
                            tool_notified = True
                            try: await on_tool_use()
                            except Exception: pass
                    # 交互式选项：检测 AskUserQuestion
                    if not ask_notified and on_ask_user_question:
                        for c in msg.get("content", []):
                            if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "AskUserQuestion":
                                questions = c.get("input", {}).get("questions", [])
                                if questions:
                                    ask_notified = True
                                    try: await on_ask_user_question(questions)
                                    except Exception: pass
                                break
            # 信号3：首次文本输出（比 end_turn 早）
            if not first_notified and on_first_respond:
                for ln in lines:
                    try: obj = json.loads(ln)
                    except Exception: continue
                    msg = obj.get("message") or obj
                    if msg.get("role") == "assistant" and _text(msg).strip():
                        first_notified = True
                        try: await on_first_respond()
                        except Exception: pass
                        break
            ei = _find_end_turn(all_lines)
            if ei is not None:
                texts = _collect_all_text(all_lines[:ei + 1])
                if texts:
                    _now_active = False
                    return "\n\n".join(texts)
        poll_count += 1
        if poll_count % 30 == 0:
            logger.debug(f"阶段2轮询中: 已等待{poll_count}s, 当前行数={len(all_lines)}")
        await asyncio.sleep(1)


# ── 会话统计（/status /usage /cost 用）──

# 标准 Claude 模型定价 ($/M tokens)，自定义模型返回 None
MODEL_PRICING = {
    "claude-opus": (15, 75),
    "claude-sonnet": (3, 15),
    "claude-haiku": (0.8, 4),
    "claude-haiku-4": (0.8, 4),
}


def _get_pricing(model_name):
    """根据模型名查找定价 (input_price, output_price) 或 None"""
    if not model_name:
        return None
    m = model_name.lower()
    for prefix, prices in MODEL_PRICING.items():
        if m.startswith(prefix) or prefix in m:
            return prices
    return None


def get_session_stats(jsonl_path=None):
    """从 JSONL 统计 token 用量，返回 dict 或 None"""
    if jsonl_path is None:
        jsonl_path = _jsonl()
    if not jsonl_path or not jsonl_path.exists():
        return None
    logger.debug(f"读取会话统计: {jsonl_path.name}")

    stats = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
        "model": None,
        "message_count": 0,
        "session_id": jsonl_path.stem,
    }

    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                msg = d.get("message") or {}
                if msg.get("role") == "assistant":
                    stats["message_count"] += 1
                    if not stats["model"]:
                        stats["model"] = msg.get("model", "")
                    usage = msg.get("usage", {})
                    for k in ["input_tokens", "cache_creation_input_tokens",
                              "cache_read_input_tokens", "output_tokens"]:
                        stats[k] += usage.get(k, 0)
    except Exception:
        logger.warning(f"读取会话统计失败: {jsonl_path}", exc_info=True)
        return None

    return stats


def format_stats(stats):
    """格式化会话统计为可读文本"""
    if not stats:
        return "无法读取会话统计"

    model = stats["model"] or "未知"
    sid = stats["session_id"][:12] if stats["session_id"] else "?"
    total_in = stats["input_tokens"] + stats["cache_read_input_tokens"] + stats["cache_creation_input_tokens"]
    total_out = stats["output_tokens"]

    lines = [
        f"会话: {sid}",
        f"模型: {model}",
        f"消息数: {stats['message_count']}",
        f"输入 tokens: {total_in:,}",
        f"输出 tokens: {total_out:,}",
        f"合计 tokens: {total_in + total_out:,}",
    ]

    pricing = _get_pricing(model)
    if pricing:
        in_price, out_price = pricing
        cost = (total_in / 1_000_000) * in_price + (total_out / 1_000_000) * out_price
        lines.append(f"预估费用: ${cost:.4f}")
    else:
        lines.append("费用: 自定义模型，无法估算")

    return "\n".join(lines)


def format_usage(stats):
    """格式化 token 用量详情"""
    if not stats:
        return "无法读取用量"

    model = stats["model"] or "未知"
    return "\n".join([
        f"模型: {model}",
        f"会话: {stats['session_id'][:12]}",
        f"",
        f"输入 tokens: {stats['input_tokens']:,}",
        f"缓存读取 tokens: {stats['cache_read_input_tokens']:,}",
        f"缓存创建 tokens: {stats['cache_creation_input_tokens']:,}",
        f"输出 tokens: {stats['output_tokens']:,}",
        f"合计: {sum(stats[k] for k in ['input_tokens','cache_read_input_tokens','cache_creation_input_tokens','output_tokens']):,}",
    ])


def format_cost(stats):
    """格式化费用估算"""
    if not stats:
        return "无法计算费用"

    model = stats["model"] or "未知"
    total_in = stats["input_tokens"] + stats["cache_read_input_tokens"] + stats["cache_creation_input_tokens"]
    total_out = stats["output_tokens"]

    pricing = _get_pricing(model)
    if not pricing:
        return f"模型 {model} 为自定义模型，无法自动计算费用\n输入: {total_in:,} tokens\n输出: {total_out:,} tokens"

    in_price, out_price = pricing
    in_cost = (total_in / 1_000_000) * in_price
    out_cost = (total_out / 1_000_000) * out_price
    total_cost = in_cost + out_cost

    return "\n".join([
        f"模型: {model}",
        f"输入: {total_in:,} tokens × ${in_price}/M = ${in_cost:.4f}",
        f"输出: {total_out:,} tokens × ${out_price}/M = ${out_cost:.4f}",
        f"合计费用: ${total_cost:.4f}",
    ])


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
    logger.debug(f"find_session_dir: {'找到' if best else '未找到，使用默认'}")
    return best or (base / "C--Users-zshyl")
