#!/usr/bin/env python3
# MIT License - Copyright (c) 2026 CCtoWechat
"""交互式选择器：AskUserQuestion 通知格式化、答案校验、按键注入"""

import re, time, logging
from inject import inject_tab, inject_down_enter

logger = logging.getLogger("selector")


def format_questions(questions):
    """格式化 AskUserQuestion 问题列表为微信通知文本"""
    lines = ["Claude 正在询问：", ""]
    for qi, q in enumerate(questions):
        prefix = chr(ord('A') + qi)
        header = q.get("header", "")
        question = q.get("question", "")
        multi = "（可多选）" if q.get("multiSelect") else ""
        lines.append(f"[{prefix}] [{header}] {question} {multi}")
        n = len(q.get("options", []))
        for i, opt in enumerate(q.get("options", []), 1):
            desc = opt.get("description", "")
            lines.append(f"  {i}. {opt.get('label', '?')}" + (f" — {desc}" if desc else ""))
        lines.append(f"  {n+1}. 自定义输入...")
        lines.append(f"  {n+2}. 讨论此问题")
        if qi < len(questions) - 1:
            lines.append("")
    lines.append("")
    lines.append("回复格式: [字母][数字][字母][数字]...  多选可连写数字  如 A123B12  不加分隔符  /submit 提交  /stop 取消")
    return "\n".join(lines)


def validate_answer(text, questions):
    """校验选择器回复，返回 {q_idx: [option_nums]} 或 None

    支持两种格式：
    - A2B13C4: 字母+数字，字母递增，每个数字字符=一个选项号
    - 纯数字: 映射到第一个问题（A），范围 1..n_options
    """
    n_questions = len(questions)
    if n_questions == 0:
        return None

    # 格式1: 严格字母+数字（A2B13C4）
    if re.fullmatch(r'([a-zA-Z]\d+)+', text):
        pairs = re.findall(r'([a-zA-Z])(\d+)', text)
        prev = -1
        answers = {}
        for letter, digits in pairs:
            idx = ord(letter.upper()) - ord('A')
            if idx <= prev:
                return None
            prev = idx
            nums = [int(c) for c in digits]
            answers[idx] = nums
        if prev >= n_questions:
            return None
        return answers

    # 格式2: 纯数字 → 第一个问题（A），允许 n+1(自定义) n+2(讨论)
    if text.isdigit() and n_questions > 0:
        num = int(text)
        n_opts = len(questions[0].get("options", []))
        if 1 <= num <= n_opts + 2:
            return {0: [num]}

    return None


def inject_answers(answers, questions):
    """按键注入已校验的答案（Tab 导航 + Down/Enter 选择）

    单选 Enter 后终端自动跳到下一题，多选 Enter 后光标停留。
    据此计算实际需要按的 Tab 次数，避免跳过头。
    遇到自定义输入(n+1)/讨论(n+2)选项时选中后立即停止，返回 True。
    """
    sorted_qs = sorted(answers.keys())
    time.sleep(0.5)  # 等待终端渲染选择器
    expected_pos = 0  # 光标当前所在问题索引
    special = False

    for q_idx in sorted_qs:
        # Tab 导航：计算实际需要按的次数
        tabs_needed = q_idx - expected_pos
        for _ in range(tabs_needed):
            inject_tab()
            time.sleep(0.2)

        # 选择选项
        nums = sorted(set(answers[q_idx]))
        n_opts = len(questions[q_idx].get("options", []))
        cur = 1
        for num in nums:
            inject_down_enter(max(0, num - cur))
            logger.info(f"选择 {chr(ord('A')+q_idx)}{num}")
            cur = num
            time.sleep(0.3)

        # 检测特殊选项：自定义输入(n+1) 或 讨论(n+2)
        if any(n > n_opts for n in nums):
            kind = "自定义输入" if any(n == n_opts + 1 for n in nums) else "讨论此问题"
            special = (q_idx, kind)
            break

        # 更新预期光标位置：多选停留，单选自动跳到下一题
        multi = questions[q_idx].get("multiSelect", False)
        expected_pos = q_idx if multi else q_idx + 1

    return special


def format_confirmation(answers, questions):
    """格式化答案摘要（模拟终端确认页）"""
    lines = ["已选择：", ""]
    for q_idx in sorted(answers.keys()):
        q = questions[q_idx]
        prefix = chr(ord('A') + q_idx)
        header = q.get("header", "")
        opts = q.get("options", [])
        selected = []
        for n in sorted(set(answers[q_idx])):
            if 1 <= n <= len(opts):
                selected.append(f"{n}.{opts[n-1].get('label', '?')}")
        lines.append(f"[{prefix}] {header}: {', '.join(selected)}")
    lines.append("")
    lines.append("回复 1 提交  2 取消  或 /stop")
    return "\n".join(lines)
