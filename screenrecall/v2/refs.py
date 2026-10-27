"""summaries.refs 的约定（空闲整理进程写，MCP 适配层读）。

refs 是 JSON 列表，元素：
- {"piece": 片段编号, "app": "x.exe"}：用到的片段和它的应用；
- {"app": "x.exe"} / {"apps": [...]}：涉及的应用；
- {"hidden": {"apps": [...], "title_keywords": [...]}}：生成时已经排除的"对 Agent 隐藏"名单。
  当前隐藏名单非空时，MCP 只把 hidden 覆盖了当前名单的天概括给 Agent（v2.adapters._generated_without）。
早期版本写过 {"kind": "piece", "id": 编号, "app": ...}，读的一方也认。
"""
from __future__ import annotations

from typing import Any, Iterable

PIECE = "piece"
HIDDEN = "hidden"


def piece_ref(piece_id: int, app: str | None) -> dict[str, Any]:
    return {PIECE: int(piece_id), "app": app}


def hidden_ref(hidden: dict | None) -> dict[str, Any]:
    """control.hidden_from_agents → 记进 refs 的名单（生成时已排除）。"""
    h = hidden if isinstance(hidden, dict) else {}

    def strs(xs: Iterable) -> list[str]:
        return [x for x in (xs or ()) if isinstance(x, str) and x]

    return {HIDDEN: {"apps": strs(h.get("apps")), "title_keywords": strs(h.get("title_keywords"))}}


def ref_piece_id(r: dict) -> int | None:
    """一条 ref 引用的片段编号（新旧两种写法）；没有返回 None。"""
    for v in (r.get(PIECE), r.get("piece_id"), r.get("id") if r.get("kind") == PIECE else None):
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None
