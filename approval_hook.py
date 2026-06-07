#!/usr/bin/env python3
"""PermissionRequest Hook: 去重后写入文件，桥接转发到微信"""
import sys, json
from pathlib import Path

ROOT = Path(__file__).parent

PENDING = ROOT.joinpath("approval_pending.json")

try:
    data = json.loads(sys.stdin.read())
    pending = {
        "tool": data.get("tool_name", "?"),
        "input": str(data.get("tool_input", ""))[:200],
        "rule": data.get("permission_rule", ""),
    }
    PENDING.write_text(json.dumps(pending, ensure_ascii=False), encoding="utf-8")
except Exception:
    PENDING.write_text(json.dumps({"tool": "?", "input": "", "rule": ""}, ensure_ascii=False), encoding="utf-8")
