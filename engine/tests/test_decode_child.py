"""Files the user brings in are decoded in a child process (decode_child): one that crashes or
hangs FFmpeg ends only the child, within its time limit, and leaves nothing behind."""

import ctypes
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import av
import numpy as np
import pytest
import soundfile as sf

from zachcap import audio, decode_child

RATE = 16000
SYNCHRONIZE, WAIT_TIMEOUT = 0x00100000, 0x102
PROCESS_QUERY_LIMITED_INFORMATION, TOKEN_QUERY = 0x1000, 0x8
TOKEN_PRIVILEGES, TOKEN_INTEGRITY_LEVEL = 3, 25
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
def files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where the children's files go: under the test's own temp folder."""
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    return temp / "zachcap-decoding"


def written(files: Path) -> int:
    """The bytes the children have written so far."""
    return sum(path.stat().st_size for path in files.iterdir()) if files.is_dir() else 0


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[subprocess.Popen]:
    """The decoders started, to check none is left running."""
    children: list[subprocess.Popen] = []
    real = subprocess.Popen

    def start(command, *args, **kwargs):
        child = real(command, *args, **kwargs)
        if decode_child.MODULE in command:  # not what Python runs to learn Windows' version
            children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", start)
    return children


def test_the_child_gives_the_very_samples_decoding_here_would(tmp_path: Path, files, started):
    rate = 44100  # stereo at another rate, so the child mixes and resamples
    t = np.arange(2 * rate) / rate
    left, right = 0.3 * np.sin(2 * np.pi * 220 * t), 0.2 * np.sin(2 * np.pi * 330 * t)
    sf.write(tmp_path / "call.wav", np.stack([left, right], axis=1).astype(np.float32), rate)

    decoded = decode_child.decode(tmp_path / "call.wav")

    assert decoded.dtype == np.int16 and abs(len(decoded) / RATE - 2.0) < 0.01
    here = audio.decode_media(tmp_path / "call.wav", audio.IMPORTS)
    assert np.array_equal(decoded.astype(np.float32) / 32768, here)
    (child,) = started
    assert not running(child.pid) and list(files.iterdir()) == []


def test_a_crash_is_reported_plainly_and_leaves_nothing_behind(
    tmp_path: Path, files, started, monkeypatch
):
    source = recording(tmp_path / "call.wav")
    monkeypatch.setenv(decode_child.TEST_HOOK, "crash")  # once it has written some audio

    with pytest.raises(audio.Refused, match=r"^call\.wav crashed the decoder \(0xC0000005\)$"):
        decode_child.decode(source)

    (child,) = started
    assert child.returncode == 0xC0000005  # an access violation, as in FFmpeg
    assert not running(child.pid) and list(files.iterdir()) == []


def test_a_hang_is_ended_at_the_time_limit_and_leaves_nothing_behind(
    tmp_path: Path, files, started, monkeypatch
):
    source = recording(tmp_path / "call.wav")
    monkeypatch.setenv(decode_child.TEST_HOOK, "hang")  # a minute's work after some audio
    monkeypatch.setattr(decode_child, "BUSY", 2.0)
    began = time.monotonic()

    with pytest.raises(audio.Refused, match=r"^call\.wav took too long to read$"):
        decode_child.decode(source)

    assert time.monotonic() - began < 50  # at its limit, not when the work is done
    (child,) = started
    assert not running(child.pid) and list(files.iterdir()) == []


def test_a_stall_is_a_file_to_try_again_and_leaves_nothing_behind(
    tmp_path: Path, files, started, monkeypatch
):
    # A drive that stops answering keeps the child waiting, with no use of the CPU.
    source = recording(tmp_path / "call.wav")
    monkeypatch.setenv(decode_child.TEST_HOOK, "stall")  # a minute's sleep after some audio
    monkeypatch.setattr(decode_child, "IDLE", 2.0)
    began = time.monotonic()

    with pytest.raises(audio.Stalled, match=r"^reading call\.wav stalled; its drive may be "):
        decode_child.decode(source)

    assert issubclass(audio.Stalled, OSError)
    assert time.monotonic() - began < 50  # at its limit, not when the sleep ends
    (child,) = started
    assert not running(child.pid) and list(files.iterdir()) == []


class Decoder:
    """A child and its job on a pretend clock: it reads ``link`` bytes and writes ``speed``
    seconds of audio a second, with ``cores`` of the CPU, until ``stops`` seconds in. Then it
    hangs (``after`` cores), or is done (None)."""

    def __init__(
        self,
        speed: float = 0.0,
        link: float = 0.0,
        cores: float = 0.0,
        stops: float = 0.0,
        after: float | None = 0.0,
        sleeps: float | None = None,
    ) -> None:
        self.speed, self.link, self.cores = speed, link, cores
        self.stops, self.after, self.sleeps = stops, after, sleeps
        self.now = self.ran = 0.0

    def wait(self, timeout: float) -> int:
        if self.after is None and self.ran >= self.stops:
            return 0
        self.ran += timeout
        self.now += timeout
        if self.sleeps is not None and self.ran >= self.sleeps:
            self.now += 8 * 3600  # the PC slept: the clock ran on, the child didn't
            self.sleeps = None
        raise subprocess.TimeoutExpired("decoder", timeout)

    def used(self) -> tuple[float, int]:
        working = min(self.ran, self.stops)
        cpu = self.cores * working + (self.after or 0.0) * (self.ran - working)
        return cpu, int(self.link * working)

    def written(self, out) -> int:
        return int(self.speed * min(self.ran, self.stops) * RATE) * 2


MBPS = 1_000_000 / 8
HOURS = 3600.0


@pytest.fixture
def outcome(monkeypatch: pytest.MonkeyPatch):
    """Waits for a pretend child reading a file of ``size`` bytes. Returns how it ended."""

    def run(child: Decoder, size: float = 0) -> int | type[Exception]:
        info = SimpleNamespace(st_size=int(size))
        source = SimpleNamespace(name="call.mkv", stat=lambda: info)
        child.now = child.ran = 0.0  # it was made when the tests were collected
        monkeypatch.setattr(decode_child, "time", SimpleNamespace(monotonic=lambda: child.now))
        monkeypatch.setattr(decode_child, "POLL", 1.0)
        monkeypatch.setattr(decode_child, "_written", child.written)
        monkeypatch.setattr(decode_child, "_full", lambda out: False)
        try:
            return decode_child._wait(child, child, source, io.BytesIO())
        except (audio.Refused, audio.Stalled) as exc:
            return type(exc)

    return run


@pytest.mark.parametrize(
    ("child", "ended"),
    [
        (Decoder(after=1.0), 60),  # hangs FFmpeg at once: a minute in
        (Decoder(after=0.25), 240),  # the same on a busy PC, with a quarter of a core
        # 200 s of audio, or hours of it, and then a hang: a minute after that too
        (Decoder(speed=100, cores=1.0, stops=2, after=1.0), 2 + 60),
        (Decoder(speed=100, cores=1.0, stops=300, after=1.0), 300 + 60),
        # still going, but only at real time: too slow
        (Decoder(speed=1, cores=1.0, stops=10 * HOURS), 120),
    ],
)
def test_a_file_that_keeps_the_cpu_busy_and_gets_nowhere_is_ended(outcome, child, ended):
    assert outcome(child) is audio.Refused

    assert ended <= child.ran <= ended + 1


@pytest.mark.parametrize(
    ("child", "size", "ended"),
    [
        (Decoder(), 0, 300),  # a drive that doesn't answer at all
        # one that stops answering part-way, whatever it did before
        (Decoder(speed=2, link=10 * MBPS, cores=0.1, stops=600), 10e9, 600 + 300),
        (Decoder(link=1024, stops=HOURS), 10e9, 305),  # or only just answers: 8 kbps
        # A file FFmpeg reads over and over: until it's been read three times, and the wait.
        (Decoder(link=10 * MBPS, cores=0.01, stops=10 * HOURS), 100e6, 3 * 80 + 307),
    ],
)
def test_a_file_that_stops_coming_is_given_up_for_now(outcome, child, size, ended):
    assert outcome(child, size) is audio.Stalled

    assert ended <= child.ran <= ended + 10


@pytest.mark.parametrize(
    ("child", "size"),
    [
        # An hour's 2 Mbps Teams recording on a mapped drive, over 2.5 Mbps: its audio comes
        # at 1.25 times real time.
        (Decoder(speed=1.25, link=2.5 * MBPS, cores=0.02, stops=HOURS / 1.25, after=None), 9e8),
        # 8 GB of video with its audio at the very end, over 10 Mbps
        (Decoder(link=10 * MBPS, cores=0.005, stops=8e9 / (10 * MBPS), after=None), 8e9),
        # A cloud file that takes four minutes to come down before any of it is read
        (Decoder(stops=240, after=None), 1e9),
        # 12 hours of audio at four times real time, on a slow PC
        (Decoder(speed=4, link=MBPS, cores=1.0, stops=3 * HOURS, after=None), 1e9),
    ],
)
def test_a_slow_file_that_keeps_coming_is_read_to_the_end(outcome, child, size):
    assert outcome(child, size) == 0


@pytest.mark.parametrize("after", [1.0, 0.0])
def test_the_pc_sleeping_meanwhile_doesnt_count(outcome, after):
    # The clock runs on while the PC sleeps; the child, its CPU time and the file don't.
    asleep = Decoder(speed=2, link=10 * MBPS, cores=0.1, stops=600, after=None, sleeps=300)
    assert outcome(asleep, 1e9) == 0 and asleep.now > 8 * HOURS

    limit = {1.0: 60, 0.0: 300}[after]  # nor for a file that hangs, or stalls
    hanging = Decoder(after=after, sleeps=limit / 2)
    assert outcome(hanging) is {1.0: audio.Refused, 0.0: audio.Stalled}[after]
    assert limit - 1 <= hanging.ran <= limit + 1


def test_audio_past_what_a_file_may_hold_buys_no_more_time(outcome, monkeypatch):
    monkeypatch.setattr(audio, "MAX_SECONDS", 100)
    child = Decoder(speed=1, cores=0.9, stops=10 * HOURS)  # half a second for each, then none

    assert outcome(child) is audio.Refused

    assert 120 <= child.ran <= 125  # 60 + 0.5 * 100 of CPU time


def test_the_job_counts_the_childs_cpu_time_and_what_it_reads(tmp_path: Path):
    (tmp_path / "call.mkv").write_bytes(bytes(2**20))
    python, env = decode_child._interpreter()
    read = "sys.stdin.read(); open(sys.argv[1], 'rb', buffering=0).read(2**20)"
    spin = "while time.process_time() < 1: pass"
    job = decode_child._Job()
    child = subprocess.Popen(
        [python, "-I", "-c", f"import sys, time; {read}\n{spin}", str(tmp_path / "call.mkv")],
        stdin=subprocess.PIPE,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        job.add(child.pid)
        child.stdin.close()
        assert eventually(lambda: job.used()[0] > 0.5)  # counted as it runs
        assert child.wait(30) == 0
        cpu, read = job.used()
        assert 0.9 < cpu < 10 and 2**20 <= read < 4 * 2**20
    finally:
        job.close()


def free_space(monkeypatch: pytest.MonkeyPatch, free: Callable[[], int]) -> None:
    """What Windows says is free, on any drive."""
    monkeypatch.setattr(shutil, "disk_usage", lambda folder: SimpleNamespace(free=free()))


def test_a_drive_that_is_nearly_full_already_isnt_written_to(
    tmp_path: Path, files, started, monkeypatch
):
    free_space(monkeypatch, lambda: audio.SPARE - 1)

    with pytest.raises(audio.NoRoom, match=r"^call\.wav needs at least 500 MB free on drive .:$"):
        decode_child.decode(recording(tmp_path / "call.wav"))

    assert started == [] and list(files.iterdir()) == []


def saying(path: Path, seconds: float) -> Path:
    """The FLAC file with a header that says it holds ``seconds``, whatever it holds."""
    data = bytearray(path.read_bytes())
    field = int.from_bytes(data[18:26], "big") & ~(2**36 - 1) | int(seconds * RATE)
    data[18:26] = field.to_bytes(8, "big")  # STREAMINFO's number of samples: its low 36 bits
    path.write_bytes(bytes(data))
    return path


def test_a_file_that_says_it_holds_more_than_there_is_room_for_isnt_decoded(
    tmp_path: Path, files, started, monkeypatch
):
    source = saying(recording(tmp_path / "workshop.flac"), 2 * 3600)
    free_space(monkeypatch, lambda: audio.SPARE + 100 * 2**20)  # two hours take 220 MB
    monkeypatch.setenv(decode_child.TEST_HOOK, "crash")  # if it decoded any of it

    with pytest.raises(audio.NoRoom) as refused:
        decode_child.decode(source)

    assert refused.value.needs == audio.SPARE + 2 * 3600 * RATE * 2
    assert str(refused.value).startswith("workshop.flac needs at least 720 MB free on drive ")
    assert not refused.value.over()
    (child,) = started
    assert not running(child.pid) and list(files.iterdir()) == []

    free_space(monkeypatch, lambda: audio.SPARE + 250 * 2**20)
    assert refused.value.over()
    monkeypatch.delenv(decode_child.TEST_HOOK)
    assert len(decode_child.decode(source)) / RATE > 0.9  # room for two hours: decoded


def test_a_decode_that_fills_the_drive_is_ended_with_space_to_spare(
    tmp_path: Path, files, started, monkeypatch
):
    # A file with no length, or a wrong one: the drive gets down to what's kept for a
    # recording while it's decoded.
    source = recording(tmp_path / "call.wav")
    monkeypatch.setenv(decode_child.TEST_HOOK, "stall")  # a minute's sleep after some audio
    free_space(monkeypatch, lambda: audio.SPARE + (-1 if written(files) else 2**30))
    began = time.monotonic()

    with pytest.raises(audio.NoRoom) as refused:
        decode_child.decode(source)

    assert time.monotonic() - began < 50  # when the drive got that full
    assert refused.value.needs > audio.SPARE  # more than there was: not tried again at once
    (child,) = started
    assert not running(child.pid) and list(files.iterdir()) == []


@pytest.mark.parametrize("full", [True, False])
def test_audio_that_cant_be_written_out_is_never_a_file_to_try_again(
    tmp_path: Path, files, monkeypatch, full: bool
):
    # Every write to the temporary file fails from some point on, as on a full drive.
    source = recording(tmp_path / "call.wav")
    monkeypatch.setenv(decode_child.TEST_HOOK, "unwritable")
    free_space(monkeypatch, lambda: audio.SPARE + (-1 if full and written(files) else 2**30))

    with pytest.raises((audio.NoRoom, RuntimeError)) as failed:  # not an OSError
        decode_child.decode(source)

    if full:
        assert isinstance(failed.value, audio.NoRoom) and failed.value.needs > audio.SPARE
    else:
        assert type(failed.value) is RuntimeError
        assert str(failed.value) == (
            "the audio of call.wav couldn't be written out: Bad file descriptor"
        )
    assert str(files) not in str(failed.value) and list(files.iterdir()) == []


def collect(folder: Path, errors: str = "", audio_bytes: bytes = b"", code: int = 0) -> np.ndarray:
    """What the engine makes of a child's two files and its exit code."""
    with tempfile.TemporaryFile(dir=folder) as out, tempfile.TemporaryFile(dir=folder) as said:
        out.write(audio_bytes)
        out.flush()
        said.write(errors.encode())
        return decode_child._collect(Path("call.wav"), out, said, 2**30, code)


def result(**fields) -> str:
    return f"{decode_child.RESULT}{json.dumps(fields)}\r\n"


def test_a_result_with_no_room_to_be_written_is_no_room_not_a_crash(tmp_path: Path, monkeypatch):
    free_space(monkeypatch, lambda: audio.SPARE - 1)

    with pytest.raises(audio.NoRoom) as refused:  # its child left no result
        collect(tmp_path, audio_bytes=bytes(4000), code=1)

    assert refused.value.needs == audio.SPARE + 8000


@pytest.mark.parametrize("size", [0, 2**30, 2**40, "a lot", None])
def test_a_child_that_lies_about_the_room_it_needs_isnt_believed(tmp_path: Path, size):
    # A child that runs code from a crafted file could ask to be tried again on every look.
    with pytest.raises(RuntimeError, match="^the decoder couldn't tell how long call.wav is$"):
        collect(tmp_path, result(result="no room", size=size))


def test_the_result_is_the_last_one_in_the_childs_errors(tmp_path: Path):
    samples = np.arange(4, dtype=np.int16)
    said = (
        "PyAV library exception being dropped:\r\n"
        + result(result="failed", message="not this one")
        + result(result="ok")
        + "Exception ignored in: <_io.TextIOWrapper name='<stdout>'>\r\n"
    )

    assert np.array_equal(collect(tmp_path, said, samples.tobytes()), samples)


@pytest.mark.parametrize(
    ("errors", "code", "error", "message"),
    [
        ("", 0xC0000005, audio.Refused, "call.wav crashed the decoder (0xC0000005)"),
        ("", 1, audio.Refused, "call.wav crashed the decoder (exit code 1)"),
        (
            "Traceback (most recent call last):\r\nMemoryError\r\n",
            1,
            RuntimeError,
            "the decoder stopped reading call.wav: MemoryError",
        ),
        (
            f"{decode_child.RESULT}[1, 2]\r\n",  # not a result
            0,
            RuntimeError,
            f"the decoder stopped reading call.wav: {decode_child.RESULT}[1, 2]",
        ),
    ],
)
def test_a_child_that_ends_without_a_result_says_why_if_it_can(
    tmp_path: Path, errors: str, code: int, error: type[Exception], message: str
):
    with pytest.raises(error) as failed:
        collect(tmp_path, errors, code=code)

    assert type(failed.value) is error and str(failed.value) == message


@pytest.mark.parametrize("size", [3, 13 * 3600 * RATE * 2])
def test_audio_that_isnt_what_a_file_can_hold_isnt_used(tmp_path: Path, monkeypatch, size: int):
    monkeypatch.setattr(decode_child, "_written", lambda out: size)

    with pytest.raises(RuntimeError, match="gave back audio for call.wav that can't be used$"):
        collect(tmp_path, result(result="ok"))


def test_what_ffmpeg_calls_a_permission_error_isnt_a_file_to_try_again(monkeypatch):
    # FFmpeg's word for some damaged AAC is EPERM, and PyAV makes that a PermissionError. The
    # watch folder would read such a file again every 30 s, as if it were in use.
    def damaged(source, allowed, starting, file):
        raise av.error.PermissionError(1, "Operation not permitted", "avcodec_send_packet()")
        yield

    monkeypatch.setattr(audio, "decode_pieces", damaged)
    assert isinstance(av.error.PermissionError(1, ""), PermissionError)

    assert decode_child._decode(Path("call.m4a"), io.BytesIO(), io.BytesIO(), 2**30) == {
        "result": "failed",
        "message": "the audio in call.m4a is damaged",
    }


ENGINE = """
import subprocess, sys
from pathlib import Path
from zachcap import decode_child

real = subprocess.Popen

def start(command, *args, **kwargs):
    child = real(command, *args, **kwargs)
    if decode_child.MODULE in command:
        print(child.pid, flush=True)
    return child

subprocess.Popen = start
decode_child.decode(Path(sys.argv[1]))
"""


@contextmanager
def another_engine(source: Path, files: Path, code: str = ENGINE, hook: str = "stall"):
    """An engine decoding ``source``, with the test's temp folder as its own. ``said`` reads
    the next line it prints."""
    python, env = decode_child._interpreter()
    temp = str(files.parent)
    env.update({decode_child.TEST_HOOK: hook, "TMP": temp, "TEMP": temp})
    engine = subprocess.Popen(
        [python, "-I", "-c", code, str(source)],
        stdout=subprocess.PIPE,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    def said(seconds: float = 30.0) -> str:
        lines: list[bytes] = []
        reader = threading.Thread(target=lambda: lines.append(engine.stdout.readline()))
        reader.daemon = True
        reader.start()
        reader.join(seconds)
        return lines[0].decode().strip()

    try:
        yield engine, said
    finally:
        engine.kill()
        engine.wait(10)


def test_the_decoder_and_its_audio_go_when_the_engine_does(tmp_path: Path, files):
    # An engine ended from Task Manager, or crashing, can't end its decoder or remove its
    # audio: Windows closes the engine's handle to the job, which ends the decoder, and deletes
    # the files with their last handle.
    source = recording(tmp_path / "call.wav")
    with another_engine(source, files) as (engine, said):
        decoder = int(said())
        assert running(decoder) and eventually(lambda: written(files) > 0)

        engine.kill()

        assert eventually(lambda: not running(decoder), seconds=10)
        assert eventually(lambda: list(files.iterdir()) == [], seconds=10)


def rights(pid: int) -> tuple[int, int]:
    """A process's integrity (0 untrusted, 0x1000 low, 0x2000 as programs have), and how many
    privileges it has."""
    windows, advapi32 = kernel32(), ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    advapi32.GetTokenInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    advapi32.GetTokenInformation.argtypes += [ctypes.c_uint32, ctypes.c_void_p]
    token, size = ctypes.c_void_p(), ctypes.c_uint32()
    process = ctypes.c_void_p(windows.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid))
    try:
        assert advapi32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token))
        try:
            told = []
            for kind in (TOKEN_INTEGRITY_LEVEL, TOKEN_PRIVILEGES):
                buffer = ctypes.create_string_buffer(4096)
                assert advapi32.GetTokenInformation(token, kind, buffer, 4096, ctypes.byref(size))
                told.append(buffer.raw[: size.value])
        finally:
            windows.CloseHandle(token)
    finally:
        windows.CloseHandle(process)
    # The label ends with its SID, and that with the level; the privileges start with how many.
    return int.from_bytes(told[0][-4:], "little"), int.from_bytes(told[1][:4], "little")


def test_the_child_has_no_rights_left_when_it_reads_the_file(tmp_path: Path, files):
    own_integrity, own_privileges = rights(os.getpid())
    assert own_integrity >= 0x2000 and own_privileges > 0

    with another_engine(recording(tmp_path / "call.wav"), files) as (engine, said):
        decoder = int(said())
        assert eventually(lambda: written(files) > 0)  # it has decoded some
        assert rights(decoder) == (0, 0)
        assert rights(engine.pid) == (own_integrity, own_privileges)


@pytest.mark.parametrize("what", ["a new file", "the recording itself", "the install"])
def test_code_in_the_child_cant_change_what_is_the_users(
    tmp_path: Path, files, monkeypatch, what: str
):
    # A crafted file that got its own code to run in the decoder would be held to this.
    source = recording(tmp_path / "call.wav")
    before = source.read_bytes()
    target = {
        "a new file": tmp_path / "planted.txt",
        "the recording itself": source,
        "the install": Path(decode_child.__file__).with_name("planted.py"),
    }[what]
    monkeypatch.setenv(decode_child.TEST_HOOK, f"write {target}")
    try:
        with pytest.raises(PermissionError):
            decode_child.decode(source)
        assert source.read_bytes() == before
        assert target == source or not target.exists()
    finally:
        if target != source:
            target.unlink(missing_ok=True)


def test_a_child_that_kept_its_rights_is_still_used_and_logged(tmp_path: Path, capsys):
    samples = np.arange(4, dtype=np.int16)
    kept = result(result="ok", kept="[WinError 5] Access is denied")

    assert np.array_equal(collect(tmp_path, kept, samples.tobytes()), samples)
    assert capsys.readouterr().out == (
        "The decoder kept the user's rights: [WinError 5] Access is denied\n"
    )

    collect(tmp_path, result(result="ok", kept=""), samples.tobytes())  # as a rule
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "refused",
    ["OpenProcessToken", "SetTokenInformation", "GetTokenInformation", "AdjustTokenPrivileges"],
)
def test_why_the_child_couldnt_give_up_its_rights_is_known(monkeypatch, refused: str):
    # Never the real ones here: they would take this process's rights.
    def answer(name: str):
        def call(*args) -> int:
            ctypes.set_last_error(5 if name == refused else 0)
            return name != refused

        return call

    names = ["OpenProcessToken", "SetTokenInformation", "GetTokenInformation"]
    fakes = {name: answer(name) for name in [*names, "AdjustTokenPrivileges"]}
    monkeypatch.setattr(decode_child, "_advapi32", lambda: SimpleNamespace(**fakes))

    assert decode_child._give_up_rights().startswith("[WinError 5] ")


def test_a_decode_under_way_in_another_process_is_left_alone(tmp_path: Path, files):
    # A second engine, or `zachcap import`: its files are in the same folder.
    with another_engine(recording(tmp_path / "call.wav"), files) as (engine, said):
        decoder = int(said())
        assert eventually(lambda: written(files) > 0)
        before = sorted(files.iterdir())

        decode_child.tidy()
        decode_child.decode(recording(tmp_path / "other.wav"))

        assert sorted(files.iterdir()) == before and len(before) == 2
        assert running(decoder) and engine.poll() is None


JUST_MADE = """
import tempfile, time
from zachcap import decode_child

with tempfile.TemporaryFile(dir=decode_child._root()):
    print("made", flush=True)
    time.sleep(60)
"""


def test_a_file_just_made_by_another_process_is_left_alone(tmp_path: Path, files):
    # Between making its file and starting its child: it is open already.
    with another_engine(tmp_path, files, JUST_MADE) as (_, said):
        assert said() == "made"
        (made,) = files.iterdir()
        decode_child.tidy()
        assert list(files.iterdir()) == [made]
    assert eventually(lambda: list(files.iterdir()) == [], seconds=10)


SEVERAL = """
import sys
from pathlib import Path
from zachcap import decode_child

for _ in range(4):
    print(len(decode_child.decode(Path(sys.argv[1]))), flush=True)
"""


def test_tidying_all_the_while_is_in_no_decodes_way(tmp_path: Path, files):
    source = recording(tmp_path / "call.wav", 2.0)
    done = threading.Event()
    tidied = []

    def tidy() -> None:
        while not done.is_set():
            decode_child.tidy()
            tidied.append(1)

    tidier = threading.Thread(target=tidy)
    tidier.start()
    try:
        with another_engine(source, files, SEVERAL, hook="") as (_, said):
            assert [said() for _ in range(4)] == ["32000"] * 4
    finally:
        done.set()
        tidier.join()
    assert len(tidied) > 100 and list(files.iterdir()) == []


def test_what_a_power_cut_left_behind_is_removed(tmp_path: Path, files):
    files.mkdir()
    for name in ("tmpaudio", "tmperrors"):  # no process has them open any more
        (files / name).write_bytes(bytes(64))

    decode_child.tidy()

    assert list(files.iterdir()) == []


def test_a_folder_an_older_zachcap_left_is_removed_but_not_one_it_is_using(tmp_path: Path, files):
    # ZachCap 0.3.0 had a folder for each decode, and left it when it was stopped meanwhile.
    files.mkdir()
    for name in ("old", "in-use", "just-made", "half-gone-left"):
        (files / name).mkdir()
        (files / name / "audio.f32").write_bytes(bytes(64))
    before = time.time() - decode_child.SETTLED - 60
    with open(files / "in-use" / "errors.txt", "wb"):  # its child's
        for name in ("old", "in-use"):
            os.utime(files / name, (before, before))

        decode_child.decode(recording(tmp_path / "call.wav"))  # the next decode tidies

        assert sorted(path.name for path in files.iterdir()) == ["in-use", "just-made"]
        assert (files / "in-use" / "audio.f32").exists()
    decode_child.tidy()
    assert [path.name for path in files.iterdir()] == ["just-made"]


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
    tmp_path: Path, files, monkeypatch, python: str | None, module: str, message: str
):
    source = recording(tmp_path / "call.wav")
    if python:
        env = decode_child._interpreter()[1]
        monkeypatch.setattr(decode_child, "_interpreter", lambda: (python, env))
    monkeypatch.setattr(decode_child, "MODULE", module)

    with pytest.raises(RuntimeError, match=message):  # not an OSError: not tried on every look
        decode_child.decode(source)

    assert list(files.iterdir()) == []


def test_a_file_in_use_elsewhere_is_still_one_to_try_again(tmp_path: Path, files):
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
