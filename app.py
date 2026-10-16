"""ScreenRecall 页面：录制控制、文字检索与原图核验（P0）。

运行（在仓库根目录）：
    python -m screenrecall.daemon     # 终端 1：后台
    streamlit run app.py              # 终端 2：页面，仅绑定 127.0.0.1
页面只读取已完成的索引；录制状态以后台 ACK 为准；关闭页面不会暂停后台。
"""

from __future__ import annotations

import base64
import io
import re
from contextlib import contextmanager

import streamlit as st
from PIL import Image, ImageDraw

from screenrecall import control, db, search, storage, timeutil, winapi
from screenrecall.config import load_config

st.set_page_config(page_title="ScreenRecall", page_icon="🔎", layout="wide")

CFG = load_config()
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~])")


def md(text: str | None) -> str:
    """转义窗口标题等外部文本，避免被当成 Markdown 渲染。"""
    return _MD_SPECIAL.sub(r"\\\1", text or "")


@contextmanager
def open_db():
    conn = db.connect(CFG.db_path)
    try:
        yield conn
    finally:
        conn.close()


if not CFG.db_path.exists():
    st.title("ScreenRecall")
    st.error(f"还没有找到数据库：{CFG.db_path}")
    st.markdown("请先在仓库根目录运行后台：`python -m screenrecall.daemon`，再刷新本页。")
    st.stop()


# ======================= 侧栏：录制状态与控制 =======================

def _age(ms: int | None, now: int) -> str:
    if not ms:
        return "—"
    s = max(0, (now - ms) // 1000)
    return f"{s} 秒" if s < 120 else f"{s // 60} 分钟"


@st.fragment(run_every="2s")
def status_panel() -> None:
    with open_db() as conn:
        ctrl = control.read(conn)
        stats = db.index_stats(conn)
    now = timeutil.utc_ms()
    target = f"{md(ctrl['target_title'])}（{md(ctrl['target_process'])}）"
    if not control.daemon_alive(ctrl, now):
        st.error("后台未运行或无响应。已保存的内容仍可搜索。")
        st.caption("启动：`python -m screenrecall.daemon`")
    elif control.is_pending(ctrl):
        st.info("正在开启录制…" if ctrl["desired_state"] == "recording" else "正在暂停…")
    elif ctrl["actual_state"] == "recording":
        st.success(f"录制中：{target}")
        if ctrl["capture_note"]:
            st.warning(ctrl["capture_note"])
    else:
        st.info(f"未在录制。{ctrl['state_reason'] or ''}")

    c1, c2 = st.columns(2)
    c1.metric("OCR 待办", stats["backlog"], help="尚未完成文字识别的画面数；这些画面暂时搜不到")
    c2.metric("最老待办", _age(stats["backlog_oldest_ms"], now) if stats["backlog"] else "—")
    c1.metric("已保存画面", stats["frames"])
    c2.metric("出现记录", stats["occurrences"])
    st.caption(
        f"图片占用 {stats['image_bytes'] / 1_048_576:.1f} MiB · OCR 失败 {stats['failed']} · "
        f"最近采样 {_age(ctrl['last_capture_ms'], now)}前 · 心跳 {_age(ctrl['heartbeat_ms'], now)}前"
    )


@st.fragment
def control_panel() -> None:
    if st.button("刷新窗口列表") or "windows" not in st.session_state:
        st.session_state.windows = {w.hwnd: w for w in winapi.list_windows()}
    windows: dict[int, winapi.WindowInfo] = st.session_state.windows
    hwnd = st.selectbox(
        "要录制的窗口",
        list(windows),
        format_func=lambda h: f"{windows[h].title[:50]} — {windows[h].process}",
        index=None,
        placeholder="选择一个窗口",
        key="target_hwnd",
    )
    c1, c2 = st.columns(2)
    start = c1.button("开始录制", type="primary", disabled=hwnd is None, width="stretch")
    pause = c2.button("暂停录制", width="stretch")
    if start or pause:
        with open_db() as conn:
            if not control.daemon_alive(control.read(conn)):
                st.error("后台未运行，请求没有发送。")
                return
            if start:
                w = windows[hwnd]
                control.send_request(conn, "recording", hwnd=w.hwnd, process=w.process, title=w.title)
            else:
                control.send_request(conn, "paused")
        st.toast("请求已发送，等待后台确认")


with st.sidebar:
    st.header("录制")
    status_panel()
    st.divider()
    control_panel()
    st.caption("页面只在本机访问（127.0.0.1），没有登录鉴权。请只录制受控的测试内容。")


# ======================= 主区：检索 =======================

st.title("ScreenRecall")
st.caption(f"按原词找回保存过的屏幕。结果是候选，请以原图核对。时间按本机时区显示（{timeutil.tz_label()}）。")

with open_db() as conn:
    apps = search.known_apps(conn)

with st.form("search_form"):
    q = st.text_input(
        "搜索文字",
        placeholder='例如：0x80070005　或　"Model-X" 电池容量（引号内为必须包含的原串）',
        key="q_input",
    )
    c1, c2, c3, c4 = st.columns([3, 3, 2, 1])
    days = c1.date_input("捕获日期（可选）", value=(), format="YYYY/MM/DD")
    chosen_apps = c2.multiselect("应用（可选）", apps)
    case_sensitive = c3.checkbox("区分大小写")
    limit = c4.selectbox("数量", [10, 20, 50], index=0)
    submitted = st.form_submit_button("搜索", type="primary", key="search_submit")

if submitted:
    start_ms = end_ms = None
    if days:
        d0 = days[0]
        d1 = days[1] if len(days) > 1 else days[0]
        start_ms, end_ms = timeutil.local_dates_to_utc_range(d0, d1)
    st.session_state.query = {
        "raw": q,
        "filters": search.Filters(start_ms, end_ms, tuple(chosen_apps)),
        "case_sensitive": case_sensitive,
        "limit": limit,
    }
    st.session_state.pop("selected", None)


def draw_highlights(img: Image.Image, quads: list[list[list[float]]]) -> Image.Image:
    base = img.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    for quad in quads:
        d.polygon([tuple(p) for p in quad], fill=(255, 196, 0, 60), outline=(230, 60, 0, 255), width=3)
    return Image.alpha_composite(base, overlay).convert("RGB")


@st.cache_data(max_entries=300, show_spinner=False)
def thumbnail(path: str, _mtime: float) -> bytes:
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((480, 300))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()


def render_detail(conn, sel: dict, parsed: search.ParsedQuery, case_sensitive: bool) -> None:
    d = search.frame_detail(conn, sel["frame_id"])
    if d is None:
        st.warning("这条记录已不存在。")
        return
    occ = next((o for o in d.occurrences if o.occurrence_id == sel["occurrence_id"]), d.occurrences[0])
    head, close = st.columns([6, 1])
    head.subheader("原图核验")
    if close.button("关闭", width="stretch"):
        st.session_state.pop("selected", None)
        st.rerun()
    st.markdown(
        f"**本次出现**：{timeutil.fmt_local(occ.first_seen_ms)} 至 {timeutil.fmt_local(occ.last_seen_ms)}"
        f"（采样 {occ.observations} 次）· {md(occ.app)} · {md(occ.window_title)}"
    )
    st.caption("首次与最近观察是采样边界，不能证明两次采样之间画面一直不变。")

    spans = [ln["span"] for ln in d.lines]
    hit = search.match_lines(d.text, d.text_fold, spans, parsed.quoted + parsed.terms, case_sensitive)
    path = storage.resolve(CFG.data_dir, d.image_path)
    if d.image_status != "ok" or not path.exists():
        st.warning("原图已不可用（过期或缺失），只保留了文字。")
    else:
        with Image.open(path) as im:
            img = draw_highlights(im, [d.lines[i]["quad"] for i in hit])
        zoom = st.radio("显示", ["适应宽度", "原始像素 100%"], horizontal=True, key="zoom")
        if zoom == "适应宽度":
            st.image(img, width="stretch", output_format="PNG")
        else:
            buf = io.BytesIO()
            img.save(buf, "PNG", compress_level=1)
            b64 = base64.b64encode(buf.getvalue()).decode()
            st.html(
                '<div style="overflow:auto;max-height:80vh;border:1px solid #ccc">'
                f'<img alt="原图" src="data:image/png;base64,{b64}" style="max-width:none;display:block"></div>'
            )
        st.caption(
            f"{d.width}×{d.height} 物理像素 · 捕获方式 {d.capture_meta.get('method')} · DPI {d.capture_meta.get('dpi')} · "
            f"橙框为本次查询命中的 OCR 行（{len(hit)} 行）"
        )
        st.download_button("下载原图", path.read_bytes(), file_name=f"{d.frame_id}.webp", mime="image/webp")

    st.markdown("**OCR 原文**")
    st.code(d.text or "（识别完成，但没有文字）", language=None, wrap_lines=True, height=260)
    st.markdown("**出现记录**（同一内容被多次观察到）")
    st.dataframe(
        [
            {
                "首次观察": timeutil.fmt_local(o.first_seen_ms),
                "最近观察": timeutil.fmt_local(o.last_seen_ms),
                "采样次数": o.observations,
                "应用": o.app,
                "窗口标题": o.window_title,
                "当前": "◀" if o.occurrence_id == occ.occurrence_id else "",
            }
            for o in d.occurrences
        ],
        hide_index=True,
    )


query = st.session_state.get("query")
if query:
    parsed = search.parse_query(query["raw"])
    with open_db() as conn:
        resp = search.search(
            conn, parsed, query["filters"], case_sensitive=query["case_sensitive"], limit=query["limit"]
        )
        sel = st.session_state.get("selected")
        if sel:
            with st.container(border=True):
                render_detail(conn, sel, parsed, query["case_sensitive"])

    if resp.message:
        st.info(resp.message)
    else:
        basis = {"time": "最新匹配时间", "bm25": "全文相关性", "terms": "命中片段数"}[resp.rank_basis]
        st.caption(f"共 {resp.total} 个匹配画面，按{basis}排序，显示前 {len(resp.results)} 个。排序依据不是正确率。")
        for r in resp.results:
            with st.container(border=True):
                c_img, c_info = st.columns([1, 3])
                path = storage.resolve(CFG.data_dir, r.image_path)
                if r.image_status == "ok" and path.exists():
                    c_img.image(thumbnail(str(path), path.stat().st_mtime), width="stretch")
                else:
                    c_img.caption("原图已不可用")
                c_info.markdown(
                    f"**{timeutil.fmt_local(r.latest.last_seen_ms)}** · {md(r.latest.app)} · {md(r.latest.window_title)}"
                )
                if r.occurrences_in_filter > 1:
                    c_info.caption(f"筛选范围内出现 {r.occurrences_in_filter} 次，显示最近一次")
                if r.matched_terms:
                    c_info.caption("命中片段：" + "、".join(md(t) for t in r.matched_terms))
                c_info.code("\n".join(r.snippet) or "（无文字）", language=None, wrap_lines=True)
                if c_info.button("查看原图", key=f"open-{r.frame_id}"):
                    st.session_state.selected = {"frame_id": r.frame_id, "occurrence_id": r.latest.occurrence_id}
                    st.rerun()
else:
    st.info("在上方输入要找的文字。只有完成 OCR 的画面才能被搜到，侧栏会显示待办数量。")
