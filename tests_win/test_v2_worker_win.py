"""空闲整理进程的真实 Windows 检查（单独运行：python -m pytest tests_win\\test_v2_worker_win.py -q -s -p no:cacheprovider）。

- 由 app.supervisor 按空闲子进程的方式拉起：低优先级（BELOW_NORMAL）、stdin 管道；
  正在等大模型回答时 stop_child（关 stdin）也在 1 秒左右正常退出（退出码 0，不必等作业被杀）；
- 真模型整条流水线（OCR tiny / small 第二遍、ONNX 文本编码器、CLIP + 内容分类器）在合成数据上的吞吐和内存。
全部数据是本测试用 PIL 画的合成画面和合成文字，数据目录、应用目录都在临时目录里。
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
HOUR = 3_600_000


def _cfg(tmp_path, **idle):
    from screenrecall.v2.config import load_v2

    cfg = load_v2(tmp_path / "data", home=tmp_path / "home")
    return replace(cfg, idle=replace(cfg.idle, **idle)) if idle else cfg


class HangingLlm:
    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.got = threading.Event()
        self.conns = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(c)
            try:
                c.recv(65536)
            except OSError:
                pass
            self.got.set()

    def close(self):
        for c in self.conns:
            c.close()
        self.sock.close()


def test_supervised_worker_is_below_normal_and_stops_on_stdin_eof(tmp_path):
    import psutil

    from screenrecall.app.supervisor import ChildSpec, Supervisor, child_env, python_child
    from screenrecall.store.writer import LineIn, Writer
    from screenrecall.timeutil import utc_ms

    cfg = _cfg(tmp_path)
    t = utc_ms() - 3 * HOUR
    with Writer(cfg.data_dir) as w:
        sid = w.segment_for("word.exe", "周报.docx", t).result(5)
        b = w.block_new(sid, t).result(5)
        w.record_lines(b, t + 1000, (0, 100), [LineIn("合成的测试内容", 10, 10, 300, 30)]).result(5)
        w.activity_start(t, hwnd=1, pid=1, process="word.exe", title="周报.docx").result(5)
        w.activity_end(t + 600_000).result(5)
    llm = HangingLlm()
    env = {k: v for k, v in child_env().items() if not k.startswith(("LLM_", "SCREENRECALL_"))}
    env.update({"LLM_BASE_URL": f"http://127.0.0.1:{llm.port}/v1", "LLM_API_KEY": "test-key",
                "LLM_MODEL": "test-model", "SCREENRECALL_HOME": str(tmp_path / "home"), "PYTHONIOENCODING": "utf-8"})
    log_path = tmp_path / "idle-worker.log"
    spec = ChildSpec("idle-worker", python_child("screenrecall.v2.worker", "--data-dir", str(cfg.data_dir),
                                                 "--home", str(tmp_path / "home")),
                     restart="on-failure", below_normal=True, autostart=False, cwd=str(ROOT), env=env,
                     log_path=str(log_path))
    sup = Supervisor([spec]).start()
    try:
        assert sup.start_child("idle-worker")
        pid = sup.pid("idle-worker")
        assert psutil.Process(pid).nice() == psutil.BELOW_NORMAL_PRIORITY_CLASS
        assert llm.got.wait(90), log_path.read_text("utf-8", "replace")[-2000:]
        # venv 的 python.exe 是个启动器，真正的解释器是它的子进程（同在作业里，同样低优先级）
        procs = [psutil.Process(pid)] + psutil.Process(pid).children(recursive=True)
        assert all(p.nice() == psutil.BELOW_NORMAL_PRIORITY_CLASS for p in procs)
        rss = sum(p.memory_info().rss for p in procs) / 2 ** 20
        t0 = time.monotonic()
        code = sup.stop_child("idle-worker")
        elapsed = time.monotonic() - t0
        text = log_path.read_text("utf-8", "replace")
        assert code == 0, text[-2000:]
        assert elapsed < 2.0, f"stop_child took {elapsed:.2f} s"
        # 进度日志写在 <home>/logs/idle-worker.log；标准输出 / 错误（log_path）只留警告和出错
        assert '"stopped": true' in (tmp_path / "home" / "logs" / "idle-worker.log").read_text("utf-8", "replace")
        assert '"stopped": true' not in text
        print(f"\nsupervised stop (stdin EOF while waiting for LLM): {elapsed * 1000:.0f} ms, "
              f"RSS while waiting for the LLM {rss:.0f} MiB (text encoder already released)")
    finally:
        sup.stop_all()
        llm.close()


def _font(size):
    from PIL import ImageFont

    for name in ("msyh.ttc", "simsun.ttc", "arial.ttf"):
        p = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / name
        if p.exists():
            return ImageFont.truetype(str(p), size)
    pytest.skip("没有可用字体")


WORDS = ("项目 进度 接口 联调 测试 报告 周会 需求 设计 文档 数据库 索引 缓存 部署 回滚 日志 监控 告警 "
         "release build pipeline cache index query latency throughput memory worker idle").split()


def _synthetic_frame(rng, w=1280, h=800, rows=18):
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    font = _font(22)
    y = 30
    for _ in range(rows):
        d.text((40, y), " ".join(rng.choice(WORDS, size=int(rng.integers(4, 9)))), fill="black", font=font)
        y += 42
    return np.ascontiguousarray(np.asarray(img)[:, :, ::-1])


def test_real_pipeline_throughput_and_memory(tmp_path):
    import psutil

    from screenrecall.store.writer import LineIn, Writer
    from screenrecall.text import ocr2
    from screenrecall.timeutil import utc_ms
    from screenrecall.v2.worker import Worker

    try:
        ocr2.resolve_models("tiny", ocr2.DEFAULT_MODELS_DIR)
    except ocr2.OcrModelsMissing:
        pytest.skip("本机没有 OCR 模型")
    cfg = _cfg(tmp_path, summaries=False)
    rng = np.random.default_rng(7)
    t = utc_ms() - 2 * HOUR
    n_frames, n_lines = 12, 3000
    with Writer(cfg.data_dir) as w:
        sid = w.segment_for("synthetic.exe", "throughput", t).result(5)
        for i in range(n_frames):
            b = w.block_new(sid, t + i).result(5)
            # 两条带：上半屏、下半屏（UIA 读不到字时 ocr_regions 给的就是这样的整宽带）
            w.save_frame(_synthetic_frame(rng), captured_ms=t + i * 1000, segment_id=sid, block_id=b, pos_y=0,
                         ocr_rects=[(0, 0, 1280, 400), (0, 400, 1280, 800)])
        w.flush()
        b = w.block_new(sid, t).result(5)
        for s in range(0, n_lines, 100):
            w.record_lines(b, t + 5000, (s * 30 - 5, (s + 100) * 30 + 5), [
                LineIn(" ".join(rng.choice(WORDS, size=int(rng.integers(3, 12)))), 10, k * 30, 900, k * 30 + 20)
                for k in range(s, s + 100)]).result(30)
    proc = psutil.Process()
    rss0 = proc.memory_info().rss / 2 ** 20
    rep = Worker(cfg, llm_factory=None).run(once=True)
    st = rep["steps"]
    ocr, text, clip = st["ocr"], st["text"], st.get("clip", {})
    print(f"\nRSS at start {rss0:.0f} MiB, after run {rep['rss_mb']} MiB, peak working set {rep['peak_mb']} MiB")
    print(f"OCR: {ocr['items']} regions ({ocr['pixels'] / 1e6:.1f} MP), {ocr['lines']} lines, {ocr['seconds']:.2f} s "
          f"-> {ocr['items'] / ocr['seconds']:.2f} regions/s, {ocr['pixels'] / 1e6 / ocr['seconds']:.2f} MP/s, "
          f"second pass {ocr.get('second_pass', 0)}")
    print(f"text vectors: {text['items']} lines in {text['chunks']} chunks, {text['seconds']:.2f} s "
          f"-> {text['items'] / text['seconds']:.0f} lines/s, {text['chunks'] / text['seconds']:.0f} chunks/s")
    if clip:
        print(f"CLIP: {clip['items']} frames, {clip['seconds']:.2f} s (incl. model load), labelled {clip['labelled']}")
    print(f"merge {st['merge']['seconds']:.3f} s, total {rep['seconds']:.1f} s")
    assert ocr["items"] == 2 * n_frames and ocr["lines"] >= n_frames * 15
    assert text["items"] >= n_lines
