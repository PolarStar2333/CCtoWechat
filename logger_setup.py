#!/usr/bin/env python3
# MIT License - Copyright (c) 2026 CCtoWechat
"""统一日志配置：文件轮转 + 控制台输出"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_DIR = Path(__file__).parent
_LOG_FILE = _LOG_DIR / "bridge.log"

_FORMAT = "[%(asctime)s] [%(levelname)-5s] [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # 文件：DEBUG+，最大 1MB × 5 个备份
    fh = RotatingFileHandler(
        _LOG_FILE, maxBytes=1_000_000, backupCount=5,
        encoding="utf-8", delay=True
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(_FORMAT, _DATE_FORMAT))
    root.addHandler(fh)

    # 控制台：INFO+，不刷屏
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(_FORMAT, _DATE_FORMAT))
    root.addHandler(ch)


def get_logger(name):
    return logging.getLogger(name)
