"""v3 网页 API（screenrecall/v3/web.py）：用假的 Store / Queries（返回 §3.6 形状的字典），不依赖 store 单元的代码。"""
from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from starlette.applications import Starlette
from starlette.testclient import TestClient

from screenrecall.v3 import config as v3config
from screenrecall.v3.web import V3Context, config_numbers, match_spans, routes

NOW = 1_800_000_000_000
POST_H = {"X-ScreenRecall": "1"}


def source(sid=1, kind="url", **over):
    s = {"source_id": sid, "kind": kind, "strong": kind != "app_title", "app": "msedge.exe", "title": "示例文章",
         "url": "https://blog.example.com/a" if kind == "url" else None, "path": None, "name": None,
         "first_seen_ms": NOW - 600_000, "last_seen_ms": NOW - 60_000, "shot_id": 1}
    s.update(over)
    return s


def entry(vid=1, src=None, **over):
    e = {"type": "entry", "visit_id": vid, "source": src or source(), "name": "示例文章", "start_ms": NOW - 600_000,
         "end_ms": NOW - 300_000, "open": False, "dwell_ms": 300_000, "background": False, "merged": 0,
         "new_lines": 12, "copied": 0, "reasons": ["still"], "thumb": {"shot_id": 1, "own": True}}
    e.update(over)
    return e


class FakeStore:
    def __init__(self):
        self.ctrl = {"paused_until_ms": None, "agent_enabled": 1, "rev": 0}
        self.tiers: dict[str, str] = {"msedge.exe": "record"}
        self.excl: list[dict] = []
        self.deleted: list[tuple[int, int]] = []

    def set_control(self, **fields):
        self.ctrl.update(fields)

    def control(self):
        return dict(self.ctrl)

    def set_app_tier(self, process, tier):
        self.tiers[process.lower()] = tier

    def add_exclude(self, kind, value):
        eid = len(self.excl) + 1
        self.excl.append({"exclude_id": eid, "kind": kind, "value": value, "added_ms": NOW})
        return eid

    def remove_exclude(self, eid):
        self.excl = [x for x in self.excl if x["exclude_id"] != eid]

    def delete_range(self, a, b):
        self.deleted.append((a, b))
        return {"visits": 1, "lines": 3, "shots": 1}


class FakeQueries:
    def __init__(self, store: FakeStore, shot_path: Path | None):
        self.store = store
        self.shot_path = shot_path
        self.calls: list[tuple] = []
        self.sources = {1: source(1), 2: source(2, kind="app_title", app="weixin.exe", title="微信", url=None),
                        3: source(3, kind="file", path=None)}

    def control(self):
        return self.store.control()

    def status(self, now_ms=None):
        until = self.store.ctrl.get("paused_until_ms")
        paused = until is not None and until > (now_ms or NOW)
        return {"state": "paused" if paused else "recording", "paused_until_ms": until if paused else None,
                "paused_forever": False, "recorder_alive": True, "last_snap_ms": NOW - 12_000, "now_ms": now_ms,
                "current": None if paused else {"app": "msedge.exe", "title": "示例文章"}, "disk_low": False,
                "agent_enabled": bool(self.store.ctrl["agent_enabled"])}

    def timeline(self, start_ms, end_ms, *, now_ms=None, limit=500):
        self.calls.append(("timeline", start_ms, end_ms, now_ms))
        grey = {"type": "grey", "span_id": 1, "reason": "unlisted", "app": "Code.exe", "start_ms": NOW - 300_000,
                "end_ms": NOW - 200_000, "tier": "never"}
        return {"from_ms": start_ms, "to_ms": end_ms, "items": [entry(), grey], "hours": []}

    def entry(self, vid):
        if vid != 1:
            return None
        return {"entry": entry(), "lines": [{"line_id": 1, "text": "第一行", "via": "uia", "first_seen_ms": NOW,
                                             "last_seen_ms": NOW, "new": True}], "shots": [], "clips": []}

    def source(self, sid, *, offset=0, limit=200):
        self.calls.append(("source", sid, offset, limit))
        s = self.sources.get(sid)
        return None if s is None else {"source": s, "visits": [], "lines": [], "total_lines": 0}

    def search(self, q, *, start_ms=None, end_ms=None, app=None, limit=30, per_result=5, semantic=None):
        self.calls.append(("search", q, start_ms, end_ms, app, limit, semantic))
        hits = [{"line_id": 1, "text": "计算方式是 Q、K、V 三个矩阵。", "via": "uia", "first_seen_ms": NOW,
                 "last_seen_ms": NOW, "semantic": False},
                {"line_id": 2, "text": "The Attention mechanism", "via": "ocr", "first_seen_ms": NOW,
                 "last_seen_ms": NOW, "semantic": False},
                {"line_id": 3, "text": "矩阵乘法", "via": "uia", "first_seen_ms": NOW, "last_seen_ms": NOW,
                 "semantic": True}]
        return {"query": q, "terms": [], "error": None, "total": 1,
                "results": [{"source": source(), "visit_id": 1, "start_ms": NOW - 600_000, "end_ms": NOW,
                             "last_seen_ms": NOW, "match": ["text"], "hits": hits, "more_hits": 0, "shot": None}],
                "copied": []}

    def copied(self, *, q=None, start_ms=None, end_ms=None, limit=100):
        self.calls.append(("copied", q, start_ms, end_ms, limit))
        return [{"clip_id": 1, "t_ms": NOW, "text": "复制的一段", "app": "weixin.exe", "title": "微信",
                 "source": None, "visit_id": None}]

    def shot(self, sid):
        if sid != 1:
            return None
        return {"shot_id": 1, "t_ms": NOW, "kind": "first", "width": 800, "height": 400, "source_id": 1,
                "visit_id": 1, "boxes": [{"line_id": 1, "x": 10, "y": 10, "w": 100, "h": 20, "text": "第一行"}],
                "source": source()}

    def shot_file(self, sid):
        return self.shot_path if sid == 1 else None

    def apps(self):
        return {"apps": [{"process": p, "tier": t, "added_ms": NOW} for p, t in sorted(self.store.tiers.items())],
                "unlisted": [{"app": "Code.exe", "last_ms": NOW, "total_ms": 60_000}], "excludes": list(self.store.excl)}


class FakeAutostart:
    def __init__(self):
        self.enabled = False
        self.cmds: list = []

    def is_enabled(self):
        return self.enabled

    def enable(self, cmd=None):
        self.enabled = True
        self.cmds.append(cmd)

    def disable(self):
        self.enabled = False


class Env:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.cfg = v3config.build({}, data_dir=tmp / "data", home=tmp / "home")
        shot = tmp / "shot.jpg"
        Image.new("RGB", (800, 400), (200, 210, 220)).save(shot, "JPEG", quality=80)
        self.store = FakeStore()
        self.queries = FakeQueries(self.store, shot)
        self.opened: list[Path] = []
        self.envfile = tmp / "dummy.env"
        self.envfile.write_text("LLM_BASE_URL=https://llm.dummy.invalid/v1\nLLM_API_KEY=sk-dummy-SECRET-123\n"
                                "LLM_MODEL=dummy-model\n", encoding="utf-8")
        self.auto = FakeAutostart()
        self.ctx = V3Context(cfg=self.cfg, store=self.store, queries=self.queries, home=tmp / "home",
                             rotate_token=lambda: "sr_session=new; HttpOnly; Path=/",
                             autostart=self.auto, open_in_explorer=self.opened.append, clock=lambda: NOW,
                             llm_env_files=[self.envfile], llm_environ={}, icons=FakeIcons())
        self.app = Starlette(routes=routes(self.ctx))


class FakeIcons:
    """只认得 Edge：名字 + 一张小 PNG；别的软件都找不到 exe。"""
    PNG = b"\x89PNG-fake-edge"

    def running_paths(self):
        return {}

    def info(self, process, running=None):
        return {"name": "Microsoft Edge", "png": self.PNG} if process.lower() == "msedge.exe" else None


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@pytest.fixture
def client(env):
    with TestClient(env.app) as c:
        yield c


def test_status_and_no_store(client):
    r = client.get("/api/v3/status")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    s = r.json()
    assert set(s) >= {"state", "paused_until_ms", "paused_forever", "recorder_alive", "last_snap_ms", "now_ms",
                      "current", "disk_low", "agent_enabled"}
    assert s["now_ms"] == NOW


def test_post_needs_header_and_json_object(client):
    assert client.post("/api/v3/pause", json={"minutes": 15}).status_code == 403
    r = client.post("/api/v3/pause", content=b"{not json", headers=POST_H)
    assert r.status_code == 400 and "JSON" in r.json()["error"]
    assert client.post("/api/v3/pause", content=b"[1]", headers=POST_H).status_code == 400
    for path in ("/resume", "/apps", "/excludes", "/excludes/remove", "/settings", "/delete", "/agent/reset-token",
                 "/sources/3/reveal"):
        assert client.post("/api/v3" + path, json={}).status_code == 403, path


def test_pause_range_and_resume(client, env):
    for bad in (4, 61, 0, "x", True, 15.5, None):
        r = client.post("/api/v3/pause", json={"minutes": bad}, headers=POST_H)
        assert r.status_code == 400, bad
        assert "error" in r.json()
    r = client.post("/api/v3/pause", json={"minutes": 30}, headers=POST_H)
    assert r.status_code == 200
    assert env.store.ctrl["paused_until_ms"] == NOW + 30 * 60_000
    assert r.json()["state"] == "paused"
    r = client.post("/api/v3/resume", headers=POST_H)
    assert env.store.ctrl["paused_until_ms"] is None and r.json()["state"] == "recording"
    for ok in (5, 60):
        assert client.post("/api/v3/pause", json={"minutes": ok}, headers=POST_H).status_code == 200


def test_timeline_day_and_range(client, env):
    r = client.get("/api/v3/timeline", params={"day": "2026-10-28"})
    assert r.status_code == 200
    body = r.json()
    assert [x["type"] for x in body["items"]] == ["entry", "grey"]
    _, lo, hi, now = env.queries.calls[-1]
    assert hi - lo in (23 * 3_600_000, 24 * 3_600_000, 25 * 3_600_000) and now == NOW
    client.get("/api/v3/timeline", params={"from_ms": 100, "to_ms": 200})
    assert env.queries.calls[-1][1:3] == (100, 200)
    assert client.get("/api/v3/timeline").status_code == 200
    assert client.get("/api/v3/timeline", params={"day": "28/10"}).status_code == 400
    assert client.get("/api/v3/timeline", params={"from_ms": 200, "to_ms": 100}).status_code == 400
    assert client.get("/api/v3/timeline", params={"from_ms": 200}).status_code == 400


def test_entry_and_source(client, env):
    r = client.get("/api/v3/entries/1")
    assert r.status_code == 200 and r.json()["entry"]["type"] == "entry" and r.json()["lines"][0]["new"] is True
    assert client.get("/api/v3/entries/9").status_code == 404
    assert client.get("/api/v3/entries/abc").status_code == 400
    r = client.get("/api/v3/sources/2", params={"offset": 10, "limit": 50})
    assert r.status_code == 200 and r.json()["source"]["kind"] == "app_title"
    assert env.queries.calls[-1] == ("source", 2, 10, 50)
    assert client.get("/api/v3/sources/99").status_code == 404
    assert client.get("/api/v3/sources/2", params={"limit": 501}).status_code == 400


def test_search_spans_cjk_and_ascii(client, env):
    assert client.get("/api/v3/search").status_code == 400
    assert client.get("/api/v3/search", params={"q": "   "}).status_code == 400
    assert client.get("/api/v3/search", params={"q": "x" * 501}).status_code == 400
    r = client.get("/api/v3/search", params={"q": "矩阵 attention", "app": "msedge.exe", "limit": 10,
                                             "from_ms": 1, "to_ms": 2})
    assert r.status_code == 200
    hits = r.json()["results"][0]["hits"]
    t0 = hits[0]["text"]
    a, b = hits[0]["spans"][0]
    assert t0[a:b] == "矩阵"
    a, b = hits[1]["spans"][0]
    assert hits[1]["text"][a:b] == "Attention"         # 大小写不敏感，按原文位置
    assert hits[2]["spans"] == [[0, 2]]                 # 语义命中也有字面重叠时照样标
    assert env.queries.calls[-1][1:6] == ("矩阵 attention", 1, 2, "msedge.exe", 10)
    assert client.get("/api/v3/search", params={"q": "a", "limit": 101}).status_code == 400


def test_match_spans_rules():
    assert match_spans("ＡＢＣ abc", ["abc"]) == [[0, 3], [4, 7]]       # 全角折叠
    assert match_spans("示例 词出现", ["示例词"]) == [[0, 4]]              # 空白不敏感
    assert match_spans("rearranged a", [("a", True)]) == [[11, 12]]       # 只认词首
    assert match_spans("行尾是示", ["示例"]) == [[3, 4]]                  # 折到下一行
    assert match_spans("行尾是示", ["示例"], tails=False) == []


def test_copied(client, env):
    r = client.get("/api/v3/copied", params={"q": "一段", "limit": 5})
    assert r.status_code == 200 and r.json()["items"][0]["clip_id"] == 1
    assert env.queries.calls[-1] == ("copied", "一段", None, None, 5)
    client.get("/api/v3/copied")
    assert env.queries.calls[-1] == ("copied", None, None, None, 100)
    assert client.get("/api/v3/copied", params={"limit": 0}).status_code == 400


def test_shot_image_and_thumb(client, env):
    r = client.get("/api/v3/shots/1")
    assert r.status_code == 200 and r.json()["boxes"][0]["text"] == "第一行"
    assert client.get("/api/v3/shots/2").status_code == 404
    r = client.get("/api/v3/shots/1/image")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert "no-store" in r.headers["cache-control"]
    assert client.get("/api/v3/shots/2/image").status_code == 404
    r = client.get("/api/v3/shots/1/thumb")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and "no-store" in r.headers["cache-control"]
    assert Image.open(io.BytesIO(r.content)).size == (240, 120)
    r = client.get("/api/v3/shots/1/thumb", params={"w": 480})
    assert Image.open(io.BytesIO(r.content)).size == (480, 240)
    assert client.get("/api/v3/shots/1/thumb", params={"w": 481}).status_code == 400
    assert len(env.ctx._thumbs) == 2
    # 缓存只在图还在时用：文件被别的进程删掉（托盘删最近、空闲整理删过期）后不再从内存给
    env.queries.shot_path.unlink()
    assert client.get("/api/v3/shots/1/thumb").status_code == 404
    assert client.get("/api/v3/shots/1/image").status_code == 404


def test_thumb_cache_is_bounded(env):
    from screenrecall.v3 import web

    for i in range(web.MAX_THUMB_CACHE + 5):
        env.ctx._thumbs[(i, 240, "")] = b"x"
        while len(env.ctx._thumbs) > web.MAX_THUMB_CACHE:
            env.ctx._thumbs.popitem(last=False)
    with TestClient(env.app) as c:
        assert c.get("/api/v3/shots/1/thumb", params={"w": 100}).status_code == 200
    assert len(env.ctx._thumbs) == web.MAX_THUMB_CACHE
    assert (1, 100, str(env.queries.shot_path)) in env.ctx._thumbs


def test_apps_installed_only_judged_for_default_browsers(client, env):
    env.ctx.installed = lambda p: p.lower() == "msedge.exe"
    client.post("/api/v3/apps", json={"process": "Weixin.exe", "tier": "record"}, headers=POST_H)
    got = {a["process"]: a["installed"] for a in client.get("/api/v3/apps").json()["apps"]}
    assert got["weixin.exe"] is True
    assert got.get("msedge.exe", True) is True and got.get("brave.exe", False) is False


def test_agents_status_and_install(client, env):
    calls = []
    env.ctx.agents = SimpleNamespace(quick_status=lambda: {"claude": bool(calls), "codex": False, "claude_cli": True},
                                     install_all=lambda: calls.append(1) or ["Claude Code：已登记。"])
    assert client.get("/api/v3/agents").json() == {"claude": False, "codex": False, "claude_cli": True}
    assert client.post("/api/v3/agents/install", json={}).status_code == 403
    r = client.post("/api/v3/agents/install", json={}, headers=POST_H).json()
    assert r["claude"] is True and r["messages"] == ["Claude Code：已登记。"]


def test_apps_tiers(client, env):
    r = client.get("/api/v3/apps")
    assert r.status_code == 200 and set(r.json()) == {"apps", "unlisted", "excludes"}
    r = client.post("/api/v3/apps", json={"process": "Code.exe", "tier": "record"}, headers=POST_H)
    assert r.status_code == 200
    assert {"process": "code.exe", "tier": "record", "added_ms": NOW, "installed": True,
            "name": None, "icon": False} in r.json()["apps"]
    for bad in ({"process": "Code.exe", "tier": "sometimes"}, {"process": "", "tier": "record"},
                {"process": "  ", "tier": "never"}, {"process": "x" * 261, "tier": "record"},
                {"process": "C:\\a\\b.exe", "tier": "record"}, {"tier": "record"}, {"process": 3, "tier": "record"},
                {"process": "a.exe"}):
        assert client.post("/api/v3/apps", json=bad, headers=POST_H).status_code == 400, bad
    assert client.post("/api/v3/apps", json={"process": "x" * 260, "tier": "always"}, headers=POST_H).status_code == 200
    # 没带扩展名的进程名永远对不上：补上 .exe
    r = client.post("/api/v3/apps", json={"process": "Notepad", "tier": "record"}, headers=POST_H)
    assert r.status_code == 200 and "notepad.exe" in {a["process"] for a in r.json()["apps"]}
    assert "notepad" not in {a["process"] for a in r.json()["apps"]}


def test_apps_running_marks_tiers(client, env):
    env.ctx.running_apps = lambda: [{"process": "Weixin.exe", "name": "Weixin", "title": "微信", "windows": 1},
                                    {"process": "MSEdge.exe", "name": "Microsoft Edge", "title": "x", "windows": 2}]
    client.post("/api/v3/apps", json={"process": "msedge.exe", "tier": "always"}, headers=POST_H)
    r = client.get("/api/v3/apps/running")
    assert r.status_code == 200
    assert {a["process"]: a["tier"] for a in r.json()["running"]} == {"Weixin.exe": None, "MSEdge.exe": "always"}
    assert {a["process"]: a["icon"] for a in r.json()["running"]} == {"Weixin.exe": False, "MSEdge.exe": True}


def test_app_names_and_icons_come_from_the_exe(client, env):
    client.post("/api/v3/apps", json={"process": "msedge.exe", "tier": "record"}, headers=POST_H)
    edge = next(a for a in client.get("/api/v3/apps").json()["apps"] if a["process"] == "msedge.exe")
    assert edge["name"] == "Microsoft Edge" and edge["icon"] is True
    r = client.get("/api/v3/apps/icon?process=MSEdge.exe")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == FakeIcons.PNG
    assert "no-store" not in r.headers["cache-control"]
    assert client.get("/api/v3/apps/icon?process=Weixin.exe").status_code == 404
    assert client.get("/api/v3/apps/icon").status_code == 400


def test_excludes(client, env):
    r = client.post("/api/v3/excludes", json={"kind": "host", "value": "https://Mail.Example.com/inbox?x=1"},
                    headers=POST_H)
    assert r.status_code == 200 and r.json()["excludes"][0]["value"] == "mail.example.com"
    r = client.post("/api/v3/excludes", json={"kind": "title", "value": "  网银  "}, headers=POST_H)
    assert r.json()["excludes"][1]["value"] == "网银"
    for bad in ({"kind": "app", "value": "x"}, {"kind": "host", "value": ""}, {"kind": "host", "value": "a b"},
                {"kind": "title", "value": "x\ny"}):
        assert client.post("/api/v3/excludes", json=bad, headers=POST_H).status_code == 400, bad
    r = client.post("/api/v3/excludes/remove", json={"exclude_id": 1}, headers=POST_H)
    assert [x["exclude_id"] for x in r.json()["excludes"]] == [2]
    assert client.post("/api/v3/excludes/remove", json={}, headers=POST_H).status_code == 400
    assert client.post("/api/v3/excludes/remove", json={"exclude_id": "a"}, headers=POST_H).status_code == 400


def test_reveal_only_existing_files(client, env, tmp_path):
    assert client.post("/api/v3/sources/1/reveal", headers=POST_H).status_code == 400    # 网址
    assert client.post("/api/v3/sources/2/reveal", headers=POST_H).status_code == 400    # 弱标签
    assert client.post("/api/v3/sources/3/reveal", headers=POST_H).status_code == 400    # 没有路径
    assert client.post("/api/v3/sources/99/reveal", headers=POST_H).status_code == 404
    env.queries.sources[3]["path"] = str(tmp_path / "missing.pdf")
    assert client.post("/api/v3/sources/3/reveal", headers=POST_H).status_code == 404
    env.queries.sources[3]["path"] = "relative.pdf"
    assert client.post("/api/v3/sources/3/reveal", headers=POST_H).status_code == 404
    f = tmp_path / "讲义 第3章.pdf"
    f.write_bytes(b"%PDF-1.4")
    env.queries.sources[3]["path"] = str(f)
    r = client.post("/api/v3/sources/3/reveal", headers=POST_H)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert env.opened == [f]


def test_settings_never_leak_llm_secrets(client, env):
    r = client.get("/api/v3/settings")
    assert r.status_code == 200
    s = r.json()
    assert set(s) == {"numbers", "data_dir", "llm", "agent_enabled", "autostart"}
    assert s["llm"] == {"configured": True, "model": "dummy-model"}
    assert "SECRET" not in r.text and "llm.dummy.invalid" not in r.text
    assert s["numbers"]["still_ms"] == 2000 and s["numbers"]["ocr_min_body_ratio"] == 0.30
    assert "port" not in s["numbers"] and "vectors" not in s["numbers"]
    assert s["data_dir"] == str(env.cfg.data_dir) and s["agent_enabled"] is True and s["autostart"] is False
    env.envfile.write_text("LLM_MODEL=only-model\n", encoding="utf-8")
    assert client.get("/api/v3/settings").json()["llm"] == {"configured": False, "model": "only-model"}


def test_settings_post(client, env):
    r = client.post("/api/v3/settings", json={"agent_enabled": False, "autostart": True}, headers=POST_H)
    assert r.status_code == 200
    assert env.store.ctrl["agent_enabled"] == 0 and r.json()["agent_enabled"] is False
    assert env.auto.enabled and r.json()["autostart"] is True and len(env.auto.cmds) == 1
    r = client.post("/api/v3/settings", json={"autostart": False}, headers=POST_H)
    assert r.json()["autostart"] is False
    for bad in ({"agent_enabled": 1}, {"autostart": "yes"}, {"image_days": 3}):
        assert client.post("/api/v3/settings", json=bad, headers=POST_H).status_code == 400, bad


def test_config_numbers_cover_all_tunables(env):
    nums = config_numbers(env.cfg)
    for k in ("still_ms", "forced_ms", "typing_quiet_ms", "ocr_min_chars", "ocr_min_body_ratio", "fallback_shot_ms",
              "weak_visit_ms", "merge_ms", "away_ms", "search_merge_ms", "image_days", "min_free_gb", "jpeg_quality"):
        assert k in nums


def test_delete(client, env):
    r = client.post("/api/v3/delete", json={"minutes": 15}, headers=POST_H)
    assert r.status_code == 200 and r.json() == {"visits": 1, "lines": 3, "shots": 1}
    assert env.store.deleted[-1] == (NOW - 15 * 60_000, NOW + 1)
    r = client.post("/api/v3/delete", json={"from_ms": 10, "to_ms": 20}, headers=POST_H)
    assert r.status_code == 200 and env.store.deleted[-1] == (10, 20)
    for bad in ({}, {"minutes": 0}, {"minutes": 5, "from_ms": 1}, {"from_ms": 10}, {"from_ms": 20, "to_ms": 10},
                {"app": "x"}):
        assert client.post("/api/v3/delete", json=bad, headers=POST_H).status_code == 400, bad


def test_reset_token_and_unknown(client, env):
    r = client.post("/api/v3/agent/reset-token", headers=POST_H)
    assert r.status_code == 200 and r.json() == {"ok": True} and "sr_session=new" in r.headers["set-cookie"]
    env.ctx.rotate_token = None
    assert client.post("/api/v3/agent/reset-token", headers=POST_H).status_code == 503
    r = client.get("/api/v3/nope")
    assert r.status_code == 404 and r.headers["cache-control"] == "no-store"


def test_internal_error_is_500_without_details(env):
    def boom(*_a, **_k):
        raise RuntimeError("屏幕上的秘密")

    env.queries.status = boom
    with TestClient(env.app, raise_server_exceptions=False) as c:
        r = c.get("/api/v3/status")
    assert r.status_code == 500 and "日志" in r.json()["error"] and "秘密" not in r.text
