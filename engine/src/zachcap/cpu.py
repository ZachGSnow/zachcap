"""How many cores this PC's processor has, for work that runs on the CPU."""

from __future__ import annotations

import ctypes
import os
import sys

# SYSTEM_LOGICAL_PROCESSOR_INFORMATION.Relationship for a physical core.
RELATION_PROCESSOR_CORE = 0


def physical_cores() -> int:
    """Physical cores, not the logical processors Windows counts (two per core with
    simultaneous multithreading). Speech models run best with one thread per core."""
    logical = os.cpu_count() or 1
    if sys.platform != "win32":
        return logical
    try:
        return _cores_from(_processor_information()) or max(1, logical // 2)
    except OSError:
        return max(1, logical // 2)


def _processor_information() -> bytes:
    """What GetLogicalProcessorInformation says: one entry per core, cache and package."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    size = ctypes.c_uint32(0)
    kernel32.GetLogicalProcessorInformation(None, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value)
    if not size.value or not kernel32.GetLogicalProcessorInformation(buffer, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    return buffer.raw[: size.value]


def _cores_from(raw: bytes) -> int:
    """The number of processor-core entries in GetLogicalProcessorInformation's answer. Each
    entry is a pointer-sized mask, a 4-byte relationship and a 16-byte union, padded to the
    pointer size (32 bytes on 64-bit Windows)."""
    pointer = ctypes.sizeof(ctypes.c_void_p)
    entry = pointer + 4 + 16
    entry += -entry % pointer
    return sum(
        1
        for offset in range(0, len(raw) - entry + 1, entry)
        if int.from_bytes(raw[offset + pointer : offset + pointer + 4], "little")
        == RELATION_PROCESSOR_CORE
    )
