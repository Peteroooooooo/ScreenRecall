"""允许/排除规则（方案 1.1 P1、4.1）。

- 排除优先于允许；进程名不区分大小写，标题关键词按不区分大小写的子串匹配；
- 开始录制时检查进程与标题：不允许或被排除的窗口不开始录制；
- 录制中每次采样（含快捷键补拍）先检查标题：命中排除关键词时记缺口、不截图。
返回给页面和缺口表的说明不包含窗口标题本身，只指出命中了哪条规则。
"""

from __future__ import annotations

from .config import RulesConfig


def check(process: str | None, title: str | None, rules: RulesConfig) -> str | None:
    """返回拒绝原因；允许录制时返回 None。"""
    proc = (process or "").strip().lower()
    low_title = (title or "").lower()
    if proc and proc in rules.excluded_processes:
        return f"程序 {process} 在排除列表中"
    for kw in rules.excluded_title_keywords:
        if kw and kw.lower() in low_title:
            return f"窗口标题含排除关键词「{kw}」"
    if rules.allowed_processes and proc not in rules.allowed_processes:
        return f"程序 {process or '未知'} 不在允许列表中"
    return None


def describe(rules: RulesConfig) -> list[str]:
    """供页面展示的当前规则摘要。"""
    out = [
        "允许：" + ("、".join(rules.allowed_processes) if rules.allowed_processes else "不限制"),
        "排除程序：" + ("、".join(rules.excluded_processes) or "无"),
        "排除标题关键词：" + ("、".join(rules.excluded_title_keywords) or "无"),
    ]
    return out
