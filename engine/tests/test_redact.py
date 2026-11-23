import json
import shutil
from pathlib import Path

import numpy as np
import soundfile as sf

from zachcap import audio, redact
from zachcap.store import REDACTED, Store
from zachcap.voiceprint import Match


def tone(seconds: float, rate: int, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def silent(track: np.ndarray, start: float, end: float, margin: float = 0.01) -> bool:
    lo, hi = int((start + margin) * audio.RATE), int((end - margin) * audio.RATE)
    return not np.any(np.abs(track[lo:hi]) > 1e-4)


def loud(track: np.ndarray, start: float, end: float) -> bool:
    piece = track[int(start * audio.RATE) : int(end * audio.RATE)]
    return float(np.sqrt(np.mean(piece**2))) > 0.1


def recorded_meeting(folder: Path) -> None:
    """Two tracks of 48 kHz chunks; the call audio started 0.5 s after the mic."""
    folder.mkdir()
    for name, rate, subtype in (("mic", 48000, "PCM_16"), ("system", 44100, "PCM_24")):
        for index in range(2):
            sf.write(folder / f"{name}-{index:03d}.flac", tone(2.0, rate), rate, subtype=subtype)
    info = {"tracks": {"mic": {"offset": 0.0}, "system": {"offset": 0.5}}, "complete": True}
    (folder / "recording.json").write_text(json.dumps(info), encoding="utf-8")


def test_a_recorded_moment_is_silenced_on_both_tracks_where_it_was_said(tmp_path: Path):
    folder = tmp_path / "meeting"
    recorded_meeting(folder)
    before = {p.name: sf.read(p, dtype="int32")[0] for p in folder.glob("*.flac")}

    # 1.5-2.7 s into the meeting spans the end of each track's first chunk and the start of
    # its second, at different places in each file because of the offset.
    changed = redact.silence(folder, 1.5, 2.7)

    assert changed == 4
    for name in ("mic", "system"):
        track = audio.load_track(folder, name)
        assert silent(track, 1.5, 2.7)
        assert loud(track, 0.6, 1.4) and loud(track, 2.8, 3.4)
    # The files keep their format, and every sample outside the stretch is untouched.
    info = sf.info(folder / "system-000.flac")
    assert (info.samplerate, info.subtype) == (44100, "PCM_24")
    after, _ = sf.read(folder / "mic-001.flac", dtype="int32")
    first_kept = int(0.7 * 48000) + 1  # mic chunk 1 starts at 2.0 s; the stretch ends at 2.7
    assert np.array_equal(after[first_kept:], before["mic-001.flac"][first_kept:])
    assert not list(folder.glob("*.redacting"))


def test_imported_and_crash_cut_files_are_silenced_too(tmp_path: Path):
    imported = tmp_path / "imported"
    imported.mkdir()
    sf.write(imported / "system.flac", tone(3.0, 16000), 16000, subtype="PCM_16")
    assert redact.silence(imported, 1.0, 2.0) == 1
    track = audio.load_track(imported, "system")
    assert silent(track, 1.0, 2.0) and loud(track, 0.1, 0.9) and loud(track, 2.1, 2.9)

    crashed = tmp_path / "crashed"
    crashed.mkdir()
    live = crashed / "live.flac"
    with sf.SoundFile(live, "w", samplerate=16000, channels=1, subtype="PCM_16") as f:
        f.write(tone(3.0, 16000))
        f.flush()
        shutil.copyfile(live, crashed / "mic-000.flac")  # never closed: no length
    live.unlink()
    assert sf.info(crashed / "mic-000.flac").frames >= audio.UNKNOWN_LENGTH

    assert redact.silence(crashed, 0.5, 1.5) == 1
    track = audio.load_track(crashed, "mic")
    assert silent(track, 0.5, 1.5) and loud(track, 1.6, 2.4)


def make_meeting(store: Store, folder: Path) -> tuple[int, int]:
    meeting = store.create_meeting("Support call", "2026-11-22T09:00:00", folder, duration=60.0)
    caller = store.add_speaker(
        meeting, "system", "Speaker 1", None, 30.0, (10.0, 16.0), Match(None, 0.0, "unknown")
    )
    store.add_segments(
        meeting,
        [
            (caller, "system", 2.0, 8.0, "Thanks for calling, how can I help", 0.9),
            (caller, "system", 10.0, 16.0, "My password is zebrapassword42 by the way", 0.9),
            (caller, "system", 16.0, 21.0, "and the code is zebracode77 then", 0.9),
            (caller, "system", 30.0, 34.0, "Great, that worked, thank you", 0.9),
        ],
    )
    return meeting, caller


def test_the_words_leave_the_transcript_the_search_and_the_database_file(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting, caller = make_meeting(store, tmp_path / "audio")
    assert store.search("zebrapassword42") and store.search("zebracode77")

    result = redact.redact(store, meeting, 9.5, 18.0, with_audio=False)

    lines = [(row["start"], row["text"]) for row in store.transcript(meeting)]
    assert lines == [
        (2.0, "Thanks for calling, how can I help"),
        (16.0, REDACTED),  # reaches into the stretch: its words go, the line stays
        (30.0, "Great, that worked, thank you"),
    ]
    assert (result["lines_removed"], result["lines_redacted"]) == (1, 1)
    assert store.search("zebrapassword42") == [] and store.search("zebracode77") == []
    # The "Who's this?" sample played the password: it moves to another of their lines.
    speaker = store.speaker(caller)
    assert (speaker["sample_start"], speaker["sample_end"]) == (2.0, 8.0)
    store.close()
    for path in tmp_path.glob("test.db*"):
        content = path.read_bytes()
        assert b"zebrapassword42" not in content and b"zebracode77" not in content, path.name


def test_a_speaker_heard_only_in_the_stretch_has_no_sample_left(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting, _ = make_meeting(store, tmp_path / "audio")
    quiet = store.add_speaker(
        meeting, "mic", "Speaker 2", None, 4.0, (11.0, 14.0), Match(None, 0.0, "unknown")
    )
    store.add_segments(meeting, [(quiet, "mic", 11.0, 14.0, "Read it slowly", 0.9)])

    redact.redact(store, meeting, 10.0, 15.0, with_audio=False)

    assert store.speaker(quiet)["sample_start"] is None
    store.close()
