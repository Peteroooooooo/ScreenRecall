"""允许/排除规则与快捷键解析。"""

import pytest

from screenrecall import hotkey, rules
from screenrecall.config import RulesConfig

R = RulesConfig(
    allowed_processes=(),
    excluded_processes=("keepass.exe",),
    excluded_title_keywords=("inprivate", "无痕"),
)


def test_excluded_process_case_insensitive():
    assert "排除列表" in rules.check("KeePass.exe", "数据库", R)


def test_excluded_title_keyword():
    assert "InPrivate".lower() in rules.check("msedge.exe", "新标签页 - [InPrivate] - Edge", R).lower()
    assert rules.check("chrome.exe", "无痕模式 - Chrome", R)


def test_rules_fold_fullwidth_and_unicode_case():
    """和"对 Agent 隐藏"名单一样按 NFKC + casefold 比较：全角、非 ASCII 大小写的标题也要拦住。"""
    r = RulesConfig(excluded_processes=("ｂａｎｋ.exe",), excluded_title_keywords=("bank", "БАНК", "ＳＴＲＡＳＳＥ"))
    assert rules.check("chrome.exe", "ＢＡＮＫ 网上银行", r)
    assert rules.check("chrome.exe", "мой банк", r)
    assert rules.check("chrome.exe", "Hauptstraße 1", r)
    assert rules.check("BANK.EXE", "x", r)
    from screenrecall.rec.windows import make_rules, rule_reason

    assert rule_reason("msedge.exe", "ＢＡＮＫ - Edge", make_rules(excluded_title_keywords=["bank"]))
    assert rules.check("notepad.exe", "a.txt", r) is None


def test_allowed_when_no_rule_matches():
    assert rules.check("notepad.exe", "a.txt - 记事本", R) is None


def test_allow_list_restricts_and_exclusion_wins():
    r = RulesConfig(allowed_processes=("code.exe", "keepass.exe"), excluded_processes=("keepass.exe",))
    assert rules.check("Code.exe", "x", r) is None
    assert "允许列表" in rules.check("notepad.exe", "x", r)
    assert "排除列表" in rules.check("keepass.exe", "x", r)


def test_parse_hotkey():
    hk = hotkey.parse_hotkey("Ctrl+Alt+S")
    assert hk.vk == ord("S")
    assert hk.modifiers == hotkey.MOD_CONTROL | hotkey.MOD_ALT | hotkey.MOD_NOREPEAT
    assert hk.label == "Ctrl+Alt+S"
    assert hotkey.parse_hotkey("ctrl+shift+f9").vk == 0x78


@pytest.mark.parametrize("bad", ["s", "ctrl+", "hyper+s", "ctrl+alt+é", "ctrl+f25"])
def test_parse_hotkey_rejects(bad):
    with pytest.raises(ValueError):
        hotkey.parse_hotkey(bad)
