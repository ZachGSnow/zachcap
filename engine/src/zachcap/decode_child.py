"""Media the user brings in (the Import dialog, `zachcap import` and the watch folder: all of
audio.IMPORTS) is decoded here, in a child process: a file that crashes or hangs FFmpeg ends
only that process, and the engine reports it as the file's reason and carries on.

The child is the engine's own Python running this module, isolated (``-I``: no current
folder, PYTHONPATH or user site-packages on its path), with the file's path on its command
line. It decodes as audio.decode_media does, with the same checks, caps and messages, and
writes the audio to its standard output as it goes (16 kHz mono int16). The last line of its
errors is a short JSON result: ok, refused (with the message for the user), no room (the file
says it holds more audio than there's free space for), unwritten (the audio couldn't be
written), unreadable (the file can't be read right now) or failed. Both are files, not pipes,
so nothing the child writes can fill the engine's memory, and the engine checks what it
reads: the child may be running code from a crafted file.

The two files are private to the user, and Windows deletes each with its last handle, the
engine's or the child's, so no audio stays behind however the engine stops, short of the PC
going off (which is what tidy is for).

Decoding never fills their drive, which a recording may be going to: audio.SPARE stays free
on it. The child is told how much room there is besides, and turns away a file that says it
holds more before decoding any of it; a file that says nothing, or something else than it
holds, ends when the drive gets down to SPARE. Both are audio.NoRoom, which the watch folder
tries again once there's room, not on every look.

The child runs in a Windows Job Object that ends it when the engine goes (kill on close, so
none is left behind), lets it start no other process, caps its memory (MEMORY), and ends it
at once if it crashes, with no error dialog. It reads the file only once it is in the job: it
waits for a go on stdin first. Its time limit follows how far it gets (_Patience), and tells
a file that hangs FFmpeg, which keeps the CPU busy and "took too long to read", from a drive
that has stopped answering, which doesn't (audio.Stalled: the watch folder tries it again).

The engine's own tracks (audio.TRACKS: the FLAC chunks it recorded, the playback cache,
redaction) are still decoded in the engine's process: ZachCap wrote them itself.
"""

from __future__ import annotations

import ctypes
import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path
from typing import BinaryIO

import numpy as np

from . import audio

MODULE = "zachcap.decode_child"  # what the child runs: this module
SAMPLE = 2  # bytes: int16, which a FLAC holds as it is (float32 took twice the space)
GO = b"go"  # on the child's stdin once it is in its job
RESULT = "decoded: "  # what the line with its result starts with, in its errors
# The time limits (_Patience): a minute of CPU time, and five minutes of waiting, without
# getting anywhere. The first ends a file that hangs FFmpeg. The second allows for a network
# drive that's slow to answer (Windows itself gives up on one after a minute or so) and for a
# cloud file, whose first read waits for all of it to come down (which goes on meanwhile if
# the child is ended, so another try finds it here).
BUSY = 60.0
IDLE = 300.0
# Each second of audio written gives half a second of both back, so the decoder has to keep
# up with twice real time: on a 6-core laptop it reads an hour of 48 kHz stereo AAC in about
# 4 seconds (850 times real time), and looks through 2 GB of video for its audio in one. The
# most CPU time a file can use up that way is six hours, with 12 hours of audio.
PER_SECOND = 0.5
# Each 64 KB read of the file gives a second of waiting back, so a drive at 0.5 Mbps is fast
# enough whatever the file's own bitrate: a 2 Mbps Teams recording over a 2.5 Mbps link, say.
# A file that's read over and over gets nowhere, so that stops at three times its size (some
# are read twice) and what Python reads to start (4.5 MB).
SLOWEST = 64 * 2**10
TIMES, STARTING = 3, 8 * 2**20
# 4 GB: more than 12 hours of audio would take even held whole (1.4 GB as int16), though
# the child holds only a piece at a time (it peaked at 54 MB on an hour of AAC, 42 MB on a
# Teams-style recording with video). The cap stops a crafted file from making FFmpeg take
# all of the PC's memory.
MEMORY = 4 * 2**30
POLL = 0.25  # seconds between looks at how far the child has got
# The most that counts between two looks. One that comes later than that says nothing about
# the file: the PC slept (the clock runs on meanwhile), or was too busy to look.
LATE = 1.0
GONE = 30.0  # the most to wait for an ended child to be gone (a network drive can hold it)
# ZachCap 0.3.0 had a folder for each decode, with a file open in it nearly all the time. One
# it left behind is removed, but not within ten minutes of a file coming or going in it, when
# its decode may just be starting or ending.
SETTLED = 600.0
LEFT = "-left"  # what its name then ends with
# Tests make the child misbehave part-way through a file with this ("crash", "hang", "stall"
# or "unwritable"), to check that the engine copes.
TEST_HOOK = "ZACHCAP_DECODE_TEST"


def decode(source: Path) -> np.ndarray:
    """The audio of a file the user brought in, as 16 kHz mono int16, decoded in a child
    process with audio.IMPORTS. Raises audio.Refused with the reason for the user (the
    refusals of decode_media, a file that crashed the decoder or took too long to read, and
    audio.NoRoom when there isn't the free space), OSError for a file that can't be read
    right now (in use, or on a drive that's offline; audio.Stalled if it stopped answering),
    and RuntimeError when the decoder couldn't start or failed some other way."""
    tidy()
    try:
        root = _root()
        audio.check_room(source.name, root)
        room = shutil.disk_usage(root).free - audio.SPARE
        out, said = tempfile.TemporaryFile(dir=root), tempfile.TemporaryFile(dir=root)
    except OSError as exc:  # not the file's doing: noted once, not tried on every look
        raise RuntimeError(f"the decoder couldn't start: {exc}") from exc
    with out, said:
        return _collect(source, out, said, room, _run(source, out, said, room))


def _run(source: Path, out: BinaryIO, said: BinaryIO, room: int) -> int:
    """Runs the child on ``source`` in a job of its own, with its output and its errors going
    to ``out`` and ``said``, and ``room`` bytes for the first. Returns its exit code, or raises
    what it ran out of (_wait), with the child ended. Raises RuntimeError if it couldn't be
    started."""
    python, env = _interpreter()
    try:
        job = _Job()
    except OSError as exc:
        raise RuntimeError(f"the decoder couldn't start: {exc}") from exc
    try:
        try:
            child = subprocess.Popen(
                [python, "-I", "-X", "utf8", "-m", MODULE, str(room), str(source)],
                bufsize=0,
                stdin=subprocess.PIPE,
                stdout=out,
                stderr=said,
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except OSError as exc:
            raise RuntimeError(f"the decoder couldn't start: {exc}") from exc
        try:
            try:
                job.add(child.pid)
            except OSError as exc:
                child.kill()  # not in the job, so ended here (it waits for its go first)
                raise RuntimeError(f"the decoder couldn't start: {exc}") from exc
            try:
                child.stdin.write(GO)  # it reads the file only now, in the job
            except OSError:
                pass  # it has stopped already: its exit code and errors say why
            child.stdin.close()
            return _wait(child, job, source, out)
        finally:
            child.stdin.close()
            if child.poll() is None:  # out of time or room, or the engine is stopping
                job.end()
            try:
                child.wait(GONE)
            except subprocess.TimeoutExpired:
                pass  # still letting go of a network drive: its files go when it does
    finally:
        job.close()  # kills anything still in the job


def _wait(child: subprocess.Popen, job: _Job, source: Path, out: BinaryIO) -> int:
    """The child's exit code. Raises audio.Refused when it has used up its CPU time,
    audio.Stalled when it has waited for the file long enough, and audio.NoRoom once the
    drive is down to what's kept free on it."""
    patience, last = _Patience(source), time.monotonic()
    while True:
        try:
            return child.wait(POLL)
        except subprocess.TimeoutExpired:
            pass
        if _full(out):
            raise _no_room(source, out)
        now = time.monotonic()
        passed, last = min(now - last, LATE), now
        patience.look(passed, *job.used(), _written(out))
        if patience.busy <= 0:
            raise audio.Refused(f"{source.name} took too long to read")
        if patience.idle <= 0:
            why = "its drive may be offline, or the file still downloading"
            raise audio.Stalled(f"reading {source.name} stalled; {why}")


class _Patience:
    """How long the child may still go on: ``busy`` seconds of CPU time, and ``idle`` seconds
    of waiting (what it didn't spend on the CPU). It starts with BUSY and IDLE, and what it
    gets through gives some back, never past those: a file that hangs is ended that long
    after it last got anywhere, however far it had got."""

    def __init__(self, source: Path) -> None:
        self.busy, self.idle = BUSY, IDLE
        self._cpu = self._read = self._seconds = 0.0
        try:
            self._most = TIMES * source.stat().st_size + STARTING
        except OSError:  # the child says why, if it can't open it either
            self._most = STARTING

    def look(self, passed: float, cpu: float, read: int, written: int) -> None:
        """``passed`` seconds since the last look, and so far: the child's CPU time, the bytes
        it has read, and the bytes of audio it has written."""
        used, self._cpu = cpu - self._cpu, cpu
        read = min(read, self._most)
        came, self._read = (read - self._read) / SLOWEST, read
        seconds = min(written / SAMPLE / audio.RATE, audio.MAX_SECONDS)
        got, self._seconds = PER_SECOND * (seconds - self._seconds), seconds
        self.busy = min(self.busy - used + got, BUSY)
        self.idle = min(self.idle - max(passed - used, 0.0) + got + came, IDLE)


def _written(out: BinaryIO) -> int:
    """The bytes of audio the child has written so far."""
    return os.fstat(out.fileno()).st_size


def _full(out: BinaryIO) -> bool:
    """Whether the drive the audio goes to is down to what's kept free on it."""
    try:
        return shutil.disk_usage(Path(out.name).parent).free < audio.SPARE
    except OSError:
        return False


def _no_room(source: Path, out: BinaryIO) -> audio.NoRoom:
    """For a file that got the drive that full. It doesn't say how long it is (or not the
    truth), so the next try waits for twice the room it took this time."""
    return audio.NoRoom(source.name, Path(out.name).parent, 2 * _written(out))


def _longest() -> int:
    """The bytes of audio a file may hold (the resampler adds a few samples after the last
    check)."""
    return (audio.MAX_SECONDS + 1) * audio.RATE * SAMPLE


def _collect(source: Path, out: BinaryIO, said: BinaryIO, room: int, code: int) -> np.ndarray:
    """The audio the child wrote, or the reason it has none, as the exception to raise."""
    lines = _lines(said)
    result = _result(lines)
    if _full(out) and (result is None or result.get("result") == "unwritten"):
        raise _no_room(source, out)  # for the audio, or even for the result
    if result is None:  # it ended without a word: a crash, or a Python that couldn't run it
        if lines:
            raise RuntimeError(f"the decoder stopped reading {source.name}: {_text(lines[-1])}")
        ended = f"0x{code:08X}" if code >= 0x80000000 else f"exit code {code}"
        raise audio.Refused(f"{source.name} crashed the decoder ({ended})")
    kind = result.get("result")
    if kind == "ok":
        return _audio(source, out)
    if kind == "refused":
        raise audio.Refused(_text(result.get("message")) or f"{source.name} couldn't be read")
    if kind == "no room":
        size = result.get("size")
        if isinstance(size, int) and room < size <= _longest():
            raise audio.NoRoom(source.name, Path(out.name).parent, size)
        raise RuntimeError(f"the decoder couldn't tell how long {source.name} is")
    if kind == "unwritten":
        why = _text(result.get("message"))
        raise RuntimeError(f"the audio of {source.name} couldn't be written out: {why}")
    if kind == "unreadable":
        raise _os_error(result)
    raise RuntimeError(_text(result.get("message")) or f"the decoder couldn't read {source.name}")


def _lines(said: BinaryIO) -> list[str]:
    """The last lines of the child's errors (a Python error, say, or its result)."""
    try:
        said.seek(max(0, said.seek(0, os.SEEK_END) - 64 * 1024))
        lines = said.read().decode("utf-8", "replace").splitlines()
    except OSError:  # this file's, not the user's
        return []
    return [line.strip() for line in lines if line.strip()]


def _result(lines: list[str]) -> dict | None:
    """The child's result, or None if it left none (or not a JSON object)."""
    line = next((line for line in reversed(lines) if line.startswith(RESULT)), RESULT)
    try:
        result = json.loads(line.removeprefix(RESULT))
    except (ValueError, RecursionError):
        return None
    return result if isinstance(result, dict) else None


def _audio(source: Path, out: BinaryIO) -> np.ndarray:
    """The audio the child wrote, once it's checked to be whole samples and no more than a
    file may hold."""
    try:
        size = _written(out)
        if size % SAMPLE == 0 and size <= _longest():
            out.seek(0)
            return np.fromfile(out, dtype="<i2").astype(np.int16, copy=False)
    except OSError:  # this file's, not the user's
        pass
    raise RuntimeError(f"the decoder gave back audio for {source.name} that can't be used")


def _os_error(result: dict) -> OSError:
    """The child's OSError made again (PermissionError for a file in use, say), so a file
    that can't be read right now is still tried again later (watchfolder)."""
    errno, strerror = result.get("errno"), result.get("strerror")
    filename, winerror = result.get("filename"), result.get("winerror")
    if not (isinstance(errno, int) and isinstance(strerror, str)):
        return OSError(_text(result.get("message")) or "the file couldn't be opened")
    details = [errno, _text(strerror), _text(filename) if isinstance(filename, str) else None]
    if isinstance(winerror, int):  # then Windows' own number is shown, as by the child
        details.append(winerror)
    return OSError(*details)


def _text(value: object) -> str:
    """A message from the child, as text of a sensible length."""
    return value[:1000] if isinstance(value, str) else ""


def _interpreter() -> tuple[str, dict[str, str]]:
    """The engine's own Python, and the environment for the child: the engine's, without its
    secrets (the child has no use for them) or a venv named by whatever started the engine
    (it is set below, only when it's this one's)."""
    from .apikeys import KEYS  # imported here: the child never needs it

    left_out = {"ZACHCAP_TOKEN", *KEYS.values(), "__PYVENV_LAUNCHER__"}
    env = {name: value for name, value in os.environ.items() if name.upper() not in left_out}
    # numpy's OpenBLAS sets memory aside for a thread per core when it loads (about 350 MB
    # on a 12-thread PC), and the child does no maths that would use them.
    env["OPENBLAS_NUM_THREADS"] = "1"
    base = getattr(sys, "_base_executable", sys.executable)
    if os.path.normcase(base) != os.path.normcase(sys.executable):
        # In a venv, python.exe is a launcher that runs the real Python as a second process,
        # which the job wouldn't allow. The real one is run directly and told it's the
        # venv's, so it uses the venv's packages (as multiprocessing does, bpo-35797).
        env["__PYVENV_LAUNCHER__"] = sys.executable
        return base, env
    return sys.executable, env


def _root() -> Path:
    """Where the children's files go: a folder of ZachCap's own in the temp folder, for this
    user (and administrators) only."""
    root = Path(tempfile.gettempdir()) / "zachcap-decoding"
    root.mkdir(mode=0o700, exist_ok=True)
    return root


def tidy() -> None:
    """Removes what decodes left behind: the PC went off during one, or ZachCap 0.3.0 was
    stopped during one. At the engine's start, and before each decode. The files of a decode
    under way, in this process or another, are open from the moment they're made until the
    last process using them is gone, and what's open is left alone."""
    try:
        left = list(_root().iterdir())
    except OSError:
        return
    for path in left:
        try:
            if not path.is_dir():
                _delete_unused(path)
            elif path.name.endswith(LEFT):
                shutil.rmtree(path)
            elif time.time() - path.stat().st_mtime > SETTLED:
                # Windows refuses to rename a folder with a file open in it.
                shutil.rmtree(path.rename(path.with_name(path.name + LEFT)))
        except OSError:
            continue


def _delete_unused(path: Path) -> None:
    """Deletes a file that no process has open, or raises OSError: it's opened to be deleted
    when closed, and shared with no one. (Plain deleting would take the name off a child's
    file too, which is open to be deleted.)"""
    kernel32 = _kernel32()
    flags = FILE_FLAG_DELETE_ON_CLOSE | FILE_FLAG_OPEN_REPARSE_POINT
    handle = kernel32.CreateFileW(str(path), DELETE, 0, None, OPEN_EXISTING, flags, None)
    if handle == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    kernel32.CloseHandle(handle)


# The Job Object ---------------------------------------------------------------------------

JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x0008
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x0200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x0400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
# The JobObjectBasicAndIoAccountingInformation and JobObjectExtendedLimitInformation classes.
JOB_OBJECT_ACCOUNTING, JOB_OBJECT_EXTENDED_LIMITS = 8, 9
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
DELETE = 0x00010000
OPEN_EXISTING = 3
FILE_FLAG_DELETE_ON_CLOSE = 0x04000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000  # a link itself, never what it points to
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


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
        (
            kernel32.CreateFileW,
            [wintypes.LPCWSTR, DWORD, DWORD, ctypes.c_void_p, DWORD, DWORD, HANDLE],
            HANDLE,
        ),
        (kernel32.CloseHandle, [HANDLE], BOOL),
    ):
        function.argtypes, function.restype = argtypes, restype
    return kernel32


class _Job:
    """A Windows Job Object for one child: ended with its last handle (the engine's, so the
    child goes when the engine does, however it stops), one process at a time (the child
    can't start another), MEMORY at most, and no error dialog or report on a crash."""

    def __init__(self) -> None:
        kernel32 = _kernel32()
        self.handle = kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | JOB_OBJECT_LIMIT_JOB_MEMORY
            | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        )
        limits.BasicLimitInformation.ActiveProcessLimit = 1
        limits.JobMemoryLimit = MEMORY
        size = ctypes.sizeof(limits)
        if not kernel32.SetInformationJobObject(
            self.handle, JOB_OBJECT_EXTENDED_LIMITS, ctypes.byref(limits), size
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def add(self, pid: int) -> None:
        kernel32 = _kernel32()
        process = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel32.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(process)

    def used(self) -> tuple[float, int]:
        """The CPU time the child has used so far, in seconds, and the bytes it has read."""
        so_far = JOBOBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION()
        _kernel32().QueryInformationJobObject(
            self.handle, JOB_OBJECT_ACCOUNTING, ctypes.byref(so_far), ctypes.sizeof(so_far), None
        )
        cpu = so_far.TotalUserTime + so_far.TotalKernelTime  # in units of 100 ns
        return cpu / 1e7, so_far.IoInfo.ReadTransferCount

    def end(self) -> None:
        """Ends every process in the job."""
        _kernel32().TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        _kernel32().CloseHandle(self.handle)


# The child --------------------------------------------------------------------------------


def main() -> None:
    """The child (started by _run): decodes the file named last on its command line to its
    output, where there's room for the bytes of audio named first."""
    room, source = int(sys.argv[1]), Path(sys.argv[2])
    if sys.stdin.buffer.read() != GO:
        return  # the engine went away before this process was in its job
    result = _decode(source, sys.stdout.buffer, room)
    print(RESULT + json.dumps(result), file=sys.stderr)


class _TooBig(Exception):
    """The file says it holds more audio (``size`` bytes) than there's room for."""

    def __init__(self, size: int) -> None:
        self.size = size


def _decode(source: Path, out: BinaryIO, room: int) -> dict:
    """Decodes ``source`` into ``out``, a piece at a time (the engine follows how far it has
    got), and says how it went. A file that says it holds more than ``room`` bytes of audio
    isn't decoded."""
    import av

    hook = os.environ.get(TEST_HOOK)

    def starting(seconds: float) -> None:
        size = int(seconds * audio.RATE) * SAMPLE
        if size > room:
            raise _TooBig(size)

    try:
        for piece in audio.decode_pieces(source, audio.IMPORTS, starting):
            try:
                out.write(piece.tobytes())
                if hook:
                    out.flush()
            except OSError as exc:
                return _unwritten(exc)
            if hook:
                _misbehave(hook, out)
    except audio.Refused as exc:
        return {"result": "refused", "message": str(exc)}
    except _TooBig as exc:
        return {"result": "no room", "size": exc.size}
    except av.error.FFmpegError as exc:
        # Some are OSErrors too, but such a file won't read any better later: EPERM, which PyAV
        # makes a PermissionError, is its decoders' word for audio they can't make out.
        damaged = isinstance(exc, PermissionError)
        why = f"the audio in {source.name} is damaged" if damaged else str(exc)
        return {"result": "failed", "message": why}
    except OSError as exc:  # can't be read right now: in use, or on a drive that's offline
        return {
            "result": "unreadable",
            "errno": exc.errno,
            "strerror": exc.strerror,
            "filename": None if exc.filename is None else str(exc.filename),
            "winerror": getattr(exc, "winerror", None),
            "message": str(exc),
        }
    except Exception as exc:
        return {"result": "failed", "message": str(exc) or type(exc).__name__}
    try:
        out.flush()
    except OSError as exc:
        return _unwritten(exc)
    return {"result": "ok"}


def _unwritten(exc: OSError) -> dict:
    """The result when the audio couldn't be written."""
    return {"result": "unwritten", "message": exc.strerror or type(exc).__name__}


def _misbehave(hook: str, out: BinaryIO) -> None:
    """What the tests make the child do once it has written some audio (TEST_HOOK)."""
    if hook == "crash":  # an access violation, as a crafted file can make FFmpeg have
        ctypes.c_int.from_address(0).value = 0
    elif hook == "hang":  # busy far past the tests' time limit, though not forever
        while time.process_time() < 60:
            pass
    elif hook == "stall":  # as while a drive doesn't answer
        time.sleep(60)
    elif hook == "unwritable":  # every write fails from here on, as on a full drive
        os.close(out.fileno())


if __name__ == "__main__":
    main()
