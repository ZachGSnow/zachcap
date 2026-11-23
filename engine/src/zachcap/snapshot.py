"""A snapshot of the call during a meeting, with its text read on this computer.

Ctrl+Alt+S, or the camera in the recording bar, saves what the call app shows (a shared
error message, a hostname, a slide) into the meeting, and Windows' own text recognition
(Windows.Media.Ocr, part of Windows) turns it into text that summaries and Ask can use.
The image stays in the meeting folder; like the rest of the notes, only the text is ever
sent to Claude (with a summary, or for Ask).
"""

from __future__ import annotations

import asyncio
import ctypes
import os
from ctypes import wintypes
from pathlib import Path

from PIL import Image, ImageGrab

FOLDER = "snapshots"
MAX_TEXT = 4000  # characters of recognized text kept with the note
THUMBNAIL = (480, 270)  # the notes list shows this; the full picture only in the full view
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
WDA_NONE = 0
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PW_RENDERFULLCONTENT = 2
PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2


class SnapshotError(RuntimeError):
    """Why there's nothing to capture, said to the user as is."""


def image_path(meeting_folder: Path, note_id: int) -> Path:
    return meeting_folder / FOLDER / f"note-{note_id}.png"


def thumbnail_path(meeting_folder: Path, note_id: int) -> Path:
    return meeting_folder / FOLDER / f"note-{note_id}.thumb.jpg"


def remove_pictures(meeting_folder: Path, note_id: int) -> None:
    """A snapshot's picture and its thumbnail (the note keeps its text unless deleted)."""
    for path in (image_path(meeting_folder, note_id), thumbnail_path(meeting_folder, note_id)):
        path.unlink(missing_ok=True)


def _user32():
    """user32 with handle-sized results: window handles and DPI contexts are pointers."""
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    return user32


def _ours() -> set[int]:
    """ZachCap's own processes: this engine and the app that started it."""
    pids = {os.getpid()}
    parent = os.environ.get("ZACHCAP_PARENT_PID")
    if parent and parent.isdigit():
        pids.add(int(parent))
    return pids


def _window_pid(user32, hwnd) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return pid.value


def _title(user32, hwnd) -> str:
    title = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(wintypes.HWND(hwnd), title, 512)
    return title.value


def _cloaked(hwnd) -> bool:
    """Hidden by Windows though "visible": on another virtual desktop, or a suspended app."""
    value = wintypes.DWORD()
    found = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value)
    )
    return found == 0 and value.value != 0


def _protected(user32, hwnd) -> bool:
    """The app asked Windows to keep it out of screenshots (a meeting app's "prevent screen
    capture", a password manager): respected, never worked around."""
    affinity = wintypes.DWORD()
    return bool(user32.GetWindowDisplayAffinity(wintypes.HWND(hwnd), ctypes.byref(affinity))) and (
        affinity.value != WDA_NONE
    )


def _shown(user32, hwnd) -> bool:
    """A real window someone can see: visible, titled and not cloaked."""
    return bool(
        user32.IsWindowVisible(wintypes.HWND(hwnd))
        and user32.GetWindowTextLengthW(wintypes.HWND(hwnd)) > 0
        and not _cloaked(hwnd)
    )


def _process_path(pid: int) -> str:
    """The program a process runs (its full path), or "" if Windows won't say."""
    kernel32 = ctypes.WinDLL("kernel32")  # its own copy: handle-sized results set here only
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _top_windows(user32) -> list[int]:
    """Every top-level window, front to back."""
    found: list[int] = []
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(
        lambda hwnd, _: found.append(hwnd) or True
    )
    user32.EnumWindows(callback, 0)
    return found


def _meeting_window(user32, ours: set[int]) -> tuple[int, str] | None:
    """The front-most window of the meeting app holding the microphone now: a call app's
    (Teams, Zoom) before a browser's, and a browser's only when it has just the one window
    (any other could show anything). Raises SnapshotError when the call's windows are all
    minimized, or it's in a browser with several windows."""
    from . import calls

    keys = calls.meeting_app_keys()
    if not keys:
        return None
    found: list[tuple[int, str, bool]] = []  # front to back: handle, title, a browser's
    minimized = False
    for hwnd in _top_windows(user32):
        if not hwnd or not _shown(user32, hwnd):
            continue
        pid = _window_pid(user32, hwnd)
        if pid in ours:
            continue
        path = _process_path(pid)
        key = next((key for key in keys if path and calls.key_matches(key, path)), None)
        if key is None:
            continue
        if user32.IsIconic(wintypes.HWND(hwnd)):
            minimized = True
            continue
        found.append((hwnd, _title(user32, hwnd), calls.is_browser(key)))
    apps = [window for window in found if not window[2]]
    if apps or len(found) == 1:
        hwnd, title, _ = (apps or found)[0]
        return hwnd, title
    if found:
        raise SnapshotError(
            "The call is in a browser with several windows open: bring the call's window to "
            "the front, then press Ctrl+Alt+S"
        )
    if minimized:
        raise SnapshotError("The call window is minimized: restore it, then take the snapshot")
    return None


def call_window() -> tuple[int, str]:
    """The window to capture. The one in front, when it isn't ZachCap (Ctrl+Alt+S from the
    call, or from a window someone shared); when ZachCap is in front (its camera was
    clicked), the window of the meeting app holding the microphone. Never just "the next
    window behind", which could be an email. Returns (handle, title); raises SnapshotError
    with the reason when there's nothing right to capture."""
    user32 = _user32()
    ours = _ours()
    front = user32.GetForegroundWindow()
    if (
        front
        and _window_pid(user32, front) not in ours
        and _shown(user32, front)
        and not user32.IsIconic(wintypes.HWND(front))
    ):
        chosen = front, _title(user32, front)
    else:
        chosen = _meeting_window(user32, ours)
    if chosen is None:
        raise SnapshotError(
            "No call window to capture: bring the window you want to the front, then press "
            "Ctrl+Alt+S"
        )
    if _protected(user32, chosen[0]):
        raise SnapshotError(f"{chosen[1] or 'That window'} is protected from screen capture")
    return chosen


def _bounds(hwnd) -> tuple[int, int, int, int]:
    """The window's visible edges (without the invisible resize border Windows adds)."""
    rect = wintypes.RECT()
    found = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)
    )
    if found != 0:
        ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def _print_window(hwnd, window: tuple[int, int, int, int]) -> Image.Image | None:
    """The window drawn by itself (PrintWindow with full content, which also covers
    Chromium and WebView2 apps like Teams), so a window partly behind ZachCap, or a screen
    that is off, still comes out right. None if the window wouldn't draw."""
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    left, top, right, bottom = window
    width, height = right - left, bottom - top
    screen = user32.GetDC(None)
    memory = gdi32.CreateCompatibleDC(screen)
    bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
    old = gdi32.SelectObject(memory, bitmap)
    try:
        if not user32.PrintWindow(wintypes.HWND(hwnd), memory, PW_RENDERFULLCONTENT):
            return None
        header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0)
        pixels = ctypes.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(memory, bitmap, 0, height, pixels, ctypes.byref(header), 0):
            return None
        return Image.frombuffer("RGB", (width, height), pixels.raw, "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(memory, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory)
        user32.ReleaseDC(None, screen)


def grab(hwnd) -> Image.Image:
    """The window's picture in real pixels on any display scaling, drawn by the window
    itself. Only when it won't draw (or draws nothing) and it is the window in front is the
    screen read instead: for any other window that would capture whatever covers it."""
    user32 = _user32()
    previous = user32.SetThreadDpiAwarenessContext(PER_MONITOR_AWARE_V2)
    try:
        left, top, right, bottom = _bounds(hwnd)
        if right - left < 10 or bottom - top < 10:
            raise RuntimeError("That window is too small to capture")
        window = wintypes.RECT()
        user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(window))
        outer = (window.left, window.top, window.right, window.bottom)
        drawn = _print_window(hwnd, outer)
        if drawn is not None and drawn.getextrema() != ((0, 0), (0, 0), (0, 0)):
            # Without the invisible resize border Windows adds around the window.
            return drawn.crop(
                (left - outer[0], top - outer[1], right - outer[0], bottom - outer[1])
            )
        if user32.GetForegroundWindow() != hwnd:
            raise SnapshotError("That window didn't let itself be captured")
        return ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
    finally:
        if previous:
            user32.SetThreadDpiAwarenessContext(previous)


async def _recognize(image: Image.Image) -> str:
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("Windows has no text recognition language installed")
    # Windows reads images up to OcrEngine.max_image_dimension on a side.
    largest = OcrEngine.max_image_dimension
    if max(image.size) > largest:
        image = image.copy()
        image.thumbnail((largest, largest))
    red, green, blue, alpha = image.convert("RGBA").split()
    writer = DataWriter()
    writer.write_bytes(Image.merge("RGBA", (blue, green, red, alpha)).tobytes())
    bitmap = SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(),
        BitmapPixelFormat.BGRA8,
        image.width,
        image.height,
        BitmapAlphaMode.PREMULTIPLIED,
    )
    result = await engine.recognize_async(bitmap)
    return "\n".join(line.text for line in result.lines)


def read_text(image: Image.Image) -> str:
    """The text in the image, line by line (Windows' own recognizer, on this computer)."""
    return asyncio.run(_recognize(image))[:MAX_TEXT]


def take(meeting_folder: Path, note_id: int) -> dict:
    """Captures the call window into the meeting folder (the picture and a thumbnail for the
    notes list) and reads its text."""
    hwnd, title = call_window()
    image = grab(hwnd)
    path = image_path(meeting_folder, note_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, optimize=True)
    small = image.convert("RGB")
    small.thumbnail(THUMBNAIL)
    small.save(thumbnail_path(meeting_folder, note_id), quality=80)
    try:
        text = read_text(image)
    except Exception as exc:  # the picture is still worth keeping
        text = ""
        print(f"Snapshot text recognition failed: {exc}")
    return {"path": path, "text": text, "window": title, "size": image.size}
