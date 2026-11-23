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
        index = np.floor(positions).astype(np.int64)
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
    ) -> None:
        self.rate = rate
        self.t0 = t0
        self.tolerance = int(tolerance * rate)
        self.live_for = live_for
        self.delay = int(delay * rate)
        self.lanes: dict[str, Lane] = {}
        self.written = 0
        self.dropped = 0  # frames that arrived after their place was already written

    def add(
        self,
        device: str,
        samples: np.ndarray,
        device_rate: int,
        stamp: float,
        now: float,
        discontinuity: bool = False,
    ) -> None:
        """``samples``: mono float32 in [-1, 1] at ``device_rate``. ``stamp``: when its first
        frame was played. ``now``: when it arrived."""
        lane = self.lanes.get(device)
        if lane is None or lane.rate != device_rate:
            lane = self.lanes[device] = Lane(device_rate, Resampler(device_rate, self.rate))
        block = lane.resample(samples)
        at = round((stamp - self.t0) * self.rate)
        if lane.next_frame is None or discontinuity or abs(at - lane.next_frame) > self.tolerance:
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
        """How far the track can be written now."""
        latest = round((now - self.t0) * self.rate) - self.delay
        live = [
            lane.next_frame
            for lane in self.lanes.values()
            if lane.next_frame is not None and now - lane.last_packet_at <= self.live_for
        ]
        limit = min(live) if live else latest
        return max(self.written, min(limit, latest))

    def drain(self, now: float) -> np.ndarray:
        """Mixed audio (float32 in [-1, 1]) from the last write up to the horizon."""
        return self._mix_until(self.horizon(now))

    def flush(self, until: int) -> np.ndarray:
        """Everything up to frame ``until``, whether or not devices are still sending (the
        end of a recording)."""
        return self._mix_until(max(self.written, until))

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
        self.written = end
        return np.clip(mix, -1.0, 1.0)
