"""事后补充判定页面（pooling，规则见 datasets/pooling.py）。只在本机用。

    $env:SCREENRECALL_DATA_DIR = "data-dataset"
    .\\.venv\\Scripts\\streamlit.exe run tools/pool_app.py --server.port 8504

两人各自选自己的名字独立判，互不查看对方结果。页面不显示是哪组实验、排第几、相似度，只给：查询、出题时的
判定依据、一张原真值里的参考图，以及待判的候选原图。判定写入 results/pooling/judgments-<名字>.csv。
"""
from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402
from PIL import Image  # noqa: E402

from screenrecall import db, storage, timeutil  # noqa: E402
from screenrecall.config import load_config  # noqa: E402

POOL = ROOT / os.environ.get("POOL_FILE", "results/pooling/pool.json")
JUDGES = {"xiao": "肖天奕", "cao": "曹贺乔"}
VERDICTS = {"relevant": "✅ 相关（能看到依据说的内容）", "not_relevant": "❌ 不相关", "unsure": "❓ 拿不准"}

st.set_page_config(page_title="ScreenRecall 补充判定", page_icon="⚖️", layout="wide")
CFG = load_config()
ss = st.session_state


@st.cache_data
def load_static():
    pool = json.loads(POOL.read_text(encoding="utf-8"))
    queries = {q["id"]: q for q in json.loads((ROOT / pool["queries_file"]).read_text(encoding="utf-8"))}
    conn = db.connect(CFG.db_path)
    try:
        info = {r["frame_id"]: dict(r) for r in conn.execute(
            "SELECT f.frame_id, f.image_path, o.first_seen_ms, o.app, o.window_title FROM frames f "
            "JOIN occurrences o ON o.occurrence_id = (SELECT occurrence_id FROM occurrences WHERE frame_id = f.frame_id "
            "ORDER BY first_seen_ms, occurrence_id LIMIT 1)")}
        text = {r[0]: r[1] for r in conn.execute("SELECT frame_id, text FROM frame_text")}
    finally:
        conn.close()
    return pool["items"], queries, info, text


def jpath(judge: str) -> Path:
    return POOL.parent / f"judgments-{judge}.csv"


def read(judge: str) -> dict[tuple[str, str], dict]:
    p = jpath(judge)
    if not p.exists():
        return {}
    with open(p, encoding="utf-8-sig", newline="") as fh:
        return {(r["query_id"], r["frame_id"]): r for r in csv.DictReader(fh)}


def write(judge: str, rows: dict[tuple[str, str], dict]) -> None:
    p = jpath(judge)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["query_id", "frame_id", "verdict", "note", "judge"])
        w.writeheader()
        w.writerows(rows.values())
    os.replace(tmp, p)


@st.cache_data(max_entries=200, show_spinner=False)
def image(path: str, w: int) -> Image.Image | None:
    p = storage.resolve(CFG.data_dir, path)
    if not p.exists():
        return None
    with Image.open(p) as im:
        im = im.convert("RGB")
        im.thumbnail((w, w * 2))
        return im


items, queries, info, text = load_static()

with st.sidebar:
    st.header("补充判定")
    judge = st.radio("我是", list(JUDGES), format_func=lambda j: JUDGES[j], key="judge", horizontal=True)
    mine = read(judge)
    st.progress(len(mine) / len(items), text=f"已判 {len(mine)} / {len(items)}")
    only_todo = st.checkbox("只看没判过的", value=True)
    qids = sorted({it["query_id"] for it in items})
    qsel = st.selectbox("按查询筛选", ["（全部）"] + qids)
    st.caption("规则：只按查询和判定依据看原图，能看到依据所说的内容才算相关；同一条消息出现在聊天列表"
               "预览里、能读出关键内容的也算。不看别人的判定。")

todo = [it for it in items if (qsel == "（全部）" or it["query_id"] == qsel)
        and (not only_todo or (it["query_id"], it["frame_id"]) not in mine)]
if not todo:
    st.success("当前筛选下都判完了。")
    st.stop()
ss.setdefault("pos", 0)
ss.pos = max(0, min(ss.pos, len(todo) - 1))
it = todo[ss.pos]
qid, fid = it["query_id"], it["frame_id"]
q = queries[qid]
f = info[fid]


def save(verdict: str) -> None:
    rows = read(ss.judge)
    rows[(qid, fid)] = {"query_id": qid, "frame_id": fid, "verdict": verdict, "note": ss.get("note", ""),
                        "judge": ss.judge}
    write(ss.judge, rows)
    ss.note = ""
    if not only_todo:
        ss.pos += 1


st.markdown(f"### {q['query']}")
st.caption(f"{qid} · {q['split']} · {q['scenario']} · {q['language']} · 模式 {q['mode']}　|　第 {ss.pos + 1} / {len(todo)} 对")
st.info("判定依据（出题时写的）：" + q.get("rationale", ""))

c1, c2 = st.columns([3, 2])
with c1:
    st.markdown("**待判的画面**")
    st.caption(f"{timeutil.fmt_local(f['first_seen_ms'])} · {f['app']} · {(f['window_title'] or '')[:70]}")
    im = image(f["image_path"], 1400)
    st.image(im, width="stretch") if im else st.warning("原图不可用")
    with st.expander("OCR 文字（辅助，以原图为准）"):
        st.text(text.get(fid, ""))
with c2:
    st.markdown("**参考：原真值里的一张答案画面**")
    ref = info[q["relevant"][0]]
    st.caption(f"{ref['app']} · {(ref['window_title'] or '')[:50]}　（共 {len(q['relevant'])} 张原答案）")
    rim = image(ref["image_path"], 800)
    st.image(rim, width="stretch") if rim else st.warning("原图不可用")
    prev = mine.get((qid, fid))
    if prev:
        st.caption(f"我之前判的：{VERDICTS.get(prev['verdict'], prev['verdict'])}")
    st.text_input("备注（可选，如：只在会话列表预览里看到）", key="note")
    for v, label in VERDICTS.items():
        st.button(label, key=f"v-{v}", on_click=save, args=(v,), width="stretch")
    a, b = st.columns(2)
    a.button("← 上一对", on_click=lambda: ss.__setitem__("pos", ss.pos - 1), width="stretch")
    b.button("下一对 →", on_click=lambda: ss.__setitem__("pos", ss.pos + 1), width="stretch")
