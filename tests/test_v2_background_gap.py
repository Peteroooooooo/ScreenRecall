"""钉住的窗口（后台录）切片段用单独的、更长的间隔（[v2.idle] background_piece_gap_ms，默认 10 分钟）。"""
from __future__ import annotations

from screenrecall.store import delete
from screenrecall.store.writer import Writer
from tests._v2_worker_fakes import L, db, make_cfg, now_ms, put_lines
from tests.test_v2_worker import run

M = 60_000


def chat_pieces(cfg):
    return [tuple(r) for r in db(cfg).execute(
        "SELECT start_ms, end_ms FROM pieces WHERE app = 'chat.exe' ORDER BY start_ms")]


def test_default_background_gap_is_ten_minutes(tmp_path):
    cfg = make_cfg(tmp_path)
    assert cfg.idle.background_piece_gap_ms == 600_000 and cfg.idle.piece_gap_ms == 120_000


def test_sporadic_pinned_chat_stays_one_piece(tmp_path):
    """消息隔 5 分钟来一条：前台的 2 分钟会切成 3 段；后台的 10 分钟是一段。超过 10 分钟没有新消息才是新片段。"""
    cfg = make_cfg(tmp_path, idle={"clip": False, "summaries": False})
    t = now_ms() - 3 * 3_600_000
    with Writer(cfg.data_dir) as w:
        sid, b, _ = put_lines(w, "chat.exe", "合成群聊", t, ["第一条"])
        w.record_lines(b, t + 5 * M, (0, 200), [L("第二条", 60)]).result(5)
        w.record_lines(b, t + 10 * M, (0, 200), [L("第三条", 90)]).result(5)
        w.record_lines(b, t + 25 * M, (0, 200), [L("隔了 15 分钟", 120)]).result(5)
    run(cfg)
    assert chat_pieces(cfg) == [(t, t + 10 * M), (t + 25 * M, t + 25 * M)]


def test_background_gap_is_configurable(tmp_path):
    cfg = make_cfg(tmp_path, idle={"clip": False, "summaries": False, "background_piece_gap_ms": 2 * M})
    t = now_ms() - 3 * 3_600_000
    with Writer(cfg.data_dir) as w:
        sid, b, _ = put_lines(w, "chat.exe", "合成群聊", t, ["第一条"])
        w.record_lines(b, t + 5 * M, (0, 200), [L("第二条", 60)]).result(5)
    run(cfg)
    assert chat_pieces(cfg) == [(t, t), (t + 5 * M, t + 5 * M)]


def test_longer_gap_still_does_not_bridge_a_deleted_range(tmp_path):
    cfg = make_cfg(tmp_path, idle={"clip": False, "summaries": False})
    t = now_ms() - 3 * 3_600_000
    with Writer(cfg.data_dir) as w:
        sid, b, _ = put_lines(w, "chat.exe", "合成群聊", t, ["删除前"])
    run(cfg)
    with Writer(cfg.data_dir) as w:
        w.record_lines(b, t + 2 * M, (0, 200), [L("会被删掉", 60)]).result(5)
        w.run_raw(lambda c: delete.delete_data(c, cfg.data_dir, time_from=t + M, time_to=t + 3 * M)).result(10)
        w.record_lines(b, t + 4 * M, (0, 200), [L("删除后", 120)]).result(5)
    run(cfg)
    rows = chat_pieces(cfg)
    assert all(not (a < t + 3 * M and e > t + M) for a, e in rows), rows
    assert rows[-1] == (t + 4 * M, t + 4 * M)
