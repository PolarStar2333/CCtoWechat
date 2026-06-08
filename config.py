#!/usr/bin/env python3
# MIT License - Copyright (c) 2026 CCtoWechat
"""配置加载：从 config.json 读取，缺失键使用内置默认值"""

import json, logging
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent / "config.json"

_DEFAULTS = {
    "api_base": "https://ilinkai.weixin.qq.com",
    "channel_version": "1.0.2",
    "http_host": "127.0.0.1",
    "http_port": 9876,
    "images_dir": "images",
    "max_qr_refresh": 3,
    "poll_interval": 1,
    "timeouts": {
        "short": 15, "long_poll": 42, "upload": 30,
        "download": 60, "subprocess": 10, "wait_reply_phase1": 600,
    },
    "retry": {
        "backoff_short": 2, "backoff_long": 30,
        "threshold": 3, "max_consecutive": 5,
    },
    "thinking_notify_after": 30,
    "log": {"max_bytes": 1_000_000, "backup_count": 5, "console_level": "INFO"},
}


def _deep_merge(defaults, user):
    """递归合并用户配置到默认值"""
    if not isinstance(user, dict):
        return user
    result = defaults.copy()
    for k, v in user.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config():
    if _CONFIG_PATH.exists():
        try:
            user = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            return _deep_merge(_DEFAULTS, user)
        except Exception:
            logging.getLogger("config").warning(
                f"读取 {_CONFIG_PATH} 失败，使用默认配置", exc_info=True)
    return _DEFAULTS.copy()


CONFIG = load_config()
