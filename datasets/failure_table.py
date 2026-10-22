"""产品文字模式（Exp 4）前 5 未命中的文字题：逐条排名 + 失败归因（归因是人工看块级相似度后写的，见 CAUSES）。

块级证据的做法：用同一个 MiniLM 编码查询，和库里全部文本块算余弦，比较"正确帧里最像查询的块"与"排在前面的
错误块"的分数和内容（data/_blk.py 的输出，结论摘在 CAUSES 里）。只读正式结果，不重跑检索。

    python datasets/failure_table.py --out results/retrieval/20261022-failures.md
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = {"dev": ROOT / "results/retrieval/20261022-dev/results.json",
        "blind": ROOT / "results/retrieval/20261022-blind/results.json"}
QUERIES = ROOT / "datasets/v1/queries.json"

CAT = {
    "A": "分栏混排：OCR 按行把左右两栏（会话列表 / 文件栏 / 侧边广告）拼进同一文本块，答案块被稀释",
    "B": "短而泛的块抢分：广告、按钮、页面标题等短文本与查询字面相近，余弦偏高",
    "C": "相近内容竞争：同一来源或别的来源里有主题相近、也部分回答问题的内容",
    "D": "疑似漏标：池内有未标为相关、但含答案原文的帧（待 pooling 复判）",
    "E": "跨语言 / 代码语义弱：英文问中文内容或问代码、中文问英文报错，正确块相似度低",
    "F": "融合拖累：某一路排得靠前，另一路没召回，RRF 后名次下降",
}
# 归因（可多项）与一句证据；分数是块级余弦（查询 vs 文本块）
CAUSES = {
    "xiao-blind-chat-en-02": ("AD", "答案块夹着会话列表行（0.544）；另有一帧含“企业级RAG知识库与Agent”原文未标"),
    "xiao-blind-chat-zh-01": ("DA", "语义第 1 名那帧主聊天区就是“凡今天进店消费会员送鸭血一份”（已看图确认），未标为相关"),
    "xiao-blind-chat-zh-02": ("C", "同群“退坑交 15 元场地费”（0.689）高于答案“每人 19.9+球费 AA”（0.631）"),
    "xiao-blind-code-en-02": ("BE", "广告“自动提示股票买卖点的炒股指标”（0.519）远高于 maxProfit 代码块（0.360）"),
    "cao-blind-code-zh-01": ("EF", "中文问英文 git 报错，语义未召回（正确块 0.363）；词法第 2，RRF 后第 7"),
    "xiao-blind-table_doc-en-01": ("C", "微信求职群的校招 offer 消息（0.559）略高于机考总结正文（0.536）"),
    "xiao-blind-table_doc-zh-02": ("C", "另一份文档《AI岗机考总结》确有“20 道选择题…共 450 分”（0.622+），查询靠“刷题工作区”区分"),
    "xiao-dev-chat-en-01": ("D", "排在前面的块“价格会提升到 5580”所在帧未标为相关"),
    "xiao-dev-chat-en-02": ("AC", "答案块混入会话列表；“prospective students …”网页导航（0.56）等抢分"),
    "cao-dev-chat-zh-03": ("C", "同一聊天应用别的房间（聚餐群、“订 COM1 讨论室”）高于答案“会议室改了：E4-04-02”"),
    "xiao-dev-chat-zh-01": ("AE", "答案块混进“羽翎社”等会话列表行（0.357）；查询写全称“国家开发银行”，原文是“国开行”"),
    "xiao-dev-chat-zh-02": ("AB", "“See Nutrition Differently”“Food & Drink”等短块（0.52–0.55）高于中粮正文（0.410）"),
    "xiao-dev-code-en-02": ("ABE", "代码与文件栏交错；短标题“ScreenRecall截图入库流程”（0.566）抢分；英文问中文注释"),
    "xiao-dev-code-zh-01": ("AB", "“Search by Websites Log in Sign Up”（0.728）远高于含答案的代码块（0.376）"),
    "xiao-dev-code-zh-02": ("AC", "曹的聊天里讨论“删除时引用计数”（0.56）高于含答案的代码块（0.437）"),
    "xiao-dev-web-en-02": ("F", "语义第 1，词法前 10 无相关帧；两路都召回的其他帧在等权 RRF 中反超，掉到第 6"),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    q = {x["id"]: x for x in json.loads(QUERIES.read_text(encoding="utf-8"))}
    rows, cnt, n_text = [], Counter(), Counter()
    for split, p in RUNS.items():
        for r in json.loads(p.read_text(encoding="utf-8"))["per_query"]:
            Q = q[r["id"]]
            if Q["mode"] != "text":
                continue
            n_text[split] += 1
            if r["results"]["exp4_rrf_text"]["hit5"]:
                continue
            rk = lambda e: r["results"][e]["first_relevant_rank"] or "—"
            cats, ev = CAUSES.get(r["id"], ("?", "未分析"))
            cnt.update(cats)
            rows.append(f"| {split} | {r['id']} | {Q['scenario']} | {Q['language']} | {Q['query']} | "
                        f"{rk('exp1_lexical')} | {rk('exp2_semantic')} | {rk('exp4_rrf_text')} | {cats} | {ev} |")
    missing = [k for k in CAUSES if not any(f"| {k} |" in x for x in rows)]
    lines = ["# 产品文字模式（Exp 4）前 5 未命中的文字题", "",
             f"文字题 dev {n_text['dev']} 条、blind {n_text['blind']} 条；未命中 {len(rows)} 条。"
             "排名是首个相关帧名次（— 为前 10 无命中）。归因来自块级相似度对比，可多项。", "",
             "| 划分 | 查询 | 场景 | 语言 | 查询内容 | 词法 | 语义 | RRF | 归因 | 证据 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"] + rows + ["", "## 归因类别", ""]
    lines += [f"- **{k}**（{cnt[k]} 条）{v}" for k, v in CAT.items()]
    lines += ["", "排在前面的错误块里只有 2/64 含窗口标签栏、菜单等界面文字：界面噪声不是主要原因。"]
    if missing:
        lines += ["", f"（CAUSES 里有不在未命中名单中的条目：{missing}）"]
    a.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
