"""配置加载：数据目录自带的 config.toml 只影响这一份数据（独立测试库用）。"""

from screenrecall.config import load_config


def test_data_dir_config_overrides_only_that_data_dir(tmp_path):
    test_dir, real_dir = tmp_path / "data-lifecycle-test", tmp_path / "data"
    test_dir.mkdir()
    real_dir.mkdir()
    (test_dir / "config.toml").write_text(
        '[storage]\ndata_dir = "elsewhere"\n[retention]\nquota_gib = 0.05\nimage_days = 1\n', encoding="utf-8")
    t, r = load_config(test_dir), load_config(real_dir)
    assert t.data_dir == test_dir.resolve()  # [storage] 在这里被忽略，不能把数据目录改走
    assert t.retention.quota_bytes == int(0.05 * 1024**3) and t.retention.image_days == 1
    assert r.retention.quota_bytes == 10 * 1024**3 and r.retention.image_days == 30


def test_db_info_cli_reports_version(h, capsys):
    from screenrecall import db

    assert db.main(["--data-dir", str(h.cfg.data_dir)]) == 0
    assert f"结构版本 v{db.SCHEMA_VERSION}" in capsys.readouterr().out
