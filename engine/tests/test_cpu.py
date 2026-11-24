import ctypes
import os
import sys
import types

import pytest

from zachcap import asr, cpu


def entry(relationship: int) -> bytes:
    """One SYSTEM_LOGICAL_PROCESSOR_INFORMATION entry, laid out as on this Python."""
    pointer = ctypes.sizeof(ctypes.c_void_p)
    size = pointer + 4 + 16
    size += -size % pointer
    raw = (1).to_bytes(pointer, "little") + relationship.to_bytes(4, "little")
    return raw + bytes(size - len(raw))


def test_only_core_entries_count():
    # A 2-core processor as Windows describes it: two cores, their caches, one package.
    raw = entry(0) + entry(2) + entry(2) + entry(0) + entry(2) + entry(3)
    assert cpu._cores_from(raw) == 2
    assert cpu._cores_from(b"") == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_this_pc_has_at_least_one_core_and_no_more_than_its_processors():
    cores = cpu.physical_cores()
    assert 1 <= cores <= (os.cpu_count() or 1)


def test_a_failed_lookup_falls_back_to_half_the_processors(monkeypatch: pytest.MonkeyPatch):
    def fails():
        raise OSError("no answer")

    monkeypatch.setattr(cpu.sys, "platform", "win32")
    monkeypatch.setattr(cpu, "_processor_information", fails)
    monkeypatch.setattr(cpu.os, "cpu_count", lambda: 12)
    assert cpu.physical_cores() == 6


def test_the_cpu_transcriber_uses_a_thread_per_core(monkeypatch: pytest.MonkeyPatch):
    loaded = []
    monkeypatch.setattr(asr.cuda, "prepare", lambda: False)
    monkeypatch.setattr(asr.cpu, "physical_cores", lambda: 6)
    monkeypatch.setattr(asr.models, "whisper", lambda name, **options: loaded.append(options))
    fake = types.SimpleNamespace(BatchedInferencePipeline=lambda model: None)
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)  # CI has no speech stack
    asr.Transcriber()
    assert loaded == [{"device": "cpu", "compute_type": "int8", "cpu_threads": 6}]


def test_the_gpu_transcriber_leaves_threads_alone(monkeypatch: pytest.MonkeyPatch):
    loaded = []
    monkeypatch.setattr(asr.cuda, "prepare", lambda: True)
    monkeypatch.setattr(asr.models, "whisper", lambda name, **options: loaded.append(options))
    fake = types.SimpleNamespace(BatchedInferencePipeline=lambda model: None)
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    asr.Transcriber()
    assert loaded == [{"device": "cuda", "compute_type": "int8_float16"}]
