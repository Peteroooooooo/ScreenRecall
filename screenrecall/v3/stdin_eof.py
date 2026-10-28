"""子进程看 stdin 的 EOF：主进程关掉管道 = 让我们收尾退出（录制进程、空闲整理进程共用）。

Windows 上不能在一个线程里对 stdin 管道挂着同步读（ReadFile）等 EOF：读挂着的时候，别的线程加载带 C 运行库的
DLL（numpy、scipy、torch……CRT 初始化时会对标准句柄调 GetFileType）会一直卡住，直到那次读返回——
空闲整理进程就这样卡死在第一次导入 CLIP 依赖上。所以 stdin 是管道时不挂读，用 PeekNamedPipe 每 poll_s 秒
看一次：管道断开（ERROR_BROKEN_PIPE）就是对面关了；有数据就读掉（数据已在，读不会挂起）。
stdin 是普通文件时照常读到 EOF（读文件不会挂起）；控制台里手动运行（isatty）不看 stdin。
"""
from __future__ import annotations

import os
import sys
import threading
from typing import Callable

FILE_TYPE_PIPE = 3
POLL_S = 0.5


def _pipe_state(handle: int) -> int | None:
    """管道里现在有多少字节可读；管道已断开（或句柄不能用了）返回 None。"""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    peek = k32.PeekNamedPipe
    peek.restype = wintypes.BOOL
    peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                     ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    avail = wintypes.DWORD(0)
    if not peek(wintypes.HANDLE(handle), None, 0, None, ctypes.byref(avail), None):
        return None  # ERROR_BROKEN_PIPE（对面关了）或别的错误：都当 EOF
    return int(avail.value)


def _file_type(handle: int) -> int:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    gft = k32.GetFileType
    gft.restype = wintypes.DWORD
    gft.argtypes = [wintypes.HANDLE]
    return int(gft(wintypes.HANDLE(handle)))


def watch(on_eof: Callable[[], None], stream=None, *, poll_s: float = POLL_S,
          name: str = "stdin-eof") -> threading.Thread | None:
    """在守护线程里等 stdin 的 EOF，到了就调 on_eof()。stdin 不存在或是控制台时不看，返回 None。"""
    stream = sys.stdin if stream is None else stream
    if stream is None:
        return None
    try:
        if stream.isatty():
            return None
        fd = stream.fileno()
    except (OSError, ValueError, AttributeError):
        return None

    handle = None
    if os.name == "nt":
        import msvcrt

        try:
            handle = msvcrt.get_osfhandle(fd)
        except OSError:
            return None
        if _file_type(handle) != FILE_TYPE_PIPE:
            handle = None

    def poll_pipe() -> None:
        stop = threading.Event()
        while not stop.wait(poll_s):
            n = _pipe_state(handle)
            if n is None:
                break
            if n:
                try:
                    if not os.read(fd, min(n, 65536)):
                        break
                except OSError:
                    break

    def read_file() -> None:
        try:
            while os.read(fd, 4096):
                pass
        except OSError:
            pass

    def run() -> None:
        (poll_pipe if handle is not None else read_file)()
        on_eof()

    th = threading.Thread(target=run, name=name, daemon=True)
    th.start()
    return th
