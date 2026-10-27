from __future__ import annotations

import pytest

from screenrecall.v2 import config as v2config


def test_defaults_load_and_fill_module_dataclasses(tmp_path, monkeypatch):
    monkeypatch.delenv("SCREENRECALL_DATA_DIR", raising=False)
    cfg = v2config.load_v2(home=tmp_path)
    assert cfg.data_dir == (tmp_path / "data").resolve()
    assert cfg.db_path.name == "recall.sqlite3"
    assert cfg.recorder.wheel_fps == 10.0
    assert cfg.recorder.tracker.still_ms == 1000
    assert cfg.ocr.model == "tiny"
    assert cfg.idle.piece_gap_ms == 120_000
    assert "KeePass.exe" in cfg.default_excluded_processes


def test_override_order(tmp_path, monkeypatch):
    monkeypatch.setenv("SCREENRECALL_DATA_DIR", str(tmp_path / "env"))
    assert v2config.load_v2(home=tmp_path).data_dir == (tmp_path / "env").resolve()
    assert v2config.load_v2(tmp_path / "cli", home=tmp_path).data_dir == (tmp_path / "cli").resolve()


def test_unknown_keys_are_errors(tmp_path, monkeypatch):
    raw = v2config._raw()
    raw["v2"]["ocr"] = {"modle": "tiny"}
    monkeypatch.setattr(v2config, "_raw", lambda: raw)
    with pytest.raises(ValueError, match="modle"):
        v2config.load_v2(home=tmp_path)
