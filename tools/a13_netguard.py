"""在“没有网络”的条件下运行一个模块：python tools/a13_netguard.py <日志.jsonl> -m <模块> [参数...]

本进程里所有连向非本机地址的 socket 连接和对外域名解析都会失败（像拔了网线），每次尝试记一行到日志，
本机 127.0.0.1 / localhost 不受影响（页面和后台之间、浏览器访问页面都走本机）。
用来证明 A13 的“断网后仍可用”：日志里零次对外尝试 = 程序根本没想联网；有尝试但功能照常 = 联网失败被正确兜住。
真断 Wi-Fi 由 a13_check --wifi-off 另外做；这一层不依赖网卡状态，结论可重复。
"""
import ipaddress
import json
import os
import runpy
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

if len(sys.argv) < 4 or sys.argv[2] != "-m":
    sys.exit(__doc__)
LOG, MOD = sys.argv[1], sys.argv[3]
sys.argv = [MOD] + sys.argv[4:]
sys.path.insert(0, os.getcwd())  # 和 python -m 一样从当前目录找包（在 ScreenRecall 目录下运行）
_lock = threading.Lock()


def _log(kind: str, target) -> None:
    # 记下是谁发起的（调用栈里最靠近的十几层，去掉本文件），方便定位是哪个库想联网
    skip = ("httpx", "httpcore", "urllib3", "requests", "socket.py", "ssl.py", "anyio", Path(__file__).name)
    stack = [f"{Path(fr.filename).parent.name}/{Path(fr.filename).name}:{fr.lineno} {fr.name}"
             for fr in traceback.extract_stack()[:-2] if not any(s in fr.filename for s in skip)][-12:]
    row = {"t": round(time.time(), 3), "module": MOD, "kind": kind, "target": str(target)}
    if kind != "start":
        row["stack"] = stack
    with _lock, open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _is_local(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).split("%")[0].strip("[]")
    if host.lower() in ("", "localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check(addr) -> None:
    if isinstance(addr, tuple) and addr and not _is_local(addr[0]):
        _log("connect", f"{addr[0]}:{addr[1] if len(addr) > 1 else ''}")
        raise OSError(10051, "a13 netguard: network is unreachable")


_connect, _connect_ex = socket.socket.connect, socket.socket.connect_ex
_getaddrinfo = socket.getaddrinfo


def connect(self, addr):
    _check(addr)
    return _connect(self, addr)


def connect_ex(self, addr):
    _check(addr)
    return _connect_ex(self, addr)


def getaddrinfo(host, *args, **kwargs):
    if not _is_local(host):
        try:
            ipaddress.ip_address(str(host.decode() if isinstance(host, bytes) else host).strip("[]"))
        except ValueError:  # 域名：像没有 DNS 一样失败；IP 字面量交给 connect 去拦
            _log("dns", host)
            raise socket.gaierror(socket.EAI_NONAME, "a13 netguard: name resolution unavailable") from None
    return _getaddrinfo(host, *args, **kwargs)


socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
socket.getaddrinfo = getaddrinfo
_log("start", MOD)
runpy.run_module(MOD, run_name="__main__", alter_sys=True)
