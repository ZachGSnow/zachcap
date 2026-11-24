"""The live transcript's speech models run here, in a child process of the engine.

The recorder runs in the engine's process, so nothing may end that. A speech model on the GPU
can: CTranslate2 frees GPU memory in a destructor, where a driver's error is std::terminate,
the end of the whole process, mid-call. In a process of its own that's a notice on the panel.
Loading the models stays out of the engine too (it held up the recorder's threads for up to
0.2 s at a time, and torch alone takes 0.8 GB), and ending the process is the one sure way to
give back all of its memory, the GPU's included.

The child is the engine's own Python running this module, isolated (``-I``), in a Job Object
(childproc): it goes when the engine does, however that stops, and it ends by itself when the
engine closes its stdin. It starts with "cuda" on its command line when it will use the GPU.

It's asked one thing at a time, on its stdin: a line of JSON ("do" says what, "samples" how
much audio comes after it, as 16 kHz float32), and answers each on its stdout with a line of
JSON, "error" in it when it couldn't. Whatever else it prints goes to the engine's log.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import subprocess
import sys
import threading
import traceback
from typing import BinaryIO

import numpy as np

from . import cuda, models

RUN = ["-m", "zachcap.live_child"]  # what the child runs: this module (tests, stand-ins)
LONGEST_ANSWER = 1 << 20  # bytes of an answer; a line is a few hundred
GONE = 10.0  # the most to wait for an ended child to be gone (a GPU driver can hold it)
GPU_CHECK = 120.0  # seconds to find out about the GPU (torch's first import is slow)


# The engine's side -------------------------------------------------------------------------


class Gone(RuntimeError):
    """The child isn't there any more, or what it says makes no sense: why, in a few words."""


class Child:
    """A speech child: started, asked one thing at a time, and ended. Used by one thread."""

    def __init__(self, *args: str) -> None:
        """Starts one. Raises OSError when it can't."""
        from . import childproc  # imported here: the child never needs it

        python, env = childproc.interpreter()
        # numpy's OpenBLAS sets memory aside for a thread per core when it loads (about 350 MB
        # on a 12-thread PC), and the little maths done with it here needs one.
        env["OPENBLAS_NUM_THREADS"] = "1"
        # Whisper's threads (OpenMP) spin for 0.2 s after each step unless told not to. With
        # every core busy, a line of 8 s took 100 s or more on the CPU, and takes 5 without.
        env["KMP_BLOCKTIME"] = "0"
        self._job = childproc.Job()
        try:
            self._process = subprocess.Popen(
                [python, "-I", "-X", "utf8", *RUN, *args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None if sys.stderr is not None else subprocess.DEVNULL,
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except OSError:
            self._job.close()
            raise
        self._requests: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self._answers: queue.SimpleQueue[bytes] = queue.SimpleQueue()
        self._talker = threading.Thread(target=self._talk, name="live-speech", daemon=True)
        self._talker.start()
        try:
            # In its job before it's asked anything.
            self._job.add(self._process.pid)
        except OSError:
            self._process.kill()
            self.end()
            raise

    @property
    def pid(self) -> int:
        return self._process.pid

    def send(self, do: str, audio: np.ndarray | None = None, **more) -> None:
        """Asks it something. Never waits: what it says is in answer."""
        data = b"" if audio is None else np.asarray(audio, "<f4").tobytes()
        head = json.dumps({"do": do, "samples": len(data) // 4, **more})
        self._requests.put(head.encode() + b"\n" + data)

    def answer(self, timeout: float) -> dict | None:
        """What it says to the last thing it was asked; None when it hasn't within ``timeout``
        seconds. Raises Gone when it won't any more."""
        try:
            line = self._answers.get(timeout=timeout)
        except queue.Empty:
            return None
        try:
            answer = json.loads(line)
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            raise Gone(self._why())
        return answer

    def end(self) -> None:
        """Ends it, and waits until it's gone: all its memory is free then, the GPU's too."""
        self._job.end()
        try:
            self._process.wait(GONE)
        except subprocess.TimeoutExpired:
            pass  # held by a driver: it goes when that lets go of it
        self._requests.put(None)
        self._talker.join(GONE)
        if not self._talker.is_alive():
            for pipe in (self._process.stdin, self._process.stdout):
                try:
                    pipe.close()
                except OSError:  # what it hadn't read of a request
                    pass
        self._job.close()

    def _talk(self) -> None:
        """Passes what's asked on to the child, and its answers back, until it's gone (on a
        thread of its own: a child that hangs mustn't hang who asks)."""
        while (request := self._requests.get()) is not None:
            try:
                self._process.stdin.write(request)
                self._process.stdin.flush()
                line = self._process.stdout.readline(LONGEST_ANSWER)
            except (OSError, ValueError):
                line = b""
            self._answers.put(line)

    def _why(self) -> str:
        """How it ended, for the panel."""
        try:
            code = self._process.wait(1.0)
        except subprocess.TimeoutExpired:
            return "it said something that can't be read"
        return f"0x{code:08X}" if code >= 0x80000000 else f"exit code {code}"


def gpu_usable() -> bool:
    """Whether a CUDA GPU is usable, found out in a child: importing torch takes seconds and
    0.8 GB, and a broken driver can crash what asks it. With no NVIDIA driver there's none."""
    if not cuda.driver():
        return False
    child = Child("cuda")
    try:
        child.send("gpu")
        answer = child.answer(GPU_CHECK)
        return answer is not None and answer.get("usable") is True
    finally:
        child.end()


class Download:
    """Downloads a speech model between calls, in a child: stopped when a recording starts
    (the call needs the bandwidth), it carries on where it was the next time."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, model: str) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(model, self._stop), name="live-model", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Ends the one under way, if any, without waiting for it."""
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        """Waits for the one under way to end (tests)."""
        if self._thread is not None:
            self._thread.join(timeout)

    @staticmethod
    def _run(model: str, stop: threading.Event) -> None:
        try:
            child = Child()
        except OSError:
            traceback.print_exc()
            return
        try:
            child.send("fetch", model=model)
            # An error (no network, say) is in the log: tried again after the next call.
            while not stop.is_set() and child.answer(0.2) is None:
                pass
        except Gone:
            pass
        finally:
            child.end()


# The child ---------------------------------------------------------------------------------

# A line is doubtful when two judges agree it isn't speech: Whisper, its words this unlikely
# in every segment (avg_logprob; its no_speech_prob is no help, always 0 with large-v3-turbo),
# and Silero VAD, never this sure of speech anywhere in the clip. Measured on a laptop whose
# audio chip cancels echo (2026-11-24, large-v3-turbo and small.en): what got through of the
# call into the mic came out as "Thank you.", "Hello.", "Ready?" and the like, at -0.54 to
# -1.31 and Silero 0.70 at most; "Yes.", "No.", "Thanks.", "Okay." said into the mic, alone
# or over the call and as quietly as that leak, at -0.05 to -0.64 and Silero 0.90 or more.
DOUBT_LOGPROB = -0.5
SURE_SPEECH = 0.8


def speech_peak(audio: np.ndarray) -> float:
    """How sure Silero VAD is, at its surest (each 32 ms), that 16 kHz audio has speech."""
    from faster_whisper.vad import get_vad_model

    if len(audio) < 512:
        return 0.0
    padded = np.pad(audio, (0, -len(audio) % 512))
    return float(np.max(get_vad_model()(padded)))


def find_speech(audio: np.ndarray) -> list[tuple[int, int]]:
    """Where people speak in 16 kHz audio, as (start, end) samples (Silero VAD)."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    if len(audio) < 512:
        return []
    found = get_speech_timestamps(
        audio,
        VadOptions(min_silence_duration_ms=300, speech_pad_ms=100, min_speech_duration_ms=200),
    )
    return [(int(span["start"]), int(span["end"])) for span in found]


class Server:
    """What the child does when asked. Its Whisper model is loaded with the first line."""

    def __init__(self) -> None:
        self._model = None

    def answer(self, request: dict, audio: np.ndarray) -> dict:
        try:
            if request["do"] == "find":
                return {"spans": find_speech(audio)}
            if request["do"] == "hear":
                return self.hear(request, audio)
            if request["do"] == "gpu":
                return {"usable": cuda.prepare()}
            if request["do"] == "fetch":
                models.download_whisper(request["model"])
                return {}
            return {"error": f"it doesn't know {request['do']!r}"}
        except Exception as exc:
            _log()
            return {"error": _said(exc)}

    def hear(self, request: dict, audio: np.ndarray) -> dict:
        """The text of a stretch of speech, and whether it's doubtful that it was speech at
        all (DOUBT_LOGPROB). A model that doesn't load is said as "load": whether it's missing
        from this PC, or why else. Only what's on this PC is loaded: a download now would take
        the call's bandwidth."""
        if self._model is None:
            try:
                self._model = models.whisper(
                    request["model"], local_files_only=True, **request["options"]
                )
            except FileNotFoundError as exc:
                return {"load": "missing", "error": _said(exc)}
            except Exception as exc:
                _log()
                return {"load": "failed", "error": _said(exc)}
        segments, _ = self._model.transcribe(
            audio,
            language=request["language"],
            beam_size=1,
            temperature=0.0,  # one pass: faster-whisper tries up to five more when in doubt
            vad_filter=False,  # already cut at the speech
            condition_on_previous_text=False,
            without_timestamps=True,
            hotwords=request["hotwords"],
        )
        segments = list(segments)
        doubtful = (
            bool(segments)
            and all(segment.avg_logprob <= DOUBT_LOGPROB for segment in segments)
            and speech_peak(audio) < SURE_SPEECH
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return {"text": text, "doubtful": doubtful}


def _said(exc: Exception) -> str:
    """An error's words, as many as the panel needs."""
    return (str(exc) or type(exc).__name__)[:500]


def _log() -> None:
    """The error being handled, in the engine's log."""
    with contextlib.suppress(OSError):  # it has none, or it's gone
        traceback.print_exc()


def _answers() -> BinaryIO:
    """This process's standard output, for the answers alone: whatever else is printed (by the
    speech libraries) goes to its errors."""
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    return out


def main() -> None:
    """The child: answers what it's asked until the engine closes its stdin."""
    requests, answers, server = sys.stdin.buffer, _answers(), Server()
    if sys.argv[1:] == ["cuda"]:
        cuda.prepare()  # before anything imports faster_whisper
    while line := requests.readline():
        request = json.loads(line)
        data = requests.read(4 * request["samples"])
        if len(data) < 4 * request["samples"]:
            return  # the engine went away
        answer = server.answer(request, np.frombuffer(data, "<f4").copy())
        answers.write(json.dumps(answer).encode() + b"\n")
        answers.flush()


if __name__ == "__main__":
    main()
