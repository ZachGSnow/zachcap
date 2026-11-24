"""Media the user brings in (the Import dialog, `zachcap import` and the watch folder: all of
audio.IMPORTS) is decoded here, in a child process: a file that crashes or hangs FFmpeg ends
only that process, and the engine reports it as the file's reason and carries on.

The child is the engine's own Python running this module, isolated (``-I``: no current
folder, PYTHONPATH or user site-packages on its path), with its folder and the file's path on
its command line. It decodes as audio.decode_media does, with the same checks, caps and
messages, and writes the audio to its folder as it goes (16 kHz mono float32), then a short
JSON result: ok, refused (with the message for the user), unreadable (the file can't be
opened right now) or failed. Nothing comes back through a pipe, so nothing the child writes
can fill the engine's memory, and the engine checks what it reads: the child may be running
code from a crafted file. The folder is private to the user and removed whatever happens.

The child runs in a Windows Job Object that ends it when the engine goes (kill on close, so
none is left behind), lets it start no other process, caps its memory (MEMORY), and ends it
at once if it crashes, with no error dialog. It reads the file only once it is in the job: it
waits for a go on stdin first. Its time limit (START, PER_SECOND) grows with the audio it has
written; when it runs out, the job is ended and the file "took too long to read".

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

import numpy as np

from . import audio

MODULE = "zachcap.decode_child"  # what the child runs: this module
AUDIO, RESULT, ERRORS = "audio.f32", "result.json", "errors.txt"  # in the child's folder
GO = b"go"  # on the child's stdin once it is in its job
# The time limit: a minute to start and open the file (the child is ready in under a
# second), then half a second more for each second of audio written. So the decoder has to
# keep up with twice real time: on a 6-core laptop it reads an hour of 48 kHz stereo AAC in
# about 4 seconds (850 times real time), and a 2 Mbps Teams recording on a mapped network
# drive needs 0.5 MB/s. A file that hangs FFmpeg at once is ended a minute in; one that
# hangs later, at most a minute plus half the length it got through (six hours for 12 hours
# of audio and then a hang, the worst a file can do).
START = 60.0
PER_SECOND = 0.5
# 4 GB: more than 12 hours of audio would take even held whole (2.8 GB as float32), though
# the child holds only a piece at a time (it peaked at 54 MB on an hour of AAC, 42 MB on a
# Teams-style recording with video). The cap stops a crafted file from making FFmpeg take
# all of the PC's memory.
MEMORY = 4 * 2**30
POLL = 0.25  # seconds between looks at how far the child has got
GONE = 30.0  # the most to wait for an ended child to be gone (a network drive can hold it)
# Folders a decode left when the engine stopped part-way (the job ended the child then) are
# removed a day on: no decode lasts that long (START + PER_SECOND * audio.MAX_SECONDS).
STALE = 24 * 3600.0
# Tests make the child misbehave part-way through a file with this ("crash" or "hang"), to
# check that the engine copes.
TEST_HOOK = "ZACHCAP_DECODE_TEST"


def decode(source: Path) -> np.ndarray:
    """The audio of a file the user brought in, as 16 kHz mono float32, decoded in a child
    process with audio.IMPORTS. Raises audio.Refused with the reason for the user (the
    refusals of decode_media, a file that crashed the decoder or took too long to read),
    OSError for a file that can't be opened right now (in use, or on a drive that's offline),
    and RuntimeError when the decoder couldn't start or failed some other way."""
    try:
        root = _root()
        _tidy(root)
        folder = Path(tempfile.mkdtemp(dir=root))  # for this user (and administrators) only
    except OSError as exc:  # not the file's doing: noted once, not tried on every look
        raise RuntimeError(f"the decoder couldn't start: {exc}") from exc
    try:
        code = _run(source, folder)
        if code is None:
            raise audio.Refused(f"{source.name} took too long to read")
        return _collect(source, folder, code)
    finally:
        _remove(folder)


def _run(source: Path, folder: Path) -> int | None:
    """Runs the child on ``source`` in a job of its own. Returns its exit code, or None if it
    ran out of time (it is ended then). Raises RuntimeError if it couldn't be started."""
    python, env = _interpreter()
    try:
        job = _Job()
    except OSError as exc:
        raise RuntimeError(f"the decoder couldn't start: {exc}") from exc
    try:
        try:
            with open(folder / ERRORS, "wb") as errors:
                child = subprocess.Popen(
                    [python, "-I", "-X", "utf8", "-m", MODULE, str(folder), str(source)],
                    bufsize=0,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=errors,
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
            return _wait(child, folder / AUDIO)
        finally:
            child.stdin.close()
            if child.poll() is None:  # out of time, or the engine is stopping
                job.end()
            try:
                child.wait(GONE)
            except subprocess.TimeoutExpired:
                pass  # still letting go of a network drive: its folder is tidied later
    finally:
        job.close()  # kills anything still in the job


def _wait(child: subprocess.Popen, written: Path) -> int | None:
    """The child's exit code, or None once it has used up its time: START seconds, and
    PER_SECOND more for each second of audio it has written so far."""
    began = time.monotonic()
    while True:
        try:
            return child.wait(POLL)
        except subprocess.TimeoutExpired:
            pass
        try:
            seconds = min(written.stat().st_size / 4 / audio.RATE, audio.MAX_SECONDS)
        except OSError:  # it hasn't got as far as the audio yet
            seconds = 0.0
        if time.monotonic() - began > START + PER_SECOND * seconds:
            return None


def _collect(source: Path, folder: Path, code: int) -> np.ndarray:
    """The audio the child wrote, or the reason it has none, as the exception to raise."""
    result = _result(folder / RESULT)
    if result is None:  # it ended without a word: a crash, or a Python that couldn't run it
        why = _last_line(folder / ERRORS)
        if why:
            raise RuntimeError(f"the decoder stopped reading {source.name}: {why}")
        ended = f"0x{code:08X}" if code >= 0x80000000 else f"exit code {code}"
        raise audio.Refused(f"{source.name} crashed the decoder ({ended})")
    kind = result.get("result")
    if kind == "ok":
        return _audio(source, folder / AUDIO)
    if kind == "refused":
        raise audio.Refused(_text(result.get("message")) or f"{source.name} couldn't be read")
    if kind == "unreadable":
        raise _os_error(result)
    raise RuntimeError(_text(result.get("message")) or f"the decoder couldn't read {source.name}")


def _result(path: Path) -> dict | None:
    """The child's result, or None if it left none (or not a JSON object)."""
    try:
        with open(path, "rb") as file:
            result = json.loads(file.read(64 * 1024))
    except (OSError, ValueError, RecursionError):
        return None
    return result if isinstance(result, dict) else None


def _audio(source: Path, path: Path) -> np.ndarray:
    """The audio the child wrote, once it's checked to be whole samples and no more than a
    file may hold (the resampler adds a few samples after the last check)."""
    try:
        size = path.stat().st_size
        if size % 4 == 0 and size <= (audio.MAX_SECONDS + 1) * audio.RATE * 4:
            return np.fromfile(path, dtype="<f4").astype(np.float32, copy=False)
    except OSError:
        pass
    raise RuntimeError(f"the decoder gave back audio for {source.name} that can't be used")


def _os_error(result: dict) -> OSError:
    """The child's OSError made again (PermissionError for a file in use, say), so a file
    that can't be opened right now is still tried again later (watchfolder)."""
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


def _last_line(path: Path) -> str:
    """The last line the child wrote to its errors (a Python error, say), or ''."""
    try:
        with open(path, "rb") as file:
            file.seek(max(0, file.seek(0, os.SEEK_END) - 4096))
            tail = file.read().decode("utf-8", "replace")
    except OSError:
        return ""
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    return _text(lines[-1]) if lines else ""


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
    """Where the children's folders go: a folder of ZachCap's own in the temp folder."""
    root = Path(tempfile.gettempdir()) / "zachcap-decoding"
    root.mkdir(mode=0o700, exist_ok=True)
    return root


def _tidy(root: Path) -> None:
    """Removes folders left by decodes the engine never finished, once they are STALE."""
    for folder in root.iterdir():
        try:
            if time.time() - folder.stat().st_mtime > STALE:
                shutil.rmtree(folder)
        except OSError:
            continue


def _remove(folder: Path) -> None:
    """Removes a child's folder, waiting a moment if a virus scanner is still looking at the
    audio just written. One that stays (its child still ending) is tidied later."""
    for _ in range(10):
        try:
            shutil.rmtree(folder)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.2)


# The Job Object ---------------------------------------------------------------------------

JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x0008
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x0200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x0400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMITS = 9  # the JobObjectExtendedLimitInformation information class
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
        (kernel32.OpenProcess, [DWORD, BOOL, DWORD], HANDLE),
        (kernel32.AssignProcessToJobObject, [HANDLE, HANDLE], BOOL),
        (kernel32.TerminateJobObject, [HANDLE, wintypes.UINT], BOOL),
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

    def end(self) -> None:
        """Ends every process in the job."""
        _kernel32().TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        _kernel32().CloseHandle(self.handle)


# The child --------------------------------------------------------------------------------


def main() -> None:
    """The child (started by _run): decodes the file named last on its command line into the
    folder named before it."""
    folder, source = Path(sys.argv[1]), Path(sys.argv[2])
    if sys.stdin.buffer.read() != GO:
        return  # the engine went away before this process was in its job
    result = _decode(source, folder / AUDIO)
    (folder / RESULT).write_text(json.dumps(result), encoding="utf-8")


def _decode(source: Path, target: Path) -> dict:
    """Decodes ``source`` into ``target``, a piece at a time (the engine follows how far it
    has got), and says how it went."""
    hook = os.environ.get(TEST_HOOK)
    try:
        with open(target, "wb") as out:
            for piece in audio.decode_pieces(source, audio.IMPORTS):
                samples = piece.astype(np.float32)
                samples /= 32768.0  # as decode_media does
                out.write(samples.tobytes())
                if hook:
                    out.flush()
                    _misbehave(hook)
    except audio.Refused as exc:
        return {"result": "refused", "message": str(exc)}
    except OSError as exc:  # can't be opened right now: in use, or on a drive that's offline
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
    return {"result": "ok"}


def _misbehave(hook: str) -> None:
    """What the tests make the child do once it has written some audio (TEST_HOOK)."""
    if hook == "crash":  # an access violation, as a crafted file can make FFmpeg have
        ctypes.c_int.from_address(0).value = 0
    elif hook == "hang":
        time.sleep(60)  # far past the tests' time limit, though not forever


if __name__ == "__main__":
    main()
