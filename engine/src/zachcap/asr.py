"""Speech-to-text with faster-whisper: GPU when available, CPU otherwise."""

from __future__ import annotations

import gc

import numpy as np

from . import cpu, cuda, models
from .transcript import Word

DEFAULT_MODEL = "large-v3-turbo"


class Transcriber:
    def __init__(
        self, model: str = DEFAULT_MODEL, device: str | None = None, compute_type: str | None = None
    ) -> None:
        gpu = cuda.prepare()
        from faster_whisper import BatchedInferencePipeline

        self.device = device or ("cuda" if gpu else "cpu")
        self.compute_type = compute_type or ("int8_float16" if self.device == "cuda" else "int8")
        # On the CPU, one thread per core: faster-whisper uses 4 unless told, and on a 6-core
        # Ryzen AI 5 340 all six made large-v3-turbo 13% faster (24 -> 21 min per meeting hour).
        threads = {"cpu_threads": cpu.physical_cores()} if self.device == "cpu" else {}
        self.model = models.whisper(
            model, device=self.device, compute_type=self.compute_type, **threads
        )
        # Batching speech chunks is ~3x faster on the RTX A500 (2 min per meeting hour,
        # 2.3 GB peak); on CPU the plain decoder is the better choice.
        self.batched = BatchedInferencePipeline(model=self.model) if self.device == "cuda" else None

    def words(
        self, audio: np.ndarray, language: str | None = "en", hotwords: str | None = None
    ) -> list[Word]:
        """Timed words for 16 kHz mono audio. Silence is skipped by Whisper's VAD.
        hotwords: names and terms to listen for (see vocabulary.py)."""
        options = {
            "language": language,
            "beam_size": 5,
            "word_timestamps": True,
            "hotwords": hotwords or None,
        }
        if self.batched is not None:
            segments, _ = self.batched.transcribe(audio, batch_size=8, **options)
        else:
            segments, _ = self.model.transcribe(
                audio,
                vad_filter=True,
                # Each window is decoded on its own; stops one bad stretch from looping.
                condition_on_previous_text=False,
                **options,
            )
        return [
            Word(w.start, w.end, w.word, w.probability)
            for segment in segments
            for w in (segment.words or [])
        ]

    def close(self) -> None:
        """Free GPU memory before the next model loads (4 GB is tight)."""
        self.batched = None
        del self.model
        gc.collect()
