"""v2 主进程：程序目录与"只有本人能读"的权限（在临时目录上用真实的 Windows 安全 API 验证）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from screenrecall.app import paths

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL")


def test_app_home_honours_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SCREENRECALL_HOME", str(tmp_path / "h"))
    assert paths.app_home() == tmp_path / "h"
    assert paths.default_data_dir() == tmp_path / "h" / "data"
    monkeypatch.delenv("SCREENRECALL_HOME")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    assert paths.app_home() == tmp_path / "lad" / "ScreenRecall"


def test_ensure_private_dir_sets_protected_user_only_acl(tmp_path):
    d = tmp_path / "a" / "b"
    paths.ensure_private_dir(d)
    protected, aces = paths.parse_dacl(paths.read_dacl_sddl(d))
    assert protected
    sids = {a.sid for a in aces if a.kind == "A"}
    assert sids == {paths.current_user_sid(), paths.SYSTEM_SID, paths.ADMINS_SID}
    assert all("OI" in a.flags and "CI" in a.flags for a in aces)  # 子项会继承
    assert paths.verify_private(d) == []


def test_existing_broad_acl_is_replaced_and_children_inherit(tmp_path):
    d = tmp_path / "open"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "f.txt").write_text("x")
    paths._set_dacl(d, "D:P(A;OICI;FA;;;WD)(A;OICI;FA;;;BU)(A;OICI;FA;;;%s)" % paths.current_user_sid())
    problems = paths.verify_private(d)
    assert any("S-1-1-0" in p for p in problems) and any("S-1-5-32-545" in p for p in problems)
    paths.ensure_private_dir(d)
    assert paths.verify_private(d) == []
    child = paths.verify_private(d / "sub" / "f.txt", require_protected=False)
    assert child == []  # Everyone / Users 的继承项已经从子文件上去掉


def test_children_with_explicit_or_protected_broad_acl_are_reset(tmp_path):
    """自选的已有数据目录：子文件自己带 Everyone 显式条目或禁止继承时，也必须改成只从私有目录继承。"""
    d = tmp_path / "custom"
    (d / "sub" / "deep").mkdir(parents=True)
    explicit, prot, deep = d / "sub" / "explicit.txt", d / "sub" / "protected.txt", d / "sub" / "deep" / "f.bin"
    for f in (explicit, prot, deep):
        f.write_bytes(b"x")
    sid = paths.current_user_sid()
    paths._set_dacl(explicit, "D:(A;;FA;;;WD)", protected=False)
    paths._set_dacl(prot, f"D:P(A;;FA;;;WD)(A;;FA;;;{sid})")
    paths._set_dacl(d / "sub" / "deep", f"D:P(A;OICI;FA;;;BU)(A;OICI;FA;;;{sid})")
    assert paths.verify_private(explicit, require_protected=False)  # 前提：确实是公开的
    paths.ensure_private_dir(d)
    for f in (explicit, prot, d / "sub" / "deep", deep):
        assert paths.verify_private(f, require_protected=False) == [], f
        assert not paths.parse_dacl(paths.read_dacl_sddl(f))[0], f  # 不再"禁止继承"


def test_unprotected_dir_is_reported(tmp_path):
    d = tmp_path / "inherit"
    d.mkdir()
    paths._set_dacl(d, "D:(A;OICI;FA;;;%s)" % paths.current_user_sid(), protected=False)
    assert any("继承" in p for p in paths.verify_private(d))


def test_write_private_file_is_atomic_and_private(tmp_path):
    paths.ensure_private_dir(tmp_path / "h")
    p = paths.write_private_file(tmp_path / "h" / "endpoint.json", b"one")
    paths.write_private_file(p, b"two")
    assert p.read_bytes() == b"two"
    assert paths.verify_private(p) == []
    assert [x.name for x in p.parent.iterdir()] == ["endpoint.json"]  # 没有留下临时文件


def test_write_private_file_sets_acl_before_content(tmp_path, monkeypatch):
    """令牌写进文件时，文件的权限必须已经是私有的。"""
    seen = []
    real_make_private = paths.make_private

    def spy(p):
        seen.append(Path(p).read_bytes())  # 设权限那一刻文件里有什么
        real_make_private(p)

    paths.ensure_private_dir(tmp_path / "h")
    monkeypatch.setattr(paths, "make_private", spy)
    paths.write_private_file(tmp_path / "h" / "endpoint.json", b"secret-token")
    assert seen == [b""]


def test_parse_dacl_expands_aliases():
    protected, aces = paths.parse_dacl("D:PAI(A;OICI;FA;;;SY)(A;OICIID;0x1200a9;;;BU)(D;;FA;;;WD)")
    assert protected
    assert [(a.kind, a.sid) for a in aces] == [("A", "S-1-5-18"), ("A", "S-1-5-32-545"), ("D", "S-1-1-0")]
