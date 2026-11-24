"""What a child process of the engine needs (decode_child, live_child): the engine's own
Python with an environment that has none of its secrets, and a Windows Job Object that ends the
child when the engine goes."""

from __future__ import annotations

import ctypes
import functools
import os
import sys
from ctypes import wintypes


def interpreter() -> tuple[str, dict[str, str]]:
    """The engine's own Python, and the environment for a child: the engine's, without its
    secrets (a child has no use for them) or a venv named by whatever started the engine (it
    is set below, only when it's this one's)."""
    from .apikeys import KEYS  # imported here: a child never needs it

    left_out = {"ZACHCAP_TOKEN", *KEYS.values(), "__PYVENV_LAUNCHER__"}
    env = {name: value for name, value in os.environ.items() if name.upper() not in left_out}
    base = getattr(sys, "_base_executable", sys.executable)
    if os.path.normcase(base) != os.path.normcase(sys.executable):
        # In a venv, python.exe is a launcher that runs the real Python as a second process,
        # which a job of one process wouldn't allow. The real one is run directly and told
        # it's the venv's, so it uses the venv's packages (as multiprocessing does, bpo-35797).
        env["__PYVENV_LAUNCHER__"] = sys.executable
        return base, env
    return sys.executable, env


JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x0008
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x0200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x0400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
# The JobObjectBasicAndIoAccountingInformation and JobObjectExtendedLimitInformation classes.
JOB_OBJECT_ACCOUNTING, JOB_OBJECT_EXTENDED_LIMITS = 8, 9
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
        ("IoInfo", IO_COUNTERS),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


@functools.cache
def _kernel32() -> ctypes.WinDLL:
    """This module's own copy of kernel32, with argument and result types set here (set on
    the shared ``ctypes.windll`` one, they would change other modules' calls)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    BOOL, DWORD, HANDLE = wintypes.BOOL, wintypes.DWORD, wintypes.HANDLE
    for function, argtypes, restype in (
        (kernel32.CreateJobObjectW, [ctypes.c_void_p, wintypes.LPCWSTR], HANDLE),
        (kernel32.SetInformationJobObject, [HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD], BOOL),
        (
            kernel32.QueryInformationJobObject,
            [HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD, ctypes.c_void_p],
            BOOL,
        ),
        (kernel32.OpenProcess, [DWORD, BOOL, DWORD], HANDLE),
        (kernel32.AssignProcessToJobObject, [HANDLE, HANDLE], BOOL),
        (kernel32.TerminateJobObject, [HANDLE, wintypes.UINT], BOOL),
        (kernel32.CloseHandle, [HANDLE], BOOL),
    ):
        function.argtypes, function.restype = argtypes, restype
    return kernel32


class Job:
    """A Windows Job Object for one child: whatever is in it ends with its last handle (the
    engine's, so the child goes when the engine does, however it stops), and a crash ends it
    at once, with no error dialog or report. ``processes`` and ``memory`` (in bytes): how many
    it may have at once, and how much they may take together, when that's limited."""

    def __init__(self, processes: int | None = None, memory: int | None = None) -> None:
        kernel32 = _kernel32()
        self._handle = kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        if processes is not None:
            flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            limits.BasicLimitInformation.ActiveProcessLimit = processes
        if memory is not None:
            flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
            limits.JobMemoryLimit = memory
        limits.BasicLimitInformation.LimitFlags = flags
        if not kernel32.SetInformationJobObject(
            self._handle, JOB_OBJECT_EXTENDED_LIMITS, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def add(self, pid: int) -> None:
        """Puts a process that was just started in the job."""
        kernel32 = _kernel32()
        process = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel32.AssignProcessToJobObject(self._handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(process)

    def used(self) -> tuple[float, int]:
        """The CPU time it has used so far, in seconds, and the bytes it has read."""
        so_far = JOBOBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION()
        _kernel32().QueryInformationJobObject(
            self._handle, JOB_OBJECT_ACCOUNTING, ctypes.byref(so_far), ctypes.sizeof(so_far), None
        )
        cpu = so_far.TotalUserTime + so_far.TotalKernelTime  # in units of 100 ns
        return cpu / 1e7, so_far.IoInfo.ReadTransferCount

    def end(self) -> None:
        """Ends every process in the job. Does nothing once it's closed."""
        if self._handle:
            _kernel32().TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        """Lets go of the job, which ends anything still in it."""
        if self._handle:
            _kernel32().CloseHandle(self._handle)
            self._handle = None
