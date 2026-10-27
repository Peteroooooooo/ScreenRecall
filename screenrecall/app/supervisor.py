"""子进程管理：录制进程、空闲整理进程都由主进程在 Windows 作业对象（Job Object）里启动。

关键保证：主进程无论怎么退出（包括被任务管理器强制结束），子进程都会被系统一起结束——
每个子进程一个作业对象，设了 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE，作业句柄只在主进程手里，
主进程一没，句柄关闭，作业里的进程（含子进程再开的进程，如 venv 的 python.exe 转启动的真解释器）全部结束。
这样录制进程不可能在没有托盘、没法暂停的情况下继续录。

启动时先挂起（CREATE_SUSPENDED），放进作业后再恢复，中间没有"子进程已经在跑、但还不在作业里"的空档。

停止约定：先关闭子进程的标准输入（子进程读到 EOF 就该自己收尾退出），等 stop_grace_s 秒，还没退就结束整个作业。
意外退出时按 restart 策略重启，退避时间从 backoff_initial_s 起每次翻倍，最多 backoff_max_s；
连续运行超过 stable_s 秒后退避时间复位。
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

log = logging.getLogger("screenrecall.app.supervisor")

Restart = Literal["always", "on-failure", "never"]

_CREATE_SUSPENDED = 0x00000004
_CREATE_NO_WINDOW = 0x08000000
_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
_JOB_OBJECT_LIMIT_PRIORITY_CLASS = 0x00000020
_JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400  # 崩溃时不弹 WER 对话框卡住，直接退出好重启
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_SUSPEND_RESUME = 0x0800
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")

    def _fn(dll, name, restype, *argtypes):
        f = getattr(dll, name)
        f.restype, f.argtypes = restype, argtypes
        return f

    _CreateJobObjectW = _fn(_kernel32, "CreateJobObjectW", wintypes.HANDLE, ctypes.c_void_p, wintypes.LPCWSTR)
    _SetInformationJobObject = _fn(_kernel32, "SetInformationJobObject", wintypes.BOOL, wintypes.HANDLE, ctypes.c_int,
                                   ctypes.c_void_p, wintypes.DWORD)
    _AssignProcessToJobObject = _fn(_kernel32, "AssignProcessToJobObject", wintypes.BOOL, wintypes.HANDLE,
                                    wintypes.HANDLE)
    _TerminateJobObject = _fn(_kernel32, "TerminateJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
    _OpenProcess = _fn(_kernel32, "OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _CloseHandle = _fn(_kernel32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
    _NtResumeProcess = _fn(_ntdll, "NtResumeProcess", ctypes.c_long, wintypes.HANDLE)


class Job:
    """一个带 KILL_ON_JOB_CLOSE 的作业对象。close() 或本进程退出都会结束作业里的所有进程。"""

    def __init__(self, below_normal: bool = False):
        h = _CreateJobObjectW(None, None)
        if not h:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = h
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        if below_normal:  # 作业层面再限定一次：子进程再开的进程也不会高于"低于正常"
            flags |= _JOB_OBJECT_LIMIT_PRIORITY_CLASS
            info.BasicLimitInformation.PriorityClass = _BELOW_NORMAL_PRIORITY_CLASS
        info.BasicLimitInformation.LimitFlags = flags
        if not _SetInformationJobObject(h, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)):
            err = ctypes.get_last_error()
            _CloseHandle(h)
            raise ctypes.WinError(err)

    def assign_and_resume(self, pid: int) -> None:
        hp = _OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_SUSPEND_RESUME
                          | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not hp:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not _AssignProcessToJobObject(self.handle, hp):
                raise ctypes.WinError(ctypes.get_last_error())
            status = _NtResumeProcess(hp)
            if status < 0:
                raise OSError(f"NtResumeProcess 失败：0x{status & 0xFFFFFFFF:08X}")
        finally:
            _CloseHandle(hp)

    def terminate(self, code: int = 1) -> None:
        if self.handle:
            _TerminateJobObject(self.handle, code)

    def close(self) -> None:
        if self.handle:
            _CloseHandle(self.handle)  # KILL_ON_JOB_CLOSE：最后一个句柄关闭时结束作业里剩下的进程
            self.handle = None


@dataclass
class ChildSpec:
    """一个子进程。argv 是完整命令（参数数组，不经过 shell）。autostart=False 的（如空闲整理进程）
    要由 Supervisor.start_child 按需启动；它做完自己正常退出（代码 0）时，on-failure 策略不会重启。"""
    name: str
    argv: list[str]
    restart: Restart = "always"
    below_normal: bool = False
    autostart: bool = True
    cwd: str | None = None
    env: dict[str, str] | None = None
    log_path: Path | None = None
    backoff_initial_s: float = 1.0
    backoff_max_s: float = 60.0
    stable_s: float = 30.0
    stop_grace_s: float = 3.0


@dataclass
class _Child:
    spec: ChildSpec
    proc: subprocess.Popen | None = None
    job: Job | None = None
    state: str = "stopped"          # running / backoff / stopped / exited / failed
    started_at: float = 0.0
    restarts: int = 0
    backoff_s: float = 0.0
    next_start_at: float = 0.0
    last_exit: int | None = None
    wanted: bool = False            # 应该在运行（stop_child 后为 False，不再重启）
    lock: threading.Lock = field(default_factory=threading.Lock)


class Supervisor:
    """启动、看护、停止一组子进程。on_event(name, event, info) 在状态变化时调用（started / exited / restart_scheduled
    / start_failed / stopped），回调在看护线程里执行。"""

    def __init__(self, specs: list[ChildSpec], *, on_event: Callable[[str, str, dict], None] | None = None,
                 poll_s: float = 0.25, clock: Callable[[], float] = time.monotonic):
        names = [s.name for s in specs]
        if len(set(names)) != len(names):
            raise ValueError("子进程名字重复")
        self._children = {s.name: _Child(s, backoff_s=s.backoff_initial_s) for s in specs}
        self._on_event, self._poll_s, self._clock = on_event, poll_s, clock
        self._stop = threading.Event()
        self._closed = False            # stop_all 之后不再启动任何子进程（关机途中空闲回调来得晚也不会再拉起）
        self._thread: threading.Thread | None = None

    # ---------- 对外 ----------

    def start(self) -> "Supervisor":
        """启动所有 autostart 的子进程和看护线程。"""
        for c in self._children.values():
            if c.spec.autostart:
                self.start_child(c.spec.name)
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="screenrecall-supervisor", daemon=True)
            self._thread.start()
        return self

    def start_child(self, name: str) -> bool:
        """按需启动一个子进程（已在运行时什么都不做）。返回是否在运行。"""
        c = self._children[name]
        with c.lock:
            if self._closed:
                return False
            c.wanted = True
            if c.proc is not None and c.proc.poll() is None:
                return True
            c.backoff_s = c.spec.backoff_initial_s
            return self._spawn(c)

    def stop_child(self, name: str, grace_s: float | None = None) -> int | None:
        """停止一个子进程：关标准输入 → 等 grace → 结束整个作业。返回退出码。"""
        c = self._children[name]
        with c.lock:
            c.wanted = False
            code = self._halt(c, c.spec.stop_grace_s if grace_s is None else grace_s)
            c.state = "stopped"
        self._emit(name, "stopped", {"code": code})
        return code

    def stop_all(self, grace_s: float | None = None) -> None:
        """停掉全部子进程并关闭看护（之后 start_child 返回 False）。"""
        self._closed = True
        for c in self._children.values():
            with c.lock:  # 等正在进行的 start_child 做完（它启动的进程下面会被停掉）
                pass
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None
        # 先同时关掉所有子进程的标准输入，让它们并行收尾，再逐个等待
        for c in self._children.values():
            c.wanted = False
            self._close_stdin(c)
        for name in self._children:
            self.stop_child(name, grace_s)

    def status(self) -> dict[str, dict]:
        out = {}
        for name, c in self._children.items():
            alive = c.proc is not None and c.proc.poll() is None
            out[name] = {"state": c.state, "pid": c.proc.pid if alive else None, "restarts": c.restarts,
                         "last_exit": c.last_exit}
        return out

    def pid(self, name: str) -> int | None:
        return self.status()[name]["pid"]

    # ---------- 内部 ----------

    def _emit(self, name: str, event: str, info: dict) -> None:
        log.info("子进程 %s：%s %s", name, event, info)
        if self._on_event:
            try:
                self._on_event(name, event, info)
            except Exception:  # noqa: BLE001
                log.exception("子进程事件回调出错")

    def _spawn(self, c: _Child) -> bool:
        s = c.spec
        flags = _CREATE_SUSPENDED | _CREATE_NO_WINDOW | (_BELOW_NORMAL_PRIORITY_CLASS if s.below_normal else 0)
        out = open(s.log_path, "ab") if s.log_path else subprocess.DEVNULL
        job = proc = None
        try:
            job = Job(below_normal=s.below_normal)
            proc = subprocess.Popen(s.argv, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT, cwd=s.cwd,
                                    env=s.env, creationflags=flags)
            job.assign_and_resume(proc.pid)
        except Exception as exc:  # noqa: BLE001 - 启动失败按崩溃处理，走退避重启
            if proc is not None:
                proc.kill()
                proc.wait(5)
                if proc.stdin is not None:  # 否则管道句柄一直留在主进程里
                    proc.stdin.close()
            if job is not None:
                job.close()
            c.proc, c.job, c.state = None, None, "failed"
            self._emit(s.name, "start_failed", {"error": f"{type(exc).__name__}: {exc}"})
            self._schedule_restart(c, failed=True)
            return False
        finally:
            if out is not subprocess.DEVNULL:
                out.close()
        c.proc, c.job, c.state, c.started_at = proc, job, "running", self._clock()
        self._emit(s.name, "started", {"pid": proc.pid, "restarts": c.restarts})
        return True

    @staticmethod
    def _close_stdin(c: _Child) -> None:
        if c.proc is not None and c.proc.stdin is not None:
            try:
                c.proc.stdin.close()
            except OSError:
                pass

    def _halt(self, c: _Child, grace_s: float) -> int | None:
        code = None
        if c.proc is not None:
            self._close_stdin(c)
            try:
                code = c.proc.wait(grace_s)
            except subprocess.TimeoutExpired:
                if c.job:
                    c.job.terminate(1)
                try:
                    code = c.proc.wait(5)
                except subprocess.TimeoutExpired:
                    c.proc.kill()
                    code = c.proc.wait(5)
        if c.job is not None:
            c.job.terminate(1)  # 子进程自己退出了，它再开的进程也一并结束
            c.job.close()
        c.proc, c.job = None, None
        c.last_exit = code if code is not None else c.last_exit
        return code

    def _schedule_restart(self, c: _Child, failed: bool) -> None:
        s = c.spec
        if not c.wanted or s.restart == "never" or (s.restart == "on-failure" and not failed):
            c.wanted = False
            if c.state != "failed":
                c.state = "exited"
            return
        now = self._clock()
        if c.started_at and now - c.started_at >= s.stable_s:
            c.backoff_s = s.backoff_initial_s
        c.next_start_at = now + c.backoff_s
        self._emit(s.name, "restart_scheduled", {"in_s": round(c.backoff_s, 2), "restarts": c.restarts})
        c.backoff_s = min(s.backoff_max_s, max(c.backoff_s * 2, 0.05))
        c.state = "backoff"

    def tick(self) -> None:
        """看护一轮：发现退出的子进程 → 记录、按策略安排重启；到点的重启 → 启动。"""
        for c in self._children.values():
            if not c.lock.acquire(blocking=False):
                continue  # 正在 start/stop，下轮再看
            try:
                if c.state == "running" and c.proc is not None and c.proc.poll() is not None:
                    code = c.proc.returncode
                    self._halt(c, 0)
                    c.last_exit = code
                    self._emit(c.spec.name, "exited", {"code": code})
                    self._schedule_restart(c, failed=code != 0)
                elif c.state == "backoff" and c.wanted and self._clock() >= c.next_start_at:
                    c.restarts += 1
                    self._spawn(c)
            finally:
                c.lock.release()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - 看护线程不能停
                log.exception("子进程看护出错")
            self._stop.wait(self._poll_s)


def python_child(module: str, *args: str, pythonw: bool = False) -> list[str]:
    """用当前解释器以 -m 方式启动本项目模块的命令（供集成时拼录制进程、空闲整理进程）。"""
    exe = Path(sys.executable)
    if pythonw and exe.name.lower() == "python.exe" and (exe.with_name("pythonw.exe")).exists():
        exe = exe.with_name("pythonw.exe")
    return [str(exe), "-m", module, *args]


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """子进程环境：继承当前环境，把仓库根目录加到 PYTHONPATH（-m 能找到 screenrecall）。"""
    root = str(Path(__file__).resolve().parent.parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.update(extra or {})
    return env
