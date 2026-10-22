"""检索评测出题工具（只在本机用，不是产品页面）。

运行（仓库根目录）：
    .\\.venv\\Scripts\\streamlit.exe run tools/query_app.py --server.port 8503

做法：先翻图找目标画面 → 把能回答的画面（含前后几张）逐张确认后设为答案 → 再写查询与判定依据。
本工具**不接任何检索**：不显示搜索结果或相似度，只按时间、应用、窗口标题、标注类别浏览，
避免"从系统自己的结果里挑答案"造成的偏差（方案：真值不以检索器是否命中反推）。

题目写入 datasets/<名称>/queries.json，格式即 screenrecall.evaluate 的查询文件。环境变量：
    SCREENRECALL_DATA_DIR  数据目录（默认 config 里的 data）
    LABEL_DATASET          数据集目录（默认 datasets/local-20261019，读其中 labels.csv 的类别用于筛选）
"""

from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402
from PIL import Image  # noqa: E402

from screenrecall import db, search, storage, timeutil  # noqa: E402
from screenrecall.config import load_config  # noqa: E402

SCENARIOS = [("code", "代码 / 终端"), ("chat", "聊天 / 沟通"), ("web", "网页"), ("table_doc", "表格 / 文档")]
SPLITS = [("dev", "开发集 dev"), ("blind", "盲测集 blind")]
LANGS = [("zh", "中文"), ("en", "英文")]
MODES = [("text", "文字"), ("image", "画面")]
NAME = dict(SCENARIOS + SPLITS + LANGS + MODES)
PER_CELL = 5          # 方案：每侧每场景 5 条
NEIGHBORS = 4         # 当前画面前后各显示几张
ALL = "（全部）"

DATASET_DIR = ROOT / os.environ.get("LABEL_DATASET", "datasets/local-20261019")
QUERIES_PATH = DATASET_DIR / "queries.json"
LABELS_CSV = DATASET_DIR / "labels.csv"

st.set_page_config(page_title="ScreenRecall 出题", page_icon="📝", layout="wide")
CFG = load_config()
ss = st.session_state


# ======================= 数据 =======================

def load_frames() -> list[dict]:
    """原图可用的画面，按首次出现时间排序；取最早一次出现的应用、窗口标题、录制段。"""
    conn = db.connect(CFG.db_path)
    try:
        rows = conn.execute(
            "SELECT f.frame_id, f.image_path, o.first_seen_ms, o.app, o.window_title, o.session_id "
            "FROM frames f JOIN occurrences o ON o.occurrence_id = ("
            "  SELECT occurrence_id FROM occurrences WHERE frame_id = f.frame_id "
            "  ORDER BY first_seen_ms, occurrence_id LIMIT 1) "
            "WHERE f.image_status = 'ok' ORDER BY o.first_seen_ms, f.frame_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def load_labels() -> dict[str, str]:
    if not LABELS_CSV.exists():
        return {}
    with open(LABELS_CSV, encoding="utf-8-sig", newline="") as fh:
        return {r["frame_id"]: (r.get("final_label") or r.get("review_label") or r.get("label") or "")
                for r in csv.DictReader(fh)}


def ocr_text(frame_id: str) -> str:
    conn = db.connect(CFG.db_path)
    try:
        row = conn.execute("SELECT text FROM frame_text WHERE frame_id = ?", (frame_id,)).fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


def load_queries() -> list[dict]:
    if not QUERIES_PATH.exists():
        return []
    return json.loads(QUERIES_PATH.read_text(encoding="utf-8"))


def save_queries() -> bool:
    try:
        DATASET_DIR.mkdir(parents=True, exist_ok=True)
        tmp = QUERIES_PATH.with_name(QUERIES_PATH.name + ".tmp")
        tmp.write_text(json.dumps(ss.queries, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, QUERIES_PATH)
        return True
    except OSError as exc:
        ss.msg = ("error", f"保存失败：{exc}")
        return False


@st.cache_data(max_entries=400, show_spinner=False)
def thumb(path: str, w: int, h: int) -> Image.Image | None:
    p = Path(path)
    if not p.exists():
        return None
    with Image.open(p) as im:
        im = im.convert("RGB")
        im.thumbnail((w, h))
        return im


def img_path(f: dict) -> str:
    return str(storage.resolve(CFG.data_dir, f["image_path"]))


# ======================= 初始化 =======================

if not CFG.db_path.exists():
    st.error(f"没有找到数据库：{CFG.db_path}")
    st.stop()

if "frames" not in ss:
    ss.frames = load_frames()
    ss.by_id = {f["frame_id"]: f for f in ss.frames}
    ss.labels = load_labels()
    ss.queries = load_queries()
    ss.cur = ss.frames[0]["frame_id"] if ss.frames else None
    ss.msg = None
    ss.d_relevant = []
    ss.d_editing = None
    ss.d_split, ss.d_scenario, ss.d_language, ss.d_mode = "dev", "code", "zh", "text"
    ss.d_query, ss.d_rationale = "", ""
    ss.f_app, ss.f_title, ss.f_label = ALL, ALL, ALL

frames: list[dict] = ss.frames
if not frames:
    st.info("数据目录里没有原图可用的画面。")
    st.stop()


def nav_list() -> list[str]:
    out = []
    for f in frames:
        if ss.f_app != ALL and f["app"] != ss.f_app:
            continue
        if ss.f_title != ALL and f["window_title"] != ss.f_title:
            continue
        if ss.f_label != ALL and ss.labels.get(f["frame_id"]) != ss.f_label:
            continue
        out.append(f["frame_id"])
    return out


# ======================= 操作（都在回调里改状态） =======================

def go(delta: int) -> None:
    nav = nav_list()
    if not nav:
        return
    i = nav.index(ss.cur) if ss.cur in nav else 0
    ss.cur = nav[max(0, min(len(nav) - 1, i + delta))]


def view(fid: str) -> None:
    ss.cur = fid


def jump_no() -> None:
    nav = nav_list()
    if nav:
        ss.cur = nav[max(0, min(len(nav) - 1, int(ss.jump_no) - 1))]


def toggle_answer(fid: str) -> None:
    if fid in ss.d_relevant:
        ss.d_relevant.remove(fid)
    else:
        ss.d_relevant.append(fid)


def same_title() -> None:
    f = ss.by_id[ss.cur]
    ss.f_app, ss.f_title, ss.f_label = f["app"], f["window_title"], ALL


def clear_filters() -> None:
    ss.f_app, ss.f_title, ss.f_label = ALL, ALL, ALL


def reset_draft() -> None:
    """新建题目：清空查询、依据与答案，保留上一题的 dev/blind、场景、语言、模式，连续出题更快。"""
    ss.d_query, ss.d_rationale, ss.d_relevant, ss.d_editing = "", "", [], None


def _next_id(split: str, scen: str, lang: str, taken: set[str]) -> str:
    n = 1
    while f"{split}-{scen}-{lang}-{n:02d}" in taken:
        n += 1
    return f"{split}-{scen}-{lang}-{n:02d}"


def save_draft() -> None:
    q = ss.d_query.strip()
    why = ss.d_rationale.strip()
    if not q:
        ss.msg = ("error", "查询不能为空")
        return
    parsed = search.parse_query(q)  # 只做语法检查（引号是否成对），不检索
    if parsed.error:
        ss.msg = ("error", parsed.error)
        return
    if not ss.d_relevant:
        ss.msg = ("error", "至少要有 1 张答案画面")
        return
    if not why:
        ss.msg = ("error", "请写判定依据：原图里能看到什么，所以算答对")
        return
    prefix = f"{ss.d_split}-{ss.d_scenario}-{ss.d_language}-"
    others = [x for x in ss.queries if x["id"] != ss.d_editing]
    taken = {x["id"] for x in others}
    qid = ss.d_editing if ss.d_editing and ss.d_editing.startswith(prefix) else _next_id(
        ss.d_split, ss.d_scenario, ss.d_language, taken)
    rel = [f for f in ss.d_relevant if f in ss.by_id]
    item = {
        "id": qid, "split": ss.d_split, "scenario": ss.d_scenario, "language": ss.d_language,
        "mode": ss.d_mode, "query": q, "apps": [], "case_sensitive": False,
        "relevant": rel, "rationale": why,
        # 额外字段（evaluate 不读）：答案来源，用来检查 dev / blind 是否共用同一来源
        "source_titles": sorted({f"{ss.by_id[f]['app']} | {ss.by_id[f]['window_title']}" for f in rel}),
    }
    old = ss.queries
    if ss.d_editing:
        pos = next((i for i, x in enumerate(old) if x["id"] == ss.d_editing), len(old))
        ss.queries = old[:pos] + [item] + old[pos + 1:]
    else:
        ss.queries = old + [item]
    if save_queries():
        ss.msg = ("success", f"已保存 {qid}（{len(rel)} 张答案）")
        reset_draft()
    else:
        ss.queries = old


def load_draft(qid: str) -> None:
    q = next(x for x in ss.queries if x["id"] == qid)
    ss.d_editing = qid
    ss.d_split, ss.d_scenario, ss.d_language, ss.d_mode = q["split"], q["scenario"], q["language"], q.get("mode", "text")
    ss.d_query, ss.d_rationale = q["query"], q.get("rationale", "")
    ss.d_relevant = [f for f in q["relevant"] if f in ss.by_id]
    if ss.d_relevant:
        ss.cur = ss.d_relevant[0]
        clear_filters()
    ss.msg = ("info", f"正在编辑 {qid}")


def delete_query(qid: str) -> None:
    old = ss.queries
    ss.queries = [x for x in old if x["id"] != qid]
    if save_queries():
        if ss.d_editing == qid:
            reset_draft()
        ss.msg = ("success", f"已删除 {qid}")
    else:
        ss.queries = old


# ======================= 侧栏：进度、筛选、题目列表 =======================

with st.sidebar:
    st.header("出题进度")
    qs = ss.queries
    cell = Counter((x["split"], x["scenario"]) for x in qs)
    langs = Counter(x["language"] for x in qs)
    head = "| | " + " | ".join(s for s, _ in SCENARIOS) + " |\n|---|" + "---|" * len(SCENARIOS) + "\n"
    body = "".join(
        f"| {sp} | " + " | ".join(
            (f"**{cell[(sp, sc)]}**" if cell[(sp, sc)] == PER_CELL else str(cell[(sp, sc)])) + f"/{PER_CELL}"
            for sc, _ in SCENARIOS) + " |\n"
        for sp, _ in SPLITS)
    st.markdown(head + body)
    n_rel = sum(len(x["relevant"]) for x in qs)
    st.caption(f"共 {len(qs)} / 40 条 · 中文 {langs['zh']} / 英文 {langs['en']}（目标各 20）· 答案标记 {n_rel} 个")
    # dev 与 blind 共用来源的提醒
    src = defaultdict(set)
    for x in qs:
        for t in x.get("source_titles", []):
            src[t].add(x["split"])
    shared = [t for t, s in src.items() if len(s) == 2 and not t.endswith("| 微信")]
    if shared:
        st.warning("dev 和 blind 用了同一来源，盲测可能不够\"盲\"：\n\n" + "\n".join(f"- {t[:60]}" for t in shared))
    st.divider()

    st.subheader("浏览筛选")
    apps = [ALL] + sorted({f["app"] for f in frames})
    if ss.f_app not in apps:
        ss.f_app = ALL
    st.selectbox("应用", apps, key="f_app")
    titles = [ALL] + sorted({f["window_title"] for f in frames if ss.f_app in (ALL, f["app"])})
    if ss.f_title not in titles:
        ss.f_title = ALL
    st.selectbox("窗口标题", titles, key="f_title", format_func=lambda t: t if len(t) < 40 else t[:40] + "…")
    label_opts = [ALL] + [s for s, _ in SCENARIOS]
    st.selectbox("标注类别", label_opts, key="f_label",
                 format_func=lambda x: x if x == ALL else f"{x}（{NAME[x]}）")
    nav = nav_list()
    c1, c2 = st.columns(2)
    c1.button("只看当前窗口", on_click=same_title, width="stretch",
              help="同一份文档 / 同一篇文章的截图可能隔了别的画面，用这个把它们都翻出来")
    c2.button("清除筛选", on_click=clear_filters, width="stretch")
    st.divider()

    st.subheader(f"已出题目（{len(qs)}）")
    for x in sorted(qs, key=lambda x: x["id"]):
        with st.container(border=True):
            st.markdown(f"**{x['id']}** · {NAME[x.get('mode', 'text')]} · {len(x['relevant'])} 张")
            st.caption(x["query"][:60])
            a, b = st.columns(2)
            a.button("编辑", key=f"q-edit-{x['id']}", on_click=load_draft, args=(x["id"],), width="stretch")
            b.button("删除", key=f"q-del-{x['id']}", on_click=delete_query, args=(x["id"],), width="stretch")
    st.caption(f"题目文件：`{QUERIES_PATH.relative_to(ROOT)}`（已 gitignore，含 frame_id 与判定依据）")

# ======================= 主区 =======================

if ss.msg:
    kind, text = ss.msg
    getattr(st, kind)(text)

if not nav:
    st.info("当前筛选下没有画面。")
    st.stop()
if ss.cur not in nav:
    ss.cur = nav[0]
pos = nav.index(ss.cur)
cur = ss.by_id[ss.cur]
is_ans = ss.cur in ss.d_relevant

c_img, c_form = st.columns([3, 2])

with c_img:
    st.markdown(f"### 第 {pos + 1} / {len(nav)} 张" + ("　✅ 已设为本题答案" if is_ans else ""))
    lab = ss.labels.get(ss.cur, "")
    st.caption(f"{timeutil.fmt_local(cur['first_seen_ms'])} · {cur['app']} · {cur['window_title'][:70]}"
               + (f" · 类别 {lab}" if lab else ""))
    im = thumb(img_path(cur), 1100, 700)
    if im is None:
        st.warning("原图文件不存在")
    else:
        st.image(im, width="stretch")
    b1, b2, b3 = st.columns(3)
    b1.button("← 上一张", on_click=go, args=(-1,), shortcut="Left", disabled=pos == 0, width="stretch")
    b2.button("移出答案 (A)" if is_ans else "设为答案 (A)", key="toggle-cur", on_click=toggle_answer,
              args=(ss.cur,), shortcut="A", type="primary" if not is_ans else "secondary", width="stretch")
    b3.button("下一张 →", on_click=go, args=(1,), shortcut="Right", disabled=pos >= len(nav) - 1, width="stretch")
    st.number_input("跳到第几张", 1, len(nav), pos + 1, key="jump_no", on_change=jump_no)
    with st.expander("这张图的 OCR 文字（写原词查询时参考）"):
        st.code(ocr_text(ss.cur) or "（没有文字）", language=None, wrap_lines=True, height=220)

    st.markdown("**前后几张**（同一内容常在相邻画面里，**逐张确认**能看到答案再勾选）")
    lo, hi = max(0, pos - NEIGHBORS), min(len(nav), pos + NEIGHBORS + 1)
    near = [fid for fid in nav[lo:hi] if fid != ss.cur]
    cols = st.columns(4)
    for k, fid in enumerate(near):
        f = ss.by_id[fid]
        with cols[k % 4]:
            t = thumb(img_path(f), 320, 200)
            if t is not None:
                st.image(t, width="stretch")
            off = nav.index(fid) - pos
            st.caption(f"{off:+d} · {timeutil.fmt_local(f['first_seen_ms'])[11:]}" + (" · ✅" if fid in ss.d_relevant else ""))
            x, y = st.columns(2)
            x.button("查看", key=f"nb-view-{fid}", on_click=view, args=(fid,), width="stretch")
            y.button("移出" if fid in ss.d_relevant else "答案", key=f"nb-tog-{fid}", on_click=toggle_answer,
                     args=(fid,), width="stretch")

with c_form:
    st.markdown("### " + (f"编辑题目 {ss.d_editing}" if ss.d_editing else "新题目"))
    st.radio("集合", [s for s, _ in SPLITS], key="d_split", horizontal=True, format_func=NAME.get)
    st.radio("场景", [s for s, _ in SCENARIOS], key="d_scenario", horizontal=True, format_func=NAME.get)
    st.radio("语言", [s for s, _ in LANGS], key="d_language", horizontal=True, format_func=NAME.get)
    st.radio("演示模式", [s for s, _ in MODES], key="d_mode", horizontal=True, format_func=NAME.get,
             help="产品演示时用哪种模式搜；五组实验都会跑，这里只是记录")
    st.text_input("查询（按普通用户会怎么问来写，写的时候不要去搜）", key="d_query",
                  placeholder='例：红黑树和 AVL 树的区别　/　"heapq" 任务调度　/　深色背景的代码编辑器')
    st.text_area("判定依据（原图里能看到什么，所以算答对）", key="d_rationale", height=90)
    used = cell[(ss.d_split, ss.d_scenario)] - (1 if ss.d_editing and any(
        x["id"] == ss.d_editing and (x["split"], x["scenario"]) == (ss.d_split, ss.d_scenario) for x in qs) else 0)
    if used >= PER_CELL:
        st.warning(f"{ss.d_split} / {ss.d_scenario} 已有 {used} 条，方案是每侧每场景 {PER_CELL} 条")

    st.markdown(f"**答案画面（{len(ss.d_relevant)} 张）**")
    if not ss.d_relevant:
        st.caption("在左边翻到能回答这道题的画面，点\"设为答案\"或按 A。")
    acols = st.columns(3)
    for k, fid in enumerate(list(ss.d_relevant)):
        f = ss.by_id.get(fid)
        if f is None:
            continue
        with acols[k % 3]:
            t = thumb(img_path(f), 240, 150)
            if t is not None:
                st.image(t, width="stretch")
            st.caption(timeutil.fmt_local(f["first_seen_ms"])[5:])
            x, y = st.columns(2)
            x.button("看", key=f"ans-view-{fid}", on_click=view, args=(fid,), width="stretch")
            y.button("移出", key=f"ans-rm-{fid}", on_click=toggle_answer, args=(fid,), width="stretch")

    s1, s2 = st.columns(2)
    s1.button("保存题目", type="primary", on_click=save_draft, width="stretch")
    s2.button("放弃 / 新建", on_click=reset_draft, width="stretch")
    st.caption("答案必须每张都能在原图里看到；滚动截图里关键内容已移出画面的不算。")
