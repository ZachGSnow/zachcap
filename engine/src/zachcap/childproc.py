"""What a child process of the engine needs: the engine's own Python with an environment that
has none of its secrets, and a Windows Job Object that ends the child when the engine goes."""

from __future__ import annotations

import ctypes
import functools
import os
import subprocess
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
        # In a venv, python.exe is a launcher that runs the real Python as a second process.
        # The real one is run directly and told it's the venv's, so it uses the venv's
        # packages (as multiprocessing does, bpo-35797).
        env["__PYVENV_LAUNCHER__"] = sys.executable
        return base, env
    return sys.executable, env


JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x0400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMITS = 9  # the JobObjectExtendedLimitInformation information class


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
        (kernel32.AssignProcessToJobObject, [HANDLE, HANDLE], BOOL),
        (kernel32.TerminateJobObject, [HANDLE, wintypes.UINT], BOOL),
        (kernel32.CloseHandle, [HANDLE], BOOL),
    ):
        function.argtypes, function.restype = argtypes, restype
    return kernel32


class Job:
    """A Windows Job Object for one child: whatever is in it ends with its last handle (the
    engine's, so the child goes when the engine does, however it stops), and a crash ends it
    at once, with no error dialog or report."""

    def __init__(self) -> None:
        kernel32 = _kernel32()
        self._handle = kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        )
        if not kernel32.SetInformationJobObject(
            self._handle, JOB_OBJECT_EXTENDED_LIMITS, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def add(self, process: subprocess.Popen) -> None:
        """Puts a process that was just started in the job."""
        if not _kernel32().AssignProcessToJobObject(self._handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def end(self) -> None:
        """Ends every process in the job. Does nothing once it's closed."""
        if self._handle:
            _kernel32().TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        """Lets go of the job, which ends anything still in it."""
        if self._handle:
            _kernel32().CloseHandle(self._handle)
            self._handle = None
