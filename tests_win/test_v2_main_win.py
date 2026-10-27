"""真实 v2 主进程：python -m screenrecall.app --no-tray --no-browser --data-dir <临时> --port 0（临时 SCREENRECALL_HOME）。

录制进程换成测试替身（--recorder-module）：替身只往临时库里写心跳，不截屏，不碰用户自己的窗口。
检查：endpoint.json → 一次性链接登录 → /api/v2/status、/api/v2/settings → 打开 Agent 访问 →
MCP 转发程序（stdio）调 search / timeline 拿到测试自己写进去的合成文字 → POST /api/app/quit → 录制子进程也退出。
打印启动用时和主进程 + 子进程的内存（RSS）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import anyio
import httpx
import psutil
import pytest

from screenrecall.app import auth

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

STUB_RECORDER = textwrap.dedent("""
    # 录制进程替身：每秒写一次心跳，标准输入关闭（主进程要它退出）就退出。不截屏。
    import argparse, os, sys, threading
    from pathlib import Path
    from screenrecall.store.writer import Writer

    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--home", required=True)
    a = ap.parse_args()
    (Path(a.home) / "stub_recorder.pid").write_text(str(os.getpid()))
    w = Writer(a.data_dir)
    done = threading.Event()

    def watch_stdin():
        sys.stdin.buffer.read()
        done.set()

    threading.Thread(target=watch_stdin, daemon=True).start()
    while not done.is_set():
        w.heartbeat(recorder_state="recording", recorder_pid=os.getpid(), current_process="stub.exe",
                    current_title="synthetic stub window").result(5)
        done.wait(1.0)
    w.heartbeat(recorder_state="stopped", recorder_pid=None).result(5)
    w.close()
""")

STUB_WORKER = textwrap.dedent("""
    # 空闲整理进程替身：什么都不做，标准输入关闭就退出
    import sys
    sys.stdin.buffer.read()
""")


def wait_for(cond, timeout=30.0, step=0.05):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            v = cond()
            if v:
                return v
        except (OSError, ValueError, httpx.HTTPError):
            pass
        time.sleep(step)
    return None


def alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def rss_mib(pid: int) -> float:
    return psutil.Process(pid).memory_info().rss / 2 ** 20


def put_synthetic(data_dir: Path) -> None:
    """测试自己写进临时库的合成内容（不是任何真实屏幕）。"""
    from screenrecall.store.writer import LineIn, Writer

    t = int(time.time() * 1000) - 60_000
    w = Writer(data_dir)
    try:
        sid = w.segment_for("synthetic.exe", "合成文档 - test", t).result(10)
        b = w.block_new(sid, t).result(10)
        w.record_lines(b, t, (0, 200), [LineIn("ScreenRecall synthetic 组会 line", 10, 10, 400, 30),
                                        LineIn("second synthetic line", 10, 50, 400, 70)]).result(10)
        w.piece_put(t, t + 30_000, app="synthetic.exe", title="合成文档 - test", segment_ids=[sid],
                    task_label="测试").result(10)
        w.flush()
    finally:
        w.close()


async def shim_calls(env: dict) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=PY, args=["-m", "screenrecall.app.mcp_shim"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = [t.name for t in (await s.list_tools()).tools]
            t0 = time.perf_counter()
            search = await s.call_tool("search", {"query": "synthetic", "time_from": "today"})
            t_search = time.perf_counter() - t0
            timeline = await s.call_tool("timeline", {"time_from": "today"})
            return {"tools": tools, "search": search, "timeline": timeline, "t_search": t_search}


def text_of(res) -> str:
    return "\n".join(getattr(b, "text", "") for b in res.content)


@pytest.fixture
def stubs(tmp_path):
    d = tmp_path / "stubs"
    d.mkdir()
    (d / "sr_stub_recorder.py").write_text(STUB_RECORDER, encoding="utf-8")
    (d / "sr_stub_worker.py").write_text(STUB_WORKER, encoding="utf-8")
    return d


def test_v2_main_end_to_end(tmp_path, stubs):
    home, data = tmp_path / "home", tmp_path / "data"
    env = dict(os.environ, SCREENRECALL_HOME=str(home), PYTHONPATH=os.pathsep.join([str(ROOT), str(stubs)]))
    env.pop("SCREENRECALL_DATA_DIR", None)
    args = [PY, "-m", "screenrecall.app", "--no-tray", "--no-browser", "--data-dir", str(data), "--port", "0",
            "--recorder-module", "sr_stub_recorder", "--worker-module", "sr_stub_worker"]
    t0 = time.perf_counter()
    proc = subprocess.Popen(args, cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    rec_pid = None
    try:
        ep = wait_for(lambda: (e := auth.read_endpoint(home)) is not None and e.pid is not None and e, 60)
        assert ep, "主进程没启动"
        t_ready = time.perf_counter() - t0
        base = ep.base_url
        with httpx.Client(base_url=base, trust_env=False, timeout=15, follow_redirects=False) as c:
            assert c.get("/api/v2/status").status_code == 401
            url = c.post("/api/app/nonce", headers=auth.bearer_headers(ep)).json()["url"]
            assert c.get(url.removeprefix(base)).status_code == 303  # 一次性链接 → Cookie

            st = wait_for(lambda: (s := c.get("/api/v2/status").json())["recorder_alive"] and s, 30, 0.2)
            assert st, "录制替身的心跳没有出现"
            t_recorder = time.perf_counter() - t0
            assert st["recording"] == "recording" and st["current"] == {"app": "stub.exe",
                                                                        "title": "synthetic stub window"}
            assert st["agent_enabled"] is False
            rec_pid = int((home / "stub_recorder.pid").read_text())
            assert alive(rec_pid)

            s = c.get("/api/v2/settings").json()
            assert s["data_dir"] == str(data.resolve()) and s["agent_enabled"] is False
            assert "api_key" not in str(s["llm"]).lower()
            r = c.post("/api/v2/settings", json={"agent_enabled": True}, headers={"X-ScreenRecall": "1"})
            assert r.status_code == 200 and r.json()["agent_enabled"] is True

            put_synthetic(data)
            r = c.get("/api/v2/search", params={"q": "synthetic"}).json()
            assert r["total_groups"] == 1 and len(r["groups"][0]["hits"]) == 2

            procs = [psutil.Process(ep.pid)] + psutil.Process(ep.pid).children(recursive=True)
            rss = {}
            for p in procs:
                cmd = p.cmdline()
                mod = cmd[cmd.index("-m") + 1] if "-m" in cmd else p.name()
                rss[p.pid] = (f"{p.name()} {mod}", rss_mib(p.pid))  # venv 的 python.exe 是启动器，真进程是它的子进程

            res = anyio.run(shim_calls, {**env, "PYTHONPATH": str(ROOT)})
            assert res["tools"] == ["search", "timeline", "summary", "get_frame"]
            out = text_of(res["search"])
            assert not res["search"].isError and "> ScreenRecall synthetic 组会 line" in out, out
            tl = text_of(res["timeline"])
            assert not res["timeline"].isError and "synthetic.exe" in tl and "任务 测试" in tl, tl

            r = c.post("/api/app/quit", headers={"X-ScreenRecall": "1"})
            assert r.status_code == 200
        assert proc.wait(30) == 0
        assert wait_for(lambda: not alive(rec_pid), 10), "主进程退出后录制子进程还在"
        assert auth.read_endpoint(home).pid is None
        print(f"\n主进程可用 {t_ready:.2f} s；录制替身心跳可见 {t_recorder:.2f} s；"
              f"shim search 一次 {res['t_search'] * 1000:.0f} ms")
        for pid, (name, mib) in rss.items():
            print(f"RSS {name} pid={pid}: {mib:.0f} MiB")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
        if proc.stdout:
            log = proc.stdout.read().decode("utf-8", "replace")
            proc.stdout.close()
            if log.strip():
                print("\n主进程输出（最后 20 行）：\n" + "\n".join(log.splitlines()[-20:]))
        if rec_pid and alive(rec_pid):
            psutil.Process(rec_pid).kill()
