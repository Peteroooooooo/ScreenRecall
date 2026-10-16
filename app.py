"""ScreenRecall 页面：录制控制、文字 / 画面两种搜索与原图核验。

运行（在仓库根目录）：
    python -m screenrecall.daemon     # 终端 1：后台
    streamlit run app.py              # 终端 2：页面，仅绑定 127.0.0.1
页面只读取已完成的索引；录制状态以后台 ACK 为准；关闭页面不会暂停后台。
查询模型按需加载并用 st.cache_resource 按模型版本缓存（文字模式不加载画面模型）；
向量矩阵在每次查询前按 meta.index_revision 判断是否重载（方案 2.5、3.6）。
"""

from __future__ import annotations

import base64
import io
import json
import re
from contextlib import contextmanager
from datetime import datetime

import streamlit as st
from PIL import Image, ImageDraw

from screenrecall import (
    control, db, evidence, importer, lifecycle, models, retrieval, rules, search, storage, timeutil, winapi,
)
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

with open_db() as _conn:
    _ver = int(_conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0])
if _ver < db.SCHEMA_VERSION:
    st.title("ScreenRecall")
    st.error(f"数据库结构是 v{_ver}，代码需要 v{db.SCHEMA_VERSION}。请重启后台（python -m screenrecall.daemon）完成升级后刷新本页。")
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
        maint = lifecycle.last_maintenance(conn)
        n_fav = conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0]
    now = timeutil.utc_ms()
    target = f"{md(ctrl['target_title'])}（{md(ctrl['target_process'])}）"
    if not control.daemon_alive(ctrl, now):
        st.error("后台未运行或无响应。已保存的内容仍可搜索。")
        st.caption("启动：`python -m screenrecall.daemon`")
    elif control.is_pending(ctrl):
        st.info("正在开启录制…" if ctrl["desired_state"] == "recording" else "正在暂停…")
    elif ctrl["actual_state"] == "recording":
        st.success(f"录制中：{target}")
        if ctrl["actual_mode"] == "debug":
            left_s = max(0, ((ctrl["mode_until_ms"] or now) - now) // 1000)
            st.error(
                f"**排错模式**：每 1 秒检查、至少间隔 2 秒自动保存，"
                f"剩余 {left_s // 60:02d}:{left_s % 60:02d} 后自动回到普通模式"
            )
            if st.button("退出排错模式", type="primary", width="stretch", key="exit_debug"):
                _send_mode("normal")
        else:
            st.caption("普通模式：每 3 秒检查，画面稳定后保存")
        if ctrl["capture_note"]:
            st.warning(ctrl["capture_note"])
    else:
        st.info(f"未在录制。{ctrl['state_reason'] or ''}")
    if ctrl["mode_reason"] and ctrl["actual_mode"] == "normal":
        st.caption(ctrl["mode_reason"])
    if ctrl["snapshot_result"]:
        st.caption(f"补拍（{_age(ctrl['snapshot_at_ms'], now)}前）：{ctrl['snapshot_result']}")
    if control.daemon_alive(ctrl, now) and ctrl["hotkey_status"]:
        st.caption(ctrl["hotkey_status"])

    c1, c2 = st.columns(2)
    c1.metric("OCR 待办", stats["backlog"], help="尚未完成文字识别的画面数；这些画面暂时搜不到")
    c2.metric("最老待办", _age(stats["backlog_oldest_ms"], now) if stats["backlog"] else "—")
    c1.metric(
        "语义向量待办", stats["text_backlog"],
        help="文字已可原词搜索、但还没有语义向量的画面数；它们暂时只参与词法排序",
    )
    c2.metric(
        "全图向量待办", stats["image_backlog"],
        help="还没有全图向量的画面数；它们暂时不参与画面模式",
    )
    c1.metric("已保存画面", stats["frames"])
    c2.metric("出现记录", stats["occurrences"])
    vec_oldest = [m for m in (stats["text_oldest_ms"], stats["image_oldest_ms"]) if m]
    st.caption(
        f"图片占用 {stats['image_bytes'] / 1_048_576:.1f} MiB · OCR 失败 {stats['failed']} · "
        f"向量失败 {stats['embed_failed']} · 最老向量待办 {_age(min(vec_oldest), now) if vec_oldest else '—'} · "
        f"最近采样 {_age(ctrl['last_capture_ms'], now)}前 · 心跳 {_age(ctrl['heartbeat_ms'], now)}前"
    )
    if maint:
        rt = CFG.retention
        st.caption(
            f"运行数据 {maint['usage_bytes'] / lifecycle.GIB:.2f} / {maint['quota_bytes'] / lifecycle.GIB:.0f} GiB · "
            f"磁盘剩余 {maint['free_bytes'] / lifecycle.GIB:.1f} GiB · 收藏 {n_fav} 条 · "
            f"原图保留 {rt.image_days} 天、记录保留 {rt.record_days} 天（收藏除外）· "
            f"上次维护 {_age(maint['at_ms'], now)}前"
        )
        if maint.get("paused"):
            st.error(maint["reason"])
    else:
        st.caption(f"收藏 {n_fav} 条 · 后台尚未执行到期与配额维护")


def _send_mode(mode: str) -> None:
    with open_db() as conn:
        ctrl = control.read(conn)
        if not control.daemon_alive(ctrl) or ctrl["actual_state"] != "recording":
            st.toast("未在录制，模式没有切换")
            return
        control.send_mode_request(conn, mode)
    st.toast("请求已发送，等待后台确认")


@st.fragment
def control_panel() -> None:
    if st.button("刷新窗口列表") or "windows" not in st.session_state:
        st.session_state.windows = {w.hwnd: w for w in winapi.list_windows()}
    windows: dict[int, winapi.WindowInfo] = st.session_state.windows
    denied = {h: rules.check(w.process, w.title, CFG.rules) for h, w in windows.items()}
    hwnd = st.selectbox(
        "要录制的窗口",
        list(windows),
        format_func=lambda h: f"{windows[h].title[:50]} — {windows[h].process}" + ("（不可录制）" if denied[h] else ""),
        index=None,
        placeholder="选择一个窗口",
        key="target_hwnd",
    )
    if hwnd is not None and denied.get(hwnd):
        st.warning(f"{denied[hwnd]}，不能录制。")
    c1, c2 = st.columns(2)
    start = c1.button(
        "开始录制", type="primary", disabled=hwnd is None or bool(denied.get(hwnd)), width="stretch"
    )
    pause = c2.button("暂停录制", width="stretch")
    c3, c4 = st.columns(2)
    debug = c3.button("排错模式 30 分钟", width="stretch", help="每 1 秒检查、至少间隔 2 秒自动保存，不等画面稳定；到时自动退出")
    snap = c4.button("立即补拍", width="stretch", help="立即保存当前画面并记一次出现；仍受暂停、锁屏和排除规则限制")
    if debug:
        _send_mode("debug")
        return
    if snap:
        with open_db() as conn:
            ctrl = control.read(conn)
            if not control.daemon_alive(ctrl) or ctrl["actual_state"] != "recording":
                st.error("未在录制，补拍没有发送。")
                return
            control.request_snapshot(conn)
        st.toast("补拍请求已发送")
        return
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
    with st.expander("允许 / 排除规则"):
        for line in rules.describe(CFG.rules):
            st.caption(md(line))
        st.caption("在 config.local.toml 的 [rules] 中修改，重启后台和页面后生效。")
    st.caption("页面只在本机访问（127.0.0.1），没有登录鉴权。请只录制受控的测试内容。")


# ======================= 主区：检索 =======================

st.title("ScreenRecall")
st.caption(
    "按原词、含义或大致画面找回保存过的屏幕。结果是相似候选，请以原图核对。"
    f"时间按本机时区显示（{timeutil.tz_label()}）。"
)

with open_db() as conn:
    apps = search.known_apps(conn)

if st.session_state.get("lifecycle_msg"):
    st.success(st.session_state.pop("lifecycle_msg"))


@st.fragment
def favorites_panel() -> None:
    with open_db() as conn:
        favs = lifecycle.list_favorites(conn)
    if not favs:
        st.caption("还没有收藏。在原图核验页点击\"收藏本次记录\"。")
        return
    st.caption("收藏的记录不随 30/90 天到期或配额清理；取消收藏后按普通规则处理。")
    for f in favs:
        c1, c2 = st.columns([5, 1])
        status = "" if f["image_status"] == "ok" else "（原图已不可用，只保留文字）"
        c1.markdown(f"★ {timeutil.fmt_local(f['first_seen_ms'])} · {md(f['app'])} · {md(f['window_title'])}{status}")
        if c2.button("查看", key=f"favopen-{f['occurrence_id']}", width="stretch"):
            st.session_state.selected = {
                "frame_id": f["frame_id"], "occurrence_id": f["occurrence_id"],
                "mode": retrieval.TEXT, "semantic_lines": [],
            }
            st.session_state.setdefault("query", {
                "raw": "", "mode": retrieval.TEXT, "filters": search.Filters(), "case_sensitive": False, "limit": 10,
            })
            st.rerun(scope="app")


with st.expander("我的收藏"):
    favorites_panel()


@st.fragment
def import_panel() -> None:
    """批量导入（方案 6.8）：逐文件验证后进入与实时截图相同的 OCR / 向量流水线。"""
    files = st.file_uploader(
        "选择截图（PNG、JPEG、WebP，可多选）", type=["png", "jpg", "jpeg", "webp"],
        accept_multiple_files=True, key="import_files",
    )
    label = st.text_input("批次名称（可选）", key="import_label")
    known = st.checkbox("我知道这批截图的捕获时间", key="import_known",
                        help="不勾选时记导入时间并标注\"时间未知\"；不会用文件修改时间代替捕获时间")
    captured_ms = None
    if known:
        c1, c2 = st.columns(2)
        d = c1.date_input("捕获日期", key="import_date")
        t = c2.time_input("捕获时间", key="import_time", step=60)
        captured_ms = int(datetime.combine(d, t).timestamp() * 1000)
    if st.button("导入", disabled=not files, key="import_go"):
        batch = [importer.ImportFile(f.name, f.getvalue()) for f in sorted(files, key=lambda f: f.name)]
        with open_db() as conn:
            res = importer.import_batch(conn, CFG, batch, label=label or None, captured_at_ms=captured_ms)
        st.session_state.import_result = res
    res = st.session_state.get("import_result")
    if res:
        msg = (f"成功 {res.count(importer.OK)}、失败 {res.count(importer.FAILED)}、跳过 {res.count(importer.SKIPPED)}"
               + ("（同一批次重新提交：已成功的跳过，失败的已重试）" if res.resubmitted else ""))
        (st.warning if res.count(importer.FAILED) else st.success)(msg)
        st.dataframe(
            [{"序号": i.seq + 1, "文件": i.name, "结果": {"ok": "成功", "failed": "失败", "skipped": "跳过"}[i.status],
              "原因": i.reason or ""} for i in res.items],
            hide_index=True,
        )
        st.caption("按文件名顺序处理。OCR 与向量由后台补算，完成后才能搜到；"
                   + ("捕获时间为你提供的时间。" if res.time_known else "这批截图标为\"时间未知\"，日期筛选按导入时间计算。"))


with st.expander("批量导入截图"):
    import_panel()
MODE_LABELS = {retrieval.TEXT: "文字", retrieval.IMAGE: "画面"}

with st.form("search_form"):
    mode = st.radio(
        "搜索模式",
        [retrieval.TEXT, retrieval.IMAGE],
        format_func=MODE_LABELS.get,
        horizontal=True,
        key="mode_input",
        help="文字：按 OCR 原词与含义（词法 + 语义融合）；画面：按大致外观描述，如“深色背景、雪山大图”",
    )
    q = st.text_input(
        "搜索内容",
        placeholder='例如：0x80070005　·　"Model-X" 电池容量　·　同事推荐的餐厅　·　深色背景 雪山大图',
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
        "mode": mode,
        "filters": search.Filters(start_ms, end_ms, tuple(chosen_apps)),
        "case_sensitive": case_sensitive,
        "limit": limit,
    }
    st.session_state.pop("selected", None)


# ---------- 查询侧模型：按需加载，按模型版本缓存 ----------

@st.cache_data(ttl=60, show_spinner=False)
def missing_models(_enabled: bool, ids: tuple[str, ...]) -> list[str]:
    """本机缺失的模型（每 60 秒重新检查一次，下载完成后无需重启页面）。"""
    if not _enabled:
        return list(ids)
    return models.available(CFG.models)


@st.cache_resource(show_spinner="正在加载文字语义模型…")
def text_engine(model_id: str):
    return models.StText(CFG.models.text, CFG.models), retrieval.TextVectorIndex(model_id)


@st.cache_resource(show_spinner="正在加载画面描述模型…")
def image_engine(clip_text_id: str, image_id: str):
    return models.StText(CFG.models.clip_text, CFG.models), retrieval.ImageVectorIndex(image_id)


def build_engines(mode: str) -> retrieval.Engines:
    """只加载当前模式需要的编码器；不可用时写明原因，由 retrieval 决定退回词法或提示。"""
    mc = CFG.models
    needed = (mc.text.model_id,) if mode == retrieval.TEXT else (mc.clip_text.model_id, mc.image.model_id)
    eng = retrieval.Engines()
    if not mc.enabled:
        eng.unavailable_reason[mode] = "配置中已关闭向量模型（models.enabled = false）"
        return eng
    absent = [m for m in missing_models(mc.enabled, (mc.text.model_id, mc.image.model_id, mc.clip_text.model_id)) if m in needed]
    if absent:
        eng.unavailable_reason[mode] = "模型未下载（" + "、".join(absent) + "），请运行 python -m screenrecall.models --download"
        return eng
    try:
        if mode == retrieval.TEXT:
            enc, idx = text_engine(mc.text.model_id)
            eng.text_encoder, eng.text_index = enc.encode, idx
        else:
            enc, idx = image_engine(mc.clip_text.model_id, models.image_model_id(mc))
            eng.clip_text_encoder, eng.image_index = enc.encode, idx
    except Exception as exc:  # noqa: BLE001 - 加载失败要在页面说明，不中断页面
        eng.unavailable_reason[mode] = f"模型加载失败：{type(exc).__name__}: {exc}"
    return eng


LEX_STYLE = ((255, 196, 0, 60), (230, 60, 0, 255))  # 橙：原词命中行
SEM_STYLE = ((0, 150, 255, 45), (0, 90, 220, 255))  # 蓝：语义贡献最高的文本块


def draw_highlights(img: Image.Image, boxes: list[tuple[list[list[float]], tuple]]) -> Image.Image:
    base = img.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    for quad, (fill, outline) in boxes:
        d.polygon([tuple(p) for p in quad], fill=fill, outline=outline, width=3)
    return Image.alpha_composite(base, overlay).convert("RGB")


@st.cache_data(max_entries=300, show_spinner=False)
def thumbnail(path: str, _mtime: float) -> bytes:
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((480, 300))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()


def render_detail(conn, sel: dict, parsed: search.ParsedQuery, case_sensitive: bool, relaxed: bool = False) -> None:
    mode = sel.get("mode", retrieval.TEXT)
    sem_lines = set(sel.get("semantic_lines") or [])
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
    if not importer.is_time_known(d.capture_meta):
        st.markdown(
            f"**本次出现**：导入的截图，**捕获时间未知**（导入于 {timeutil.fmt_local(occ.first_seen_ms)}）"
            f" · 文件 {md(occ.window_title)}"
        )
        st.caption("导入时没有提供可信捕获时间，上面是导入时间；文件修改时间不当作捕获时间。")
    elif occ.source_type == "import":
        st.markdown(
            f"**本次出现**：导入的截图，捕获时间 {timeutil.fmt_local(occ.first_seen_ms)}（导入时由用户提供）"
            f" · 文件 {md(occ.window_title)}"
        )
    else:
        st.markdown(
            f"**本次出现**：{timeutil.fmt_local(occ.first_seen_ms)} 至 {timeutil.fmt_local(occ.last_seen_ms)}"
            f"（采样 {occ.observations} 次）· {md(occ.app)} · {md(occ.window_title)}"
        )
        st.caption("首次与最近观察是采样边界，不能证明两次采样之间画面一直不变。")

    spans = [ln["span"] for ln in d.lines]
    # 画面模式只展示整图，不伪造局部定位（方案 5.2、6.2）
    hit = [] if mode == retrieval.IMAGE else search.match_lines(
        d.text, d.text_fold, spans, parsed.quoted + parsed.terms, case_sensitive, relaxed
    )
    sem_only = sorted(i for i in sem_lines if i not in hit and i < len(d.lines))
    path = storage.resolve(CFG.data_dir, d.image_path)
    if d.image_status == "expired":
        st.warning("原图已过期（超过保留期，或因空间配额提前清理），只保留了文字，无法再核验画面细节。")
    elif d.image_status != "ok" or not path.exists():
        st.warning("原图已不可用（文件缺失），只保留了文字。")
    else:
        with Image.open(path) as im:
            img = draw_highlights(
                im,
                [(d.lines[i]["quad"], SEM_STYLE) for i in sem_only] + [(d.lines[i]["quad"], LEX_STYLE) for i in hit],
            )
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
        if mode == retrieval.IMAGE:
            legend = "画面模式按整图相似度排序，不标注局部位置"
        else:
            parts = []
            if hit:
                parts.append(f"橙框为原词命中的 OCR 行（{len(hit)} 行）")
            if sem_only:
                parts.append(f"蓝框为语义贡献最高的文本块（{len(sem_only)} 行）")
            legend = "，".join(parts) or "本次查询没有可标注的 OCR 行"
        st.caption(
            f"{d.width}×{d.height} 物理像素 · 捕获方式 {d.capture_meta.get('method')} · "
            f"DPI {d.capture_meta.get('dpi')} · {legend}"
        )
        st.download_button("下载原图", path.read_bytes(), file_name=f"{d.frame_id}.webp", mime="image/webp")

    render_context(conn, sel, occ.occurrence_id)

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
                "来源": SOURCE_LABELS.get(o.source_type, o.source_type),
                "当前": "◀" if o.occurrence_id == occ.occurrence_id else "",
            }
            for o in d.occurrences
        ],
        hide_index=True,
    )
    if len(d.occurrences) > 1:
        ids = [o.occurrence_id for o in d.occurrences]
        by_id = {o.occurrence_id: o for o in d.occurrences}
        pick = st.selectbox(
            "围绕另一次出现前后浏览",
            ids,
            index=ids.index(occ.occurrence_id),
            format_func=lambda i: f"{timeutil.fmt_local(by_id[i].first_seen_ms)} · {by_id[i].app or ''}",
            key=f"occ-pick-{d.frame_id}",
        )
        if pick != occ.occurrence_id:
            _goto(sel, d.frame_id, pick)

    render_lifecycle(conn, d, occ)


def _after_delete(msg: str) -> None:
    for k in [k for k in st.session_state if str(k).startswith("confirm-")]:
        st.session_state.pop(k, None)
    st.session_state.pop("selected", None)
    st.session_state.pop("compare_with", None)
    st.session_state.lifecycle_msg = msg
    st.rerun()


def render_lifecycle(conn, d, occ) -> None:
    """收藏与删除（方案 5.2、5.4）。删除前都要确认，并写明会删掉什么、保留什么。"""
    st.markdown("**收藏与删除**")
    fav = occ.occurrence_id in lifecycle.favorite_ids(conn, [occ.occurrence_id])
    c1, c2, c3 = st.columns(3)
    if fav:
        if c1.button("取消收藏", key=f"unfav-{occ.occurrence_id}", width="stretch"):
            lifecycle.remove_favorite(conn, occ.occurrence_id)
            st.rerun()
        st.caption("★ 已收藏：这次记录及其原图、文字、特征和来源不随 30/90 天到期或配额清理。")
    else:
        if c1.button("收藏本次记录", key=f"fav-{occ.occurrence_id}", width="stretch"):
            if lifecycle.add_favorite(conn, occ.occurrence_id):
                st.rerun()
            st.error("这条记录已不存在，无法收藏。")
        if d.image_status != "ok":
            st.caption("原图已不可用：收藏只保护现有的文字、特征和来源，不能恢复已删除的原图。")
        else:
            st.caption("收藏保留已保存的文件，不重新编码，也不会让有损截图变清晰。")

    key_occ, key_frame = f"confirm-occ-{occ.occurrence_id}", f"confirm-frame-{d.frame_id}"
    if c2.button("删除本次记录", key=f"del-occ-{occ.occurrence_id}", width="stretch"):
        st.session_state[key_occ] = True
        st.session_state.pop(key_frame, None)
    if c3.button("删除此画面的全部记录", key=f"del-frame-{d.frame_id}", width="stretch"):
        plan = lifecycle.plan_frame_delete(conn, d.frame_id)
        st.session_state[key_frame] = sorted(plan.favorites) if plan else []
        st.session_state.pop(key_occ, None)

    if st.session_state.get(key_occ):
        others = len(d.occurrences) - 1
        lines = [f"将删除 {timeutil.fmt_local(occ.first_seen_ms)} 这一次出现的时间与来源。"]
        if fav:
            lines.append("这次记录的收藏也会一起删除。")
        if others > 0:
            lines.append(f"这幅画面还有 {others} 次其他出现记录，图片、OCR 文字和向量保留，那些记录不受影响。")
        else:
            lines.append("这是这幅画面唯一一次出现，图片、OCR 文字和向量会一起删除。")
        st.warning(" ".join(lines))
        a, b = st.columns(2)
        if a.button("确认删除本次记录", type="primary", key=f"yes-{key_occ}", width="stretch"):
            res = lifecycle.delete_occurrence(conn, CFG.data_dir, occ.occurrence_id)
            _after_delete(
                f"已删除 {res.occurrences_removed} 次出现记录"
                + (f"、{res.favorites_removed} 个收藏" if res.favorites_removed else "")
                + ("，画面内容已一并删除。" if res.frames_purged else "，画面内容仍被其他记录引用，已保留。")
            )
        if b.button("取消", key=f"no-{key_occ}", width="stretch"):
            st.session_state.pop(key_occ, None)
            st.rerun()

    if key_frame in st.session_state:
        plan = lifecycle.plan_frame_delete(conn, d.frame_id)
        if plan is None:
            st.session_state.pop(key_frame, None)
            st.info("这幅画面已被删除。")
            return
        shown = set(st.session_state[key_frame])
        st.error(
            f"将删除这幅画面的全部 {len(plan.occurrences)} 次出现记录"
            + (f"，以及其中 {len(plan.favorites)} 个收藏" if plan.favorites else "")
            + "，并删除图片、OCR 文字和向量。删除后搜索不再返回这些记录。"
            "这是应用数据删除，不保证存储介质上不可恢复。"
        )
        st.dataframe(
            [
                {
                    "首次观察": timeutil.fmt_local(o["first_seen_ms"]),
                    "最近观察": timeutil.fmt_local(o["last_seen_ms"]),
                    "应用": o["app"],
                    "窗口标题": o["window_title"],
                    "来源": SOURCE_LABELS.get(o["source_type"], o["source_type"]),
                    "收藏": "★ 将删除" if o["occurrence_id"] in plan.favorites else "",
                }
                for o in plan.occurrences
            ],
            hide_index=True,
        )
        a, b = st.columns(2)
        if a.button("确认删除全部记录", type="primary", key=f"yes-{key_frame}", width="stretch"):
            try:
                res = lifecycle.delete_frame(conn, CFG.data_dir, d.frame_id, confirmed_favorites=shown)
            except lifecycle.PlanChanged as exc:
                st.session_state[key_frame] = sorted(plan.favorites)
                st.warning(f"{exc}。上表已更新。")
            else:
                _after_delete(
                    f"已删除这幅画面的 {res.occurrences_removed} 次出现记录"
                    + (f"和 {res.favorites_removed} 个收藏" if res.favorites_removed else "") + "。"
                )
        if b.button("取消", key=f"no-{key_frame}", width="stretch"):
            st.session_state.pop(key_frame, None)
            st.rerun()


def _goto(sel: dict, frame_id: str, occurrence_id: str) -> None:
    """切换到另一次出现。语义贡献行只属于搜索结果那一帧，换帧后不再沿用。"""
    origin = sel.get("origin") or {k: sel.get(k) for k in ("frame_id", "occurrence_id", "semantic_lines")}
    st.session_state.selected = {
        "frame_id": frame_id,
        "occurrence_id": occurrence_id,
        "mode": sel.get("mode", retrieval.TEXT),
        "semantic_lines": origin["semantic_lines"] if frame_id == origin["frame_id"] else [],
        "origin": origin,
    }
    st.session_state.pop("compare_with", None)
    st.rerun()


SOURCE_LABELS = {"manual": "补拍", "import": "导入", "live": "自动"}


def _step_caption(s: evidence.Step) -> str:
    src = SOURCE_LABELS.get(s.source_type, s.source_type)
    if s.source_type == "import":
        return f"{md(s.window_title)} · {src}"
    return f"{timeutil.fmt_local(s.first_seen_ms)} · {md(s.app)} · {md(s.window_title)} · {src}"


def _step_image(col, s: evidence.Step) -> None:
    path = storage.resolve(CFG.data_dir, s.image_path)
    if s.image_status == "ok" and path.exists():
        col.image(thumbnail(str(path), path.stat().st_mtime), width="stretch")
    else:
        col.info("原图已过期或缺失，只保留了文字，不生成替代预览。")
    col.caption(_step_caption(s))


def render_context(conn, sel: dict, occurrence_id: str) -> None:
    """前后浏览与并排（方案 6.1）：只在选中出现所在的录制段内移动，跨缺口与段边界明确提示。"""
    ctx = evidence.context(conn, occurrence_id)
    if ctx is None:
        return
    s = ctx.timeline.session
    if s.is_import:
        st.markdown("**前后浏览**（本次出现所在的导入批次）")
        st.caption(
            f"导入批次「{md(s.target_title)}」· 按导入时的文件顺序，第 {ctx.index + 1} / {len(ctx.timeline.steps)} 张。"
            "这只是文件顺序，不是连续的屏幕历史。"
        )
    else:
        st.markdown("**前后浏览**（本次出现所在的录制段）")
        end = timeutil.fmt_local(s.ended_at_ms) if s.ended_at_ms else "—"
        st.caption(
            f"录制段 {timeutil.fmt_local(s.started_at_ms)} 至 {end}（{s.end_label}）· "
            f"第 {ctx.index + 1} / {len(ctx.timeline.steps)} 次出现。只显示这一段，不把其他录制段拼成连续录像。"
        )
    c_prev, c_back, c_next = st.columns([2, 2, 2])
    if ctx.prev and c_prev.button("◀ 上一条", key="ctx-prev", width="stretch"):
        _goto(sel, ctx.prev.frame_id, ctx.prev.occurrence_id)
    if not ctx.prev:
        c_prev.caption("已是本录制段的第一条")
    origin = sel.get("origin")
    if origin and origin["occurrence_id"] != occurrence_id and c_back.button("回到搜索结果", key="ctx-back", width="stretch"):
        _goto(sel, origin["frame_id"], origin["occurrence_id"])
    if ctx.next and c_next.button("下一条 ▶", key="ctx-next", width="stretch"):
        _goto(sel, ctx.next.frame_id, ctx.next.occurrence_id)
    if not ctx.next:
        c_next.caption(f"已是本录制段的最后一条（{s.end_label}）")

    before = evidence.describe_gaps(ctx.gaps_before)
    after = evidence.describe_gaps(ctx.gaps_after)
    if before:
        st.warning(("与上一条之间" if ctx.prev else "录制段开始到这一条之间") + before)
    if after:
        st.warning(("与下一条之间" if ctx.next else "这一条到录制段结束之间") + after)

    options = [k for k, v in (("上一条", ctx.prev), ("下一条", ctx.next)) if v]
    if not options:
        return
    choice = st.radio("并排比较", ["不比较"] + options, horizontal=True, key="compare_with")
    if choice == "不比较":
        return
    other = ctx.prev if choice == "上一条" else ctx.next
    left, right = (other, ctx.current) if choice == "上一条" else (ctx.current, other)
    c1, c2 = st.columns(2)
    _step_image(c1, left)
    _step_image(c2, right)
    gap_msg = before if choice == "上一条" else after
    st.caption("并排只放两张原图，不计算文字或表格差异。" + (gap_msg or "两次出现之间没有记录到缺口。"))


RANK_BASIS = {
    "time": "最新匹配时间",
    "bm25": "全文相关性",
    "terms": "命中片段数",
    "rrf": "词法与语义排名融合（RRF）",
    "image": "画面描述与整图的相似度",
}

query = st.session_state.get("query")
if query:
    parsed = search.parse_query(query["raw"])
    qmode = query.get("mode", retrieval.TEXT)
    # 只有引号原串时不调用模型，也就不必加载
    engines = build_engines(qmode) if parsed.terms and not parsed.error else retrieval.Engines()
    relaxed = bool(query.get("relaxed"))
    with open_db() as conn:
        resp = retrieval.run(
            conn, parsed, query["filters"], mode=qmode, engines=engines,
            case_sensitive=query["case_sensitive"], limit=query["limit"],
            rrf_k=CFG.search.rrf_k, branch_top=CFG.search.branch_top, relaxed=relaxed,
        )
        unknown_time = importer.time_unknown_frames(conn, [r.frame_id for r in resp.results])
        fav_frames = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT frame_id FROM favorites WHERE frame_id IN (SELECT value FROM json_each(?))",
                (json.dumps([r.frame_id for r in resp.results]),),
            )
        }
        sel = st.session_state.get("selected")
        if sel:
            with st.container(border=True):
                render_detail(conn, sel, parsed, query["case_sensitive"], resp.relaxed)

    for note in resp.notes:
        st.warning(note)
    if resp.relaxed and st.button("恢复严格匹配", key="strict_again"):
        st.session_state.query = {**query, "relaxed": False}
        st.session_state.pop("selected", None)
        st.rerun()
    if resp.message:
        st.info(resp.message)
        # 只在严格规则下引号原串无匹配时提供，且必须由用户点击（方案 5.2）
        if resp.can_relax:
            st.caption(search.RELAX_RULE)
            if st.button("放宽匹配", key="relax"):
                st.session_state.query = {**query, "relaxed": True}
                st.session_state.pop("selected", None)
                st.rerun()
    else:
        basis = RANK_BASIS.get(resp.rank_basis, resp.rank_basis)
        st.caption(
            f"{MODE_LABELS[qmode]}模式 · 共 {resp.total} 个候选画面，按{basis}排序，显示前 {len(resp.results)} 个。"
            "分数只用于排序，不是正确率；排在前列也不代表库内一定有答案。"
        )
        for r in resp.results:
            with st.container(border=True):
                c_img, c_info = st.columns([1, 3])
                path = storage.resolve(CFG.data_dir, r.image_path)
                if r.image_status == "ok" and path.exists():
                    c_img.image(thumbnail(str(path), path.stat().st_mtime), width="stretch")
                else:
                    c_img.caption("原图已不可用")
                if r.frame_id in unknown_time:
                    when = f"捕获时间未知（导入于 {timeutil.fmt_local(r.latest.last_seen_ms)}）"
                else:
                    when = timeutil.fmt_local(r.latest.last_seen_ms)
                src = " · 导入" if r.latest.source_type == "import" else ""
                star = " · ★ 已收藏" if r.frame_id in fav_frames else ""
                c_info.markdown(f"**{when}** · {md(r.latest.app)} · {md(r.latest.window_title)}{src}{star}")
                if r.occurrences_in_filter > 1:
                    c_info.caption(f"筛选范围内出现 {r.occurrences_in_filter} 次，显示最近一次")
                tags = []
                if r.sources:
                    tags.append("命中分支：" + " + ".join(r.sources))
                if r.matched_terms:
                    tags.append("命中片段：" + "、".join(md(t) for t in r.matched_terms))
                if tags:
                    c_info.caption(" · ".join(tags))
                if qmode != retrieval.IMAGE:
                    c_info.code("\n".join(r.snippet) or "（无文字）", language=None, wrap_lines=True)
                if c_info.button("查看原图", key=f"open-{r.frame_id}"):
                    st.session_state.selected = {
                        "frame_id": r.frame_id,
                        "occurrence_id": r.latest.occurrence_id,
                        "mode": qmode,
                        "semantic_lines": list(r.semantic_lines),
                    }
                    st.rerun()
else:
    st.info(
        "在上方选择模式并输入内容。文字模式要等 OCR 完成；语义与画面排序要等对应向量完成，侧栏显示各阶段待办。"
    )
