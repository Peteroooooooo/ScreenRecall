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


def test_default_toml_documents_every_v2_setting_with_its_default():
    """config.default.toml 的 [v2.*] 各节写全了对应 dataclass 的键，值和代码里的默认值一致（改一边忘了另一边会报）。"""
    import dataclasses
    import tomllib

    from screenrecall.config import REPO_ROOT

    raw = tomllib.loads((REPO_ROOT / "config.default.toml").read_text(encoding="utf-8"))["v2"]
    for name, cls in (("app", v2config.AppSettings), ("ocr", v2config.OcrSettings),
                      ("encoder", v2config.EncoderSettings), ("idle", v2config.IdleSettings)):
        sec = raw.get(name, {})
        for f in dataclasses.fields(cls):
            assert f.name in sec, f"[v2.{name}] 缺 {f.name}"
            assert sec[f.name] == f.default, f"[v2.{name}] {f.name} = {sec[f.name]!r}，代码默认 {f.default!r}"


def test_app_switches_config_and_flags():
    from types import SimpleNamespace as NS

    from screenrecall.app.main import app_switches

    cfg = NS(app=v2config.AppSettings())
    flags = NS(no_tray=False, no_browser=False, autostart=False)
    assert app_switches(cfg, flags) == (True, True)
    assert app_switches(NS(app=v2config.AppSettings(open_browser=False, tray=False)), flags) == (False, False)
    assert app_switches(cfg, NS(no_tray=True, no_browser=False, autostart=True)) == (False, False)
    assert app_switches(None, flags) == (True, True)
