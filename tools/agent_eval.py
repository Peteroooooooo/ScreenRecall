"""让真实 Agent（Claude Code / Codex CLI）通过 MCP 回答演示数据上的问题集，并打分。

    python tools/agent_eval.py --root <临时目录> --build                 # 生成演示数据（含大模型概括）
    python tools/agent_eval.py --root <临时目录> --label before [--agents claude,codex] [--only C1,P2]

- 演示数据：screenrecall.v2.demo（全是合成文字）；问题集：demo.QUESTIONS；
- 每轮先在 <root>/home（SCREENRECALL_HOME）+ <root>/data 上起一个隔离的主程序（--no-children：不录屏、
  不起空闲整理，空闲端口），Agent 通过 stdio 转发程序 python -m screenrecall.app.mcp_shim 连它；
- Claude：claude -p --model claude-opus-5-5-thinking --strict-mcp-config（只有 screenrecall），
  Codex：codex exec -m gpt-6.1-sol --ephemeral -s read-only，用 -c 临时加 screenrecall、关掉用户配置里其他 MCP；
  都在空的临时目录里跑，不改 ~/.claude.json、~/.codex/config.toml；
- setup='agent_off' 的题：先把 control.agent_enabled 关掉，答完再打开；
- 原始记录（只有合成数据）写在 <root>/runs/<label>/，汇总写 <root>/results_<label>.json，并打印表格。
用完由调用方删掉 <root>。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from screenrecall.v2 import demo  # noqa: E402

CLAUDE_MODEL = "claude-opus-5-5-thinking"
CODEX_MODEL = "gpt-6.1-sol"
PREFIX = "Answer from my screen history using the ScreenRecall tools. Be concise.\n\n"
RUN_TIMEOUT_S = 900


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def child_env(home: Path) -> dict:
    env = dict(os.environ)
    env.update({"SCREENRECALL_HOME": str(home), "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8",
                "TQDM_DISABLE": "1", "HF_HUB_DISABLE_PROGRESS_BARS": "1"})
    env.pop("SCREENRECALL_DATA_DIR", None)
    return env


class MainApp:
    """隔离的主程序：临时 home + 演示数据目录 + 空闲端口，不起录制 / 整理子进程。"""

    def __init__(self, root: Path):
        self.root = root
        self.home, self.data = root / "home", root / "data"
        self.proc: subprocess.Popen | None = None
        import sqlite3
        from datetime import date

        c = sqlite3.connect(f"file:{self.data / 'recall.sqlite3'}?mode=ro", uri=True)
        try:
            meta = json.loads(c.execute("SELECT value FROM meta WHERE key = 'demo_seed'").fetchone()[0])
        finally:
            c.close()
        self.end_date = date.fromisoformat(meta["end_date"])

    def __enter__(self) -> "MainApp":
        self.home.mkdir(parents=True, exist_ok=True)
        ep = self.home / "endpoint.json"
        ep.unlink(missing_ok=True)
        port = free_port()
        log = open(self.root / "main.out.log", "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "screenrecall.app", "--no-tray", "--no-browser", "--no-children",
             "--data-dir", str(self.data), "--port", str(port)],
            cwd=str(ROOT), env=child_env(self.home), stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 60:
            try:
                # venv 的 python.exe 可能只是启动器，真正的解释器是它的子进程：endpoint 里记的是子进程的 pid
                if json.loads(ep.read_text(encoding="utf-8")).get("pid"):
                    break
            except (OSError, ValueError):
                pass
            if self.proc.poll() is not None:
                raise RuntimeError("主程序启动失败，见 main.out.log")
            time.sleep(0.2)
        else:
            raise RuntimeError("主程序 60 秒内没有就绪")
        print(f"main ready on port {port} in {time.monotonic() - t0:.1f}s", file=sys.stderr)
        return self

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            import psutil

            try:
                kids = psutil.Process(self.proc.pid).children(recursive=True)
            except psutil.Error:
                kids = []
            for k in kids:
                try:
                    k.kill()
                except psutil.Error:
                    pass
            self.proc.kill()
            self.proc.wait(15)

    def set_agent_enabled(self, on: bool) -> None:
        from screenrecall.store.writer import Writer

        with Writer(self.data) as w:
            w.control_set(agent_enabled=on).result(30)
        time.sleep(1.5)  # 主进程的 control 缓存 1 秒


def mcp_entry(home: Path) -> dict:
    return {"command": sys.executable, "args": ["-m", "screenrecall.app.mcp_shim"],
            "env": {"SCREENRECALL_HOME": str(home), "PYTHONPATH": str(ROOT)}}


def run_claude(q: demo.Question, home: Path, workdir: Path, raw: Path) -> dict:
    cfg = workdir / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"screenrecall": mcp_entry(home)}}), encoding="utf-8")
    exe = shutil.which("claude") or "claude"
    cmd = [exe, "-p", "--model", CLAUDE_MODEL, "--output-format", "stream-json", "--verbose",
           "--mcp-config", str(cfg), "--strict-mcp-config", "--allowedTools", "mcp__screenrecall__*"]
    t0 = time.monotonic()
    p = subprocess.run(cmd, input=PREFIX + q.text, cwd=str(workdir), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=RUN_TIMEOUT_S)
    dur = time.monotonic() - t0
    raw.write_text(p.stdout + "\n--- stderr ---\n" + p.stderr, encoding="utf-8")
    tools, answer, cost, turns, mcp_status = [], "", None, None, None
    for line in p.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            mcp_status = [s.get("status") for s in ev.get("mcp_servers", [])]
        if ev.get("type") == "assistant":
            for b in ev.get("message", {}).get("content", []):
                if b.get("type") == "tool_use":
                    tools.append(b.get("name", "").replace("mcp__screenrecall__", ""))
        if ev.get("type") == "result":
            answer = ev.get("result") or ""
            cost, turns = ev.get("total_cost_usd"), ev.get("num_turns")
            dur = (ev.get("duration_ms") or dur * 1000) / 1000
    return {"answer": answer, "tools": tools, "duration_s": round(dur, 1), "cost_usd": cost, "turns": turns,
            "mcp": mcp_status, "exit": p.returncode}


def codex_overrides(home: Path) -> list[str]:
    import tomllib

    e = mcp_entry(home)

    def lit(s: str) -> str:  # TOML 字面字符串（Windows 路径里的反斜杠不用转义）
        assert "'" not in s
        return f"'{s}'"

    args = "[" + ", ".join(lit(a) for a in e["args"]) + "]"
    env = "{" + ", ".join(f"{k} = {lit(v)}" for k, v in e["env"].items()) + "}"
    out = ["-c", f"mcp_servers.screenrecall.command={lit(e['command'])}", "-c", f"mcp_servers.screenrecall.args={args}",
           "-c", f"mcp_servers.screenrecall.env={env}"]
    if os.environ.get("SR_EVAL_CODEX_APPROVE", "1") == "1":
        # codex exec 的审批策略是 never：MCP 工具调用要审批就直接失败。只读工具预先批准
        out += ["-c", 'mcp_servers.screenrecall.default_tools_approval_mode="approve"']
    if os.environ.get("SR_EVAL_CODEX_REQUIRED", "1") == "1":
        # 不加的话 Codex 不等 MCP 服务器起来就开始第一轮：约一半的运行里模型看不到 screenrecall 的工具
        out += ["-c", "mcp_servers.screenrecall.required=true"]
    out += [x for x in os.environ.get("SR_EVAL_CODEX_EXTRA", "").split("|") if x]
    try:
        user = tomllib.loads((Path.home() / ".codex" / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        user = {}
    for name in (user.get("mcp_servers") or {}):
        if name != "screenrecall":
            out += ["-c", f"mcp_servers.{name}.enabled=false"]
    return out


def run_codex(q: demo.Question, home: Path, workdir: Path, raw: Path) -> dict:
    exe = shutil.which("codex") or "codex"
    cmd = [exe, "exec", "-m", CODEX_MODEL, "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
           "-C", str(workdir), "--json", *codex_overrides(home), "-"]
    t0 = time.monotonic()
    p = subprocess.run(cmd, input=PREFIX + q.text, cwd=str(workdir), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=RUN_TIMEOUT_S)
    dur = time.monotonic() - t0
    raw.write_text(p.stdout + "\n--- stderr ---\n" + p.stderr, encoding="utf-8")
    tools, other, answer, usage = [], [], "", None
    for line in p.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        item = ev.get("item") or {}
        if ev.get("type") == "item.completed":
            t = item.get("type")
            if t == "mcp_tool_call":
                tools.append(item.get("tool") if item.get("server") == "screenrecall"
                             else f"{item.get('server')}:{item.get('tool')}")
            elif t == "agent_message":
                answer = item.get("text") or answer
            elif t in ("command_execution", "web_search", "file_change"):
                other.append(t)
        if ev.get("type") == "turn.completed":
            usage = ev.get("usage")
    return {"answer": answer, "tools": tools, "other_tools": other, "duration_s": round(dur, 1), "usage": usage,
            "exit": p.returncode}


RUNNERS = {"claude": run_claude, "codex": run_codex}


def one(agent: str, q: demo.Question, app: MainApp, runs: Path) -> dict:
    work = runs / f"work_{agent}_{q.qid}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        r = RUNNERS[agent](q, app.home, work, runs / f"{agent}_{q.qid}.jsonl")
    except subprocess.TimeoutExpired:
        r = {"answer": "", "tools": [], "duration_s": RUN_TIMEOUT_S, "error": "timeout"}
    ok, why = demo.score(q, r.get("answer", ""), app.end_date)
    r.update({"agent": agent, "qid": q.qid, "kind": q.kind, "correct": ok, "reason": why,
              "n_tools": len(r.get("tools", []))})
    print(f"{agent:6} {q.qid:3} {'OK ' if ok else 'BAD'} tools={r['n_tools']:2} {r['duration_s']:6.1f}s {why}",
          file=sys.stderr, flush=True)
    return r


def table(results: list[dict]) -> str:
    rows = ["| q | agent | correct | tool calls | tools | s | reason |", "|---|---|---|---|---|---|---|"]
    for r in sorted(results, key=lambda r: ([q.qid for q in demo.QUESTIONS].index(r["qid"]), r["agent"])):
        from collections import Counter

        c = Counter(r.get("tools", []))
        tools = ", ".join(f"{k}x{v}" if v > 1 else k for k, v in c.items())
        rows.append(f"| {r['qid']} | {r['agent']} | {'yes' if r['correct'] else 'no'} | {r['n_tools']} | {tools} | "
                    f"{r['duration_s']} | {r['reason']} |")
    for agent in sorted({r["agent"] for r in results}):
        rs = [r for r in results if r["agent"] == agent]
        rows.append(f"\n{agent}: {sum(r['correct'] for r in rs)}/{len(rs)} correct, "
                    f"mean tool calls {sum(r['n_tools'] for r in rs) / max(1, len(rs)):.1f}, "
                    f"mean {sum(r['duration_s'] for r in rs) / max(1, len(rs)):.0f}s")
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--label", default="run")
    ap.add_argument("--agents", default="claude,codex")
    ap.add_argument("--only", default="")
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8")
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if args.build:
        os.environ["SCREENRECALL_HOME"] = str(root / "home")
        rep = demo.generate(root / "data", log=lambda m: print(m, file=sys.stderr))
        wrep = demo.run_worker(root / "data")
        print(json.dumps({"gen_s": rep.seconds, "worker_s": wrep.get("seconds"), "llm": wrep.get("steps", {}).get("llm"),
                          "counts": demo.counts(root / "data")}, ensure_ascii=False, indent=1))
        return 0
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    qs = [q for q in demo.QUESTIONS if not only or q.qid in only]
    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    runs = root / "runs" / args.label
    runs.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    lock = threading.Lock()
    with MainApp(root) as app:
        normal = [(a, q) for q in qs if q.setup is None for a in agents]
        with ThreadPoolExecutor(args.concurrency) as pool:
            for r in pool.map(lambda aq: one(aq[0], aq[1], app, runs), normal):
                with lock:
                    results.append(r)
        off = [q for q in qs if q.setup == "agent_off"]
        if off:
            app.set_agent_enabled(False)
            try:
                with ThreadPoolExecutor(args.concurrency) as pool:
                    results += list(pool.map(lambda aq: one(aq[0], aq[1], app, runs),
                                             [(a, q) for q in off for a in agents]))
            finally:
                app.set_agent_enabled(True)
    (root / f"results_{args.label}.json").write_text(json.dumps(results, ensure_ascii=False, indent=1),
                                                     encoding="utf-8")
    print(table(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
