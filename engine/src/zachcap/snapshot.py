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
DWMWA_EXTENDED_FRAME_BOUNDS = 9
GW_HWNDNEXT = 2
PW_RENDERFULLCONTENT = 2
PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2


def image_path(meeting_folder: Path, note_id: int) -> Path:
    return meeting_folder / FOLDER / f"note-{note_id}.png"


def _user32():
    """user32 with handle-sized results: window handles and DPI contexts are pointers."""
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
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
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _usable(user32, hwnd) -> bool:
    return bool(
        user32.IsWindowVisible(hwnd)
        and not user32.IsIconic(hwnd)
        and user32.GetWindowTextLengthW(hwnd) > 0
    )


def call_window() -> tuple[int, str] | None:
    """The window to capture: the one in front, or, when that is ZachCap itself (its camera
    button was clicked), the next real window behind it. Returns (handle, title)."""
    user32 = _user32()
    ours = _ours()
    hwnd = user32.GetForegroundWindow()
    while hwnd:
        if _usable(user32, hwnd) and _window_pid(user32, hwnd) not in ours:
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            return hwnd, title.value
        hwnd = user32.GetWindow(hwnd, GW_HWNDNEXT)
    return None


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
    """The window's picture in real pixels on any display scaling: drawn by the window
    itself, or read off the screen if it won't draw (or drew nothing)."""
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
    """Captures the call window into the meeting folder and reads its text."""
    found = call_window()
    if found is None:
        raise RuntimeError("There's no window to capture")
    hwnd, title = found
    image = grab(hwnd)
    path = image_path(meeting_folder, note_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, optimize=True)
    try:
        text = read_text(image)
    except Exception as exc:  # the picture is still worth keeping
        text = ""
        print(f"Snapshot text recognition failed: {exc}")
    return {"path": path, "text": text, "window": title, "size": image.size}
