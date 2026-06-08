#!/usr/bin/env python3
# MIT License - Copyright (c) 2026 CCtoWechat
"""统一日志配置：文件轮转 + 控制台输出"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import CONFIG

_LOG_DIR = Path(__file__).parent
_LOG_FILE = _LOG_DIR / "bridge.log"

_FORMAT = "[%(asctime)s] [%(levelname)-5s] [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_log_cfg = CONFIG.get("log", {})


def setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # 文件：DEBUG+，最大 N MB × N 个备份
    fh = RotatingFileHandler(
        _LOG_FILE,
        maxBytes=_log_cfg.get("max_bytes", 1_000_000),
        backupCount=_log_cfg.get("backup_count", 5),
        encoding="utf-8", delay=True
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(_FORMAT, _DATE_FORMAT))
    root.addHandler(fh)

    # 控制台：可配置级别
    console_level = getattr(logging, _log_cfg.get("console_level", "INFO").upper(), logging.INFO)
    ch = logging.StreamHandler()
    ch.setLevel(console_level)
    ch.setFormatter(logging.Formatter(_FORMAT, _DATE_FORMAT))
    root.addHandler(ch)


def get_logger(name):
    return logging.getLogger(name)
