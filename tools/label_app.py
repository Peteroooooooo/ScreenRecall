"""本地截图标注工具（只在本机用，不是产品页面）。

运行（仓库根目录）：
    .\\.venv\\Scripts\\streamlit.exe run tools/label_app.py --server.port 8502

- 四个类别按钮（快捷键 1–4）：记录类别并自动跳到下一张；
- 异常按钮（快捷键 X）：标记隐私等要删除的画面，再按一次取消；
- 上一张 / 下一张（← / →），可回去改手误：点别的类别会直接覆盖；
- 侧栏可一键删除全部异常画面：走 lifecycle.delete_frame，图片、OCR 文字、向量、出现记录一起删除，不可恢复。

标注写入 datasets/<名称>/labels.csv（screenrecall.dataset 的格式，标完可直接 check / split / 训练分类器），
异常写入同目录 anomalies.json。环境变量：
    SCREENRECALL_DATA_DIR  数据目录（默认 config 里的 data）
    LABEL_DATASET          标注目录（默认 datasets/local-20261019）
    LABEL_ANNOTATOR        标注人（默认 xiaotianyi）
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

from screenrecall import dataset, db, lifecycle, storage  # noqa: E402
from screenrecall.config import load_config  # noqa: E402

# (类别, 按钮文字, 快捷键)。类别名与 datasets/labelset.example.json 一致
LABELS = [
    ("code", "代码 / 终端", "1"),
    ("chat", "聊天 / 沟通", "2"),
    ("web", "网页", "3"),
    ("table_doc", "表格 / 文档", "4"),
]
LABEL_NAME = {lab: name for lab, name, _ in LABELS}
ANNOTATOR = os.environ.get("LABEL_ANNOTATOR", "xiaotianyi")
DATASET_DIR = ROOT / os.environ.get("LABEL_DATASET", "datasets/local-20261019")
CSV_PATH = DATASET_DIR / "labels.csv"
ANOM_PATH = DATASET_DIR / "anomalies.json"
LABELSET_PATH = DATASET_DIR / "labelset.json"

st.set_page_config(page_title="ScreenRecall 标注", page_icon="🏷️", layout="wide")
CFG = load_config()


# ======================= 文件读写 =======================

def _connect():
    return db.connect(CFG.db_path)


def _alive_ids() -> set[str]:
    conn = _connect()
    try:
        return {r[0] for r in conn.execute("SELECT frame_id FROM frames WHERE image_status = 'ok'")}
    finally:
        conn.close()


def init_files() -> None:
    """第一次运行时：建类别表，并从数据库导出待标注清单（已存在则不覆盖）。"""
    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    if not LABELSET_PATH.exists():
        LABELSET_PATH.write_text(
            json.dumps({"labels": [lab for lab, _, _ in LABELS]}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if not CSV_PATH.exists():
        conn = _connect()
        try:
            dataset.export_frames(conn, CSV_PATH)
        finally:
            conn.close()


def _read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def merge_new_frames() -> int:
    """把库里新增的、OCR 已完成的画面追加到 labels.csv 末尾（已有行和已标类别不变）。返回新增行数。"""
    rows = _read_csv(CSV_PATH)
    have = {r["frame_id"] for r in rows}
    tmp = CSV_PATH.with_name("_export.tmp.csv")
    conn = _connect()
    try:
        dataset.export_frames(conn, tmp)
    finally:
        conn.close()
    try:
        new = [r for r in _read_csv(tmp) if r["frame_id"] not in have]
    finally:
        tmp.unlink(missing_ok=True)
    if new:
        out = CSV_PATH.with_name(CSV_PATH.name + ".tmp")
        with open(out, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=dataset.ALL_COLS)
            w.writeheader()
            for r in rows + new:
                w.writerow({k: r.get(k, "") for k in dataset.ALL_COLS})
        os.replace(out, CSV_PATH)
    return len(new)


def load_rows(alive: set[str]) -> list[dict]:
    return [r for r in _read_csv(CSV_PATH) if r["frame_id"] in alive]  # 已删除的画面不再出现


def load_anomalies(alive: set[str]) -> dict[str, str]:
    if not ANOM_PATH.exists():
        return {}
    data = json.loads(ANOM_PATH.read_text(encoding="utf-8"))
    return {fid: reason for fid, reason in data.get("frames", {}).items() if fid in alive}


def persist() -> bool:
    """CSV 与异常清单都先写临时文件再原子替换。文件被 Excel 打开时会失败，界面提示后不丢内存中的状态。"""
    try:
        tmp = CSV_PATH.with_name(CSV_PATH.name + ".tmp")
        with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=dataset.ALL_COLS)
            w.writeheader()
            for r in st.session_state.rows:
                w.writerow({k: r.get(k, "") for k in dataset.ALL_COLS})
        os.replace(tmp, CSV_PATH)
        tmp = ANOM_PATH.with_name(ANOM_PATH.name + ".tmp")
        tmp.write_text(json.dumps({"frames": st.session_state.anom}, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, ANOM_PATH)
        st.session_state.error = None
        return True
    except OSError as exc:
        st.session_state.error = f"保存失败：{exc}（labels.csv 是否被 Excel 打开？关掉后再点一次）"
        return False


# ======================= 状态与操作 =======================

def is_done(r: dict) -> bool:
    return bool(r["label"]) or r["frame_id"] in st.session_state.anom


def first_unlabeled(start: int = 0) -> int:
    rows = st.session_state.rows
    n = len(rows)
    for k in range(n):
        i = (start + k) % n
        if not is_done(rows[i]):
            return i
    return min(start, n - 1) if n else 0


def go(i: int) -> None:
    n = len(st.session_state.rows)
    st.session_state.idx = max(0, min(i, n - 1)) if n else 0


def set_label(lab: str) -> None:
    rows, i = st.session_state.rows, st.session_state.idx
    r = rows[i]
    before = (r["label"], r["annotator"], st.session_state.anom.get(r["frame_id"]))
    r["label"], r["annotator"] = lab, ANNOTATOR
    st.session_state.anom.pop(r["frame_id"], None)  # 标了类别就不再是异常
    if persist():
        go(i + 1)
    else:  # 保存失败就恢复，避免界面与文件不一致
        r["label"], r["annotator"] = before[0], before[1]
        if before[2] is not None:
            st.session_state.anom[r["frame_id"]] = before[2]


def toggle_anomaly() -> None:
    rows, i = st.session_state.rows, st.session_state.idx
    r = rows[i]
    fid = r["frame_id"]
    if fid in st.session_state.anom:
        st.session_state.anom.pop(fid)
        if not persist():
            st.session_state.anom[fid] = "异常"
        return
    before = (r["label"], r["annotator"])
    st.session_state.anom[fid] = "异常"
    r["label"], r["annotator"] = "", ""  # 异常画面不参与分类
    if persist():
        go(i + 1)
    else:
        st.session_state.anom.pop(fid, None)
        r["label"], r["annotator"] = before


def jump_to_unlabeled() -> None:
    """从头找第一张还没处理的画面，跳着标时也不会漏掉前面的空白。"""
    go(first_unlabeled(0))


def jump_to_number() -> None:
    go(int(st.session_state.jump_no) - 1)


def delete_anomalies() -> None:
    """逐张调用 lifecycle：先取删除计划（含收藏），再删除。删除成功的从清单与 CSV 里移除。"""
    removed: set[str] = set()
    errors: list[str] = []
    files = 0
    conn = _connect()
    try:
        for fid in list(st.session_state.anom):
            try:
                plan = lifecycle.plan_frame_delete(conn, fid)
                if plan is not None:
                    res = lifecycle.delete_frame(conn, CFG.data_dir, fid, confirmed_favorites=plan.favorites)
                    files += res.files_removed
                removed.add(fid)
            except Exception as exc:  # noqa: BLE001 - 单张失败不影响其他
                errors.append(f"{fid[:8]}：{type(exc).__name__}: {exc}")
    finally:
        conn.close()
    for fid in removed:
        st.session_state.anom.pop(fid, None)
    cur = st.session_state.rows[st.session_state.idx]["frame_id"] if st.session_state.rows else None
    st.session_state.rows = [r for r in st.session_state.rows if r["frame_id"] not in removed]
    ids = [r["frame_id"] for r in st.session_state.rows]
    go(ids.index(cur) if cur in ids else st.session_state.idx)
    persist()
    st.session_state.confirm_del = False
    st.session_state.delete_report = (len(removed), files, errors)


# ======================= 初始化 =======================

if not CFG.db_path.exists():
    st.error(f"没有找到数据库：{CFG.db_path}。请确认 SCREENRECALL_DATA_DIR 指向录制好的数据目录。")
    st.stop()

def reload_from_db() -> None:
    """侧栏"重新载入"：清掉会话状态，下次运行时重新合并新帧并读取文件。"""
    for k in ("rows", "anom", "idx", "error", "delete_report", "merged"):
        st.session_state.pop(k, None)


if "rows" not in st.session_state:
    init_files()
    st.session_state.merged = merge_new_frames()
    alive = _alive_ids()
    st.session_state.rows = load_rows(alive)
    st.session_state.anom = load_anomalies(alive)
    st.session_state.idx = first_unlabeled(0) if st.session_state.rows else 0
    st.session_state.error = None
    st.session_state.delete_report = None

rows: list[dict] = st.session_state.rows
anom: dict[str, str] = st.session_state.anom
n = len(rows)


@st.cache_data(max_entries=64, show_spinner=False)
def display_image(path: str, _mtime: float) -> Image.Image:
    """缩到适合屏幕的尺寸再显示（原图不变），按钮不用滚动就能点到。"""
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((1280, 760))
        return im


# ======================= 侧栏：进度与删除 =======================

with st.sidebar:
    st.header("标注进度")
    counts = {lab: sum(1 for r in rows if r["label"] == lab) for lab, _, _ in LABELS}
    done = sum(1 for r in rows if is_done(r))
    st.progress(done / n if n else 1.0, text=f"已处理 {done} / {n}")
    for lab, name, key in LABELS:
        st.caption(f"[{key}] {name}（{lab}）：{counts[lab]}")
    st.caption(f"[X] 异常：{len(anom)}　·　未标注：{n - done}")
    st.divider()
    st.number_input("跳到第几张", min_value=1, max_value=max(n, 1), value=st.session_state.idx + 1,
                    step=1, key="jump_no", on_change=jump_to_number)
    st.button("跳到第一张未标注", on_click=jump_to_unlabeled, width="stretch", disabled=done == n)
    st.button("重新载入（并入新录的画面）", on_click=reload_from_db, width="stretch",
              help="补录后点这里：只追加库里新增、OCR 已完成的画面，已标注的不受影响")
    if st.session_state.get("merged"):
        st.success(f"本次并入了 {st.session_state.merged} 张新画面，已放在清单末尾")
    st.divider()
    with st.expander(f"删除异常画面（{len(anom)} 张）", expanded=False):
        st.caption("会永久删除这些画面的图片、OCR 文字、向量和出现记录，搜索也搜不到，**不可恢复**。建议先停掉录制后台。")
        ok = st.checkbox("我确认永久删除这些画面", key="confirm_del")
        st.button("删除全部异常画面", type="primary", disabled=not anom or not ok,
                  on_click=delete_anomalies, width="stretch")
    rep = st.session_state.delete_report
    if rep:
        n_del, n_files, errs = rep
        st.success(f"已删除 {n_del} 张画面（删除图片文件 {n_files} 个）")
        for e in errs:
            st.error(e)
    st.divider()
    st.caption(f"标注文件：`{CSV_PATH.relative_to(ROOT)}`")
    st.caption(f"数据目录：`{CFG.data_dir}`")
    st.caption("按画面**内容**标，不按应用：微信里打开的网页标 web，浏览器里的在线表格标 table_doc。")
    st.caption("labels.csv 含窗口标题，可能有真实群名人名，**不要提交到 git**。")

# ======================= 主区：当前画面 =======================

if st.session_state.error:
    st.error(st.session_state.error)

if n == 0:
    st.info("没有可标注的画面（数据目录里没有 OCR 完成且原图可用的帧）。")
    st.stop()

r = rows[st.session_state.idx]
fid = r["frame_id"]
if fid in anom:
    status = "⚠️ 已标为异常（将被删除）"
elif r["label"]:
    status = f"✅ 已标注：{LABEL_NAME.get(r['label'], r['label'])}（{r['label']}）"
else:
    status = "⬜ 未标注"

st.markdown(f"### 第 {st.session_state.idx + 1} / {n} 张　{status}")
st.caption(f"{r['captured_local']} · 应用 {r['app'] or '—'} · 窗口 {r['window_title'][:60] or '—'}")

c_img, c_btn = st.columns([4, 1])
with c_img:
    path = storage.resolve(CFG.data_dir, r["image_path"])
    if path.exists():
        st.image(display_image(str(path), path.stat().st_mtime), width="stretch")
    else:
        st.warning("原图文件不存在（可能已被删除），可以直接跳过或标为异常。")

with c_btn:
    st.markdown("**类别**")
    for lab, name, key in LABELS:
        st.button(f"{key}　{name}", key=f"lab-{lab}", on_click=set_label, args=(lab,), shortcut=key,
                  type="primary" if r["label"] == lab else "secondary", width="stretch")
    st.markdown("**其他**")
    st.button("X　取消异常标记" if fid in anom else "X　异常 / 隐私（待删除）", key="anomaly",
              on_click=toggle_anomaly, shortcut="X", width="stretch")
    st.markdown("**翻页**")
    p, q = st.columns(2)
    p.button("← 上一张", key="prev", on_click=go, args=(st.session_state.idx - 1,), shortcut="Left",
             disabled=st.session_state.idx == 0, width="stretch")
    q.button("下一张 →", key="next", on_click=go, args=(st.session_state.idx + 1,), shortcut="Right",
             disabled=st.session_state.idx >= n - 1, width="stretch")
    st.caption("点类别会自动跳到下一张；回到上一张再点别的类别就是改标注。")
