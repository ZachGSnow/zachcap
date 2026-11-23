"""Mixing several output devices into one track by when each packet was played."""

import numpy as np

from zachcap.mixer import Mixer, Resampler

RATE = 48000


def tone(seconds: float, rate: int, freq: float = 440.0, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def packets(audio: np.ndarray, rate: int, start: float, size: float = 0.01, jitter: float = 0.0,
            gap: tuple[float, float] | None = None):  # fmt: skip
    """(stamp, samples) for 10 ms packets from ``start``; none inside ``gap`` (seconds)."""
    step = int(size * rate)
    rng = np.random.default_rng(1)
    for index in range(0, audio.size, step):
        offset = index / rate
        if gap and gap[0] <= offset < gap[1]:
            continue  # the device sent nothing here
        wobble = rng.uniform(-jitter, jitter) if jitter else 0.0
        yield start + offset + wobble, audio[index : index + step]


def loud(track: np.ndarray, threshold: float = 0.01) -> np.ndarray:
    return np.abs(track) > threshold


def test_one_device_lands_where_it_was_played_and_keeps_real_time():
    mixer = Mixer(RATE, t0=100.0)
    out = []
    for stamp, block in packets(tone(2.0, RATE), RATE, start=100.5, jitter=0.002):
        mixer.add("speakers", block, RATE, stamp, now=stamp + 0.012)
        out.append(mixer.drain(now=stamp + 0.012))
    out.append(mixer.drain(now=103.5))
    track = np.concatenate(out)

    assert track.size == round(3.5 * RATE) - mixer.delay  # real time, minus the delay
    assert abs(int(np.argmax(loud(track))) - RATE // 2) < 50  # starts at 0.5 s
    assert mixer.dropped == 0


def test_a_device_that_sends_nothing_while_silent_stays_in_place():
    mixer = Mixer(RATE, t0=0.0)
    for stamp, block in packets(tone(3.0, RATE), RATE, start=0.0, gap=(1.0, 2.0)):
        mixer.add("speakers", block, RATE, stamp, now=stamp + 0.01)
    track = mixer.drain(now=10.0)

    assert loud(track[: RATE - 500]).mean() > 0.9
    assert not loud(track[RATE + 500 : 2 * RATE - 500]).any()  # nothing invented
    assert loud(track[2 * RATE + 500 : 3 * RATE - 500]).mean() > 0.9  # back in its place


def test_a_headset_plugged_in_mid_call_joins_at_its_time_and_rate():
    mixer = Mixer(RATE, t0=0.0)
    silent = [
        ("speakers", s, b, RATE) for s, b in packets(np.zeros(4 * RATE, np.float32), RATE, 0.0)
    ]
    headset = [("headset", s, b, 44100) for s, b in packets(tone(2.0, 44100, 300), 44100, 2.0)]
    out = []
    for device, stamp, block, rate in sorted(silent + headset, key=lambda p: p[1]):
        mixer.add(device, block, rate, stamp, now=stamp + 0.01)
        out.append(mixer.drain(now=stamp + 0.01))
    out.append(mixer.drain(now=5.0))
    track = np.concatenate(out)

    assert not loud(track[: 2 * RATE - 100]).any()
    assert loud(track[2 * RATE + 500 : 4 * RATE - 500]).mean() > 0.9
    second = track[2 * RATE + 1000 : 3 * RATE + 1000]
    assert abs(int(np.argmax(np.abs(np.fft.rfft(second)))) - 300) <= 2  # 44.1 -> 48 kHz kept pitch


def test_an_unplugged_device_does_not_hold_the_track_back():
    mixer = Mixer(RATE, t0=0.0)
    for stamp, block in packets(tone(1.0, RATE), RATE, start=0.0):
        mixer.add("headset", block, RATE, stamp, now=stamp + 0.01)
    # Unplugged after 1 s: nothing more arrives, and the track still reaches "now".
    assert mixer.drain(now=6.0).size == 6 * RATE - mixer.delay


def test_two_devices_playing_at_once_are_summed():
    mixer = Mixer(RATE, t0=0.0)
    first = packets(tone(1.0, RATE, 200, 0.3), RATE, 0.0)
    second = packets(tone(1.0, RATE, 700, 0.3), RATE, 0.0)
    for (s1, b1), (s2, b2) in zip(first, second, strict=True):
        mixer.add("a", b1, RATE, s1, now=s1 + 0.01)
        mixer.add("b", b2, RATE, s2, now=s2 + 0.01)
    track = mixer.drain(now=2.0)[:RATE]

    peaks = sorted(int(i) for i in np.argsort(np.abs(np.fft.rfft(track)))[-2:])
    assert peaks == [200, 700]


def test_late_audio_is_dropped_not_shifted():
    mixer = Mixer(RATE, t0=0.0)
    mixer.drain(now=2.0)  # 1.7 s already written as silence
    mixer.add("speakers", tone(0.5, RATE), RATE, stamp=1.5, now=2.0)
    track = mixer.drain(now=3.0)

    assert mixer.dropped == round(0.2 * RATE)  # the part before 1.7 s
    assert loud(track[: round(0.3 * RATE) - 50]).mean() > 0.9  # the rest is at 1.7-2.0 s


def test_the_end_of_a_recording_is_flushed_exactly():
    mixer = Mixer(RATE, t0=0.0)
    for stamp, block in packets(tone(1.0, RATE), RATE, start=0.0):
        mixer.add("speakers", block, RATE, stamp, now=stamp + 0.01)
    written = mixer.drain(now=1.0).size
    rest = mixer.flush(until=round(1.25 * RATE))

    assert written + rest.size == round(1.25 * RATE)


def test_resampling_in_blocks_matches_resampling_at_once():
    audio = tone(1.0, 44100, 1000)
    whole = Resampler(44100, RATE)(audio)
    blocks = Resampler(44100, RATE)
    pieces = np.concatenate([blocks(audio[i : i + 441]) for i in range(0, audio.size, 441)])

    assert abs(pieces.size - RATE) <= 1
    assert np.allclose(pieces[: whole.size], whole[: pieces.size], atol=1e-5)
