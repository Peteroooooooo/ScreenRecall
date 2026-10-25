"""网页英文模式的提示翻译（screenrecall.i18n）：已收录的句子翻成英文，未收录的原样保留，只动指定字段。"""
from screenrecall import i18n


def test_exact_and_parameterized_messages():
    assert i18n.to_en("没有找到匹配的记录。") == "No matching records."
    assert i18n.to_en("3 个候选画面的语义向量尚未完成，暂时只参与词法排序。").startswith("3 candidate screens")
    assert i18n.to_en("程序 KeePass.exe 在排除列表中，不能录制。") == "Program KeePass.exe is on the exclusion list; cannot record."
    assert i18n.to_en("中间有已知缺口：屏幕锁定、画面有变化但未保存×3。这段时间的画面没有保存，不能当成连续录像。") == (
        "known gaps in between: screen locked, screen changed but not saved ×3. "
        "Screens in this period were not saved; do not treat it as continuous footage.")
    assert i18n.to_en("已删除这幅画面的 2 次出现记录和 1 个收藏。") == "Deleted 2 occurrence(s) of this screen and 1 favorite(s)."


def test_unknown_text_is_left_alone():
    assert i18n.to_en("周五聚餐小分队") == "周五聚餐小分队"


def test_localize_only_touches_message_fields():
    data = {"message": "没有找到匹配的记录。", "text": "没有找到匹配的记录。", "notes": ["原图已过期或缺失的记录不参与画面模式。"],
            "results": [{"sources": ["词法", "语义"], "snippet": ["词法"]}]}
    out = i18n.localize(data, "en")
    assert out["message"] == "No matching records." and out["text"] == "没有找到匹配的记录。"
    assert out["notes"][0].startswith("Records whose image expired")
    assert out["results"][0] == {"sources": ["lexical", "semantic"], "snippet": ["词法"]}
    assert i18n.localize(data, "zh") is data
