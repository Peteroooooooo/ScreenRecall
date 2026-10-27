"""Load the synthetic search eval set (eval_set.json) and expand its generated filler segments.

Everything here is synthetic. Generated filler (server log lines, spreadsheet rows, file lists) is deterministic
and never labelled; it is there to make the corpus look like a real screen history (many near-identical short
lines) so that hub-like vectors and score calibration get tested.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVAL_SET = HERE / "eval_set.json"


@dataclass
class Segment:
    key: str
    app: str
    title: str
    age_h: float
    blocks: list[list[str]]
    labelled: bool = True

    def lines(self) -> list[str]:
        return [x for b in self.blocks for x in b]


@dataclass
class Query:
    q: str
    kind: str
    rel: list[str] = field(default_factory=list)


def _gen(kind: str, n: int, seed: int) -> list[str]:
    rnd = random.Random(seed)
    out: list[str] = []
    if kind == "log":
        paths = ["/api/orders", "/api/users/me", "/health", "/api/search", "/static/app.js", "/api/cart"]
        for i in range(n):
            lvl = rnd.choice(["INFO", "INFO", "INFO", "DEBUG", "WARN"])
            out.append(f"2026-10-26 14:{i % 60:02d}:{rnd.randrange(60):02d} {lvl} GET {rnd.choice(paths)} "
                       f"{rnd.choice([200, 200, 200, 304, 404])} {rnd.randrange(3, 180)} ms id={rnd.randrange(16**6):06x}")
    elif kind == "sheet":
        items = ["Widget", "Gadget", "Bracket", "Hinge", "Spring", "Valve", "Gasket", "Bolt"]
        colours = ["blue", "red", "steel", "black", "white"]
        for i in range(n):
            out.append(f"{1000 + i}  {rnd.choice(items)} {rnd.choice(colours)}  {rnd.randrange(1, 40)}  "
                       f"{rnd.randrange(100, 9000) / 100:.2f}")
    elif kind == "files":
        names = ["report", "invoice", "photo", "setup", "notes", "scan", "export", "draft"]
        exts = ["pdf", "zip", "png", "exe", "docx", "csv"]
        for i in range(n):
            out.append(f"{rnd.choice(names)}_{rnd.randrange(100, 999)}.{rnd.choice(exts)}    "
                       f"2026/10/{rnd.randrange(1, 27):02d}    {rnd.randrange(10, 9000)} KB")
    else:
        raise ValueError(kind)
    return out


def load(path: Path = EVAL_SET) -> tuple[list[Segment], list[Query]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    segs: list[Segment] = []
    for i, s in enumerate(raw["segments"]):
        if "gen" in s:
            segs.append(Segment(s["key"], s["app"], s["title"], float(s["age_h"]), [_gen(s["gen"], int(s["n"]), i)],
                                labelled=False))
        else:
            segs.append(Segment(s["key"], s["app"], s["title"], float(s["age_h"]), [list(b) for b in s["blocks"]]))
    queries = [Query(q["q"], q["kind"], list(q["rel"])) for q in raw["queries"]]
    keys = {s.key: s for s in segs}
    for q in queries:  # labels must point at real lines
        for r in q.rel:
            k, n = r.split(":")
            assert k in keys and 0 <= int(n) < len(keys[k].lines()), r
    return segs, queries


def stats() -> dict:
    segs, queries = load()
    kinds: dict[str, int] = {}
    for q in queries:
        kinds[q.kind] = kinds.get(q.kind, 0) + 1
    return {"segments": len(segs), "lines": sum(len(s.lines()) for s in segs), "queries": len(queries), "kinds": kinds}


if __name__ == "__main__":
    print(stats())
