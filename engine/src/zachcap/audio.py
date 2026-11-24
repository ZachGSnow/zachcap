"""Audio helpers: every track is processed as 16 kHz mono float32."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from math import gcd
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import numpy as np
import soundfile as sf

from .watchdog import LOW_DISK_BYTES

if TYPE_CHECKING:
    from av.container import InputContainer

RATE = 16000
# libsndfile reports about 2**63 frames for a FLAC file whose writer never closed it.
UNKNOWN_LENGTH = 2**62


class Allowed(NamedTuple):
    """What FFmpeg may read, in its own names: the kinds of file (its demuxers) and the
    audio in them (its decoders), each a comma-separated list."""

    formats: str
    decoders: str


IMPORTS = Allowed(
    # The kinds the Import dialog offers (imports.MEDIA): "mov" reads .mp4, .m4a and .mov,
    # "matroska" .mkv and .webm, "asf" .wma, and "aac" a raw .aac stream. A demuxer with
    # several names, such as "mov,mp4,m4a,3gp,3g2,mj2", is let in by any one of them.
    formats="mov,mp3,wav,flac,ogg,matroska,aac,asf",
    # The audio that meeting apps, recorders and phone systems save in those: AAC (ALAC in
    # a lossless .m4a), MP3, Opus, Vorbis, FLAC, WMA, PCM, and the G.711, GSM, ADPCM, G.722,
    # G.726 and G.729 of phone systems' .wav files. No decoder for video, pictures or
    # subtitles ever runs, nor one for the audio of films and music (AC-3, DTS, WMA Pro).
    decoders=(
        "aac,alac,mp3float,opus,vorbis,flac,wmav1,wmav2,"
        "pcm_u8,pcm_s16le,pcm_s16be,pcm_s24le,pcm_s24be,pcm_s32le,pcm_f32le,pcm_f64le,"
        "pcm_mulaw,pcm_alaw,gsm_ms,adpcm_ima_wav,adpcm_ms,g722,g726,g729"
    ),
)
# ZachCap's own tracks: FLAC chunks, or a single FLAC or WAV file. These are decoded in the
# engine's own process (load_track, the playback cache, redaction): ZachCap wrote them
# itself. Media the user brings in (IMPORTS) is decoded in a child process (decode_child).
TRACKS = Allowed(formats="flac,wav", decoders="flac,pcm_s16le,pcm_s24le,pcm_s32le,pcm_f32le")
# Well past any meeting recording: Teams ends one at 4 hours or 1.5 GB and starts another,
# and 8 GB is 12 hours of video at about 1.5 Mbps. Twelve hours of audio is 1.4 GB once
# decoded (2.8 GB as float32), so the length is also what bounds memory.
MAX_BYTES = 8 * 2**30
MAX_SECONDS = 12 * 3600
# What decoding and importing leave free on a drive: a recording may be running on it, whose
# watch warns below this.
SPARE = LOW_DISK_BYTES


class Refused(ValueError):
    """A file ZachCap won't import: of a kind it doesn't read, with no audio or audio it
    doesn't read, too big or too long, or one that crashed the decoder or took it too long
    to read (decode_child). The message says which, for the user."""


class NoRoom(Refused):
    """A file there isn't the free space to decode: ``size`` bytes of audio go on the drive
    ``folder`` is on, and SPARE stays free there besides. It's not the file's doing, so the
    watch folder tries it again once there's room."""

    def __init__(self, name: str, folder: Path, size: int) -> None:
        self.folder, self.needs = folder, size + SPARE
        needs = math.ceil(self.needs / 2**20)
        super().__init__(f"{name} needs at least {needs:,} MB free on drive {folder.drive}")

    def over(self) -> bool:
        """Whether there's room now (or no telling, in which case it's worth a try)."""
        try:  # an import's own folder is removed when it fails: the one it was in, then
            folder = next(f for f in (self.folder, *self.folder.parents) if f.is_dir())
            return shutil.disk_usage(folder).free >= self.needs
        except (OSError, StopIteration):
            return True


def check_room(name: str, folder: Path, size: int = 0) -> None:
    """Raises NoRoom unless there's room for ``size`` bytes of a file's audio."""
    lacking = NoRoom(name, folder, size)
    if not lacking.over():
        raise lacking


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
        # Imported here: scipy takes most of a second to load, which the decoder's child
        # process (decode_child), started for every import, never needs.
        from scipy.signal import resample_poly

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
def open_media(path: Path, allowed: Allowed) -> Iterator[InputContainer]:
    """The file opened with FFmpeg, kept to the ``allowed`` kinds of file and decoders and
    to local files. Everything ZachCap decodes with FFmpeg is opened here.

    Python opens the file and FFmpeg reads it through that file object, so the path is never
    taken for a URL, a protocol ("http:", "concat:", "subfile,") or a numbered image
    sequence, and the size checked is that of the file being read. FFmpeg names the format
    from the first bytes and refuses one that isn't listed before reading the file as that
    format, so a playlist or concat list renamed .mp3 (which would make it open other files)
    goes no further. FFmpeg opens decoders in two places and both are told the list: while
    it looks at the streams (it decodes a frame of each, the video of a Teams recording
    included) and when a stream is decoded. Raises Refused for a format that isn't listed,
    for a device or a pipe, and for a file over MAX_BYTES.
    """
    import av

    with open(path, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise Refused(f"{path.name} isn't a file")
        if info.st_size > MAX_BYTES:
            raise Refused(f"{path.name} is over {MAX_BYTES // 2**30} GB, too big to import")
        options = {
            "protocol_whitelist": "file",
            "format_whitelist": allowed.formats,
            "codec_whitelist": allowed.decoders,  # for the frames decoded while looking
        }
        try:
            # container_options, not options: PyAV hands options to every stream's decoder
            # too. FFmpeg answers a format that isn't on the list with "Invalid argument".
            container = av.open(file, container_options=options)
        except av.error.ArgumentError as exc:
            raise Refused(f"{path.name} isn't in a format ZachCap imports") from exc
        with container:
            # PyAV opens the decoder that decodes a stream with that stream's own options,
            # which container_options don't reach.
            for stream in container.streams:
                if stream.codec_context is not None:
                    stream.codec_context.options = {"codec_whitelist": allowed.decoders}
            yield container


def decode_pieces(
    source: Path, allowed: Allowed, starting: Callable[[float], object] | None = None
) -> Iterator[np.ndarray]:
    """The first audio stream of a media file as 16 kHz mono int16, a piece at a time as
    FFmpeg decodes it. ``starting`` is told how long the file says it is, in seconds (0 if it
    doesn't say), before any of it is decoded. Raises Refused for a file open_media won't
    open, one with no audio or with audio that isn't allowed, and one that holds over
    MAX_SECONDS of audio (once that much has come out, if the file doesn't say so first)."""
    import av

    too_long = f"{source.name} is over {MAX_SECONDS // 3600} hours, too long to import"
    pieces = 0
    with open_media(source, allowed) as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise Refused(f"{source.name} has no audio")
        decoder = stream.codec_context
        if decoder is None or decoder.name not in allowed.decoders.split(","):
            # FFmpeg would refuse to open it too, only later and as "Invalid argument".
            raise Refused(f"{source.name} has its audio in a format ZachCap doesn't import")
        # The length the file gives is checked before anything is decoded. What is decoded
        # is counted as well: a file cut off by a crash gives no length, and a file can lie.
        seconds = (container.duration or 0) / av.time_base
        if seconds > MAX_SECONDS:
            raise Refused(too_long)
        if starting is not None:
            starting(seconds)
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        kept = 0
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                pieces += 1
                kept += out.samples
                yield out.to_ndarray().reshape(-1)
            if kept > MAX_SECONDS * RATE:
                raise Refused(too_long)
        for out in resampler.resample(None):
            pieces += 1
            yield out.to_ndarray().reshape(-1)
    if not pieces:
        raise Refused(f"{source.name} has no audio")


def decode_media(source: Path, allowed: Allowed = TRACKS) -> np.ndarray:
    """The first audio stream of a media file as 16 kHz mono float32, from ZachCap's own
    tracks unless ``allowed`` says otherwise. Raises Refused as decode_pieces does."""
    audio = np.concatenate(list(decode_pieces(source, allowed))).astype(np.float32)
    audio /= 32768.0  # in place, so hours of audio aren't held twice
    return audio


def import_media(
    source: Path, target: Path, start: str | None = None, end: str | None = None
) -> float:
    """Decode an audio or video file of a kind the Import dialog offers (mp4, m4a, mp3,
    wav...) to a 16 kHz mono FLAC, optionally keeping only start..end. Returns the kept
    length in seconds. Raises Refused, with the reason, for a file it won't read, and NoRoom
    (one of those) when the decoder's drive or the FLAC's hasn't the free space.

    Decoding uses PyAV (FFmpeg built in), so no separate FFmpeg install is needed. It runs
    in a child process (decode_child), so a file that crashes or hangs FFmpeg ends only that
    process, never the engine.
    """
    from .decode_child import decode  # imported here: it imports this module

    audio = decode(source)
    first = int((parse_time(start) or 0) * RATE)
    last = parse_time(end)
    audio = audio[first : int(last * RATE) if last is not None else None]
    if audio.size == 0:
        raise ValueError("The selected range has no audio")
    check_room(source.name, target.parent, audio.nbytes)  # FLAC takes about half of that
    sf.write(target, audio, RATE, subtype="PCM_16")  # int16, as decoded: nothing is lost
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
