"""v3 数据层：建库、名单、出处 / 条目 / 灰字、字的全库去重、截图与硬盘保护、过期、隐私删除、查询的 JSON 形状。"""

import dataclasses
import sqlite3
import threading
from datetime import datetime

import pytest

from screenrecall.store.fts import docs
from screenrecall.v3 import config
from screenrecall.v3.store import Queries, Store, hour_floor, schema, writer
from screenrecall.v3.types import (PAUSE_FOREVER_MS, EncodedShot, SourceLabel, TextLine)

MIN = 60_000
T0 = int(datetime(2026, 10, 28, 14, 0).timestamp() * 1000)  # 本机 14:00


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    clk = Clock()
    cfg = config.build({}, data_dir=tmp_path / "data", home=tmp_path)
    st = Store(cfg, clock=clk)
    q = Queries(cfg, clock=clk)
    yield cfg, st, q, clk
    q.close()
    st.close()


def raw(cfg):
    """测试自己的临时库：直接看表里的行数。"""
    c = sqlite3.connect(cfg.db_path)
    c.row_factory = sqlite3.Row
    return c


def L(*texts, via="uia", y0=10):
    return [TextLine(t, (10, y0 + 40 * i, 300, 24), via) for i, t in enumerate(texts)]


def URL(u, title="网页", app="msedge.exe"):
    return SourceLabel("url", u, app, title, url=u)


def APP(app, title):
    return SourceLabel("app_title", f"{app.lower()}\t{title}", app, title)


def FILE(path):
    return SourceLabel("file", path, "msedge.exe", path.rsplit("\\", 1)[-1], url=None, path=path)


def shot_of(lines, w=640, h=480):
    return EncodedShot(b"\xff\xd8demo-jpeg\xff\xd9", w, h, 1.0, [(docs.norm(x.text), x.box) for x in lines])


def look(st, label, t, lines=(), *, reason="still", via="uia", background=False, vid=None):
    """录制处理线程的一拍：出处 → 条目 → 存字 → 拍。返回 (sid, vid, outcome, snap_id)。"""
    sid, _ = st.upsert_source(label, t)
    vid = vid or st.visit_enter(sid, t, background=background)
    out = st.record_lines(sid, list(lines), t, visit_id=vid)
    snap = st.add_snap(t_ms=t, source_id=sid, visit_id=vid, reason=reason, via=via, new_lines=out.new_count,
                       seen_lines=len(out.seen_ids))
    st.visit_touch(vid, t)
    return sid, vid, out, snap


def result_lines(res):
    return sorted(h["text"] for r in res["results"] for h in r["hits"])


# ---------------- 建库 ----------------

def test_schema_create_and_reopen(env, tmp_path):
    cfg, st, q, clk = env
    with raw(cfg) as c:
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type IN ('table')")}
        meta = dict(c.execute("SELECT key, value FROM meta").fetchall())
        ctl = dict(c.execute("SELECT * FROM control").fetchone())
        jm = c.execute("PRAGMA journal_mode").fetchone()[0]
    assert {"meta", "apps", "excludes", "control", "sources", "lines", "source_lines", "lines_tri", "lines_bi",
            "visits", "snaps", "shots", "shot_lines", "clips", "grey_spans", "hour_lines"} <= names
    assert meta["schema_version"] == "1" and meta["fts_version"] == schema.FTS_VERSION
    assert ctl["id"] == 1 and ctl["updated_ms"] == T0 and ctl["rev"] == 0 and ctl["agent_enabled"] == 1
    assert jm == "wal"
    look(st, URL("https://a.example/x"), T0, L("重新打开以后还能搜到"))
    st.close()
    st2 = Store(cfg, clock=clk)  # 再开一次：不重复建表、不报错
    assert st2.control()["id"] == 1
    st2.close()


def test_fts_rebuilt_when_rules_change(env):
    cfg, st, q, clk = env
    look(st, URL("https://a.example/x"), T0, L("全文索引重建测试 ab"))
    st.meta_put("fts_version", "old-rules")
    st.close()
    st2 = Store(cfg, clock=clk)
    assert st2.meta_get("fts_version") == schema.FTS_VERSION
    q2 = Queries(cfg, clock=clk)
    assert result_lines(q2.search("索引重建")) == ["全文索引重建测试 ab"]
    assert result_lines(q2.search("ab")) == ["全文索引重建测试 ab"]
    q2.close()
    st2.close()


def test_schema_version_mismatch_is_an_error(env):
    cfg, st, q, clk = env
    st.meta_put("schema_version", "99")
    st.close()
    with pytest.raises(schema.SchemaError):
        Store(cfg, clock=clk)


def test_queries_on_missing_db_returns_empty(tmp_path):
    cfg = config.build({}, data_dir=tmp_path / "nothing", home=tmp_path)
    q = Queries(cfg)
    assert q.status()["state"] == "stopped" and q.status()["recorder_alive"] is False
    assert q.timeline(0, 10)["items"] == [] and q.timeline(0, 10)["from_ms"] == 0
    assert q.search("任何东西")["results"] == [] and q.copied() == [] and q.entry(1) is None
    assert q.source(1) is None and q.shot(1) is None and q.shot_file(1) is None and q.line_context(1) is None
    assert q.apps() == {"apps": [], "unlisted": [], "excludes": []}
    assert q.unnamed_sources(5) == [] and q.hours_without_line(10) == [] and q.hour_digest(0) == []
    assert q.lines_after(0) == [] and q.control() == {}
    assert not (tmp_path / "nothing").exists()  # 只读查询从不建库
    q.close()


# ---------------- 名单 / 控制 ----------------

def test_init_defaults_idempotent_and_tiers(env):
    cfg, st, q, clk = env
    assert st.init_defaults(cfg.default_record_apps) is True
    rev = st.control()["rev"]
    st.set_app_tier("msedge.exe", "never")
    assert st.init_defaults(cfg.default_record_apps) is False  # 第二次不再写，页面上改的保留
    tiers = st.app_tiers()
    assert tiers["msedge.exe"] == "never" and tiers["chrome.exe"] == "record"
    st.set_app_tier("Weixin.EXE", "always")
    assert st.app_tiers()["weixin.exe"] == "always"
    assert st.control()["rev"] == rev + 2
    with pytest.raises(ValueError):
        st.set_app_tier("x.exe", "sometimes")


def test_excludes_and_control(env):
    cfg, st, q, clk = env
    r0 = st.control()["rev"]
    a = st.add_exclude("host", "Bank.Example.COM")
    assert st.add_exclude("host", "bank.example.com") == a  # 同样的规则不重复
    b = st.add_exclude("title", "工资")
    assert [(e["kind"], e["value"]) for e in st.excludes()] == [("host", "bank.example.com"), ("title", "工资")]
    st.remove_exclude(b)
    assert len(st.excludes()) == 1 and st.control()["rev"] == r0 + 4
    with pytest.raises(ValueError):
        st.add_exclude("app", "x")
    clk.t = T0 + 5000
    st.set_control(paused_until_ms=T0 + 10 * MIN, agent_enabled=0)
    c = st.control()
    assert c["paused_until_ms"] == T0 + 10 * MIN and c["agent_enabled"] == 0 and c["updated_ms"] == T0 + 5000
    with pytest.raises(ValueError):
        st.set_control(id=2)
    assert q.apps()["excludes"][0]["value"] == "bank.example.com"


def test_status_recorder_alive_and_states(env):
    cfg, st, q, clk = env
    assert q.status()["state"] == "stopped"
    st.heartbeat(recorder_pid=123, recorder_state="recording", last_snap_ms=T0 - 3000, current_app="msedge.exe",
                 current_title="网页")
    s = q.status(T0 + 5000)
    assert s["recorder_alive"] is True and s["state"] == "recording" and s["last_snap_ms"] == T0 - 3000
    assert s["current"] == {"app": "msedge.exe", "title": "网页"} and s["agent_enabled"] is True
    assert s["disk_low"] is False and s["now_ms"] == T0 + 5000 and s["background"] == 0
    # 后台在录的窗口数 = 开着的后台条目
    sb, _ = st.upsert_source(APP("Zoom.exe", "会议"), T0)
    vb = st.visit_enter(sb, T0, background=True)
    assert q.status(T0 + 5000)["background"] == 1
    st.visit_leave(vb, T0 + 1000)
    assert q.status(T0 + 5000)["background"] == 0
    assert q.status(T0 + 11_000)["recorder_alive"] is False and q.status(T0 + 11_000)["state"] == "stopped"
    st.set_control(paused_until_ms=T0 + 10 * MIN)
    s = q.status(T0 + 1000)
    assert s["state"] == "paused" and s["paused_until_ms"] == T0 + 10 * MIN and not s["paused_forever"]
    assert s["current"] is None
    st.set_control(paused_until_ms=PAUSE_FOREVER_MS)
    assert q.status(T0 + 1000)["paused_forever"] is True
    st.heartbeat(recorder_state="paused", current_app=None, current_title=None)
    st.set_control(paused_until_ms=None)  # 刚点继续：录制端心跳还写着 paused，但按 recording 算
    s = q.status(T0 + 1000)
    assert s["state"] == "recording" and s["paused_until_ms"] is None and s["current"] is None
    st.set_control(paused_until_ms=T0 - 1)  # 暂停到点了也一样
    assert q.status(T0 + 1000)["state"] == "recording"
    st.set_control(paused_until_ms=None)
    st.heartbeat(recorder_state="locked")
    assert q.status(T0 + 1000)["state"] == "locked"
    st.heartbeat(recorder_state="stopped")
    assert q.status(T0 + 1000)["state"] == "stopped"


def test_recover_closes_open_visits_and_greys(env):
    cfg, st, q, clk = env
    sid, vid, _, _ = look(st, URL("https://a.example/"), T0, L("恢复测试"))
    st.visit_touch(vid, T0 + 2 * MIN)
    g = st.grey_enter("unlisted", "Code.exe", T0 + 3 * MIN)
    st.heartbeat(recorder_state="recording")  # clock = T0
    clk.t = T0 + 4 * MIN
    st.heartbeat(recorder_state="recording")
    r = st.recover(T0 + 60 * MIN)
    assert r["crashed"] is True and r["visits"] == 1 and r["greys"] == 1
    e = q.entry(vid)["entry"]
    assert e["open"] is False and e["dwell_ms"] == 2 * MIN and e["end_ms"] == T0 + 2 * MIN
    grey = [i for i in q.timeline(T0, T0 + 99 * MIN)["items"] if i["type"] == "grey"][0]
    assert grey["span_id"] == g and grey["end_ms"] == T0 + 4 * MIN
    st.heartbeat(recorder_state="stopped")
    assert st.recover(T0 + 61 * MIN)["crashed"] is False


# ---------------- 条目 / 灰字 ----------------

def test_visit_merge_within_5_min_vs_new_after(env):
    cfg, st, q, clk = env
    sid, _ = st.upsert_source(URL("https://a.example/"), T0)
    v1 = st.visit_enter(sid, T0)
    assert st.visit_enter(sid, T0 + MIN) == v1  # 还开着：同一条
    st.visit_leave(v1, T0 + 2 * MIN)
    assert st.visit_enter(sid, T0 + 6 * MIN) == v1  # 离开 4 分钟又回来：合成一条
    st.visit_leave(v1, T0 + 7 * MIN)
    v2 = st.visit_enter(sid, T0 + 13 * MIN)  # 离开 6 分钟：新的一条
    assert v2 != v1
    e1 = q.entry(v1)["entry"]
    assert e1["merged"] == 1 and e1["dwell_ms"] == 3 * MIN and (e1["start_ms"], e1["end_ms"]) == (T0, T0 + 7 * MIN)
    # 前台、后台看同一个东西是同一条：后台合进前台条目只算回来过一次，不打开（后台那段不算"看了多久"）
    assert st.visit_enter(sid, T0 + 13 * MIN, background=True) == v2  # 还开着
    st.visit_leave(v2, T0 + 14 * MIN)
    assert st.visit_enter(sid, T0 + 15 * MIN, background=True) == v2
    st.visit_touch(v2, T0 + 17 * MIN)
    st.visit_leave(v2, T0 + 17 * MIN)  # 没打开：不加
    e2 = q.entry(v2)["entry"]
    assert (e2["open"], e2["background"], e2["merged"], e2["dwell_ms"], e2["end_ms"]) == (False, False, 1, MIN,
                                                                                         T0 + 17 * MIN)


def test_background_entry_becomes_foreground_when_brought_to_front(env):
    """一直录的窗口后台录着，切到前台：同一条变成前台条目，"看了多久"从 0 算（后台跨度不是在看）；
    切回后台又合进这条，不再出一条并排的后台条目。"""
    cfg, st, q, clk = env
    sid, _ = st.upsert_source(APP("Chat.exe", "群聊"), T0)
    vb = st.visit_enter(sid, T0, background=True)
    st.visit_leave(vb, T0 + 4 * MIN)
    assert q.entry(vb)["entry"]["dwell_ms"] == 4 * MIN
    assert st.visit_enter(sid, T0 + 4 * MIN) == vb
    st.visit_leave(vb, T0 + 6 * MIN)
    e = q.entry(vb)["entry"]
    assert (e["background"], e["dwell_ms"], e["merged"]) == (False, 2 * MIN, 1)
    assert st.visit_enter(sid, T0 + 7 * MIN, background=True) == vb
    assert q.entry(vb)["entry"]["open"] is False


def test_dwell_minus_away_and_background_span(env):
    cfg, st, q, clk = env
    sid, _ = st.upsert_source(APP("Zoom.exe", "会议"), T0)
    v = st.visit_enter(sid, T0)
    st.visit_leave(v, T0 + 20 * MIN, away_ms=8 * MIN)
    assert q.entry(v)["entry"]["dwell_ms"] == 12 * MIN
    st.visit_leave(v, T0 + 30 * MIN)  # 已离开：不再算
    assert q.entry(v)["entry"]["dwell_ms"] == 12 * MIN
    vb = st.visit_enter(sid, T0 + 40 * MIN, background=True)
    st.visit_touch(vb, T0 + 45 * MIN)
    eb = q.entry(vb)["entry"]  # 后台还开着：算到最后一拍
    assert eb["open"] and eb["dwell_ms"] == 5 * MIN
    st.visit_leave(vb, T0 + 50 * MIN, away_ms=8 * MIN)  # 后台的没有"走开"可扣：就是跨度
    eb = q.entry(vb)["entry"]
    assert eb["dwell_ms"] == 10 * MIN and eb["background"] is True and eb["end_ms"] == T0 + 50 * MIN
    v3 = st.visit_enter(sid, T0 + 60 * MIN)
    st.visit_leave(v3, T0 + 61 * MIN, away_ms=10 * MIN)  # 走开比这一段还长：0，不是负数
    assert q.entry(v3)["entry"]["dwell_ms"] == 0


def test_grey_reopen_within_merge(env):
    cfg, st, q, clk = env
    g1 = st.grey_enter("unlisted", "Code.exe", T0)
    assert st.grey_enter("unlisted", "Code.exe", T0 + MIN) == g1  # 还开着
    st.grey_leave(g1, T0 + 2 * MIN)
    assert st.grey_enter("unlisted", "Code.exe", T0 + 5 * MIN) == g1  # 3 分钟内回来：重新打开
    st.grey_leave(g1, T0 + 6 * MIN)
    assert st.grey_enter("unlisted", "WindowsTerminal.exe", T0 + 7 * MIN) != g1  # 别的软件：新的一段
    g2 = st.grey_enter("unlisted", "Code.exe", T0 + 20 * MIN)
    assert g2 != g1
    p = st.grey_enter("paused", None, T0 + 30 * MIN)
    st.grey_leave(p, T0 + 31 * MIN)
    assert st.grey_enter("paused", None, T0 + 32 * MIN) == p
    items = [i for i in q.timeline(T0, T0 + 60 * MIN)["items"] if i["span_id"] == g1]
    assert items[0]["start_ms"] == T0 and items[0]["end_ms"] == T0 + 6 * MIN
    with pytest.raises(ValueError):
        st.grey_enter("idle", None, T0)


# ---------------- 字 ----------------

def test_global_line_dedup_across_sources(env):
    cfg, st, q, clk = env
    s1, v1, o1, _ = look(st, URL("https://a.example/"), T0, L("同一句话出现在两个地方", "只在网页里"))
    s2, v2, o2, _ = look(st, APP("Weixin.exe", "微信"), T0 + MIN, L("同一 句话出现在两个地方", via="ocr"))
    assert o1.new_count == 2 and o2.new_count == 0 and o2.seen_ids == (o1.ids_by_norm[docs.norm("同一句话出现在两个地方")],)
    with raw(cfg) as c:
        assert c.execute("SELECT COUNT(*) FROM lines").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM source_lines").fetchone()[0] == 3
        row = c.execute("SELECT text, via FROM lines WHERE norm = ?", (docs.norm("同一句话出现在两个地方"),)).fetchone()
    assert tuple(row) == ("同一句话出现在两个地方", "uia")  # 显示用第一次的原文
    assert q.entry(v1)["entry"]["new_lines"] == 2 and q.entry(v2)["entry"]["new_lines"] == 0
    # 同一批里重复、空白的行只算一次 / 跳过
    o3 = st.record_lines(s1, L("新的一行", "新的 一行", "   "), T0 + 2 * MIN)
    assert o3.new_count == 1 and len(o3.ids_by_norm) == 1
    assert st.record_lines(s1, [], T0).new_count == 0


def test_seen_lines_only_bump_last_seen(env):
    cfg, st, q, clk = env
    sid, vid, o1, _ = look(st, URL("https://a.example/"), T0, L("第一行", "第二行"))
    _, _, o2, _ = look(st, URL("https://a.example/"), T0 + 3 * MIN, L("第二行", "第三行"), vid=vid)
    assert o2.new_count == 1 and len(o2.seen_ids) == 1
    with raw(cfg) as c:
        r = c.execute("SELECT text, first_seen_ms, last_seen_ms FROM lines ORDER BY line_id").fetchall()
        sl = c.execute("SELECT first_seen_ms, last_seen_ms FROM source_lines ORDER BY line_id").fetchall()
    assert [tuple(x) for x in r] == [("第一行", T0, T0), ("第二行", T0, T0 + 3 * MIN), ("第三行", T0 + 3 * MIN, T0 + 3 * MIN)]
    assert [tuple(x) for x in sl] == [(T0, T0), (T0, T0 + 3 * MIN), (T0 + 3 * MIN, T0 + 3 * MIN)]
    assert q.entry(vid)["entry"]["new_lines"] == 3


def test_concurrent_writes_are_serialised(env):
    cfg, st, q, clk = env
    sid, _ = st.upsert_source(URL("https://a.example/"), T0)
    errors = []

    def work(k):
        try:
            for i in range(20):
                st.record_lines(sid, L(f"线程{k}的第{i}行", "大家都有的一行"), T0 + i)
        except Exception as e:  # pragma: no cover - 失败时才走到
            errors.append(e)

    ts = [threading.Thread(target=work, args=(k,)) for k in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    with raw(cfg) as c:
        assert c.execute("SELECT COUNT(*) FROM lines").fetchone()[0] == 81


# ---------------- 截图 ----------------

def test_save_shot_writes_file_and_boxes(env):
    cfg, st, q, clk = env
    lines = L("截图上的第一行", "截图上的第二行", "没登记的行")
    sid, vid, out, snap = look(st, URL("https://a.example/"), T0, lines[:2])
    shot = shot_of(lines)
    shot_id = st.save_shot(source_id=sid, visit_id=vid, snap_id=snap, t_ms=T0, kind="first", shot=shot,
                           ids_by_norm=out.ids_by_norm)
    assert shot_id
    f = q.shot_file(shot_id)
    assert f is not None and f.read_bytes() == shot.jpeg and f.parent.parent == cfg.shots_dir
    assert f.parent.name == datetime.fromtimestamp(T0 / 1000).strftime("%Y-%m-%d")
    assert not list(f.parent.glob("*.tmp"))
    d = q.shot(shot_id)
    assert (d["kind"], d["width"], d["height"], d["source_id"], d["visit_id"]) == ("first", 640, 480, sid, vid)
    assert [(b["text"], b["x"], b["y"], b["w"], b["h"]) for b in d["boxes"]] == [
        ("截图上的第一行", 10, 10, 300, 24), ("截图上的第二行", 10, 50, 300, 24)]
    assert d["source"]["shot_id"] == shot_id
    with raw(cfg) as c:
        assert c.execute("SELECT shot_id FROM snaps WHERE snap_id = ?", (snap,)).fetchone()[0] == shot_id
        assert c.execute("SELECT shot_id FROM visits WHERE visit_id = ?", (vid,)).fetchone()[0] == shot_id
        assert c.execute("SELECT bytes FROM shots").fetchone()[0] == len(shot.jpeg)
    assert st.upsert_source(URL("https://a.example/"), T0 + 1)[1] is True  # 已有截图
    # 回退图不覆盖这一条已有的缩略图；'visit' 总是覆盖
    fb = st.save_shot(source_id=sid, visit_id=vid, snap_id=None, t_ms=T0 + MIN, kind="fallback",
                      shot=shot_of([]), ids_by_norm={})
    assert q.entry(vid)["entry"]["thumb"] == {"shot_id": shot_id, "own": True}
    vs = st.save_shot(source_id=sid, visit_id=vid, snap_id=None, t_ms=T0 + 2 * MIN, kind="visit",
                      shot=shot_of([]), ids_by_norm={})
    assert q.entry(vid)["entry"]["thumb"]["shot_id"] == vs and fb
    with pytest.raises(ValueError):
        st.save_shot(source_id=sid, visit_id=vid, snap_id=None, t_ms=T0, kind="other", shot=shot, ids_by_norm={})


def test_save_shot_replaces_old_visit_shot(env):
    """弱标签这一次换成新字更多的那张：旧图的行、框、文件都删，那一拍的 shot_id 置空，条目缩略图换成新的。"""
    cfg, st, q, clk = env
    lines = L("群里第一句", "群里第二句")
    sid, vid, out, snap = look(st, APP("Chat.exe", "群聊"), T0, lines[:1])
    old = st.save_shot(source_id=sid, visit_id=vid, snap_id=snap, t_ms=T0, kind="visit", shot=shot_of(lines[:1]),
                       ids_by_norm=out.ids_by_norm)
    old_file = q.shot_file(old)
    out2 = st.record_lines(sid, lines, T0 + MIN, visit_id=vid)
    new = st.save_shot(source_id=sid, visit_id=vid, snap_id=None, t_ms=T0 + MIN, kind="visit", shot=shot_of(lines),
                       ids_by_norm=out2.ids_by_norm, replaces=old)
    assert new and new != old and not old_file.exists() and q.shot(old) is None
    assert q.entry(vid)["entry"]["thumb"] == {"shot_id": new, "own": True}
    assert [s["shot_id"] for s in q.entry(vid)["shots"]] == [new]
    with raw(cfg) as c:
        assert c.execute("SELECT shot_id FROM snaps WHERE snap_id = ?", (snap,)).fetchone()[0] is None
        assert c.execute("SELECT COUNT(*) FROM shot_lines WHERE shot_id = ?", (old,)).fetchone()[0] == 0


def test_disk_guard_skips_shots_and_rechecks_after_60s(env, monkeypatch):
    cfg, st, q, clk = env
    sid, vid, out, snap = look(st, APP("Weixin.exe", "微信"), T0, L("硬盘不够"))
    free = {"gb": 1.0}

    class Usage:
        def __init__(self):
            self.total, self.used, self.free = 100 * 1024 ** 3, 0, int(free["gb"] * 1024 ** 3)

    monkeypatch.setattr(writer.shutil, "disk_usage", lambda p: Usage())
    kw = dict(source_id=sid, visit_id=vid, snap_id=snap, kind="visit", shot=shot_of(L("硬盘不够")),
              ids_by_norm=out.ids_by_norm)
    assert st.save_shot(t_ms=T0, **kw) is None
    assert not cfg.shots_dir.exists() or not list(cfg.shots_dir.rglob("*.jpg"))
    free["gb"] = 50.0
    clk.t = T0 + 30_000
    assert st.save_shot(t_ms=T0, **kw) is None  # 60 秒内不重查
    clk.t = T0 + 61_000
    assert st.save_shot(t_ms=T0, **kw) is not None
    monkeypatch.setattr(writer.shutil, "disk_usage", lambda p: Usage())
    free["gb"] = 2.0
    from screenrecall.v3.store import queries as qmod
    monkeypatch.setattr(qmod.shutil, "disk_usage", lambda p: Usage())
    assert q.status()["disk_low"] is True


def test_file_source_has_no_lines(env):
    cfg, st, q, clk = env
    label = FILE(r"C:\Docs\报告.pdf")
    sid, has = st.upsert_source(label, T0)
    vid = st.visit_enter(sid, T0)
    snap = st.add_snap(t_ms=T0, source_id=sid, visit_id=vid, reason="still", via="uia", new_lines=0, seen_lines=0)
    shot_id = st.save_shot(source_id=sid, visit_id=vid, snap_id=snap, t_ms=T0, kind="first",
                           shot=shot_of(L("PDF 里的字")), ids_by_norm={})
    assert has is False and shot_id
    with raw(cfg) as c:
        assert c.execute("SELECT COUNT(*) FROM lines").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM shot_lines").fetchone()[0] == 0
    src = q.source(sid)
    assert src["source"]["kind"] == "file" and src["source"]["strong"] is True and src["total_lines"] == 0
    assert src["source"]["name"] == "报告.pdf" and src["source"]["path"] == r"C:\Docs\报告.pdf"
    assert q.search("报告")["results"][0]["match"] == ["title", "url"]  # 标题 + 路径命中
    assert q.search("PDF 里的字")["results"] == []


def test_expire_shots_keeps_text(env):
    cfg, st, q, clk = env
    sid, vid, out, snap = look(st, URL("https://a.example/"), T0, L("过期以后字还在"))
    old = st.save_shot(source_id=sid, visit_id=vid, snap_id=snap, t_ms=T0, kind="first",
                       shot=shot_of(L("过期以后字还在")), ids_by_norm=out.ids_by_norm)
    new = st.save_shot(source_id=sid, visit_id=vid, snap_id=None, t_ms=T0 + 10 * MIN, kind="fallback",
                       shot=shot_of([]), ids_by_norm={})
    f_old = q.shot_file(old)
    assert st.expire_shots(T0 + MIN) == 1
    assert not f_old.exists() and q.shot(old) is None and q.shot_file(new) is not None
    res = q.search("过期以后")
    assert result_lines(res) == ["过期以后字还在"]
    assert res["results"][0]["shot"] is None  # 包含命中行的那张没了；只剩的回退图没有这一行
    assert q.source(sid)["source"]["shot_id"] is None
    assert q.entry(vid)["entry"]["thumb"] is None


def test_delete_range_removes_text_index_and_files(env):
    cfg, st, q, clk = env
    s_keep, v_keep, _, _ = look(st, URL("https://keep.example/"), T0 - 60 * MIN, L("保留的内容 早一点"))
    st.visit_leave(v_keep, T0 - 50 * MIN)
    sid, vid, out, snap = look(st, APP("Weixin.exe", "私聊"), T0, L("要删掉的秘密内容", "保留的内容 早一点"))
    shot = st.save_shot(source_id=sid, visit_id=vid, snap_id=snap, t_ms=T0, kind="visit",
                        shot=shot_of(L("要删掉的秘密内容")), ids_by_norm=out.ids_by_norm)
    f = q.shot_file(shot)
    st.add_clip(t_ms=T0 + 1000, text="要删掉的秘密内容", app="Weixin.exe", title="私聊", source_id=sid, visit_id=vid)
    st.grey_enter("unlisted", "Code.exe", T0 + 2 * MIN)
    st.put_hour_line(hour_floor(T0), "这一小时在聊秘密", None)
    assert result_lines(q.search("秘密")) == ["要删掉的秘密内容"]
    n = st.delete_range(T0 - MIN, T0 + 10 * MIN)
    assert n["visits"] == 1 and n["snaps"] == 1 and n["shots"] == 1 and n["clips"] == 1 and n["grey_spans"] == 1
    assert n["source_lines"] == 2 and n["lines"] == 1 and n["sources"] == 1 and n["hour_lines"] == 1
    assert not f.exists()
    assert q.search("秘密")["results"] == [] and q.search("秘")["results"] == []
    assert q.copied(q="秘密") == []
    with raw(cfg) as c:
        assert c.execute("SELECT COUNT(*) FROM lines_tri WHERE lines_tri MATCH '\"秘密内\"'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM lines_bi WHERE lines_bi MATCH '\"秘密\"'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    # 更早看到的保留
    assert result_lines(q.search("保留的内容")) == ["保留的内容 早一点"]
    assert q.entry(v_keep) is not None
    # 录制进程据 delete_rev 知道删过了；对得上哪些还在
    assert st.meta_get("delete_rev") == "1"
    assert st.alive(visits=[v_keep, vid], sources=[s_keep, sid], spans=[999]) == ({v_keep}, {s_keep}, set())
    st.delete_range(T0 + 20 * MIN, T0 + 30 * MIN)
    assert st.meta_get("delete_rev") == "2"
    # 编号不重用：删掉的条目 / 出处 / 截图 / 行的编号不会给新行（录制进程、缩略图缓存、向量里的旧编号不会指错）
    sid2, vid2, out2, snap2 = look(st, APP("Weixin.exe", "私聊"), T0 + 40 * MIN, L("新的一行"))
    shot2 = st.save_shot(source_id=sid2, visit_id=vid2, snap_id=snap2, t_ms=T0 + 40 * MIN, kind="visit",
                         shot=shot_of(L("新的一行")), ids_by_norm=out2.ids_by_norm)
    assert sid2 > sid and vid2 > vid and snap2 > snap and shot2 > shot
    assert min(out2.new_ids) > max(out.ids_by_norm.values())


def test_url_apps_and_timeline_keeps_newest(env):
    cfg, st, q, clk = env
    look(st, URL("https://a.example/", app="NewBrowser.exe"), T0)
    look(st, APP("notepad.exe", "笔记"), T0)
    assert st.url_apps() == {"newbrowser.exe"}
    for i in range(6):
        sid, vid, _, _ = look(st, APP("notepad.exe", f"第 {i} 篇"), T0 + (i + 1) * MIN)
        st.visit_leave(vid, T0 + (i + 1) * MIN + 1000)
    st.grey_enter("unlisted", "Code.exe", T0 + 10 * MIN)
    items = q.timeline(T0, T0 + 60 * MIN, now_ms=T0 + 11 * MIN, limit=3)["items"]
    assert [i["start_ms"] for i in items] == [T0 + 5 * MIN, T0 + 6 * MIN, T0 + 10 * MIN]  # 超过上限留最近的


# ---------------- 查找 ----------------

def test_search_cjk_short_and_long_terms(env):
    cfg, st, q, clk = env
    look(st, URL("https://a.example/"), T0, L("明天下午组会在 3 楼", "微信支付成功", "会", "Machine Learning 101",
                                              "ＡＩ assistant", "rain gauge"))
    assert result_lines(q.search("组会")) == ["明天下午组会在 3 楼"]
    assert result_lines(q.search("会")) == ["会", "明天下午组会在 3 楼"]
    assert result_lines(q.search("付")) == ["微信支付成功"]
    assert result_lines(q.search("下午组会")) == ["明天下午组会在 3 楼"]
    assert result_lines(q.search("组会在3楼")) == ["明天下午组会在 3 楼"]  # 空白不敏感
    assert result_lines(q.search("支付失败")) == []
    assert result_lines(q.search("machine learning")) == ["Machine Learning 101"]
    assert result_lines(q.search("ai")) == ["ＡＩ assistant"]  # 两个字母 = 词开头：rain 不算
    assert result_lines(q.search("ain")) == ["rain gauge"]
    assert result_lines(q.search("ai assistant")) == ["ＡＩ assistant"]  # 有长词时短词也按词开头核验
    assert result_lines(q.search("ai gauge")) == []
    r = q.search('"未闭合')
    assert r["error"] and r["results"] == []
    assert q.search("   ")["error"] and q.search("##")["error"]


def test_search_title_and_url_hits(env):
    cfg, st, q, clk = env
    look(st, URL("https://docs.python.org/3/library/sqlite3.html", title="sqlite3 — DB-API"), T0, L("正文一行"))
    look(st, APP("Weixin.exe", "项目讨论群"), T0 + MIN, L("今天开会"))
    r = q.search("项目讨论")
    assert len(r["results"]) == 1 and r["results"][0]["match"] == ["title"] and r["results"][0]["hits"] == []
    r = q.search("docs.python")
    assert r["results"][0]["match"] == ["url"]
    r = q.search("sqlite3")
    assert r["results"][0]["match"] == ["title", "url"]
    # 字和标题都命中：同一条结果
    look(st, APP("Weixin.exe", "项目讨论群"), T0 + 2 * MIN, L("项目讨论的结论"))
    r = q.search("项目讨论")
    assert len(r["results"]) == 1 and set(r["results"][0]["match"]) == {"text", "title"}


def test_search_merge_within_10_min_and_recent_first(env):
    cfg, st, q, clk = env
    a = APP("Weixin.exe", "群聊")
    look(st, a, T0, L("报销单 第一张"))
    look(st, a, T0 + 5 * MIN, L("报销单 第二张"))
    r = q.search("报销单")
    assert r["total"] == 1 and len(r["results"][0]["hits"]) == 2
    res = r["results"][0]
    assert (res["start_ms"], res["end_ms"], res["last_seen_ms"]) == (T0, T0 + 5 * MIN, T0 + 5 * MIN)
    assert [h["text"] for h in res["hits"]] == ["报销单 第二张", "报销单 第一张"]  # 新的在前
    assert res["visit_id"] is not None and res["source"]["app"] == "Weixin.exe"
    look(st, a, T0 + 30 * MIN, L("报销单 第三张"))  # 隔了 25 分钟：另一条结果
    look(st, URL("https://b.example/"), T0 + 20 * MIN, L("报销单 网页上"))
    r = q.search("报销单")
    assert r["total"] == 3
    assert [x["last_seen_ms"] for x in r["results"]] == [T0 + 30 * MIN, T0 + 20 * MIN, T0 + 5 * MIN]
    # 条数、每条命中数
    r = q.search("报销单", limit=1, per_result=1)
    assert len(r["results"]) == 1 and r["total"] == 3 and r["results"][0]["more_hits"] == 0
    r = q.search("报销单", per_result=1)
    assert r["results"][2]["more_hits"] == 1
    # 时间、软件筛选
    assert [x["source"]["kind"] for x in q.search("报销单", start_ms=T0 + 15 * MIN, end_ms=T0 + 25 * MIN)["results"]] \
        == ["url"]
    assert {x["source"]["app"] for x in q.search("报销单", app="weixin.exe")["results"]} == {"Weixin.exe"}


def test_search_shot_boxes_only_hit_lines(env):
    cfg, st, q, clk = env
    a = APP("Weixin.exe", "群聊")
    lines1 = L("第一屏 甲", "第一屏 乙", "关键词 丙")
    sid, vid, out, snap = look(st, a, T0, lines1)
    s1 = st.save_shot(source_id=sid, visit_id=vid, snap_id=snap, t_ms=T0, kind="visit", shot=shot_of(lines1),
                      ids_by_norm=out.ids_by_norm)
    lines2 = L("关键词 丁", "关键词 戊")
    _, _, out2, snap2 = look(st, a, T0 + MIN, lines2, vid=vid)
    s2 = st.save_shot(source_id=sid, visit_id=vid, snap_id=snap2, t_ms=T0 + MIN, kind="fallback",
                      shot=shot_of(lines2), ids_by_norm=out2.ids_by_norm)
    r = q.search("关键词")["results"][0]
    assert r["shot"]["shot_id"] == s2  # 包含命中行最多的那张
    assert sorted(b["text"] for b in r["shot"]["boxes"]) == ["关键词 丁", "关键词 戊"]
    r = q.search("第一屏 甲")["results"][0]
    assert r["shot"]["shot_id"] == s1 and [b["text"] for b in r["shot"]["boxes"]] == ["第一屏 甲"]
    # 强标签：用出处第一次的那张
    u = URL("https://c.example/")
    sid3, vid3, out3, snap3 = look(st, u, T0 + 2 * MIN, L("网页 关键词 己", "网页 别的"))
    s3 = st.save_shot(source_id=sid3, visit_id=vid3, snap_id=snap3, t_ms=T0 + 2 * MIN, kind="first",
                      shot=shot_of(L("网页 关键词 己", "网页 别的")), ids_by_norm=out3.ids_by_norm)
    r = q.search("网页 关键词")["results"][0]
    assert r["shot"]["shot_id"] == s3 and [b["text"] for b in r["shot"]["boxes"]] == ["网页 关键词 己"]


def test_search_copied_group_and_copied(env):
    cfg, st, q, clk = env
    sid, vid, _, _ = look(st, APP("Weixin.exe", "微信"), T0, L("小李：老师说报告要加一段局限性分析"))
    st.add_clip(t_ms=T0 + 1000, text="老师说报告要加一段局限性分析", app="Weixin.exe", title="微信", source_id=sid,
                visit_id=vid)
    st.add_clip(t_ms=T0 + 2000, text="别的东西", app="Code.exe", title="x", source_id=None, visit_id=None)
    r = q.search("局限性")
    assert len(r["copied"]) == 1
    c0 = r["copied"][0]
    assert c0["text"] == "老师说报告要加一段局限性分析" and c0["app"] == "Weixin.exe" and c0["source"]["source_id"] == sid
    assert c0["visit_id"] == vid and c0["t_ms"] == T0 + 1000
    assert [c["text"] for c in q.copied()] == ["别的东西", "老师说报告要加一段局限性分析"]
    assert q.copied()[0]["source"] is None
    assert [c["text"] for c in q.copied(q="ＬＩ".lower() + "局限")] == []
    assert [c["text"] for c in q.copied(start_ms=T0 + 1500)] == ["别的东西"]
    assert q.entry(vid)["entry"]["copied"] == 1 and len(q.entry(vid)["clips"]) == 1


def test_search_semantic_hits_after_lexical(env):
    cfg, st, q, clk = env
    _, _, o1, _ = look(st, URL("https://a.example/"), T0, L("注意力机制"))
    _, _, o2, _ = look(st, URL("https://b.example/"), T0 + 5 * 60 * MIN, L("Transformer 的核心"))
    sem_id = o2.ids_by_norm[docs.norm("Transformer 的核心")]
    calls = []

    def semantic(text, k):
        calls.append((text, k))
        return [(sem_id, 0.8), (o1.ids_by_norm[docs.norm("注意力机制")], 0.9)]

    r = q.search("注意力", semantic=semantic)
    assert calls and r["total"] == 2
    assert r["results"][0]["hits"][0]["semantic"] is False  # 全文命中在前（虽然更早）
    assert r["results"][1]["hits"][0] == {**r["results"][1]["hits"][0], "text": "Transformer 的核心", "semantic": True}


def test_search_semantic_floor_short_lines_and_score_order(env):
    cfg, st, q, clk = env
    assert cfg.vectors and cfg.semantic_min_score == 0.60 and cfg.semantic_min_chars == 6
    _, _, oa, _ = look(st, URL("https://a.example/"), T0, L("自注意力怎么算的呢"))
    _, _, ob, _ = look(st, URL("https://b.example/"), T0 + 30 * MIN, L("多头注意力的直观解释"))
    _, _, oc, _ = look(st, URL("https://c.example/"), T0 + 60 * MIN, L("和题目无关的一行字"))
    _, _, od, _ = look(st, URL("https://d.example/"), T0 + 90 * MIN, L("数据看板"))
    ida, idb = oa.ids_by_norm[docs.norm("自注意力怎么算的呢")], ob.ids_by_norm[docs.norm("多头注意力的直观解释")]
    idc, idd = oc.ids_by_norm[docs.norm("和题目无关的一行字")], od.ids_by_norm[docs.norm("数据看板")]
    sem = lambda text, k: [(idd, 0.95), (ida, 0.80), (idb, 0.70), (idc, 0.59)]  # noqa: E731
    r = q.search("attention", semantic=sem)
    # 0.59 低于下限、"数据看板"太短都不算；只有按意思搜到的按分数排（a 更早但更像，排前面）
    assert [x["source"]["url"] for x in r["results"]] == ["https://a.example/", "https://b.example/"]
    assert all(h["semantic"] for x in r["results"] for h in x["hits"])
    loose = Queries(dataclasses.replace(cfg, semantic_min_score=0.5, semantic_min_chars=0), clock=clk)
    try:
        assert loose.search("attention", semantic=sem)["total"] == 4
    finally:
        loose.close()


def test_search_semantic_error_falls_back_to_lexical(env):
    cfg, st, q, clk = env
    look(st, URL("https://a.example/"), T0, L("注意力机制"))

    def broken(text, k):
        raise RuntimeError("模型没装")

    r = q.search("注意力", semantic=broken)
    assert r["error"] is None and r["total"] == 1 and r["results"][0]["hits"][0]["semantic"] is False


# ---------------- 时间线 / 条目 / 出处 ----------------

def test_timeline_shape(env):
    cfg, st, q, clk = env
    st.init_defaults(cfg.default_record_apps)
    u = URL("https://blog.example.com/transformer-intro", title="Transformer 入门")
    sid, v1, out, snap = look(st, u, T0, L("第一行", "第二行"))
    s1 = st.save_shot(source_id=sid, visit_id=v1, snap_id=snap, t_ms=T0, kind="first", shot=shot_of(L("第一行")),
                      ids_by_norm=out.ids_by_norm)
    look(st, u, T0 + 2 * MIN, L("第二行", "第三行"), vid=v1, reason="forced")
    st.visit_leave(v1, T0 + 5 * MIN)
    g = st.grey_enter("unlisted", "Code.exe", T0 + 5 * MIN)
    st.grey_leave(g, T0 + 12 * MIN)
    lk = st.grey_enter("locked", None, T0 + 12 * MIN)
    st.grey_leave(lk, T0 + 13 * MIN)
    _, v2, _, _ = look(st, u, T0 + 15 * MIN, L("第三行"))  # 回来已超过 5 分钟：新的一条，还开着
    tl = q.timeline(T0 - MIN, T0 + 60 * MIN, now_ms=T0 + 20 * MIN)
    assert [(i["type"], i.get("visit_id") or i.get("span_id")) for i in tl["items"]] == [
        ("entry", v1), ("grey", g), ("grey", lk), ("entry", v2)]
    e1, grey, locked, e2 = tl["items"]
    assert e1["name"] == "Transformer 入门" and e1["open"] is False and e1["dwell_ms"] == 5 * MIN
    assert e1["new_lines"] == 3 and e1["reasons"] == ["still", "forced"] and e1["thumb"] == {"shot_id": s1, "own": True}
    assert e1["source"]["strong"] is True and e1["source"]["url"] == u.url and e1["copied"] == 0
    assert grey == {"type": "grey", "span_id": g, "reason": "unlisted", "app": "Code.exe", "start_ms": T0 + 5 * MIN,
                    "end_ms": T0 + 12 * MIN, "tier": "never"}
    assert locked["tier"] is None and locked["app"] is None
    assert e2["open"] is True and e2["dwell_ms"] == 5 * MIN and e2["end_ms"] == T0 + 20 * MIN
    assert e2["new_lines"] == 0 and e2["thumb"] == {"shot_id": s1, "own": False}  # 已有图
    st.set_app_tier("Code.exe", "record")
    assert [i for i in q.timeline(T0, T0 + 60 * MIN)["items"] if i["type"] == "grey"][0]["tier"] == "record"
    assert q.timeline(T0 + 30 * MIN, T0 + 40 * MIN, now_ms=T0 + 40 * MIN)["items"][0]["visit_id"] == v2  # 开着的跨过来
    assert len(q.timeline(T0 - MIN, T0 + 60 * MIN, limit=2)["items"]) == 2
    st.put_hour_line(hour_floor(T0), "这一小时读了 Transformer", "m")
    assert q.timeline(T0 - MIN, T0 + 60 * MIN)["hours"] == [{"hour_ms": hour_floor(T0), "text": "这一小时读了 Transformer"}]


def test_entry_source_and_line_context(env):
    cfg, st, q, clk = env
    a = APP("Weixin.exe", "微信")
    sid, v1, _, _ = look(st, a, T0, L("旧一", "旧二"))
    st.visit_leave(v1, T0 + MIN)
    _, v2, _, _ = look(st, a, T0 + 20 * MIN, L("旧二", "新三", "新四"))
    d = q.entry(v2)
    assert [(x["text"], x["new"]) for x in d["lines"]] == [("旧二", False), ("新三", True), ("新四", True)]
    assert d["entry"]["name"] == "微信" and d["shots"] == [] and d["clips"] == []
    assert q.entry(9999) is None
    s = q.source(sid)
    assert s["total_lines"] == 4 and [x["text"] for x in s["lines"]] == ["旧一", "旧二", "新三", "新四"]
    assert [v["visit_id"] for v in s["visits"]] == [v2, v1]
    assert [x["text"] for x in q.source(sid, offset=1, limit=2)["lines"]] == ["旧二", "新三"]
    lid = q.search("新三")["results"][0]["hits"][0]["line_id"]
    ctx = q.line_context(lid, before=1, after=1)
    assert [x["text"] for x in ctx["lines"]] == ["旧二", "新三", "新四"] and ctx["target"] == 1 and ctx["total"] == 4
    assert ctx["source"]["source_id"] == sid
    assert q.line_context(lid, before=10, after=0)["target"] == 2


def test_display_names(env):
    cfg, st, q, clk = env
    sid, _ = st.upsert_source(URL("https://example.com/a/b/", title=""), T0)
    assert q.source(sid)["source"]["name"] == "example.com/a/b"
    sid2, _ = st.upsert_source(URL("https://example.com/", title=""), T0)
    assert q.source(sid2)["source"]["name"] == "example.com"
    sid3, _ = st.upsert_source(SourceLabel("app_title", "x.exe\t", "x.exe", ""), T0)
    assert q.source(sid3)["source"]["name"] == "x.exe"
    st.set_source_name(sid, "Python 文档")
    assert q.source(sid)["source"]["name"] == "Python 文档"
    st.visit_enter(sid, T0)
    assert all(e["name"] != "" for e in q.timeline(T0 - 1, T0 + 1)["items"])


def test_apps_listing_with_unlisted(env):
    cfg, st, q, clk = env
    st.init_defaults(["msedge.exe"])
    for app, a, b in (("Code.exe", T0 - 10 * MIN, T0 - 5 * MIN), ("code.exe", T0 - 3 * MIN, T0 - 2 * MIN),
                      ("Steam.exe", T0 - MIN, None), ("Old.exe", T0 - 40 * 86_400_000, T0 - 40 * 86_400_000 + 1)):
        sp = st.grey_enter("unlisted", app, a)
        if b:
            st.grey_leave(sp, b)
    st.grey_enter("unlisted", "msedge.exe", T0 - 30 * MIN)
    d = q.apps()
    assert [(a["process"], a["tier"]) for a in d["apps"]] == [("msedge.exe", "record")]
    assert [u["app"].lower() for u in d["unlisted"]] == ["steam.exe", "code.exe"]
    code = d["unlisted"][1]
    assert code["total_ms"] == 6 * MIN and code["last_ms"] == T0 - 2 * MIN
    assert d["unlisted"][0]["last_ms"] == T0 and d["unlisted"][0]["total_ms"] == MIN  # 还开着：算到现在
    st.set_app_tier("Code.exe", "record")
    assert [u["app"].lower() for u in q.apps()["unlisted"]] == ["steam.exe"]


def test_idle_worker_queries(env):
    cfg, st, q, clk = env
    h14 = hour_floor(T0)
    sid, v1, o, _ = look(st, URL("https://a.example/", title="A"), T0 + 10 * MIN, L(*[f"第{i}行" for i in range(25)]))
    st.visit_leave(v1, T0 + 70 * MIN)  # 14:10–15:10
    clk.t = T0 + 3 * 60 * MIN
    assert q.hours_without_line(T0 + 2 * 60 * MIN + 5 * MIN) == [h14, h14 + 60 * MIN]
    assert q.hours_without_line(T0 + 60 * MIN + 59 * MIN) == [h14]  # 15 点这一小时还没完整
    st.put_hour_line(h14, "读了 A", None)
    assert q.hours_without_line(T0 + 3 * 60 * MIN) == [h14 + 60 * MIN]
    # 界面换了语言：这个时间以前写的那句也要重写
    assert q.hours_without_line(T0 + 3 * 60 * MIN, stale_before_ms=clk.t) == [h14 + 60 * MIN]
    assert q.hours_without_line(T0 + 3 * 60 * MIN, stale_before_ms=clk.t + 1) == [h14, h14 + 60 * MIN]
    assert q.hour_digest(h14) == [{"name": "A", "app": "msedge.exe", "dwell_ms": 60 * MIN, "new_lines": 25,
                                   "copied": 0}]
    assert q.hour_digest(h14 + 2 * 60 * MIN) == []
    u = q.unnamed_sources(5)
    assert u[0]["source_id"] == sid and len(u[0]["sample"]) == 20 and u[0]["sample"][0] == "第0行"
    st.set_source_name(sid, "某网页")
    assert q.unnamed_sources(5) == [] and q.unnamed_sources(5, stale_before_ms=clk.t) == []
    assert q.unnamed_sources(5, stale_before_ms=clk.t + 1)[0]["source_id"] == sid  # 名字是换语言以前起的
    after = q.lines_after(0, limit=3)
    assert [x[1] for x in after] == ["第0行", "第1行", "第2行"] and after[0][3] == sid
    assert q.lines_after(after[-1][0], limit=100)[0][1] == "第3行"
