"""空闲整理进程作为子进程：--once 自己退出；stdin 关闭后约 1 秒内退出（正在等大模型回答时也一样）；同一数据目录只跑一个。

真模型：OCR tiny 端到端（合成的 PIL 渲染证据帧；缺模型时跳过）。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from screenrecall.store.writer import Writer
from tests._v2_worker_fakes import db, make_cfg, now_ms, put_lines

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
HOUR = 3_600_000


def _env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LLM_", "SCREENRECALL_"))}
    env.update({"PYTHONPATH": str(ROOT), "SCREENRECALL_HOME": str(tmp_path / "home"), "PYTHONIOENCODING": "utf-8",
                # 不让子进程碰真实的大模型接口：三个键都指向本测试自己的假服务（或一个不会被用到的地址）
                "LLM_BASE_URL": "http://127.0.0.1:9/v1", "LLM_API_KEY": "test-key", "LLM_MODEL": "test-model"})
    env.update(extra)
    return env


def _start(tmp_path, *args, env=None):
    return subprocess.Popen([PY, "-m", "screenrecall.v2.worker", "--data-dir", str(tmp_path / "data"),
                             "--home", str(tmp_path / "home"), *args], cwd=str(ROOT), env=env or _env(tmp_path),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def test_once_on_empty_data_dir_exits_zero(tmp_path):
    p = _start(tmp_path, "--once")
    try:
        out, err = p.communicate(timeout=60)
    finally:
        if p.poll() is None:
            p.kill()
    text = err.decode("utf-8", "replace")
    assert p.returncode == 0, text[-2000:]
    assert "整理结束" not in text  # 标准错误只留警告和出错（主进程把它追加到 idle-worker.out.log）
    assert "整理结束" in _log(tmp_path) and (tmp_path / "data" / "recall.sqlite3").exists()


def _log(tmp_path) -> str:
    p = tmp_path / "home" / "logs" / "idle-worker.log"
    return p.read_text("utf-8", "replace") if p.exists() else ""


CHILD_LOG = r"""
import logging, sys
from pathlib import Path
import httpx
from screenrecall.v2.worker import setup_logging
from screenrecall.app.llm import LlmClient
setup_logging(Path(sys.argv[1]))
n = [0]
def h(req):
    n[0] += 1
    if n[0] == 1:
        return httpx.Response(503, json={"error": {"message": "echo SCREEN-TEXT-SENTINEL"}})
    return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "OK"}}]})
c = LlmClient("http://relay.invalid/secret-path-XYZ/v1", "k", "m", transport=httpx.MockTransport(h), sleep=lambda s: None)
c.chat([{"role": "user", "content": "x"}])
logging.getLogger("screenrecall.v2.worker").info("an info line")
logging.getLogger("screenrecall.v2.worker").warning("a warning line")
"""


def test_worker_logging_goes_to_its_file_without_llm_url_or_error_detail(tmp_path):
    logs = tmp_path / "logs"
    r = subprocess.run([PY, "-c", CHILD_LOG, str(logs)], cwd=str(ROOT), env=_env(tmp_path), capture_output=True,
                       timeout=60)
    err = r.stderr.decode("utf-8", "replace")
    assert r.returncode == 0, err[-2000:]
    text = (logs / "idle-worker.log").read_text("utf-8")
    assert "an info line" in text and "a warning line" in text and "LlmHttpError HTTP 503" in text
    assert "an info line" not in err and "a warning line" in err
    for out in (text, err):
        assert "secret-path-XYZ" not in out and "SCREEN-TEXT-SENTINEL" not in out


class HangingLlm:
    """接受连接、读请求、永远不回答的"大模型接口"。"""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.got_request = threading.Event()
        self._conns = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self._conns.append(c)
            try:
                c.recv(65536)
            except OSError:
                pass
            self.got_request.set()

    def close(self):
        for c in self._conns:
            c.close()
        self.sock.close()


def test_stdin_eof_stops_a_busy_worker_within_a_second_and_lock_blocks_a_second_one(tmp_path):
    cfg = make_cfg(tmp_path)
    t = now_ms() - 3 * HOUR
    with Writer(cfg.data_dir) as w:
        put_lines(w, "word.exe", "周报.docx", t + 1000, ["合成的测试内容"])
        w.activity_start(t, hwnd=1, pid=1, process="word.exe", title="周报.docx").result(5)
        w.activity_end(t + 600_000).result(5)
    llm = HangingLlm()
    env = _env(tmp_path, LLM_BASE_URL=f"http://127.0.0.1:{llm.port}/v1")
    p = _start(tmp_path, env=env)
    try:
        assert llm.got_request.wait(90), "子进程没走到大模型这一步"
        # 第二个整理进程：同一数据目录已被占用，直接退出 0
        p2 = _start(tmp_path, "--once", env=env)
        p2.communicate(timeout=60)
        assert p2.returncode == 0 and "已有一个空闲整理进程" in _log(tmp_path)
        t0 = time.monotonic()
        p.stdin.close()
        p.wait(timeout=5)
        elapsed = time.monotonic() - t0
        err = p.stderr.read().decode("utf-8", "replace")
        assert p.returncode == 0, err[-2000:]
        assert elapsed < 2.0, f"stdin 关闭后 {elapsed:.2f} s 才退出"
        assert '"stopped": true' in _log(tmp_path)
        print(f"\nEOF -> exit: {elapsed * 1000:.0f} ms")
    finally:
        if p.poll() is None:
            p.kill()
        llm.close()
    # 没写完的概括待办没有算作一次失败
    r = db(cfg).execute("SELECT status, attempts FROM jobs WHERE kind = 'summary_piece'").fetchone()
    assert tuple(r) == ("pending", 0)
    # 文字向量这一步在大模型之前已经做完（真 ONNX 编码器）
    assert db(cfg).execute("SELECT COUNT(*) FROM text_chunks").fetchone()[0] == 1


def _font(size):
    from PIL import ImageFont

    for name in ("msyh.ttc", "simsun.ttc", "arial.ttf"):
        p = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / name
        if p.exists():
            return ImageFont.truetype(str(p), size), name != "arial.ttf"
    return None, False


def test_real_tiny_ocr_end_to_end_on_synthetic_frame(tmp_path):
    from screenrecall.text import ocr2

    try:
        ocr2.resolve_models("tiny", ocr2.DEFAULT_MODELS_DIR)
    except ocr2.OcrModelsMissing:
        pytest.skip("本机没有 OCR tiny 模型")
    font, cjk = _font(28)
    if font is None:
        pytest.skip("没有可用字体")
    from PIL import Image, ImageDraw

    from screenrecall.v2.worker import Worker

    img = Image.new("RGB", (900, 300), "white")
    d = ImageDraw.Draw(img)
    d.text((40, 60), "ScreenRecall idle worker 2026", fill="black", font=font)
    d.text((40, 160), "空闲整理识别测试 12345" if cjk else "Synthetic OCR check 12345", fill="black", font=font)
    bgr = np.ascontiguousarray(np.asarray(img)[:, :, ::-1])
    cfg = make_cfg(tmp_path, idle={"clip": False, "summaries": False})
    t = now_ms() - HOUR
    with Writer(cfg.data_dir) as w:
        sid = w.segment_for("synthetic.exe", "ocr test", t).result(5)
        b = w.block_new(sid, t).result(5)
        w.save_frame(bgr, captured_ms=t, segment_id=sid, block_id=b, pos_y=0, ocr_rects=[(20, 140, 880, 220),
                                                                                         (20, 40, 880, 120)])
        w.flush()
    from tests._v2_worker_fakes import FakeEncoder

    rep = Worker(cfg, encoder_factory=FakeEncoder, llm_factory=None).run(once=True)
    rows = db(cfg).execute("SELECT text, y0, y1, source FROM lines ORDER BY y0").fetchall()
    texts = " ".join(r["text"] for r in rows)
    assert "ScreenRecall" in texts and "12345" in texts
    if cjk:
        assert "识别测试" in texts
    assert rows[0]["y0"] > 40 and rows[-1]["y1"] < 220 and all(r["source"] == "ocr" for r in rows)
    st = rep["steps"]["ocr"]
    print(f"\nreal tiny OCR: {st['items']} regions, {st['lines']} lines, {st['seconds']:.2f} s "
          f"(model load not included), second pass {st.get('second_pass', 0)}")
