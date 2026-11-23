"""Mix what several output devices play into one track, in the right place in time.

Every packet from a device carries the time its first frame was played (on the
``time.perf_counter()`` clock). A packet goes where that time says, unless it is within a
small tolerance of where the device's previous packet ended (normal jitter, which would
otherwise click). So a device that sends nothing while it is silent, one plugged in
mid-call, and one unplugged mid-call all land where they belong. Devices are resampled to
the track's rate and summed.

The track is written up to the point every device still sending has reached, and never
past "now" minus a short delay; when nothing is sending, it is written as silence up to
that point. So it always keeps real-time length, which keeps it in sync with the mic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


class Resampler:
    """Linear interpolation that carries its position across blocks (good enough for
    speech, which is resampled to 16 kHz for transcription anyway)."""

    def __init__(self, source_rate: int, target_rate: int) -> None:
        self.step = source_rate / target_rate
        self.phase = 0.0  # where the next output sample falls, in input samples
        self.previous = 0.0  # the last input sample of the previous block

    def __call__(self, block: np.ndarray) -> np.ndarray:
        if self.step == 1.0 or block.size == 0:
            return block.astype(np.float32, copy=False)
        extended = np.concatenate(([self.previous], block)).astype(np.float32)
        length = block.size
        count = int(np.ceil((length - self.phase) / self.step)) if length > self.phase else 0
        positions = self.phase + np.arange(count) * self.step
        # Rounding can put the last position a hair past the block: stay inside it.
        index = np.minimum(np.floor(positions).astype(np.int64), length - 1)
        fraction = (positions - index).astype(np.float32)
        out = extended[index] * (1 - fraction) + extended[index + 1] * fraction
        self.phase = self.phase + count * self.step - length
        self.previous = float(block[-1])
        return out


@dataclass
class Lane:
    """One device's audio waiting to be mixed."""

    rate: int
    resample: Resampler
    next_frame: int | None = None
    last_packet_at: float = float("-inf")
    blocks: list[tuple[int, np.ndarray]] = field(default_factory=list)


class Mixer:
    """One track's timeline at ``rate``. Frame 0 is the moment the recording started (``t0``,
    on the same clock as the packet times)."""

    def __init__(
        self,
        rate: int,
        t0: float,
        tolerance: float = 0.05,
        live_for: float = 0.5,
        delay: float = 0.3,
        stuck_after: float = 1.0,
        early_follows_on: bool = False,
    ) -> None:
        self.rate = rate
        # Times measured by the device (WASAPI loopback) are trusted both ways: a packet
        # clearly early or late is placed where its time says, so one odd time is corrected
        # by the next packet. Times estimated on arrival (a PortAudio callback) aren't: a
        # burst of packets after a hiccup looks early, and follows on instead.
        self.early_follows_on = early_follows_on
        self.t0 = t0
        self.tolerance = int(tolerance * rate)
        self.live_for = live_for
        self.delay = int(delay * rate)
        self.stuck_after = int(stuck_after * rate)
        self.lanes: dict[str, Lane] = {}
        self.written = 0
        self.dropped = 0  # frames that arrived after their place was already written

    def add(
        self,
        device: str,
        samples: np.ndarray,
        device_rate: int,
        stamp: float | None,
        now: float,
        discontinuity: bool = False,
    ) -> None:
        """``samples``: mono float32 in [-1, 1] at ``device_rate``. ``stamp``: when its first
        frame was played (None when the device said its time is wrong: it then follows on
        from the previous packet). ``now``: when it arrived.

        A packet follows on from the previous one unless it starts clearly later (the
        device sent nothing for a while: the gap stays silent), clearly earlier (see
        ``early_follows_on``), or the device reported a break."""
        lane = self.lanes.get(device)
        if lane is None or lane.rate != device_rate:
            lane = self.lanes[device] = Lane(device_rate, Resampler(device_rate, self.rate))
        block = lane.resample(samples)
        if stamp is None:
            at = lane.next_frame
            if at is None:  # nothing to follow: about when it arrived
                at = round((now - self.t0) * self.rate) - block.size
        else:
            at = round((stamp - self.t0) * self.rate)
        off = 0 if lane.next_frame is None else at - lane.next_frame
        if (
            lane.next_frame is None
            or discontinuity
            or off > self.tolerance
            or (-off > self.tolerance and not self.early_follows_on)
        ):
            lane.next_frame = at
        start = lane.next_frame
        lane.next_frame = start + block.size
        lane.last_packet_at = now
        if start < self.written:  # too late for audio that was already written
            cut = min(self.written - start, block.size)
            self.dropped += cut
            block = block[cut:]
            start += cut
        if block.size:
            lane.blocks.append((start, block))

    def horizon(self, now: float) -> int:
        """How far the track can be written now: where every live device has reached, never
        past now minus the delay. A device that stopped sending, or that has fallen more than
        ``stuck_after`` behind (its times are off), doesn't hold the track back."""
        latest = round((now - self.t0) * self.rate) - self.delay
        live = [
            lane.next_frame
            for lane in self.lanes.values()
            if lane.next_frame is not None
            and now - lane.last_packet_at <= self.live_for
            and latest - lane.next_frame <= self.stuck_after
        ]
        limit = min(live) if live else latest
        return max(self.written, min(limit, latest))

    def end(self) -> int:
        """The furthest any device has reached (the end of the audio received)."""
        reached = [lane.next_frame for lane in self.lanes.values() if lane.next_frame is not None]
        return max(reached, default=self.written)

    def drain(self, now: float, most: int | None = None) -> np.ndarray:
        """Mixed audio (float32 in [-1, 1]) from the last write up to the horizon, at most
        ``most`` frames at a time (after a long stall the caller takes it in slices)."""
        end = self.horizon(now)
        if most is not None:
            end = min(end, self.written + most)
        return self._mix_until(end)

    def flush(self, until: int, most: int | None = None) -> np.ndarray:
        """Everything up to frame ``until`` (at most ``most`` frames of it per call), whether
        or not devices are still sending: the end of a recording."""
        end = max(self.written, until)
        if most is not None:
            end = min(end, self.written + most)
        return self._mix_until(end)

    def _mix_until(self, end: int) -> np.ndarray:
        count = end - self.written
        if count <= 0:
            return np.zeros(0, np.float32)
        mix = np.zeros(count, np.float32)
        for lane in self.lanes.values():
            kept = []
            for start, block in lane.blocks:
                stop = start + block.size
                if stop <= self.written:
                    continue
                lo, hi = max(start, self.written), min(stop, end)
                if hi > lo:
                    mix[lo - self.written : hi - self.written] += block[lo - start : hi - start]
                if stop > end:
                    kept.append((start, block))
            lane.blocks = kept
        np.clip(mix, -1.0, 1.0, out=mix)
        self.written = end  # only once the slice exists
        return mix
