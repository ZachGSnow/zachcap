from pathlib import Path

import numpy as np
import soundfile as sf

from zachcap import audio


def test_times_accept_seconds_and_clock_formats():
    assert audio.parse_time("90") == 90
    assert audio.parse_time("1:30") == 90
    assert audio.parse_time("1:02:30") == 3750
    assert audio.parse_time("") is None and audio.parse_time(None) is None


def test_recording_time_comes_from_a_teams_style_file_name(tmp_path: Path):
    recording = tmp_path / "Weekly Sync-20261027_090709-Meeting Recording.mp4"
    recording.write_bytes(b"")
    assert f"{audio.recorded_at(recording):%Y-%m-%d %H:%M:%S}" == "2026-10-27 09:07:09"


def test_import_converts_to_16k_mono_and_keeps_only_the_range(tmp_path: Path):
    rate = 44100
    t = np.arange(3 * rate) / rate
    tone = np.where(t < 1.0, 0.5 * np.sin(2 * np.pi * 440 * t), 0.0).astype(np.float32)
    sf.write(tmp_path / "in.wav", np.stack([tone, tone], axis=1), rate)

    seconds = audio.import_media(tmp_path / "in.wav", tmp_path / "out.flac", start="0.5", end="2")

    data, out_rate = sf.read(tmp_path / "out.flac")
    assert out_rate == 16000 and data.ndim == 1
    assert abs(seconds - 1.5) < 0.01 and abs(len(data) / out_rate - 1.5) < 0.01
    assert np.abs(data[: int(0.4 * out_rate)]).max() > 0.2  # the tone from 0.5 to 1.0 s
    assert np.abs(data[int(0.6 * out_rate) :]).max() < 0.01  # silence after it
