"""Audio helpers: every track is processed as 16 kHz mono float32."""

from __future__ import annotations

import json
import re
from datetime import datetime
from math import gcd
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

RATE = 16000
# libsndfile reports about 2**63 frames for a FLAC file whose writer never closed it.
UNKNOWN_LENGTH = 2**62


def load_mono_16k(path: Path) -> np.ndarray:
    try:
        if sf.info(path).frames >= UNKNOWN_LENGTH:
            raise ValueError(f"{path.name} was never finished")
        audio, rate = sf.read(path, dtype="float32", always_2d=True)
    except (RuntimeError, ValueError, MemoryError):
        # The chunk being written when ZachCap quit or crashed has no length in its
        # header; FFmpeg decodes whatever audio it holds.
        return decode_media(path)
    mono = audio.mean(axis=1)
    if rate != RATE:
        g = gcd(rate, RATE)
        mono = resample_poly(mono, RATE // g, rate // g).astype(np.float32)
    return mono


def load_track(folder: Path, name: str) -> np.ndarray | None:
    """A track as 16 kHz mono, aligned to the recording start.

    Recorded meetings have chunked ``name-NNN.flac`` files plus recording.json
    (with each track's start offset); imported or synthetic ones have a single
    ``name.wav`` / ``name.flac``.
    """
    chunks = sorted(folder.glob(f"{name}-[0-9][0-9][0-9].flac"))
    if chunks:
        parts = []
        for index, chunk in enumerate(chunks):
            try:
                parts.append(load_mono_16k(chunk))
            except Exception:
                # The chunk started just before a crash can hold no audio at all.
                if index < len(chunks) - 1:
                    raise
        if not parts:
            return None
        audio = np.concatenate(parts)
        info_path = folder / "recording.json"
        if info_path.exists():
            info = json.loads(info_path.read_text(encoding="utf-8"))
            offset = info["tracks"].get(name, {}).get("offset", 0.0)
            audio = np.concatenate([np.zeros(int(offset * RATE), np.float32), audio])
        return audio
    for suffix in (".flac", ".wav"):
        path = folder / f"{name}{suffix}"
        if path.exists():
            return load_mono_16k(path)
    return None


def parse_time(value: str | None) -> float | None:
    """'90', '1:30' or '1:02:30' as seconds."""
    if value is None or not str(value).strip():
        return None
    seconds = 0.0
    for part in str(value).strip().split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def decode_media(source: Path) -> np.ndarray:
    """The first audio stream of any media file as 16 kHz mono float32."""
    import av

    chunks = []
    with av.open(str(source)) as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise ValueError(f"{source.name} has no audio")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        for frame in container.decode(stream):
            chunks += [out.to_ndarray().reshape(-1) for out in resampler.resample(frame)]
        chunks += [out.to_ndarray().reshape(-1) for out in resampler.resample(None)]
    if not chunks:
        raise ValueError(f"{source.name} has no audio")
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def import_media(
    source: Path, target: Path, start: str | None = None, end: str | None = None
) -> float:
    """Decode any audio or video file (mp4, m4a, mp3, wav...) to a 16 kHz mono FLAC,
    optionally keeping only start..end. Returns the kept length in seconds.

    Decoding uses PyAV (FFmpeg built in), so no separate FFmpeg install is needed.
    """
    audio = decode_media(source)
    first = int((parse_time(start) or 0) * RATE)
    last = parse_time(end)
    audio = audio[first : int(last * RATE) if last is not None else None]
    if audio.size == 0:
        raise ValueError("The selected range has no audio")
    sf.write(target, audio, RATE, subtype="PCM_16")
    return len(audio) / RATE


def recorded_at(source: Path) -> datetime:
    """When a recording was made: from a Teams-style name (..-20261027_090709-..),
    else the file's modified time."""
    match = re.search(r"(\d{8})_(\d{6})", source.name)
    if match:
        try:
            return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").astimezone()
        except ValueError:
            pass
    return datetime.fromtimestamp(source.stat().st_mtime).astimezone()


def dbfs(audio: np.ndarray) -> float:
    if audio.size == 0:
        return -180.0
    return float(20 * np.log10(np.sqrt(np.mean(audio**2)) + 1e-9))


def clip(audio: np.ndarray, start: float, end: float) -> np.ndarray:
    return audio[int(start * RATE) : int(end * RATE)]
