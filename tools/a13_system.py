"""A13 用的系统开关：显示缩放与 Wi-Fi。python tools/a13_system.py <命令>

  status                 打印主显示器当前 / 推荐缩放、Wi-Fi 状态（只读）
  set-scale <百分比>      把主显示器缩放改成 100 / 125 / 150 …（立即生效，不用注销）
  restore <state.json>   按 a13_check 开始时保存的状态恢复缩放并重连 Wi-Fi
  wifi-off / wifi-on <配置名>

缩放用的是 Windows 设置页自己调用的 DisplayConfigGet/SetDeviceInfo（类型 -3 / -4，未公开但 Win10 1607 起一直可用，
不需要管理员）。数值是相对“推荐缩放”的档位差：档位表 100,125,150,175,200,…。
Wi-Fi 用 netsh wlan disconnect / connect，也不需要管理员。
"""
from __future__ import annotations

import ctypes
import json
import re
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

DPI_VALUES = [100, 125, 150, 175, 200, 225, 250, 300, 350, 400, 450, 500]
QDC_ONLY_ACTIVE_PATHS = 2
GET_SOURCE_NAME, GET_DPI_SCALE, SET_DPI_SCALE = 1, -3, -4


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _PathSource(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                ("statusFlags", ctypes.c_uint32)]


class _PathTarget(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                ("outputTechnology", ctypes.c_uint32), ("rotation", ctypes.c_uint32), ("scaling", ctypes.c_uint32),
                ("refreshNum", ctypes.c_uint32), ("refreshDen", ctypes.c_uint32), ("scanLineOrdering", ctypes.c_uint32),
                ("targetAvailable", wintypes.BOOL), ("statusFlags", ctypes.c_uint32)]


class _PathInfo(ctypes.Structure):
    _fields_ = [("sourceInfo", _PathSource), ("targetInfo", _PathTarget), ("flags", ctypes.c_uint32)]


class _ModeInfo(ctypes.Structure):
    _fields_ = [("infoType", ctypes.c_uint32), ("id", ctypes.c_uint32), ("adapterId", LUID),
                ("data", ctypes.c_byte * 48)]


class _Header(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int32), ("size", ctypes.c_uint32), ("adapterId", LUID), ("id", ctypes.c_uint32)]


class _DpiGet(ctypes.Structure):
    _fields_ = [("header", _Header), ("minScaleRel", ctypes.c_int32), ("curScaleRel", ctypes.c_int32),
                ("maxScaleRel", ctypes.c_int32)]


class _DpiSet(ctypes.Structure):
    _fields_ = [("header", _Header), ("scaleRel", ctypes.c_int32)]


class _SourceName(ctypes.Structure):
    _fields_ = [("header", _Header), ("viewGdiDeviceName", ctypes.c_wchar * 32)]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_shcore = ctypes.WinDLL("shcore")
_user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
_user32.MonitorFromPoint.restype = ctypes.c_void_p  # 句柄是 64 位，默认 int 返回值会截断


def set_dpi_aware() -> None:
    """本进程按显示器 DPI 感知，读到的坐标和 DPI 都是物理值。"""
    try:
        _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass


def _paths() -> list[tuple[_PathInfo, tuple[int, int] | None]]:
    n_paths, n_modes = ctypes.c_uint32(0), ctypes.c_uint32(0)
    if _user32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), ctypes.byref(n_modes)):
        raise OSError("GetDisplayConfigBufferSizes 失败")
    paths = (_PathInfo * n_paths.value)()
    modes = (_ModeInfo * n_modes.value)()
    rc = _user32.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), paths, ctypes.byref(n_modes), modes,
                                    None)
    if rc:
        raise OSError(f"QueryDisplayConfig 失败：{rc}")
    out = []
    for p in paths[: n_paths.value]:
        pos = None
        i = p.sourceInfo.modeInfoIdx
        if i < n_modes.value and modes[i].infoType == 1:  # DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE
            w, h, _fmt, x, y = (int.from_bytes(bytes(modes[i].data[k:k + 4]), "little", signed=k >= 12)
                                for k in (0, 4, 8, 12, 16))
            pos = (x, y)
        out.append((p, pos))
    return out


def _header(t: int, size: int, src: _PathSource) -> _Header:
    return _Header(type=t, size=size, adapterId=src.adapterId, id=src.id)


def displays() -> list[dict]:
    """所有活动显示器：GDI 名、是否主显示器（源位置在 0,0）、当前 / 推荐缩放。"""
    out = []
    for p, pos in _paths():
        src = p.sourceInfo
        name = _SourceName(header=_header(GET_SOURCE_NAME, ctypes.sizeof(_SourceName), src))
        _user32.DisplayConfigGetDeviceInfo(ctypes.byref(name))
        g = _DpiGet(header=_header(GET_DPI_SCALE, ctypes.sizeof(_DpiGet), src))
        rc = _user32.DisplayConfigGetDeviceInfo(ctypes.byref(g))
        if rc:
            raise OSError(f"读取缩放失败：{rc}")
        rec = abs(g.minScaleRel)
        cur = rec + g.curScaleRel
        out.append({"device": name.viewGdiDeviceName, "primary": pos == (0, 0),
                    "current": DPI_VALUES[cur] if 0 <= cur < len(DPI_VALUES) else None,
                    "recommended": DPI_VALUES[rec] if rec < len(DPI_VALUES) else None,
                    "allowed": [DPI_VALUES[rec + r] for r in range(g.minScaleRel, g.maxScaleRel + 1)
                                if 0 <= rec + r < len(DPI_VALUES)],
                    "_src": src, "_rec": rec})
    return out


def primary() -> dict:
    ds = displays()
    return next((d for d in ds if d["primary"]), ds[0])


def effective_dpi() -> int:
    """主显示器当前的有效 DPI（96 = 100%）。"""
    mon = _user32.MonitorFromPoint(wintypes.POINT(0, 0), 1)  # MONITOR_DEFAULTTOPRIMARY
    x, y = ctypes.c_uint(0), ctypes.c_uint(0)
    _shcore.GetDpiForMonitor(ctypes.c_void_p(mon), 0, ctypes.byref(x), ctypes.byref(y))
    return int(x.value)


def set_scale(percent: int, timeout: float = 20.0) -> None:
    """改主显示器缩放并等到系统确实切换完（有效 DPI 变成 96 × 百分比）。"""
    d = primary()
    if percent not in d["allowed"]:
        raise ValueError(f"这台显示器只允许 {d['allowed']}，不能设成 {percent}%")
    s = _DpiSet(header=_header(SET_DPI_SCALE, ctypes.sizeof(_DpiSet), d["_src"]),
                scaleRel=DPI_VALUES.index(percent) - d["_rec"])
    rc = _user32.DisplayConfigSetDeviceInfo(ctypes.byref(s))
    if rc:
        raise OSError(f"设置缩放失败：{rc}")
    want = round(96 * percent / 100)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if effective_dpi() == want and primary()["current"] == percent:
            return
        time.sleep(0.3)
    raise TimeoutError(f"等了 {timeout:g} 秒，缩放仍不是 {percent}%（有效 DPI {effective_dpi()}）")


# ---------------- Wi-Fi ----------------

def _netsh(*args: str) -> str:
    r = subprocess.run(["netsh", "wlan", *args], capture_output=True)
    raw = r.stdout + r.stderr
    try:  # 输出编码跟着控制台代码页走：65001 时是 UTF-8，否则是 OEM（中文系统为 GBK）
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("oem", errors="replace")


def wifi_status() -> dict:
    """{'present', 'connected', 'profile'}；同时认英文和中文的 netsh 输出。"""
    out = _netsh("show", "interfaces")
    kv = {}
    for line in out.splitlines():
        m = re.match(r"\s*([^:]+?)\s*:\s*(.*?)\s*$", line)
        if m and m.group(1) not in kv:
            kv[m.group(1)] = m.group(2)
    state = kv.get("State") or kv.get("状态") or ""
    return {"present": bool(state), "connected": state.lower() in ("connected", "已连接"),
            "profile": kv.get("Profile") or kv.get("配置文件") or ""}


def wifi_off() -> None:
    _netsh("disconnect")


def wifi_on(profile: str) -> None:
    if profile:
        _netsh("connect", f"name={profile}")


def internet_reachable(timeout: float = 3.0) -> bool:
    """能否连到外网（1.1.1.1:443 或 8.8.8.8:53 任一 TCP 连通即算）。"""
    import socket
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53)):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def restore(state_file: Path) -> None:
    st = json.loads(state_file.read_text(encoding="utf-8"))
    if st.get("scale") and primary()["current"] != st["scale"]:
        set_scale(int(st["scale"]))
        print(f"缩放已恢复为 {st['scale']}%")
    if st.get("wifi_profile") and not wifi_status()["connected"]:
        wifi_on(st["wifi_profile"])
        print("已请求重连 Wi-Fi")


def main(argv: list[str]) -> int:
    set_dpi_aware()
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        for d in displays():
            print({k: v for k, v in d.items() if not k.startswith("_")})
        print("有效 DPI（主显示器）:", effective_dpi())
        w = wifi_status()
        print("Wi-Fi:", "已连接" if w["connected"] else ("未连接" if w["present"] else "没有无线网卡"))
        print("外网可达:", internet_reachable())
    elif cmd == "set-scale":
        set_scale(int(argv[1]))
        print(f"已切到 {argv[1]}%，有效 DPI {effective_dpi()}")
    elif cmd == "restore":
        restore(Path(argv[1]))
    elif cmd == "wifi-off":
        wifi_off()
    elif cmd == "wifi-on":
        wifi_on(argv[1])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
