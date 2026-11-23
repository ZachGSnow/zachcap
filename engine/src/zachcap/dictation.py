"""Dictation: hold Ctrl+Alt+D, speak, let go, and what you said is typed where your cursor is.

For ticket notes, replies and chat while a call is on: transcribed on this computer by a
small Whisper model on the CPU (so it never waits for the GPU, which may be busy with a
meeting), spelled with your Words to know, then typed into the app in front as keystrokes
(the clipboard is used only if that app refuses typing). Audio is kept in memory only, never
saved.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from pathlib import Path

import numpy as np

RATE = 16_000
MAX_SECONDS = 120  # a hold longer than this is cut off: dictation is for notes, not meetings
ENGLISH_MODEL = "small.en"
ANY_LANGUAGE_MODEL = "small"
SETTING = "dictation"


def _sound(name: str) -> None:
    """Windows' own listening sounds, so you know it heard the keys (missing ones are
    skipped)."""
    path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Media" / name
    if not path.exists():
        return
    try:
        import winsound

        winsound.PlaySound(
            str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT
        )
    except (ImportError, RuntimeError):
        pass


class Microphone:
    """The mic, read into memory while the keys are held (a recording may have it open too:
    Windows shares it)."""

    def __init__(self, name: str | None = None) -> None:
        import pyaudiowpatch as pyaudio

        from .capture import _find_device

        self._pyaudio = pyaudio
        self._pa = pyaudio.PyAudio()
        try:
            wasapi = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            device = _find_device(self._pa, wasapi, name, "input") or (
                self._pa.get_device_info_by_index(wasapi["defaultInputDevice"])
            )
            self.rate = int(device["defaultSampleRate"])
            self.channels = int(device["maxInputChannels"])
            self.name = device["name"]
            self._chunks: list[bytes] = []
            self._limit = MAX_SECONDS * self.rate * self.channels * 2
            self._size = 0
            self._stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.rate,
                input=True,
                input_device_index=device["index"],
                frames_per_buffer=1024,
                stream_callback=self._callback,
            )
        except BaseException:
            self._pa.terminate()
            raise

    def _callback(self, in_data, frame_count, time_info, status):
        if self._size < self._limit:
            self._chunks.append(in_data)
            self._size += len(in_data)
        return (None, self._pyaudio.paContinue)

    def stop(self) -> np.ndarray:
        """Stops and returns what was heard, as 16 kHz mono float32."""
        try:
            self._stream.stop_stream()
            self._stream.close()
        finally:
            self._pa.terminate()
        return to_16k_mono(b"".join(self._chunks), self.channels, self.rate)


def to_16k_mono(data: bytes, channels: int, rate: int) -> np.ndarray:
    if not data:
        return np.zeros(0, np.float32)
    samples = np.frombuffer(data, np.int16).reshape(-1, channels).mean(axis=1) / 32768.0
    if rate == RATE:
        return samples.astype(np.float32)
    from math import gcd

    from scipy.signal import resample_poly

    common = gcd(rate, RATE)
    return resample_poly(samples, RATE // common, rate // common).astype(np.float32)


class Transcriber:
    """A small Whisper model on the CPU, loaded once and kept (about 0.5 GB of memory)."""

    def __init__(self) -> None:
        self._models: dict[str, object] = {}
        self._lock = threading.Lock()

    def _model(self, name: str):
        with self._lock:
            if name not in self._models:
                from faster_whisper import WhisperModel

                threads = max(2, (os.cpu_count() or 4) // 2)
                self._models[name] = WhisperModel(
                    name, device="cpu", compute_type="int8", cpu_threads=threads
                )
            return self._models[name]

    def warm_up(self, language: str | None) -> None:
        """Loads the model in the background, so the first dictation is quick. A failure
        here (no speech stack installed, no network for the first download) is left for the
        first dictation to report."""

        def load() -> None:
            try:
                self._model(model_for(language))
            except Exception:
                pass

        threading.Thread(target=load, name="dictation-model", daemon=True).start()

    def text(self, audio: np.ndarray, language: str | None, hotwords: str | None) -> str:
        if audio.size < RATE // 4:  # under a quarter second: nothing said
            return ""
        model = self._model(model_for(language))
        segments, _ = model.transcribe(
            audio,
            language=language,
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
            hotwords=hotwords or None,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()


def model_for(language: str | None) -> str:
    return ENGLISH_MODEL if language in (None, "en") else ANY_LANGUAGE_MODEL


# Typing ------------------------------------------------------------------------------

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_RETURN = 0x0D


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUTUNION(ctypes.Union):
    # The largest member (MOUSEINPUT) sets the size; only the keyboard member is used.
    _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


def key_events(text: str) -> list[INPUT]:
    """Keystrokes that type ``text`` as characters (any language, no keyboard layout
    involved); a line break is the Enter key."""
    events = []
    for line_index, line in enumerate(text.split("\n")):
        if line_index:
            for flags in (0, KEYEVENTF_KEYUP):
                events.append(
                    INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(VK_RETURN, 0, flags, 0, 0)))
                )
        units = line.encode("utf-16-le")
        for index in range(0, len(units), 2):
            code = int.from_bytes(units[index : index + 2], "little")
            for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                events.append(
                    INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(0, code, flags, 0, 0)))
                )
    return events


MODIFIER_KEYS = (0x10, 0x11, 0x12, 0x5B, 0x5C)  # Shift, Ctrl, Alt, left and right Windows


def modifiers_released(timeout: float = 2.0) -> bool:
    """Waits until no modifier key is held: typed with Ctrl or Alt still down (the dictation
    keys, let go one at a time), the characters would be shortcuts in the app in front."""
    deadline = time.monotonic() + timeout
    held = ctypes.windll.user32.GetAsyncKeyState
    while any(held(key) & 0x8000 for key in MODIFIER_KEYS):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


def type_text(text: str) -> int:
    """Types ``text`` into the window in front, once no modifier key is held. Returns how many
    key events Windows took (none when the window in front runs as administrator and refuses
    them, or a modifier stayed held)."""
    events = key_events(text)
    if not events or not modifiers_released():
        return 0
    array = (INPUT * len(events))(*events)
    return ctypes.windll.user32.SendInput(len(events), array, ctypes.sizeof(INPUT))


CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def copy_to_clipboard(text: str) -> bool:
    """Puts ``text`` on the clipboard: only when typing was refused (a window running as
    administrator), so it isn't lost."""
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    data = text.encode("utf-16-le") + b"\0\0"
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        pointer = kernel32.GlobalLock(handle)
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        return bool(user32.SetClipboardData(CF_UNICODETEXT, handle))
    finally:
        user32.CloseClipboard()


class Dictation:
    """One dictation at a time: start when the keys go down, finish when they come up."""

    def __init__(self, transcriber: Transcriber | None = None, microphone=None) -> None:
        self.transcriber = transcriber or Transcriber()
        self._microphone = microphone  # None: the real one (looked up when used)
        self._mic = None
        self._started = 0.0
        self._lock = threading.Lock()

    @property
    def listening(self) -> bool:
        return self._mic is not None

    def start(self, mic_name: str | None = None) -> None:
        with self._lock:
            if self._mic is not None:
                raise RuntimeError("Already listening")
            self._mic = (self._microphone or Microphone)(mic_name)
            self._started = time.perf_counter()
        _sound("Speech On.wav")

    def finish(
        self, language: str | None, hotwords: str | None, fix=None, type_it: bool = True
    ) -> dict:
        """Stops listening, transcribes, applies the Words to know corrections (``fix``)
        and types the text. Returns the text and timings."""
        with self._lock:
            mic, self._mic = self._mic, None
        if mic is None:
            raise RuntimeError("Not listening")
        audio = mic.stop()
        _sound("Speech Off.wav")
        began = time.perf_counter()
        text = self.transcriber.text(audio, language, hotwords)
        if fix is not None and text:
            text = fix(text)
        typed = type_text(text + " ") if text and type_it else 0
        # Typing refused (the window in front runs as administrator): copied instead.
        copied = bool(text and type_it and typed == 0 and copy_to_clipboard(text))
        return {
            "text": text,
            "seconds": round(audio.size / RATE, 1),
            "took": round(time.perf_counter() - began, 2),
            "typed": typed,
            "copied": copied,
        }

    def cancel(self) -> None:
        with self._lock:
            mic, self._mic = self._mic, None
        if mic is not None:
            mic.stop()
