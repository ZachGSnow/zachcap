import shutil
from pathlib import Path
from types import SimpleNamespace

import av
import numpy as np
import pytest
import soundfile as sf
from PIL import Image

from zachcap import audio, decode_child, imports

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
    return encode(path, form, codec, video, seconds)


def encode(
    path: Path,
    form: str,
    codec: str | None,
    video: bool = False,
    seconds: float = 1.0,
    rate: int = RATE,
) -> Path:
    """A tone written by FFmpeg as ``form`` with ``codec`` (its names; None for no sound),
    with black H.264 video as in a Teams recording if ``video``."""
    with av.open(str(path), "w", format=form) as out:
        packets = []
        if video:
            picture = out.add_stream("libx264", rate=10)
            picture.width, picture.height, picture.pix_fmt = 64, 48, "yuv420p"
        if codec:
            sound = out.add_stream(codec, rate=rate, layout="mono")
            sound.bit_rate = 64000
            samples = (tone(seconds) * 32767).astype(np.int16)[None, :]
            frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
            frame.sample_rate, frame.pts = RATE, 0
            packets += [*sound.encode(frame), *sound.encode(None)]
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


def test_an_import_keeps_every_sample_as_decoded(tmp_path: Path):
    # Decoded as int16 and stored as 16-bit FLAC: every value there is comes through unchanged.
    samples = np.random.default_rng(1).permutation(np.arange(-32768, 32768)).astype(np.int16)
    sf.write(tmp_path / "in.wav", samples, RATE, subtype="PCM_16")

    audio.import_media(tmp_path / "in.wav", tmp_path / "out.flac")

    assert np.array_equal(sf.read(tmp_path / "out.flac", dtype="int16")[0], samples)
    assert np.array_equal(audio.load_mono_16k(tmp_path / "out.flac"), samples / np.float32(32768))


def test_an_import_leaves_space_to_spare_on_the_drive_it_goes_to(tmp_path: Path, monkeypatch):
    # The decoder's temp folder can be on another drive than the recordings.
    free = {"GB": 10}
    monkeypatch.setattr(decode_child, "decode", lambda source: np.ones(RATE * 3600, np.int16))
    monkeypatch.setattr(
        shutil, "disk_usage", lambda folder: SimpleNamespace(free=free["GB"] * 2**30)
    )
    folder = tmp_path / "2026-11-21_093000-import"
    folder.mkdir()
    free["GB"] = 0.5  # an hour of audio is 110 MB, and 500 MB is kept for a recording

    with pytest.raises(audio.NoRoom, match=r"^call\.mp4 needs at least 610 MB free on drive .:$"):
        audio.import_media(tmp_path / "call.mp4", folder / "system.flac")

    assert list(folder.iterdir()) == []
    free["GB"] = 0.6
    assert audio.import_media(tmp_path / "call.mp4", folder / "system.flac") == 3600


def test_whether_there_is_room_is_still_known_once_the_import_is_gone(tmp_path: Path, monkeypatch):
    asked = []

    def usage(folder):
        asked.append(Path(folder))
        return SimpleNamespace(free=audio.SPARE)

    monkeypatch.setattr(shutil, "disk_usage", usage)
    gone = tmp_path / "2026-11-21_093000-import"  # removed when its import failed

    assert not audio.NoRoom("call.mp4", gone, 1).over()
    assert audio.NoRoom("call.mp4", gone, 0).over()
    assert asked == [tmp_path, tmp_path]


@pytest.mark.parametrize("suffix", sorted(imports.MEDIA))
def test_every_kind_the_import_dialog_offers_is_read(tmp_path: Path, suffix: str):
    source = media(tmp_path / f"call{suffix}")

    seconds = audio.import_media(source, tmp_path / "out.flac")

    assert abs(seconds - 1.0) < 0.15  # AAC, Opus and WMA pad the start or the end a little


@pytest.mark.parametrize(
    ("decoder", "name", "how"),
    [
        # .wav files at the telephone rate, written by libsndfile in these encodings
        ("pcm_u8", "call.wav", "PCM_U8"),
        ("pcm_s24le", "call.wav", "PCM_24"),
        ("pcm_s32le", "call.wav", "PCM_32"),
        ("pcm_f32le", "call.wav", "FLOAT"),
        ("pcm_f64le", "call.wav", "DOUBLE"),
        ("pcm_mulaw", "call.wav", "ULAW"),
        ("pcm_alaw", "call.wav", "ALAW"),
        ("gsm_ms", "call.wav", "GSM610"),
        ("adpcm_ima_wav", "call.wav", "IMA_ADPCM"),
        ("adpcm_ms", "call.wav", "MS_ADPCM"),
        ("g726", "call.wav", "G721_32"),
        # written by FFmpeg: its names for the kind of file and the codec
        ("g722", "call.wav", "wav/g722"),
        ("alac", "memo.m4a", "ipod/alac"),
        ("pcm_s16be", "clip.mov", "mov/pcm_s16be"),
        ("pcm_s24be", "clip.mov", "mov/pcm_s24be"),
        ("wmav1", "call.wma", "asf/wmav1"),
    ],
)
def test_the_audio_phone_systems_and_recorders_save_is_read(
    tmp_path: Path, decoder: str, name: str, how: str
):
    path = tmp_path / name
    if "/" in how:
        encode(path, *how.split("/"))
    else:
        sf.write(path, tone(1.0)[::2], 8000, subtype=how)
    with audio.open_media(path, audio.IMPORTS) as container:
        assert container.streams.audio[0].codec_context.name == decoder

    assert abs(audio.import_media(path, tmp_path / "out.flac") - 1.0) < 0.15


def test_every_decoder_allowed_is_one_for_audio_that_this_ffmpeg_has():
    for allowed in (audio.IMPORTS, audio.TRACKS):
        for name in allowed.decoders.split(","):
            assert av.codec.Codec(name, "r").type == "audio", name


def test_a_teams_recording_imports_its_audio_and_its_video_is_never_decoded(tmp_path: Path):
    source = media(tmp_path / "Weekly sync-20261121_093000-Meeting Recording.mp4")
    with audio.open_media(source, audio.IMPORTS) as container:
        (picture,), (sound,) = container.streams.video, container.streams.audio
        # FFmpeg learns a stream's pixel or sample format by decoding a frame of it.
        assert picture.codec_context.pix_fmt is None
        assert sound.codec_context.format is not None

    assert abs(audio.import_media(source, tmp_path / "out.flac") - 1.0) < 0.15


class Counted:
    """A file that counts what FFmpeg reads of it."""

    def __init__(self, file) -> None:
        self.file, self.name, self.count = file, file.name, 0
        self.seek, self.tell = file.seek, file.tell

    def read(self, size: int) -> bytes:
        data = self.file.read(size)
        self.count += len(data)
        return data


def video_recording(
    path: Path,
    form: str,
    options: dict[str, str] | None = None,
    first: str = "",
    tracks: int = 1,
    late: float = 0.0,
    seconds: float = 4.0,
) -> Path:
    """A recording that's mostly its picture (noise, as JPEGs), with ``tracks`` of audio that
    start ``late`` seconds in. FFmpeg lays it out by time, unless one kind goes ``first``."""
    noise = np.random.default_rng(1)
    with av.open(str(path), "w", format=form, options=options or {}) as out:
        picture = out.add_stream("mjpeg", rate=10)
        picture.width, picture.height, picture.pix_fmt = 320, 240, "yuvj420p"
        picture.bit_rate = 10_000_000
        sounds = [out.add_stream("aac", rate=RATE, layout="mono") for _ in range(tracks)]
        packets = {"audio": [], "video": []}
        for number, sound in enumerate(sounds):
            samples = tone(seconds - late) * (1 + number) / 2
            frame = av.AudioFrame.from_ndarray(
                (samples * 32767).astype(np.int16)[None, :], format="s16", layout="mono"
            )
            frame.sample_rate, frame.pts = RATE, int(late * RATE)
            packets["audio"] += [*sound.encode(frame), *sound.encode(None)]
        for index in range(int(seconds * 10)):
            image = noise.integers(0, 255, (240, 320, 3), np.uint8)
            frame = av.VideoFrame.from_ndarray(image, format="rgb24")
            frame.pts = index
            packets["video"] += picture.encode(frame)
        packets["video"] += picture.encode(None)
        both = sorted(
            packets["audio"] + packets["video"],
            key=lambda p: (first not in ("", p.stream.type), p.dts * p.time_base),
        )
        for packet in both:
            out.mux(packet)
    return path


@pytest.mark.parametrize("form", ["mp4", "mov"])
def test_only_the_audio_of_a_video_recording_is_read(tmp_path: Path, monkeypatch, form: str):
    # On a mapped drive over a slow link, reading the video too took so long that the decoder
    # was ended for taking too long to read. Laid out a second of each at a time (Windows' own
    # writer takes a third of a second).
    source = video_recording(tmp_path / f"Weekly sync.{form}", form, {"chunk_duration": "1000000"})
    opened: list[Counted] = []
    real = av.open

    def counting(file, **options):
        opened.append(Counted(file))
        return real(opened[-1], **options)

    monkeypatch.setattr(av, "open", counting)

    samples = audio.decode_media(source, audio.IMPORTS)

    assert abs(len(samples) / RATE - 4.0) < 0.15
    (read,) = opened
    assert read.count < 0.5 * source.stat().st_size


@pytest.mark.parametrize(
    ("name", "form", "how"),
    [
        ("call.mp4", "mp4", {}),
        ("call.mp4", "mp4", {"options": {"movflags": "faststart"}}),
        ("call.mp4", "mp4", {"options": {"movflags": "frag_keyframe+empty_moov"}}),
        ("call.mp4", "mp4", {"options": {"chunk_duration": "1000000"}}),
        ("call.mp4", "mp4", {"first": "audio"}),
        ("call.mp4", "mp4", {"first": "video"}),
        ("call.mp4", "mp4", {"tracks": 2}),  # the first is the one that's imported
        ("call.mp4", "mp4", {"late": 2.0}),
        ("call.mov", "mov", {}),
        ("call.mkv", "matroska", {}),
        ("call.mkv", "matroska", {"first": "video"}),
        ("call.mkv", "matroska", {"late": 2.0, "tracks": 2}),
    ],
)
def test_skipping_the_rest_leaves_the_audio_as_it_was(tmp_path: Path, name, form, how):
    source = video_recording(tmp_path / name, form, **how)
    whole = []  # decoded with everything in the file read, and nothing skipped
    with av.open(str(source)) as container:
        sound = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        for frame in [*container.decode(sound), None]:
            whole += [out.to_ndarray().reshape(-1) for out in resampler.resample(frame)]

    pieces = list(audio.decode_pieces(source, audio.IMPORTS))

    assert np.array_equal(np.concatenate(pieces), np.concatenate(whole))
    assert len(np.concatenate(pieces)) / RATE > 1.9


def test_a_file_with_only_video_is_refused(tmp_path: Path):
    source = encode(tmp_path / "screen.mp4", "mp4", None, video=True)

    with pytest.raises(audio.Refused, match="^screen.mp4 has no audio$"):
        audio.import_media(source, tmp_path / "out.flac")


@pytest.mark.parametrize(
    ("name", "form", "codec", "rate"),
    [("film.mkv", "matroska", "ac3", 48000), ("broadcast.wav", "wav", "mp2", RATE)],
)
def test_audio_in_a_format_zachcap_doesnt_import_is_refused(
    tmp_path: Path, name: str, form: str, codec: str, rate: int
):
    source = encode(tmp_path / name, form, codec, rate=rate)

    with pytest.raises(
        audio.Refused, match=f"^{name} has its audio in a format ZachCap doesn't import$"
    ):
        audio.import_media(source, tmp_path / "out.flac")
    assert not (tmp_path / "out.flac").exists()


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
    # Here, where the caps are checked: an import does it in a child process, which a cap
    # changed by a test doesn't reach.
    source = media(tmp_path / "all-day.mp4")
    monkeypatch.setattr(audio, "MAX_BYTES", source.stat().st_size - 1)
    with monkeypatch.context() as patch:
        patch.setattr(av, "open", lambda *args, **kwargs: pytest.fail("FFmpeg opened it"))
        with pytest.raises(
            audio.Refused, match=r"^all-day\.mp4 is over \d+ GB, too big to import$"
        ):
            audio.decode_media(source, audio.IMPORTS)

    monkeypatch.setattr(audio, "MAX_BYTES", source.stat().st_size)
    assert len(audio.decode_media(source, audio.IMPORTS)) / RATE > 0.9


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
        audio.decode_media(source, audio.IMPORTS)  # here: the patch doesn't reach a child


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


def not_here(*args, **kwargs):
    pytest.fail("FFmpeg opened the file in the engine's own process")


def test_an_import_is_decoded_in_a_child_process(tmp_path: Path, monkeypatch):
    # FFmpeg never opens a file the user brings in inside the engine, so a crafted or broken
    # one can only crash or hang the child.
    source = media(tmp_path / "Weekly sync-20261121_093000-Meeting Recording.mp4")
    monkeypatch.setattr(av, "open", not_here)

    seconds = audio.import_media(source, tmp_path / "out.flac", start="0.25")

    data, rate = sf.read(tmp_path / "out.flac")
    assert abs(seconds - 0.75) < 0.15 and rate == RATE and data.ndim == 1
    assert np.abs(data).max() > 0.2  # the tone, kept from 0.25 s on


@pytest.mark.parametrize(
    ("name", "make", "message"),
    [
        ("screen.mp4", lambda path: encode(path, "mp4", None, video=True), "has no audio"),
        (
            "film.mkv",
            lambda path: encode(path, "matroska", "ac3", rate=48000),
            "has its audio in a format ZachCap doesn't import",
        ),
        ("voicemail.mp3", concat_list, "isn't in a format ZachCap imports"),
        (
            "workshop.flac",
            lambda path: saying(media(path), 13 * 3600),
            "is over 12 hours, too long to import",
        ),
    ],
)
def test_a_refusal_from_the_child_reads_as_it_did(
    tmp_path: Path, monkeypatch, name: str, make, message: str
):
    source = make(tmp_path / name)
    monkeypatch.setattr(av, "open", not_here)

    with pytest.raises(audio.Refused) as refused:
        audio.import_media(source, tmp_path / "out.flac")

    assert str(refused.value) == f"{name} {message}"
    assert not (tmp_path / "out.flac").exists()
