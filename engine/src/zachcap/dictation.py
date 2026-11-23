"""Dictation: hold Ctrl+Alt+D, speak, let go, and what you said is typed where your cursor is.

For ticket notes, replies and chat while a call is on: transcribed on this computer by a
small Whisper model on the CPU (so it never waits for the GPU, which may be busy with a
meeting), spelled with your Words to know, then typed as keystrokes into the window that was
in front when you let go. When it can't be typed there (you switched windows, or that window
runs as administrator), it goes on the clipboard instead, so it's never lost. Audio is kept
in memory only, never saved.
"""

from __future__ import annotations

import ctypes
import functools
import itertools
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import numpy as np

RATE = 16_000
MAX_SECONDS = 120  # a hold longer than this is cut off: dictation is for notes, not meetings
GRACE_SECONDS = 60  # no key-up this long after the cut-off: it was lost, so the mic is closed
MAX_AT_ONCE = 2  # a quick second dictation may start while the first is still being finished
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
        if self._size >= self._limit:  # held past MAX_SECONDS: stop listening, keep what was said
            return (None, self._pyaudio.paComplete)
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


# Windows ------------------------------------------------------------------------------


@functools.cache
def _windows() -> SimpleNamespace:
    """This module's own copies of the Windows libraries, with argument and result types set
    here (set on the shared ``ctypes.windll`` ones, they would change other modules' calls)."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    BOOL, DWORD, HANDLE, HWND = wintypes.BOOL, wintypes.DWORD, wintypes.HANDLE, wintypes.HWND
    pointer, c_int = ctypes.POINTER, ctypes.c_int
    for function, argtypes, restype in (
        (user32.GetForegroundWindow, [], HWND),
        (user32.GetWindowThreadProcessId, [HWND, pointer(DWORD)], DWORD),
        (user32.GetAsyncKeyState, [c_int], ctypes.c_short),
        (user32.SendInput, [wintypes.UINT, ctypes.c_void_p, c_int], wintypes.UINT),
        (
            user32.CreateWindowExW,
            [DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, DWORD, c_int, c_int, c_int, c_int]
            + [HWND, HANDLE, HANDLE, ctypes.c_void_p],
            HWND,
        ),
        (user32.DestroyWindow, [HWND], BOOL),
        (user32.OpenClipboard, [HWND], BOOL),
        (user32.EmptyClipboard, [], BOOL),
        (user32.SetClipboardData, [wintypes.UINT, HANDLE], HANDLE),
        (user32.CloseClipboard, [], BOOL),
        (kernel32.GlobalAlloc, [wintypes.UINT, ctypes.c_size_t], HANDLE),
        (kernel32.GlobalLock, [HANDLE], ctypes.c_void_p),
        (kernel32.GlobalUnlock, [HANDLE], BOOL),
        (kernel32.GlobalFree, [HANDLE], HANDLE),
        (kernel32.GetCurrentProcess, [], HANDLE),
        (kernel32.OpenProcess, [DWORD, BOOL, DWORD], HANDLE),
        (kernel32.CloseHandle, [HANDLE], BOOL),
        (advapi32.OpenProcessToken, [HANDLE, DWORD, pointer(HANDLE)], BOOL),
        (
            advapi32.GetTokenInformation,
            [HANDLE, c_int, ctypes.c_void_p, DWORD, pointer(DWORD)],
            BOOL,
        ),
        (advapi32.GetSidSubAuthorityCount, [ctypes.c_void_p], pointer(ctypes.c_ubyte)),
        (advapi32.GetSidSubAuthority, [ctypes.c_void_p, DWORD], pointer(DWORD)),
    ):
        function.argtypes, function.restype = argtypes, restype
    return SimpleNamespace(user32=user32, kernel32=kernel32, advapi32=advapi32)


def foreground_window() -> int | None:
    """The window in front, where typing goes (None if there is none)."""
    if sys.platform != "win32":
        return None
    return _windows().user32.GetForegroundWindow() or None


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_INTEGRITY_LEVEL = 25  # the TokenIntegrityLevel information class


def _integrity(process) -> int | None:
    """A program's integrity level: Windows' rank for who may send it keystrokes (medium for
    everyday programs, high for ones run as administrator). None if Windows won't say."""
    win = _windows()
    token = wintypes.HANDLE()
    if not win.advapi32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        size = wintypes.DWORD()
        win.advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size))
        label = ctypes.create_string_buffer(max(size.value, 64))
        if not win.advapi32.GetTokenInformation(
            token, TOKEN_INTEGRITY_LEVEL, label, len(label), ctypes.byref(size)
        ):
            return None
        sid = ctypes.cast(label, ctypes.POINTER(ctypes.c_void_p))[0]  # the label's SID
        last = win.advapi32.GetSidSubAuthorityCount(sid)[0] - 1
        return win.advapi32.GetSidSubAuthority(sid, last)[0]
    finally:
        win.kernel32.CloseHandle(token)


def runs_above_us(window: int) -> bool:
    """Whether the window's program runs at a higher integrity level than this engine (it was
    run as administrator, say). Windows drops keystrokes sent to such a window, and SendInput
    doesn't say so. Also True when Windows won't tell: the text is then copied, not lost."""
    win = _windows()
    pid = wintypes.DWORD()
    win.user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
    process = win.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not process:
        return True
    try:
        theirs = _integrity(process)
    finally:
        win.kernel32.CloseHandle(process)
    ours = _integrity(win.kernel32.GetCurrentProcess())
    return theirs is None or ours is None or theirs > ours


# Typing ------------------------------------------------------------------------------

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


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


def _no_controls(text: str) -> str:
    return "".join(" " if unicodedata.category(char) == "Cc" else char for char in text)


def printable(text: str) -> str:
    """The text as one line of plain characters: line breaks, tabs and other control
    characters become spaces, and runs of spaces one."""
    return re.sub(r"\s+", " ", _no_controls(text)).strip()


def key_events(text: str) -> list[INPUT]:
    """Keystrokes that type ``text`` as characters (any language, no keyboard layout
    involved). Control characters are typed as spaces: never Enter, which would send a chat
    message or press a dialog's default button, and never Tab."""
    events = []
    units = _no_controls(text).encode("utf-16-le")
    for index in range(0, len(units), 2):
        code = int.from_bytes(units[index : index + 2], "little")
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            events.append(INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(0, code, flags, 0, 0))))
    return events


MODIFIER_KEYS = (0x10, 0x11, 0x12, 0x5B, 0x5C)  # Shift, Ctrl, Alt, left and right Windows


def modifiers_released(timeout: float = 2.0) -> bool:
    """Waits until no modifier key is held: typed with Ctrl or Alt still down (the dictation
    keys, let go one at a time), the characters would be shortcuts in the app in front."""
    deadline = time.monotonic() + timeout
    held = _windows().user32.GetAsyncKeyState
    while any(held(key) & 0x8000 for key in MODIFIER_KEYS):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


def type_into(window: int | None, text: str) -> tuple[int, str | None]:
    """Types ``text`` into ``window`` (the one in front when the keys came up), once no
    modifier key is held. Returns how many key events Windows took and, unless all of them,
    why not: "nowhere" (no window was in front), "keys" (a modifier key stayed held),
    "switched" (another window is in front now), "admin" (that window runs as administrator)
    or "refused" (Windows took only some or none)."""
    events = key_events(text)
    if not events:
        return 0, None
    if window is None:
        return 0, "nowhere"
    if not modifiers_released():
        return 0, "keys"
    if foreground_window() != window:
        return 0, "switched"
    if runs_above_us(window):
        return 0, "admin"
    array = (INPUT * len(events))(*events)
    sent = _windows().user32.SendInput(len(events), array, ctypes.sizeof(INPUT))
    return sent, None if sent == len(events) else "refused"


CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
HWND_MESSAGE = -3  # the parent of a window that is never shown


def copy_to_clipboard(text: str) -> bool:
    """Puts ``text`` on the clipboard: only when it couldn't be typed, so it isn't lost. A
    hidden window owns it while it's set (with no owner, Windows lets SetClipboardData fail)."""
    win = _windows()
    data = text.encode("utf-16-le") + b"\0\0"
    owner = win.user32.CreateWindowExW(
        0, "STATIC", None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None
    )
    try:
        for _ in range(10):  # another program may have it open for a moment
            if win.user32.OpenClipboard(owner):
                break
            time.sleep(0.05)
        else:
            return False
        try:
            win.user32.EmptyClipboard()
            memory = win.kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not memory:
                return False
            pointer = win.kernel32.GlobalLock(memory)
            if not pointer:
                win.kernel32.GlobalFree(memory)
                return False
            ctypes.memmove(pointer, data, len(data))
            win.kernel32.GlobalUnlock(memory)
            if win.user32.SetClipboardData(CF_UNICODETEXT, memory):
                return True  # the clipboard owns the memory now
            win.kernel32.GlobalFree(memory)
            return False
        finally:
            win.user32.CloseClipboard()
    finally:
        if owner:
            win.user32.DestroyWindow(owner)


class Dictation:
    """Listens while the keys are held: start when they go down, finish when they come up.
    Each hold is its own dictation with a number. A quick second one may start while the
    first is still being finished; they are typed in the order they ended."""

    def __init__(self, transcriber: Transcriber | None = None, microphone=None) -> None:
        self.transcriber = transcriber or Transcriber()
        self._microphone = microphone  # None: the real one (looked up when used)
        self._open: dict[int, tuple[object, threading.Timer]] = {}
        self._numbers = itertools.count(1)
        self._lock = threading.Lock()
        self._in_order = ThreadPoolExecutor(1, thread_name_prefix="dictation")

    @property
    def listening(self) -> bool:
        return bool(self._open)

    def start(self, mic_name: str | None = None) -> int:
        """Starts listening and returns this dictation's number, for finish. A dictation
        whose key-up never arrives is closed MAX_SECONDS + GRACE_SECONDS later, and the oldest
        is closed when more than MAX_AT_ONCE are open, so the mic is never left open."""
        mic = (self._microphone or Microphone)(mic_name)
        with self._lock:
            number = next(self._numbers)
            expire = threading.Timer(MAX_SECONDS + GRACE_SECONDS, self.cancel, args=(number,))
            expire.daemon = True
            self._open[number] = (mic, expire)
            extra = [self._open.pop(old) for old in sorted(self._open)[:-MAX_AT_ONCE]]
        expire.start()
        for old_mic, old_expire in extra:
            old_expire.cancel()
            old_mic.stop()
        _sound("Speech On.wav")
        return number

    def finish(
        self,
        number: int | None,
        language: str | None,
        hotwords: str | None,
        fix=None,
        type_it: bool = True,
    ) -> dict:
        """Stops listening, transcribes, applies the Words to know corrections (``fix``) and
        types the text into the window that was in front when the keys came up (``number``
        None: the newest dictation). Returns the text, timings and how it was delivered."""
        window = foreground_window() if type_it else None
        with self._lock:
            if number is None and self._open:
                number = max(self._open)
            mic, expire = self._open.pop(number, (None, None))
        if mic is None:
            raise RuntimeError("Not listening")
        expire.cancel()
        audio = mic.stop()
        _sound("Speech Off.wav")
        job = self._in_order.submit(self._deliver, audio, window, language, hotwords, fix, type_it)
        return job.result()

    def _deliver(self, audio, window, language, hotwords, fix, type_it: bool) -> dict:
        began = time.perf_counter()
        text = self.transcriber.text(audio, language, hotwords)
        if fix is not None and text:
            text = fix(text)
        text = printable(text)
        typed, why = type_into(window, text + " ") if text and type_it else (0, None)
        return {
            "text": text,
            "seconds": round(audio.size / RATE, 1),
            "took": round(time.perf_counter() - began, 2),
            "typed": typed,
            "copied": bool(why) and copy_to_clipboard(text),
            "why": why,
        }

    def cancel(self, number: int | None = None) -> None:
        """Stops listening and drops what was heard: one dictation, or all of them."""
        with self._lock:
            numbers = list(self._open) if number is None else [number]
            dropped = [self._open.pop(n) for n in numbers if n in self._open]
        for mic, expire in dropped:
            expire.cancel()
            mic.stop()
