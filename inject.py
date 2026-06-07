#!/usr/bin/env python3
# MIT License - Copyright (c) 2026 CCtoWechat
"""跨平台键盘注入模块（Windows / macOS / Linux）"""

import ctypes, os, subprocess, time

try:
    import pyperclip
except ImportError:
    pyperclip = None

_WIN = os.name == "nt"
_OSX = os.uname().sysname == "Darwin" if hasattr(os, "uname") else False


# ── 剪贴板 ──
def _clip_fallback(text):
    if _WIN:
        try:
            subprocess.run(["clip"], input=text.encode("utf-16-le", errors="replace"), check=False)
        except Exception:
            pass
    elif _OSX:
        try:
            subprocess.run(["pbcopy"], input=text.encode(), check=False)
        except Exception:
            pass
    else:
        try:
            subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(), check=False)
        except Exception:
            pass


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
            subprocess.run(["osascript", "-e",
                f'tell app "System Events" to key code {code}'], check=False)
            time.sleep(0.03)
    else:
        MAP = {0x28: "Down", 0x0D: "Return", 0x1B: "Escape"}
        for vk in vks:
            key = MAP.get(vk, str(vk))
            subprocess.run(["xdotool", "key", key], check=False)
            time.sleep(0.03)


def send_interrupt():
    """Ctrl+C 中断 + 0.5s后 End+Ctrl+U 清空输入栏"""
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
        subprocess.run(["osascript", "-e",
            'tell app "System Events" to keystroke "c" using command down'], check=False)
        time.sleep(0.5)
        subprocess.run(["osascript", "-e",
            'tell app "System Events" to key code 124 using command down'], check=False)
        time.sleep(0.05)
        subprocess.run(["osascript", "-e",
            'tell app "System Events" to keystroke "u" using control down'], check=False)
    else:
        subprocess.run(["xdotool", "key", "ctrl+c"], check=False)
        time.sleep(0.5)
        subprocess.run(["xdotool", "key", "End"], check=False)
        time.sleep(0.05)
        subprocess.run(["xdotool", "key", "ctrl+u"], check=False)


def select_session(num):
    """映射数字到键盘操作：0=Esc取消, 1=Enter, N=↓(N-1)+Enter"""
    VK_ESC, VK_RET, VK_DOWN = 0x1B, 0x0D, 0x28
    if num == 0:
        _inject_keys(VK_ESC)
    elif num == 1:
        _inject_keys(VK_RET)
    else:
        _inject_keys(*([VK_DOWN] * (num - 1) + [VK_RET]))


# ── 文本注入（剪贴板 + 粘贴）──
def _inject_win(text):
    user32 = ctypes.windll.user32
    VK_CTRL, VK_V, VK_RET, KEYUP = 0x11, 0x56, 0x0D, 0x0002
    user32.keybd_event(VK_CTRL, 0, 0, 0)
    user32.keybd_event(VK_V, 0, 0, 0); time.sleep(0.03)
    user32.keybd_event(VK_V, 0, KEYUP, 0); time.sleep(0.01)
    user32.keybd_event(VK_CTRL, 0, KEYUP, 0)
    time.sleep(0.3)
    user32.keybd_event(VK_RET, 0, 0, 0); time.sleep(0.05)
    user32.keybd_event(VK_RET, 0, KEYUP, 0)
    return True


def _inject_osx(text):
    script = (
        'tell application "System Events"\n'
        '    keystroke "v" using command down\n'
        '    delay 0.1\n'
        '    keystroke return\n'
        'end tell'
    )
    subprocess.run(["osascript", "-e", script], check=False)
    return True


def _inject_linux(text):
    subprocess.run(["xdotool", "key", "ctrl+v"], check=False)
    time.sleep(0.1)
    subprocess.run(["xdotool", "key", "Return"], check=False)
    return True


def inject_to_terminal(text):
    """将文本注入当前活动终端（剪贴板 → 粘贴 → 回车）"""
    if pyperclip is not None:
        try:
            pyperclip.copy(text)
        except Exception:
            _clip_fallback(text)
    else:
        _clip_fallback(text)
    time.sleep(0.15)

    if _WIN:
        return _inject_win(text)
    elif _OSX:
        return _inject_osx(text)
    else:
        return _inject_linux(text)
