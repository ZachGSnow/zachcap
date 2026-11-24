"""The live transcript's speech models run in a child process: one that crashes, hangs or is
killed never takes the engine (and its recording) with it, and it goes when the engine does.

The processes are real here; the models in them are stand-ins (CI has no GPU stack)."""

import ctypes
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import zachcap
from zachcap import childproc, cuda, live, live_child, models
from zachcap.live import RATE, GpuCheck, GpuTurns, Live, Speech

SYNCHRONIZE, WAIT_TIMEOUT = 0x00100000, 0x102

# The child as the engine starts it, with stand-ins for the models. The first sample of the
# audio says how it behaves.
STAND_IN = """
import ctypes, sys, time
sys.path.insert(0, sys.argv.pop(1))  # this zachcap, whichever one this Python has installed
from types import SimpleNamespace
from zachcap import cuda, live_child, models

def behave(audio):
    if audio[0] == 1:  # an access violation, as a driver can have
        ctypes.c_int.from_address(0).value = 0
    if audio[0] == 2:
        time.sleep(60)
    if audio[0] == 3:
        raise RuntimeError("CUDA failed with error out of memory")

def find(audio):
    print("a library that prints to the standard output", flush=True)
    behave(audio)
    return [(0, len(audio))]

class Model:
    def __init__(self, name, **options):
        self.name = name

    def transcribe(self, audio, **options):
        behave(audio)
        said = f"{self.name}: {len(audio)} samples, caf\\u00e9 {' '.join(sys.argv[1:])}"
        return [SimpleNamespace(text=said, avg_logprob=-0.2)], None

def download(name):
    time.sleep(60)

live_child.find_speech = find
models.whisper, models.download_whisper = Model, download
cuda.prepare = lambda: True
live_child.main()
"""


def kernel32() -> ctypes.WinDLL:
    """The test's own kernel32, with the result types it needs."""
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.OpenProcess.restype = ctypes.c_void_p
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


def eventually(check: Callable[[], bool], seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


@pytest.fixture
def stand_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Children run the stand-in."""
    script = tmp_path / "stand_in.py"
    script.write_text(STAND_IN, encoding="utf-8")
    run = [str(script), str(Path(zachcap.__file__).parents[1])]
    monkeypatch.setattr(live_child, "RUN", run)
    return run


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[live_child.Child]:
    """The children started, ended at the end whatever happens."""
    children: list[live_child.Child] = []
    real = live_child.Child.__init__

    def init(self, *args: str) -> None:
        real(self, *args)
        children.append(self)

    monkeypatch.setattr(live_child.Child, "__init__", init)
    yield children
    for child in children:
        child.end()


def clip(first: float = 0.0, seconds: float = 1.0) -> np.ndarray:
    audio = np.zeros(int(seconds * RATE), np.float32)
    audio[0] = first
    return audio


CRASHES, HANGS, FAILS = 1.0, 2.0, 3.0


# The engine's side ----------------------------------------------------------------------


def test_the_child_finds_speech_and_hears_it(stand_in, started):
    speech = Speech("en", None)
    assert speech.ready()
    window = clip(seconds=30)  # the most the worker looks at
    assert speech.find(window) == [(0, len(window))]
    heard = speech(clip(), "mic")
    # What libraries print isn't taken for an answer, and the text comes through whole.
    assert heard == "small.en: 16000 samples, caf\u00e9" and not heard.doubtful
    [child] = started
    assert running(child.pid)
    speech.close()
    assert not running(child.pid)


def test_the_gpus_child_is_told_so(stand_in, started):
    check = GpuCheck()
    check._usable = True
    speech = Speech("en", None, gpu=GpuTurns(), check=check)
    assert speech.ready()
    assert speech(clip(), "system") == "large-v3-turbo: 16000 samples, caf\u00e9 cuda"


def test_its_threads_dont_spin_for_cores_the_call_needs(monkeypatch: pytest.MonkeyPatch):
    asked: list[dict] = []

    def popen(command, **options):
        asked.append(options["env"])
        raise OSError("not started")

    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(OSError, match="not started"):
        live_child.Child()
    assert asked[0]["KMP_BLOCKTIME"] == "0" and asked[0]["OPENBLAS_NUM_THREADS"] == "1"


@pytest.mark.parametrize("ask", ["find", "__call__"])
def test_a_crash_ends_only_the_child(stand_in, started, ask: str):
    speech = Speech("en", None)
    assert speech.ready()

    def asked(audio: np.ndarray):
        return speech.find(audio) if ask == "find" else speech(audio, "mic")

    with pytest.raises(RuntimeError, match=r"stopped \(0xC0000005\) and is starting again"):
        asked(clip(CRASHES))
    assert not running(started[0].pid)
    assert asked(clip()) and len(started) == 2  # a new one, at once


def test_a_hang_is_ended_at_its_limit(stand_in, started, monkeypatch: pytest.MonkeyPatch):
    speech = Speech("en", None)
    assert speech.ready() and speech(clip(), "mic")  # it's up, and its model is loaded
    monkeypatch.setattr(live, "ANSWER", 1.0)
    began = time.monotonic()
    with pytest.raises(RuntimeError, match="no answer for 1 seconds"):
        speech(clip(HANGS), "mic")
    assert time.monotonic() - began < 15
    assert not running(started[0].pid)
    monkeypatch.setattr(live, "ANSWER", 60.0)
    assert speech(clip(), "mic")


def test_one_that_hangs_without_reading_is_ended_too(stand_in, started):
    child = live_child.Child()
    child.send("find", clip(HANGS))
    for _ in range(4):  # more than a pipe holds: the engine never waits for it to be read
        child.send("find", clip(seconds=30))
    assert child.answer(0.3) is None
    began = time.monotonic()
    child.end()
    assert time.monotonic() - began < 10 and not running(child.pid)


def test_a_failing_line_gets_a_fresh_child(stand_in, started):
    speech = Speech("en", None)
    assert speech.ready()
    with pytest.raises(RuntimeError, match="out of memory"):
        speech(clip(FAILS), "mic")
    assert not running(started[0].pid)
    assert speech(clip(), "mic") and len(started) == 2


def test_a_child_killed_mid_call_is_a_notice_and_lines_go_on(stand_in, started, monkeypatch):
    monkeypatch.setattr(live, "TICK", 0.02)
    transcript = Live(Speech("en", None))
    transcript.start()
    frame = 0

    def feed() -> None:
        nonlocal frame
        audio = np.concatenate([0.1 * np.ones(RATE, np.float32), np.zeros(RATE, np.float32)])
        for i in range(0, len(audio), 1600):
            transcript.feed("system", audio[i : i + 1600], RATE, frame)
            frame += 1600

    # Speech is found to the end of what's fed (the stand-in), so a line needs LONGEST.
    monkeypatch.setattr(live, "LONGEST", 1.0)
    feed()
    assert eventually(transcript.lines)
    started[0]._process.kill()  # from Task Manager, say
    feed()
    assert eventually(lambda: transcript.error is not None or len(started) == 2)
    count = len(transcript.lines())
    feed()
    assert eventually(lambda: len(transcript.lines()) > count)
    assert transcript.error is None and len(started) == 2
    transcript.stop()
    assert transcript.join(20) and not running(started[1].pid)


ENGINE = """
import sys, time
sys.path.insert(0, sys.argv[2])
import numpy as np
from zachcap import live_child
live_child.RUN = sys.argv[1:3]
child = live_child.Child()
child.send("find", np.full(16000, 2, np.float32))  # it hangs, so it won't see its stdin close
print(child.pid, flush=True)
time.sleep(60)
"""


@pytest.mark.parametrize("hangs", [True, False])
def test_the_child_goes_when_the_engine_does(stand_in, hangs: bool):
    # Ended from Task Manager, or crashing, the engine can't end it: Windows closes the
    # engine's handle to the job, and that does.
    python, env = childproc.interpreter()
    code = ENGINE if hangs else ENGINE.replace("np.full(16000, 2", "np.full(16000, 0")
    engine = subprocess.Popen(
        [python, "-I", "-c", code, *stand_in],
        stdout=subprocess.PIPE,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        said: list[bytes] = []
        reader = threading.Thread(target=lambda: said.append(engine.stdout.readline()))
        reader.daemon = True
        reader.start()
        reader.join(30)
        child = int(said[0])
        time.sleep(1.0)  # hanging by now
        assert running(child)
        engine.kill()
        assert eventually(lambda: not running(child), seconds=10)
    finally:
        engine.kill()
        engine.wait(10)


def test_a_child_gets_the_real_python_and_none_of_the_engines_secrets(monkeypatch):
    venv = r"C:\Users\Public\venv\Scripts\python.exe"
    base = r"C:\Users\Public\Python312\python.exe"
    monkeypatch.setattr(sys, "executable", venv)
    monkeypatch.setattr(sys, "_base_executable", base)
    monkeypatch.setenv("ZACHCAP_TOKEN", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("ZACHCAP_HOME", r"C:\Users\Public\ZachCap")

    python, env = childproc.interpreter()

    # The venv's launcher would be a second process: the real Python runs as the venv's.
    assert python == base and env["__PYVENV_LAUNCHER__"] == venv
    assert "ZACHCAP_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
    assert env["ZACHCAP_HOME"] == r"C:\Users\Public\ZachCap"


def test_one_that_cant_be_started_leaves_nothing_behind(stand_in, monkeypatch):
    def refused(self, pid: int) -> None:
        pids.append(pid)
        raise OSError("not allowed in a job")

    pids: list[int] = []
    monkeypatch.setattr(childproc.Job, "add", refused)
    with pytest.raises(OSError, match="not allowed in a job"):
        live_child.Child()
    assert not running(pids[0])
    env = dict(os.environ)
    monkeypatch.setattr(childproc, "interpreter", lambda: (r"C:\Users\Public\none.exe", env))
    with pytest.raises(OSError):
        live_child.Child()


# The GPU check, and downloads ------------------------------------------------------------


def test_the_nvidia_driver_is_looked_for_without_loading_anything(tmp_path, monkeypatch):
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    assert not cuda.driver()
    (tmp_path / "System32").mkdir()
    (tmp_path / "System32" / "nvcuda.dll").touch()
    assert cuda.driver()


def test_with_no_nvidia_driver_theres_no_gpu_and_no_child(started, monkeypatch):
    monkeypatch.setattr(cuda, "driver", lambda: False)
    assert live_child.gpu_usable() is False and started == []


def test_with_a_driver_a_child_finds_out_and_goes(stand_in, started, monkeypatch):
    monkeypatch.setattr(cuda, "driver", lambda: True)
    assert live_child.gpu_usable() is True
    assert len(started) == 1 and not running(started[0].pid)


def test_a_driver_that_crashes_what_asks_it_means_no_gpu(tmp_path, started, monkeypatch):
    script = tmp_path / "crashes.py"
    script.write_text("import ctypes; ctypes.c_int.from_address(0).value = 0", encoding="utf-8")
    monkeypatch.setattr(live_child, "RUN", [str(script)])
    monkeypatch.setattr(cuda, "driver", lambda: True)
    check = GpuCheck()
    assert check.known() is None
    assert check.look() is False and check.known() is False
    assert len(started) == 1 and not running(started[0].pid)


def test_a_download_is_stopped_at_once_and_started_again(stand_in, started):
    download = live_child.Download()
    download.start("small.en")
    assert eventually(lambda: len(started) == 1 and running(started[0].pid))
    download.start("small.en")  # under way already
    began = time.monotonic()
    download.stop()  # a recording starts: it doesn't wait
    assert time.monotonic() - began < 0.1
    assert eventually(lambda: not running(started[0].pid), seconds=5)
    download.join(5)
    download.start("small.en")
    assert eventually(lambda: len(started) == 2 and running(started[1].pid))
    download.stop()
    download.join(10)


# The child's side ------------------------------------------------------------------------


class FakeModel:
    loaded: list = []
    said: dict = {"logprob": -0.2}
    asked: list = []

    def __init__(self, name, **options) -> None:
        FakeModel.loaded.append((name, options))

    def transcribe(self, audio, **options):
        FakeModel.asked.append(options)
        segment = SimpleNamespace(
            text=" Thank you.", no_speech_prob=0.0, avg_logprob=FakeModel.said["logprob"]
        )
        return [segment], None


HEAR = {
    "do": "hear",
    "model": "small.en",
    "options": {"device": "cpu", "compute_type": "int8", "cpu_threads": 3},
    "language": "en",
    "hotwords": "Contoso",
}


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> live_child.Server:
    FakeModel.loaded, FakeModel.asked, FakeModel.said = [], [], {"logprob": -0.2}
    monkeypatch.setattr(models, "whisper", FakeModel)
    return live_child.Server()


def test_the_model_is_loaded_once_and_only_from_this_pc(server):
    for _ in range(2):
        answer = server.answer(HEAR, clip())
        assert answer == {"text": "Thank you.", "doubtful": False}
    options = {"device": "cpu", "compute_type": "int8", "cpu_threads": 3, "local_files_only": True}
    assert FakeModel.loaded == [("small.en", options)]


def test_a_line_takes_one_pass(server):
    server.answer(HEAR, clip())
    # Left to itself, faster-whisper tries up to five more temperatures when in doubt.
    [asked] = FakeModel.asked
    assert asked["temperature"] == 0 and asked["beam_size"] == 1
    assert asked["language"] == "en" and asked["hotwords"] == "Contoso"


def test_a_line_is_doubtful_only_when_whisper_and_silero_both_doubt_it(server, monkeypatch):
    asked: list = []
    peak = {"now": 0.5}
    FakeModel.said["logprob"] = -0.9
    monkeypatch.setattr(live_child, "speech_peak", lambda audio: asked.append(1) or peak["now"])
    assert server.answer(HEAR, clip())["doubtful"]  # unlikely words, and not speech
    peak["now"] = 0.95
    assert not server.answer(HEAR, clip())["doubtful"]  # Silero is sure: someone said it
    FakeModel.said["logprob"], peak["now"] = -0.2, 0.5
    asked.clear()
    assert not server.answer(HEAR, clip())["doubtful"] and asked == []  # no need to ask


@pytest.mark.parametrize(
    ("error", "load"),
    [
        (FileNotFoundError("not in the cache, and downloads are off"), "missing"),
        (RuntimeError("Unable to open file 'model.bin' in model"), "failed"),  # cut off
    ],
)
def test_a_model_that_doesnt_load_is_said_as_such(server, monkeypatch, error, load: str):
    def fails(name, **options):
        raise error

    monkeypatch.setattr(models, "whisper", fails)
    assert server.answer(HEAR, clip()) == {"load": load, "error": str(error)}


def test_what_goes_wrong_is_an_answer_too(server, monkeypatch):
    def fails(audio):
        raise ValueError("no such thing")

    monkeypatch.setattr(live_child, "find_speech", fails)
    assert server.answer({"do": "find"}, clip()) == {"error": "no such thing"}
    assert "error" in server.answer({"do": "dance"}, clip())
    monkeypatch.setattr(models, "download_whisper", lambda name: fails(None))
    assert server.answer({"do": "fetch", "model": "small.en"}, clip()) == {"error": "no such thing"}
