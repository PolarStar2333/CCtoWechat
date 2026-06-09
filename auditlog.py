#!/usr/bin/env python3
# MIT License - Copyright (c) 2026 CCtoWechat
"""审计日志：仅记录元数据（字符数/行数/编码/事件类型），不记录用户内容。
自动清除超过 24 小时的条目。仅 /log 命令触发发送。"""

import json
import time
from pathlib import Path

_AUDIT_FILE = Path(__file__).parent / "audit.jsonl"
_MAX_AGE = 24 * 3600


def _purge_old():
    if not _AUDIT_FILE.exists():
        return
    cutoff = time.time() - _MAX_AGE
    kept = []
    try:
        for line in _AUDIT_FILE.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                if json.loads(line).get("ts", 0) >= cutoff:
                    kept.append(line)
            except json.JSONDecodeError:
                continue
    except Exception:
        return
    _AUDIT_FILE.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")


def audit(event: str, **meta):
    """写入一条审计日志（仅元数据，不含用户内容）"""
    _purge_old()
    entry = {"ts": int(time.time()), "event": event, "meta": meta}
    try:
        with open(_AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def get_log_text():
    """获取过去 24 小时的审计日志，格式化为可读文本"""
    if not _AUDIT_FILE.exists():
        return "暂无日志（过去 24 小时无记录）"
    cutoff = time.time() - _MAX_AGE
    lines = []
    try:
        for line in _AUDIT_FILE.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("ts", 0) < cutoff:
                    continue
                ts = time.strftime("%m-%d %H:%M", time.localtime(entry["ts"]))
                ev = entry["event"]
                meta = entry.get("meta", {})

                if ev == "rx_msg":
                    lines.append(
                        f"[{ts}] 收到 | {meta.get('type','?')} "
                        f"字符:{meta.get('chars',0)} 行:{meta.get('lines',0)} "
                        f"编码:{meta.get('encoding','?')} 引用:{'是' if meta.get('has_ref') else '否'}"
                    )
                elif ev == "tx_msg":
                    lines.append(
                        f"[{ts}] 发送 | 字符:{meta.get('chars',0)} "
                        f"行:{meta.get('lines',0)} {'OK' if meta.get('ok') else '失败'}"
                    )
                elif ev == "cmd":
                    lines.append(f"[{ts}] 命令 | {meta.get('cmd','?')}")
                elif ev == "error":
                    lines.append(f"[{ts}] 异常 | {meta.get('msg','?')}")
                elif ev == "lifecycle":
                    lines.append(f"[{ts}] 桥 | {meta.get('action','?')}")
                elif ev == "media":
                    lines.append(
                        f"[{ts}] 媒体 | {meta.get('action','?')} "
                        f"{meta.get('type','?')} {meta.get('size',0)}B "
                        f"编码:{meta.get('encoding','?')}"
                    )
                elif ev == "push":
                    lines.append(
                        f"[{ts}] 推送 | {meta.get('action','?')} "
                        f"字符:{meta.get('chars',0)} 行:{meta.get('lines',0)}"
                    )
                else:
                    lines.append(f"[{ts}] {ev} | {json.dumps(meta, ensure_ascii=False)}")
            except Exception:
                continue
    except Exception:
        return "日志读取失败"

    if not lines:
        return "暂无日志（过去 24 小时无记录）"

    return f"审计日志（{time.strftime('%m-%d %H:%M')} 生成，过去 24 小时）：\n\n" + "\n".join(lines)


def get_log_raw():
    """获取原始 JSONL 日志内容，用于打包发送"""
    if not _AUDIT_FILE.exists():
        return ""
    cutoff = time.time() - _MAX_AGE
    kept = []
    try:
        for line in _AUDIT_FILE.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                if json.loads(line).get("ts", 0) >= cutoff:
                    kept.append(line)
            except json.JSONDecodeError:
                continue
    except Exception:
        return ""
    return "\n".join(kept)
