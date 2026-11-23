"""Capture what each output device plays (WASAPI loopback), through Windows Core Audio.

PortAudio lists devices once, when it starts, so a headset plugged in mid-call is invisible
to it. Core Audio's device enumerator is live, so this module asks it directly (through
comtypes). Each output device is captured on its own thread, and every packet carries the
performance-counter time of its first frame, which is what lets the mixer place it.

Windows only. Per-app capture (process loopback) was tried first: it returns silence for the
Teams desktop app (Windows-classic-samples issue 414), so every device is captured instead.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from ctypes import (
    HRESULT,
    POINTER,
    Structure,
    byref,
    c_longlong,
    c_uint32,
    c_ulong,
    c_ulonglong,
    c_ushort,
    c_void_p,
    wintypes,
)

import numpy as np


def _import_comtypes() -> None:
    """comtypes joins the importing thread to a COM apartment as it loads, and fails if that
    thread already belongs to another kind. Load it on a short-lived thread of its own, so
    no engine thread is affected; every capture thread then joins COM explicitly."""
    failed: list[BaseException] = []

    def load() -> None:
        had_flags = hasattr(sys, "coinit_flags")
        sys.coinit_flags = 0  # COINIT_MULTITHREADED, for that short-lived thread only
        try:
            import comtypes  # noqa: F401
        except BaseException as exc:
            failed.append(exc)
        finally:
            if not had_flags:
                del sys.coinit_flags

    thread = threading.Thread(target=load, name="comtypes-import")
    thread.start()
    thread.join()
    if failed:
        raise failed[0]


_import_comtypes()

import comtypes  # noqa: E402  (already loaded above, without side effects here)
from comtypes import CLSCTX_ALL, COMMETHOD, GUID, IUnknown  # noqa: E402

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
WAVE_FORMAT_IEEE_FLOAT = 3
E_RENDER = 0
E_CONSOLE = 0
DEVICE_STATE_ACTIVE = 1
STGM_READ = 0
BUFFER_100NS = 2_000_000  # 200 ms of slack; packets arrive about every 10 ms
WAIT_MS = 50  # also polled, since some systems don't signal loopback while nothing plays


class WAVEFORMATEX(Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", c_ushort),
        ("nChannels", c_ushort),
        ("nSamplesPerSec", c_ulong),
        ("nAvgBytesPerSec", c_ulong),
        ("nBlockAlign", c_ushort),
        ("wBitsPerSample", c_ushort),
        ("cbSize", c_ushort),
    ]


class PROPERTYKEY(Structure):
    _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]


class PROPVARIANT(Structure):
    """Only the string member is read (a device's friendly name)."""

    _fields_ = [
        ("vt", c_ushort),
        ("reserved1", c_ushort),
        ("reserved2", c_ushort),
        ("reserved3", c_ushort),
        ("pointer", c_void_p),
        ("extra", c_void_p),
    ]


PKEY_DEVICE_FRIENDLY_NAME = PROPERTYKEY(GUID("{a45c254e-df1c-4efd-8020-67d146a850e0}"), 14)
CLSID_MM_DEVICE_ENUMERATOR = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
    _methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "GetBuffer",
            (["out"], POINTER(POINTER(ctypes.c_byte)), "ppData"),
            (["out"], POINTER(c_uint32), "pNumFramesToRead"),
            (["out"], POINTER(wintypes.DWORD), "pdwFlags"),
            (["out"], POINTER(c_ulonglong), "pu64DevicePosition"),
            (["out"], POINTER(c_ulonglong), "pu64QPCPosition"),
        ),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "NumFramesRead")),
        COMMETHOD(
            [], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "pNumFramesInNextPacket")
        ),
    ]


class IAudioClient(IUnknown):
    _iid_ = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
    _methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "Initialize",
            (["in"], ctypes.c_int, "ShareMode"),
            (["in"], wintypes.DWORD, "StreamFlags"),
            (["in"], c_longlong, "hnsBufferDuration"),
            (["in"], c_longlong, "hnsPeriodicity"),
            (["in"], POINTER(WAVEFORMATEX), "pFormat"),
            (["in"], POINTER(GUID), "AudioSessionGuid"),
        ),
        COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(c_uint32), "pNumBufferFrames")),
        COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(c_longlong), "phnsLatency")),
        COMMETHOD(
            [], HRESULT, "GetCurrentPadding", (["out"], POINTER(c_uint32), "pNumPaddingFrames")
        ),
        COMMETHOD(
            [],
            HRESULT,
            "IsFormatSupported",
            (["in"], ctypes.c_int, "ShareMode"),
            (["in"], POINTER(WAVEFORMATEX), "pFormat"),
            (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppClosestMatch"),
        ),
        COMMETHOD(
            [], HRESULT, "GetMixFormat", (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppDeviceFormat")
        ),
        COMMETHOD(
            [],
            HRESULT,
            "GetDevicePeriod",
            (["out"], POINTER(c_longlong), "phnsDefaultDevicePeriod"),
            (["out"], POINTER(c_longlong), "phnsMinimumDevicePeriod"),
        ),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (["in"], wintypes.HANDLE, "eventHandle")),
        COMMETHOD(
            [],
            HRESULT,
            "GetService",
            (["in"], POINTER(GUID), "riid"),
            (["out"], POINTER(c_void_p), "ppv"),
        ),
    ]


class IPropertyStore(IUnknown):
    _iid_ = GUID("{886d8eeb-8cf2-4446-8d02-cdba1dbdcf99}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(wintypes.DWORD), "cProps")),
        COMMETHOD(
            [],
            HRESULT,
            "GetAt",
            (["in"], wintypes.DWORD, "iProp"),
            (["out"], POINTER(PROPERTYKEY), "pkey"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "GetValue",
            (["in"], POINTER(PROPERTYKEY), "key"),
            (["out"], POINTER(PROPVARIANT), "pv"),
        ),
    ]


class IMMDevice(IUnknown):
    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "Activate",
            (["in"], POINTER(GUID), "iid"),
            (["in"], wintypes.DWORD, "dwClsCtx"),
            (["in"], c_void_p, "pActivationParams"),
            (["out"], POINTER(c_void_p), "ppInterface"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "OpenPropertyStore",
            (["in"], wintypes.DWORD, "stgmAccess"),
            (["out"], POINTER(POINTER(IPropertyStore)), "ppProperties"),
        ),
        COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(wintypes.LPWSTR), "ppstrId")),
        COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(wintypes.DWORD), "pdwState")),
    ]


class IMMDeviceCollection(IUnknown):
    _iid_ = GUID("{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_uint32), "pcDevices")),
        COMMETHOD(
            [],
            HRESULT,
            "Item",
            (["in"], c_uint32, "nDevice"),
            (["out"], POINTER(POINTER(IMMDevice)), "ppDevice"),
        ),
    ]


class IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "EnumAudioEndpoints",
            (["in"], ctypes.c_int, "dataFlow"),
            (["in"], wintypes.DWORD, "dwStateMask"),
            (["out"], POINTER(POINTER(IMMDeviceCollection)), "ppDevices"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "GetDefaultAudioEndpoint",
            (["in"], ctypes.c_int, "dataFlow"),
            (["in"], ctypes.c_int, "role"),
            (["out"], POINTER(POINTER(IMMDevice)), "ppEndpoint"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "GetDevice",
            (["in"], wintypes.LPCWSTR, "pwstrId"),
            (["out"], POINTER(POINTER(IMMDevice)), "ppDevice"),
        ),
    ]


@contextmanager
def com_thread() -> Iterator[None]:
    """This thread joins the multithreaded COM apartment for the duration (or keeps the
    apartment it already has)."""
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        joined = True
    except OSError:
        joined = False
    try:
        yield
    finally:
        if joined:
            comtypes.CoUninitialize()


def _enumerator():
    return comtypes.CoCreateInstance(CLSID_MM_DEVICE_ENUMERATOR, IMMDeviceEnumerator, CLSCTX_ALL)


def _name(device) -> str:
    value = device.OpenPropertyStore(STGM_READ).GetValue(byref(PKEY_DEVICE_FRIENDLY_NAME))
    return ctypes.wstring_at(value.pointer) if value.pointer else "Unknown output"


def output_devices() -> list[dict]:
    """The output devices Windows has active right now: id, name, and whether it's the
    default. Fresh every call, so plugged-in headsets show up."""
    with com_thread():
        enumerator = _enumerator()
        try:
            default = enumerator.GetDefaultAudioEndpoint(E_RENDER, E_CONSOLE).GetId()
        except OSError:  # no output device at all
            default = None
        collection = enumerator.EnumAudioEndpoints(E_RENDER, DEVICE_STATE_ACTIVE)
        devices = []
        for index in range(collection.GetCount()):
            device = collection.Item(index)
            device_id = device.GetId()
            devices.append(
                {"id": device_id, "name": _name(device), "default": device_id == default}
            )
        return devices


# on_packet(device_id, mono float32 samples, sample rate, when the first frame played,
# whether the device reported a gap before this packet)
Packet = Callable[[str, np.ndarray, int, float, bool], None]


class LoopbackCapture:
    """Captures what one output device plays, on its own thread, until stopped or unplugged."""

    def __init__(self, device_id: str, name: str, on_packet: Packet) -> None:
        self.device_id = device_id
        self.name = name
        self.on_packet = on_packet
        self.error: str | None = None
        self.rate = 0
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"loopback {name}", daemon=True)

    def start(self, timeout: float = 5.0) -> None:
        """Opens the device; raises if it can't be captured."""
        self._thread.start()
        self._ready.wait(timeout)
        if self.error:
            raise RuntimeError(self.error)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run(self) -> None:
        kernel32 = ctypes.windll.kernel32
        event = None
        try:
            with com_thread():
                device = _enumerator().GetDevice(self.device_id)
                pointer = device.Activate(byref(IAudioClient._iid_), CLSCTX_ALL, None)
                client = ctypes.cast(pointer, POINTER(IAudioClient))
                mix = client.GetMixFormat().contents
                rate, channels = int(mix.nSamplesPerSec), int(mix.nChannels)
                # Float samples at the device's own rate and channels, whatever it mixes in.
                wanted = WAVEFORMATEX(
                    WAVE_FORMAT_IEEE_FLOAT, channels, rate, rate * channels * 4, channels * 4, 32, 0
                )
                flags = (
                    AUDCLNT_STREAMFLAGS_LOOPBACK
                    | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                    | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
                    | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
                )
                client.Initialize(
                    AUDCLNT_SHAREMODE_SHARED, flags, BUFFER_100NS, 0, byref(wanted), None
                )
                event = kernel32.CreateEventW(None, False, False, None)
                client.SetEventHandle(event)
                service = client.GetService(byref(IAudioCaptureClient._iid_))
                capture = ctypes.cast(service, POINTER(IAudioCaptureClient))
                self.rate = rate
                client.Start()
                self._ready.set()
                try:
                    while not self._stop.is_set():
                        kernel32.WaitForSingleObject(event, WAIT_MS)
                        while capture.GetNextPacketSize() > 0:
                            data, frames, flags_out, _position, qpc = capture.GetBuffer()
                            if flags_out & AUDCLNT_BUFFERFLAGS_SILENT or not frames:
                                mono = np.zeros(frames, np.float32)
                            else:
                                raw = ctypes.string_at(data, frames * channels * 4)
                                samples = np.frombuffer(raw, np.float32).reshape(-1, channels)
                                mono = samples.mean(axis=1)
                            capture.ReleaseBuffer(frames)
                            self.on_packet(
                                self.device_id,
                                mono,
                                rate,
                                qpc / 1e7,  # 100 ns units of the performance counter
                                bool(flags_out & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY),
                            )
                finally:
                    try:
                        client.Stop()
                    except OSError:
                        pass
        except Exception as exc:  # unplugged mid-call (device invalidated), or refused
            self.error = f"{self.name}: {exc}"
        finally:
            if event:
                kernel32.CloseHandle(event)
            self._ready.set()
