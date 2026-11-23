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


def test_packets_without_a_usable_time_follow_on():
    mixer = Mixer(RATE, t0=0.0)
    mixer.add("speakers", tone(0.5, RATE), RATE, stamp=1.0, now=1.01)
    mixer.add("speakers", tone(0.5, RATE), RATE, stamp=None, now=1.52)  # time flagged wrong
    assert mixer.lanes["speakers"].next_frame == 2 * RATE  # 1.0 s + 0.5 s + 0.5 s


def test_a_burst_of_late_packets_stays_in_order():
    # A hiccup delivers several packets at once: their arrival makes them look early, but
    # they still follow on, so nothing overlaps (for times estimated on arrival).
    mixer = Mixer(RATE, t0=0.0, early_follows_on=True)
    for index in range(5):
        mixer.add("mic", tone(0.1, RATE), RATE, stamp=1.0, now=1.5 + index * 0.001)
    assert mixer.lanes["mic"].next_frame == round(1.5 * RATE)


def test_a_device_whose_times_are_stuck_doesnt_freeze_the_track():
    mixer = Mixer(RATE, t0=0.0)
    for second in range(1, 6):
        mixer.add("good", tone(1.0, RATE), RATE, stamp=second - 1.0, now=float(second))
        mixer.add("stuck", tone(0.01, RATE), RATE, stamp=0.0, now=float(second))
    assert mixer.horizon(5.0) >= round(4.5 * RATE)


def test_a_long_stall_is_written_in_slices():
    mixer = Mixer(RATE, t0=0.0)
    slices = []
    while (block := mixer.drain(now=600.0, most=RATE)).size:
        slices.append(block.size)
    assert max(slices) == RATE and sum(slices) == 600 * RATE - mixer.delay


def test_resampling_odd_block_sizes_never_reads_past_the_block():
    rng = np.random.default_rng(7)
    for rate in (22050, 32000, 44100):
        resample = Resampler(rate, RATE)
        for _ in range(3000):
            resample(rng.standard_normal(int(rng.integers(1, 700))).astype(np.float32))


def test_one_odd_time_from_the_device_is_corrected_by_the_next_packet():
    # Loopback times come from Windows: one packet stamped seconds ahead (it happened when
    # a sound started playing) must not push the rest of the call along with it.
    mixer = Mixer(RATE, t0=0.0)
    mixer.add("speakers", tone(0.01, RATE), RATE, stamp=1.00, now=1.01)
    mixer.add("speakers", tone(0.01, RATE), RATE, stamp=6.00, now=1.02)  # an odd one
    mixer.add("speakers", tone(0.01, RATE), RATE, stamp=1.02, now=1.03)
    assert mixer.lanes["speakers"].next_frame == round(1.03 * RATE)
