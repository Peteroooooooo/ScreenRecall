"""程序目录与"只有本人能读"的目录权限（v2 主进程）。

默认位置：%LOCALAPPDATA%\\ScreenRecall（app_home），数据放在 app_home()/data。
环境变量 SCREENRECALL_HOME 可以改到别处（测试、便携使用）。

权限做法：用 SDDL 直接写一份"受保护"的 DACL（去掉继承），只留三条：当前用户、SYSTEM、Administrators，
全部是完全控制；目录的三条带 OI/CI，子文件和子目录自动继承。用 SID 而不是账户名，系统语言不影响结果。
写完再把 DACL 读回来核对（verify_private），不对就报错，不假设"命令成功 = 权限正确"。
全部通过 ctypes 调 advapi32，不额外依赖 pywin32。
"""
from __future__ import annotations

import ctypes
import os
import re
import secrets
import sys
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "ScreenRecall"
SYSTEM_SID = "S-1-5-18"
ADMINS_SID = "S-1-5-32-544"


def app_home() -> Path:
    """程序自己的目录：%LOCALAPPDATA%\\ScreenRecall（SCREENRECALL_HOME 优先）。"""
    override = os.environ.get("SCREENRECALL_HOME")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_NAME


def default_data_dir() -> Path:
    """v2 默认数据目录。"""
    return app_home() / "data"


# ---------------- Windows 安全描述符（ctypes） ----------------

class AclError(OSError):
    """设置或核对目录权限失败。"""


if sys.platform == "win32":
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def _fn(dll, name, restype, *argtypes):
        f = getattr(dll, name)
        f.restype, f.argtypes = restype, argtypes
        return f

    _PVOID = ctypes.c_void_p
    _OpenProcessToken = _fn(_advapi32, "OpenProcessToken", wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD,
                            ctypes.POINTER(wintypes.HANDLE))
    _GetTokenInformation = _fn(_advapi32, "GetTokenInformation", wintypes.BOOL, wintypes.HANDLE, ctypes.c_int,
                               _PVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    _ConvertSidToStringSidW = _fn(_advapi32, "ConvertSidToStringSidW", wintypes.BOOL, _PVOID,
                                  ctypes.POINTER(wintypes.LPWSTR))
    _ConvertSddl = _fn(_advapi32, "ConvertStringSecurityDescriptorToSecurityDescriptorW", wintypes.BOOL,
                       wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_PVOID), ctypes.POINTER(wintypes.ULONG))
    _ConvertToSddl = _fn(_advapi32, "ConvertSecurityDescriptorToStringSecurityDescriptorW", wintypes.BOOL,
                         _PVOID, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR),
                         ctypes.POINTER(wintypes.ULONG))
    _GetSecurityDescriptorDacl = _fn(_advapi32, "GetSecurityDescriptorDacl", wintypes.BOOL, _PVOID,
                                     ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(_PVOID),
                                     ctypes.POINTER(wintypes.BOOL))
    _SetNamedSecurityInfoW = _fn(_advapi32, "SetNamedSecurityInfoW", wintypes.DWORD, wintypes.LPWSTR, ctypes.c_int,
                                 wintypes.DWORD, _PVOID, _PVOID, _PVOID, _PVOID)
    _TreeResetNamedSecurityInfoW = _fn(_advapi32, "TreeResetNamedSecurityInfoW", wintypes.DWORD, wintypes.LPWSTR,
                                       ctypes.c_int, wintypes.DWORD, _PVOID, _PVOID, _PVOID, _PVOID, wintypes.BOOL,
                                       _PVOID, ctypes.c_int, _PVOID)
    _GetNamedSecurityInfoW = _fn(_advapi32, "GetNamedSecurityInfoW", wintypes.DWORD, wintypes.LPCWSTR,
                                 ctypes.c_int, wintypes.DWORD, ctypes.POINTER(_PVOID), ctypes.POINTER(_PVOID),
                                 ctypes.POINTER(_PVOID), ctypes.POINTER(_PVOID), ctypes.POINTER(_PVOID))
    _LocalFree = _fn(_kernel32, "LocalFree", _PVOID, _PVOID)
    _GetCurrentProcess = _fn(_kernel32, "GetCurrentProcess", wintypes.HANDLE)
    _CloseHandle = _fn(_kernel32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)

_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_SE_FILE_OBJECT = 1
_SDDL_REVISION_1 = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000
_PROG_INVOKE_NEVER = 1


def current_user_sid() -> str:
    """当前进程用户的 SID 字符串（S-1-5-21-...）。"""
    token = wintypes.HANDLE()
    if not _OpenProcessToken(_GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        need = wintypes.DWORD(0)
        _GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(need))
        buf = ctypes.create_string_buffer(need.value)
        if not _GetTokenInformation(token, _TOKEN_USER, buf, need, ctypes.byref(need)):
            raise ctypes.WinError(ctypes.get_last_error())
        psid = ctypes.c_void_p.from_buffer(buf).value  # TOKEN_USER 第一个字段 SID_AND_ATTRIBUTES.Sid
        out = wintypes.LPWSTR()
        if not _ConvertSidToStringSidW(psid, ctypes.byref(out)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return out.value
        finally:
            _LocalFree(out)
    finally:
        _CloseHandle(token)


def _private_sddl(user_sid: str, is_dir: bool) -> str:
    inherit = "OICI" if is_dir else ""
    return "D:P" + "".join(f"(A;{inherit};FA;;;{sid})" for sid in (user_sid, SYSTEM_SID, ADMINS_SID))


def _set_dacl(path: Path, sddl: str, protected: bool = True, reset_tree: bool = False) -> None:
    """写 DACL。reset_tree=True（目录）时用 TreeResetNamedSecurityInfo：子项上原有的显式 ACE、
    "禁止继承"一并去掉，全部改成只从这个目录继承。只用 SetNamedSecurityInfo 时，子文件自己带的
    Everyone 等显式条目会原样保留（实测），自选的已有数据目录里旧文件仍然谁都能读。"""
    psd = ctypes.c_void_p()
    if not _ConvertSddl(sddl, _SDDL_REVISION_1, ctypes.byref(psd), None):
        raise AclError(f"无法生成权限描述：{ctypes.WinError(ctypes.get_last_error())}")
    try:
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        pdacl = ctypes.c_void_p()
        if not _GetSecurityDescriptorDacl(psd, ctypes.byref(present), ctypes.byref(pdacl), ctypes.byref(defaulted)):
            raise AclError(f"无法读取权限描述：{ctypes.WinError(ctypes.get_last_error())}")
        # SetNamedSecurityInfo 会把可继承的三条 ACE 传播到已有的子文件 / 子目录
        flag = _PROTECTED_DACL_SECURITY_INFORMATION if protected else _UNPROTECTED_DACL_SECURITY_INFORMATION
        if reset_tree:
            err = _TreeResetNamedSecurityInfoW(str(path), _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION | flag,
                                               None, None, pdacl, None, False, None, _PROG_INVOKE_NEVER, None)
        else:
            err = _SetNamedSecurityInfoW(str(path), _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION | flag,
                                         None, None, pdacl, None)
        if err:
            raise AclError(f"设置权限失败：{path}（{ctypes.WinError(err)}）")
    finally:
        _LocalFree(psd)


def read_dacl_sddl(path: str | os.PathLike) -> str:
    """读回 DACL 的 SDDL 字符串（如 D:PAI(A;OICI;FA;;;SY)...）。"""
    psd = ctypes.c_void_p()
    err = _GetNamedSecurityInfoW(str(path), _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION,
                                 None, None, None, None, ctypes.byref(psd))
    if err:
        raise AclError(f"读取权限失败：{path}（{ctypes.WinError(err)}）")
    try:
        out = wintypes.LPWSTR()
        if not _ConvertToSddl(psd, _SDDL_REVISION_1, _DACL_SECURITY_INFORMATION, ctypes.byref(out), None):
            raise AclError(f"无法解析权限：{ctypes.WinError(ctypes.get_last_error())}")
        try:
            return out.value
        finally:
            _LocalFree(out)
    finally:
        _LocalFree(psd)


@dataclass(frozen=True)
class Ace:
    kind: str     # A 允许 / D 拒绝
    flags: str    # OICI、ID（继承来的）等
    rights: str   # FA 完全控制等
    sid: str      # 已展开成 S-1-...


_SID_ALIAS = {"SY": SYSTEM_SID, "BA": ADMINS_SID, "WD": "S-1-1-0", "BU": "S-1-5-32-545",
              "AU": "S-1-5-11", "IU": "S-1-5-4", "CO": "S-1-3-0", "OW": "S-1-3-4"}
_ACE = re.compile(r"\(([^;()]*);([^;()]*);([^;()]*);[^;()]*;[^;()]*;([^;()]*)(?:;[^()]*)?\)")


def parse_dacl(sddl: str) -> tuple[bool, list[Ace]]:
    """(是否受保护 = 不继承上级, ACE 列表)。"""
    m = re.match(r"D:([A-Z]*)", sddl)
    protected = bool(m and "P" in m.group(1))
    aces = [Ace(k, f, r, _SID_ALIAS.get(s, s)) for k, f, r, s in _ACE.findall(sddl)]
    return protected, aces


def verify_private(path: str | os.PathLike, user_sid: str | None = None, *, require_protected: bool = True) -> list[str]:
    """核对 path 只有本人 / SYSTEM / Administrators 能访问。返回问题列表（空 = 合格）。

    require_protected=False 用于私有目录里的子项：从私有上级继承来的权限也算合格。"""
    user_sid = user_sid or current_user_sid()
    allowed = {user_sid, SYSTEM_SID, ADMINS_SID}
    protected, aces = parse_dacl(read_dacl_sddl(path))
    problems = []
    if require_protected and not protected:
        problems.append("仍在继承上级目录的权限")
    for a in aces:
        if a.kind == "A" and a.sid not in allowed:
            problems.append(f"还允许其他账户访问：{a.sid}")
    if not any(a.kind == "A" and a.sid == user_sid and a.rights in ("FA", "GA") for a in aces):
        problems.append("当前用户没有完全控制权限")
    return problems


def make_private(path: str | os.PathLike) -> None:
    """给已存在的目录或文件设上"只有本人能读"的权限，并读回核对。"""
    p = Path(path)
    if sys.platform != "win32":  # pragma: no cover - 本项目只在 Windows 上运行；其他平台退回 POSIX 权限
        os.chmod(p, 0o700 if p.is_dir() else 0o600)
        return
    sid = current_user_sid()
    is_dir = p.is_dir()
    _set_dacl(p, _private_sddl(sid, is_dir), reset_tree=is_dir)
    problems = verify_private(p, sid)
    if problems:
        raise AclError(f"权限核对没通过：{p}：" + "；".join(problems))


def ensure_private_dir(path: str | os.PathLike) -> Path:
    """创建目录（含上级）并设成只有本人能读；已经合格时不重复写。返回 Path。"""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32" and not verify_private(p):
        return p
    make_private(p)
    return p


def write_private_file(path: str | os.PathLike, data: bytes) -> Path:
    """先建同目录的空临时文件、设好权限，再写内容、原子替换：内容（如令牌）从来不会出现在权限未设好的文件里。
    上级目录本身应当已是私有目录（ensure_private_dir），否则别的账户可能在设权限前就打开了这个空文件。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")  # 同进程多线程写也不撞名
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        os.close(fd)
        make_private(tmp)
        with open(tmp, "r+b") as f:
            f.write(data)
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink()
    return p
