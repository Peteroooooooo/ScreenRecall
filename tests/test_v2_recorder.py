"""v2 录制子进程：录制核心（假窗口 / 假输入 / 假截图来源）+ 假 UIA 读取器 → StoreSink / 读字线程 / 控制循环
→ 真的 Writer 写临时库。全部是合成画面和合成文字。

不开线程：每一步手动 core.step()，然后让读字线程同步处理完队列，每 500 ms 跑一次控制循环。
"""

from __future__ import annotations

import json
import random
import sqlite3

import numpy as np
import pytest
from PIL import Image, ImageDraw

from screenrecall.rec.core import RecorderConfig
from screenrecall.rec.inputmon import FakeInput
from screenrecall.rec.wgc import _SlotSource
from screenrecall.rec.windows import FakeWindows
from screenrecall.text import uia
from screenrecall.v2.config import load_v2
from screenrecall.v2.rec_sink import EXCLUDED_TITLE, STOPPED_DETAIL
from screenrecall.v2.rec_text import TextWorker, covered_spans
from screenrecall.v2.recorder import RecorderProcess
from tests import _rec_synth as S

W, VH, HEAD = 800, 480, 60
LINE_H, PITCH = 30, 34
T0 = 1_760_000_000_000


# ---------------- 合成文档和窗口 ----------------

class Doc:
    """一张长文档图 + 每行的文字和纵向位置（文档坐标）。"""

    def __init__(self, n: int = 100, seed: int = 3, prefix: str = "第"):
        rng = random.Random(seed)
        img = Image.new("RGB", (W, 10 + PITCH * n + VH), (255, 255, 255))
        d = ImageDraw.Draw(img)
        self.lines: list[list] = []
        for i in range(n):
            y = 10 + PITCH * i
            text = f"{prefix}{i:03d}行 " + S.sentence(rng, 6)
            d.text((20, y), text, fill=(20, 20, 20), font=S.font(22))
            self.lines.append([y, y + LINE_H, text])
        self.img = np.asarray(img)[:, :, ::-1].copy()

    def rewrite(self, i: int, text: str) -> None:
        y = self.lines[i][0]
        self.img = S.write_text(self.img, 20, y, text)
        self.lines[i][2] = text


class Win:
    def __init__(self, env, hwnd: int, process: str, title: str, doc: Doc | None, *, uia_text: bool = True):
        self.env, self.hwnd, self.doc, self.uia_text = env, hwnd, doc, uia_text
        self.top = 0
        self.origin = (0, 0)  # 窗口在屏幕上的左上角（只影响 UIA 给的屏幕坐标；假截图来源的帧边界固定在 (0, 0)）
        self.head = S.header(W)
        env.win.add(hwnd, process, title)
        env.wins[hwnd] = self

    def image(self) -> np.ndarray:
        return np.ascontiguousarray(np.vstack([self.head, self.doc.img[self.top:self.top + VH]]))

    def show(self, top: int | None = None) -> None:
        if top is not None:
            self.top = top
        self.env.src(self.hwnd).push(self.image(), self.env.clock.t)

    def uia_result(self) -> uia.UiaResult:
        ox, oy = self.origin
        rect = (ox, oy, ox + W, oy + HEAD + VH)
        if not self.uia_text:
            return uia.UiaResult("empty", [], False, 3.0, 4, rect)
        lines = [uia.UiaLine("合成窗口 - 标题栏", (ox + 16, oy + 14, ox + 300, oy + 40), "TabItem", "name", "text")]
        for y0, y1, text in self.doc.lines:
            sy0, sy1 = HEAD + y0 - self.top, HEAD + y1 - self.top
            if sy1 <= HEAD or sy0 >= HEAD + VH:
                continue
            # 和真的 UIA 一样：露出半截的行给整行文字、但矩形裁到可见部分
            lines.append(uia.UiaLine(text, (ox + 20, oy + max(sy0, HEAD), ox + 780, oy + min(sy1, HEAD + VH)),
                                     "Document", "textpattern:Document", "text"))
        unc = tuple(uia.uncovered_bands(lines, rect))
        return uia.UiaResult("ok", lines, uia.content_chars(lines) >= 16, 5.0, len(lines), rect, uncovered=unc)


class FakeReader:
    def __init__(self, env):
        self.env = env
        self.reads = 0
        self.status: str | None = None  # 设成 'busy' / 'timeout' 模拟读不了

    def read(self, hwnd: int, timeout_s=None) -> uia.UiaResult:
        self.reads += 1
        if self.status:
            return uia.UiaResult(self.status, [], False, 500.0, window_rect=(0, 0, W, HEAD + VH))
        return self.env.wins[hwnd].uia_result()

    def has_text_hint(self, hwnd: int) -> bool:
        return True

    def close(self) -> None:
        pass


class FakeSource(_SlotSource):
    def __init__(self, hwnd, kind, clock):
        super().__init__(hwnd, None, None, 1.0, clock)
        self.kind = kind
        self.stopped = False
        self.started_ms = clock()

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    @property
    def finished(self):
        return False

    def push(self, img: np.ndarray, t: int):
        self._put(img, t, (0, 0, img.shape[1], img.shape[0]), img.shape[2] == 4)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class Env:
    def __init__(self, tmp_path, **core_cfg):
        self.tmp_path = tmp_path
        self.clock = Clock()
        self.win = FakeWindows()
        self.inp = FakeInput()
        self.wins: dict[int, Win] = {}
        self.reader = FakeReader(self)
        self.window_list: list[tuple[int, str, str]] = []
        self.evidence: list[tuple[int, str]] = []
        self.core_cfg = core_cfg
        self._last_tick = self.clock.t
        self.proc = self._make()

    def _make(self) -> RecorderProcess:
        cfg = load_v2(self.tmp_path / "data", home=self.tmp_path / "home")
        if self.core_cfg:
            from dataclasses import replace

            cfg = replace(cfg, recorder=RecorderConfig(**self.core_cfg))

        def factory(hwnd, kind):
            return FakeSource(hwnd, kind, self.clock)

        proc = RecorderProcess(cfg, core_kwargs=dict(windows=self.win, inputs=self.inp, source_factory=factory,
                                                     watcher=None),
                               reader=self.reader, window_list=lambda: list(self.window_list), clock=self.clock,
                               pid=4242, private_dirs=False)
        proc.open()
        if proc.control.paused:  # 新库的 control 默认 paused（schema 默认值）；测试里先打开录制
            proc.writer.control_set(desired_state="recording").result(10)
            proc.control.tick()
        sink = proc.sink
        orig = sink.evidence

        def spy(ev, reason):
            self.evidence.append((ev.still_id, reason))
            orig(ev, reason)

        sink.evidence = spy
        return proc

    @property
    def core(self):
        return self.proc.core

    def src(self, hwnd) -> FakeSource:
        return self.core.pool.sources()[hwnd]

    def run(self, ms: int, step: int = 40) -> None:
        end = self.clock.t + ms
        while self.clock.t < end:
            self.clock.t = min(end, self.clock.t + step)
            self.core.step()
            self.proc.text.drain()
            if self.clock.t - self._last_tick >= 500:
                self._last_tick = self.clock.t
                self.proc.control.tick()

    def scroll(self, w: Win, to: int, step_px: int = 120) -> None:
        """滚轮往下滚到 to：每 100 ms 一帧、每帧 step_px。"""
        while w.top < to:
            self.inp.wheel(self.clock.t, -120)
            w.show(min(to, w.top + step_px))
            self.run(100)

    def control(self, **fields) -> None:
        self.proc.writer.control_set(**fields).result(10)
        self.proc.control.tick()
        self._last_tick = self.clock.t
        self.run(80)

    def db(self) -> sqlite3.Connection:
        self.proc.writer.flush(30)
        conn = sqlite3.connect(self.proc.cfg.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def close(self) -> None:
        if self.proc.writer is not None:
            self.proc.close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def _lines(conn, block_id=None):
    sql = "SELECT * FROM lines" + (" WHERE block_id = ?" if block_id else "") + " ORDER BY y0"
    return conn.execute(sql, (block_id,) if block_id else ()).fetchall()


# ---------------- 纯函数 ----------------

def test_covered_spans():
    assert covered_spans(0, 100, []) == [(0, 100)]
    assert covered_spans(10, 100, [(30, 50), (40, 60), (90, 120)]) == [(10, 30), (60, 90)]
    assert covered_spans(0, 100, [(0, 100)]) == []


# ---------------- 读得到字的窗口：滚动 ----------------

def test_scroll_writes_lines_once_in_block_coordinates(env):
    w = Win(env, 1, "msedge.exe", "综述 - Edge", Doc())
    env.win.fg = 1
    env.run(40)
    w.show(0)
    env.run(1200)
    conn = env.db()
    seg = conn.execute("SELECT * FROM segments").fetchall()
    assert len(seg) == 1 and seg[0]["app"] == "msedge.exe" and seg[0]["title"] == "综述 - Edge"
    first = _lines(conn)
    visible = [ln for ln in w.doc.lines if ln[0] < VH]  # 最下面露出半截的那行也算
    assert [r["text"] for r in first] == [ln[2] for ln in visible]  # 标题栏那一行（正文上方）不收
    for r, ln in zip(first, visible):
        assert r["y0"] == HEAD + ln[0] and r["source"] == "uia"
    assert env.proc.text.stats.uia_reads == 1

    env.scroll(w, 360)
    env.run(1200)
    conn = env.db()
    blocks = conn.execute("SELECT * FROM blocks").fetchall()
    assert len(blocks) == 1
    rows = _lines(conn)
    texts = [r["text"] for r in rows]
    assert len(texts) == len(set(texts))  # 重叠的行只存一次
    seen = [ln for ln in w.doc.lines if ln[1] - 360 > 0 and ln[0] - 360 < VH]
    for ln in seen:
        r = next(r for r in rows if r["text"] == ln[2])
        assert abs(r["y0"] - (HEAD + ln[0])) <= 1  # 块坐标 = 屏幕 y + 位置（半截的行按行高补齐）
    # 第一屏的上半截滚出去了 → 候选帧存成证据帧，带上那次读到的行
    assert [r for _, r in env.evidence] == ["leaving"]
    fr = conn.execute("SELECT * FROM frames").fetchall()
    assert len(fr) == 1 and fr[0]["pos_y"] == 0 and fr[0]["block_id"] == blocks[0]["block_id"]
    assert fr[0]["app"] == "msedge.exe"
    links = conn.execute("SELECT COUNT(*) FROM line_in_frame WHERE frame_id = ?", (fr[0]["frame_id"],)).fetchone()[0]
    assert links == len(visible)
    assert conn.execute("SELECT COUNT(*) FROM ocr_regions").fetchone()[0] == 0
    # 滚了不到一屏：起止两屏连着，没有漏看的内容 → 不记划过
    assert conn.execute("SELECT COUNT(*) FROM skims").fetchone()[0] == 0
    act = conn.execute("SELECT * FROM activity").fetchall()
    assert len(act) == 1 and act[0]["process"] == "msedge.exe" and act[0]["excluded"] == 0


def test_fast_scroll_records_skipped_range(env):
    w = Win(env, 1, "msedge.exe", "综述 - Edge", Doc())
    env.win.fg = 1
    env.run(40)
    w.show(0)
    env.run(1200)
    env.scroll(w, 1500, step_px=150)  # 一路滑下去，中间没停
    env.run(1200)
    conn = env.db()
    sk = conn.execute("SELECT * FROM skims").fetchall()
    fh = HEAD + VH
    assert len(sk) == 1 and (sk[0]["y_from"], sk[0]["y_to"]) == (fh, 1500)
    assert sk[0]["block_id"] == conn.execute("SELECT block_id FROM blocks").fetchone()[0]
    # 中间滑过的行没读过：库里没有
    texts = {r["text"] for r in _lines(conn)}
    skipped = [ln[2] for ln in w.doc.lines if HEAD + ln[0] >= fh and HEAD + ln[1] <= 1500]
    assert skipped and not texts & set(skipped)


def test_jump_opens_new_block_in_same_segment(env):
    w = Win(env, 1, "msedge.exe", "综述 - Edge", Doc())
    env.win.fg = 1
    env.run(40)
    w.show(0)
    env.run(1200)
    w.show(2400)  # 点目录：一下跳很远，画面接不上
    env.run(1200)
    conn = env.db()
    blocks = conn.execute("SELECT * FROM blocks ORDER BY block_id").fetchall()
    assert len(blocks) == 2 and blocks[0]["segment_id"] == blocks[1]["segment_id"]
    second = _lines(conn, blocks[1]["block_id"])
    first_vis = next(ln for ln in w.doc.lines if ln[1] > 2400)
    assert second[0]["text"] == first_vis[2]
    assert abs(second[0]["y0"] - (HEAD + first_vis[0] - 2400)) <= 1  # 新块的标尺从这一屏的顶开始
    assert env.evidence == [(env.evidence[0][0], "replaced")]


def test_in_place_change_marks_replacement(env):
    doc = Doc()
    w = Win(env, 1, "notepad.exe", "笔记 - 记事本", doc)
    env.win.fg = 1
    env.run(40)
    w.show(0)
    env.run(1200)
    old = doc.lines[3][2]
    doc.rewrite(3, "第003行 这句话被改写了 rewritten")
    w.show()
    env.run(1200)
    conn = env.db()
    rows = {r["text"]: r for r in _lines(conn)}
    assert rows[old]["disappeared_ms"] is not None
    new = rows["第003行 这句话被改写了 rewritten"]
    assert new["replaces_line_id"] == rows[old]["line_id"]
    assert [r for _, r in env.evidence] == ["replaced"]  # 原话有证据


def test_identical_still_only_updates_last_seen(env):
    w = Win(env, 1, "msedge.exe", "综述 - Edge", Doc())
    env.win.fg = 1
    env.run(40)
    w.show(0)
    env.run(1200)
    env.scroll(w, 360)
    env.run(1200)
    reads = env.reader.reads
    w.show(0)  # 回到存过的那一屏（中间接不上，跳回去）
    env.run(1200)
    assert env.reader.reads == reads  # 不再读字
    conn = env.db()
    fr = conn.execute("SELECT * FROM frames").fetchone()
    assert fr["last_seen_ms"] > fr["captured_ms"]
    assert env.proc.text.stats.frame_seen == 1


# ---------------- 读不到字的窗口：要 OCR ----------------

def test_ocr_window_saves_evidence_now_with_regions_once(env):
    w = Win(env, 3, "python.exe", "Tk 文档", Doc(seed=5), uia_text=False)
    env.win.fg = 3
    env.run(40)
    w.show(0)
    env.run(1300)
    conn = env.db()
    fr = conn.execute("SELECT * FROM frames").fetchall()
    assert [r for _, r in env.evidence] == ["needs_ocr"]
    assert len(fr) == 1
    reg = conn.execute("SELECT * FROM ocr_regions").fetchall()
    assert len(reg) == 1 and (reg[0]["x0"], reg[0]["y0"], reg[0]["x1"], reg[0]["y1"]) == (0, 0, W, HEAD + VH)
    assert reg[0]["block_id"] == fr[0]["block_id"] and reg[0]["pos_y"] == 0 and reg[0]["status"] == "pending"
    assert conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0] == 0
    # 往下滚：第一屏已经存过，不会再因为"离开"存第二次；新的一屏又要 OCR，存一张新的
    env.scroll(w, 240)
    env.run(1300)
    assert [r for _, r in env.evidence] == ["needs_ocr", "needs_ocr"]
    conn = env.db()
    pos = [r["pos_y"] for r in conn.execute("SELECT pos_y FROM frames ORDER BY captured_ms")]
    assert pos == [0, 240]


def test_busy_or_timeout_uia_means_ocr(env):
    w = Win(env, 1, "msedge.exe", "综述 - Edge", Doc())
    env.reader.status = "timeout"
    env.win.fg = 1
    env.run(40)
    w.show(0)
    env.run(1300)
    assert [r for _, r in env.evidence] == ["needs_ocr"]
    conn = env.db()
    assert conn.execute("SELECT COUNT(*) FROM ocr_regions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0] == 0


def test_stale_still_is_not_read(env):
    """静止之后画面又变了（读字线程来不及）：不去读（读到的是别的画面），整帧待 OCR。"""
    w = Win(env, 1, "msedge.exe", "综述 - Edge", Doc())
    env.win.fg = 1
    env.run(40)
    w.show(0)
    # 让静止事件留在队列里，先推进画面
    t = env.clock.t + 1100
    while env.clock.t < t:
        env.clock.t += 40
        env.core.step()
    assert env.proc.text.pending() == 1
    env.inp.wheel(env.clock.t, -120)
    w.show(120)
    env.clock.t += 40
    env.core.step()
    env.proc.text.drain()
    assert env.reader.reads == 0 and env.proc.text.stats.stale == 1


# ---------------- 排除、暂停、锁屏 ----------------

def test_excluded_window_never_stores_title(env):
    # config.default.toml 的默认排除关键词里有 InPrivate；新库第一次启动时写进 control
    conn = env.db()
    ctl = conn.execute("SELECT * FROM control").fetchone()
    assert "InPrivate" in json.loads(ctl["excluded_title_keywords"])
    w = Win(env, 5, "msedge.exe", "网上银行 账户 - InPrivate", Doc(seed=9))
    env.win.fg = 5
    env.run(1200)
    assert 5 not in env.core.pool
    conn = env.db()
    gaps = conn.execute("SELECT * FROM gaps WHERE reason = 'excluded'").fetchall()
    assert len(gaps) == 1 and gaps[0]["hwnd"] == 5 and gaps[0]["process"] == "msedge.exe"
    act = conn.execute("SELECT * FROM activity").fetchall()
    assert len(act) == 1 and act[0]["excluded"] == 1 and act[0]["title"] == EXCLUDED_TITLE
    ctl = conn.execute("SELECT * FROM control").fetchone()
    assert ctl["current_title"] == EXCLUDED_TITLE and ctl["current_process"] is None
    # 库里任何地方都没有这个标题
    for table in ("segments", "activity", "gaps", "frames", "lines", "control"):
        for row in conn.execute(f"SELECT * FROM {table}"):
            assert not any(isinstance(v, str) and "银行" in v for v in tuple(row)), table
    assert w is not None and env.reader.reads == 0


def test_pause_drops_candidate_and_resume_after_pause_until(env):
    w = Win(env, 1, "msedge.exe", "综述 - Edge", Doc())
    env.win.fg = 1
    env.run(40)
    w.show(0)
    env.run(1200)
    assert env.core.trackers[1].candidate is not None
    env.control(desired_state="paused", pause_until_ms=env.clock.t + 3000)
    assert env.core.paused and env.evidence == []  # 候选帧丢掉，不存
    assert len(env.core.pool) == 0  # 暂停期间不开会话，什么都截不到
    env.run(1500)
    conn = env.db()
    # （启动时新库默认暂停、随即打开录制，留下一段零长度的；这里看之后那段）
    gap = conn.execute("SELECT * FROM gaps WHERE reason = 'paused' AND start_ms > ?", (T0,)).fetchall()
    assert len(gap) == 1 and gap[0]["hwnd"] is None and gap[0]["end_ms"] is None
    assert conn.execute("SELECT recorder_state FROM control").fetchone()[0] == "paused"
    n_lines = conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
    env.run(2000)  # pause_until 到了：自动继续
    assert not env.core.paused
    conn = env.db()
    gap = conn.execute("SELECT * FROM gaps WHERE reason = 'paused' AND start_ms > ?", (T0,)).fetchone()
    assert gap["end_ms"] is not None
    assert conn.execute("SELECT recorder_state FROM control").fetchone()[0] == "recording"
    w.show(240)
    env.run(1300)
    assert env.db().execute("SELECT COUNT(*) FROM lines").fetchone()[0] > n_lines


def test_lock_writes_one_global_gap(env):
    a = Win(env, 1, "msedge.exe", "综述 - Edge", Doc())
    b = Win(env, 2, "notepad.exe", "笔记 - 记事本", Doc(seed=4))
    env.win.fg = 1
    env.window_list = [(2, "notepad.exe", "笔记 - 记事本")]
    env.control(pinned_windows=[{"process": "notepad.exe", "title": "笔记 - 记事本"}])
    a.show(0)
    b.show(0)
    env.run(1200)
    env.win.is_locked = True
    env.run(1200)
    env.win.is_locked = False
    env.run(1200)
    conn = env.db()
    locked = conn.execute("SELECT * FROM gaps WHERE reason = 'locked'").fetchall()
    assert len(locked) == 1 and locked[0]["hwnd"] is None and locked[0]["end_ms"] is not None
    act = conn.execute("SELECT * FROM activity ORDER BY start_ms").fetchall()
    assert len(act) == 2 and act[0]["end_ms"] is not None  # 锁屏时结束，解锁后新的一条


# ---------------- 钉住的窗口 ----------------

def test_pinned_window_resolves_and_reresolves_after_reopen(env):
    Win(env, 1, "msedge.exe", "综述 - Edge", Doc())
    Win(env, 7, "x.exe", "看板", Doc(seed=7))
    env.win.fg = 1
    env.window_list = [(1, "msedge.exe", "综述 - Edge"), (7, "x.exe", "看板")]
    env.control(pinned_windows=[{"process": "X.EXE", "title": "看板"}, {"process": "x.exe", "title": "不存在"}])
    assert env.core.pinned == (7,)
    assert 7 in env.core.pool
    # 窗口关了又以新句柄打开
    del env.win.windows[7]
    env.window_list = [(1, "msedge.exe", "综述 - Edge")]
    env.run(1200)
    assert env.core.pinned == ()
    Win(env, 8, "x.exe", "看板", Doc(seed=7))
    env.window_list.append((8, "x.exe", "看板"))
    env.run(2600)
    assert env.core.pinned == (8,)
    # 标题只是相似、或进程不对的不钉
    env.control(pinned_windows=[{"process": "y.exe", "title": "看板"}])
    assert env.core.pinned == ()


def test_pin_survives_title_change_when_the_list_is_edited(env):
    """钉住的窗口标题变了（浏览器换页），之后页面又加了一个钉住项：已经找到的那一项不能因为按标题找不到而停掉。"""
    Win(env, 1, "msedge.exe", "第一页 - Edge", Doc())
    Win(env, 7, "tk.exe", "合成", Doc(seed=7))
    env.win.fg = 1
    env.window_list = [(1, "msedge.exe", "第一页 - Edge"), (7, "tk.exe", "合成")]
    env.control(pinned_windows=[{"process": "msedge.exe", "title": "第一页 - Edge"}])
    assert env.core.pinned == (1,)
    env.window_list = [(1, "msedge.exe", "第二页 - Edge"), (7, "tk.exe", "合成")]  # 标题变了
    env.control(pinned_windows=[{"process": "msedge.exe", "title": "第一页 - Edge"}, {"process": "tk.exe", "title": "合成"}])
    assert sorted(env.core.pinned) == [1, 7]
    # 页面带着窗口句柄保存：重启录制进程后按句柄找回（标题对不上也行）
    from screenrecall.v2.rec_control import _pins, resolve_pins

    pins = _pins([{"process": "msedge.exe", "title": "第一页 - Edge", "hwnd": 1}])
    assert resolve_pins(pins, env.window_list) == [1]


# ---------------- 隐私删除 ----------------

def test_block_gone_recreates_segment_and_block(env):
    w = Win(env, 1, "msedge.exe", "综述 - Edge", Doc())
    env.win.fg = 1
    env.run(40)
    w.show(0)
    env.run(1200)

    def wipe(conn):
        conn.execute("BEGIN IMMEDIATE")
        for t in ("line_in_frame", "lines", "skims", "ocr_regions", "frames", "blocks", "segments"):
            conn.execute(f"DELETE FROM {t}")
        conn.execute("COMMIT")

    env.proc.writer.run_raw(wipe).result(10)
    doc = w.doc
    doc.rewrite(2, "第002行 删除之后又看到的新内容")
    w.show()
    env.run(1200)
    assert env.proc.text.stats.block_gone == 1
    conn = env.db()
    assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0] > 0


def test_privacy_delete_drops_candidate_and_mapping(env):
    from screenrecall.store import delete

    w = Win(env, 1, "msedge.exe", "综述 - Edge", Doc())
    env.win.fg = 1
    env.run(40)
    w.show(0)
    env.run(1200)
    t_from, now = T0, env.clock.t
    env.proc.writer.run_raw(lambda c: delete.delete_data(c, env.proc.cfg.data_dir, time_from=t_from,
                                                         time_to=now + 1, now_ms=now + 1)).result(10)
    env.run(600)  # 控制循环看到新的 purges 行
    assert env.core.trackers[1].candidate is None
    env.scroll(w, 360)
    env.run(1200)
    conn = env.db()
    assert env.evidence == []  # 删除前那一屏不再存盘
    assert env.proc.text.stats.block_gone == 0
    assert conn.execute("SELECT COUNT(*) FROM lines WHERE first_seen_ms < ?", (now + 1,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0] > 0


# ---------------- 活动日志、心跳、收尾、恢复 ----------------

def test_activity_on_foreground_and_settled_title_change(env):
    Win(env, 1, "msedge.exe", "页面 A - Edge", Doc())
    Win(env, 2, "notepad.exe", "笔记 - 记事本", Doc(seed=4))
    env.win.fg = 1
    env.run(200)
    env.win.windows[1]["title"] = "下载 10% - Edge"
    env.run(200)
    env.win.windows[1]["title"] = "页面 B - Edge"  # 很快又变：只记稳定下来的那个
    env.run(1500)
    env.win.fg = 2
    env.run(200)
    env.win.fg = 0
    env.run(200)
    conn = env.db()
    act = conn.execute("SELECT * FROM activity ORDER BY activity_id").fetchall()
    assert [(a["process"], a["title"]) for a in act] == [
        ("msedge.exe", "页面 A - Edge"), ("msedge.exe", "页面 B - Edge"), ("notepad.exe", "笔记 - 记事本")]
    assert all(a["end_ms"] is not None for a in act) and act[0]["pid"] == 1001
    ctl = conn.execute("SELECT * FROM control").fetchone()
    assert ctl["recorder_pid"] == 4242 and ctl["recorder_state"] == "recording"


def test_heartbeat_reports_current_window(env):
    Win(env, 1, "msedge.exe", "综述 - Edge", Doc())
    env.win.fg = 1
    env.run(600)
    ctl = env.db().execute("SELECT * FROM control").fetchone()
    assert (ctl["current_hwnd"], ctl["current_process"], ctl["current_title"]) == (1, "msedge.exe", "综述 - Edge")
    assert ctl["recorder_heartbeat_ms"] > 0


def test_close_saves_candidate_and_marks_stopped_then_reopen_recovers(tmp_path):
    env = Env(tmp_path)
    w = Win(env, 1, "msedge.exe", "综述 - Edge", Doc())
    env.win.fg = 1
    env.run(40)
    w.show(0)
    env.run(1200)
    env.proc.close()
    assert [r for _, r in env.evidence] == ["closing"]
    conn = sqlite3.connect(env.proc.cfg.db_path)
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT COUNT(*) FROM frames WHERE status = 'ok'").fetchone()[0] == 1
    stopped = conn.execute("SELECT * FROM gaps WHERE reason = 'closed'").fetchall()
    assert len(stopped) == 1 and stopped[0]["detail"] == STOPPED_DETAIL and stopped[0]["end_ms"] is None
    assert conn.execute("SELECT recorder_state FROM control").fetchone()[0] == "stopped"
    assert conn.execute("SELECT COUNT(*) FROM activity WHERE end_ms IS NULL").fetchone()[0] == 0
    conn.close()

    env.clock.t += 60_000
    env2 = Env.__new__(Env)
    env2.__dict__.update(env.__dict__)
    env2.evidence = []
    env2.proc = env2._make()
    conn = env2.db()
    stopped = conn.execute("SELECT * FROM gaps WHERE reason = 'closed'").fetchone()
    assert stopped["end_ms"] == env.clock.t
    assert conn.execute("SELECT COUNT(*) FROM gaps WHERE reason = 'error'").fetchone()[0] == 0
    # 默认规则只写一次：页面改过的列表不被覆盖
    env2.proc.writer.control_set(excluded_title_keywords=["只有这个"]).result(10)
    env2.close()
    env3 = Env.__new__(Env)
    env3.__dict__.update(env2.__dict__)
    env3.proc = env3._make()
    kws = json.loads(env3.db().execute("SELECT excluded_title_keywords FROM control").fetchone()[0])
    assert kws == ["只有这个"]
    env3.close()


def test_crash_recovery_closes_open_rows_at_last_heartbeat(tmp_path):
    env = Env(tmp_path)
    w = Win(env, 1, "msedge.exe", "综述 - Edge", Doc())
    w.uia_text = True
    env.win.fg = 1
    env.win.windows[1]["minimized"] = False
    env.run(600)
    env.proc.writer.gap_open(env.clock.t, "minimised", hwnd=1)
    env.proc.writer.flush(10)
    hb = env.db().execute("SELECT recorder_heartbeat_ms FROM control").fetchone()[0]
    # 模拟崩溃：不收尾，直接丢掉写线程
    env.proc.control.stop()
    env.proc.writer.close()
    env.clock.t += 120_000
    env2 = Env.__new__(Env)
    env2.__dict__.update(env.__dict__)
    env2.proc = env2._make()
    conn = env2.db()
    assert conn.execute("SELECT COUNT(*) FROM gaps WHERE end_ms IS NULL AND reason != 'closed'").fetchone()[0] == 0
    err = conn.execute("SELECT * FROM gaps WHERE reason = 'error'").fetchall()
    assert len(err) == 1 and err[0]["start_ms"] == hb and err[0]["end_ms"] == env.clock.t
    act = conn.execute("SELECT * FROM activity").fetchone()
    assert act["end_ms"] == hb
    env2.close()


def test_threads_run_and_stop_cleanly(tmp_path):
    import threading
    import time

    env = Env(tmp_path)
    w = Win(env, 1, "msedge.exe", "综述 - Edge", Doc())
    env.win.fg = 1
    env.proc.start()
    try:
        deadline = time.monotonic() + 10
        shown = False
        while time.monotonic() < deadline and not env.proc.text.stats.still_ms:  # 一次静止处理完
            env.clock.t += 40
            if not shown and 1 in env.core.pool:
                w.show(0)
                shown = True
            time.sleep(0.005)
        assert env.proc.text.stats.stills == 1 and env.proc.text.stats.uia_reads == 1
    finally:
        env.proc.close()
    alive = [t.name for t in threading.enumerate() if t.name.startswith("sr-recorder")]
    assert alive == []
    conn = sqlite3.connect(env.proc.cfg.db_path)
    assert conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 1  # 收尾时候选帧存成证据帧
    conn.close()


# ---------------- 评审修复：排队中的静止、坐标、划过、出错后的块映射 ----------------

def _queue_still(env, w):
    """让一次静止留在读字线程的队列里（不处理）。"""
    env.win.fg = w.hwnd
    env.run(40)
    w.show(0)
    t = env.clock.t + 1100
    while env.clock.t < t:
        env.clock.t += 40
        env.core.step()
    assert env.proc.text.pending() == 1


def _no_text_anywhere(conn, needle):
    for table in ("segments", "activity", "gaps", "frames", "lines", "control"):
        for row in conn.execute(f"SELECT * FROM {table}"):
            assert not any(isinstance(v, str) and needle in v for v in tuple(row)), table


def test_queued_still_is_not_read_after_pause(env):
    """暂停之后不能再去读排队中的静止：那时屏幕上的已经是用户暂停后才打开的内容。"""
    w = Win(env, 1, "msedge.exe", "综述 - Edge", Doc())
    _queue_still(env, w)
    env.proc.writer.control_set(desired_state="paused").result(10)
    env.proc.control.tick()  # 暂停命令已发给核心，核心还没走下一轮
    env.proc.text.drain()
    assert env.reader.reads == 0
    env.run(200)
    conn = env.db()
    assert conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 0


def test_queued_still_is_not_read_after_window_becomes_excluded(env):
    """静止排队期间窗口标题命中了排除规则（核心还没来得及走一轮）：不读字，也不建带这个标题的内容源。"""
    w = Win(env, 1, "msedge.exe", "综述 - Edge", Doc())
    _queue_still(env, w)
    env.win.windows[1]["title"] = "网上银行 - InPrivate"
    env.proc.text.drain()
    assert env.reader.reads == 0
    env.run(1200)
    conn = env.db()
    assert conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0] == 0
    _no_text_anywhere(conn, "银行")


def test_background_still_with_excluded_title_creates_no_segment(env):
    """前台换走后，旧窗口的静止晚一秒才到；这时它的标题已命中排除：不建内容源、不读字、不存图。"""
    a = Win(env, 1, "msedge.exe", "综述 - Edge", Doc())
    Win(env, 2, "notepad.exe", "笔记 - 记事本", Doc(seed=4))
    env.win.fg = 1
    env.run(40)
    a.show(0)
    env.run(200)
    env.win.fg = 2
    env.win.windows[1]["title"] = "网上银行 - InPrivate"
    env.run(4500)  # 静止 + 后台会话延迟停止时的 closing 证据帧
    conn = env.db()
    _no_text_anywhere(conn, "银行")
    assert conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 0
    assert env.reader.reads == 0


def test_window_moved_after_still_keeps_block_coordinates(env):
    """静止之后、读字之前窗口被拖动了（内容没变，核心手里的帧还是旧位置）：按读字时的窗口位置换算。"""
    w = Win(env, 1, "msedge.exe", "综述 - Edge", Doc())
    _queue_still(env, w)
    w.origin = (130, 70)
    env.proc.text.drain()
    rows = _lines(env.db())
    visible = [ln for ln in w.doc.lines if ln[0] < VH]
    assert [r["text"] for r in rows] == [ln[2] for ln in visible]
    for r, ln in zip(rows, visible):
        assert r["y0"] == HEAD + ln[0] and r["x0"] == 20


def test_window_resized_after_still_discards_read(env):
    w = Win(env, 1, "msedge.exe", "综述 - Edge", Doc())
    _queue_still(env, w)
    orig = w.uia_result

    def resized():
        r = orig()
        return uia.UiaResult(r.status, r.lines, r.usable, r.elapsed_ms, r.element_count,
                             (0, 0, W + 200, HEAD + VH + 100), uncovered=r.uncovered)

    w.uia_result = resized
    env.proc.text.drain()
    assert env.reader.reads == 1 and env.proc.text.stats.stale == 1
    assert env.db().execute("SELECT COUNT(*) FROM lines").fetchone()[0] == 0


def test_skim_before_first_still_uses_live_frame_height(env):
    """窗口一出现就一路滑下去（之前没有过静止）：划过的范围仍然去掉起止两屏。"""
    w = Win(env, 1, "msedge.exe", "综述 - Edge", Doc())
    env.win.fg = 1
    env.run(40)
    w.show(0)
    env.run(200)
    env.scroll(w, 1500, step_px=150)
    env.run(1200)
    sk = env.db().execute("SELECT * FROM skims").fetchall()
    assert len(sk) == 1 and (sk[0]["y_from"], sk[0]["y_to"]) == (HEAD + VH, 1500)


def test_failed_block_creation_is_retried(env):
    """建库块的那批写入失败了（Future 带异常）：映射不能一直指着它，下次重新建。"""
    from concurrent.futures import Future

    real = env.proc.writer.block_new
    calls = []

    def flaky(seg, t, anchor="none"):
        calls.append(t)
        if len(calls) == 1:
            f = Future()
            f.set_exception(RuntimeError("同批操作出错，事务已回滚"))
            return f
        return real(seg, t, anchor)

    env.proc.writer.block_new = flaky
    w = Win(env, 1, "msedge.exe", "综述 - Edge", Doc())
    env.win.fg = 1
    env.run(40)
    w.show(0)
    env.run(1200)
    assert env.db().execute("SELECT COUNT(*) FROM lines").fetchone()[0] == 0
    w.doc.rewrite(2, "第002行 写入失败之后的新内容")
    w.show()
    env.run(1300)
    conn = env.db()
    assert len(calls) == 2
    assert conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 1


def test_control_loop_closes_its_connection_after_an_error(env):
    import time

    ctl = env.proc.control
    ctl.tick()
    first = ctl._conn
    real = env.core.stats
    n = {"k": 0}

    def boom():
        n["k"] += 1
        if n["k"] == 1:
            raise RuntimeError("synthetic")
        return real()

    env.core.stats = boom
    ctl.interval_s = 0.01
    ctl.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (ctl.errors == 0 or ctl._conn is None):
            time.sleep(0.01)
    finally:
        ctl.stop()
    assert ctl.errors == 1
    with pytest.raises(sqlite3.ProgrammingError):
        first.execute("SELECT 1")


def test_text_backlog_skips_oldest_reads(tmp_path):
    from screenrecall.rec.frames import Frame
    from screenrecall.rec.tracker import StillEvent
    from screenrecall.v2.rec_sink import BlockRef, StillCtx

    class NullWriter:
        pass

    tw = TextWorker(NullWriter(), FakeReader(None), max_pending_stills=2)
    img = np.zeros((10, 10, 3), np.uint8)
    for i in range(4):
        f = Frame.from_bgr(1, i, img)
        ev = StillEvent(1, i, 0, 0, 0, f, f.fmap, False, i)
        tw.put_still(StillCtx(ev, "a", "b", BlockRef(1, 0, "a", "b", 1, 1)))
    skipped = [item.skip_uia for _, item, _ in tw._q]
    assert skipped == [True, True, False, False] and tw.stats.backlog_skips == 2


def test_only_hwnds_from_flag_and_test_env():
    """测试隔离：--only-hwnds 和 SCREENRECALL_TEST_ONLY_HWNDS（主进程启动的录制子进程继承它）合并；写错就报错，
    不会悄悄变成录全部窗口。"""
    import pytest

    from screenrecall.v2 import recorder

    assert recorder.TEST_ONLY_HWNDS_ENV == "SCREENRECALL_TEST_ONLY_HWNDS"
    assert recorder.only_hwnds("", "") == set()
    assert recorder.only_hwnds("12, 0x10", "5") == {12, 16, 5}
    with pytest.raises(ValueError):
        recorder.only_hwnds("", "abc")
