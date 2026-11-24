"""Audio helpers: every track is processed as 16 kHz mono float32."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from math import gcd
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

if TYPE_CHECKING:
    from av.container import InputContainer

RATE = 16000
# libsndfile reports about 2**63 frames for a FLAC file whose writer never closed it.
UNKNOWN_LENGTH = 2**62

# What FFmpeg may read, by its demuxer names (a demuxer with several names, such as
# "mov,mp4,m4a,3gp,3g2,mj2", is let in by any one of them). Imports take the kinds the
# Import dialog offers (imports.MEDIA): "mov" reads .mp4, .m4a and .mov, "matroska" .mkv and
# .webm, "asf" .wma, and "aac" a raw .aac stream. ZachCap's own tracks are FLAC chunks, or
# a single FLAC or WAV file.
IMPORT_FORMATS = "mov,mp3,wav,flac,ogg,matroska,aac,asf"
TRACK_FORMATS = "flac,wav"
# Well past any meeting recording: Teams ends one at 4 hours or 1.5 GB and starts another,
# and 8 GB is 12 hours of video at about 1.5 Mbps. Twelve hours of audio is 2.8 GB once
# decoded, so the length is also what bounds memory.
MAX_BYTES = 8 * 2**30
MAX_SECONDS = 12 * 3600


class Refused(ValueError):
    """A file ZachCap won't decode: not in a format it reads, or too big or too long. The
    message says which, for the user."""


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


@contextmanager
def open_media(path: Path, formats: str) -> Iterator[InputContainer]:
    """The file opened with FFmpeg, kept to ``formats`` (FFmpeg's demuxer names) and to
    local files. Everything ZachCap decodes with FFmpeg is opened here.

    Python opens the file and FFmpeg reads it through that file object, so the path is never
    taken for a URL, a protocol ("http:", "concat:", "subfile,") or a numbered image
    sequence, and the size checked is that of the file being read. FFmpeg names the format
    from the first bytes and refuses one that isn't listed before reading the file as that
    format, so a playlist or concat list renamed .mp3 (which would make it open other files)
    goes no further. Raises Refused for that, for a device or a pipe, and for a file over
    MAX_BYTES.
    """
    import av

    with open(path, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise Refused(f"{path.name} isn't a file")
        if info.st_size > MAX_BYTES:
            raise Refused(f"{path.name} is over {MAX_BYTES // 2**30} GB, too big to import")
        try:
            # container_options, not options: PyAV hands options to every stream's decoder
            # too. FFmpeg answers a format that isn't on the list with "Invalid argument".
            container = av.open(
                file, container_options={"protocol_whitelist": "file", "format_whitelist": formats}
            )
        except av.error.ArgumentError as exc:
            raise Refused(f"{path.name} isn't in a format ZachCap imports") from exc
        with container:
            yield container


def decode_media(source: Path, formats: str = TRACK_FORMATS) -> np.ndarray:
    """The first audio stream of a media file as 16 kHz mono float32. ``formats`` are the
    ones it may be in (ZachCap's own tracks unless told otherwise). Raises Refused for a
    file open_media won't open, or one that holds over MAX_SECONDS of audio."""
    import av

    too_long = f"{source.name} is over {MAX_SECONDS // 3600} hours, too long to import"
    chunks = []
    with open_media(source, formats) as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise ValueError(f"{source.name} has no audio")
        # The length the file gives is checked before anything is decoded. What is decoded
        # is counted as well: a file cut off by a crash gives no length, and a file can lie.
        if (container.duration or 0) / av.time_base > MAX_SECONDS:
            raise Refused(too_long)
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        kept = 0
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
                kept += out.samples
            if kept > MAX_SECONDS * RATE:
                raise Refused(too_long)
        chunks += [out.to_ndarray().reshape(-1) for out in resampler.resample(None)]
    if not chunks:
        raise ValueError(f"{source.name} has no audio")
    audio = np.concatenate(chunks).astype(np.float32)
    audio /= 32768.0  # in place, so hours of audio aren't held twice
    return audio


def import_media(
    source: Path, target: Path, start: str | None = None, end: str | None = None
) -> float:
    """Decode an audio or video file of a kind the Import dialog offers (mp4, m4a, mp3,
    wav...) to a 16 kHz mono FLAC, optionally keeping only start..end. Returns the kept
    length in seconds. Raises Refused, with the reason, for a file it won't read.

    Decoding uses PyAV (FFmpeg built in), so no separate FFmpeg install is needed.
    """
    audio = decode_media(source, IMPORT_FORMATS)
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


def even_out(track: np.ndarray, target: float = 0.5, max_gain: float = 50.0) -> np.ndarray:
    """A track scaled so its loud moments sit at ``target``, for listening back. Loopback is
    recorded after the Windows volume, so a call at low volume comes up to the level of the
    mic. At most ``max_gain`` (34 dB), so a track with nothing on it stays quiet."""
    if track.size == 0:
        return track
    sample = np.abs(track[:: max(1, track.size // 1_000_000)])  # fast on hour-long tracks
    loud = float(np.percentile(sample, 99.9))
    if loud <= 0:
        return track
    return (track * min(max_gain, target / loud)).astype(np.float32)


def dbfs(audio: np.ndarray) -> float:
    if audio.size == 0:
        return -180.0
    return float(20 * np.log10(np.sqrt(np.mean(audio**2)) + 1e-9))


def clip(audio: np.ndarray, start: float, end: float) -> np.ndarray:
    return audio[int(start * RATE) : int(end * RATE)]
