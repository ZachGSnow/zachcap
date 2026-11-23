"""Redact a moment: when a password or an MFA code was read aloud.

The stretch is silenced in every audio file of the meeting (each file rewritten whole and
moved into place, so a crash leaves either the old file or the new one, never half of
each), what was said in it leaves the transcript and the search index, and a "Who's this?"
sample that played it moves to another line. Nothing about the redacted words is logged.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path

import soundfile as sf

from . import audio, pipeline
from .store import REDACTED, Store

TRACKS = ("mic", "system")
PLAYBACK = ("playback.flac", "playback-mic.flac", "playback-system.flac")


def _patiently(action: Callable[[], object], tries: int = 15, wait: float = 0.2) -> None:
    """Run a file operation, retrying while Windows reports the file in use: the player
    lets go of a recording a moment after it is told to."""
    for attempt in range(tries):
        try:
            action()
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(wait)


def _length(path: Path) -> float:
    """A file's length in seconds."""
    info = sf.info(path)
    if info.frames >= audio.UNKNOWN_LENGTH:  # cut off by a crash: no length in the header
        return len(audio.decode_media(path)) / audio.RATE
    return info.frames / info.samplerate


def _rewrite(path: Path, start: float, end: float) -> bool:
    """Zero start..end (seconds into this file) and write the file back in its own format.
    Returns whether anything changed."""
    info = sf.info(path)
    if info.frames >= audio.UNKNOWN_LENGTH:
        # The chunk a crash cut off: FFmpeg reads it (as 16 kHz mono) and it is written
        # back finished.
        samples, rate, subtype = audio.decode_media(path)[:, None], audio.RATE, "PCM_16"
    else:
        # As integers, so every sample outside the stretch comes back bit for bit.
        samples, rate = sf.read(path, dtype="int32", always_2d=True)
        subtype = info.subtype
    first, last = max(0, round(start * rate)), min(len(samples), round(end * rate))
    if last <= first:
        return False
    samples[first:last] = 0
    partial = path.with_name(f"{path.name}.redacting")
    sf.write(partial, samples, rate, subtype=subtype, format=info.format)
    _patiently(lambda: os.replace(partial, path))
    return True


def silence(folder: Path, start: float, end: float) -> int:
    """Silence start..end (seconds into the meeting) in every audio file in ``folder``:
    recorded tracks (chunks, placed after each track's start offset in recording.json)
    and single-file ones (imported or synthetic). Returns how many files changed."""
    offsets: dict[str, float] = {}
    info_path = folder / "recording.json"
    if info_path.exists():
        try:
            tracks = json.loads(info_path.read_text(encoding="utf-8")).get("tracks", {})
            offsets = {name: float(track.get("offset", 0.0)) for name, track in tracks.items()}
        except ValueError:  # cut off while being written: the tracks start together
            offsets = {}
    changed = 0
    for name in TRACKS:
        chunks = sorted(folder.glob(f"{name}-[0-9][0-9][0-9].flac"))
        if chunks:
            begin = offsets.get(name, 0.0)
            for chunk in chunks:
                seconds = _length(chunk)
                if start < begin + seconds and end > begin:
                    changed += _rewrite(chunk, start - begin, end - begin)
                begin += seconds
            continue
        for suffix in (".flac", ".wav"):
            path = folder / f"{name}{suffix}"
            if path.exists():
                changed += _rewrite(path, start, end)
    return changed


def drop_playback(folder: Path) -> None:
    """Remove the cached player files, so they are rebuilt from the silenced tracks. Raises
    PermissionError while one is still open (being played)."""
    for path in [*(folder / name for name in PLAYBACK), *folder.glob("playback*.partial.flac")]:
        _patiently(lambda path=path: path.unlink(missing_ok=True))


def _move_samples(store: Store, meeting_id: int, start: float, end: float) -> int:
    """Speakers whose "Who's this?" sample played the redacted stretch get another of their
    lines (the best length, as when processing), or none. Returns how many changed."""
    lines = store.transcript(meeting_id)
    moved = 0
    for speaker in store.speakers(meeting_id):
        s, e = speaker["sample_start"], speaker["sample_end"]
        if s is None or e is None or not (s < end and e > start):
            continue
        spans = [
            (row["start"], row["end"])
            for row in lines
            if row["meeting_speaker_id"] == speaker["id"]
            and row["text"] != REDACTED
            and not (row["start"] < end and row["end"] > start)
        ]
        score = pipeline._sample_score  # the same choice as when the meeting was processed
        best = max(spans, key=lambda span: score(span[1] - span[0]), default=None)
        store.set_speaker_sample(speaker["id"], best)
        moved += 1
    return moved


def redact(
    store: Store, meeting_id: int, start: float, end: float, with_audio: bool = True
) -> dict:
    """Redact start..end (seconds) of a meeting: audio first (if it still exists), then the
    text, so a failure part way can simply be run again. The caller holds the player's
    locks and has refused meetings being recorded or processed."""
    folder = Path(store.meeting(meeting_id)["folder"])
    files = 0
    if with_audio and folder.is_dir():
        drop_playback(folder)
        files = silence(folder, start, end)
    removed, replaced = store.redact_lines(meeting_id, start, end)
    moved = _move_samples(store, meeting_id, start, end)
    store.scrub_deleted_text()
    return {
        "audio_files": files,
        "lines_removed": removed,
        "lines_redacted": replaced,
        "samples_moved": moved,
    }
