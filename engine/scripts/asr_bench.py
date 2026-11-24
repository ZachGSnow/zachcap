"""Phase 0: time faster-whisper on this machine's GPU.

Reports model load time, speed (x real time, and minutes per hour of audio),
peak GPU memory, and word error rate against a reference.json when given.

    uv run python scripts/asr_bench.py system.wav --reference reference.json --track system
    uv run python scripts/asr_bench.py system.wav --repeat 10 --batch-size 8
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from zachcap import cuda, models


class GpuMemorySampler(threading.Thread):
    """Polls nvidia-smi for used GPU memory; CTranslate2 allocations are invisible to torch."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.peak_mb = 0
        self._done = threading.Event()

    def run(self) -> None:
        while not self._done.is_set():
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                ).stdout
                self.peak_mb = max(self.peak_mb, int(out.split()[0]))
            except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
                pass
            self._done.wait(0.5)

    def stop(self) -> int:
        self._done.set()
        self.join()
        return self.peak_mb


def load_16k_mono(path: Path) -> np.ndarray:
    audio, rate = sf.read(path, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if rate != 16000:
        positions = np.arange(round(len(audio) * 16000 / rate)) * rate / 16000
        audio = np.interp(positions, np.arange(len(audio)), audio).astype(np.float32)
    return audio


def words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split()


def word_error_rate(reference: list[str], hypothesis: list[str]) -> float:
    previous = list(range(len(hypothesis) + 1))
    for i, ref_word in enumerate(reference, 1):
        current = [i] + [0] * len(hypothesis)
        for j, hyp_word in enumerate(hypothesis, 1):
            current[j] = min(
                previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ref_word != hyp_word)
            )
        previous = current
    return previous[-1] / max(len(reference), 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("audio", type=Path)
    parser.add_argument(
        "--model",
        default="large-v3-turbo",
        help="a name pinned in zachcap/models.py, or a model folder",
    )
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--compute-type", default="int8_float16")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=0, help=">0 uses the batched pipeline")
    parser.add_argument("--repeat", type=int, default=1, help="tile the audio to test longer runs")
    parser.add_argument("--reference", type=Path, help="reference.json from make_meeting.py")
    parser.add_argument("--track", default="system")
    args = parser.parse_args()

    gpu = cuda.prepare()
    print(f"CUDA available: {gpu}")
    from faster_whisper import BatchedInferencePipeline

    audio = load_16k_mono(args.audio)
    if args.repeat > 1:
        gap = np.zeros(16000, np.float32)
        audio = np.concatenate([np.concatenate([audio, gap])] * args.repeat)
    duration = len(audio) / 16000

    sampler = GpuMemorySampler()
    sampler.start()
    started = time.perf_counter()
    model = models.whisper(args.model, device=args.device, compute_type=args.compute_type)
    load_seconds = time.perf_counter() - started

    options = {"language": "en", "beam_size": args.beam_size, "word_timestamps": True}
    started = time.perf_counter()
    if args.batch_size > 0:
        pipeline = BatchedInferencePipeline(model=model)
        segments, _ = pipeline.transcribe(audio, batch_size=args.batch_size, **options)
    else:
        segments, _ = model.transcribe(audio, vad_filter=True, **options)
    segments = list(segments)
    asr_seconds = time.perf_counter() - started
    peak_mb = sampler.stop()

    speed = duration / asr_seconds
    print(
        f"model       {args.model} ({args.compute_type}, beam {args.beam_size}, "
        f"{'batch ' + str(args.batch_size) if args.batch_size else 'sequential'})"
    )
    print(f"load        {load_seconds:.1f} s")
    print(f"audio       {duration / 60:.1f} min")
    print(
        f"transcribe  {asr_seconds:.1f} s  ->  {speed:.1f}x real time, "
        f"{60 / speed:.1f} min per hour of audio"
    )
    print(f"gpu memory  peak {peak_mb} MB used (whole GPU)")

    text = " ".join(segment.text.strip() for segment in segments)
    if args.reference and args.repeat == 1:
        reference = json.loads(args.reference.read_text(encoding="utf-8"))
        ref_text = " ".join(s["text"] for s in reference["segments"] if s["track"] == args.track)
        print(
            f"WER         {word_error_rate(words(ref_text), words(text)):.1%} "
            f"vs reference ({args.track} track)"
        )
    print("--- first lines")
    for segment in segments[:6]:
        print(f"[{segment.start:6.1f}-{segment.end:6.1f}] {segment.text.strip()}")


if __name__ == "__main__":
    main()
