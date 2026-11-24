"""Files the user brings in are decoded in a child process (decode_child): one that crashes or
hangs FFmpeg ends only the child, within its time limit, and leaves nothing behind."""

import ctypes
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from zachcap import audio, decode_child

RATE = 16000
SYNCHRONIZE, WAIT_TIMEOUT = 0x00100000, 0x102
GENERIC_READ, OPEN_EXISTING = 0x80000000, 3
NO_HANDLE = (None, 2**64 - 1)  # what CreateFileW gives on failure: NULL or INVALID_HANDLE_VALUE


def recording(path: Path, seconds: float = 1.0) -> Path:
    t = np.arange(int(seconds * RATE)) / RATE
    sf.write(path, (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), RATE)
    return path


def kernel32() -> ctypes.WinDLL:
    """The test's own kernel32, with the result types it needs."""
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.OpenProcess.restype = ctypes.c_void_p
    library.CreateFileW.restype = ctypes.c_void_p
    library.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    library.CreateFileW.argtypes += [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
    library.CreateFileW.argtypes += [ctypes.c_void_p]
    return library


def running(pid: int) -> bool:
    """Whether a process is still running, as Windows sees it."""
    windows = kernel32()
    handle = windows.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return False  # gone, and nothing holds it
    try:
        return windows.WaitForSingleObject(ctypes.c_void_p(handle), 0) == WAIT_TIMEOUT
    finally:
        windows.CloseHandle(ctypes.c_void_p(handle))


def eventually(check: Callable[[], bool], seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True


@pytest.fixture
def folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where the children's folders go: under the test's own temp folder."""
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    return temp / "zachcap-decoding"


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[subprocess.Popen]:
    """The decoders started, to check none is left running."""
    children: list[subprocess.Popen] = []
    real = subprocess.Popen

    def start(*args, **kwargs):
        children.append(real(*args, **kwargs))
        return children[-1]

    monkeypatch.setattr(subprocess, "Popen", start)
    return children


def test_the_child_gives_the_very_samples_decoding_here_would(tmp_path: Path, folders, started):
    rate = 44100  # stereo at another rate, so the child mixes and resamples
    t = np.arange(2 * rate) / rate
    left, right = 0.3 * np.sin(2 * np.pi * 220 * t), 0.2 * np.sin(2 * np.pi * 330 * t)
    sf.write(tmp_path / "call.wav", np.stack([left, right], axis=1).astype(np.float32), rate)

    decoded = decode_child.decode(tmp_path / "call.wav")

    assert decoded.dtype == np.float32 and abs(len(decoded) / RATE - 2.0) < 0.01
    assert np.array_equal(decoded, audio.decode_media(tmp_path / "call.wav", audio.IMPORTS))
    (child,) = started
    assert not running(child.pid) and list(folders.iterdir()) == []


def test_a_crash_is_reported_plainly_and_leaves_nothing_behind(
    tmp_path: Path, folders, started, monkeypatch
):
    source = recording(tmp_path / "call.wav")
    monkeypatch.setenv(decode_child.TEST_HOOK, "crash")  # once it has written some audio

    with pytest.raises(audio.Refused, match=r"^call\.wav crashed the decoder \(0xC0000005\)$"):
        decode_child.decode(source)

    (child,) = started
    assert child.returncode == 0xC0000005  # an access violation, as in FFmpeg
    assert not running(child.pid) and list(folders.iterdir()) == []


def test_a_hang_is_ended_at_the_time_limit_and_leaves_nothing_behind(
    tmp_path: Path, folders, started, monkeypatch
):
    source = recording(tmp_path / "call.wav")
    monkeypatch.setenv(decode_child.TEST_HOOK, "hang")  # a minute's sleep after some audio
    monkeypatch.setattr(decode_child, "START", 2.0)
    began = time.monotonic()

    with pytest.raises(audio.Refused, match=r"^call\.wav took too long to read$"):
        decode_child.decode(source)

    assert time.monotonic() - began < 20  # at its limit, not when the sleep ends
    (child,) = started
    assert not running(child.pid) and list(folders.iterdir()) == []


class Decoder:
    """A child on a pretend clock: it writes ``speed`` seconds of audio a second until
    ``hangs`` seconds in, then nothing more."""

    def __init__(self, written: Path, speed: float, hangs: float) -> None:
        self.written, self.speed, self.hangs, self.now = written, speed, hangs, 0.0
        written.touch()

    def wait(self, timeout: float) -> int:
        self.now += timeout
        seconds = self.speed * min(self.now, self.hangs)
        os.truncate(self.written, int(seconds * RATE) * 4)
        raise subprocess.TimeoutExpired("decoder", timeout)


@pytest.mark.parametrize(
    ("speed", "hangs", "cap", "ended"),
    [
        (0, 0, None, 60),  # hangs at once: a minute in
        (100, 2, None, 60 + 0.5 * 200),  # 200 s of audio, then a hang
        (4, 100, None, 60 + 0.5 * 400),  # slower, and longer: the same rule
        (1, 10_000, None, 120),  # still going, but only at real time: too slow
        (100, 2, 100, 60 + 0.5 * 100),  # more than a file may hold doesn't buy more time
    ],
)
def test_the_time_limit_grows_with_the_audio_written(
    tmp_path: Path, monkeypatch, speed: float, hangs: float, cap: int | None, ended: float
):
    child = Decoder(tmp_path / "audio.f32", speed, hangs)
    monkeypatch.setattr(decode_child, "time", SimpleNamespace(monotonic=lambda: child.now))
    if cap:
        monkeypatch.setattr(audio, "MAX_SECONDS", cap)

    assert decode_child._wait(child, child.written) is None  # ran out of time

    assert ended < child.now <= ended + decode_child.POLL


ENGINE = """
import subprocess, sys
from pathlib import Path
from zachcap import decode_child

real = subprocess.Popen

def start(*args, **kwargs):
    child = real(*args, **kwargs)
    print(child.pid, flush=True)
    return child

subprocess.Popen = start
decode_child.decode(Path(sys.argv[1]))
"""


def test_the_decoder_goes_when_the_engine_does(tmp_path: Path, folders):
    # An engine ended from Task Manager, or crashing, can't end its decoder: Windows closes
    # the engine's handle to the job, and that ends the decoder.
    source = recording(tmp_path / "call.wav")
    python, env = decode_child._interpreter()
    temp = str(folders.parent)
    env.update({decode_child.TEST_HOOK: "hang", "TMP": temp, "TEMP": temp})
    engine = subprocess.Popen(
        [python, "-I", "-c", ENGINE, str(source)],
        stdout=subprocess.PIPE,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    def hanging() -> bool:  # it has written some audio, and sleeps
        return any(path.stat().st_size for path in folders.glob("*/audio.f32"))

    try:
        said: list[bytes] = []
        reader = threading.Thread(target=lambda: said.append(engine.stdout.readline()))
        reader.daemon = True
        reader.start()
        reader.join(30)
        decoder = int(said[0])
        assert running(decoder) and eventually(hanging)

        engine.kill()

        assert eventually(lambda: not running(decoder), seconds=10)
    finally:
        engine.kill()
        engine.wait(10)


def test_folders_left_by_a_stopped_engine_go_a_day_on(tmp_path: Path, folders):
    folders.mkdir()
    for name in ("left", "in-use"):
        (folders / name).mkdir()
        (folders / name / "audio.f32").write_bytes(bytes(64))
    a_day_ago = time.time() - decode_child.STALE - 60
    os.utime(folders / "left", (a_day_ago, a_day_ago))

    decode_child.decode(recording(tmp_path / "call.wav"))  # the next decode tidies

    assert [path.name for path in folders.iterdir()] == ["in-use"]  # another engine's, maybe


def test_the_job_allows_one_process_caps_memory_and_goes_with_its_handle(monkeypatch):
    python, env = decode_child._interpreter()
    monkeypatch.setattr(decode_child, "MEMORY", 256 * 2**20)

    def in_a_job(code: str) -> tuple[decode_child._Job, subprocess.Popen]:
        job = decode_child._Job()
        child = subprocess.Popen(
            [python, "-I", "-c", f"import sys; sys.stdin.read(); {code}"],
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        job.add(child.pid)
        child.stdin.close()  # in the job: go on
        return job, child

    job, child = in_a_job("import subprocess; subprocess.run([sys.executable, '-V'])")
    errors = child.communicate(timeout=30)[1].decode()
    job.close()
    assert child.returncode == 1 and "[WinError 1816]" in errors  # no quota for a second one

    job, child = in_a_job("data = bytearray(512 * 2**20)")
    errors = child.communicate(timeout=30)[1].decode()
    job.close()
    assert child.returncode == 1 and errors.strip().endswith("MemoryError")

    job, child = in_a_job("import time; time.sleep(60)")
    try:
        job.close()  # as when the engine goes, however it goes
        assert child.wait(10) is not None
    finally:
        child.kill()


@pytest.mark.parametrize(
    ("python", "module", "message"),
    [
        (
            r"C:\Users\Public\no-python\python.exe",
            decode_child.MODULE,
            r"^the decoder couldn't start: \[WinError 2\] ",
        ),
        (
            None,
            "zachcap.no_such_module",
            r"^the decoder stopped reading call\.wav: .*No module named zachcap\.no_such_module$",
        ),
    ],
    ids=["no python", "no module"],
)
def test_a_decoder_that_cant_start_is_a_failure_with_the_reason(
    tmp_path: Path, folders, monkeypatch, python: str | None, module: str, message: str
):
    source = recording(tmp_path / "call.wav")
    if python:
        env = decode_child._interpreter()[1]
        monkeypatch.setattr(decode_child, "_interpreter", lambda: (python, env))
    monkeypatch.setattr(decode_child, "MODULE", module)

    with pytest.raises(RuntimeError, match=message):  # not an OSError: not tried on every look
        decode_child.decode(source)

    assert list(folders.iterdir()) == []


def test_a_file_in_use_elsewhere_is_still_one_to_try_again(tmp_path: Path, folders):
    # Open in another program with no sharing: the watch folder tries such a file again on
    # its next look, so the child's PermissionError comes back as one.
    source = recording(tmp_path / "busy.wav")
    windows = kernel32()
    handle = windows.CreateFileW(str(source), GENERIC_READ, 0, None, OPEN_EXISTING, 0, None)
    assert handle not in NO_HANDLE
    try:
        with pytest.raises(PermissionError) as refused:
            decode_child.decode(source)
    finally:
        windows.CloseHandle(ctypes.c_void_p(handle))

    assert str(refused.value) == f"[Errno 13] Permission denied: {str(source)!r}"
    assert len(decode_child.decode(source)) / RATE > 0.9  # and read once it's let go


def test_the_child_gets_the_real_python_and_none_of_the_engines_secrets(monkeypatch):
    venv = r"C:\Users\Public\venv\Scripts\python.exe"
    base = r"C:\Users\Public\Python312\python.exe"
    monkeypatch.setattr(sys, "executable", venv)
    monkeypatch.setattr(sys, "_base_executable", base)
    monkeypatch.setenv("ZACHCAP_TOKEN", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("ZACHCAP_HOME", r"C:\Users\Public\ZachCap")

    python, env = decode_child._interpreter()

    # The venv's launcher would be a second process: the real Python runs as the venv's.
    assert python == base and env["__PYVENV_LAUNCHER__"] == venv
    assert "ZACHCAP_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
    assert env["ZACHCAP_HOME"] == r"C:\Users\Public\ZachCap"
    assert env["OPENBLAS_NUM_THREADS"] == "1"

    monkeypatch.setattr(sys, "_base_executable", venv)  # a Python that isn't a venv's
    monkeypatch.setenv("__PYVENV_LAUNCHER__", r"C:\Users\Public\other\Scripts\python.exe")
    python, env = decode_child._interpreter()
    assert python == venv and "__PYVENV_LAUNCHER__" not in env
