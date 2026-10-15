"""证据浏览（方案 5.2、6.1）：用真实 Recorder 生成录制段、出现与缺口，再检查前后浏览的结果。"""

from screenrecall import evidence

from .conftest import make_image

A, B, C = make_image(1), make_image(2), make_image(3)


def _saved(h, img):
    """显示新画面并采样两次：大变化先等稳、下一次采样保存。"""
    h.show(img)
    h.tick()
    h.tick()


def test_prev_next_within_session_and_session_edges(h):
    h.show(A)
    h.start()
    h.rec.sample(h.conn)
    _saved(h, B)
    _saved(h, C)
    occ = h.occurrences()
    assert [o["frame_id"] for o in occ] == [f["frame_id"] for f in h.frames()]  # A、B、C 各一次

    mid = evidence.context(h.conn, occ[1]["occurrence_id"])
    assert mid.index == 1 and len(mid.timeline.steps) == 3
    assert mid.prev.occurrence_id == occ[0]["occurrence_id"]
    assert mid.next.occurrence_id == occ[2]["occurrence_id"]
    # 大变化等稳期间各记了一条 unsaved_change，前后都应提示
    assert [g.reason for g in mid.gaps_before] == ["unsaved_change"]
    assert [g.reason for g in mid.gaps_after] == ["unsaved_change"]

    first = evidence.context(h.conn, occ[0]["occurrence_id"])
    assert first.at_session_start and first.prev is None and first.gaps_before == []
    last = evidence.context(h.conn, occ[2]["occurrence_id"])
    assert last.at_session_end and last.next is None
    assert last.timeline.session.ended_at_ms is None
    assert last.timeline.session.is_active
    assert last.timeline.session.end_label == "仍在录制"


def test_a_b_a_unsaved_change_is_shown_between_two_a_occurrences(h):
    h.show(A)
    h.start()
    h.rec.sample(h.conn)
    h.show(B)
    h.tick()  # B 未保存
    h.show(A)
    h.tick()
    occ = h.occurrences()
    assert len(occ) == 2 and occ[0]["frame_id"] == occ[1]["frame_id"]
    ctx = evidence.context(h.conn, occ[1]["occurrence_id"])
    assert ctx.prev.occurrence_id == occ[0]["occurrence_id"]
    assert ctx.prev.frame_id == ctx.current.frame_id  # 同一画面，两次出现
    assert [g.reason for g in ctx.gaps_before] == ["unsaved_change"]
    msg = evidence.describe_gaps(ctx.gaps_before)
    assert "画面有变化但未保存" in msg and "不能当成连续录像" in msg


def test_lock_gap_between_occurrences(h):
    h.show(A)
    h.start()
    h.rec.sample(h.conn)
    h.screen.locked = True
    h.tick()
    h.tick()
    h.screen.locked = False
    h.tick()
    occ = h.occurrences()
    ctx = evidence.context(h.conn, occ[1]["occurrence_id"])
    assert [g.reason for g in ctx.gaps_before] == ["locked"]
    assert ctx.gaps_before[0].ended_at_ms is not None
    assert "屏幕锁定" in evidence.describe_gaps(ctx.gaps_before)


def test_pause_ends_session_and_does_not_stitch_sessions(h):
    h.show(A)
    h.start()
    h.rec.sample(h.conn)
    h.request("paused")
    h.start()
    h.rec.sample(h.conn)  # 同一画面 A 在新录制段再次出现
    occ = h.occurrences()
    assert len(occ) == 2 and occ[0]["session_id"] != occ[1]["session_id"]
    assert occ[0]["frame_id"] == occ[1]["frame_id"]

    first = evidence.context(h.conn, occ[0]["occurrence_id"])
    assert len(first.timeline.steps) == 1 and first.next is None
    assert first.timeline.session.end_reason == "paused"
    assert first.timeline.session.end_label == "用户暂停"
    assert not first.timeline.session.is_active

    second = evidence.context(h.conn, occ[1]["occurrence_id"])
    assert second.prev is None  # 不把上一个录制段拼成连续录像
    assert second.timeline.session.is_active


def test_open_gap_at_session_tail(h):
    h.show(A)
    h.start()
    h.rec.sample(h.conn)
    h.screen.locked = True
    h.tick()  # 锁屏缺口仍未结束
    occ = h.occurrences()
    ctx = evidence.context(h.conn, occ[0]["occurrence_id"])
    assert ctx.next is None
    assert [g.reason for g in ctx.gaps_after] == ["locked"]
    assert ctx.gaps_after[0].ended_at_ms is None
    assert ctx.timeline.tail == ctx.gaps_after


def test_expired_image_stays_on_timeline_deleted_frame_is_skipped(h):
    h.show(A)
    h.start()
    h.rec.sample(h.conn)
    _saved(h, B)
    _saved(h, C)
    occ = h.occurrences()
    h.conn.execute("UPDATE frames SET image_status = 'expired' WHERE frame_id = ?", (occ[0]["frame_id"],))
    h.conn.execute("UPDATE frames SET image_status = 'deleted' WHERE frame_id = ?", (occ[1]["frame_id"],))
    ctx = evidence.context(h.conn, occ[2]["occurrence_id"])
    assert [s.occurrence_id for s in ctx.timeline.steps] == [occ[0]["occurrence_id"], occ[2]["occurrence_id"]]
    assert ctx.prev.image_status == "expired"
    # 被删画面所在区间的缺口仍然在两侧之间提示
    assert {g.reason for g in ctx.gaps_before} == {"unsaved_change"}
    assert evidence.context(h.conn, occ[1]["occurrence_id"]) is None


def test_unknown_occurrence_and_unfinished_session(h):
    assert evidence.context(h.conn, "nope") is None
    h.show(A)
    h.start()
    h.rec.sample(h.conn)
    occ = h.occurrences()
    # 模拟后台异常退出：段未结束、control 不再指向它
    h.conn.execute("UPDATE control SET active_session_id = NULL WHERE id = 1")
    s = evidence.context(h.conn, occ[0]["occurrence_id"]).timeline.session
    assert not s.is_active and "未正常结束" in s.end_label


def test_describe_gaps_merges_same_reason():
    gaps = [
        evidence.Gap(1, 10, 20, "unsaved_change"),
        evidence.Gap(2, 30, 40, "unsaved_change"),
        evidence.Gap(3, 50, 60, "excluded"),
    ]
    assert evidence.describe_gaps(gaps).startswith("中间有已知缺口：画面有变化但未保存×2、命中排除规则")
    assert evidence.describe_gaps([]) == ""
