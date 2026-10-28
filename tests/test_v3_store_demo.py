"""v3 演示数据：方案"实际跑一遍"到 14:15 库里该有的东西（19 行字、2 张截图、时间线 4 项、复制 1 段）。"""

import sqlite3
from datetime import date, datetime

import pytest
from PIL import Image

from screenrecall.v3 import config, demo
from screenrecall.v3.store import Queries

DAY = date(2026, 10, 28)


def ms(h, m, s=0):
    return int(datetime(DAY.year, DAY.month, DAY.day, h, m, s).timestamp() * 1000)


@pytest.fixture
def demo_q(tmp_path):
    data = tmp_path / "demo"
    assert demo.main(["--data-dir", str(data), "--day", DAY.isoformat()]) == 0
    cfg = config.build({}, data_dir=data, home=tmp_path)
    q = Queries(cfg, clock=lambda: ms(14, 20))
    yield cfg, q
    q.close()


def test_demo_counts_match_spec(demo_q):
    cfg, q = demo_q
    with sqlite3.connect(cfg.db_path) as c:
        n = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in ("lines", "shots", "snaps", "clips", "visits", "grey_spans", "sources")}
        noshot = c.execute("SELECT COUNT(*) FROM snaps WHERE shot_id IS NULL").fetchone()[0]
    assert n == {"lines": 19, "shots": 2, "snaps": 6, "clips": 1, "visits": 3, "grey_spans": 1, "sources": 2}
    assert noshot == 4  # 另外拍过的 4 次都没留图


def test_demo_timeline(demo_q):
    cfg, q = demo_q
    tl = q.timeline(ms(13, 0), ms(15, 0))
    items = tl["items"]
    assert [i["type"] for i in items] == ["entry", "grey", "entry", "entry"]
    web1, grey, chat, web2 = items
    assert web1["name"] == "Transformer 入门" and (web1["start_ms"], web1["end_ms"]) == (ms(14, 0), ms(14, 5))
    assert web1["dwell_ms"] == 5 * 60_000 and web1["new_lines"] == 12 and web1["thumb"]["own"] is True
    assert grey["app"] == "Code.exe" and grey["tier"] == "never" and grey["reason"] == "unlisted"
    assert (grey["start_ms"], grey["end_ms"]) == (ms(14, 5), ms(14, 12))
    assert chat["name"] == "微信" and chat["source"]["strong"] is False and chat["new_lines"] == 7
    assert chat["copied"] == 1 and chat["dwell_ms"] == 3 * 60_000 and chat["thumb"]["own"] is True
    assert chat["reasons"] == ["still", "copy"]
    assert web2["open"] is True and web2["new_lines"] == 0 and web2["thumb"] == {"shot_id": web1["thumb"]["shot_id"],
                                                                                  "own": False}
    assert web2["start_ms"] == ms(14, 15)


def test_demo_shots_boxes_and_search(demo_q):
    cfg, q = demo_q
    r = q.search("局限性")
    assert r["copied"][0]["text"] == demo.COPIED and r["copied"][0]["app"] == "Weixin.exe"
    res = r["results"][0]
    assert res["source"]["app"] == "Weixin.exe" and [h["text"] for h in res["hits"]] == [demo.COPIED]
    box = res["shot"]["boxes"][0]
    img = Image.open(q.shot_file(res["shot"]["shot_id"]))
    assert img.size == (res["shot"]["width"], res["shot"]["height"])
    assert 0 <= box["x"] and box["x"] + box["w"] <= img.width and box["y"] + box["h"] <= img.height
    # 网页：命中第一屏里的行，截图是 14:00 那张，有框；第二屏才出现的行没有框
    r = q.search("Q、K、V")["results"][0]
    assert r["source"]["url"] == demo.WEB and len(r["shot"]["boxes"]) == 1
    r = q.search("正弦和余弦")["results"][0]
    assert r["shot"]["shot_id"] == r["source"]["shot_id"] and r["shot"]["boxes"] == []
    assert {a["process"]: a["tier"] for a in q.apps()["apps"]}["weixin.exe"] == "record"
    assert {a["process"]: a["tier"] for a in q.apps()["apps"]}["zoom.exe"] == "always"
    assert q.status(ms(14, 20))["state"] == "stopped"


def test_demo_refuses_existing_db(demo_q, capsys):
    cfg, q = demo_q
    assert demo.main(["--data-dir", str(cfg.data_dir), "--day", DAY.isoformat()]) == 2
    assert "已经有库文件" in capsys.readouterr().err
    assert demo.main(["--data-dir", str(cfg.data_dir / "x"), "--day", "2026-13-01"]) == 2
