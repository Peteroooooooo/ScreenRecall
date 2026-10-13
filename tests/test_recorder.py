"""Recorder 集成：真实 SQLite + WebP 落盘，假窗口与注入时钟（对应方案 8.3 A01–A04 及 4.2 表）。"""

import json

from screenrecall import db

from .conftest import make_image, tweak

A, B, C = make_image(1), make_image(2), make_image(3)


def _accepted_triggers(h):
    return [json.loads(f["capture_meta"])["trigger"] for f in h.frames()]


# ---------- 普通模式 ----------

def test_start_captures_directly(h):
    h.show(A)
    h.start()
    h.rec.sample(h.conn)
    assert len(h.frames()) == 1
    assert h.ctrl()["actual_state"] == "recording"
    assert h.ctrl()["actual_mode"] == "normal"


def test_a01_small_digit_change_saved_as_new_frame(h):
    h.show(A)
    h.start()
    h.rec.sample(h.conn)
    h.show(tweak(A, 4))  # 4/1200 ≈ 0.3% < 1%
    h.tick()
    frames = h.frames()
    assert len(frames) == 2
    assert frames[0]["pixel_hash"] != frames[1]["pixel_hash"]


def test_identical_frames_extend_one_occurrence(h):
    h.show(A)
    h.start()
    for _ in range(4):
        h.tick()
    assert len(h.frames()) == 1
    occ = h.occurrences()
    assert len(occ) == 1 and occ[0]["observations"] == 4


def test_a02_a_b_a_with_unsaved_b_matches_spec_table(h):
    """方案 4.2 表：B 在等稳期间消失 → 一个 A 内容、两次 A 出现、一个可见中断。"""
    h.show(A)
    h.start()
    h.rec.sample(h.conn)  # t0：保存 A
    h.show(B)
    h.tick()  # t1：B 与 A 差别大，等待稳定，不保存
    h.show(A)
    h.tick()  # t2：回到 A，复用 frame_A，新增出现
    h.tick()  # t3：同 t2，延长 A₂
    frames, occ = h.frames(), h.occurrences()
    assert len(frames) == 1
    assert [o["frame_id"] for o in occ] == [frames[0]["frame_id"]] * 2
    assert occ[0]["observations"] == 1 and occ[1]["observations"] == 2
    gaps = h.gaps("unsaved_change")
    assert len(gaps) == 1
    g = gaps[0]
    assert g["ended_at_ms"] is not None
    assert occ[0]["last_seen_ms"] < g["started_at_ms"] <= g["ended_at_ms"] <= occ[1]["first_seen_ms"]
    assert json.loads(g["detail"])["prev_occurrence_id"] == occ[0]["occurrence_id"]
    # 未接受候选不产生 OCR 待办
    assert h.conn.execute("SELECT COUNT(*) FROM index_jobs").fetchone()[0] == 1


def test_big_change_saved_after_it_stabilises(h):
    h.show(A)
    h.start()
    h.rec.sample(h.conn)
    h.show(B)
    h.tick()
    assert len(h.frames()) == 1
    assert "等稳定" in h.ctrl()["capture_note"]
    h.tick()
    assert len(h.frames()) == 2
    assert h.ctrl()["capture_note"] is None


def test_continuous_change_saved_after_timeout(h):
    h.show(A)
    h.start()
    h.rec.sample(h.conn)
    saved_at = []
    for i in range(6):  # 每 3 秒一帧全新画面，持续 18 秒
        h.show(make_image(100 + i))
        h.tick()
        saved_at.append(len(h.frames()))
    # 第 1 次变化在 t=3 开始等待；t=12 才满 9 秒，t=15 首次满 10 秒 → 第 5 次采样保存
    assert saved_at == [1, 1, 1, 1, 2, 2]


def test_lock_gap_rebaselines_and_breaks_occurrence(h):
    h.show(A)
    h.start()
    h.rec.sample(h.conn)
    h.screen.locked = True
    h.tick()
    h.screen.locked = False
    h.tick()  # 解锁后同一画面：复用并新增出现
    assert len(h.frames()) == 1
    assert len(h.occurrences()) == 2
    assert len(h.gaps("locked")) == 1


def test_a03_pause_stops_capture_and_discards_candidate(h):
    h.show(A)
    h.start()
    h.rec.sample(h.conn)
    h.show(B)
    h.tick()  # B 等待中
    h.request("paused")
    assert h.ctrl()["actual_state"] == "paused"
    before = h.screen.captures
    h.tick()
    h.tick()
    assert h.screen.captures == before
    h.start()
    h.rec.sample(h.conn)  # 恢复后直接捕获
    assert len(h.frames()) == 2


# ---------- 排除规则 ----------

def test_excluded_title_records_gap_without_capture(h):
    h.show(A)
    h.start()
    h.rec.sample(h.conn)
    h.screen.title = "Secret - [InPrivate] - Microsoft Edge"
    before = h.screen.captures
    h.tick()
    h.tick()
    assert h.screen.captures == before
    gaps = h.gaps("excluded")
    assert len(gaps) == 1 and "InPrivate" in gaps[0]["detail"]
    # 标题恢复后继续录制
    h.screen.title = "test.txt - 记事本"
    h.tick()
    assert h.screen.captures == before + 1
    assert h.gaps("excluded")[0]["ended_at_ms"] is not None


def test_start_on_excluded_window_is_refused(h):
    h.screen.process = "KeePass.exe"
    h.start()
    c = h.ctrl()
    assert c["actual_state"] == "paused"
    assert "排除" in c["state_reason"]
    assert int(c["applied_seq"]) == int(c["request_seq"])


# ---------- 排错模式 ----------

def test_a04_debug_mode_min_gap_between_auto_saves(h):
    h.show(A)
    h.start(mode="debug")
    assert h.ctrl()["actual_mode"] == "debug"
    assert h.ctrl()["mode_until_ms"] is not None
    assert h.rec._interval() == 1.0
    times = []
    for i in range(10):  # 每秒都变：只应每 2 秒保存一次
        h.show(make_image(200 + i))
        h.tick()
        times.append(h.clock())
    metas = [(f["captured_at_ms"], json.loads(f["capture_meta"])) for f in h.frames()]
    assert all(m["mode"] == "debug" for _, m in metas)
    assert len(metas) == 5  # t=1,3,5,7,9：每两次自动保存间隔恰为 2 秒


def test_debug_mode_saves_held_candidate_when_screen_returns(h):
    """限频期间只出现 1 秒的 B（例如一闪而过的报错）在允许时补存。"""
    h.show(A)
    h.start(mode="debug")
    h.rec.sample(h.conn)  # 保存 A（t=0）
    h.show(B)
    h.tick()  # t=1：限频，B 作为候选
    h.show(A)
    h.tick()  # t=2：允许；先存 B，再复用 A
    assert _accepted_triggers(h) == ["auto", "held"]
    occ = h.occurrences()
    assert len(occ) == 3  # A₁、B、A₂
    assert occ[0]["frame_id"] == occ[2]["frame_id"] != occ[1]["frame_id"]


def test_debug_mode_exits_after_30_minutes(h):
    h.show(A)
    h.start(mode="debug")
    h.clock.advance(29 * 60)
    h.rec.check_mode(h.conn)
    assert h.rec.mode == "debug"
    h.clock.advance(61)
    h.rec.check_mode(h.conn)
    assert h.rec.mode == "normal"
    c = h.ctrl()
    assert c["actual_mode"] == "normal" and "30" in c["mode_reason"]
    assert c["actual_state"] == "recording"
    assert h.rec._interval() == 3.0


def test_debug_mode_exits_early_on_ocr_backlog(harness_factory):
    h = harness_factory(debug_exit_backlog=3)
    h.start(mode="debug")
    for i in range(3):
        h.show(make_image(300 + i))
        h.tick(2.0)
    h.rec.check_mode(h.conn)
    assert h.rec.mode == "normal"
    assert "OCR 待办" in h.ctrl()["mode_reason"]


def test_debug_request_does_not_extend_deadline(h):
    h.start(mode="debug")
    until = h.ctrl()["mode_until_ms"]
    h.clock.advance(60)
    h.request("recording", mode="debug")
    assert h.ctrl()["mode_until_ms"] == until


def test_pause_resets_mode_to_normal(h):
    h.start(mode="debug")
    h.request("paused")
    assert h.ctrl()["actual_mode"] == "normal"
    h.start()
    assert h.rec.mode == "normal"


# ---------- 补拍 ----------

def test_snapshot_same_content_creates_manual_occurrence(h):
    h.show(A)
    h.start()
    h.rec.sample(h.conn)
    result = h.rec.snapshot(h.conn, None)
    assert "新的出现" in result
    occ = h.occurrences()
    assert len(h.frames()) == 1 and len(occ) == 2
    assert occ[1]["source_type"] == "manual"


def test_snapshot_new_content_bypasses_stability_and_rate_limit(h):
    h.show(A)
    h.start(mode="debug")
    h.rec.sample(h.conn)
    h.show(B)
    h.clock.advance(0.2)
    h.rec.snapshot(h.conn, None)  # 距上次自动保存 0.2 秒
    assert _accepted_triggers(h) == ["auto", "manual"]


def test_snapshot_refused_when_paused_or_locked_or_excluded(h):
    h.show(A)
    assert "未在录制" in h.rec.snapshot(h.conn, 1)
    h.start()
    h.screen.locked = True
    assert "锁定" in h.rec.snapshot(h.conn, 2)
    h.screen.locked = False
    h.screen.title = "无痕窗口"
    assert "排除" in h.rec.snapshot(h.conn, 3)
    assert len(h.frames()) == 0
    assert int(h.ctrl()["snapshot_applied_seq"]) == 3


def test_snapshot_request_dedup_and_run_once(h):
    from screenrecall import control

    h.show(A)
    h.start()
    seq = control.request_snapshot(h.conn)
    h.rec.request_snapshot(seq)
    h.rec.request_snapshot(seq)  # 控制轮询重复看到同一请求
    h.rec.run_once(h.conn)
    assert len([o for o in h.occurrences() if o["source_type"] == "manual"]) == 1
    assert not control.snapshot_pending(h.ctrl())


# ---------- 结构迁移 ----------

def test_migration_from_v1(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = db.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript("BEGIN;" + db._SCHEMA + "COMMIT;")
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"
    db.init_schema(conn)
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
    row = conn.execute("SELECT actual_mode, snapshot_seq FROM control").fetchone()
    assert tuple(row) == ("normal", 0)
    db.init_schema(conn)  # 再次调用无副作用
    conn.close()
