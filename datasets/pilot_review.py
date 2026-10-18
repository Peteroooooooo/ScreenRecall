"""试采复核材料（一次性辅助脚本，不进产品）。

生成到 data-dataset/review/（被 git 忽略）：
- pilot-review-cao.csv：每个保留帧一行，列出要人工判断的问题（近重复对、地址栏任务编号、弹窗 / 悬停状态栏等），
  以及空白的人工列。类别只给"建议"，不写进标注表。
- web-facts-draft.csv：网页画面事实的草稿（代理看图写的），待人工看图确认后再写进 raw/cao/facts.csv 和清单。
- queries-dev-draft.json：四个场景各 1 条开发查询草稿；relevant 是建议，待复核。

    .venv/bin/python datasets/pilot_review.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from screenrecall import collect  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DD = ROOT / "data-dataset"
RAW = DD / "raw"
OUT = DD / "review"
SUGGEST = {"code": "code", "chat": "chat", "web": "web", "table": "table_doc"}

# 代理看图记下的、需要人判断的画面问题（2026-10-18）
VISUAL_NOTES = {
    "cao-web-01-002": "左下角有链接悬停状态栏",
    "cao-web-01-003": "与 011 同为 Jungfrau 首页顶部，只差窗口宽度（半屏 / 全屏），近重复检查因宽高比不同未报；请决定是否只留一张",
    "cao-web-01-004": "右下角有网站 AI 助手气泡，遮挡少量画面",
    "cao-web-01-005": "右下角有网站 AI 助手气泡",
    "cao-web-01-006": "右下角有网站 AI 助手气泡",
    "cao-web-01-008": "左下角有链接悬停状态栏；半屏窗口",
    "cao-web-01-011": "与 003 只差窗口宽度，见 003",
    "cao-web-01-016": "左下角有链接悬停状态栏",
    "cao-table-01-002": "与 003 只差选中单元格和公式栏（数值 → =SUM 公式）；检查报告建议作为有目的的核验样本保留，请确认",
    "cao-table-01-003": "见 002",
    "cao-code-01-008": "画面说明已按画面更正为形状报错（原记录误写为训练成功）",
}

WEB_FACTS = [  # (fact_id, 内容, 语言, 建议相关帧)
    ("cao-web-01#1", "Jungfraujoch 雪山与山顶观测站主图，大字 JUNGFRAUJOCH / TOP OF EUROPE", "en",
     ["cao-web-01-003", "cao-web-01-011"]),
    ("cao-web-01#2", "湖边石头上两人背影眺望山谷湖泊，叠字 Explore the Trails（Banff & Lake Louise）", "en",
     ["cao-web-01-004"]),
    ("cao-web-01#3", "碧绿湖泊与雪山松林，大字 Park Mode（Banff & Lake Louise）", "en", ["cao-web-01-006"]),
    ("cao-web-01#4", "秋季金黄落叶松林与山湖倒影，标语 We need Switzerland.（MySwitzerland）", "en", ["cao-web-01-016"]),
]

QUERIES = [
    dict(id="dev-code-en-01", split="dev", scenario="code", language="en", mode="text",
         query='"is invalid for input of size 1920000"', facts=["cao-code-01#1"],
         rationale="完整报错原文是脚本真实运行打出来的；凡画面上能看见这一行报错的帧都相关"),
    dict(id="dev-chat-zh-01", split="dev", scenario="chat", language="zh", mode="text",
         query="同事在频道里分享的 API 文档 v2.3 链接", facts=["cao-chat-01#1"],
         rationale="合成聊天里分享的文档链接；注意 cao-chat-01 目前全是 mock_ui 仿界面，这条只用于流程演练，"
                   "正式查询要等真实聊天应用的截图"),
    dict(id="dev-web-en-01", split="dev", scenario="web", language="en", mode="image",
         query="snowy mountain summit with an observatory under a clear blue sky", facts=["cao-web-01#1"],
         rationale="画面主图是雪山与山顶观测站；事实 cao-web-01#1 仍是草稿，相关帧待人工看图确认"),
    dict(id="dev-table-zh-01", split="dev", scenario="table", language="zh", mode="text",
         query="一月买菜 Groceries 改成 465 之后的金额", facts=["cao-table-01#1"],
         rationale="合成预算表里 1 月 Groceries 改为 465.00；凡画面上能看见该金额的帧都相关"),
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    res = collect.check(RAW)
    pairs: dict[str, list[str]] = {}
    for a, b, _ in res.near_pairs:
        pairs.setdefault(a, []).append(b)
        pairs.setdefault(b, []).append(a)
    labels = {r["image_id"]: r for r in collect.read_csv(DD / "labels-cao.csv")}
    rows = []
    for it in sorted(res.items, key=lambda i: i.row["image_id"]):
        r = it.row
        if r["collector"] != "cao" or not it.keep:
            continue
        lab = labels.get(r["image_id"], {})
        issues = []
        if r["image_id"] in pairs:
            issues.append("近重复：与 " + "、".join(sorted(pairs[r["image_id"]])) + " 相近，请判断差异是否有意义")
        if lab.get("ocr_task_id_leak"):
            issues.append(f"OCR 读到任务编号 {lab['ocr_task_id_leak']}（地址栏），分类时是类别线索")
        if r["image_id"] in VISUAL_NOTES:
            issues.append(VISUAL_NOTES[r["image_id"]])
        rows.append({
            "image_id": r["image_id"], "file": f"raw/cao/{r['task_id']}/{r['file']}", "source_kind": r["source_kind"],
            "use": "开发测试样本（不进正式数据集）" if r["source_kind"] in collect.DEV_ONLY_KINDS else "候选正式样本",
            "app": r["app"], "language": r["language"], "facts": r["facts"],
            "suggested_label": SUGGEST[r["scenario"]], "suggestion_status": "按采集场景推断的建议，不是人工真值",
            "issues": "；".join(issues),
            "decision": "", "near_dup_reason": "", "checked_by": "", "comment": ""})
    cols = list(rows[0].keys())
    collect.write_csv(OUT / "pilot-review-cao.csv", cols, rows)

    collect.write_csv(OUT / "web-facts-draft.csv",
                      ["fact_id", "task_id", "kind", "content", "language", "suggested_frames", "status", "note"],
                      [{"fact_id": f, "task_id": "cao-web-01", "kind": "visual", "content": c, "language": lg,
                        "suggested_frames": ";".join(fr), "status": "draft",
                        "note": "代理看图草拟；人工确认后写进 raw/cao/facts.csv，并在对应清单行的 facts 列补标"}
                       for f, c, lg, fr in WEB_FACTS])

    mapping = collect.fact_frames(RAW, str(DD))
    id2fid = {r["image_id"]: r["frame_id"] for r in labels.values()}
    web_draft = {f: [id2fid[i] for i in fr if i in id2fid] for f, _, _, fr in WEB_FACTS}
    qs = []
    for q in QUERIES:
        fid = q["facts"][0]
        rel = mapping.get(fid) or web_draft.get(fid, [])
        q = dict(q, relevant=sorted(rel), review_status="draft",
                 relevant_source=("清单 facts 标记（脚本植入的事实）" if fid in mapping else "代理看图建议（事实为草稿）")
                 + "，待复核人对照原图确认")
        qs.append(q)
    (OUT / "queries-dev-draft.json").write_text(json.dumps(qs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"复核表 {len(rows)} 行；有待判断问题的 {sum(1 for r in rows if r['issues'])} 行")
    for q in qs:
        print(f"{q['id']}: relevant {len(q['relevant'])} 帧（{q['relevant_source']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
