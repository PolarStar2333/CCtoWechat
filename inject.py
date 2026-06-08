#!/usr/bin/env python3
# MIT License - Copyright (c) 2026 CCtoWechat
"""跨平台键盘注入模块（Windows / macOS / Linux）"""

import ctypes, os, subprocess, time, logging

logger = logging.getLogger("inject")

try:
    import pyperclip
except ImportError:
    pyperclip = None
    logger.debug("pyperclip 未安装，将使用系统剪贴板命令")

_WIN = os.name == "nt"
_OSX = os.uname().sysname == "Darwin" if hasattr(os, "uname") else False


# ── 剪贴板 ──
def _clip_fallback(text):
    if _WIN:
        try:
            # utf-16 带 BOM，Windows clip 命令需要
            r = subprocess.run(["clip"], input=text.encode("utf-16", errors="replace"), check=False)
            if r.returncode != 0:
                logger.debug(f"clip.exe 返回 {r.returncode}")
        except Exception:
            logger.debug("clip.exe 不可用", exc_info=True)
    elif _OSX:
        try:
            r = subprocess.run(["pbcopy"], input=text.encode(), check=False)
            if r.returncode != 0:
                logger.debug(f"pbcopy 返回 {r.returncode}")
        except Exception:
            logger.debug("pbcopy 不可用", exc_info=True)
    else:
        try:
            r = subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(), check=False)
            if r.returncode != 0:
                logger.debug(f"xclip 返回 {r.returncode}")
        except Exception:
            logger.debug("xclip 不可用", exc_info=True)


# ── 特殊按键 ──
def _inject_keys(*vks):
    """注入特殊按键序列：Windows 虚键码 / macOS key code / Linux xdotool"""
    if _WIN:
        user32 = ctypes.windll.user32
        KEYUP = 0x0002
        for vk in vks:
            user32.keybd_event(vk, 0, 0, 0); time.sleep(0.03)
            user32.keybd_event(vk, 0, KEYUP, 0); time.sleep(0.02)
    elif _OSX:
        MAP = {0x28: 125, 0x0D: 36, 0x1B: 53}  # Down/Return/Escape
        for vk in vks:
            code = MAP.get(vk, vk)
            r = subprocess.run(["osascript", "-e",
                f'tell app "System Events" to key code {code}'], check=False)
            if r.returncode != 0:
                logger.debug(f"osascript key code {code} 失败 rc={r.returncode}")
            time.sleep(0.03)
    else:
        MAP = {0x28: "Down", 0x0D: "Return", 0x1B: "Escape"}
        for vk in vks:
            key = MAP.get(vk, str(vk))
            r = subprocess.run(["xdotool", "key", key], check=False)
            if r.returncode != 0:
                logger.debug(f"xdotool key {key} 失败 rc={r.returncode}")
            time.sleep(0.03)


def send_interrupt():
    """Ctrl+C 中断 + 0.5s后 End+Ctrl+U 清空输入栏"""
    logger.info("发送中断信号")
    if _WIN:
        user32 = ctypes.windll.user32
        KEYUP = 0x0002
        user32.keybd_event(0x11, 0, 0, 0); user32.keybd_event(0x43, 0, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(0x43, 0, KEYUP, 0); user32.keybd_event(0x11, 0, KEYUP, 0)
        time.sleep(0.5)
        user32.keybd_event(0x23, 0, 0, 0); time.sleep(0.02); user32.keybd_event(0x23, 0, KEYUP, 0)
        time.sleep(0.05)
        user32.keybd_event(0x11, 0, 0, 0); user32.keybd_event(0x55, 0, 0, 0)
        time.sleep(0.03)
        user32.keybd_event(0x55, 0, KEYUP, 0); user32.keybd_event(0x11, 0, KEYUP, 0)
    elif _OSX:
        r = subprocess.run(["osascript", "-e",
            'tell app "System Events" to keystroke "c" using command down'], check=False)
        if r.returncode != 0: logger.debug(f"osascript cmd+c 失败 rc={r.returncode}")
        time.sleep(0.5)
        r = subprocess.run(["osascript", "-e",
            'tell app "System Events" to key code 124 using command down'], check=False)
        if r.returncode != 0: logger.debug(f"osascript cmd+end 失败 rc={r.returncode}")
        time.sleep(0.05)
        r = subprocess.run(["osascript", "-e",
            'tell app "System Events" to keystroke "u" using control down'], check=False)
        if r.returncode != 0: logger.debug(f"osascript ctrl+u 失败 rc={r.returncode}")
    else:
        r = subprocess.run(["xdotool", "key", "ctrl+c"], check=False)
        if r.returncode != 0: logger.debug(f"xdotool ctrl+c 失败 rc={r.returncode}")
        time.sleep(0.5)
        r = subprocess.run(["xdotool", "key", "End"], check=False)
        if r.returncode != 0: logger.debug(f"xdotool End 失败 rc={r.returncode}")
        time.sleep(0.05)
        r = subprocess.run(["xdotool", "key", "ctrl+u"], check=False)
        if r.returncode != 0: logger.debug(f"xdotool ctrl+u 失败 rc={r.returncode}")


def select_session(num):
    """映射数字到键盘操作：0=Esc取消, 1=Enter, N=↓(N-1)+Enter"""
    logger.info(f"选择会话 #{num}")
    VK_ESC, VK_RET, VK_DOWN = 0x1B, 0x0D, 0x28
    if num == 0:
        _inject_keys(VK_ESC)
    elif num == 1:
        _inject_keys(VK_RET)
    else:
        _inject_keys(*([VK_DOWN] * (num - 1) + [VK_RET]))


# ── 文本注入 ──
def _make_input_structs():
    """构建 INPUT 等结构体（复用，避免重复定义）"""
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", wintypes.WPARAM)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", wintypes.WPARAM)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]

    return INPUT, INPUT_UNION, KEYBDINPUT


def _inject_win_sendinput(text):
    """SendInput 模拟 Ctrl+V 粘贴（利用剪贴板原子性，多行不被打断）"""
    user32 = ctypes.windll.user32
    VK_CTRL, VK_V, VK_RET, KEYUP = 0x11, 0x56, 0x0D, 0x0002

    # 1) 写剪贴板（pyperclip 处理编码，回退 clip 命令带 BOM）
    if pyperclip is not None:
        try:
            pyperclip.copy(text)
        except Exception:
            _clip_fallback(text)
    else:
        _clip_fallback(text)
    time.sleep(0.2)

    # 2) Ctrl+V 粘贴（SendInput 模拟，绕过终端粘贴警告）
    inputs = []
    INPUT, _, _ = _make_input_structs()
    INPUT_KEYBOARD = 1

    # Ctrl down
    inp = INPUT(); inp.type = INPUT_KEYBOARD
    inp.u.ki.wVk = VK_CTRL
    inputs.append(inp)
    # V down
    inp = INPUT(); inp.type = INPUT_KEYBOARD
    inp.u.ki.wVk = VK_V
    inputs.append(inp)
    time.sleep(0.05)
    # V up
    inp = INPUT(); inp.type = INPUT_KEYBOARD
    inp.u.ki.wVk = VK_V; inp.u.ki.dwFlags = KEYUP
    inputs.append(inp)
    # Ctrl up
    inp = INPUT(); inp.type = INPUT_KEYBOARD
    inp.u.ki.wVk = VK_CTRL; inp.u.ki.dwFlags = KEYUP
    inputs.append(inp)

    INPUT_ARRAY = INPUT * len(inputs)
    sent = user32.SendInput(len(inputs), INPUT_ARRAY(*inputs), ctypes.sizeof(INPUT))
    if sent < len(inputs):
        raise OSError(f"仅发送 {sent}/{len(inputs)}")

    # 3) 等粘贴完成，回车提交
    time.sleep(0.4)
    user32.keybd_event(VK_RET, 0, 0, 0); time.sleep(0.05)
    user32.keybd_event(VK_RET, 0, KEYUP, 0)
    return True


def _inject_win_clipboard(text):
    """剪贴板 + Ctrl+V 注入（回退方案）"""
    user32 = ctypes.windll.user32
    _clip_fallback(text)
    time.sleep(0.15)
    VK_CTRL, VK_V, VK_RET, KEYUP = 0x11, 0x56, 0x0D, 0x0002
    user32.keybd_event(VK_CTRL, 0, 0, 0)
    user32.keybd_event(VK_V, 0, 0, 0); time.sleep(0.03)
    user32.keybd_event(VK_V, 0, KEYUP, 0); time.sleep(0.01)
    user32.keybd_event(VK_CTRL, 0, KEYUP, 0)
    time.sleep(0.3)
    user32.keybd_event(VK_RET, 0, 0, 0); time.sleep(0.05)
    user32.keybd_event(VK_RET, 0, KEYUP, 0)
    return True


def _inject_win(text):
    """Windows 文本注入：优先 SendInput，失败回退剪贴板"""
    try:
        return _inject_win_sendinput(text)
    except Exception as e:
        logger.warning(f"SendInput 失败，回退剪贴板: {e}")
        return _inject_win_clipboard(text)


def _inject_osx(text):
    script = (
        'tell application "System Events"\n'
        '    keystroke "v" using command down\n'
        '    delay 0.1\n'
        '    keystroke return\n'
        'end tell'
    )
    r = subprocess.run(["osascript", "-e", script], check=False)
    if r.returncode != 0:
        logger.debug(f"osascript 粘贴失败 rc={r.returncode}")
    return True


def _inject_linux(text):
    r = subprocess.run(["xdotool", "key", "ctrl+v"], check=False)
    if r.returncode != 0: logger.debug(f"xdotool ctrl+v 失败 rc={r.returncode}")
    time.sleep(0.1)
    r = subprocess.run(["xdotool", "key", "Return"], check=False)
    if r.returncode != 0: logger.debug(f"xdotool Return 失败 rc={r.returncode}")
    return True


def inject_to_terminal(text):
    """将文本注入当前活动终端"""
    platform = "Win" if _WIN else "OSX" if _OSX else "Linux"
    logger.info(f"注入文本 ({len(text)} chars, {platform})")
    # Windows: 写剪贴板 + SendInput Ctrl+V 粘贴（多行原子操作）
    if _WIN:
        result = _inject_win(text)
        if result:
            logger.debug(f"注入成功 ({len(text)} chars)")
        else:
            logger.error(f"注入失败 ({len(text)} chars)")
        return result

    # macOS / Linux: 剪贴板 + 粘贴
    if pyperclip is not None:
        try:
            pyperclip.copy(text)
        except Exception:
            _clip_fallback(text)
    else:
        _clip_fallback(text)
    time.sleep(0.15)

    if _OSX:
        return _inject_osx(text)
    else:
        return _inject_linux(text)
