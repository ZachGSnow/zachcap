import shutil
from pathlib import Path

import av
import numpy as np
import pytest
import soundfile as sf
from PIL import Image

from zachcap import audio, imports

RATE = 16000


def tone(seconds: float) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


# Kinds written with FFmpeg: its format, the audio codec, and whether there's video too
# (H.264, as in a Teams recording). The rest are written by libsndfile.
WRITTEN_BY_FFMPEG = {
    ".mp4": ("mp4", "aac", True),
    ".m4a": ("ipod", "aac", False),
    ".mov": ("mov", "aac", True),
    ".mkv": ("matroska", "aac", True),
    ".webm": ("webm", "libopus", False),
    ".aac": ("adts", "aac", False),
    ".wma": ("asf", "wmav2", False),
}


def media(path: Path, seconds: float = 1.0) -> Path:
    """A tone saved as the kind of file its name says, the way other apps save them."""
    if path.suffix not in WRITTEN_BY_FFMPEG:
        sf.write(path, tone(seconds), RATE)
        return path
    form, codec, video = WRITTEN_BY_FFMPEG[path.suffix]
    with av.open(str(path), "w", format=form) as out:
        if video:
            picture = out.add_stream("libx264", rate=10)
            picture.width, picture.height, picture.pix_fmt = 64, 48, "yuv420p"
        sound = out.add_stream(codec, rate=RATE, layout="mono")
        sound.bit_rate = 64000
        samples = (tone(seconds) * 32767).astype(np.int16)[None, :]
        frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
        frame.sample_rate, frame.pts = RATE, 0
        packets = [*sound.encode(frame), *sound.encode(None)]
        if video:
            for index in range(int(seconds * 10)):
                image = av.VideoFrame.from_ndarray(np.zeros((48, 64, 3), np.uint8), format="rgb24")
                image.pts = index
                packets += picture.encode(image)
            packets += picture.encode(None)
        for packet in packets:
            out.mux(packet)
    return path


def cut_off_copy(folder: Path, name: str, audio_data: np.ndarray) -> Path:
    """A FLAC file as the recorder leaves it when ZachCap dies mid-chunk: never closed."""
    live = folder / f"live-{name}"
    with sf.SoundFile(live, "w", samplerate=RATE, channels=1, subtype="PCM_16") as f:
        if audio_data.size:
            f.write(audio_data)
            f.flush()
        shutil.copyfile(live, folder / name)
    live.unlink()
    return folder / name


def test_a_chunk_cut_off_by_a_crash_still_loads(tmp_path: Path):
    path = cut_off_copy(tmp_path, "mic-000.flac", tone(3.0))
    assert sf.info(path).frames >= audio.UNKNOWN_LENGTH  # no length in the header

    loaded = audio.load_mono_16k(path)

    assert len(loaded) / RATE > 2.5


def test_an_empty_last_chunk_is_skipped(tmp_path: Path):
    sf.write(tmp_path / "mic-000.flac", tone(1.0), RATE, subtype="PCM_16")
    cut_off_copy(tmp_path, "mic-001.flac", np.zeros(0, np.float32))

    track = audio.load_track(tmp_path, "mic")

    assert track is not None and abs(len(track) / RATE - 1.0) < 0.01


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


@pytest.mark.parametrize("suffix", sorted(imports.MEDIA))
def test_every_kind_the_import_dialog_offers_is_read(tmp_path: Path, suffix: str):
    source = media(tmp_path / f"call{suffix}")

    seconds = audio.import_media(source, tmp_path / "out.flac")

    assert abs(seconds - 1.0) < 0.15  # AAC, Opus and WMA pad the start or the end a little


def concat_list(path: Path) -> Path:
    """An FFmpeg concat list: read as one, it plays the file it names."""
    (path.parent / "private").mkdir()
    media(path.parent / "private" / "other-call.wav")
    path.write_text("ffconcat version 1.0\nfile 'private/other-call.wav'\n", encoding="ascii")
    return path


def hls_playlist(path: Path) -> Path:
    media(path.parent / "part.aac")
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:1", "#EXTINF:1.0,", "part.aac", "#EXT-X-ENDLIST"]
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


def picture(path: Path) -> Path:
    Image.new("RGB", (8, 8)).save(path, format="PNG")
    return path


def text(path: Path) -> Path:
    path.write_text("Notes from the call with Dana Lee. " * 20, encoding="ascii")
    return path


@pytest.mark.parametrize(
    ("name", "make"),
    [
        ("voicemail.mp3", concat_list),  # would read private/other-call.wav
        ("call.m3u8", hls_playlist),  # would read part.aac
        ("photo.mp3", picture),  # FFmpeg reads a picture as a one-frame video
        ("notes.txt", text),  # and text as a video of the characters
    ],
)
def test_a_file_ffmpeg_would_read_as_another_kind_is_refused(tmp_path: Path, name: str, make):
    source = make(tmp_path / name)

    with pytest.raises(audio.Refused, match=f"^{name} isn't in a format ZachCap imports$"):
        audio.import_media(source, tmp_path / "out.flac")
    assert not (tmp_path / "out.flac").exists()


def test_a_path_is_never_taken_for_a_url_or_a_protocol(tmp_path: Path):
    wav = media(tmp_path / "call.wav")
    for url in (f"concat:{wav}|{wav}", f"subfile,,start,0,end,{wav.stat().st_size},,:{wav}"):
        with pytest.raises(OSError):  # read as a path: no such file
            audio.import_media(Path(url), tmp_path / "out.flac")


def test_a_file_over_the_size_cap_is_refused_before_ffmpeg_sees_it(tmp_path: Path, monkeypatch):
    source = media(tmp_path / "all-day.mp4")
    monkeypatch.setattr(audio, "MAX_BYTES", source.stat().st_size - 1)
    with monkeypatch.context() as patch:
        patch.setattr(av, "open", lambda *args, **kwargs: pytest.fail("FFmpeg opened it"))
        with pytest.raises(
            audio.Refused, match=r"^all-day\.mp4 is over \d+ GB, too big to import$"
        ):
            audio.import_media(source, tmp_path / "out.flac")

    monkeypatch.setattr(audio, "MAX_BYTES", source.stat().st_size)
    assert audio.import_media(source, tmp_path / "out.flac") > 0.9


def saying(path: Path, seconds: float) -> Path:
    """The FLAC file with a header that says it holds ``seconds``, whatever it holds."""
    data = bytearray(path.read_bytes())
    # After "fLaC" and the block's 4-byte header, STREAMINFO keeps the number of samples in
    # the low 36 bits of its bytes 10 to 17.
    field = int.from_bytes(data[18:26], "big") & ~(2**36 - 1) | int(seconds * RATE)
    data[18:26] = field.to_bytes(8, "big")
    path.write_bytes(bytes(data))
    return path


def test_a_file_that_says_it_is_too_long_is_refused_before_it_is_decoded(
    tmp_path: Path, monkeypatch
):
    source = saying(media(tmp_path / "workshop.flac"), 13 * 3600)
    monkeypatch.setattr(av, "AudioResampler", lambda **kwargs: pytest.fail("decoded"))

    with pytest.raises(audio.Refused, match="^workshop.flac is over 12 hours, too long to import$"):
        audio.import_media(source, tmp_path / "out.flac")


@pytest.mark.parametrize("header", ["no length", "a wrong length"])
def test_a_length_the_file_doesnt_give_is_counted_while_decoding(
    tmp_path: Path, monkeypatch, header: str
):
    if header == "no length":  # as a crash leaves a chunk
        source = cut_off_copy(tmp_path, "mic-000.flac", tone(3.0))
    else:
        sf.write(tmp_path / "mic-000.flac", tone(3.0), RATE, subtype="PCM_16")
        source = saying(tmp_path / "mic-000.flac", 1.0)
    monkeypatch.setattr(audio, "MAX_SECONDS", 2)

    with pytest.raises(audio.Refused, match="too long to import"):
        audio.decode_media(source)
