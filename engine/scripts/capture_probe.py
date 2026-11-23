"""Phase 0 capture probe: record the mic and the speakers (loopback) together.

Optionally plays a WAV through the speakers part-way in, then reports:

* whether loopback capture works on this machine at all;
* gaps: WASAPI loopback only delivers audio while something is rendering, so
  without a silent "keepalive" output stream the system track loses its timeline;
* echo: how much of the speaker output leaks back into the mic.

    uv run python scripts/capture_probe.py --seconds 14 --play clip.wav --out ../.scratch/probe
    uv run python scripts/capture_probe.py --no-keepalive ...   # to see the gap problem
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pyaudiowpatch as pyaudio
import soundfile as sf

FRAMES_PER_BUFFER = 1024


class InputTrack:
    """Collects int16 frames and callback timing from one input stream."""

    def __init__(self, name: str, device: dict) -> None:
        self.name = name
        self.device = device
        self.channels = int(device["maxInputChannels"])
        self.rate = int(device["defaultSampleRate"])
        self.chunks: list[bytes] = []
        self.callbacks: list[tuple[float, int]] = []
        self.status_flags = 0
        self.opened_at = 0.0
        self.closed_at = 0.0
        self.stream = None

    def _callback(self, in_data, frame_count, time_info, status):
        self.callbacks.append((time.perf_counter(), frame_count))
        self.status_flags |= status
        self.chunks.append(in_data)
        return (None, pyaudio.paContinue)

    def open(self, pa: pyaudio.PyAudio) -> None:
        self.stream = pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.rate,
            input=True,
            input_device_index=self.device["index"],
            frames_per_buffer=FRAMES_PER_BUFFER,
            stream_callback=self._callback,
        )
        self.opened_at = time.perf_counter()

    def close(self) -> None:
        if self.stream is not None:
            self.stream.stop_stream()
            self.stream.close()
            self.stream = None
            self.closed_at = time.perf_counter()

    def mono(self) -> np.ndarray:
        raw = np.frombuffer(b"".join(self.chunks), dtype=np.int16)
        return raw.reshape(-1, self.channels).astype(np.float32).mean(axis=1) / 32768.0

    def gaps(self) -> tuple[int, float]:
        """Count stretches where callbacks stopped arriving (no audio delivered)."""
        count, missing = 0, 0.0
        for (t_prev, _), (t_next, frames) in zip(self.callbacks, self.callbacks[1:], strict=False):
            expected = frames / self.rate
            if t_next - t_prev > 2.5 * expected + 0.02:
                count += 1
                missing += t_next - t_prev - expected
        return count, missing


class Player:
    """Plays a mono signal to the speakers, outputting silence before and after it."""

    def __init__(self, device: dict, signal: np.ndarray | None, start_at: float) -> None:
        self.device = device
        self.channels = int(device["maxOutputChannels"])
        self.rate = int(device["defaultSampleRate"])
        self.signal = signal
        self.start_at = start_at
        self.pos = 0
        self.stream = None

    @property
    def done(self) -> bool:
        return self.signal is None or self.pos >= len(self.signal)

    def _callback(self, in_data, frame_count, time_info, status):
        out = np.zeros((frame_count, self.channels), dtype=np.int16)
        if not self.done and time.perf_counter() >= self.start_at:
            chunk = self.signal[self.pos : self.pos + frame_count]
            out[: len(chunk)] = (np.clip(chunk, -1.0, 1.0) * 32767).astype(np.int16)[:, None]
            self.pos += len(chunk)
        return (out.tobytes(), pyaudio.paContinue)

    def open(self, pa: pyaudio.PyAudio) -> None:
        self.stream = pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.rate,
            output=True,
            output_device_index=self.device["index"],
            frames_per_buffer=FRAMES_PER_BUFFER,
            stream_callback=self._callback,
        )

    def close(self) -> None:
        if self.stream is not None:
            self.stream.stop_stream()
            self.stream.close()
            self.stream = None


def find_loopback(pa: pyaudio.PyAudio, speakers: dict) -> dict:
    for device in pa.get_loopback_device_info_generator():
        if speakers["name"] in device["name"]:
            return device
    raise RuntimeError(f"No loopback device found for {speakers['name']!r}")


def load_signal(path: Path, rate: int) -> np.ndarray:
    audio, source_rate = sf.read(path, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if source_rate != rate:
        positions = np.arange(round(len(audio) * rate / source_rate)) * source_rate / rate
        audio = np.interp(positions, np.arange(len(audio)), audio).astype(np.float32)
    return audio


def dbfs(x: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(x**2)) + 1e-9)) if x.size else -180.0


def envelope(x: np.ndarray, rate: int, hop_s: float = 0.01) -> np.ndarray:
    hop = int(rate * hop_s)
    n = len(x) // hop
    return np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1))


def echo_estimate(ref: np.ndarray, ref_rate: int, mic: np.ndarray, mic_rate: int):
    """Peak correlation of speech-activity envelopes, and the lag in ms."""
    a = np.log(envelope(ref, ref_rate) + 1e-6)
    b = np.log(envelope(mic, mic_rate) + 1e-6)
    n = min(len(a), len(b))
    if n < 100:
        return 0.0, 0
    a = (a[:n] - a[:n].mean()) / (a[:n].std() + 1e-9)
    b = (b[:n] - b[:n].mean()) / (b[:n].std() + 1e-9)
    best_corr, best_lag = -1.0, 0
    for lag in range(-100, 101):  # +/- 1 s in 10 ms steps
        x, y = (a[: n - lag], b[lag:]) if lag >= 0 else (a[-lag:], b[: n + lag])
        corr = float(np.mean(x * y))
        if corr > best_corr:
            best_corr, best_lag = corr, lag
    return best_corr, best_lag * 10


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=14.0)
    parser.add_argument("--play", type=Path, help="WAV to play through the speakers")
    parser.add_argument("--play-at", type=float, default=3.0, help="seconds in to start playback")
    parser.add_argument("--play-max", type=float, default=8.0, help="max seconds of playback")
    parser.add_argument("--no-keepalive", action="store_true", help="skip the silent output stream")
    parser.add_argument("--out", type=Path, default=Path("../.scratch/probe"))
    args = parser.parse_args()
    keepalive = not args.no_keepalive

    pa = pyaudio.PyAudio()
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        mic_dev = pa.get_device_info_by_index(wasapi["defaultInputDevice"])
        speakers = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        loop_dev = find_loopback(pa, speakers)

        signal = None
        if args.play:
            signal = load_signal(args.play, int(speakers["defaultSampleRate"]))
            signal = signal[: int(args.play_max * int(speakers["defaultSampleRate"]))]

        mic, system = InputTrack("mic", mic_dev), InputTrack("system", loop_dev)
        mic.open(pa)
        system.open(pa)
        t0 = time.perf_counter()
        player = Player(speakers, signal, start_at=t0 + args.play_at)
        if keepalive:
            player.open(pa)

        while time.perf_counter() - t0 < args.seconds:
            if not keepalive and signal is not None:
                started = time.perf_counter() >= player.start_at
                if started and player.stream is None and not player.done:
                    player.open(pa)
                elif player.stream is not None and player.done:
                    player.close()
            time.sleep(0.01)

        mic.close()
        system.close()
        player.close()
    finally:
        pa.terminate()

    args.out.mkdir(parents=True, exist_ok=True)
    report: dict = {"keepalive": keepalive, "played": str(args.play) if args.play else None}
    print(f"keepalive: {'on' if keepalive else 'off'}")
    for track in (mic, system):
        audio = track.mono()
        sf.write(args.out / f"{track.name}.flac", audio, track.rate, subtype="PCM_16")
        expected = track.rate * (track.closed_at - track.opened_at)
        gap_count, gap_seconds = track.gaps()
        info = {
            "device": track.device["name"],
            "rate": track.rate,
            "channels": track.channels,
            "frames": len(audio),
            "coverage": round(len(audio) / expected, 4) if expected else 0.0,
            "gaps": gap_count,
            "gap_seconds": round(gap_seconds, 2),
            "level_dbfs": round(dbfs(audio), 1),
            "peak_dbfs": round(float(20 * np.log10(np.abs(audio).max() + 1e-9)), 1),
            "start_offset_ms": round((track.callbacks[0][0] - t0) * 1000)
            if track.callbacks
            else None,
            "status_flags": track.status_flags,
        }
        report[track.name] = info
        print(
            f"{track.name:<7} {info['device'][:48]:<48} {track.rate} Hz x{track.channels}  "
            f"coverage {info['coverage']:.1%}  gaps {gap_count} ({gap_seconds:.1f} s)  "
            f"level {info['level_dbfs']} dBFS  peak {info['peak_dbfs']} dBFS"
        )

    corr, lag_ms = echo_estimate(system.mono(), system.rate, mic.mono(), mic.rate)
    report["echo"] = {"envelope_correlation": round(corr, 3), "lag_ms": lag_ms}
    verdict = "strong" if corr > 0.5 else "some" if corr > 0.25 else "little or none"
    print(f"echo    mic vs speakers correlation {corr:.2f} at {lag_ms:+d} ms -> {verdict} echo")
    (args.out / "probe.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"files   {args.out.resolve()}")


if __name__ == "__main__":
    main()
